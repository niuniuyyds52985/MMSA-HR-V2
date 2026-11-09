"""Versioned research queue, annotations and explicit report publication.

Research inference never calls the live scoring/decision writeback path.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import sys
import time
from interview_core import DomainError, dumps, uid, utc
from interview_profile import FACTORS, factor_profile

MODULES = {'word_frequency', 'semantic', 'au', 'gaze', 'expression', 'audio', 'au_net', 'gaze360', 'emotic'}
FINAL = {'succeeded', 'partial', 'failed'}
LOCAL_MODULES = {'au', 'gaze', 'expression', 'audio'}
MEDIA_PHASE_MODULES = {'core': LOCAL_MODULES, 'extended': {'gaze360', 'emotic'}}


def _media_plan(plan):
    return bool(plan and plan.get('trigger') == 'after_media_saved')


def _media_phases(plan):
    """Validate the frozen local-only plan, without selecting fallback models."""
    phases = plan.get('phases', []) if plan else []
    if not _media_plan(plan) or not isinstance(plan.get('version'), str) or not plan['version'] or plan.get('cloud_media'):
        return []
    if not isinstance(phases, list) or not phases:
        return []
    seen, modules = set(), []
    for phase in phases:
        if not isinstance(phase, dict):
            return []
        name, selected = phase.get('id'), phase.get('modules')
        if name not in MEDIA_PHASE_MODULES or name in seen or not isinstance(selected, list) or not selected:
            return []
        if any(not isinstance(m, str) for m in selected) or len(set(selected)) != len(selected) or not set(selected) <= MEDIA_PHASE_MODULES[name]:
            return []
        seen.add(name)
        modules.extend(selected)
    declared = plan.get('modules', [])
    if not isinstance(declared, list) or any(not isinstance(m, str) for m in declared) or len(set(declared)) != len(declared) or set(modules) != set(declared):
        return []
    return phases


def _phase_status(states):
    for pending in ('running', 'queued', 'waiting_worker', 'paused'):
        if pending in states:
            return pending
    if states and all(s == 'succeeded' for s in states):
        return 'succeeded'
    if 'partial' in states or ('succeeded' in states and 'failed' in states):
        return 'partial'
    return 'failed' if 'failed' in states else 'not_applicable'


def _media_projection(raw, answer, original_revision):
    """Project immutable media observations onto current text; never mutate a run.

    Geometry/acoustics share the same media clock. Text-dependent whole-answer
    rates are recomputed from the current transcript and measured speech time.
    """
    result = json.loads(raw) if raw else None
    if not result:
        return result
    from interview_multimodal import speech_units
    projection = {'schema_version': 'standard-media-projection.v1', 'basis': 'same_answer_and_media_sha256',
        'answer_id': answer['id'], 'media_sha256': answer['media_hash'],
        'analysis_input_transcript_revision': original_revision,
        'current_transcript_revision': answer['revision'], 'raw_result_unchanged': True,
        'text_metrics': 'recomputed_from_current_transcript_and_measured_speech_time'}
    result['projection'] = projection
    for output in result.get('multimodal', []):
        if output.get('answer_id') != answer['id']:
            continue
        output.update(analysis_input_transcript_revision=original_revision,
                      transcript_revision=answer['revision'], projection=dict(projection))
        audio = output.get('modules', {}).get('audio')
        if not audio or not audio.get('summary'):
            continue
        summary = audio['summary']
        duration, spoken = summary.get('duration_ms'), summary.get('speech_ms')
        units = speech_units(answer['text']) if answer['text'].strip() else None
        summary.update(transcript_units=units,
            speech_rate_units_per_min=round(units * 60000 / duration, 2) if units and duration and spoken else None,
            articulation_rate_units_per_speech_min=round(units * 60000 / spoken, 2) if units and spoken else None)
        audio.setdefault('provenance', {}).update(rate_transcript_revision=answer['revision'],
            rate_projection='current transcript; original run metrics remain available in the raw run')
    return result


def public(value):
    if isinstance(value, list):
        return [public(x) for x in value]
    if isinstance(value, dict):
        return {k: public(v) for k, v in value.items() if not k.startswith('_') and k not in {
            'path', 'media_path', 'output_dir', 'weights_path', 'lease_token', 'api_key', 'authorization', 'token'}}
    return value


def standard_snapshot(db, sid, config, include_results=True):
    """Read-only projection of explicitly configured local analysis, never schedules."""
    plan = config.get('analysis_plan')
    enabled = bool(plan and plan.get('enabled'))
    value = {'plan': plan, 'enabled': enabled, 'usage': 'observed_only', 'runs': [], 'answers': [],
             'pending_count': 0, 'failed_count': 0, 'paused': False, 'pause_reason': None}
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'standard_analysis_controls' in tables:
        control = db.execute('SELECT paused,reason FROM standard_analysis_controls WHERE session_id=?', (sid,)).fetchone()
        if control:
            value.update(paused=bool(control['paused']), pause_reason=control['reason'] if control['paused'] else None)
    media_bound = _media_plan(plan)
    phases = _media_phases(plan) if media_bound else []
    value['identity_basis'] = 'answer_media_plan_phase' if media_bound else 'answer_transcript_revision'
    value['count_unit'] = 'phase' if media_bound else 'answer'
    for answer in db.execute('SELECT a.id,a.revision,a.path,a.media_hash,v.text,j.status AS media_status FROM answers a JOIN revisions v ON v.answer_id=a.id AND v.version=a.revision LEFT JOIN jobs j ON j.answer_id=a.id AND j.job_type=\'media\' WHERE a.session_id=? AND a.current=1 ORDER BY a.created_at,a.id', (sid,)):
        item = {'answer_id': answer['id'], 'transcript_revision': answer['revision'], 'run_id': None}
        if not enabled or not answer['path']:
            item['status'] = 'not_applicable'
        elif media_bound:
            item['phases'] = []
            for phase in phases:
                run = None
                if 'standard_media_runs' in tables:
                    run = db.execute('''SELECT r.*,s.analysis_revision,s.attempt FROM standard_media_runs s
                        JOIN research_runs r ON r.id=s.run_id WHERE s.session_id=? AND s.answer_id=?
                        AND s.media_hash=? AND s.plan_version=? AND s.phase=? ORDER BY s.attempt DESC LIMIT 1''',
                        (sid, answer['id'], answer['media_hash'], plan['version'], phase['id'])).fetchone()
                phase_item = {'phase': phase['id'], 'modules': phase['modules'], 'run_id': run['id'] if run else None,
                    'status': run['status'] if run else 'paused' if value['paused'] else 'waiting_worker'}
                item['phases'].append(phase_item)
                if run:
                    snapshot = json.loads(run['input'])
                    value['runs'].append(public({'run_id': run['id'], 'status': run['status'], 'phase': phase['id'],
                        'answer_id': answer['id'], 'media_sha256': answer['media_hash'], 'plan_version': plan['version'],
                        'transcript_revision': answer['revision'], 'analysis_input_transcript_revision': run['analysis_revision'],
                        'attempt': run['attempt'], 'input_versions': snapshot['input_versions'], 'modules': snapshot['modules'],
                        'identity_basis': 'answer_media_plan_phase',
                        'projection': {'basis': 'same_answer_and_media_sha256', 'current_transcript_revision': answer['revision'],
                            'analysis_input_transcript_revision': run['analysis_revision'], 'raw_result_unchanged': True},
                        'created_at': run['created_at'], 'started_at': run['started_at'], 'completed_at': run['completed_at'],
                        'error': run['error'], 'has_result': bool(run['result']),
                        **({'result': _media_projection(run['result'], answer, run['analysis_revision'])} if include_results else {})}))
            item['status'] = _phase_status([p['status'] for p in item['phases']]) if phases else 'invalid_plan'
        else:
            run = None
            if 'standard_analysis_runs' in tables:
                run = db.execute('SELECT r.*,s.answer_id,s.revision,s.attempt FROM standard_analysis_runs s JOIN research_runs r ON r.id=s.run_id WHERE s.session_id=? AND s.answer_id=? AND s.revision=? ORDER BY s.attempt DESC LIMIT 1',
                                 (sid, answer['id'], answer['revision'])).fetchone()
            if run:
                snapshot = json.loads(run['input'])
                item.update(status=run['status'], run_id=run['id'])
                value['runs'].append(public({'run_id': run['id'], 'status': run['status'],
                    'answer_id': answer['id'], 'transcript_revision': answer['revision'], 'attempt': run['attempt'],
                    'input_versions': snapshot['input_versions'], 'modules': snapshot['modules'],
                    'created_at': run['created_at'], 'started_at': run['started_at'], 'completed_at': run['completed_at'],
                    'error': run['error'], 'has_result': bool(run['result']),
                    **({'result': json.loads(run['result']) if run['result'] else None} if include_results else {})}))
            else:
                item['status'] = 'paused' if value['paused'] else 'waiting_media' if answer['media_status'] in ('queued', 'running') else 'waiting_worker'
        value['answers'].append(item)
    stages = [p for a in value['answers'] for p in (a.get('phases') or [a])]
    value['pending_count'] = sum(a['status'] in ('queued', 'running', 'waiting_media', 'waiting_worker') for a in stages)
    value['failed_count'] = sum(a['status'] in ('partial', 'failed', 'invalid_plan') for a in stages)
    return value


class ResearchStore:
    def __init__(self, core):
        self.core = core
        with core._db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY,applied_at TEXT);
            CREATE TABLE IF NOT EXISTS research_experiments(id TEXT PRIMARY KEY,session_id TEXT,payload TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS research_runs(id TEXT PRIMARY KEY,session_id TEXT,experiment_id TEXT,status TEXT,input TEXT,result TEXT,error TEXT,created_at TEXT,started_at TEXT,completed_at TEXT,lease_token TEXT,lease_until REAL);
            CREATE INDEX IF NOT EXISTS research_runs_queue ON research_runs(status,created_at);
            CREATE TABLE IF NOT EXISTS reference_annotations(id TEXT PRIMARY KEY,session_id TEXT,experiment_id TEXT,payload TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS reference_profiles(id TEXT PRIMARY KEY,session_id TEXT,version INTEGER,payload TEXT,created_at TEXT,UNIQUE(session_id,version));
            CREATE TABLE IF NOT EXISTS research_publications(id TEXT PRIMARY KEY,session_id TEXT,payload TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS alignment_reviews(id TEXT PRIMARY KEY,session_id TEXT,answer_id TEXT,revision INTEGER,payload TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS standard_analysis_runs(run_id TEXT PRIMARY KEY,session_id TEXT,answer_id TEXT,revision INTEGER,attempt INTEGER,
                UNIQUE(session_id,answer_id,revision,attempt));
            CREATE TABLE IF NOT EXISTS standard_analysis_controls(session_id TEXT PRIMARY KEY,paused INTEGER NOT NULL,reason TEXT,updated_at TEXT);
            CREATE TABLE IF NOT EXISTS standard_media_runs(run_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,
                answer_id TEXT NOT NULL,media_hash TEXT NOT NULL,plan_version TEXT NOT NULL,phase TEXT NOT NULL,
                analysis_revision INTEGER NOT NULL,attempt INTEGER NOT NULL,
                UNIQUE(session_id,answer_id,media_hash,plan_version,phase,attempt));
            CREATE TRIGGER IF NOT EXISTS annotation_immutable_update BEFORE UPDATE ON reference_annotations BEGIN SELECT RAISE(ABORT,'annotation is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS annotation_immutable_delete BEFORE DELETE ON reference_annotations BEGIN SELECT RAISE(ABORT,'annotation is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS profile_immutable_update BEFORE UPDATE ON reference_profiles BEGIN SELECT RAISE(ABORT,'reference is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS profile_immutable_delete BEFORE DELETE ON reference_profiles BEGIN SELECT RAISE(ABORT,'reference is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS alignment_immutable_update BEFORE UPDATE ON alignment_reviews BEGIN SELECT RAISE(ABORT,'alignment is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS alignment_immutable_delete BEFORE DELETE ON alignment_reviews BEGIN SELECT RAISE(ABORT,'alignment is immutable'); END;
            ''')
            db.execute('INSERT OR IGNORE INTO schema_migrations VALUES(?,?)', ('research.v1', utc()))
            db.execute('INSERT OR IGNORE INTO schema_migrations VALUES(?,?)', ('standard-media.v1', utc()))

    def alignment(self, sid, *, answer_id, revision, segments, reviewer, reason, idempotency_key):
        """Append a full manual timing snapshot; never bump interview/report state."""
        from interview_alignment import AlignmentError, VERSION, validate_mapping, text_digest, segment_digest
        if type(revision) is not int or revision < 1:
            raise DomainError('invalid_alignment_revision', '请指定有效转写版本', 422)
        if (not isinstance(reviewer, str) or not 1 <= len(reviewer.strip()) <= 120
                or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000):
            raise DomainError('alignment_audit_required', '请填写校准者代号和核准理由', 422)
        request = {'answer_id': answer_id, 'revision': revision, 'segments': segments,
                   'reviewer': reviewer.strip(), 'reason': reason.strip()}
        scope = 'alignment-review:' + sid + ':' + str(answer_id)
        with self.core._db(True) as db:
            previous = self.core._idempotent(db, scope, idempotency_key, request)
            if previous:
                return previous
            self.core._session(db, sid)
            answer = db.execute('SELECT a.* FROM answers a JOIN revisions r ON r.answer_id=a.id AND r.version=? WHERE a.session_id=? AND a.id=?',
                                (revision, sid, answer_id)).fetchone()
            if not answer:
                raise DomainError('not_found', '所选回答或转写版本不属于本场', 404)
            if not answer['path']:
                raise DomainError('alignment_no_media', '纯文字回答没有可校准的原片', 422)
            media = Path(answer['path']).resolve()
            if not media.is_relative_to((self.core.data_dir / 'media').resolve()) or not media.is_file():
                raise DomainError('alignment_no_media', '原始媒体不可用，未保存人工时间', 422)
            # BEGIN IMMEDIATE keeps the source segmentation stable until commit.
            timeline = self.core.get_answer_timeline(sid, answer_id, revision)
            try:
                mapping = validate_mapping(timeline, segments)
            except AlignmentError as error:
                raise DomainError('invalid_alignment', str(error), 422) from error
            prior = db.execute('SELECT id,payload FROM alignment_reviews WHERE session_id=? AND answer_id=? AND revision=? ORDER BY rowid DESC LIMIT 1',
                               (sid, answer_id, revision)).fetchone()
            version = json.loads(prior['payload'])['version'] + 1 if prior else 1
            value = {'schema_version': VERSION, 'alignment_review_id': uid(), 'version': version,
                     'session_id': sid, 'answer_id': answer_id, 'transcript_revision': revision,
                     'segments': mapping, 'mapping_policy': 'explicit_segments_only_full_snapshot',
                     'media_sha256': timeline['media']['media_sha256'], 'duration_ms': timeline['media']['duration_ms'],
                     'text_sha256': text_digest(timeline), 'segments_sha256': segment_digest(timeline),
                     'reviewer': reviewer.strip(), 'reason': reason.strip(), 'created_at': utc(),
                     'supersedes_review_id': prior['id'] if prior else None, 'source_alignment': timeline['alignment'],
                     'usage': 'playback_alignment_only'}
            db.execute('INSERT INTO alignment_reviews VALUES(?,?,?,?,?,?)',
                       (value['alignment_review_id'], sid, answer_id, revision, dumps(value), value['created_at']))
            result = {'alignment_review_id': value['alignment_review_id'], 'version': version, 'payload': value}
            return self.core._remember(db, scope, idempotency_key, request, result)

    def _snapshot(self, db, sid, selections, allow_missing_media=False):
        session = self.core._session(db, sid)
        config = json.loads(session['config'])
        answers = []
        for selection in selections:
            aid, rev = selection['answer_id'], selection['transcript_revision']
            row = db.execute('SELECT a.*,r.question_id,r.parent_question_id,r.kind,v.text FROM answers a JOIN rounds r ON r.id=a.round_id JOIN revisions v ON v.answer_id=a.id AND v.version=? WHERE a.id=? AND a.session_id=?', (rev, aid, sid)).fetchone()
            if not row:
                raise DomainError('not_found', '所选回答或转写版本不属于本场', 404)
            if row['path']:
                path = Path(row['path']).resolve()
                if not path.is_relative_to(self.core.data_dir / 'media') or (not path.is_file() and not allow_missing_media):
                    raise DomainError('media_unavailable', '所选原始媒体不可用', 422)
            timeline = self.core.get_answer_timeline(sid, aid, rev)
            answers.append({'answer_id': aid, 'transcript_revision': rev, 'transcript': row['text'],
                            'question_id': row['question_id'], 'parent_question_id': row['parent_question_id'],
                            'media_sha256': row['media_hash'], 'duration_ms': row['duration_ms'], 'media_type': row['media_type'],
                            'created_at': row['created_at'], 'segments': timeline['segments'], 'alignment': timeline['alignment'],
                            '_media_path': row['path']})
        if len({a['answer_id'] for a in answers}) != len(answers):
            raise DomainError('duplicate_input', '一次运行每条回答只能选择一个版本', 422)
        return {'session_id': sid, 'session_version': session['version'], 'answers': answers,
                'input_versions': {a['answer_id']: a['transcript_revision'] for a in answers},
                'questions': config['questions'], 'branches': config['branches'], 'model_config': config.get('model_config', {}),
                'question_bank_version': config.get('provenance'), 'config_version': config['version']}

    def _insert_standard(self, db, sid, aid, revision, modules, attempt=1):
        snapshot = self._snapshot(db, sid, [{'answer_id': aid, 'transcript_revision': revision}], allow_missing_media=True)
        rid, created = uid(), utc()
        # A standard run has no independent experiment. It never calls semantic/cloud.
        payload = {**snapshot, 'run_id': rid, 'experiment_id': None, 'modules': modules,
            'condition': '本场默认本地观测', 'purpose': '回答音视频的可回放观察', 'participant_group': 'current-session',
            'repeat_index': attempt, 'cache_policy': 'independent_inference_no_result_cache',
            'cloud_authorized': False, 'created_at': created, 'run_kind': 'standard_local',
            'analysis_plan': json.loads(self.core._session(db, sid)['config'])['analysis_plan']}
        db.execute('INSERT INTO research_runs(id,session_id,experiment_id,status,input,created_at) VALUES(?,?,?,?,?,?)',
                   (rid, sid, None, 'queued', dumps(payload), created))
        db.execute('INSERT INTO standard_analysis_runs VALUES(?,?,?,?,?)', (rid, sid, aid, revision, attempt))
        self.core._bump(db, sid)
        return rid

    def _insert_standard_media(self, db, sid, aid, revision, media_hash, plan, phase, attempt=1):
        snapshot = self._snapshot(db, sid, [{'answer_id': aid, 'transcript_revision': revision}], allow_missing_media=True)
        if snapshot['answers'][0]['media_sha256'] != media_hash:
            raise DomainError('stale_research_input', '媒体已变更，默认观测不能复用旧输入')
        rid, created = uid(), utc()
        identity = {'answer_id': aid, 'media_sha256': media_hash, 'plan_version': plan['version'], 'phase': phase['id']}
        payload = {**snapshot, 'run_id': rid, 'experiment_id': None, 'modules': phase['modules'],
            'condition': '本场默认本地观测', 'purpose': '回答音视频的可回放观察', 'participant_group': 'current-session',
            'repeat_index': attempt, 'cache_policy': 'independent_inference_no_result_cache',
            'cloud_authorized': False, 'created_at': created, 'run_kind': 'standard_media',
            'analysis_plan': plan, 'analysis_phase': phase['id'], 'media_identity': identity}
        db.execute('INSERT INTO research_runs(id,session_id,experiment_id,status,input,created_at) VALUES(?,?,?,?,?,?)',
                   (rid, sid, None, 'queued', dumps(payload), created))
        db.execute('INSERT INTO standard_media_runs VALUES(?,?,?,?,?,?,?,?)',
                   (rid, sid, aid, media_hash, plan['version'], phase['id'], revision, attempt))
        self.core._bump(db, sid)
        return rid

    def enqueue_standard(self):
        """Reconcile frozen plans. v3 starts on saved media; v2 waits for terminal ASR.

        The immediate transaction and unique identities make concurrent lanes safe.
        """
        scheduled = []
        with self.core._db(True) as db:
            rows = db.execute('''SELECT a.id,a.session_id,a.revision,a.media_hash,s.config
                FROM answers a JOIN sessions s ON s.id=a.session_id
                WHERE a.current=1 AND a.path IS NOT NULL
                AND json_extract(s.config,'$.analysis_plan.enabled')=1
                AND NOT EXISTS(SELECT 1 FROM standard_analysis_controls c WHERE c.session_id=a.session_id AND c.paused=1)
                ORDER BY a.created_at,a.id''').fetchall()
            for answer in rows:
                plan = json.loads(answer['config']).get('analysis_plan') or {}
                if _media_plan(plan):
                    phases = _media_phases(plan)
                    if not answer['media_hash']:
                        continue
                    for phase in phases:
                        exists = db.execute('''SELECT 1 FROM standard_media_runs WHERE session_id=? AND answer_id=?
                            AND media_hash=? AND plan_version=? AND phase=?''',
                            (answer['session_id'], answer['id'], answer['media_hash'], plan['version'], phase['id'])).fetchone()
                        if exists or db.execute("SELECT count(*) FROM research_runs WHERE session_id=? AND status IN ('queued','running')", (answer['session_id'],)).fetchone()[0] >= 20:
                            continue
                        scheduled.append(self._insert_standard_media(db, answer['session_id'], answer['id'],
                            answer['revision'], answer['media_hash'], plan, phase))
                    continue
                if (not db.execute("SELECT 1 FROM jobs WHERE answer_id=? AND job_type='media' AND status IN ('succeeded','failed')", (answer['id'],)).fetchone()
                    or db.execute('SELECT 1 FROM standard_analysis_runs WHERE session_id=? AND answer_id=? AND revision=?',
                                  (answer['session_id'], answer['id'], answer['revision'])).fetchone()):
                    continue
                modules = plan.get('modules', [])
                if not modules or len(set(modules)) != len(modules) or not set(modules) <= LOCAL_MODULES or plan.get('cloud_media'):
                    continue
                if db.execute("SELECT count(*) FROM research_runs WHERE session_id=? AND status IN ('queued','running')", (answer['session_id'],)).fetchone()[0] >= 20:
                    continue
                scheduled.append(self._insert_standard(db, answer['session_id'], answer['id'], answer['revision'], modules))
        return scheduled

    def resume_standard(self, sid, *, version, idempotency_key):
        request = {'version': version}
        with self.core._db(True) as db:
            previous = self.core._idempotent(db, 'standard-resume:' + sid, idempotency_key, request)
            if previous:
                return previous
            session = self.core._session(db, sid)
            if session['version'] != version:
                raise DomainError('version_conflict', '本场已更新，请核对后恢复')
            if not (json.loads(session['config']).get('analysis_plan') or {}).get('enabled'):
                raise DomainError('plan_not_enabled', '历史会话未配置默认观测；请显式建立研究运行', 422)
            db.execute('INSERT INTO standard_analysis_controls VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET paused=excluded.paused,reason=excluded.reason,updated_at=excluded.updated_at',
                       (sid, 0, '老师显式启用当前环境的本场本地自动观测', utc()))
            self.core._bump(db, sid)
            return self.core._remember(db, 'standard-resume:' + sid, idempotency_key, request,
                {'session_id': sid, 'status': 'enabled', 'version': self.core._session(db, sid)['version'], 'cloud_media': False})

    def _media_current(self, db, sid, identity):
        answer = db.execute('SELECT revision,current,media_hash FROM answers WHERE id=? AND session_id=?',
                            (identity.get('answer_id'), sid)).fetchone()
        if not answer or not answer['current'] or answer['media_hash'] != identity.get('media_sha256'):
            return False
        plan = json.loads(self.core._session(db, sid)['config']).get('analysis_plan') or {}
        return (plan.get('enabled') and plan.get('version') == identity.get('plan_version')
                and any(p['id'] == identity.get('phase') for p in _media_phases(plan)))

    def retry_standard(self, sid, *, run_id, version, idempotency_key):
        request = {'run_id': run_id, 'version': version}
        with self.core._db(True) as db:
            previous = self.core._idempotent(db, 'standard-retry:' + sid, idempotency_key, request)
            if previous:
                return previous
            session = self.core._session(db, sid)
            if session['version'] != version:
                raise DomainError('version_conflict', '本场已更新，请核对后重试')
            control = db.execute('SELECT paused FROM standard_analysis_controls WHERE session_id=?', (sid,)).fetchone()
            if control and control['paused']:
                raise DomainError('analysis_paused', '迁移后自动观测已暂停，请先显式启用本场分析')
            media_row = db.execute('SELECT s.*,r.status,r.input FROM standard_media_runs s JOIN research_runs r ON r.id=s.run_id WHERE s.run_id=? AND s.session_id=?', (run_id, sid)).fetchone()
            if media_row:
                snapshot = json.loads(media_row['input'])
                identity = snapshot['media_identity']
                if media_row['status'] not in ('failed', 'partial'):
                    raise DomainError('invalid_retry', '只能重试本场失败或部分完成的默认观测', 422)
                if not self._media_current(db, sid, identity):
                    raise DomainError('stale_research_input', '回答媒体或分析计划已更新，不能重试旧输入')
                latest = db.execute('''SELECT max(attempt) FROM standard_media_runs WHERE session_id=? AND answer_id=?
                    AND media_hash=? AND plan_version=? AND phase=?''',
                    (sid, media_row['answer_id'], media_row['media_hash'], media_row['plan_version'], media_row['phase'])).fetchone()[0]
                if latest != media_row['attempt']:
                    raise DomainError('retry_exists', '该阶段已有后续重试，请查看最新结果')
                if latest >= 3:
                    raise DomainError('retry_limit', '本媒体阶段已运行3次；请检查错误或另建研究实验', 422)
                revision = db.execute('SELECT revision FROM answers WHERE id=?', (media_row['answer_id'],)).fetchone()[0]
                plan = json.loads(session['config'])['analysis_plan']
                phase = next(p for p in _media_phases(plan) if p['id'] == media_row['phase'])
                rid = self._insert_standard_media(db, sid, media_row['answer_id'], revision, media_row['media_hash'], plan, phase, latest + 1)
                return self.core._remember(db, 'standard-retry:' + sid, idempotency_key, request,
                    {'run_id': rid, 'status': 'queued', 'phase': phase['id'], 'version': self.core._session(db, sid)['version']})
            row = db.execute('SELECT s.*,r.status,r.input FROM standard_analysis_runs s JOIN research_runs r ON r.id=s.run_id WHERE s.run_id=? AND s.session_id=?', (run_id, sid)).fetchone()
            if not row or row['status'] not in ('failed', 'partial'):
                raise DomainError('invalid_retry', '只能重试本场失败或部分完成的默认观测', 422)
            answer = db.execute('SELECT revision,current FROM answers WHERE id=? AND session_id=?', (row['answer_id'], sid)).fetchone()
            if not answer or not answer['current'] or answer['revision'] != row['revision']:
                raise DomainError('stale_research_input', '回答已更新，不能将旧输入重试为当前结果')
            latest = db.execute('SELECT max(attempt) FROM standard_analysis_runs WHERE session_id=? AND answer_id=? AND revision=?', (sid, row['answer_id'], row['revision'])).fetchone()[0]
            if latest != row['attempt']:
                raise DomainError('retry_exists', '该运行已有后续重试，请查看最新结果')
            if latest >= 3:
                raise DomainError('retry_limit', '本版本默认观测已运行3次；请检查错误或另建研究实验', 422)
            rid = self._insert_standard(db, sid, row['answer_id'], row['revision'], json.loads(row['input'])['modules'], latest + 1)
            return self.core._remember(db, 'standard-retry:' + sid, idempotency_key, request,
                {'run_id': rid, 'status': 'queued', 'version': self.core._session(db, sid)['version']})

    def analysis_status(self, sid, include_results=False):
        # Querying progress cannot start or repeat local or paid model execution.
        with self.core._db(True) as db:
            session = self.core._session(db, sid)
            state = self.core._state(db, sid, True)
            standard = standard_snapshot(db, sid, json.loads(session['config']), include_results=include_results)
            current = {a['answer_id']: a['transcript_revision'] for a in state['answers'] if a['current']}
            stages = {}
            for kind in ('media', 'content'):
                jobs = []
                for row in db.execute('SELECT * FROM jobs WHERE session_id=? AND job_type=?', (sid, kind)):
                    if row['status'] == 'superseded':
                        continue
                    inputs = json.loads(row['input_versions'] or '{}')
                    if kind == 'media' and row['answer_id'] not in current:
                        continue
                    if kind == 'content':
                        expected = {a['answer_id']: a['transcript_revision'] for a in state['answers']
                                    if a['current'] and row['question_id'] in (a['question_id'], a['parent_question_id'])}
                        if not inputs or inputs != expected:
                            continue
                    jobs.append(row)
                stages[kind] = {'total': len(jobs), 'pending': sum(j['status'] in ('queued', 'running') for j in jobs),
                    'failed': sum(j['status'] in ('failed', 'blocked') for j in jobs),
                    'jobs': [{'job_id': j['id'], 'answer_id': j['answer_id'], 'status': j['status'], 'error': j['error']} for j in jobs]}
            report = self.core._report(db, sid)
        return {'session_id': sid, 'version': report['version'], **stages, 'multimodal': standard,
                'reviewed_question_count': report.get('reviewed_question_count', 0), 'report_lifecycle': report['report_lifecycle']}

    def create_runs(self, sid, *, version, selections, modules, repeats, purpose, condition, participant_group,
                    cloud_consent, idempotency_key, experiment_id=None):
        if not selections or len(selections) > 4 or not modules or not set(modules) <= MODULES or len(set(modules)) != len(modules):
            raise DomainError('invalid_research_input', '请选择1至4条回答及有效研究模块', 422)
        if type(repeats) is not int or repeats not in (1, 2, 3):
            raise DomainError('invalid_repeats', '独立重复次数为1至3次', 422)
        if not purpose.strip() or not condition.strip() or not participant_group.strip():
            raise DomainError('research_description_required', '请记录研究目的、条件及参与者分组代号', 422)
        if 'semantic' in modules and cloud_consent is not True:
            raise DomainError('cloud_authorization_required', '语义重复实验须明确授权将所选文字发送至已配置百炼接口', 422)
        request = dict(version=version, selections=selections, modules=modules, repeats=repeats, purpose=purpose,
                       condition=condition, participant_group=participant_group, cloud_consent=cloud_consent, experiment_id=experiment_id)
        with self.core._db(True) as db:
            prior = self.core._idempotent(db, 'research-create:' + sid, idempotency_key, request)
            if prior:
                return prior
            snapshot = self._snapshot(db, sid, selections)
            if snapshot['session_version'] != version:
                raise DomainError('version_conflict', '面试已更新，请核对当前版本后重新创建研究运行')
            if db.execute("SELECT count(*) FROM research_runs WHERE session_id=? AND status IN ('queued','running')", (sid,)).fetchone()[0] + repeats > 20:
                raise DomainError('queue_full', '本场待处理研究运行已达上限', 429)
            if 'semantic' in modules:
                from interview_credentials import public_settings
                from interview_llm import provider_status
                settings = public_settings()
                if snapshot['model_config'].get('base_url', '').rstrip('/') != settings['base_url'] or snapshot['model_config'].get('model') != settings['model']:
                    raise DomainError('model_configuration_changed', '会话模型快照与当前授权配置不一致；没有发送文字')
                if provider_status(snapshot['model_config'])['status'] == 'not_configured':
                    raise DomainError('provider_not_configured', '当前百炼接口未配置', 422)
            if experiment_id:
                row = db.execute('SELECT payload FROM research_experiments WHERE id=? AND session_id=?', (experiment_id, sid)).fetchone()
                if not row:
                    raise DomainError('not_found', '研究实验不存在', 404)
                experiment = json.loads(row[0])
                if experiment['input_versions'] != snapshot['input_versions'] or experiment['participant_group'] != participant_group:
                    raise DomainError('experiment_input_conflict', '同一实验须使用相同素材版本和参与者分组；改变素材请新建实验')
            else:
                experiment_id = uid()
                experiment = {'experiment_id': experiment_id, 'session_id': sid, 'purpose': purpose.strip(), 'participant_group': participant_group.strip(),
                              'input_versions': snapshot['input_versions'], 'created_at': utc(), 'protocol_version': 'research-protocol.v1'}
                db.execute('INSERT INTO research_experiments VALUES(?,?,?,?)', (experiment_id, sid, dumps(experiment), experiment['created_at']))
            runs = []
            for index in range(repeats):
                rid, created = uid(), utc()
                payload = {**snapshot, 'run_id': rid, 'experiment_id': experiment_id, 'modules': modules,
                           'condition': condition.strip(), 'purpose': purpose.strip(), 'participant_group': participant_group.strip(),
                           'repeat_index': index + 1, 'cache_policy': 'independent_inference_no_result_cache',
                           'cloud_authorized': 'semantic' in modules and cloud_consent, 'created_at': created}
                db.execute('INSERT INTO research_runs(id,session_id,experiment_id,status,input,created_at) VALUES(?,?,?,?,?,?)', (rid, sid, experiment_id, 'queued', dumps(payload), created))
                runs.append({'run_id': rid, 'status': 'queued', 'repeat_index': index + 1})
            return self.core._remember(db, 'research-create:' + sid, idempotency_key, request,
                                       {'experiment': experiment, 'runs': runs, 'session_version_unchanged': version})

    def list_runs(self, sid):
        with self.core._db() as db:
            self.core._session(db, sid)
            runs = []
            for row in db.execute('SELECT * FROM research_runs WHERE session_id=? ORDER BY created_at DESC,id DESC LIMIT 200', (sid,)):
                data = json.loads(row['input'])
                runs.append({k: row[k] for k in ('status', 'created_at', 'started_at', 'completed_at', 'error')} |
                            {k: data[k] for k in ('run_id', 'experiment_id', 'modules', 'condition', 'purpose', 'repeat_index', 'input_versions', 'participant_group', 'cache_policy')} |
                            {'run_kind': data.get('run_kind', 'independent_research')})
            return {'runs': runs, 'experiments': [json.loads(r[0]) for r in db.execute('SELECT payload FROM research_experiments WHERE session_id=? ORDER BY created_at DESC', (sid,))]}

    def get_run(self, sid, rid):
        with self.core._db() as db:
            row = db.execute('SELECT * FROM research_runs WHERE session_id=? AND id=?', (sid, rid)).fetchone()
            if not row:
                raise DomainError('not_found', '本场研究运行不存在', 404)
            return public({'run_id': rid, 'experiment_id': row['experiment_id'], 'status': row['status'], 'error': row['error'],
                           'created_at': row['created_at'], 'started_at': row['started_at'], 'completed_at': row['completed_at'],
                           'input': json.loads(row['input']), 'result': json.loads(row['result']) if row['result'] else None})

    def claim(self, lease_seconds=120, lane='all'):
        if lane not in ('all', 'core', 'extended'):
            raise ValueError('Unsupported research worker lane')
        self.enqueue_standard()
        now = time.time()
        with self.core._db(True) as db:
            db.execute('''UPDATE research_runs SET status='failed',completed_at=?,
                error='回答版本已更新；旧版本默认观测未执行。' WHERE status='queued' AND id IN (
                SELECT s.run_id FROM standard_analysis_runs s LEFT JOIN answers a ON a.id=s.answer_id
                WHERE a.id IS NULL OR a.current!=1 OR a.revision!=s.revision)''', (utc(),))
            obsolete = db.execute('''SELECT r.id,r.session_id,r.input FROM research_runs r
                JOIN standard_media_runs s ON s.run_id=r.id WHERE r.status='queued' ''').fetchall()
            for candidate in obsolete:
                if not self._media_current(db, candidate['session_id'], json.loads(candidate['input'])['media_identity']):
                    db.execute("UPDATE research_runs SET status='failed',completed_at=?,error='回答媒体或分析计划已更新；旧媒体阶段未执行。' WHERE id=?", (utc(), candidate['id']))
            # Do not silently repeat a paid call after a crashed worker.
            expired = db.execute("""SELECT r.id,r.session_id FROM research_runs r WHERE r.status='running' AND r.lease_until<?
                AND (EXISTS(SELECT 1 FROM standard_analysis_runs s WHERE s.run_id=r.id)
                     OR EXISTS(SELECT 1 FROM standard_media_runs s WHERE s.run_id=r.id))""", (now,)).fetchall()
            db.execute("UPDATE research_runs SET status='failed',error='研究运行进程中断；如需重复请显式新建运行。',completed_at=? WHERE status='running' AND lease_until<?", (utc(), now))
            for item in expired:
                self.core._bump(db, item['session_id'])
            lane_filter = ''
            core_filter = "(json_extract(input,'$.run_kind')='standard_local' OR (json_extract(input,'$.run_kind')='standard_media' AND json_extract(input,'$.analysis_phase')='core'))"
            if lane == 'core':
                lane_filter = ' AND ' + core_filter
            elif lane == 'extended':
                lane_filter = ' AND NOT COALESCE(' + core_filter + ',0)'
            row = db.execute("""SELECT * FROM research_runs WHERE status='queued'
                AND NOT (COALESCE(json_extract(input,'$.run_kind'),'') IN ('standard_local','standard_media')
                    AND EXISTS(SELECT 1 FROM standard_analysis_controls c WHERE c.session_id=research_runs.session_id AND c.paused=1))"""
                + lane_filter + ' ORDER BY created_at,id LIMIT 1').fetchone()
            if not row:
                return None
            token = uid()
            db.execute("UPDATE research_runs SET status='running',started_at=?,lease_token=?,lease_until=? WHERE id=?", (utc(), token, now + lease_seconds, row['id']))
            if json.loads(row['input']).get('run_kind') in ('standard_local', 'standard_media'):
                self.core._bump(db, row['session_id'])
            return {'run_id': row['id'], 'lease_token': token, 'input': json.loads(row['input'])}

    def heartbeat(self, rid, token):
        with self.core._db(True) as db:
            changed = db.execute("UPDATE research_runs SET lease_until=? WHERE id=? AND lease_token=? AND status='running' AND lease_until>=?", (time.time() + 120, rid, token, time.time())).rowcount
            if not changed:
                raise DomainError('lease_conflict', '研究运行租约已失效')

    def complete(self, rid, token, result=None, error=None):
        with self.core._db(True) as db:
            row = db.execute("SELECT * FROM research_runs WHERE id=? AND lease_token=? AND status='running' AND lease_until>=?", (rid, token, time.time())).fetchone()
            if not row:
                raise DomainError('lease_conflict', '研究运行租约已失效')
            if result and (result.get('run_id') != rid or result.get('input_versions') != json.loads(row['input'])['input_versions']):
                raise DomainError('wrong_research_output', '研究输出不对应冻结输入')
            frozen = json.loads(row['input'])
            if result and frozen.get('run_kind') == 'standard_media':
                identity = frozen['media_identity']
                if (result.get('media_identity', identity) != identity
                    or any(output.get('answer_id') != identity['answer_id']
                           or output.get('media_sha256', identity['media_sha256']) != identity['media_sha256']
                           for output in result.get('multimodal', []))):
                    raise DomainError('wrong_research_output', '研究输出不对应冻结媒体身份')
            state = result.get('status', 'failed') if result else 'failed'
            if state not in FINAL:
                raise DomainError('invalid_research_state', '研究结果状态不合法')
            encoded = dumps(public(result)) if result else None
            if encoded and len(encoded) > 8 * 1024 * 1024:
                raise DomainError('oversized_research_result', '研究输出超过8MB限制', 422)
            db.execute('UPDATE research_runs SET status=?,result=?,error=?,completed_at=?,lease_token=NULL,lease_until=NULL WHERE id=?', (state, encoded, error, utc(), rid))
            snapshot = json.loads(row['input'])
            if snapshot.get('run_kind') == 'standard_local':
                # Historical/late runs stay inspectable but cannot revise a current report.
                current = {r['id']: r['revision'] for r in db.execute('SELECT id,revision FROM answers WHERE session_id=? AND current=1', (row['session_id'],))}
                if all(current.get(aid) == rev for aid, rev in snapshot['input_versions'].items()):
                    self.core._bump(db, row['session_id'])
            elif snapshot.get('run_kind') == 'standard_media' and self._media_current(db, row['session_id'], snapshot['media_identity']):
                self.core._bump(db, row['session_id'])
            return {'run_id': rid, 'status': state}

    def annotations(self, sid):
        with self.core._db() as db:
            self.core._session(db, sid)
            return {'annotations': [json.loads(r[0]) for r in db.execute('SELECT payload FROM reference_annotations WHERE session_id=? ORDER BY created_at', (sid,))]}

    def annotate(self, sid, *, experiment_id, rater, exposure, ratings, note, idempotency_key):
        if exposure not in ('blind_self_declared', 'ai_seen') or not rater.strip() or not note.strip():
            raise DomainError('invalid_annotation', '请记录标注者代号、是否已看AI及标注说明', 422)
        request = dict(experiment_id=experiment_id, rater=rater, exposure=exposure, ratings=ratings, note=note)
        with self.core._db(True) as db:
            prior = self.core._idempotent(db, 'research-annotation:' + sid, idempotency_key, request)
            if prior:
                return prior
            run = db.execute('SELECT input FROM research_runs WHERE session_id=? AND experiment_id=? ORDER BY created_at LIMIT 1', (sid, experiment_id)).fetchone()
            if not run:
                raise DomainError('not_found', '研究实验不存在', 404)
            snapshot = json.loads(run[0])
            qids = {a['parent_question_id'] or a['question_id'] for a in snapshot['answers']}
            if not ratings or len({r['question_id'] for r in ratings}) != len(ratings) or not {r['question_id'] for r in ratings} <= qids:
                raise DomainError('invalid_annotation', '标注题目须属于所选实验且不能重复', 422)
            for rating in ratings:
                if rating['score'] is not None and (type(rating['score']) is not int or rating['score'] not in range(1, 6)):
                    raise DomainError('invalid_score', '标注分值须为1至5或null', 422)
                if not rating.get('reason', '').strip() or (rating['score'] is not None and not rating.get('evidence')):
                    raise DomainError('evidence_required', '数字标注需要原话引用和理由', 422)
                for ref in rating.get('evidence', []):
                    answer = next((a for a in snapshot['answers'] if a['answer_id'] == ref['answer_id'] and a['transcript_revision'] == ref['transcript_revision']), None)
                    if not answer or (answer['parent_question_id'] or answer['question_id']) != rating['question_id'] or not ref.get('quote', '').strip() or ref['quote'] not in answer['transcript']:
                        raise DomainError('invalid_evidence', '标注引用须精确对应实验冻结文字及题目', 422)
            value = {**request, 'annotation_id': uid(), 'session_id': sid, 'input_versions': snapshot['input_versions'],
                     'created_at': utc(), 'version': 1, 'independence': 'self_declared_not_audited', 'usage': 'research_reference_only'}
            db.execute('INSERT INTO reference_annotations VALUES(?,?,?,?,?)', (value['annotation_id'], sid, experiment_id, dumps(value), value['created_at']))
            return self.core._remember(db, 'research-annotation:' + sid, idempotency_key, request, value)

    def reference(self, sid, *, version, targets, title, source, status, idempotency_key):
        if not targets or not set(targets) <= {f['factor_id'] for f in FACTORS} or any(type(v) is not int or v not in range(1, 6) for v in targets.values()):
            raise DomainError('invalid_reference', '参考锚点须为已知因子的1至5分，不填写的因子保持未设置', 422)
        if not title.strip() or not source.strip() or status not in ('draft', 'teacher_confirmed'):
            raise DomainError('invalid_reference', '参考要求需要名称、来源和明确状态', 422)
        request = dict(version=version, targets=targets, title=title, source=source, status=status)
        with self.core._db(True) as db:
            prior = self.core._idempotent(db, 'reference:' + sid, idempotency_key, request)
            if prior:
                return prior
            session = self.core._session(db, sid)
            if version != session['version']:
                raise DomainError('version_conflict', '面试报告已更新，请刷新后核对')
            number = db.execute('SELECT COALESCE(MAX(version),0)+1 FROM reference_profiles WHERE session_id=?', (sid,)).fetchone()[0]
            value = {'reference_profile_id': uid(), 'version': number, 'targets': targets, 'title': title.strip(), 'source': source.strip(),
                     'status': status, 'scale': 'proposed_1_to_5', 'config_version': json.loads(session['config'])['version'],
                     'created_at': utc(), 'interpretation': '老师研究参考锚点；未经验证的资格阈值，不能据此决定录用或淘汰。'}
            db.execute('INSERT INTO reference_profiles VALUES(?,?,?,?,?)', (value['reference_profile_id'], sid, number, dumps(value), value['created_at']))
            self.core._bump(db, sid)
            result = {'reference_profile': value, 'report': self.core._report(db, sid)}
            return self.core._remember(db, 'reference:' + sid, idempotency_key, request, result)

    def publish(self, sid, *, version, run_ids, note, idempotency_key):
        if not 1 <= len(run_ids) <= 6 or len(set(run_ids)) != len(run_ids) or not note.strip():
            raise DomainError('invalid_publication', '请选择1至6个不同的完成运行并记录说明', 422)
        request = dict(version=version, run_ids=run_ids, note=note)
        with self.core._db(True) as db:
            prior = self.core._idempotent(db, 'research-publish:' + sid, idempotency_key, request)
            if prior:
                return prior
            session = self.core._session(db, sid)
            if version != session['version']:
                raise DomainError('version_conflict', '报告已更新，请刷新后重新核对')
            current = {r['id']: r['revision'] for r in db.execute('SELECT id,revision FROM answers WHERE session_id=? AND current=1', (sid,))}
            selected, versions = [], {}
            for rid in run_ids:
                row = db.execute('SELECT * FROM research_runs WHERE id=? AND session_id=?', (rid, sid)).fetchone()
                if not row or row['status'] not in ('succeeded', 'partial') or not row['result']:
                    raise DomainError('run_not_ready', '所选运行尚未完成或不属于本场', 422)
                snapshot = json.loads(row['input'])
                if any(current.get(aid) != rev for aid, rev in snapshot['input_versions'].items()):
                    raise DomainError('stale_research_input', '历史素材运行可保留研究，但不能发布为当前回答的报告附录')
                versions.update(snapshot['input_versions'])
                selected.append({'run_id': rid, 'experiment_id': row['experiment_id'], 'modules': snapshot['modules'],
                                 'condition': snapshot['condition'], 'cache_policy': snapshot['cache_policy'],
                                 'completed_at': row['completed_at'], 'result': json.loads(row['result'])})
            value = {'publication_id': uid(), 'selected_runs': selected, 'input_versions': versions, 'note': note.strip(),
                     'created_at': utc(), 'usage': 'observed_only', 'input_stale': False}
            db.execute('INSERT INTO research_publications VALUES(?,?,?,?)', (value['publication_id'], sid, dumps(value), value['created_at']))
            self.core._bump(db, sid)
            result = self.core._report(db, sid)
            return self.core._remember(db, 'research-publish:' + sid, idempotency_key, request, result)

    def compare(self, sid, ids):
        if not 1 <= len(ids) <= 6 or len(set(ids)) != len(ids):
            raise DomainError('invalid_comparison', '请选择1至6个不同的研究运行', 422)
        runs = [self.get_run(sid, rid) for rid in ids]
        first = runs[0]['input']
        comparable = all(r['input']['input_versions'] == first['input_versions'] and r['input']['participant_group'] == first['participant_group'] for r in runs)
        annotations = [a for a in self.annotations(sid)['annotations'] if a['input_versions'] == first['input_versions'] and a['experiment_id'] in {r['experiment_id'] for r in runs}]
        rows = []
        for run in runs:
            result = run.get('result') or {}
            for semantic in result.get('semantic', []):
                evaluation = semantic.get('evaluation') or {}
                score = evaluation.get('rating', {}).get('score')
                for annotation in annotations:
                    reference = next((x for x in annotation['ratings'] if x['question_id'] == evaluation.get('question_id')), None)
                    if reference:
                        rows.append({'run_id': run['run_id'], 'question_id': evaluation['question_id'], 'annotation_id': annotation['annotation_id'],
                                     'exposure': annotation['exposure'], 'model_score': score, 'reference_score': reference['score'],
                                     'absolute_difference': abs(score - reference['score']) if comparable and score is not None and reference['score'] is not None else None})
        blind = [x['absolute_difference'] for x in rows if x['exposure'] == 'blind_self_declared' and x['absolute_difference'] is not None]
        return {'runs': runs, 'same_inputs': comparable, 'annotations': annotations, 'differences': rows,
                'blind_reference_mae': sum(blind) / len(blind) if blind else None, 'blind_scored_pairs': len(blind),
                'limitations': '描述性对照；盲评为标注者自报，未验证独立性。样本不足不能推断效度；词频不是能力分。'}

    def export(self, sid):
        listed = self.list_runs(sid)
        with self.core._db() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM research_runs WHERE session_id=? ORDER BY created_at,id', (sid,))]
            extra = {table: [json.loads(r[0]) for r in db.execute('SELECT payload FROM '+table+' WHERE session_id=? ORDER BY created_at,rowid', (sid,))]
                     for table in ('reference_profiles', 'research_publications', 'alignment_reviews')}
        return {'schema_version': 'interview-research-export.v1', 'exported_at': utc(), 'session_id': sid,
                'experiments': listed['experiments'], 'runs': [self.get_run(sid, rid) for rid in ids], **public(extra),
                'annotations': self.annotations(sid)['annotations'], 'usage': '模拟测试／方法验证', 'contains_raw_media': False}


def execute_run(snapshot, output_dir):
    """Runs in an isolated worker child with frozen inputs and no score writes."""
    begin, started = time.monotonic(), utc()
    selected = snapshot['modules']
    result = {'schema_version': 'research-result.v1', 'run_id': snapshot['run_id'], 'input_versions': snapshot['input_versions'],
              'started_at': started, 'cache_policy': 'independent_inference_no_result_cache', 'result_cache_reused': False,
              'environment': {'python': sys.version.split()[0], 'platform': platform.platform()}, 'semantic': [], 'word_frequency': [], 'multimodal': [], 'errors': []}
    if snapshot.get('run_kind') == 'standard_media':
        result.update(media_identity=snapshot['media_identity'], analysis_phase=snapshot['analysis_phase'])
    for question in snapshot['questions']:
        answers = [a for a in snapshot['answers'] if (a['parent_question_id'] or a['question_id']) == question['question_id']]
        if not answers:
            continue
        if 'word_frequency' in selected:
            from interview_text_research import run_text_baseline
            result['word_frequency'].append(run_text_baseline(answers, question))
        if 'semantic' in selected:
            try:
                from interview_llm import evaluate_answer
                from interview_credentials import public_settings
                configured = public_settings()
                if not snapshot.get('cloud_authorized') or snapshot['model_config'].get('base_url', '').rstrip('/') != configured['base_url'].rstrip('/') or snapshot['model_config'].get('model') != configured['model']:
                    raise ValueError('provider binding changed')
                result['semantic'].append(evaluate_answer(question, answers, snapshot['branches'], snapshot['model_config']))
            except Exception:
                result['errors'].append({'module': 'semantic', 'question_id': question['question_id'], 'reason': '语义运行失败或提供方绑定已变化；未生成替代分数。'})
    multimodal = [m for m in selected if m in {'au', 'gaze', 'expression', 'audio', 'au_net', 'gaze360', 'emotic'}]
    if multimodal:
        from interview_multimodal import run_multimodal
        for answer in snapshot['answers']:
            try:
                media = answer.get('_media_path')
                if media and hashlib.sha256(Path(media).read_bytes()).hexdigest() != answer['media_sha256']:
                    raise ValueError('media changed')
                output = run_multimodal(media, public(answer), Path(output_dir) / answer['answer_id'], modules=multimodal)
                result['multimodal'].append({'answer_id': answer['answer_id'], 'transcript_revision': answer['transcript_revision'], **output})
            except Exception:
                result['errors'].append({'module': 'multimodal', 'answer_id': answer['answer_id'], 'reason': '原片或模型运行不可用；请检查模块状态，不填充观测。'})
    successes = len(result['semantic']) + len(result['word_frequency'])
    incomplete = bool(result['errors'])
    for output in result['multimodal']:
        for module in output.get('modules', {}).values():
            state = module.get('status')
            successes += int(state in ('complete', 'succeeded', 'success', 'ok', 'partial'))
            incomplete |= state not in ('complete', 'succeeded', 'success', 'ok')
    result.update(status='partial' if successes and incomplete else 'succeeded' if successes else 'failed',
                  completed_at=utc(), duration_seconds=round(time.monotonic() - begin, 3))
    return public(result)
