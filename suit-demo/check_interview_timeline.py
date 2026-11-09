"""Read-only local M1 check. Saves metadata only; never prints credentials or answers."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parent


def snapshot(session_id):
    database = ROOT / 'interview-data/interviews.sqlite3'
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        version = db.execute('SELECT version FROM sessions WHERE id=?', (session_id,)).fetchone()
        if version is None:
            raise ValueError('Selected session does not exist')
        fingerprints = {}
        for table in ('answers', 'jobs', 'decision_events', 'decisions', 'reports', 'reviews', 'model_evaluations'):
            rows = db.execute(f'SELECT * FROM {table} WHERE session_id=? ORDER BY rowid', (session_id,)).fetchall()
            fingerprints[table] = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {'session_version': version[0], 'table_fingerprints': fingerprints}


def check(base_url, session_id):
    parsed = urlsplit(base_url)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
        raise ValueError('Only the existing local HTTP service can be checked')
    secret = (ROOT / 'interview-data/.token-secret').read_bytes()
    if len(secret) != 32:
        raise ValueError('Unexpected local credential format')

    def get(suffix, role='hr', params=None):
        token = hmac.new(secret, f'{session_id}:{role}'.encode(), hashlib.sha256).hexdigest()
        return requests.get(base_url.rstrip('/') + '/v1/interviews/' + session_id + suffix,
                            headers={'Authorization': 'Bearer ' + token}, params=params,
                            allow_redirects=False, timeout=(3, 20))

    before = snapshot(session_id)
    response = get('')
    assert response.status_code == 200, 'Session read failed'
    state = response.json()
    summaries = []
    for answer in state.get('answers', []):
        if not answer.get('current'):
            continue
        suffix = '/answers/' + answer['answer_id'] + '/timeline'
        revision = answer['transcript_revision']
        response = get(suffix, params={'transcript_revision': revision})
        assert response.status_code == 200, 'Timeline read failed'
        data = response.json()
        assert data['answer_id'] == answer['answer_id'] and data['transcript_revision'] == revision
        assert data['text'] == answer['transcript'], 'Timeline text does not match its version'
        text = data['text']
        duration = data['media']['duration_ms']
        aligned = 0
        for segment in data['segments']:
            assert text[segment['char_start']:segment['char_end']] == segment['text'], 'Incorrect segment offsets'
            if segment['start_ms'] is not None:
                assert 0 <= segment['start_ms'] < segment['end_ms'] <= duration, 'Invalid media interval'
                aligned += 1
            else:
                assert segment['end_ms'] is None, 'Partial time pair'
        for reference in data['references']:
            assert reference['transcript_revision'] == revision, 'Reference version mismatch'
            for occurrence in reference['occurrences']:
                assert text[occurrence['char_start']:occurrence['char_end']] == reference['quote'], 'Incorrect quote offsets'
        assert get(suffix, 'candidate', {'transcript_revision': revision}).status_code == 403, 'Candidate accessed teacher timeline'
        assert get(suffix, params={'transcript_revision': 999999}).status_code == 404, 'Unknown revision not rejected'
        historical = None
        if revision > 1:
            old = get(suffix, params={'transcript_revision': 1})
            assert old.status_code == 200 and old.json()['transcript_revision'] == 1, 'Historical revision failed'
            historical = {'revision': 1, 'alignment_status': old.json()['alignment']['status']}
        repeated = get(suffix, params={'transcript_revision': revision})
        assert repeated.status_code == 200 and repeated.json() == data, 'Repeated read not stable'
        summaries.append({'answer_id': answer['answer_id'], 'revision': revision,
                          'alignment_status': data['alignment']['status'], 'segment_count': len(data['segments']),
                          'aligned_segment_count': aligned, 'reference_count': len(data['references']),
                          'audio_window_count': len(data['observations']['audio']['windows']), 'historical': historical})
    assert summaries, 'Selected session has no committed answers'
    after = snapshot(session_id)
    assert after == before, 'Session changed during read-only check; inspect concurrent activity'
    return {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'session_id': session_id,
            'scope': 'Existing session, HTTP GET only; no cloud calls, no media decoding, no transcript export',
            'session_version_before': before['session_version'], 'session_version_after': after['session_version'],
            'business_rows_unchanged': True, 'candidate_access_denied': True, 'unknown_revision_rejected': True,
            'answers': summaries, 'passed': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session', required=True)
    parser.add_argument('--base-url', default='http://127.0.0.1:8766')
    args = parser.parse_args()
    result = check(args.base_url, args.session)
    output = ROOT / 'local-data/interview-validation/timeline-live-check.json'
    output.parent.mkdir(exist_ok=True, parents=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
