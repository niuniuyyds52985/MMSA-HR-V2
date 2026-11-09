"""Read-only online backup and new-directory restore proof, without workers.

Records counts/digests and boolean checks only; no capabilities, transcripts,
model outputs or original media are printed or copied into the proof.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from delivery_tools import backup_data, restore_data, digest, _research_artifacts


def checksum(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        default=lambda v: {'binary_digest': hashlib.sha256(v).hexdigest()}).encode()).hexdigest()


def snapshot(folder):
    with closing(sqlite3.connect((folder / 'interviews.sqlite3').as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        tables = sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
        table_digests = {}
        for name in tables:
            escaped = '"' + name.replace('"', '""') + '"'
            values = db.execute('SELECT * FROM ' + escaped + ' ORDER BY rowid').fetchall()
            table_digests[name] = {'count': len(values), 'sha256': checksum(values)}
        reports = db.execute('SELECT session_id,version,payload FROM reports ORDER BY session_id,version').fetchall()
        results = db.execute('SELECT id,result FROM research_runs ORDER BY id').fetchall()
        media = {}
        for stored, expected in db.execute('SELECT DISTINCT path,media_hash FROM answers WHERE path IS NOT NULL'):
            file = Path(stored).resolve()
            if not file.is_relative_to(folder / 'media'):
                raise ValueError('原片不在所选数据目录。')
            actual = digest(file)
            if expected and expected != actual:
                raise ValueError('原片与数据库摘要不一致。')
            media[file.relative_to(folder).as_posix()] = actual
        artifacts = {relative: digest(folder / relative) for relative, _ in _research_artifacts(db)}
        credentials = {sid: (candidate, hr) for sid, candidate, hr in db.execute('SELECT id,candidate_hash,hr_hash FROM sessions')}
        plans = {sid for sid, config in db.execute('SELECT id,config FROM sessions') if (json.loads(config).get('analysis_plan') or {}).get('enabled')}
        paused = {sid for sid, value in db.execute('SELECT session_id,paused FROM standard_analysis_controls') if value} if 'standard_analysis_controls' in tables else set()
        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    return {'tables': table_digests, 'reports': {'count': len(reports), 'sha256': checksum(reports)},
        'research_results': {'count': len(results), 'sha256': checksum(results)}, 'media': media,
        'artifacts': artifacts, 'credentials': credentials, 'plans': plans, 'paused': paused, 'integrity': integrity}


def verify(source, target, archive_dir):
    source, target, archive_dir = Path(source).resolve(), Path(target).resolve(), Path(archive_dir).resolve()
    if target.exists():
        raise ValueError('恢复目标必须不存在。')
    if source == target or target.is_relative_to(source) or archive_dir.is_relative_to(source):
        raise ValueError('恢复和备份目录须在原数据目录之外。')
    before = snapshot(source)
    archive = backup_data(source, archive_dir)
    restored = restore_data(archive['archive'], target)
    copied, after = snapshot(target), snapshot(source)
    checks = {'source_business_tables_unchanged': before['tables'] == after['tables'],
        'source_media_unchanged': before['media'] == after['media'],
        'source_artifacts_unchanged': before['artifacts'] == after['artifacts'],
        'report_snapshots_unchanged': before['reports'] == copied['reports'],
        'research_results_unchanged': before['research_results'] == copied['research_results'],
        'media_bytes_identical': before['media'] == copied['media'],
        'artifact_bytes_identical': before['artifacts'] == copied['artifacts'],
        'permissions_rotated': all(copied['credentials'].get(sid) and all(old != new for old, new in zip(pair, copied['credentials'][sid]))
                                   for sid, pair in before['credentials'].items()),
        'default_plans_paused': before['plans'] == copied['plans'] and before['plans'] <= copied['paused'],
        'integrity_ok': copied['integrity'] == before['integrity'] == 'ok',
        'all_declared_artifacts_present': archive['missing_research_artifacts'] == restored['missing_research_artifacts'] == 0}
    return {'schema_version': 'online-delivery-copy-proof.v1', 'checked_at': datetime.now(timezone.utc).isoformat(),
        'passed': all(checks.values()), 'checks': checks, 'target_confirmed_absent_before_restore': True,
        'source_directory': source.name, 'restored_directory': target.name,
        'backup_file': Path(archive['archive']).name, 'backup_sha256': archive['sha256'],
        'source_report_snapshots': before['reports'], 'restored_report_snapshots': copied['reports'],
        'source_research_results': before['research_results'], 'restored_research_results': copied['research_results'],
        'media_files': len(before['media']), 'media_manifest_sha256': checksum(before['media']),
        'observation_artifacts': len(before['artifacts']), 'artifact_manifest_sha256': checksum(before['artifacts']),
        'sessions': len(before['credentials']), 'default_analysis_sessions_paused': len(copied['plans']),
        'pending_upload_chunks_preserved': restored['pending_upload_chunks_preserved'],
        'operation_cache_cleared_in_copy': restored['operation_cache_cleared'],
        'workers_started': False, 'model_calls': 0, 'credentials_in_proof': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'target', 'archive-dir', 'proof'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    result = verify(args.source, args.target, args.archive_dir)
    args.proof.parent.mkdir(parents=True, exist_ok=True)
    args.proof.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result['passed'] else 1)
