"""Local update guards: temporary databases and fake process inventories only."""
import copy
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import interview_runtime as runtime

spec = importlib.util.spec_from_file_location('interview_update_test_module', runtime.ROOT / 'update-interview.py')
updater = importlib.util.module_from_spec(spec)
spec.loader.exec_module(updater)


class IdentityTests(unittest.TestCase):
    def test_hash_tracks_backend_inputs_not_models_data_mcp_or_local_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve() / 'suit-demo'
            root.mkdir()
            (root.parent / 'VERSION').write_text('1.0.0\n', encoding='utf-8')
            (root / 'interview_core.py').write_text('VERSION = 1\n', encoding='utf-8')
            (root / 'config').mkdir()
            config = root / 'config/interview.v4.json'
            config.write_text('{"value":1}', encoding='utf-8')
            with patch.object(runtime, 'ROOT', root):
                first = runtime.build_identity()
                for relative in ('interview_mcp.py', 'models/model.bin', 'interview-data/answers.json',
                                 '.secrets-user/key.json', 'config/models/model.json',
                                 'config/provider.local.json', 'config/credentials.json', 'config/.private/key.json'):
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('fixture excluded content', encoding='utf-8')
                self.assertEqual(runtime.build_identity(), first)
                config.write_text('{"value":2}', encoding='utf-8')
                self.assertNotEqual(runtime.build_identity()['backend_build_id'], first['backend_build_id'])
                self.assertEqual(first['product_version'], '1.0.0')
                self.assertEqual(len(first['backend_build_id']), 64)

    def test_marker_detection_does_not_create_files_and_invalid_marker_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            self.assertFalse(runtime.maintenance_active(data))
            self.assertEqual(list(data.iterdir()), [])
            runtime.maintenance_path(data).write_text('interrupted JSON', encoding='utf-8')
            self.assertTrue(runtime.maintenance_active(data))


class ProcessIdentityTests(unittest.TestCase):
    def setUp(self):
        self.exe = str(runtime.ROOT.parent / '.venv-suit/Scripts/python.exe')
        allowed = patch.object(runtime, '_allowed_executables', return_value={runtime._path(self.exe)})
        allowed.start()
        self.addCleanup(allowed.stop)
        self.record = {'project': str(runtime.ROOT), 'port': 8766}
        self.inventory = {'processes': [], 'listeners': [101]}
        for index, (role, (script, lane)) in enumerate(runtime.ROLES.items(), 101):
            args = (['-m', 'uvicorn', 'interview_entry:app', '--app-dir', str(runtime.ROOT),
                     '--host', '127.0.0.1', '--port', '8766'] if role == 'server'
                    else [str(runtime.ROOT / script), '--lane', lane])
            item = {'pid': index, 'parent_pid': 0, 'executable': self.exe, 'argv': [self.exe, *args],
                    'created_at': '2026-10-31T12:00:00+00:00', 'command_line': 'fixture-' + role}
            self.inventory['processes'].append(item)
            self.record[role + '_pid'] = index
            self.record[role + '_created_at'] = item['created_at']

    def test_valid_inventory_ignores_unrelated_qwen_and_mcp(self):
        self.inventory['processes'].extend([
            {'pid': 999, 'parent_pid': 0, 'executable': 'C:/Qwen/qwen.exe', 'argv': ['qwen.exe']},
            {'pid': 998, 'parent_pid': 0, 'executable': self.exe,
             'argv': [self.exe, str(runtime.ROOT / 'interview_mcp.py')]}])
        result = runtime.managed_processes(self.record, self.inventory, require_all=True)
        self.assertEqual({p['pid'] for p in result}, set(range(101, 108)))

    def test_reused_pid_wrong_lane_foreign_port_or_project_are_rejected(self):
        cases = []
        changed = copy.deepcopy(self.inventory)
        changed['processes'][1]['created_at'] = '2026-10-31T12:00:01+00:00'
        cases.append(changed)
        changed = copy.deepcopy(self.inventory)
        changed['processes'][1]['argv'][-1] = 'content'
        cases.append(changed)
        changed = copy.deepcopy(self.inventory)
        changed['listeners'] = [999]
        cases.append(changed)
        changed = copy.deepcopy(self.inventory)
        changed['processes'][0]['argv'][5] = str(runtime.ROOT.parent / 'another-project')
        cases.append(changed)
        for inventory in cases:
            with self.subTest(inventory=inventory), self.assertRaises(runtime.RuntimeErrorSafe):
                runtime.managed_processes(self.record, inventory)

    def test_partial_start_record_can_stop_registered_roles_but_cannot_verify_complete(self):
        record = {key: value for key, value in self.record.items()
                  if key in ('project', 'port', 'server_pid', 'server_created_at')}
        inventory = {'processes': self.inventory['processes'][:1], 'listeners': [101]}
        self.assertEqual(len(runtime.managed_processes(record, inventory)), 1)
        with self.assertRaises(runtime.RuntimeErrorSafe):
            runtime.managed_processes(record, inventory, require_all=True)

    def test_missing_creation_time_requires_explicit_legacy_mode(self):
        del self.record['worker_created_at']
        with self.assertRaises(runtime.RuntimeErrorSafe):
            runtime.managed_processes(self.record, self.inventory)
        self.assertEqual(len(runtime.managed_processes(self.record, self.inventory, allow_legacy=True)), 7)

    def test_only_exact_system_console_host_is_allowed_in_managed_tree(self):
        child = {'pid': 200, 'parent_pid': 101,
                 'executable': str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/conhost.exe'),
                 'argv': ['conhost.exe', 'fixture']}
        self.inventory['processes'].append(child)
        self.assertEqual(len(runtime.managed_processes(self.record, self.inventory)), 7)
        child['executable'] = str(runtime.ROOT / 'conhost.exe')
        with self.assertRaises(runtime.RuntimeErrorSafe):
            runtime.managed_processes(self.record, self.inventory)

    def test_new_unknown_child_is_rejected_before_tree_termination(self):
        changed = copy.deepcopy(self.inventory)
        changed['processes'].append({'pid': 200, 'parent_pid': 101, 'executable': self.exe,
                                     'argv': [self.exe, str(runtime.ROOT / 'interview_mcp.py')]})
        with patch.object(runtime, 'load_process_record', return_value=self.record), \
             patch.object(runtime, 'process_inventory', side_effect=[self.inventory, changed]), \
             patch.object(runtime.subprocess, 'run') as terminate:
            with self.assertRaises(runtime.RuntimeErrorSafe):
                runtime.stop_managed(runtime.ROOT / 'interview-data')
        terminate.assert_not_called()

    def test_nonzero_taskkill_is_accepted_only_after_entire_verified_tree_exits(self):
        record = {key: value for key, value in self.record.items()
                  if key in ('project', 'port', 'server_pid', 'server_created_at')}
        child = {'pid': 200, 'parent_pid': 101,
                 'executable': str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/conhost.exe'),
                 'created_at': '2026-10-31T12:00:00+00:00', 'command_line': 'fixture-console', 'argv': ['conhost.exe']}
        before = {'processes': [self.inventory['processes'][0], child], 'listeners': [101]}
        empty = {'processes': [], 'listeners': []}
        with patch.object(runtime, 'load_process_record', return_value=record), \
             patch.object(runtime, 'process_inventory', side_effect=[before, before, empty, empty]), \
             patch.object(runtime.subprocess, 'run', return_value=Mock(returncode=128)) as terminate:
            result = runtime.stop_managed(runtime.ROOT / 'interview-data')
        terminate.assert_called_once()
        self.assertEqual(result['stopped_process_count'], 1)
        self.assertEqual(result['stop_results'][0]['exit_code'], 128)
        self.assertEqual(result['stop_results'][0]['verified_tree_size'], 2)
        self.assertTrue(result['stop_results'][0]['tree_exited'])

    def test_nonzero_taskkill_with_remaining_or_reused_pid_is_never_retried(self):
        record = {key: value for key, value in self.record.items()
                  if key in ('project', 'port', 'server_pid', 'server_created_at')}
        before = {'processes': self.inventory['processes'][:1], 'listeners': [101]}
        for changed in (False, True):
            after = copy.deepcopy(before)
            if changed:
                after['processes'][0]['created_at'] = '2026-10-31T12:00:01+00:00'
            with self.subTest(changed=changed), patch.object(runtime, 'load_process_record', return_value=record), \
                 patch.object(runtime, 'process_inventory', side_effect=[before, before, after]), \
                 patch.object(runtime.subprocess, 'run', return_value=Mock(returncode=128)) as terminate:
                with self.assertRaises(runtime.RuntimeErrorSafe) as failure:
                    runtime.stop_managed(runtime.ROOT / 'interview-data')
            terminate.assert_called_once()
            detail = failure.exception.diagnostics['stop_results'][0]
            self.assertEqual(detail['remaining_count'], 1)
            self.assertEqual(detail['identity_changed_count'], int(changed))


class UpdateFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'suit-demo'
        self.data = self.root / 'interview-data'
        self.data.mkdir(parents=True)
        self.db_path = self.data / 'interviews.sqlite3'
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.executescript("CREATE TABLE sessions(id TEXT,status TEXT); CREATE TABLE jobs(id TEXT,status TEXT); "
                             "CREATE TABLE research_runs(id TEXT,status TEXT); INSERT INTO sessions VALUES('s1','interview_finished');")
        self.identity = {'product_version': '1.0.0', 'backend_build_id': 'b' * 64}
        self.record = {'port': 8766, 'backend_build_id': self.identity['backend_build_id']}
        self.health_value = {'service': 'zhenjian-interview', **self.identity, 'maintenance_supported': True,
                             'active_requests': 0, 'active_write_requests': 0}
        self.script = Mock()
        patches = [patch.object(updater, 'ROOT', self.root), patch.object(updater, 'DATA', self.data),
                   patch.object(updater, 'build_identity', return_value=self.identity),
                   patch.object(updater, 'load_process_record', return_value=self.record),
                   patch.object(updater, 'process_inventory', return_value={}),
                   patch.object(updater, 'managed_processes', return_value=[{}] * 7),
                   patch.object(updater, 'health', side_effect=lambda port: {
                       **self.health_value, 'maintenance_active': runtime.maintenance_active(self.data)}),
                   patch.object(updater, 'run_script', self.script)]
        for active in patches:
            active.start()
            self.addCleanup(active.stop)

    def insert_job(self, table, state):
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(f'INSERT INTO {table} VALUES(?,?)', ('fixture', state))

    def test_status_is_read_only_and_does_not_write_evidence_or_marker(self):
        before = self.db_path.read_bytes()
        result = updater.status(8766)
        self.assertTrue(result['processes_verified'])
        self.assertEqual(before, self.db_path.read_bytes())
        self.assertFalse((self.root / 'local-data').exists())
        self.assertFalse(runtime.maintenance_active(self.data))
        self.script.assert_not_called()

    def test_any_pending_job_refuses_without_touching_statuses(self):
        for table in ('jobs', 'research_runs'):
            for state in ('queued', 'running'):
                with self.subTest(table=table, state=state):
                    self.insert_job(table, state)
                    result = updater.apply(8766)
                    self.assertEqual(result['status'], 'failed')
                    self.script.assert_not_called()
                    self.assertFalse(runtime.maintenance_active(self.data))
                    with closing(sqlite3.connect(self.db_path)) as db, db:
                        self.assertEqual(db.execute(f'SELECT status FROM {table}').fetchall(), [(state,)])
                        db.execute(f'DELETE FROM {table}')

    def test_active_read_request_refuses_even_with_zero_write_requests(self):
        self.health_value['active_requests'] = 1
        result = updater.apply(8766)
        self.assertEqual(result['status'], 'failed')
        self.script.assert_not_called()
        self.assertFalse(runtime.maintenance_active(self.data))

    def test_legacy_requires_bootstrap_and_bootstrap_rejects_unfinished_sessions(self):
        self.health_value.pop('maintenance_supported')
        self.assertEqual(updater.apply(8766)['status'], 'failed')
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE sessions SET status='answering'")
        self.assertEqual(updater.apply(8766, bootstrap=True)['status'], 'failed')
        self.script.assert_not_called()

    def test_busy_race_is_rechecked_inside_lock(self):
        empty = updater.read_counts()
        self.insert_job('jobs', 'queued')
        with patch.object(updater, 'read_counts', return_value=empty):
            result = updater.apply(8766)
        self.assertEqual(result['status'], 'failed')
        self.script.assert_not_called()
        self.assertFalse(runtime.maintenance_active(self.data))

    def test_stop_holds_database_lock_start_is_unlocked_and_data_unchanged(self):
        before = self.db_path.read_bytes()
        def execute(name, *args):
            self.assertTrue(runtime.maintenance_active(self.data))
            db = sqlite3.connect(self.db_path, timeout=0, isolation_level=None)
            try:
                if name == 'stop-interview.ps1':
                    with self.assertRaises(sqlite3.OperationalError):
                        db.execute('BEGIN IMMEDIATE')
                else:
                    db.execute('BEGIN IMMEDIATE')
                    db.rollback()
            finally:
                db.close()
        self.script.side_effect = execute
        result = updater.apply(8766)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual([call.args[0] for call in self.script.call_args_list], ['stop-interview.ps1', 'start-interview.ps1'])
        self.assertEqual(before, self.db_path.read_bytes())
        self.assertFalse(runtime.maintenance_active(self.data))

    def test_stop_or_start_failure_retains_owned_maintenance_for_recovery(self):
        for fail_at in ('stop-interview.ps1', 'start-interview.ps1'):
            with self.subTest(fail_at=fail_at):
                def execute(name, *args):
                    if name == fail_at:
                        raise runtime.RuntimeErrorSafe('fixture failure')
                self.script.side_effect = execute
                result = updater.apply(8766)
                self.assertEqual(result['status'], 'failed')
                self.assertTrue(result['recovery_required'])
                self.assertTrue(runtime.maintenance_active(self.data))
                with closing(sqlite3.connect(self.db_path, timeout=0)) as db, db:
                    db.execute('BEGIN IMMEDIATE')
                marker = runtime.maintenance_path(self.data)
                updater.clear_marker(json.loads(marker.read_text(encoding='utf-8'))['owner'])

    def test_foreign_maintenance_marker_is_never_removed(self):
        updater.create_marker('another-owner', False)
        result = updater.apply(8766)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(json.loads(runtime.maintenance_path(self.data).read_text())['owner'], 'another-owner')
        self.script.assert_not_called()

    def test_bounded_wait_leaves_workers_unpaused_while_existing_queue_drains(self):
        empty = updater.read_counts()
        busy = copy.deepcopy(empty)
        busy['jobs']['running'] = 1
        def wait(_duration):
            self.assertFalse(runtime.maintenance_active(self.data))
        with patch.object(updater, 'read_counts', side_effect=[busy, empty]), \
             patch.object(updater.time, 'sleep', side_effect=wait):
            result = updater.apply(8766, wait_seconds=1)
        self.assertEqual(result['status'], 'succeeded')


class ScriptDiagnosticTests(unittest.TestCase):
    def test_known_helper_error_is_preserved_without_raw_output_or_unknown_fields(self):
        payload = {'ok': False, 'error_type': 'RuntimeErrorSafe', 'error': 'Verified fixture did not exit.',
                   'diagnostics': {'token': 'fixture-secret', 'stop_results': [
                       {'pid': 101, 'exit_code': 128, 'remaining_count': 1, 'command_line': 'private-command'}]}}
        def execute(*args, **kwargs):
            kwargs['stdout'].write((json.dumps(payload) + '\n').encode())
            kwargs['stderr'].write(b'private raw stderr')
            return Mock(returncode=1)
        with patch.object(updater.subprocess, 'run', side_effect=execute):
            with self.assertRaises(runtime.RuntimeErrorSafe) as failure:
                updater.run_script('stop-interview.ps1')
        self.assertIn('Verified fixture did not exit.', str(failure.exception))
        detail = json.dumps(failure.exception.diagnostics)
        self.assertIn('128', detail)
        for secret in ('fixture-secret', 'private-command', 'private raw stderr'):
            self.assertNotIn(secret, str(failure.exception) + detail)

    def test_script_capture_uses_regular_files_instead_of_inheritable_pipes(self):
        payload = {'stop_results': [{'pid': 101, 'exit_code': 0, 'tree_exited': True}]}
        def execute(*args, **kwargs):
            self.assertEqual(kwargs['stdin'], updater.subprocess.DEVNULL)
            for name in ('stdout', 'stderr'):
                self.assertNotEqual(kwargs[name], updater.subprocess.PIPE)
                self.assertTrue(kwargs[name].seekable())
                self.assertGreaterEqual(kwargs[name].fileno(), 0)
            kwargs['stdout'].write((json.dumps(payload) + '\n').encode())
            return Mock(returncode=0)
        with patch.object(updater.subprocess, 'run', side_effect=execute):
            self.assertEqual(updater.run_script('stop-interview.ps1'), payload)


if __name__ == '__main__':
    unittest.main()
