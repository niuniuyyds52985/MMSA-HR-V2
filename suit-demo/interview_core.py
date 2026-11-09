"""Durable local interview domain. Never imports the legacy affect classifiers."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from interview_credentials import credential_available


BASE = Path(__file__).resolve().parent


class DomainError(Exception):
    def __init__(self, code, message, status=409):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


def utc():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def uid():
    return uuid.uuid4().hex


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def compact_report(report):
    """Return a read-only projection; never rewrite a frozen report snapshot.

    Keep judgments, evidence, model identities and summaries. Dense observation
    arrays are available via the existing same-session run endpoint.
    """
    def project(value):
        if isinstance(value, list):
            return [project(item) for item in value]
        if isinstance(value, dict):
            result = {key: project(item) for key, item in value.items() if key != 'observations'}
            if isinstance(value.get('observations'), list):
                result['observation_count'] = len(value['observations'])
                result['observations_omitted'] = True
            return result
        return value
    result = dict(report)
    for field in ('standard_analysis', 'research_appendix'):
        if result.get(field):
            result[field] = project(result[field])
    result['projection'] = {'mode': 'compact', 'frozen_version': report['version'],
                            'observation_detail': 'GET /v1/interviews/{session_id}/research/runs/{run_id}'}
    return result


class InterviewCore:
    def __init__(self, data_dir=None, config_path=None):
        self.data_dir = Path(data_dir or os.environ.get("INTERVIEW_DATA_DIR", BASE / "interview-data")).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "media").mkdir(exist_ok=True)
        self.db_path = self.data_dir / "interviews.sqlite3"
        self.config_path = Path(config_path or BASE / "config" / "interview.v2.json")
        self.config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
        self._validate_config(self.config)
        bank_path = self.config_path.parent / "question-bank.v1.json"
        if bank_path.exists():
            bank = json.loads(bank_path.read_text(encoding="utf-8-sig"))
            normalize = lambda value: value.replace('“', '"').replace('”', '"')
            canonical_questions = {q["question_id"]: q for q in bank["questions"]}
            canonical_branches = {b["branch_id"]: b for b in bank["branches"]}
            for question in self.config["questions"]:
                canonical = canonical_questions.get(question["question_id"])
                if not canonical or normalize(canonical["stem"]) != normalize(question["text"]) or canonical["activation_status"] != "proposed_dev":
                    raise ValueError("Active question does not match source bank")
                question["rubric_id"] = canonical["rubric_id"]
                question["source_file_hash"] = canonical["source_file_hash"]
            if len({b["branch_id"] for b in self.config["branches"]}) != len(self.config["branches"]):
                raise ValueError("Duplicate branch ID")
            for branch in self.config["branches"]:
                canonical = canonical_branches.get(branch["branch_id"])
                if not canonical or canonical["template"] != branch["text"] or canonical["parent_question_id"] != branch["parent_question_id"] or canonical["review_status"] != "proposed_dev":
                    raise ValueError("Active branch does not match source bank allowlist")
            self.config["provenance"].update({"bank_version": bank["bank_version"], "bank_sha256": hashlib.sha256(bank_path.read_bytes()).hexdigest(), "bank_question_count": len(bank["questions"]), "status": "source_workbook_verified"})
        from interview_credentials import public_settings
        self.config["model_config"] = public_settings()
        # Only newly created v2 sessions receive the plan. Stored historical configs
        # remain unchanged; merely opening a report never schedules inference.
        if self.config.get('followup_timing') == 'after_each_main':
            self.config.setdefault('analysis_plan', {'version': 'local-observations.v1',
                'enabled': True, 'modules': ['au', 'gaze', 'expression', 'audio'],
                'trigger': 'after_media_terminal', 'usage': 'observed_only',
                'automatic_report_snapshot': True, 'cloud_media': False})
        secret_file = self.data_dir / ".token-secret"
        try:
            with secret_file.open("xb") as stream:
                stream.write(secrets.token_bytes(32))
        except FileExistsError:
            pass
        self.secret = secret_file.read_bytes()
        if len(self.secret) != 32:
            raise RuntimeError("Invalid local token secret")
        with self._db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,candidate_ref TEXT,created_at TEXT,status TEXT,version INTEGER,config TEXT,candidate_hash TEXT,hr_hash TEXT);
            CREATE TABLE IF NOT EXISTS rounds(id TEXT PRIMARY KEY,session_id TEXT,ordinal INTEGER,question_id TEXT,kind TEXT,text TEXT,parent_question_id TEXT,issued_at TEXT,UNIQUE(session_id,ordinal));
            CREATE TABLE IF NOT EXISTS answers(id TEXT PRIMARY KEY,session_id TEXT,round_id TEXT,attempt_id TEXT,path TEXT,media_hash TEXT,media_type TEXT,duration_ms INTEGER,language TEXT,created_at TEXT,revision INTEGER,current INTEGER,UNIQUE(session_id,attempt_id));
            CREATE UNIQUE INDEX IF NOT EXISTS one_current_answer ON answers(round_id) WHERE current=1;
            CREATE TABLE IF NOT EXISTS revisions(answer_id TEXT,version INTEGER,text TEXT,reason TEXT,actor TEXT,created_at TEXT,PRIMARY KEY(answer_id,version));
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,session_id TEXT,answer_id TEXT,status TEXT,attempts INTEGER,lease_token TEXT,lease_until REAL,worker_id TEXT,input_revision INTEGER,result TEXT,error TEXT,created_at TEXT,ready_at TEXT);
            CREATE TABLE IF NOT EXISTS operations(scope TEXT,key TEXT,payload_hash TEXT,response TEXT,PRIMARY KEY(scope,key));
            CREATE TABLE IF NOT EXISTS decisions(id TEXT PRIMARY KEY,session_id TEXT UNIQUE,payload TEXT);
            CREATE TABLE IF NOT EXISTS assessments(id TEXT PRIMARY KEY,session_id TEXT,payload TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS reviews(id TEXT PRIMARY KEY,session_id TEXT,parent_version INTEGER,payload TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS reports(session_id TEXT,version INTEGER,payload TEXT,created_at TEXT,PRIMARY KEY(session_id,version));
            CREATE TABLE IF NOT EXISTS model_evaluations(id TEXT PRIMARY KEY,session_id TEXT,question_id TEXT,payload TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS job_failures(id TEXT PRIMARY KEY,job_id TEXT,attempt INTEGER,payload TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS cloud_authorizations(id TEXT PRIMARY KEY,session_id TEXT,payload TEXT,created_at TEXT);
            CREATE TABLE IF NOT EXISTS decision_events(id TEXT PRIMARY KEY,session_id TEXT,round_id TEXT,payload TEXT,UNIQUE(session_id,round_id));
            CREATE TRIGGER IF NOT EXISTS decision_events_immutable_update BEFORE UPDATE ON decision_events BEGIN SELECT RAISE(ABORT,'decision events are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS decision_events_immutable_delete BEFORE DELETE ON decision_events BEGIN SELECT RAISE(ABORT,'decision events are immutable'); END;
            """)
            columns = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            for name, declaration in (("job_type", "TEXT DEFAULT 'media'"), ("question_id", "TEXT"), ("input_versions", "TEXT"), ("error_code", "TEXT"), ("error_details", "TEXT"), ("retry_of", "TEXT"), ("manual_retry_count", "INTEGER DEFAULT 0")):
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
            session_columns = {r[1] for r in db.execute("PRAGMA table_info(sessions)")}
            if "decision_started" not in session_columns:
                db.execute("ALTER TABLE sessions ADD COLUMN decision_started REAL")

    @staticmethod
    def _validate_config(config):
        questions = config["questions"]
        if (not 1 <= len(questions) <= 8 or config["max_main"] != len(questions)
                or type(config["max_followup"]) is not int or not 0 <= config["max_followup"] <= 8
                or config["max_total"] != len(questions) + config["max_followup"]):
            raise ValueError("Invalid main/follow-up form budget")
        ids = {q["question_id"] for q in questions}
        if len(ids) != len(questions) or any(set(q["anchors"]) != set("12345") for q in questions):
            raise ValueError("Question IDs/anchors invalid")
        if any(b["parent_question_id"] not in ids for b in config["branches"]):
            raise ValueError("Invalid follow-up parent")

    @contextlib.contextmanager
    def _db(self, transaction=False):
        db = sqlite3.connect(str(self.db_path), timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            if transaction:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if transaction:
                db.commit()
        except BaseException:
            if transaction:
                db.rollback()
            raise
        finally:
            db.close()

    def _session(self, db, session_id):
        row = db.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            raise DomainError("not_found", "面试不存在", 404)
        return dict(row)

    def _token(self, session_id, role):
        return hmac.new(self.secret, f"{session_id}:{role}".encode(), hashlib.sha256).hexdigest()

    def authenticate(self, session_id, token, roles=("candidate", "hr")):
        hashed = hashlib.sha256(token.encode()).hexdigest()
        with self._db() as db:
            session = self._session(db, session_id)
        for role in roles:
            if hmac.compare_digest(hashed, session[f"{role}_hash"]):
                return role
        raise DomainError("forbidden", "该凭证无权访问此会话或操作", 403)

    def _idempotent(self, db, scope, key, payload):
        if not isinstance(key, str) or not 8 <= len(key) <= 200:
            raise DomainError("invalid_idempotency_key", "幂等键长度须为8至200字符", 422)
        row = db.execute("SELECT * FROM operations WHERE scope=? AND key=?", (scope, key)).fetchone()
        if row:
            if not hmac.compare_digest(row["payload_hash"], digest(payload)):
                raise DomainError("idempotency_conflict", "同一幂等键已用于不同内容")
            return json.loads(row["response"])
        return None

    def _remember(self, db, scope, key, payload, result):
        db.execute("INSERT INTO operations VALUES(?,?,?,?)", (scope, key, digest(payload), dumps(result)))
        return result

    def _bump(self, db, session_id):
        db.execute("UPDATE sessions SET version=version+1 WHERE id=?", (session_id,))

    def _issue(self, db, session_id, ordinal, question, kind="main"):
        db.execute("INSERT INTO rounds VALUES(?,?,?,?,?,?,?,?)", (uid(), session_id, ordinal, question.get("question_id", question.get("branch_id")), kind, question["text"], question.get("parent_question_id"), utc()))

    def create_interview(self, idempotency_key, candidate_ref="", form_mode=None):
        try:
            key_uuid = uuid.UUID(idempotency_key)
            if key_uuid.version != 4 or key_uuid.variant != uuid.RFC_4122:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise DomainError("invalid_creation_key", "创建幂等键必须为随机UUID4；该键是敏感恢复凭证", 422)
        if len(candidate_ref) > 200:
            raise DomainError("invalid_candidate_ref", "候选人引用过长", 422)
        if form_mode not in (None, "quick", "full"):
            raise DomainError("invalid_form_mode", "请选择短体验或完整八因子题组", 422)
        if form_mode is not None and not self.config.get("form_generation"):
            raise DomainError("form_not_supported", "此配置仅支持历史固定卷", 422)
        payload = {"candidate_ref": candidate_ref}
        if form_mode is not None:
            payload["form_mode"] = form_mode
        with self._db(True) as db:
            prior = self._idempotent(db, "create", idempotency_key, payload)
            if prior:
                session_id = prior["session_id"]
            else:
                session_id = uid()
                frozen = copy.deepcopy(self.config)
                if frozen.get("form_generation"):
                    from interview_forms import build_form
                    # Separate rotation for each use; the seed is not a capability token.
                    rotation = db.execute("SELECT count(*) FROM sessions WHERE json_extract(config,'$.form.mode')=?", (form_mode or "quick",)).fetchone()[0]
                    frozen = build_form(frozen, mode=form_mode or "quick", seed="cohort-cycle-v1", rotation_index=rotation)
                    self._validate_config(frozen)
                hashes = [hashlib.sha256(self._token(session_id, role).encode()).hexdigest() for role in ("candidate", "hr")]
                db.execute("INSERT INTO sessions(id,candidate_ref,created_at,status,version,config,candidate_hash,hr_hash) VALUES(?,?,?,?,?,?,?,?)", (session_id, candidate_ref, utc(), "answering", 1, dumps(frozen), *hashes))
                self._issue(db, session_id, 1, frozen["questions"][0])
                self._remember(db, "create", idempotency_key, payload, {"session_id": session_id})
            candidate_token, hr_token = [self._token(session_id, role) for role in ("candidate", "hr")]
            state = self._state(db, session_id)
            return {**state, "candidate_token": candidate_token, "hr_token": hr_token,
                    "candidate_url": f"/interview#session={session_id}&token={candidate_token}",
                    "enter_url": f"/interview#session={session_id}&token={candidate_token}",
                    "hr_url": f"/hr-review#session={session_id}&token={hr_token}",
                    "report_view_url": f"/interview-report#session={session_id}&token={hr_token}",
                    "report_url": f"/hr-review#session={session_id}&token={hr_token}"}

    def _answer(self, db, row, include_result=True):
        item = dict(row)
        item["answer_id"] = item.pop("id")
        item["has_media"] = bool(item.pop("path", None))
        item["transcript_revision"] = item.pop("revision")
        item["current"] = bool(item["current"])
        revision = db.execute("SELECT * FROM revisions WHERE answer_id=? AND version=?", (item["answer_id"], item["transcript_revision"])).fetchone()
        item["transcript"] = revision["text"] if revision else ""
        item["transcript_source"] = revision["actor"] if revision else None
        round_row = db.execute("SELECT question_id,parent_question_id FROM rounds WHERE id=?", (item["round_id"],)).fetchone()
        item.update(dict(round_row))
        item["revisions"] = [dict(r) for r in db.execute("SELECT * FROM revisions WHERE answer_id=? ORDER BY version", (item["answer_id"],))]
        job = db.execute("SELECT * FROM jobs WHERE answer_id=? AND job_type='media'", (item["answer_id"],)).fetchone()
        item["job_status"] = job["status"] if job else "not_requested"
        item["analysis_job_id"] = job["id"] if job else None
        item["analysis"] = json.loads(job["result"]) if include_result and job and job["result"] else None
        item["analysis_ready_at"] = job["ready_at"] if job else None
        content_job = db.execute("SELECT status FROM jobs WHERE answer_id=? AND job_type='content' ORDER BY created_at DESC LIMIT 1", (item["answer_id"],)).fetchone()
        item["content_status"] = content_job[0] if content_job else "not_requested"
        return item

    def _state(self, db, session_id, hr=False):
        session = self._session(db, session_id)
        config = json.loads(session["config"])
        rounds = [{**dict(r), "round_id": r["id"]} for r in db.execute("SELECT * FROM rounds WHERE session_id=? ORDER BY ordinal", (session_id,))]
        answers = [self._answer(db, r, hr) for r in db.execute("SELECT * FROM answers WHERE session_id=? ORDER BY created_at", (session_id,))]
        jobs = [dict(r) for r in db.execute("SELECT id AS job_id,answer_id,job_type,question_id,status,attempts,error,error_code,error_details,retry_of,manual_retry_count,input_versions,created_at,ready_at FROM jobs WHERE session_id=?", (session_id,))]
        from interview_job_errors import safe_diagnostics
        for job in jobs:
            job["input_versions"] = json.loads(job["input_versions"]) if job["input_versions"] else {}
            job["error_details"] = safe_diagnostics(json.loads(job["error_details"])) if job["error_details"] else {}
        wait_remaining=max(0, config["decision_wait_seconds"] - (time.time() - session["decision_started"])) if session.get("decision_started") else 0
        if session.get("decision_started") and config.get('policy_version')=='semantic-bank.v1' and rounds:
            qid=rounds[-1].get('parent_question_id') or rounds[-1]['question_id']
            active_route=next((j for j in reversed(jobs) if j['job_type']=='routing' and j['question_id']==qid),None)
            if active_route:
                wait_remaining=max(0,datetime.fromisoformat(active_route['created_at']).timestamp()+config.get('semantic_wait_seconds',6)-time.time())
        return {"session_id": session_id, "status": session["status"], "version": session["version"], "candidate_ref": session["candidate_ref"], "created_at": session['created_at'],
                "config_version": config["version"], "rubric_status": config["rubric_status"], "provenance": config["provenance"],
                "current_round": rounds[-1] if session["status"] == "answering" else None, "rounds": rounds, "answers": answers, "jobs": jobs,
                "followup_timing": config.get('followup_timing', 'after_all_main'),
                "analysis_plan": config.get('analysis_plan'),
                "form_mode": config.get("form", {}).get("mode", "legacy"),
                "main_question_count": len(config["questions"]), "max_followups": config["max_followup"],
                "decision_pending": bool(session.get("decision_started") and session["status"] == "answering" and (config.get('followup_timing') == 'after_each_main' or len(rounds) == 3)),
                "decision_wait_remaining": wait_remaining,
                "budget": {"main": sum(r["kind"] == "main" for r in rounds), "followup": sum(r["kind"] == "followup" for r in rounds), "total": len(rounds), "maximum": config["max_total"], **({"main_maximum": len(config["questions"]), "followup_maximum": config["max_followup"]} if config.get("form") else {})}}

    def get_state(self, session_id, hr=False):
        with self._db() as db:
            return self._state(db, session_id, hr)

    def get_answer_timeline(self, session_id, answer_id, transcript_revision):
        """Project a single consistent read snapshot; never generate/cache reports."""
        if type(transcript_revision) is not int or transcript_revision < 1:
            raise DomainError('invalid_revision', '必须指定正整数转写版本', 422)
        with self._db() as db:
            db.execute('BEGIN')
            self._session(db, session_id)
            row = db.execute('SELECT a.*,r.question_id,r.parent_question_id FROM answers a JOIN rounds r ON r.id=a.round_id WHERE a.session_id=? AND a.id=?', (session_id, answer_id)).fetchone()
            if not row:
                raise DomainError('not_found', '本场回答不存在', 404)
            answer = dict(row)
            revisions = [dict(r) for r in db.execute('SELECT * FROM revisions WHERE answer_id=? ORDER BY version', (answer_id,))]
            selected = next((r for r in revisions if r['version'] == transcript_revision), None)
            if selected is None:
                raise DomainError('not_found', '指定转写版本不存在', 404)
            jobs = []
            for r in db.execute("SELECT * FROM jobs WHERE session_id=? AND answer_id=? AND job_type='media' ORDER BY created_at,id", (session_id, answer_id)):
                item = dict(r)
                item['result'] = json.loads(item['result']) if item['result'] else None
                jobs.append(item)
            question_id = answer['parent_question_id'] or answer['question_id']
            sources = []
            def append_source(kind, source_id, reason, evidence):
                sources.append({'source': kind, 'source_id': source_id, 'reason': reason, 'evidence': evidence})
            for r in db.execute('SELECT payload FROM model_evaluations WHERE session_id=? AND question_id=? ORDER BY created_at,id', (session_id, question_id)):
                value = json.loads(r[0])
                for field, kind in (('rating', 'model_rating'), ('followup', 'model_followup')):
                    part = value.get(field) or {}
                    append_source(kind, value['evaluation_id'], part.get('reason'), part.get('evidence', []))
            for r in db.execute('SELECT payload FROM reviews WHERE session_id=? ORDER BY created_at,id', (session_id,)):
                value = json.loads(r[0])
                for rating in value.get('ratings', []):
                    if rating.get('question_id') == question_id:
                        append_source('hr_rating', value['review_id'], rating.get('reason'), rating.get('evidence', []))
            for r in db.execute('SELECT payload FROM assessments WHERE session_id=? ORDER BY created_at,id', (session_id,)):
                value = json.loads(r[0])
                if value.get('question_id') == question_id:
                    append_source('followup_assessment', value['assessment_id'], value.get('reason'), value.get('evidence', []))
            for table in ('decisions', 'decision_events'):
                for r in db.execute(f'SELECT payload FROM {table} WHERE session_id=? ORDER BY id', (session_id,)):
                    value = json.loads(r[0])
                    append_source('decision', value['decision_id'], value.get('reason_for_hr'), value.get('used_evidence', []))
            snapshot = {'answer': answer, 'revision': selected, 'revisions': revisions, 'jobs': jobs, 'sources': sources}
            manual_review = None
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='alignment_reviews'").fetchone():
                row = db.execute('SELECT payload FROM alignment_reviews WHERE session_id=? AND answer_id=? AND revision=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (session_id,answer_id,transcript_revision)).fetchone()
                manual_review = json.loads(row[0]) if row else None
        from interview_timeline import build_timeline
        timeline = build_timeline(snapshot)
        if manual_review:
            from interview_alignment import overlay, AlignmentError
            try:
                timeline = overlay(timeline, manual_review)
            except AlignmentError:
                timeline['alignment']['manual_review_status'] = 'stale'
                timeline['notices'].append('历史人工时间校准不再匹配此段落划分或原片，未应用；请重新核对。')
        return timeline

    def workbench_session_links(self, session_id):
        """Caller must hold the independent local operator capability."""
        with self._db() as db:
            session = self._session(db, session_id)
        tokens = {role: self._token(session_id, role) for role in ('candidate', 'hr')}
        # Never grant a capability if this data directory's secret no longer matches.
        if any(not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), session[f'{role}_hash'])
               for role, token in tokens.items()):
            raise DomainError('credentials_unavailable', '会话凭据与本机密钥不一致，无法恢复链接')
        return {'session_id': session_id,
                'candidate_url': f'/interview#session={session_id}&token={tokens["candidate"]}',
                'hr_url': f'/hr-review#session={session_id}&token={tokens["hr"]}',
                'report_url': f'/interview-report#session={session_id}&token={tokens["hr"]}',
                'research_url': f'/research#session={session_id}&token={tokens["hr"]}'}

    def _cloud_analysis_authorized(self, db, session_id):
        current = {r['id']: r['revision'] for r in db.execute(
            'SELECT id,revision FROM answers WHERE session_id=? AND current=1', (session_id,))}
        if not current:
            return False
        return any(json.loads(row[0]).get('input_versions') == current for row in db.execute(
            'SELECT payload FROM cloud_authorizations WHERE session_id=?', (session_id,)))

    def cloud_analysis_authorized(self, session_id):
        """Read-only worker check; authorization covers exactly the current answer versions."""
        with self._db() as db:
            return self._cloud_analysis_authorized(db, session_id)

    def authorize_cloud_analysis(self, session_id, idempotency_key, reason):
        """Explicit operator action. Never resumes another session or stale input jobs."""
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
            raise DomainError('reason_required', '生成本场内容草稿需要明确授权理由', 422)
        payload = {'reason': reason}
        with self._db(True) as db:
            prior = self._idempotent(db, f'cloud-analysis:{session_id}', idempotency_key, payload)
            if prior:
                return prior
            session = self._session(db, session_id)
            config = json.loads(session['config'])
            from interview_credentials import public_settings
            from interview_llm import provider_status
            settings = public_settings()
            model_config = config.get('model_config', {})
            if provider_status(model_config)['status'] == 'not_configured':
                raise DomainError('provider_not_configured', '请先配置本机内容模型接口')
            if (model_config.get('base_url', '').rstrip('/') != settings.get('base_url', '').rstrip('/')
                    or model_config.get('model') != settings.get('model')):
                raise DomainError('model_configuration_changed', '本场模型快照与当前配置不同；未更换模型或发送回答')
            answers = [dict(r) for r in db.execute('SELECT a.*,v.text FROM answers a JOIN revisions v ON v.answer_id=a.id AND v.version=a.revision WHERE a.session_id=? AND a.current=1', (session_id,))]
            if not any(a['text'].strip() for a in answers):
                raise DomainError('transcript_required', '本场尚无可分析的回答文字', 422)
            authorization_id, authorized_at = uid(), utc()
            authorization = {'authorization_id': authorization_id, 'session_id': session_id, 'reason': reason,
                             'actor': 'local_operator', 'authorized_at': authorized_at,
                             'input_versions': {a['id']: a['revision'] for a in answers},
                             'model_config': model_config}
            db.execute('INSERT INTO cloud_authorizations VALUES(?,?,?,?)',
                       (authorization_id, session_id, dumps(authorization), authorized_at))
            requeued = 0
            for job in db.execute("SELECT id,question_id,input_versions FROM jobs WHERE session_id=? AND job_type='content' AND status IN ('blocked','failed')", (session_id,)).fetchall():
                inputs = self._content_inputs(db, session_id, job['question_id'])
                actual = {a['answer_id']: a['transcript_revision'] for a in inputs}
                if actual != json.loads(job['input_versions']) or not any(a['transcript'].strip() for a in inputs):
                    continue
                db.execute("UPDATE jobs SET status='queued',attempts=0,error=NULL,ready_at=NULL,lease_token=NULL,lease_until=NULL,worker_id=NULL WHERE id=?", (job['id'],))
                requeued += 1
            self._bump(db, session_id)
            result = {'session_id': session_id, 'authorization_id': authorization_id, 'authorized_at': authorized_at,
                      'requeued': requeued, 'status': 'authorized'}
            return self._remember(db, f'cloud-analysis:{session_id}', idempotency_key, payload, result)

    def list_workbench_sessions(self):
        """A credential-free summary, never transcripts, stored secrets or role links."""
        sessions = []
        with self._db() as db:
            for row in db.execute('SELECT * FROM sessions ORDER BY created_at DESC').fetchall():
                sid = row['id']
                config = json.loads(row['config'])
                current = {r['id']: r['revision'] for r in db.execute('SELECT id,revision FROM answers WHERE session_id=? AND current=1', (sid,))}
                counts = {status: 0 for status in ('queued', 'running', 'succeeded', 'blocked', 'failed', 'not_requested', 'superseded')}
                inputs_by_question = {q['question_id']: {a['answer_id']: a['transcript_revision'] for a in self._content_inputs(db, sid, q['question_id'])} for q in config['questions']}
                for job in db.execute("SELECT status,question_id,input_versions FROM jobs WHERE session_id=? AND job_type='content'", (sid,)):
                    if json.loads(job['input_versions']) == inputs_by_question.get(job['question_id']):
                        counts[job['status']] = counts.get(job['status'], 0) + 1
                counts['not_requested'] = max(0, sum(bool(v) for v in inputs_by_question.values()) - sum(counts.values()))
                content_status = next((status for status in ('running', 'queued', 'blocked', 'failed', 'succeeded') if counts[status]), 'not_requested')
                reviews = [json.loads(r[0]) for r in db.execute('SELECT payload FROM reviews WHERE session_id=? ORDER BY parent_version,rowid', (sid,))]
                scored, reviewed, stale = 0, 0, False
                for question in config['questions']:
                    qid = question['question_id']
                    latest = next((r for r in reversed(reviews) if any(x['question_id'] == qid for x in r['ratings'])), None)
                    rating = next((r for r in latest['ratings'] if r['question_id'] == qid), None) if latest else None
                    if rating:
                        expected = rating.get('input_versions')
                        if expected is None:
                            related = {r['id'] for r in db.execute('SELECT a.id FROM answers a JOIN rounds r ON r.id=a.round_id WHERE a.session_id=? AND (r.question_id=? OR r.parent_question_id=?)', (sid, qid, qid))}
                            expected = {aid: rev for aid, rev in latest.get('input_versions', {}).items() if aid in related}
                        question_stale = expected != inputs_by_question[qid]
                        stale |= question_stale
                        reviewed += int(not question_stale and bool(expected))
                        scored += int(not question_stale and rating['score'] is not None)
                    else:
                        evaluation = next((json.loads(r[0]) for r in db.execute('SELECT payload FROM model_evaluations WHERE session_id=? AND question_id=? ORDER BY created_at DESC', (sid, qid)) if json.loads(r[0])['input_versions'] == inputs_by_question[qid]), None)
                        scored += int(bool(evaluation and evaluation['rating']['score'] is not None))
                rounds = db.execute('SELECT kind,id FROM rounds WHERE session_id=?', (sid,)).fetchall()
                main_answers = db.execute("SELECT COUNT(*) FROM answers a JOIN rounds r ON r.id=a.round_id WHERE a.session_id=? AND a.current=1 AND r.kind='main'", (sid,)).fetchone()[0]
                sessions.append({'session_id': sid, 'candidate_ref': row['candidate_ref'], 'created_at': row['created_at'],
                                 'status': row['status'], 'config_version': config['version'], 'answer_count': len(current),
                                 'main_answer_count': main_answers, 'round_count': len(rounds), 'content_status': content_status,
                                 'content_jobs': counts, 'form_mode': config.get('form', {}).get('mode', 'legacy'), 'main_question_count': len(config['questions']), 'max_followups': config['max_followup'], 'score_status': 'complete' if scored == len(config['questions']) and row['status'] == 'interview_finished' else 'incomplete',
                                 'review_status': 'stale' if stale else 'reviewed' if reviewed == len(config['questions']) else 'partial' if reviewed else 'not_reviewed',
                                 'reviewed_question_count': reviewed,
                                 'cloud_analysis_authorized': self._cloud_analysis_authorized(db, sid)})
        return {'sessions': sessions, 'total': len(sessions)}

    def submit_answer(self, session_id, round_id, attempt_id, idempotency_key, transcript="", path=None, media_meta=None, media_hash=None, language="zh", replace_answer_id=None):
        if not attempt_id or len(attempt_id) > 100 or len(transcript) > 30000:
            raise DomainError("invalid_answer", "回答版本或文字长度无效", 422)
        if not transcript.strip() and not path:
            raise DomainError("empty_answer", "请提交有效音视频或明确输入的文字回答", 422)
        media_meta = media_meta or {}
        payload = {"round_id": round_id, "attempt_id": attempt_id, "transcript": transcript, "media_hash": media_hash, "language": language, "replace_answer_id": replace_answer_id}
        with self._db(True) as db:
            prior = self._idempotent(db, f"answer:{session_id}", idempotency_key, payload)
            if prior:
                return prior
            session = self._session(db, session_id)
            latest = db.execute("SELECT * FROM rounds WHERE session_id=? ORDER BY ordinal DESC LIMIT 1", (session_id,)).fetchone()
            if session["status"] != "answering" or latest["id"] != round_id:
                raise DomainError("round_closed", "只能提交当前正式问题")
            old_attempt = db.execute("SELECT id FROM answers WHERE session_id=? AND attempt_id=?", (session_id, attempt_id)).fetchone()
            if old_attempt:
                raise DomainError("attempt_conflict", "该尝试版本已提交，请使用原幂等键或明确的新版本")
            current = db.execute("SELECT * FROM answers WHERE round_id=? AND current=1", (round_id,)).fetchone()
            if current and replace_answer_id != current["id"]:
                raise DomainError("explicit_replacement_required", "重录必须指明要替换的当前回答")
            if replace_answer_id and not current:
                raise DomainError("replacement_conflict", "被替换的当前回答不存在")
            count = db.execute("SELECT COUNT(*) FROM answers WHERE round_id=?", (round_id,)).fetchone()[0]
            if count >= json.loads(session["config"])["max_attempts_per_round"]:
                raise DomainError("attempt_limit", "同题最多提交5个回答版本")
            if current:
                db.execute("UPDATE answers SET current=0 WHERE id=?", (current["id"],))
                db.execute("UPDATE jobs SET status='superseded',error='answer explicitly replaced',ready_at=? WHERE answer_id=? AND status='queued'", (utc(), current['id']))
            answer_id = uid()
            db.execute("INSERT INTO answers VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (answer_id, session_id, round_id, attempt_id, str(path) if path else None, media_hash, media_meta.get("mime_type"), media_meta.get("duration_ms"), language, utc(), 1, 1))
            db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?)", (answer_id, 1, transcript.strip(), "正式提交" if not current else f"显式替换 {current['id']}", "candidate", utc()))
            if path:
                db.execute("INSERT INTO jobs(id,session_id,answer_id,status,attempts,input_revision,created_at,job_type) VALUES(?,?,?,?,?,?,?,?)", (uid(), session_id, answer_id, "queued", 0, 1, utc(), "media"))
            if transcript.strip():
                self._enqueue_content(db, session_id, answer_id)
            self._bump(db, session_id)
            answer = self._answer(db, db.execute("SELECT * FROM answers WHERE id=?", (answer_id,)).fetchone())
            result = {**answer, "state": self._state(db, session_id)}
            return self._remember(db, f"answer:{session_id}", idempotency_key, payload, result)

    def revise_transcript(self, session_id, answer_id, version, text, reason, idempotency_key, actor="candidate"):
        if not reason.strip() or not text.strip() or len(text) > 30000:
            raise DomainError("invalid_revision", "修订需要文字和理由", 422)
        payload = {"answer_id": answer_id, "version": version, "text": text, "reason": reason, "actor": actor}
        with self._db(True) as db:
            prior = self._idempotent(db, f"revision:{session_id}", idempotency_key, payload)
            if prior:
                return prior
            row = db.execute("SELECT * FROM answers WHERE id=? AND session_id=?", (answer_id, session_id)).fetchone()
            if not row:
                raise DomainError("not_found", "回答不存在", 404)
            if row["revision"] != version or not row["current"]:
                raise DomainError("version_conflict", "回答已被修改，请刷新后核对")
            db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?)", (answer_id, version + 1, text.strip(), reason, actor, utc()))
            db.execute("UPDATE answers SET revision=revision+1 WHERE id=?", (answer_id,))
            self._enqueue_content(db, session_id, answer_id)
            self._bump(db, session_id)
            result = {**self._answer(db, db.execute("SELECT * FROM answers WHERE id=?", (answer_id,)).fetchone()), "state": self._state(db, session_id)}
            return self._remember(db, f"revision:{session_id}", idempotency_key, payload, result)

    def _validate_evidence(self, db, session_id, evidence, question_id=None):
        validated = []
        for e in evidence:
            answer = db.execute("SELECT a.*,r.question_id,r.parent_question_id FROM answers a JOIN rounds r ON a.round_id=r.id WHERE a.id=? AND a.session_id=?", (e.get("answer_id"), session_id)).fetchone()
            if not answer or not answer["current"]:
                raise DomainError("invalid_evidence", "引用必须属于此会话的当前正式回答", 422)
            if question_id and question_id not in (answer["question_id"], answer["parent_question_id"]):
                raise DomainError("wrong_question_evidence", "引用须来自本题或其追问", 422)
            if e.get("transcript_revision") != answer["revision"]:
                raise DomainError("stale_evidence", "引用的转写版本已过期")
            text = db.execute("SELECT text FROM revisions WHERE answer_id=? AND version=?", (answer["id"], answer["revision"])).fetchone()[0]
            quote = e.get("quote", "")
            if not isinstance(quote, str) or not quote.strip() or quote not in text:
                raise DomainError("invalid_quote", "引用必须是当前转写中的原话", 422)
            position = {}
            if any(e.get(k) is not None for k in ('segment_id', 'char_start', 'char_end')):
                start, end = e.get('char_start'), e.get('char_end')
                segments = self._answer_segments(db, answer, answer['revision'], text)
                segment = next((s for s in segments if s['segment_id'] == e.get('segment_id')), None)
                if (type(start) is not int or type(end) is not int or not segment
                        or not segment['char_start'] <= start < end <= segment['char_end'] or text[start:end] != quote):
                    raise DomainError('invalid_evidence_position', '引用位置必须精确对应此版本句段中的原话', 422)
                position = {'segment_id': segment['segment_id'], 'char_start': start, 'char_end': end}
            suffix = f":{position['char_start']}:{position['char_end']}" if position else ''
            validated.append({"answer_id": answer["id"], "quote": quote, "transcript_revision": answer["revision"],
                              "evidence_id": f"{answer['id']}:t{answer['revision']}:{digest(quote)[:12]}{suffix}", **position})
        return validated

    def assess_followup(self, session_id, version, status, branch_id, reason, evidence, idempotency_key, actor="hr", model_run_id=None):
        if actor not in ("hr", "content_provider"):
            raise DomainError("invalid_actor", "内容判断来源无效", 422)
        original = {"version": version, "status": status, "branch_id": branch_id, "reason": reason, "evidence": evidence, "actor": actor, "model_run_id": model_run_id}
        payload = dict(original)
        with self._db(True) as db:
            prior = self._idempotent(db, f"assessment:{session_id}", idempotency_key, payload)
            if prior:
                return prior
            session = self._session(db, session_id)
            if session["version"] != version:
                raise DomainError("version_conflict", "会话已更新，请刷新")
            if db.execute("SELECT 1 FROM decisions WHERE session_id=?", (session_id,)).fetchone():
                raise DomainError("decision_sealed", "追问决策已封存，不可事后改写")
            if status not in ("gap", "sufficient", "no_legal_branch") or not reason.strip() or not evidence:
                raise DomainError("invalid_assessment", "内容判断需要合法状态、理由和原话证据", 422)
            config = json.loads(session["config"])
            branch = next((b for b in config["branches"] if b["branch_id"] == branch_id), None)
            if status == "gap" and not branch:
                raise DomainError("invalid_branch", "只能选择短卷允许的父题原有分支", 422)
            payload["evidence"] = self._validate_evidence(db, session_id, evidence, branch["parent_question_id"] if branch else None)
            first_answer = db.execute("SELECT r.question_id,r.parent_question_id FROM answers a JOIN rounds r ON r.id=a.round_id WHERE a.id=?", (payload["evidence"][0]["answer_id"],)).fetchone()
            payload.update({"assessment_id": uid(), "created_at": utc(), "question_id": branch["parent_question_id"] if branch else first_answer["parent_question_id"] or first_answer["question_id"]})
            payload['input_versions'] = {a['answer_id']: a['transcript_revision'] for a in self._content_inputs(db, session_id, payload['question_id'])}
            if config.get('followup_timing') == 'after_each_main' and db.execute(
                    "SELECT 1 FROM decision_events e JOIN rounds r ON r.id=e.round_id WHERE e.session_id=? AND r.question_id=? AND r.kind='main'",
                    (session_id, payload['question_id'])).fetchone():
                raise DomainError('decision_sealed', '本题追问决策已封存，不可事后改写')
            db.execute("INSERT INTO assessments VALUES(?,?,?,?)", (payload["assessment_id"], session_id, dumps(payload), payload["created_at"]))
            self._bump(db, session_id)
            result = {"assessment": payload, "state": self._state(db, session_id, True)}
            return self._remember(db, f"assessment:{session_id}", idempotency_key, original, result)

    def next_round(self, session_id, idempotency_key, round_id=None):
        payload = {"round_id": round_id}
        with self._db(True) as db:
            prior = self._idempotent(db, f"next:{session_id}", idempotency_key, payload)
            if prior:
                return prior
            session = self._session(db, session_id)
            state = self._state(db, session_id)
            current = state["current_round"]
            if current is None:
                return self._remember(db, f"next:{session_id}", idempotency_key, payload, state)
            if round_id and current["round_id"] != round_id:
                raise DomainError("round_conflict", "当前问题已变化，请刷新")
            if not any(a["round_id"] == current["round_id"] and a["current"] for a in state["answers"]):
                raise DomainError("answer_required", "请先正式提交当前题的回答")
            config = json.loads(session["config"])
            if config.get('followup_timing') == 'after_each_main':
                return self._next_round_v2(db, session_id, idempotency_key, payload, session, config, state, current)
            if current["ordinal"] < 3:
                self._issue(db, session_id, current["ordinal"] + 1, config["questions"][current["ordinal"]])
            elif current["ordinal"] == 3:
                pending = db.execute("SELECT 1 FROM jobs WHERE session_id=? AND status IN ('queued','running') LIMIT 1", (session_id,)).fetchone()
                if pending and credential_available() and config["decision_wait_seconds"] > 0:
                    started = session.get("decision_started")
                    if started is None:
                        started = time.time()
                        db.execute("UPDATE sessions SET decision_started=? WHERE id=?", (started, session_id))
                        self._bump(db, session_id)
                    if time.time() - started < config["decision_wait_seconds"]:
                        # This is not a completed advance: the same idempotency key may poll.
                        return self._state(db, session_id)
                self._decide(db, session_id, config, state)
            else:
                db.execute("UPDATE sessions SET status='interview_finished' WHERE id=?", (session_id,))
            self._bump(db, session_id)
            return self._remember(db, f"next:{session_id}", idempotency_key, payload, self._state(db, session_id))

    def _next_round_v2(self, db, session_id, key, payload, session, config, state, current):
        """Decide once after this answer, using only its parent question's content."""
        question_id = current['parent_question_id'] or current['question_id']
        question = next(q for q in config['questions'] if q['question_id'] == question_id)
        inputs = self._content_inputs(db, session_id, question_id)
        versions = {a['answer_id']: a['transcript_revision'] for a in inputs}
        allowed = [b for b in config['branches'] if b['parent_question_id'] == question_id]
        assessment = None
        hybrid = config.get('policy_version') == 'semantic-bank.v1'
        quick = config.get('policy_version') in ('dictionary-bank.v1','semantic-bank.v1')
        semantic_result = None
        semantic_fallback = None
        semantic_job_started = None
        semantic_deadline = None
        routing = None
        if quick and current['kind'] == 'main' and any(a['transcript'].strip() for a in inputs):
            from interview_followup import select_followup
            if hybrid:
                from interview_routing import lexical_check
                routing = lexical_check(inputs, question, allowed)
            else:
                routing = select_followup(inputs, question, allowed)
            assessment = {'question_id': question_id, 'input_versions': versions,
                          'branch_id': routing['branch_id'], 'reason': routing['reason'],
                          'status': 'gap' if routing['branch_id'] else 'no_legal_branch' if routing.get('coverage_incomplete') else 'sufficient',
                          'evidence': self._validate_evidence(db, session_id, routing['evidence'], question_id),
                          'actor': 'dictionary_policy'}
        elif not quick and current['kind'] == 'main':
            for row in db.execute('SELECT payload FROM assessments WHERE session_id=? ORDER BY created_at DESC', (session_id,)):
                candidate = json.loads(row[0])
                if candidate.get('question_id') != question_id or candidate.get('input_versions') != versions:
                    continue
                try:
                    self._validate_evidence(db, session_id, candidate['evidence'], question_id)
                except DomainError:
                    continue
                assessment = candidate
                break
        if hybrid and assessment and current['kind']=='main' and allowed and state['budget']['followup']<config['max_followup']:
            job = db.execute("SELECT * FROM jobs WHERE session_id=? AND question_id=? AND job_type='routing' AND input_versions=? ORDER BY created_at DESC LIMIT 1", (session_id,question_id,dumps(versions))).fetchone()
            if job:
                semantic_job_started=datetime.fromisoformat(job['created_at']).timestamp()
                semantic_deadline=semantic_job_started+config.get('semantic_wait_seconds',6)
            if job and job['status']=='succeeded' and job['ready_at'] and datetime.fromisoformat(job['ready_at']).timestamp() <= semantic_deadline:
                saved=json.loads(job['result']);semantic_result=saved
                checked=saved['routing'];chosen=next((b for b in checked['branches'] if b['branch_id']==checked['selected_branch_id']),None)
                refs=chosen['evidence'] if chosen else [ref for b in checked['branches'] for ref in b['evidence']][:8]
                assessment={'question_id':question_id,'input_versions':versions,'branch_id':checked['selected_branch_id'],
                    'reason':checked['reason'],'status':'gap' if chosen else 'no_legal_branch' if any(b['status'] in ('gap','uncertain') for b in checked['branches']) else 'sufficient',
                    'evidence':self._validate_evidence(db,session_id,refs,question_id),'actor':'semantic_routing','model_run_id':job['id']}
            elif job and job['status'] in ('queued','running') and credential_available():
                created=datetime.fromisoformat(job['created_at']).timestamp()
                limit=config.get('semantic_wait_seconds',6)
                if time.time()<created+limit:
                    if not session.get('decision_started'):
                        db.execute('UPDATE sessions SET decision_started=? WHERE id=?',(created,session_id));self._bump(db,session_id)
                    waiting=self._state(db,session_id);waiting['decision_wait_remaining']=max(0,created+limit-time.time());waiting['decision_stage']='semantic_check'
                    return waiting
                semantic_fallback='semantic_deadline'
            else:
                semantic_fallback=('semantic_deadline' if job['status']=='succeeded' else job['error_code'] or job['status']) if job else 'semantic_not_available'
        started = session.get('decision_started')
        # Only a possible follow-up needs synchronous content evidence. Scoring
        # continues in the background when the branch or budget cannot change
        # the next question. Other questions' jobs never delay this checkpoint.
        pending = False
        can_ask_followup = bool(allowed) and state['budget']['followup'] < config['max_followup']
        if current['kind'] == 'main' and can_ask_followup and assessment is None:
            pending = not quick and any(json.loads(r['input_versions']) == versions for r in db.execute(
                "SELECT input_versions FROM jobs WHERE session_id=? AND question_id=? AND job_type='content' AND status IN ('queued','running')", (session_id, question_id)))
            if not pending:
                pending = bool(db.execute("SELECT 1 FROM jobs j JOIN answers a ON a.id=j.answer_id WHERE j.session_id=? AND a.round_id=? AND a.current=1 AND j.job_type='media' AND j.status IN ('queued','running')", (session_id, current['round_id'])).fetchone())
            if pending and (quick or credential_available()) and config['decision_wait_seconds'] > 0:
                if started is None:
                    started = time.time()
                    db.execute('UPDATE sessions SET decision_started=? WHERE id=?', (started, session_id))
                    self._bump(db, session_id)
                if time.time() < started + config['decision_wait_seconds']:
                    return self._state(db, session_id)
        cutoff = utc()
        selected = None
        stop_reason = None
        if current['kind'] == 'followup':
            reason = '本次追问回答已保存，并入父题内容评分；按固定顺序继续剩余主问。'
            stop_reason = 'followup_completed'
        elif not assessment and state['budget']['followup'] >= config['max_followup']:
            stop_reason = 'followup_budget_exhausted'
            reason = '本场追问预算已使用；回答已保存，内容评分在后台继续，直接进入后续流程。'
        elif not assessment and not allowed:
            stop_reason = 'no_legal_branch'
            reason = '本题未配置可用追问分支；回答已保存，内容评分在后台继续，直接进入后续流程。'
        elif not assessment:
            stop_reason = ('transcription_timeout' if started and pending else 'transcription_unavailable') if quick else ('content_evaluation_timeout' if started and pending else 'content_evaluation_unavailable')
            reason = ('本题文字转写尚不可用；未猜测回答内容，先继续流程，后台仍处理原始录音，老师端保留待核实项。' if quick else '截至本题决策时限，尚无可核验的内容判断；继续流程并保留待复核项。')
        else:
            reason = assessment['reason']
            if assessment['status'] == 'gap':
                candidate = next((b for b in allowed if b['branch_id'] == assessment.get('branch_id')), None)
                if state['budget']['followup'] >= config['max_followup']:
                    stop_reason = 'followup_budget_exhausted'
                    reason += '；本场唯一追问预算已使用，保留缺口供HR核对。'
                elif candidate:
                    selected = candidate
                else:
                    stop_reason = 'no_legal_branch'
            else:
                stop_reason = 'content_sufficient' if assessment['status'] == 'sufficient' else 'no_legal_branch'
        next_question = None
        if selected:
            action = 'ask_followup'
            self._issue(db, session_id, len(state['rounds']) + 1, selected, 'followup')
        else:
            issued = {r['question_id'] for r in state['rounds'] if r['kind'] == 'main'}
            next_question = next((q for q in config['questions'] if q['question_id'] not in issued), None)
            action = 'continue_main' if next_question else 'finish'
            if next_question:
                self._issue(db, session_id, len(state['rounds']) + 1, next_question)
            else:
                db.execute("UPDATE sessions SET status='interview_finished' WHERE id=?", (session_id,))
        db.execute('UPDATE sessions SET decision_started=NULL WHERE id=?', (session_id,))
        after = self._state(db, session_id)
        evidence = assessment['evidence'] if assessment else []
        observed = []
        answer_ids = {a['answer_id'] for a in inputs}
        for row in db.execute("SELECT id,answer_id,result,ready_at FROM jobs WHERE session_id=? AND job_type='media' AND status='succeeded' AND ready_at<=?", (session_id, cutoff)):
            if row['answer_id'] not in answer_ids:
                continue
            result = json.loads(row['result'])
            audio = result.get('audio', {})
            windows = audio.get('windows') if isinstance(audio, dict) else None
            valid_windows = isinstance(windows, list) and bool(windows) and all(
                isinstance(w, dict) and all(type(w.get(k)) in (int, float) and math.isfinite(w[k]) for k in ('start_ms', 'end_ms', 'rms_dbfs'))
                and 0 <= w['start_ms'] < w['end_ms'] for w in windows)
            if isinstance(audio, dict) and audio.get('status') == 'ok' and valid_windows:
                observed.append({'evidence_id': f"{row['id']}:audio", 'answer_id': row['answer_id'],
                                 'ready_at': row['ready_at'], 'audio': audio, 'usage': 'observed_only'})
        event = {'decision_id': uid(), 'session_id': session_id, 'round_id': current['round_id'],
                 'checkpoint_kind': current['kind'], 'question_id': question_id, 'question_text': current['text'],
                 'target_factor_id': question['factor_id'], 'parent_question_id': question_id,
                 'decision_time': cutoff, 'evidence_cutoff_time': cutoff,
                 'wait_started_at': datetime.fromtimestamp(started, timezone.utc).isoformat() if started else None,
                 'decision_deadline': datetime.fromtimestamp(started + config['decision_wait_seconds'], timezone.utc).isoformat() if started else cutoff,
                 'action': action, 'selected_branch_id': selected['branch_id'] if selected else None,
                 'target_evidence': selected['target_evidence'] if selected else None,
                 'allowed_branch_ids': [b['branch_id'] for b in allowed],
                 'candidate_branch_ids': [assessment['branch_id']] if assessment and assessment.get('branch_id') else [],
                 'actual_next_round': after['current_round'], 'next_question_id': after['current_round']['question_id'] if after['current_round'] else None,
                 'rule_id': 'per_answer_content.v2', 'policy_version': config['policy_version'],
                 'used_evidence': evidence, 'used_evidence_ids': [e['evidence_id'] for e in evidence],
                 'input_versions': versions, 'model_run_ids': [assessment['model_run_id']] if assessment and assessment.get('model_run_id') else [],
                 'model_id': config.get('model_config', {}).get('model'), 'model_config': config.get('model_config', {}),
                 'available_evidence_snapshot': [{'answer_id': a['answer_id'], 'transcript_revision': a['transcript_revision'], 'transcript': a['transcript'], 'job_status': a['job_status']} for a in inputs],
                 'available_modalities': (['text'] if any(a['transcript'] for a in inputs) else []) + (['audio'] if observed else []),
                 'missing_modalities': ([] if assessment and assessment.get('actor') == 'content_provider' else ['automatic_content_analysis']) + ['validated_visual_observations'],
                 'observed_only': observed, 'observed_only_ids': [o['evidence_id'] for o in observed],
                 'visual_observation_status': 'not_enabled', 'budget_before': state['budget'], 'budget_after': after['budget'],
                 'reason_for_hr': reason, 'stop_reason': stop_reason, 'decision_mode': 'validated_content_only'}
        if quick:
            if current['kind'] == 'main' and state['budget']['followup'] >= config['max_followup']:
                reason = '本场追问预算已使用，未继续提问；本题回答已保存，后台内容分析继续，待核实线索保留在报告中。'
                event.update(reason_for_hr=reason, stop_reason='followup_budget_exhausted')
            elif current['kind'] == 'main' and not allowed:
                event['stop_reason'] = 'no_legal_branch'
            elif routing and routing.get('stop_reason'):
                event['stop_reason'] = routing['stop_reason']
            event.update(rule_id='dictionary-bank.v1', decision_mode='dictionary_bank_fast_path',
                         model_id=None, model_run_ids=[], decision_source='dictionary-bank.v1')
            event['decision_summary'] = routing['summary'] if routing else {
                'schema_version': 'followup-decision-summary.v1', 'decision_source': 'dictionary-bank.v1',
                'model_involved_in_selection': False, 'model_role': 'asynchronous_scoring_and_comparison_only'}
            event['decision_summary'].update(brief_reason=reason, used_evidence=evidence,
                                            selected_branch=selected, target_evidence=event['target_evidence'])
        if hybrid:
            involved=semantic_result is not None
            source='semantic-bank.v1' if involved else 'dictionary-bank.v2-fallback'
            if current['kind']!='main' or not allowed or state['budget']['followup']>=config['max_followup']:
                source='workflow-budget';semantic_fallback='not_needed'
            event.update(rule_id=source,decision_source=source,decision_mode='semantic_dictionary_check' if involved else 'dictionary_fallback',
                model_id=(semantic_result or {}).get('provider',{}).get('model'),
                model_run_ids=[assessment['model_run_id']] if involved else [],
                model_config={**config.get('model_config',{}),'model':semantic_result['provider']['model']} if involved else {})
            if semantic_deadline is not None:
                event['wait_started_at']=datetime.fromtimestamp(semantic_job_started,timezone.utc).isoformat()
                event['decision_deadline']=datetime.fromtimestamp(semantic_deadline,timezone.utc).isoformat()
            summary=event['decision_summary']
            summary.update(decision_source=source,model_involved_in_selection=involved,
                model_role='bounded_semantic_selection' if involved else 'background_scoring_only',
                semantic_check=(semantic_result or {}).get('routing'),fallback_reason=semantic_fallback,
                dictionary_coverage=(routing or {}).get('coverage',[]),brief_reason=event['reason_for_hr'])
            if semantic_fallback and semantic_fallback!='not_needed':
                event['reason_for_hr']+=' 本次语义核对未在现场时限内取得有效结果，采用已记录的本地规则；后续模型意见另列。'
                summary['brief_reason']=event['reason_for_hr']
        db.execute('INSERT INTO decision_events VALUES(?,?,?,?)', (event['decision_id'], session_id, current['round_id'], dumps(event)))
        self._bump(db, session_id)
        return self._remember(db, f'next:{session_id}', key, payload, self._state(db, session_id))

    def _decide(self, db, session_id, config, state):
        cutoff = utc()
        assessments = [json.loads(r[0]) for r in db.execute("SELECT payload FROM assessments WHERE session_id=? ORDER BY created_at DESC", (session_id,))]
        valid = []
        for a in assessments:
            try:
                self._validate_evidence(db, session_id, a["evidence"])
                valid.append(a)
            except DomainError:
                continue
        # Latest valid assessment per parent question; Q3 sufficiency cannot erase Q1 gaps.
        by_question = {}
        for assessment in valid:
            by_question.setdefault(assessment.get("question_id"), assessment)
        valid = list(by_question.values())
        latest_stop = next((a for a in valid if a["status"] == "no_legal_branch"), None)
        candidates = []
        for branch in config["branches"]:
            assessment = next((a for a in valid if a["status"] == "gap" and a["branch_id"] == branch["branch_id"]), None)
            if assessment:
                candidates.append((branch, assessment))
        candidates.sort(key=lambda item: (item[0]["priority"], item[0]["branch_id"]))
        selected = candidates[0] if candidates else None
        answers = [a for a in state["answers"] if a["current"]]
        all_sufficient = len(valid) == 3 and all(a["status"] == "sufficient" for a in valid)
        stop_reason = None if selected else "content_sufficient" if all_sufficient else "no_legal_branch" if latest_stop else "content_evaluation_unavailable"
        evidence = selected[1]["evidence"] if selected else [e for assessment in valid for e in assessment["evidence"]]
        rationale = selected[1]["reason"] if selected else "三道主问当前内容判断均充分，按政策结束。" if all_sufficient else latest_stop["reason"] if latest_stop else "截至决策时点，无完整可核验的内容判断；结束提问并保留待复核项。"
        observed = []
        for row in db.execute("SELECT j.id,j.answer_id,j.result,j.ready_at FROM jobs j JOIN answers a ON j.answer_id=a.id WHERE j.session_id=? AND j.job_type='media' AND j.status='succeeded' AND j.ready_at<=? AND a.current=1", (session_id, cutoff)):
            result = json.loads(row["result"])
            if result.get("audio"):
                observed.append({"evidence_id": f"{row['id']}:audio", "answer_id": row["answer_id"], "ready_at": row["ready_at"], "audio": result["audio"], "usage": "observed_only"})
        decision = {"decision_id": uid(), "session_id": session_id, "decision_time": cutoff, "evidence_cutoff_time": cutoff,
                    "action": "ask_followup" if selected else "finish", "candidate_branch_ids": [b["branch_id"] for b, _ in candidates],
                    "selected_branch_id": selected[0]["branch_id"] if selected else None, "parent_question_id": selected[0]["parent_question_id"] if selected else None,
                    "rule_id": "validated_content_priority.v1", "policy_version": config["policy_version"], "available_modalities": (["text"] if any(a["transcript"] for a in answers) else []) + (["audio"] if observed else []),
                    "used_evidence_ids": [e["evidence_id"] for e in evidence], "used_evidence": evidence, "observed_only_ids": [o["evidence_id"] for o in observed], "observed_only": observed,
                    "missing_modalities": (["automatic_content_analysis"] if not any(a.get("actor") == "content_provider" for a in valid) else []) + ["validated_visual_observations"], "budget_before": state["budget"],
                    "budget_after": {**state["budget"], "followup": int(bool(selected)), "total": 4 if selected else 3},
                    "input_versions": {a["answer_id"]: a["transcript_revision"] for a in answers}, "model_run_ids": [a["model_run_id"] for a in valid if a.get("model_run_id")],
                    "available_evidence_snapshot": [{"answer_id": a["answer_id"], "transcript_revision": a["transcript_revision"], "transcript": a["transcript"], "job_status": a["job_status"]} for a in answers],
                    "reason_for_hr": rationale,
                    "stop_reason": stop_reason, "decision_mode": "validated_content_only"}
        db.execute("INSERT INTO decisions VALUES(?,?,?)", (decision["decision_id"], session_id, dumps(decision)))
        if selected:
            self._issue(db, session_id, 4, selected[0], "followup")
        else:
            db.execute("UPDATE sessions SET status='interview_finished' WHERE id=?", (session_id,))

    def submit_review(self, session_id, version, reason, ratings, idempotency_key, reviewer="local_hr"):
        payload = {"version": version, "reason": reason, "ratings": ratings, "reviewer": reviewer}
        with self._db(True) as db:
            prior = self._idempotent(db, f"review:{session_id}", idempotency_key, payload)
            if prior:
                return prior
            session = self._session(db, session_id)
            if session["version"] != version:
                raise DomainError("version_conflict", "报告已更新，请刷新后复核")
            if not reason.strip():
                raise DomainError("reason_required", "复核必须保留理由", 422)
            config = json.loads(session["config"])
            ids = {q["question_id"] for q in config["questions"]}
            supplied = [r.get('question_id') for r in ratings]
            if not 1 <= len(ratings) <= len(ids) or len(set(supplied)) != len(supplied) or not set(supplied) <= ids:
                raise DomainError("invalid_ratings", "复核题目须属于本场且不能重复；追问并入父题", 422)
            clean = []
            for r in ratings:
                score = r.get("score")
                if score is not None and (type(score) is not int or score not in range(1, 6)):
                    raise DomainError("invalid_score", "分数只能是1至5的整数或N/A", 422)
                if not r.get("reason", "").strip():
                    raise DomainError("reason_required", "每题须说明评分或N/A理由", 422)
                evidence = self._validate_evidence(db, session_id, r.get("evidence", []), r["question_id"])
                if score is not None and not evidence:
                    raise DomainError("evidence_required", "有效分数必须引用该题回答原话", 422)
                input_versions = {a["answer_id"]: a["transcript_revision"] for a in self._content_inputs(db, session_id, r["question_id"])}
                original_ai = next((json.loads(row[0]) for row in db.execute("SELECT payload FROM model_evaluations WHERE session_id=? AND question_id=? ORDER BY created_at DESC", (session_id, r["question_id"])) if json.loads(row[0])["input_versions"] == input_versions), None)
                clean.append({"question_id": r["question_id"], "score": score, "reason": r["reason"], "evidence": evidence,
                              'input_versions': input_versions, "ai_score": original_ai["rating"]["score"] if original_ai else None,
                              "ai_evaluation_id": original_ai["evaluation_id"] if original_ai else None})
            inputs = {a["id"]: a["revision"] for a in db.execute("SELECT id,revision FROM answers WHERE session_id=? AND current=1", (session_id,))}
            review = {"review_id": uid(), "parent_version": version, "reason": reason, "ratings": clean, "reviewer": reviewer, "created_at": utc(), "ai_score_status": "draft_results_available" if any(r["ai_evaluation_id"] for r in clean) else "not_available", "input_versions": inputs}
            db.execute("INSERT INTO reviews VALUES(?,?,?,?,?)", (review["review_id"], session_id, version, dumps(review), review["created_at"]))
            self._bump(db, session_id)
            result = self._report(db, session_id)
            return self._remember(db, f"review:{session_id}", idempotency_key, payload, result)

    def _report(self, db, session_id):
        session = self._session(db, session_id)
        config = json.loads(session["config"])
        cached = db.execute("SELECT payload FROM reports WHERE session_id=? AND version=?", (session_id, session["version"])).fetchone()
        if cached:
            restored = json.loads(cached[0])
            restored.setdefault('created_at', session['created_at'])
            return restored
        state = self._state(db, session_id, True)
        reviews = [json.loads(r[0]) for r in db.execute("SELECT payload FROM reviews WHERE session_id=? ORDER BY parent_version,rowid", (session_id,))]
        evaluations = [json.loads(r[0]) for r in db.execute("SELECT payload FROM model_evaluations WHERE session_id=? ORDER BY created_at", (session_id,))]
        last = reviews[-1] if reviews else None
        questions = []
        for q in config["questions"]:
            # Reviews are append-only patches. A later review of Q2 cannot erase Q1.
            question_review = next((review for review in reversed(reviews)
                                    if any(r['question_id'] == q['question_id'] for r in review['ratings'])), None)
            rating = next((r for r in question_review['ratings'] if r['question_id'] == q['question_id']), None) if question_review else None
            stale = False
            if rating:
                try:
                    self._validate_evidence(db, session_id, rating["evidence"], q["question_id"])
                except DomainError:
                    stale = True
            answers = [a for a in state["answers"] if q["question_id"] in (a["question_id"], a["parent_question_id"]) and a["current"]]
            actual_inputs = {a["answer_id"]: a["transcript_revision"] for a in answers}
            if rating:
                expected_inputs = rating.get('input_versions')
                if expected_inputs is None:
                    # Historical full-form reviews recorded all answers together.
                    relevant_ids = {a['answer_id'] for a in state['answers'] if q['question_id'] in (a['question_id'], a['parent_question_id'])}
                    expected_inputs = {aid: rev for aid, rev in question_review.get('input_versions', {}).items() if aid in relevant_ids}
                stale |= expected_inputs != actual_inputs
            model_eval = next((e for e in reversed(evaluations) if e["question_id"] == q["question_id"] and e["input_versions"] == actual_inputs), None)
            effective = rating if rating else model_eval["rating"] if model_eval else None
            questions.append({**q, "rubric_status": "proposed", "score": effective["score"] if effective and not stale else None, "ai_score": model_eval["rating"]["score"] if model_eval else None,
                              "reason": effective["reason"] if effective else "尚无可核验的内容模型评分或HR评分", "evidence": effective["evidence"] if effective else [],
                              "score_source": "hr" if rating else "model_draft" if model_eval else "unavailable", "model_evaluation": model_eval,
                              "review_stale": stale, 'review_status': 'stale' if stale else 'reviewed' if rating and answers else 'not_reviewed',
                              'review_id': question_review['review_id'] if question_review else None,
                              'reviewer': question_review['reviewer'] if question_review else None,
                              'reviewed_at': question_review['created_at'] if question_review else None, "answers": answers})
        scores = [25 * (q["score"] - 1) for q in questions if q["score"] is not None]
        full = len(scores) == len(questions) and session["status"] == "interview_finished"
        final = sum(scores) / len(questions) if full else None
        planned = [q["factor_id"] for q in questions]
        scored = [q["factor_id"] for q in questions if q["score"] is not None]
        pending = any(j["status"] in ("queued", "running") for j in state["jobs"])
        decision_events = [json.loads(r[0]) for r in db.execute("SELECT e.payload FROM decision_events e JOIN rounds r ON r.id=e.round_id WHERE e.session_id=? ORDER BY r.ordinal", (session_id,))]
        decisions = decision_events or [json.loads(r[0]) for r in db.execute("SELECT payload FROM decisions WHERE session_id=?", (session_id,))]
        cutoff = decisions[0]["evidence_cutoff_time"] if decisions else None
        for a in state["answers"]:
            answer_cutoff = next((e['evidence_cutoff_time'] for e in decision_events if e['round_id'] == a['round_id']), None) if decision_events else cutoff
            a["analysis_after_decision"] = bool(answer_cutoff and a["analysis_ready_at"] and a["analysis_ready_at"] > answer_cutoff)
        report = {**state, "questions": questions, "reviews": reviews, "decisions": decisions, "decision_events": decision_events, "model_evaluations": evaluations, "model_config": config.get("model_config"),
                  "assessments": [json.loads(r[0]) for r in db.execute("SELECT payload FROM assessments WHERE session_id=? ORDER BY created_at", (session_id,))],
                  "total_score": final, "final_score": final, "total_status": "complete" if full else "N/A", "score_status": "complete" if full else "incomplete",
                  "provisional_scored_mean": sum(scores) / len(scores) if scores and not full else None,
                  "score_name": "本次面试答案证据总分", "form": config.get("form"), "planned_main_count": len(questions), "scorable_main_count": len(scores),
                  "coverage": {"planned_factor_ids": planned, "scored_factor_ids": scored, "uncovered_factor_ids": [f for f in config["factor_ids"] if f not in planned]},
                  'reviewed_question_count': sum(q['review_status'] == 'reviewed' for q in questions),
                  "report_lifecycle": "pending" if pending else "reviewed" if all(q['review_status'] == 'reviewed' for q in questions) else "draft",
                  "report_completeness": "incomplete", "automatic_scoring": {"status": "draft_results_available" if evaluations else "awaiting_results" if credential_available() else "not_configured", "reason": "模型只依据答案内容形成草稿；HR复核优先；不使用音视频心理推断。"},
                  "analysis_completeness_by_modality": {"asr": "pending" if pending else "see_answer_results", "audio": "pending" if pending else "see_answer_results", "au_net": "not_enabled", "expression": "not_enabled", "gaze360": "not_enabled", "emotic": "not_enabled"},
                  "generated_at": utc()}
        from interview_profile import report_extensions
        report.update(report_extensions(db, session_id, report))
        from interview_conclusion import potential_conclusion
        report["potential_conclusion"] = potential_conclusion(report)
        from interview_research import standard_snapshot
        report['standard_analysis'] = standard_snapshot(db, session_id, config)
        standard = report['standard_analysis']
        from interview_observations import summarize_multimodal
        for run in standard['runs']:
            for output in (run.get('result') or {}).get('multimodal', []):
                output['reader_summary'] = summarize_multimodal(output, source_run_id=run['run_id'])
        if standard['enabled']:
            if standard['pending_count']:
                report['report_lifecycle'] = 'pending'
            for module in (standard['plan'] or {}).get('modules', []):
                states = [output['modules'][module].get('status')
                          for run in standard['runs'] for output in (run.get('result') or {}).get('multimodal', [])
                          if module in output.get('modules', {})]
                module_pending = any(p.get('status') in ('pending', 'queued', 'running', 'waiting_worker') and module in p.get('modules', [])
                                     for a in standard['answers'] for p in a.get('phases', []))
                if not any('phases' in a for a in standard['answers']):
                    module_pending = bool(standard['pending_count'])
                report['analysis_completeness_by_modality'][module] = ('pending' if module_pending and not states
                    else 'complete' if states and all(s in ('complete', 'succeeded', 'success', 'ok') for s in states)
                    else 'partial' if any(s in ('complete', 'succeeded', 'success', 'ok', 'partial') for s in states)
                    else 'unavailable')
                if module_pending and states:
                    report['analysis_completeness_by_modality'][module] = 'partial'
                if any(p.get('status') == 'paused' and module in p.get('modules', []) for a in standard['answers'] for p in a.get('phases', [])):
                    report['analysis_completeness_by_modality'][module] = 'paused'
        report['analysis_completeness_by_modality']['asr'] = ('pending' if any(
            j['job_type'] == 'media' and j['status'] in ('queued', 'running') for j in state['jobs']) else 'see_answer_results')
        for event in report['decision_events']:
            if event.get('decision_source') in ('dictionary-bank.v1','semantic-bank.v1','dictionary-bank.v2-fallback','workflow-budget'):
                relevant = [e for e in evaluations if e['question_id'] == event['question_id']]
                event['model_comparison'] = [{'evaluation_id': e['evaluation_id'],
                    'created_at': e['created_at'], 'input_versions': e['input_versions'],
                    'same_input_as_decision': e['input_versions'] == event['input_versions'],
                    'role': 'later_comparison_not_decision_input', 'followup': e['followup'],
                    'provider': e.get('provider', {})} for e in relevant]
        report['reader_summary'] = {
            'schema_version': 'report-reader.v1',
            'answered_count': sum(a['current'] for a in state['answers']),
            'interview_finished': state['status'] == 'interview_finished',
            'content_pending_count': len({j['question_id'] for j in state['jobs'] if j['job_type'] == 'content' and j['status'] in ('queued', 'running')}),
            'multimodal_pending_count': standard['pending_count'],
            'multimodal_failed_count': standard['failed_count'],
            'scorable_main_count': len(scores), 'planned_main_count': len(questions),
            'updated_at': report['generated_at'],
            'accuracy_validation': 'awaiting_independent_annotations'}
        report['report_completeness'] = ('complete' if full and not pending and not standard['pending_count'] and not standard['failed_count']
                                         and not any(a['status'] == 'paused' for a in standard['answers']) else 'incomplete')
        db.execute("INSERT OR IGNORE INTO reports VALUES(?,?,?,?)", (session_id, session["version"], dumps(report), report["generated_at"]))
        return report

    def get_report(self, session_id, version=None):
        with self._db(True) as db:
            if version is not None:
                row = db.execute("SELECT payload FROM reports WHERE session_id=? AND version=?", (session_id, version)).fetchone()
                if not row:
                    raise DomainError("not_found", "报告版本不存在", 404)
                restored = json.loads(row[0])
                restored.setdefault('created_at', self._session(db, session_id)['created_at'])
                return restored
            return self._report(db, session_id)

    def _answer_segments(self, db, answer, revision, text):
        """Same timeline projection in the caller's transaction; no nested DB read."""
        from interview_timeline import build_timeline
        raw = dict(answer)
        if 'question_id' not in raw:
            raw.update(dict(db.execute('SELECT question_id,parent_question_id FROM rounds WHERE id=?', (raw['round_id'],)).fetchone()))
        revisions = [dict(r) for r in db.execute('SELECT * FROM revisions WHERE answer_id=? ORDER BY version', (raw['id'],))]
        jobs = []
        for row in db.execute("SELECT * FROM jobs WHERE answer_id=? AND job_type='media' ORDER BY created_at,id", (raw['id'],)):
            job = dict(row)
            job['result'] = json.loads(job['result']) if job['result'] else None
            jobs.append(job)
        return build_timeline({'answer': raw, 'revision': {'version': revision, 'text': text},
                               'revisions': revisions, 'jobs': jobs, 'sources': []})['segments']

    def _content_inputs(self, db, session_id, question_id):
        answers = []
        for row in db.execute("SELECT a.* FROM answers a JOIN rounds r ON a.round_id=r.id WHERE a.session_id=? AND a.current=1 AND (r.question_id=? OR r.parent_question_id=?) ORDER BY r.ordinal", (session_id, question_id, question_id)):
            answer = self._answer(db, row, False)
            answer['segments'] = self._answer_segments(db, row, answer['transcript_revision'], answer['transcript'])
            answers.append(answer)
        return answers

    def _enqueue_content(self, db, session_id, answer_id):
        row = db.execute("SELECT a.revision,r.question_id,r.parent_question_id FROM answers a JOIN rounds r ON a.round_id=r.id WHERE a.id=?", (answer_id,)).fetchone()
        question_id = row["parent_question_id"] or row["question_id"]
        answers = self._content_inputs(db, session_id, question_id)
        versions = {a["answer_id"]: a["transcript_revision"] for a in answers}
        key = dumps(versions)
        if db.execute("SELECT 1 FROM jobs WHERE session_id=? AND job_type='content' AND question_id=? AND input_versions=?", (session_id, question_id, key)).fetchone():
            return
        db.execute("INSERT INTO jobs(id,session_id,answer_id,status,attempts,input_revision,created_at,job_type,question_id,input_versions) VALUES(?,?,?,?,?,?,?,?,?,?)", (uid(), session_id, answer_id, "queued", 0, row["revision"], utc(), "content", question_id, key))
        config=json.loads(self._session(db,session_id)['config'])
        if (config.get('policy_version')=='semantic-bank.v1' and not row['parent_question_id']
                and any(b['parent_question_id']==question_id for b in config['branches'])
                and any(a['transcript'].strip() for a in answers)
                and not db.execute("SELECT 1 FROM decision_events WHERE session_id=? AND json_extract(payload,'$.question_id')=?",(session_id,question_id)).fetchone()
                and db.execute("SELECT count(*) FROM rounds WHERE session_id=? AND kind='followup'",(session_id,)).fetchone()[0]<config['max_followup']):
            db.execute("INSERT INTO jobs(id,session_id,answer_id,status,attempts,input_revision,created_at,job_type,question_id,input_versions) VALUES(?,?,?,?,?,?,?,?,?,?)",(uid(),session_id,answer_id,'queued',0,row['revision'],utc(),'routing',question_id,key))


    def _apply_model_evaluation(self, db, session_id, question_id, result, expected_input_versions, provider_metadata):
        session = self._session(db, session_id)
        config = json.loads(session["config"])
        question = next((q for q in config["questions"] if q["question_id"] == question_id), None)
        if not question or result.get("question_id") != question_id:
            raise DomainError("invalid_model_question", "模型输出题目不匹配", 422)
        answers = self._content_inputs(db, session_id, question_id)
        actual = {a["answer_id"]: a["transcript_revision"] for a in answers}
        if actual != expected_input_versions:
            raise DomainError("stale_model_input", "模型输入版本已经变化；拒绝覆盖当前输入")
        if result.get('schema_version') == 'answer-evaluation.v2':
            from interview_llm import validate_evaluation, ProviderError
            try:
                result = validate_evaluation(result, question, answers, config['branches'], require_v2=True)
            except ProviderError as error:
                raise DomainError(error.code, str(error), 422) from None
        rating = result.get("rating", {})
        score = rating.get("score")
        if score is not None and (type(score) is not int or score not in range(1, 6)):
            raise DomainError("invalid_score", "模型分数越界", 422)
        if not isinstance(rating.get("reason"), str) or not rating["reason"].strip():
            raise DomainError("reason_required", "模型评分缺少理由", 422)
        evidence = self._validate_evidence(db, session_id, rating.get("evidence", []), question_id)
        if score is not None and not evidence:
            raise DomainError("evidence_required", "模型数字评分必须有可核验的原话", 422)
        followup = result.get("followup", {})
        status = {"eligible": "gap", "content_sufficient": "sufficient", "no_legal_branch": "no_legal_branch"}.get(followup.get("status"))
        if not status or not isinstance(followup.get("reason"), str) or not followup["reason"].strip():
            raise DomainError("invalid_model_followup", "模型追问判断结构无效", 422)
        branch_id = followup.get("branch_id")
        branch = next((b for b in config["branches"] if b["branch_id"] == branch_id and b["parent_question_id"] == question_id), None)
        if status == "gap" and not branch:
            raise DomainError("invalid_branch", "模型选择了未允许的分支", 422)
        if status != "gap" and branch_id is not None:
            raise DomainError("invalid_branch", "停止判断不得携带追问分支", 422)
        followup_evidence = self._validate_evidence(db, session_id, followup.get("evidence", []), question_id)
        if status == "gap" and not followup_evidence:
            raise DomainError("evidence_required", "模型追问需要内容原话与明确理由", 422)
        evaluation_id = uid()
        evaluation = {"evaluation_id": evaluation_id, "question_id": question_id, "source": "model", "lifecycle": "draft", "input_versions": actual,
                      "rating": {"score": score, "reason": rating["reason"], "evidence": evidence}, "followup": {**followup, "evidence": followup_evidence},
                      "provider": provider_metadata, "created_at": utc()}
        for field in ('schema_version', 'followup_contract_version', 'evidence_units', 'criterion_matches', 'criterion_judgments', 'requirement_judgments', 'gaps'):
            if field in result:
                evaluation[field] = result[field]
        db.execute("INSERT INTO model_evaluations VALUES(?,?,?,?,?)", (evaluation_id, session_id, question_id, dumps(evaluation), evaluation["created_at"]))
        # After decision cutoff, preserve model output as later observation only.
        sealed = bool(db.execute("SELECT 1 FROM decisions WHERE session_id=?", (session_id,)).fetchone())
        if config.get('followup_timing') == 'after_each_main':
            sealed = bool(db.execute("SELECT 1 FROM decision_events e JOIN rounds r ON r.id=e.round_id WHERE e.session_id=? AND r.question_id=? AND r.kind='main'", (session_id, question_id)).fetchone())
        if not sealed and (followup_evidence or (config.get('followup_timing') == 'after_each_main' and status != 'gap')):
            assessment = {"assessment_id": uid(), "question_id": question_id, "status": status, "branch_id": branch_id, "reason": followup["reason"], "evidence": followup_evidence,
                          "actor": "content_provider", "model_run_id": evaluation_id, "input_versions": actual, "created_at": utc()}
            db.execute("INSERT INTO assessments VALUES(?,?,?,?)", (assessment["assessment_id"], session_id, dumps(assessment), assessment["created_at"]))
        self._bump(db, session_id)
        return {"evaluation_id": evaluation_id, "decision_sealed": sealed, "source": "model", "lifecycle": "draft"}

    def apply_model_evaluation(self, session_id, question_id, result, expected_input_versions, provider_metadata):
        with self._db(True) as db:
            return self._apply_model_evaluation(db, session_id, question_id, result, expected_input_versions, provider_metadata)

    def complete_content_job(self, job_id, lease_token, result):
        with self._db(True) as db:
            job = self._lease(db, job_id, lease_token)
            if job["job_type"] != "content":
                raise DomainError("wrong_job_type", "任务不是内容分析", 422)
            expected = json.loads(job["input_versions"])
            if result.get("input_versions") != expected:
                raise DomainError("stale_model_input", "内容服务返回的输入版本不匹配")
            applied = self._apply_model_evaluation(db, job["session_id"], job["question_id"], result["evaluation"], expected, result.get("provider", {}))
            db.execute("UPDATE jobs SET status='succeeded',result=?,ready_at=?,lease_token=NULL WHERE id=?", (dumps({**result, **applied}), utc(), job_id))
            return applied


    def complete_routing_job(self, job_id, lease_token, result):
        with self._db(True) as db:
            job=self._lease(db,job_id,lease_token)
            if job['job_type']!='routing':raise DomainError('wrong_job_type','任务不是短语义核对',422)
            expected=json.loads(job['input_versions']);answers=self._content_inputs(db,job['session_id'],job['question_id'])
            actual={a['answer_id']:a['transcript_revision'] for a in answers}
            if actual!=expected or result.get('input_versions')!=expected:raise DomainError('stale_model_input','追问核对输入已变化')
            config=json.loads(self._session(db,job['session_id'])['config']);question=next(q for q in config['questions'] if q['question_id']==job['question_id'])
            branches=[b for b in config['branches'] if b['parent_question_id']==question['question_id']]
            from interview_routing import validate_routing
            result={**result,'routing':validate_routing(result['routing'],question,answers,branches)}
            sealed=bool(db.execute("SELECT 1 FROM decision_events WHERE session_id=? AND json_extract(payload,'$.question_id')=?",(job['session_id'],job['question_id'])).fetchone())
            result['decision_already_sealed']=sealed
            db.execute("UPDATE jobs SET status='succeeded',result=?,ready_at=?,lease_token=NULL WHERE id=?",(dumps(result),utc(),job_id));self._bump(db,job['session_id'])
            return {'status':'succeeded','decision_already_sealed':sealed}

    def block_job(self, job_id, lease_token, reason):
        with self._db(True) as db:
            job = self._lease(db, job_id, lease_token)
            db.execute("UPDATE jobs SET status='blocked',error=?,ready_at=?,lease_token=NULL WHERE id=?", (str(reason)[:1000], utc(), job_id))
            self._bump(db, job["session_id"])
            return {"status": "blocked"}

    def retry_blocked_content_jobs(self, session_id=None):
        """Explicit worker/operator action after credentials become available."""
        with self._db(True) as db:
            rows = db.execute("SELECT id,session_id FROM jobs WHERE status='blocked' AND job_type='content' AND (? IS NULL OR session_id=?)", (session_id, session_id)).fetchall()
            for row in rows:
                db.execute("UPDATE jobs SET status='queued',attempts=0,error=NULL,ready_at=NULL WHERE id=?", (row["id"],))
                self._bump(db, row["session_id"])
            return {"requeued": len(rows)}

    resume_blocked_content_jobs = retry_blocked_content_jobs

    def retry_content_analysis(self, session_id, question_id, version, idempotency_key):
        """Explicit HR recovery of one current failed input, with append-only retry jobs."""
        payload = {'question_id': question_id, 'version': version}
        with self._db(True) as db:
            previous = self._idempotent(db, 'content-retry:' + session_id, idempotency_key, payload)
            if previous:
                return previous
            session = self._session(db, session_id)
            if session['version'] != version:
                raise DomainError('version_conflict', '本场已更新，请刷新后重试本题')
            config = json.loads(session['config'])
            if not any(q['question_id'] == question_id for q in config['questions']):
                raise DomainError('invalid_question', '只能重试本场所属的主问题', 422)
            answers = self._content_inputs(db, session_id, question_id)
            if not answers or not any(a['transcript'].strip() for a in answers):
                raise DomainError('no_transcript', '本题尚无可分析的回答文字，请先核对转写', 422)
            inputs = {a['answer_id']: a['transcript_revision'] for a in answers}
            current_jobs = [r for r in db.execute("SELECT * FROM jobs WHERE session_id=? AND question_id=? AND job_type='content' ORDER BY created_at DESC,rowid DESC", (session_id, question_id))
                            if json.loads(r['input_versions'] or '{}') == inputs]
            latest = current_jobs[0] if current_jobs else None
            if not latest or latest['status'] not in ('failed', 'blocked'):
                raise DomainError('invalid_retry', '本题没有当前失败任务，或已有任务正在处理；请刷新查看', 409)
            retry_count = max((r['manual_retry_count'] or 0 for r in current_jobs), default=0)
            if retry_count >= 3:
                raise DomainError('retry_limit', '当前回答已人工重试3次，请检查服务配置后再处理', 422)
            # A retry cannot race a media job that is about to add a transcript.
            answer_ids = {a['answer_id'] for a in answers}
            if any(r['answer_id'] in answer_ids for r in db.execute("SELECT answer_id FROM jobs WHERE session_id=? AND job_type='media' AND status IN ('queued','running')", (session_id,))):
                raise DomainError('transcription_pending', '本题仍在转写，请待文字更新后重试', 409)
            job_id = uid()
            db.execute("INSERT INTO jobs(id,session_id,answer_id,status,attempts,input_revision,created_at,job_type,question_id,input_versions,retry_of,manual_retry_count) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (job_id, session_id, latest['answer_id'], 'queued', 0, latest['input_revision'], utc(), 'content', question_id, dumps(inputs), latest['id'], retry_count + 1))
            db.execute("UPDATE jobs SET status='superseded' WHERE id=?", (latest['id'],))
            self._bump(db, session_id)
            result = {'job_id': job_id, 'question_id': question_id, 'status': 'queued',
                      'retry_of': latest['id'], 'manual_retry_count': retry_count + 1,
                      'version': self._session(db, session_id)['version']}
            return self._remember(db, 'content-retry:' + session_id, idempotency_key, payload, result)

    def media_path(self, session_id, answer_id):
        with self._db() as db:
            row = db.execute("SELECT path,media_type FROM answers WHERE id=? AND session_id=?", (answer_id, session_id)).fetchone()
        if not row or not row["path"]:
            raise DomainError("not_found", "媒体不存在", 404)
        path = Path(row["path"]).resolve()
        if not path.is_relative_to(self.data_dir / "media") or not path.is_file():
            raise DomainError("not_found", "媒体文件不可用", 404)
        return path, row["media_type"]

    def claim_job(self, worker_id="local-worker", lease_seconds=120, job_type="media"):
        now = time.time()
        with self._db(True) as db:
            exhausted = db.execute("SELECT id,session_id FROM jobs WHERE status='running' AND lease_until<? AND attempts>=3", (now,)).fetchall()
            for job in exhausted:
                db.execute("UPDATE jobs SET status='failed',error='worker lease expired after 3 attempts',ready_at=? WHERE id=?", (utc(), job["id"]))
                self._bump(db, job["session_id"])
            while True:
                row = db.execute("SELECT j.*,a.path,a.language,a.current AS answer_current FROM jobs j JOIN answers a ON j.answer_id=a.id WHERE (j.status='queued' OR (j.status='running' AND j.lease_until<?)) AND j.attempts<3 AND j.job_type=? ORDER BY j.created_at LIMIT 1", (now, job_type)).fetchone()
                if not row:
                    return None
                if row['job_type']=='routing' and db.execute("SELECT 1 FROM decision_events WHERE session_id=? AND json_extract(payload,'$.question_id')=?",(row['session_id'],row['question_id'])).fetchone():
                    db.execute("UPDATE jobs SET status='superseded',ready_at=? WHERE id=?",(utc(),row['id']));self._bump(db,row['session_id']);continue
                if row['job_type'] == 'media' and not row['answer_current']:
                    db.execute("UPDATE jobs SET status='superseded',error='answer replaced before execution',ready_at=? WHERE id=?", (utc(), row['id']))
                    self._bump(db, row['session_id'])
                    continue
                if row["job_type"] in ("content", "routing"):
                    current_inputs = {a["answer_id"]: a["transcript_revision"] for a in self._content_inputs(db, row["session_id"], row["question_id"])}
                    if current_inputs != json.loads(row["input_versions"]):
                        db.execute("UPDATE jobs SET status='superseded',error='input revision changed before execution',ready_at=? WHERE id=?", (utc(), row["id"]))
                        self._bump(db, row["session_id"])
                        continue
                break
            token = uid()
            db.execute("UPDATE jobs SET status='running',attempts=attempts+1,lease_token=?,lease_until=?,worker_id=? WHERE id=?", (token, now + lease_seconds, worker_id, row["id"]))
            self._bump(db, row["session_id"])
            claimed = {"job_id": row["id"], "job_type": row["job_type"], "session_id": row["session_id"], "answer_id": row["answer_id"], "path": row["path"], "language": row["language"], "transcript_revision": row["input_revision"], "lease_token": token, "attempts": row["attempts"] + 1}
            if row['job_type'] == 'media':
                session_config = json.loads(self._session(db, row['session_id'])['config'])
                claimed['asr_engine'] = session_config.get('asr_engine', 'openai-whisper-base.v1')
            if row["job_type"] in ("content", "routing"):
                config = json.loads(self._session(db, row["session_id"])["config"])
                claimed.update({"question_id": row["question_id"], "question": next(q for q in config["questions"] if q["question_id"] == row["question_id"]),
                                "answers": self._content_inputs(db, row["session_id"], row["question_id"]), "branches": [b for b in config["branches"] if b["parent_question_id"] == row["question_id"]],
                                "input_versions": json.loads(row["input_versions"]), "model_config": {**config.get("model_config", {}), **({"model": config["routing_model"]} if row["job_type"]=="routing" and config.get("routing_model") else {})}})
            return claimed

    def _lease(self, db, job_id, lease_token):
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["status"] != "running" or row["lease_token"] != lease_token or row["lease_until"] < time.time():
            raise DomainError("lease_conflict", "任务租约已失效，拒绝旧worker写回")
        return row

    def heartbeat_job(self, job_id, lease_token, lease_seconds=120):
        with self._db(True) as db:
            self._lease(db, job_id, lease_token)
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (time.time() + lease_seconds, job_id))
        return {"status": "running"}

    def complete_job(self, job_id, lease_token, result):
        with self._db(True) as db:
            job = self._lease(db, job_id, lease_token)
            if job["job_type"] != "media":
                raise DomainError("wrong_job_type", "媒体写回不能用于内容任务", 422)
            answer = db.execute("SELECT * FROM answers WHERE id=?", (job["answer_id"],)).fetchone()
            current_text = db.execute("SELECT text FROM revisions WHERE answer_id=? AND version=?", (answer["id"], answer["revision"])).fetchone()[0]
            transcription = result.get("transcription", {})
            asr_text = transcription.get("text") or ""
            fresh = bool(answer["current"] and answer["revision"] == job["input_revision"])
            output_transcript_revision = None
            if fresh and not current_text.strip() and asr_text.strip() and transcription.get("status") in ("succeeded", "success", "completed", "ok"):
                db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?)", (answer["id"], answer["revision"] + 1, asr_text, "真实ASR输出；原始版本保留", "asr", utc()))
                db.execute("UPDATE answers SET revision=revision+1 WHERE id=?", (answer["id"],))
                output_transcript_revision = answer['revision'] + 1
                self._enqueue_content(db, job["session_id"], answer["id"])
            stored = {**result, "input_transcript_revision": job["input_revision"], "output_transcript_revision": output_transcript_revision, "input_current_at_completion": fresh, "result_ready_at": utc(), "job_id": job_id, "answer_id": answer["id"]}
            db.execute("UPDATE jobs SET status='succeeded',result=?,ready_at=?,lease_token=NULL WHERE id=?", (dumps(stored), stored["result_ready_at"], job_id))
            self._bump(db, job["session_id"])
            return {"status": "succeeded", "answer_id": answer["id"]}

    def fail_job(self, job_id, lease_token, error, retryable=True, code=None, details=None):
        with self._db(True) as db:
            job = self._lease(db, job_id, lease_token)
            status = "queued" if retryable and job["attempts"] < 3 else "failed"
            from interview_job_errors import safe_error_code, safe_diagnostics
            code, details = safe_error_code(code), safe_diagnostics(details)
            db.execute("UPDATE jobs SET status=?,error=?,error_code=?,error_details=?,lease_token=NULL,lease_until=NULL,ready_at=? WHERE id=?", (status, str(error)[:1000], code, dumps(details), utc() if status == "failed" else None, job_id))
            db.execute("INSERT INTO job_failures VALUES(?,?,?,?,?)", (uid(), job_id, job['attempts'], dumps({'code': code, 'details': details, 'message': str(error)[:1000]}), utc()))
            self._bump(db, job["session_id"])
            return {"status": status}
