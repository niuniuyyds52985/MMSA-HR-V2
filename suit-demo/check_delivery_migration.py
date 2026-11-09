"""Verify the first additive research migration on an old backup's temporary copy.

The live database is never opened. No participant rows, credentials or media
are written to the proof; only table counts, digests and validation outcomes.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from interview_core import InterviewCore
from interview_research import ResearchStore

ROOT = Path(__file__).resolve().parent
RESEARCH_TABLES = {'research_experiments', 'research_runs', 'reference_annotations',
                   'reference_profiles', 'research_publications', 'alignment_reviews', 'schema_migrations'}


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def table_names(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def old_backup():
    for path in sorted((ROOT / 'interview-data' / 'backups').glob('pre-delivery-*.sqlite3')):
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            if not table_names(db) & RESEARCH_TABLES:
                return path
    raise ValueError('没有找到尚未包含研究表的 pre-delivery 备份；请使用 --source 指定升级前快照。')


def rows(db, tables):
    result = {}
    for table in sorted(tables):
        name = '"' + table.replace('"', '""') + '"'
        data = db.execute('SELECT * FROM ' + name + ' ORDER BY rowid').fetchall()
        raw = json.dumps(data, ensure_ascii=False, default=lambda value: {'bytes_hex': value.hex()}).encode('utf-8')
        result[table] = {'rows': len(data), 'sha256': hashlib.sha256(raw).hexdigest()}
    return result


def verify(source=None):
    source = Path(source or old_backup()).resolve()
    if source == (ROOT / 'interview-data' / 'interviews.sqlite3').resolve():
        raise ValueError('首次迁移验证只接受升级前备份，不使用活库。')
    source_hash = sha(source)
    with tempfile.TemporaryDirectory(prefix='suit-first-migration-') as folder:
        copy = Path(folder) / 'interviews.sqlite3'
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
            original_tables = table_names(src)
            if original_tables & RESEARCH_TABLES:
                raise ValueError('所选备份已有研究表，不能证明首次增量迁移。')
            with closing(sqlite3.connect(copy)) as dest:
                src.backup(dest)
        with closing(sqlite3.connect(copy)) as db:
            before = rows(db, original_tables)
        with patch('interview_credentials.public_settings', return_value={'model': 'migration-fixture', 'base_url': 'https://example.invalid'}):
            core = InterviewCore(folder)
        ResearchStore(core)
        with closing(sqlite3.connect(copy)) as db:
            after = rows(db, original_tables)
            new_tables = table_names(db) - original_tables
            assert RESEARCH_TABLES <= new_tables, 'Expected additive research tables are missing'
            assert before == after, 'Migration changed existing business rows'
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            first_migration = rows(db, table_names(db))
        ResearchStore(core)
        with closing(sqlite3.connect(copy)) as db:
            assert first_migration == rows(db, table_names(db)), 'Second migration is not idempotent'
    assert sha(source) == source_hash, 'Source backup changed'
    return {'schema_version': 'first-research-migration-check.v2',
            'checked_at': datetime.now(timezone.utc).isoformat(), 'source_backup': source.name,
            'source_sha256': source_hash, 'source_file_unchanged': True, 'live_database_opened': False,
            'research_tables_absent_before': True, 'added_tables': sorted(new_tables),
            'original_business_tables': before, 'original_business_tables_unchanged': True,
            'migration_on_copy': 'passed', 'second_migration_idempotent': True,
            'integrity_check': 'ok', 'passed': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, help='升级前 SQLite 备份；默认使用最早尚无研究表的 pre-delivery 快照')
    args = parser.parse_args()
    result = verify(args.source)
    destination = ROOT / 'local-data' / 'interview-validation'
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'delivery-migration-check.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in result.items() if key != 'original_business_tables'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
