"""Synthetic migration proof: pending chunks, frozen report, paused restore.

Creates and deletes temporary data only. Never opens the installed participant
database, decrypts credentials, executes models, or calls external services.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch
import uuid

from delivery_tools import backup_data, restore_data, source_files
from interview_core import InterviewCore
from interview_research import ResearchStore
from interview_uploads import UploadStore, MIN_CHUNK

ROOT = Path(__file__).resolve().parent


def key():
    return str(uuid.uuid4())


def database_digest(path):
    """Hash the logical read-only snapshot including private capability hashes."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        return hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()


def verify():
    with tempfile.TemporaryDirectory(prefix='suit-upgrade-copy-') as directory:
        root = Path(directory)
        original = root / 'source'
        with patch.dict(os.environ, {'DASHSCOPE_API_KEY': '', 'INTERVIEW_CREDENTIALS_FILE': str(root / 'missing.json')}):
            core = InterviewCore(original)
            store = ResearchStore(core)
            uploads = UploadStore(core)
            created = core.create_interview(key(), 'synthetic-migration-upgrade')
            sid = created['session_id']
            media = original / 'media' / 'fixture.wav'
            media.write_bytes(b'fixture-only; no decoder or inference execution')
            core.submit_answer(sid, created['current_round']['round_id'], key(), key(), path=media,
                media_hash=hashlib.sha256(media.read_bytes()).hexdigest(), media_meta={'duration_ms': 1000, 'mime_type': 'audio/wav'})
            job = core.claim_job('fixture-worker')
            core.complete_job(job['job_id'], job['lease_token'], {'transcription': {'status': 'ok', 'text': '合成迁移原话',
                'segments': [{'start_ms': 0, 'end_ms': 1000, 'text': '合成迁移原话'}]}})
            old_report = core.get_report(sid)
            content = b'x' * (MIN_CHUNK + 32)
            upload = uploads.create(sid, round_id=created['current_round']['round_id'], attempt_id=key(), filename='pending.webm',
                total_size=len(content), chunk_size=MIN_CHUNK, sha256=hashlib.sha256(content).hexdigest(), idempotency_key=key())
            uploads.put_chunk(sid, upload['upload_id'], 0, content[:MIN_CHUNK], hashlib.sha256(content[:MIN_CHUNK]).hexdigest())
            source_before = database_digest(core.db_path)
            package = backup_data(original, root / 'packages')
            restored = restore_data(package['archive'], root / 'restored')
            target = InterviewCore(root / 'restored')
            target_store = ResearchStore(target)
            assert target_store.analysis_status(sid)['multimodal']['paused']
            assert target_store.enqueue_standard() == []
            assert target.get_report(sid, old_report['version']) == old_report
            assert UploadStore(target).status(sid, upload['upload_id'])['received_chunks'] == [0]
            with target._db() as db:
                assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
                assert db.execute('SELECT candidate_hash FROM sessions WHERE id=?', (sid,)).fetchone()[0] != hashlib.sha256(created['candidate_token'].encode()).hexdigest()
            target_store.resume_standard(sid, version=target.get_state(sid)['version'], idempotency_key=key())
            assert len(target_store.enqueue_standard()) == 1
            assert database_digest(core.db_path) == source_before
            assert package['pending_upload_chunks'] == restored['pending_upload_chunks_preserved'] == 1
    packaged = {p.relative_to(ROOT.parent).as_posix() for p in source_files()}
    required = {'suit-demo/interview_uploads.py', 'suit-demo/interview_au_net.py', 'suit-demo/fetch_original_au_net.py',
        'suit-demo/check_original_au_net.py', 'suit-demo/site/js/resumable-upload.js', 'suit-demo/site/css/review-workspace.css',
        'suit-demo/site/js/multimodal-view.js', 'suit-demo/models/original-au-net/assets.json',
        'suit-demo/models/original-au-net/LICENSE', 'suit-demo/models/original-au-net/README.md'}
    assert required <= packaged, 'Missing delivery sources: ' + ', '.join(sorted(required - packaged))
    assert not any(name.endswith(('.pt', '.pth', '.onnx')) or '/interview-data/' in name or '/.secrets-' in name for name in packaged)
    return {'schema_version': 'delivery-upgrade-check.v1', 'passed': True, 'fixture_only': True,
        'participant_database_opened': False, 'cloud_or_model_calls': 0, 'source_database_unchanged': True,
        'frozen_report_unchanged': True, 'restored_capabilities_rotated': True, 'pending_chunks_preserved': 1,
        'restored_default_analysis_paused': True, 'explicit_resume_only': True, 'integrity_check': 'ok',
        'required_source_assets_present': sorted(required), 'weights_and_credentials_excluded': True}


if __name__ == '__main__':
    result = verify()
    destination = ROOT / 'local-data' / 'interview-validation' / 'delivery-upgrade-check.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
