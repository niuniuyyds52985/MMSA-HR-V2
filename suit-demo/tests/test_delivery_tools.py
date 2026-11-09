import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from delivery_tools import backup_data, restore_data, source_files, build_source, doctor
from interview_core import InterviewCore, digest


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.data = self.root / 'original'
        (self.data / 'media').mkdir(parents=True)
        self.media = self.data / 'media' / 'synthetic.wav'
        self.media.write_bytes(b'synthetic fixture; no person or real recording')
        self.snapshot = {'run_id': 'r1', 'cloud_authorized': True, 'answers': [{'_media_path': str(self.media)}]}
        with closing(sqlite3.connect(self.data / 'interviews.sqlite3')) as db, db:
            db.executescript('''
            CREATE TABLE sessions(id TEXT PRIMARY KEY,candidate_hash TEXT,hr_hash TEXT);
            CREATE TABLE answers(id TEXT,path TEXT,media_hash TEXT);
            CREATE TABLE jobs(id TEXT,status TEXT,error TEXT,lease_token TEXT,lease_until REAL);
            CREATE TABLE research_runs(id TEXT,input TEXT,status TEXT,error TEXT,lease_token TEXT,lease_until REAL);
            CREATE TABLE operations(scope TEXT,key TEXT,payload_hash TEXT,response TEXT,PRIMARY KEY(scope,key));
            CREATE TABLE reports(session_id TEXT,version INTEGER,payload TEXT);
            CREATE TABLE decision_events(id TEXT,payload TEXT);
            INSERT INTO sessions VALUES('s1','original-candidate-hash','original-hr-hash');
            INSERT INTO jobs VALUES('j1','queued',NULL,'private-live-lease',9999999999);
            ''')
            db.execute('INSERT INTO answers VALUES(?,?,?)', ('a1', str(self.media), hashlib.sha256(self.media.read_bytes()).hexdigest()))
            db.execute('INSERT INTO research_runs VALUES(?,?,?,?,?,?)', ('r1', json.dumps(self.snapshot), 'running', None, 'private-live-research-lease', 9999999999))
            db.execute('INSERT INTO reports VALUES(?,?,?)', ('s1', 4, '{"formal_score":3,"evidence":"synthetic business record"}'))
            db.execute('INSERT INTO decision_events VALUES(?,?)', ('d1', '{"branch_id":"synthetic-followup"}'))
        (self.data / '.token-secret').write_bytes(b'x' * 32)

    def tearDown(self):
        self.temp.cleanup()

    def make_backup(self):
        return Path(backup_data(self.data, self.root / 'backups')['archive'])

    def legacy_operation(self, path):
        candidate = hashlib.sha256(b'synthetic old candidate capability').hexdigest()
        hr = hashlib.sha256(b'synthetic old hr capability').hexdigest()
        key = 'synthetic-old-creation-recovery-key'
        payload = {'candidate_ref': 'SIM-legacy-cache'}
        response = {'session_id': 's1', 'candidate_token': candidate, 'hr_token': hr,
                    'candidate_url': '/interview#session=s1&token=' + candidate,
                    'hr_url': '/hr-review#session=s1&token=' + hr}
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('INSERT INTO operations VALUES(?,?,?,?)', ('create', key, digest(payload), json.dumps(response)))
            db.execute('UPDATE sessions SET candidate_hash=?,hr_hash=? WHERE id=?',
                       (hashlib.sha256(candidate.encode()).hexdigest(), hashlib.sha256(hr.encode()).hexdigest(), 's1'))
        return key, payload, response

    def assert_cache_removed_business_kept(self, path, secret):
        with closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            self.assertEqual(db.execute('SELECT count(*) FROM operations').fetchone()[0], 0)
            self.assertIsNone(InterviewCore._idempotent(None, db, 'create', secret[0], secret[1]))
            self.assertEqual(json.loads(db.execute('SELECT payload FROM reports').fetchone()[0])['formal_score'], 3)
            self.assertEqual(json.loads(db.execute('SELECT payload FROM decision_events').fetchone()[0])['branch_id'], 'synthetic-followup')
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM answers').fetchone()[0], 1)
        raw = Path(path).read_bytes()
        for value in [secret[0], *secret[2].values()]:
            if value != 's1':
                self.assertNotIn(value.encode(), raw, 'removed capability must not remain in SQLite free pages')

    def test_backup_removes_legacy_capabilities_and_cache_but_preserves_source_and_business(self):
        source = self.data / 'interviews.sqlite3'
        secret = self.legacy_operation(source)
        original = source.read_bytes()
        archive = self.make_backup()
        self.assertEqual(source.read_bytes(), original)
        copy = self.root / 'sanitized.sqlite3'
        with zipfile.ZipFile(archive) as bundle:
            copy.write_bytes(bundle.read('interviews.sqlite3'))
            manifest = json.loads(bundle.read('BACKUP-MANIFEST.json'))
        self.assertEqual(manifest['operation_cache']['removed_rows'], 1)
        self.assertTrue(manifest['operation_cache']['freed_pages_removed'])
        self.assert_cache_removed_business_kept(copy, secret)

    def test_restore_clears_legacy_unsanitized_backup_and_cannot_replay_old_links(self):
        archive = self.make_backup()
        with zipfile.ZipFile(archive) as bundle:
            members = {name: bundle.read(name) for name in bundle.namelist()}
        legacy_db = self.root / 'legacy.sqlite3'
        legacy_db.write_bytes(members['interviews.sqlite3'])
        secret = self.legacy_operation(legacy_db)
        members['interviews.sqlite3'] = legacy_db.read_bytes()
        manifest = json.loads(members['BACKUP-MANIFEST.json'])
        manifest.pop('operation_cache')
        for item in manifest['files']:
            if item['path'] == 'interviews.sqlite3':
                item.update(sha256=hashlib.sha256(members['interviews.sqlite3']).hexdigest(), bytes=len(members['interviews.sqlite3']))
        members['BACKUP-MANIFEST.json'] = json.dumps(manifest).encode()
        old_archive = self.root / 'old-unsanitized.zip'
        with zipfile.ZipFile(old_archive, 'w') as bundle:
            for name, data in members.items():
                bundle.writestr(name, data)
        original_archive = old_archive.read_bytes()
        target = self.root / 'restored-old'
        result = restore_data(old_archive, target)
        self.assertTrue(result['operation_cache_cleared'])
        self.assertEqual(result['removed_operation_rows'], 1)
        self.assertEqual(old_archive.read_bytes(), original_archive)
        self.assert_cache_removed_business_kept(target / 'interviews.sqlite3', secret)
        with closing(sqlite3.connect(target / 'interviews.sqlite3')) as db:
            stored = db.execute('SELECT candidate_hash,hr_hash FROM sessions WHERE id=?', ('s1',)).fetchone()
        for index, role in enumerate(('candidate', 'hr')):
            self.assertNotEqual(stored[index], hashlib.sha256(secret[2][role + '_token'].encode()).hexdigest())

    def test_backup_restore_preserves_evidence_rotates_access_rebases_media(self):
        original_db = (self.data / 'interviews.sqlite3').read_bytes()
        archive = self.make_backup()
        self.assertEqual((self.data / 'interviews.sqlite3').read_bytes(), original_db)
        with zipfile.ZipFile(archive) as bundle:
            self.assertNotIn('.token-secret', bundle.namelist())
            self.assertEqual(set(bundle.namelist()), {'interviews.sqlite3', 'media/synthetic.wav', 'BACKUP-MANIFEST.json'})
            self.assertNotIn(b'private-live-lease', bundle.read('interviews.sqlite3'))
        target = self.root / 'restored'
        result = restore_data(archive, target)
        self.assertTrue(result['session_tokens_rotated'])
        self.assertNotEqual((target / '.token-secret').read_bytes(), b'x' * 32)
        self.assertEqual((target / 'media/synthetic.wav').read_bytes(), self.media.read_bytes())
        with closing(sqlite3.connect(target / 'interviews.sqlite3')) as db, db:
            self.assertEqual(db.execute('SELECT path FROM answers').fetchone()[0], str(target / 'media/synthetic.wav'))
            self.assertNotEqual(db.execute('SELECT candidate_hash FROM sessions').fetchone()[0], 'original-candidate-hash')
            self.assertEqual(db.execute('SELECT status,lease_token FROM jobs').fetchone(), ('blocked', None))
            self.assertEqual(db.execute('SELECT status FROM research_runs').fetchone()[0], 'failed')
            self.assertEqual(json.loads(db.execute('SELECT input FROM research_runs').fetchone()[0]), self.snapshot)

    def test_restore_refuses_existing_target(self):
        archive = self.make_backup()
        target = self.root / 'already-exists'
        target.mkdir()
        sentinel = target / 'keep.txt'
        sentinel.write_text('keep', encoding='utf-8')
        with self.assertRaises(ValueError):
            restore_data(archive, target)
        self.assertEqual(sentinel.read_text(), 'keep')

    def test_invalid_hash_or_zip_traversal_refused_before_target_exists(self):
        archive = self.make_backup()
        for name, change in [('tampered', False), ('traversal', True)]:
            corrupt = self.root / (name + '.zip')
            with zipfile.ZipFile(archive) as source, zipfile.ZipFile(corrupt, 'w') as output:
                for member in source.namelist():
                    content = source.read(member)
                    output.writestr(member, b'tampered' if member.startswith('media/') and not change else content)
                if change:
                    output.writestr('../outside.txt', b'unsafe')
            target = self.root / name
            with self.assertRaises(ValueError):
                restore_data(corrupt, target)
            self.assertFalse(target.exists())
            self.assertFalse((self.root / 'outside.txt').exists())

    def test_backup_rejects_media_outside_declared_directory(self):
        outsider = self.root / 'outside.wav'
        outsider.write_bytes(b'outside')
        with closing(sqlite3.connect(self.data / 'interviews.sqlite3')) as db, db:
            db.execute('UPDATE answers SET path=?', (str(outsider),))
        with self.assertRaises(ValueError):
            self.make_backup()

    def observation_artifact(self):
        run_id, answer_id, artifact_id = '0' * 32, '1' * 32, '2' * 32
        value = {'run_id': artifact_id, 'answer_id': answer_id,
                 'artifact': 'multimodal-' + artifact_id + '.json', 'modules': {
                     'audio': {'status': 'complete', 'observations': [{'start_ms': 0, 'end_ms': 1000, 'values': {'fixture': 1}}]}}}
        relative = f"research-work/{run_id}/{answer_id}/{value['artifact']}"
        path = self.data / relative
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(value), encoding='utf-8')
        with closing(sqlite3.connect(self.data / 'interviews.sqlite3')) as db, db:
            db.execute('ALTER TABLE research_runs ADD COLUMN result TEXT')
            db.execute('UPDATE research_runs SET id=?,result=?', (run_id, json.dumps({'multimodal': [value]})))
        return relative, path, value

    def test_declared_observation_artifact_survives_restore_byte_for_byte(self):
        relative, path, _ = self.observation_artifact()
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        result = backup_data(self.data, self.root / 'out')
        self.assertEqual(result['research_artifacts'], 1)
        target = self.root / 'restored-artifact'
        restored = restore_data(result['archive'], target)
        self.assertEqual(restored['research_artifacts_preserved'], 1)
        self.assertEqual(hashlib.sha256((target / relative).read_bytes()).hexdigest(), checksum)

    def test_missing_legacy_artifact_is_explicit_not_reconstructed(self):
        _, path, _ = self.observation_artifact()
        path.unlink()
        result = backup_data(self.data, self.root / 'out')
        self.assertEqual(result['research_artifacts'], 0)
        self.assertEqual(result['missing_research_artifacts'], 1)
        restored = restore_data(result['archive'], self.root / 'restored-missing-artifact')
        self.assertEqual(restored['missing_research_artifacts'], 1)

    def test_original_au_artifact_is_preserved_under_exact_nested_path(self):
        relative, path, value = self.observation_artifact()
        original = {'run_id': '3' * 32, 'status': 'complete', 'model_id': 'synthetic-AU-Net',
                    'observations': [], 'summary': {'sampled_windows': 0}}
        value['modules']['au_net'] = {**original, 'version': 'synthetic', 'usage': 'observed_only'}
        nested = path.parent / 'original-au-net' / ('au-net-' + original['run_id'] + '.json')
        nested.parent.mkdir()
        nested.write_text(json.dumps(original), encoding='utf-8')
        path.write_text(json.dumps(value), encoding='utf-8')
        with closing(sqlite3.connect(self.data / 'interviews.sqlite3')) as db, db:
            db.execute('UPDATE research_runs SET result=?', (json.dumps({'multimodal': [value]}),))
        result = backup_data(self.data, self.root / 'out')
        self.assertEqual(result['research_artifacts'], 2)
        target = self.root / 'restored-original-au'
        restored = restore_data(result['archive'], target)
        self.assertEqual(restored['research_artifacts_preserved'], 2)
        self.assertEqual((target / nested.relative_to(self.data)).read_bytes(), nested.read_bytes())

    def test_modified_observation_artifact_is_rejected(self):
        _, path, value = self.observation_artifact()
        value['modules']['audio']['observations'][0]['values']['fixture'] = 2
        path.write_text(json.dumps(value), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '冻结结果'):
            self.make_backup()

    def test_new_model_artifacts_survive_restore_without_traversal(self):
        _, path, value = self.observation_artifact()
        paths = []
        for name, digit in [('gaze360', '4'), ('emotic', '5')]:
            model = {'run_id': digit * 32, 'artifact': name + '-' + digit * 32 + '.json',
                'status': 'complete', 'model_id': 'synthetic-' + name, 'observations': [],
                'summary': {'sampled_windows': 0}, 'usage': 'observed_only'}
            value['modules'][name] = model
            nested = path.parent / name / model['artifact']
            nested.parent.mkdir()
            nested.write_text(json.dumps(model), encoding='utf-8')
            paths.append(nested)
        path.write_text(json.dumps(value), encoding='utf-8')
        with closing(sqlite3.connect(self.data / 'interviews.sqlite3')) as db, db:
            db.execute('UPDATE research_runs SET result=?', (json.dumps({'multimodal': [value]}),))
        result = backup_data(self.data, self.root / 'out')
        target = self.root / 'restored-new-models'
        restored = restore_data(result['archive'], target)
        self.assertEqual(restored['research_artifacts_preserved'], 3)
        for nested in paths:
            self.assertEqual((target / nested.relative_to(self.data)).read_bytes(), nested.read_bytes())
        value['modules']['gaze360']['artifact'] = '../outside.json'
        with closing(sqlite3.connect(self.data / 'interviews.sqlite3')) as db, db:
            db.execute('UPDATE research_runs SET result=?', (json.dumps({'multimodal': [value]}),))
        with self.assertRaisesRegex(ValueError, '引用无效'):
            self.make_backup()

    def test_legacy_au_completion_stamp_difference_does_not_hide_changed_measurement(self):
        _, path, value = self.observation_artifact()
        original = {'run_id': '6' * 32, 'status': 'complete', 'model_id': 'synthetic-AU-Net',
            'completed_at': '2026-10-25T01:00:00Z', 'summary': {},
            'observations': [{'start_ms': 0, 'end_ms': 1000, 'values': {'AU01': .4}, 'quality': 'valid'}]}
        frozen = json.loads(json.dumps(original))
        frozen['completed_at'] = '2026-10-25T01:00:00.02Z'
        frozen['observations'][0]['completed_at'] = frozen['completed_at']
        value['modules']['au_net'] = frozen
        nested = path.parent / 'original-au-net' / ('au-net-' + original['run_id'] + '.json')
        nested.parent.mkdir()
        nested.write_text(json.dumps(original), encoding='utf-8')
        path.write_text(json.dumps(value), encoding='utf-8')
        with closing(sqlite3.connect(self.data / 'interviews.sqlite3')) as db, db:
            db.execute('UPDATE research_runs SET result=?', (json.dumps({'multimodal': [value]}),))
        result = backup_data(self.data, self.root / 'out')
        self.assertEqual(result['research_artifacts'], 2)
        original['observations'][0]['values']['AU01'] = .5
        nested.write_text(json.dumps(original), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '冻结结果'):
            self.make_backup()

    def test_observation_artifact_traversal_is_rejected(self):
        _, _, value = self.observation_artifact()
        value['artifact'] = '../../../outside.json'
        with closing(sqlite3.connect(self.data / 'interviews.sqlite3')) as db, db:
            db.execute('UPDATE research_runs SET result=?', (json.dumps({'multimodal': [value]}),))
        with self.assertRaisesRegex(ValueError, '引用无效'):
            self.make_backup()

    def test_source_package_allowlist_excludes_runtime_secrets_weights_and_local_import(self):
        workspace = self.root / 'workspace'
        app = workspace / 'suit-demo'
        for name in ['interview_core.py', 'interview_forms.py', 'interview_conclusion.py', 'interview_routing.py',
                     'interview_runtime.py', 'update-interview.py', 'update-interview.ps1',
                     'config/interview.v2.json', 'config/interview.v4.json', 'config/form-rubrics.v1.json',
                     'tests/test_interview_forms.py', 'tests/test_interview_pdf.py', 'site/interview.html', 'UPSTREAM.json',
                     'tests/fixtures/team-text-contract.v1.json', 'check_upgrade_contracts_live.py',
                     'site/js/resumable-upload.js', 'site/css/candidate-flow.css', 'site/css/review-workspace.css',
                     'site/css/multimodal-view.css', 'site/js/multimodal-view.js',
                     'interview_au_net.py', 'fetch_original_au_net.py', 'models/original-au-net/assets.json',
                     'models/original-au-net/LICENSE', 'models/original-au-net/README.md', 'models/original-au-net/weights.pth',
                     'models/openface-2.2.0/Copyright.txt', 'models/openface-2.2.0/OpenFace-license.txt', 'models/openface-2.2.0/FeatureExtraction.exe',
                     '.secrets-user/bailian.dpapi.json', 'interview-data/interviews.sqlite3',
                     'models/base.pt', 'local-data/report.json', 'qwenwork/mcp-import.local.json',
                     'qwenwork/prepare-qwenwork.ps1', 'qwenwork/mcp-import.example.json']:
            path = app / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}', encoding='utf-8')
        (workspace / 'docs').mkdir()
        (workspace / '启动面试工作台.cmd').write_text('Synthetic portable launcher.', encoding='utf-8')
        (workspace / '应用面试工作台更新.cmd').write_text('Synthetic controlled updater.', encoding='utf-8')
        (workspace / 'docs/qwenwork-local-demo.md').write_text('Synthetic local demo.', encoding='utf-8')
        (workspace / 'docs/usability-fix-20261024.md').write_text('Synthetic delivery note.', encoding='utf-8')
        (workspace / 'docs/focused-delivery-20261027.md').write_text('Synthetic v4 acceptance.', encoding='utf-8')
        (workspace / 'docs/qwenwork-v1-deployment.md').write_text('Synthetic deployment.', encoding='utf-8')
        (workspace / 'VERSION').write_text('1.0.0\n', encoding='utf-8')
        for name in ['LICENSE', 'README.md', 'CHANGELOG.md', 'NOTICE.md', '.gitignore']:
            (workspace / name).write_text('Synthetic release metadata.', encoding='utf-8')
        result = build_source(workspace, self.root / 'source-output')
        with zipfile.ZipFile(result['archive']) as bundle:
            names = bundle.namelist()
            self.assertEqual(json.loads(bundle.read('DELIVERY-MANIFEST.json'))['product_version'], '1.0.0')
        self.assertTrue(Path(result['archive']).name.startswith('MMSA-HR-v1.0.0-source-'))
        for name in ['VERSION', 'LICENSE', 'README.md', 'CHANGELOG.md', 'NOTICE.md', '.gitignore',
                     '应用面试工作台更新.cmd', 'suit-demo/interview_runtime.py',
                     'suit-demo/update-interview.py', 'suit-demo/update-interview.ps1',
                     'suit-demo/qwenwork/prepare-qwenwork.ps1',
                     'suit-demo/qwenwork/mcp-import.example.json']:
            self.assertIn(name, names)
        self.assertIn('suit-demo/interview_core.py', names)
        self.assertIn('DELIVERY-MANIFEST.json', names)
        for name in ['docs/qwenwork-local-demo.md', 'docs/qwenwork-v1-deployment.md',
                     'docs/usability-fix-20261024.md', 'docs/focused-delivery-20261027.md']:
            self.assertNotIn(name, names)
        for name in ('interview_forms.py', 'interview_conclusion.py', 'interview_routing.py',
                     'config/interview.v4.json', 'config/form-rubrics.v1.json',
                     'tests/test_interview_forms.py', 'tests/test_interview_pdf.py'):
            self.assertIn('suit-demo/' + name, names)
        self.assertIn('启动面试工作台.cmd', names)
        self.assertIn('suit-demo/tests/fixtures/team-text-contract.v1.json', names)
        self.assertIn('suit-demo/check_upgrade_contracts_live.py', names)
        for name in ['js/resumable-upload.js', 'css/candidate-flow.css', 'css/review-workspace.css', 'css/multimodal-view.css', 'js/multimodal-view.js']:
            self.assertIn('suit-demo/site/' + name, names)
        self.assertNotIn('suit-demo/models/original-au-net/weights.pth', names)
        for name in ['UPSTREAM.json', 'models/openface-2.2.0/Copyright.txt', 'models/openface-2.2.0/OpenFace-license.txt',
                     'interview_au_net.py', 'fetch_original_au_net.py', 'models/original-au-net/assets.json',
                     'models/original-au-net/LICENSE', 'models/original-au-net/README.md']:
            self.assertIn('suit-demo/' + name, names)
        for fragment in ['.secrets-user', 'interview-data', 'models/base.pt', 'local-data', 'mcp-import.local.json', 'FeatureExtraction.exe']:
            self.assertFalse(any(fragment in n for n in names))

    def test_v2_source_package_keeps_runtime_sources_and_excludes_installed_assets_and_people(self):
        workspace = self.root / 'v2-workspace'
        included = [
            'src/mmsa_v2/qwen_stdio.py', 'src/mmsa_v2/speech_emotion.py',
            'src/mmsa_v2/vendor/emotion2vec/model.py',
            'src/mmsa_v2/vendor/emotion2vec/LICENSE', 'src/mmsa_v2/vendor/emotion2vec/SOURCE.json',
            'config/v2/bank.json', 'config/v2/choice_wording.json', 'config/v2/compile_bank.py',
            'web/v2/personal-template.html', 'web/v2/opening.js', 'web/v2/interview-opening.mp4',
            'web/v2/assets/tongji-qwen-challenge-poster.jpg',
            'web/v2/assets/tongji-qwen-challenge-source.json',
            'scripts/prepare_speech_emotion.py', 'scripts/prepare_visual_emotion.py',
            'scripts/validate_multimodal_local.py', 'docs/speech-emotion-runtime.md',
            'scripts/install_v2.py', 'scripts/install-v2.ps1', 'scripts/prepare_qwenwork_v2.py',
            'scripts/first-run-v2.ps1', 'scripts/configure-bailian-gui.ps1', 'src/mmsa_v2/cloud_policy.py',
            'config/v2-install-assets.json', 'suit-demo/requirements-v2.txt',
            '安装真才实鉴.cmd', '配置百炼API.cmd', 'docs/v2-local-install.md',
            '开始使用真才实鉴.cmd', '开始使用.txt', 'docs/bailian-user-setup.md',
            'docs/qwenwork-v2-staging/zhenjian-v2/SKILL.md',
            'tests/v2/test_cloud_content.py', 'tests/v2/browser_candidate.cjs',
            'tests/v2/fixtures/short_oral_replies.json', 'tests/v2/fixtures/simulated_state_report.json',
        ]
        excluded = [
            'suit-demo/models/emotion2vec-plus-base/model.pt',
            'suit-demo/models/emotion2vec-plus-base/config.yaml',
            'suit-demo/models/openface-2.2.0/FeatureExtraction.exe',
            'suit-demo/models/openface-2.2.0/model.dat',
            '.runtime/emotion2vec/omegaconf/__init__.py', '.venv-suit/pyvenv.cfg',
            'src/mmsa_v2/vendor/emotion2vec/__pycache__/model.pyc',
            'src/mmsa_v2/vendor/emotion2vec/model.pt',
            'suit-demo/local-data/v2/workflow.sqlite3', 'suit-demo/local-data/v2/.operator-key',
            'suit-demo/local-data/v2/media/answer.webm', 'suit-demo/.secrets-user/bailian.dpapi.json',
            '模拟简历/01_学生.docx', 'recordings/answer.wav', 'web/v2/assets/private-photo.jpg',
            'config/v2/operator.local.json', 'scripts/local-debug.json',
            'delivery/qwenwork-v2/mcp-import.local.json', '.runtime/install-v2-report.json',
        ]
        for relative in included + excluded:
            path = workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic package fixture')
        expected = set(included)
        self.assertEqual({p.relative_to(workspace).as_posix() for p in source_files(workspace)}, expected)
        result = build_source(workspace, self.root / 'v2-output')
        with zipfile.ZipFile(result['archive']) as bundle:
            self.assertEqual(set(bundle.namelist()), expected | {'DELIVERY-MANIFEST.json'})
            manifest = json.loads(bundle.read('DELIVERY-MANIFEST.json'))
            self.assertFalse(manifest['weights_included'])
            self.assertFalse(manifest['contains_runtime_data'])
            self.assertFalse(manifest['contains_credentials'])
            for record in manifest['files']:
                self.assertEqual(record['sha256'], hashlib.sha256(bundle.read(record['path'])).hexdigest())

    def test_source_package_rejects_live_credential_shaped_content(self):
        workspace = self.root / 'workspace'
        (workspace / 'suit-demo').mkdir(parents=True)
        (workspace / 'suit-demo/interview_core.py').write_text('sk-' + 'a' * 40, encoding='utf-8')
        with self.assertRaises(ValueError):
            build_source(workspace, self.root / 'source-output')

    def test_source_package_rejects_unsafe_version_before_archive_creation(self):
        workspace = self.root / 'workspace'
        (workspace / 'suit-demo').mkdir(parents=True)
        (workspace / 'suit-demo/interview_core.py').write_text('pass', encoding='utf-8')
        (workspace / 'VERSION').write_text('../unsafe/version', encoding='utf-8')
        output = self.root / 'source-output'
        with self.assertRaisesRegex(ValueError, 'VERSION'):
            build_source(workspace, output)
        self.assertFalse(list(output.glob('*.zip')))

    def test_doctor_reports_optional_original_au_without_loading_network(self):
        from interview_au_net import PINNED
        for name in PINNED:
            path = self.root / 'models' / 'original-au-net' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'presence-fixture')
        with patch('delivery_tools.ROOT', self.root), \
             patch('interview_credentials.public_settings', return_value={'model': 'fixture', 'base_url': ''}), \
             patch('interview_credentials.credential_available', return_value=False), \
             patch('interview_au_net._verified_assets', return_value={}) as verified, \
             patch('interview_au_net._load_network') as load:
            status = doctor()['optional_models']['original_au_net']
            self.assertTrue(status['assets_present'])
            self.assertFalse(status['default_enabled'])
            self.assertEqual(status['verification'], 'not_requested')
            verified.assert_not_called()
            status = doctor(True)
            self.assertEqual(status['model_hash_verification']['original_au_net'], 'verified')
            self.assertEqual(status['optional_models']['original_au_net']['verification'], 'verified')
            verified.assert_called_once()
            load.assert_not_called()

    def test_doctor_labels_v4_optional_research_and_asr_without_inference(self):
        with patch('delivery_tools.ROOT', self.root), \
             patch('interview_credentials.public_settings', return_value={'model': 'fixture', 'base_url': ''}), \
             patch('interview_credentials.credential_available', return_value=False), \
             patch('interview_au_net._verified_assets') as au_verify, \
             patch('interview_au_net._load_network') as au_load, \
             patch('requests.post', side_effect=AssertionError('doctor must not call cloud')):
            result = doctor()
        for model in ('gaze360', 'emotic'):
            state = result['optional_models'][model]
            self.assertFalse(state['default_enabled'])
            self.assertEqual(state['default_config'], 'interview.v4')
            self.assertEqual(state['execution'], 'explicit_independent_research_only')
            self.assertEqual(state['verification'], 'not_requested')
        self.assertEqual(result['fast_local_asr']['default_config'], 'interview.v4')
        self.assertFalse(result['performs_inference'])
        self.assertFalse(result['calls_cloud'])
        au_verify.assert_not_called()
        au_load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
