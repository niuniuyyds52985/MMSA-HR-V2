"""SQLite-backed V2 simulation workflow, independent of the V1 database."""
import copy
import hashlib
import json
import math
import os
import secrets
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from . import assessment, sources
from .scoring import calculate_report, score_summary, COVERAGE_POLICY

ROOT = Path(__file__).resolve().parents[2]
FACTOR_IDS = tuple(f"F{i}" for i in range(1, 9))
DRAFT_FIELDS=("experience_score","experience_reason","experience_evidence","operation_score","operation_reason","summary","next_steps")


def automatic_draft(draft):
    """Protect any teacher/harness changes when retrying an automatic draft."""
    if not draft:
        return True
    allowed = {"local_auto", "bailian_auto"}
    origins = [f.get("source", draft.get("source")) for f in draft.get("factors", [])]
    origins.extend(p.get("source") for p in draft.get("provenance", {}).values())
    return bool(origins) and all(source in allowed for source in origins)


class Conflict(ValueError):
    """Inputs or lifecycle no longer match the requested operation."""


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def required(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"请提供{label}")
    return value.strip()


def bounded_score(value, label):
    if value is None:
        return None
    if type(value) not in (float, int) or not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError(f"{label}应为 0–100")
    return value


def question_by_id(state, qid):
    for q in state["plan"]:
        if q["id"] == qid:
            return q
    raise KeyError("题目不在本场题卷中")


def answer_texts(answer):
    values = [answer.get("text"), answer.get("transcript"), answer.get("answer_label")]
    value = answer.get("value")
    if isinstance(value, str):
        values.append(value)
    return list(dict.fromkeys(t for t in values if isinstance(t, str) and t.strip()))


def has_answer(question, answer):
    return answer.get("value") is not None or (
        question.get("kind") == "text" and bool(answer.get("text") or answer.get("media"))
    )


def set_answer_value(question, answer, body):
    value = body.get("value",body.get("fields",body.get("choices",body.get("text"))))
    if question["kind"]=="text" and answer.get("media") and isinstance(value,str) and not value.strip():
        for key in ("value","text","answer_label"):
            answer.pop(key,None)
        return
    if question["kind"]=="choice" and isinstance(value,list) and len(value)==1:
        value=value[0]
    cleaned = assessment.validate_answer(question,value)
    text = body.get("text","")
    if not isinstance(text,str):
        raise ValueError("作答文字应为文本")
    answer.update(value=cleaned,text=cleaned if question["kind"]=="text" else text.strip(),
                  answer_label=assessment.answer_label(question,cleaned))


def editable(state):
    if state["status"] not in ("interview_open", "awaiting_verification"):
        raise Conflict("场次作答已结束")


def new_input(state):
    state["input_version"] += 1
    state["draft"] = None
    state["review"] = None
    state.pop("teacher_assessment", None)
    state.pop("auto_scoring", None)


def corrected_input(state, factor_id):
    """Invalidate only changed evidence; other successful factor work survives."""
    state["input_version"] += 1
    # Approving an automatic draft or recording an independent assessment is
    # still a human decision, even when every draft field retains an auto source.
    if state.get("review") or state.get("teacher_assessment"):
        state["manual_scoring_required"] = True
    draft = draft_metadata(state)
    if draft:
        if not automatic_draft(draft):
            state["manual_scoring_required"] = True
        draft["factors"] = [f for f in draft.get("factors", []) if f["factor_id"] != factor_id]
        for key in ("experience_score", "experience_reason", "experience_evidence", "summary", "next_steps"):
            draft.pop(key, None)
            draft.get("provenance", {}).pop(key, None)
        draft.pop("model_provenance", None)
        draft["input_version"] = state["input_version"]
        state["draft"] = draft_metadata({**state, "draft": draft})
    if state.get("review"):
        state.setdefault("review_history", []).append({**state["review"], "report_version":state["version"],
                                                       "superseded_by":"answer_correction"})
    state["review"] = None
    teacher = state.get("teacher_assessment")
    if teacher:
        state.setdefault("teacher_assessment_history", []).append({**copy.deepcopy(teacher),
            "report_version":state["version"],"superseded_by":"answer_correction"})
        teacher["factors"] = [f for f in teacher.get("factors", []) if f["factor_id"] != factor_id]
        teacher["input_version"] = state["input_version"]
    state.pop("auto_scoring", None)


def draft_metadata(state):
    draft=copy.deepcopy(state.get("draft"))
    if not draft:
        return None
    old_demo=(state.get("batch_name")=="流程演练 · 合成作答与分数"
              and draft.get("summary","").startswith("以下为脚本生成的流程演练"))
    fallback={"source":"scripted_demo","author":"流程演练脚本"} if old_demo else {"source":"unrecorded","author":""}
    provenance=draft.setdefault("provenance",{})
    for key in DRAFT_FIELDS:
        if draft.get(key) not in (None,"",[]):
            provenance.setdefault(key,fallback.copy())
    for factor in draft.get("factors",[]):
        factor.setdefault("source",fallback["source"])
        factor.setdefault("author",fallback["author"])
    origins=[(f["source"],f["author"]) for f in draft.get("factors",[])]
    origins.extend((p["source"],p["author"]) for p in provenance.values())
    sources_used={source for source,_ in origins}
    authors=list(dict.fromkeys(author for _,author in origins if author))
    draft["source"]=next(iter(sources_used)) if len(sources_used)==1 else ("mixed" if sources_used else fallback["source"])
    draft["author"]="、".join(authors)
    return draft


def media_question(q, a):
    annotation = a.get("observations") or a.get("state_estimate") or {}
    media = a.get("media") or {}
    seconds = annotation.get("duration_seconds") or media.get("duration_seconds")
    if media:
        capture = "recorded" if seconds is not None else "missing"
    elif q.get("kind") == "text":
        capture = "missing"
    else:
        capture = "not_applicable"
    return {"id": q["id"], "title": q["prompt"], "capture": capture,
            "answer_seconds": seconds, "segments": annotation.get("segments", [])}


class Workflow:
    def __init__(self, data_dir=None):
        self.root = Path(data_dir or os.getenv("MMSA_V2_DATA_DIR") or ROOT / "suit-demo/local-data/v2").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = self.root / "workflow.sqlite3"
        with self.connection() as db:
            db.executescript("""
              PRAGMA journal_mode=WAL;
              CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE,created REAL);
              CREATE TABLE IF NOT EXISTS candidates(
                 id TEXT PRIMARY KEY,batch_id TEXT REFERENCES batches(id), digest TEXT NOT NULL, resume TEXT NOT NULL,
                 UNIQUE(batch_id,digest));
              CREATE TABLE IF NOT EXISTS sessions(
                 id TEXT PRIMARY KEY,candidate_id TEXT UNIQUE REFERENCES candidates(id),
                 token TEXT NOT NULL UNIQUE,bank TEXT NOT NULL,state TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS revisions(
                 session_id TEXT,version INTEGER,state TEXT NOT NULL,PRIMARY KEY(session_id,version));
            """)
        # Current reports and cohort rankings use the current coverage policy.
        # Append a revision so explicitly requested historical reports retain
        # their original legacy policy, including the HR iframe's pinned URLs.
        with self.connection(True) as db:
            rows=db.execute("SELECT id,state FROM sessions WHERE json_type(state,'$.scoring_policy') IS NULL").fetchall()
            for row in rows:
                state=json.loads(row["state"])
                state["scoring_policy"]=dict(COVERAGE_POLICY)
                state["version"]+=1
                text=dump(state)
                db.execute("UPDATE sessions SET state=? WHERE id=?",(text,row["id"]))
                db.execute("INSERT INTO revisions VALUES(?,?,?)",(row["id"],state["version"],text))

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.db, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def new_batch(self, name):
        name = required(name, "批次名称")
        with self.connection(True) as db:
            db.execute("INSERT OR IGNORE INTO batches VALUES(?,?,?)",(uuid4().hex,name,time.time()))
            return dict(db.execute("SELECT * FROM batches WHERE name=?",(name,)).fetchone())

    def batches(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute("""SELECT b.*,count(c.id) AS candidate_count FROM batches b
                    LEFT JOIN candidates c ON c.batch_id=b.id GROUP BY b.id ORDER BY b.created DESC""")]

    def import_files(self,bid,files):
        # Parse and validate all documents before committing a batch import.
        docs = []
        for name, raw in files:
            digest = hashlib.sha256(raw).hexdigest()
            resume=sources.parse_resume(raw, filename=name)
            resume["resume_version"]=1
            docs.append((digest,resume,raw))
        with self.connection(True) as db:
            if not db.execute("SELECT 1 FROM batches WHERE id=?",(bid,)).fetchone():
                raise KeyError("批次不存在")
            folder=self.root/"resumes"
            folder.mkdir(exist_ok=True)
            for digest, resume, raw in docs:
                destination=folder/(digest+".docx")
                if not destination.exists():
                    with tempfile.NamedTemporaryFile(dir=folder,delete=False) as stream:
                        temporary=Path(stream.name)
                    try:
                        temporary.write_bytes(raw)
                        os.replace(temporary,destination)
                    finally:
                        temporary.unlink(missing_ok=True)
                db.execute("INSERT OR IGNORE INTO candidates VALUES(?,?,?,?)",
                           (uuid4().hex,bid,digest,dump(resume)))
        return self.batch(bid)

    def correct_resume(self,bid,cid,body):
        changes=body.get("changes")
        reviewer=required(body.get("reviewer"),"更正人")
        reason=required(body.get("reason"),"更正来源及原因")
        allowed={"education","education_status","school","school_tags","internship","english","english_none"}
        if not isinstance(changes,dict) or not changes or not set(changes)<=allowed:
            raise ValueError("仅可更正指定的简历事实字段")
        with self.connection(True) as db:
            row=db.execute("SELECT resume FROM candidates WHERE batch_id=? AND id=?",(bid,cid)).fetchone()
            if not row:
                raise KeyError("批次中不存在该人员")
            resume=json.loads(row["resume"])
            audit=resume.setdefault("corrections",[])
            version=resume.get("resume_version",1)
            if body.get("version")!=version:
                if audit and audit[-1]["from_version"]==body.get("version") and audit[-1]["changes"]==changes and audit[-1]["reviewer"]==reviewer and audit[-1]["reason"]==reason:
                    return self.batch(bid)
                raise Conflict("简历已更正，请刷新后重试")
            if "english" in changes and changes["english"] and changes.get("english_none"):
                raise ValueError("已提供英语成绩时不能同时登记为无")
            enums={"education":{"专科","本科","学士","硕士","博士","未提供"},
                   "education_status":{"在读","已取得","待核实"},"internship":{"有","无","未提供"}}
            for key,value in changes.items():
                if key in enums and (not isinstance(value,str) or value not in enums[key]):
                    raise ValueError(f"{key} 的更正值无效")
                if key=="school":
                    required(value,"院校名称")
                if key=="school_tags" and (not isinstance(value,list) or not all(isinstance(t,str) and t in ("985","211","其他","双一流") for t in value)):
                    raise ValueError("院校标签无效")
                if key=="english_none" and type(value) is not bool:
                    raise ValueError("请明确英语成绩是否为无")
                if key=="english":
                    if not isinstance(value,list):
                        raise ValueError("考试记录应为列表")
                    for e in value:
                        if not isinstance(e,dict) or not isinstance(e.get("exam"),str) or not e["exam"].strip():
                            raise ValueError("考试名称缺失")
                        score=e.get("score")
                        if score is not None and (type(score) not in (int,float) or not math.isfinite(score) or score<0):
                            raise ValueError("考试成绩无效")
            before={k:copy.deepcopy(resume.get(k)) for k in changes}
            resume.update(changes)
            if "school_tags" in changes:
                resume["school_tier"]=" / ".join(changes["school_tags"]) or "未提供"
            if changes.get("english"):
                resume["english_none"]=False
            if changes.get("english_none"):
                resume["english"]=[]
            if changes.get("internship") in ("无","未提供"):
                previous=resume.get("internships",[])
                resume["experiences"]=[e for e in resume.get("experiences",[]) if e not in previous]
                resume["internships"]=[]
            event={"from_version":version,"previous":before,"changes":changes,"reviewer":reviewer,"reason":reason}
            audit.append(event)
            resume["resume_version"]=version+1
            db.execute("UPDATE candidates SET resume=? WHERE id=?",(dump(resume),cid))
            previous=db.execute("SELECT id,state,bank FROM sessions WHERE candidate_id=?",(cid,)).fetchone()
            if previous:
                s=json.loads(previous["state"])
                if s["status"]=="interview_open" and not s["answers"]:
                    builder = sources.screening_plan if s.get("interview_mode")==sources.SHORT_MODE else sources.make_plan
                    s.update(resume=resume,plan=builder(resume,json.loads(previous["bank"])))
                    s.pop("plan_reason",None)
                    new_input(s)
                    s["version"]+=1
                    text=dump(s)
                    db.execute("UPDATE sessions SET state=? WHERE id=?",(text,s["id"]))
                    db.execute("INSERT INTO revisions VALUES(?,?,?)",(s["id"],s["version"],text))
        return self.batch(bid)

    def create_session(self,bid,cid,interview_mode=sources.SHORT_MODE):
        if interview_mode not in (sources.SHORT_MODE,sources.LEGACY_MODE):
            raise ValueError("无法识别的面试题卷类型")
        with self.connection(True) as db:
            candidate = db.execute("SELECT * FROM candidates WHERE id=? AND batch_id=?",(cid,bid)).fetchone()
            if not candidate:
                raise KeyError("候选人不在该批次")
            existing = db.execute("SELECT state FROM sessions WHERE candidate_id=?",(cid,)).fetchone()
            if existing:
                return json.loads(existing["state"])
            sid = uuid4().hex
            token = secrets.token_urlsafe(32)
            resume = json.loads(candidate["resume"])
            bank = sources.load_bank()
            short = interview_mode == sources.SHORT_MODE
            plan = sources.screening_plan(resume,bank) if short else sources.make_plan(resume,bank)
            state = {"id":sid,"candidate_id":cid,"batch_id":bid,"candidate":resume["name"],"resume":resume,
                     "batch_name":db.execute("SELECT name FROM batches WHERE id=?",(bid,)).fetchone()["name"],
                     "token":token,"version":1,"input_version":1,"status":"interview_open","purpose":"simulation",
                     "bank_version":bank["version"],"plan":plan,"answers":{},"draft":None,"review":None,
                     "interview_mode":interview_mode,"created":time.time()}
            state["scoring_policy"] = dict(COVERAGE_POLICY)
            if short:
                state["expected_questions"]=sources.SCREENING_COUNT+sources.INTERVIEW_COUNT
                state["operation_log"]={"policy":"submission.v1","errors":[]}
            text = dump(state)
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",(sid,cid,token,dump(bank),text))
            db.execute("INSERT INTO revisions VALUES(?,?,?)",(sid,1,text))
            return state

    def session(self,sid,version=None):
        with self.connection() as db:
            if version is None:
                row = db.execute("SELECT state FROM sessions WHERE id=?",(sid,)).fetchone()
            else:
                row = db.execute("SELECT state FROM revisions WHERE session_id=? AND version=?",(sid,version)).fetchone()
            if not row:
                raise KeyError("场次或版本不存在")
            state=json.loads(row[0])
            state["draft"]=draft_metadata(state)
            return state

    def resolve_token(self,token):
        with self.connection() as db:
            row = db.execute("SELECT id FROM sessions WHERE token=?",(token,)).fetchone()
            if not row:
                raise KeyError("作答链接无效")
            return row[0]

    def change(self,sid,body,mutate,*,before_commit=None):
        with self.connection(True) as db:
            row = db.execute("SELECT state,bank FROM sessions WHERE id=?",(sid,)).fetchone()
            if not row:
                raise KeyError("场次不存在")
            state = json.loads(row["state"])
            old = state["version"]
            mutate(state,json.loads(row["bank"]))
            changed = dump(state) != row["state"]
            if changed and body.get("version") is not None and body["version"] != old:
                raise Conflict("报告版本已更新，请刷新")
            if before_commit is not None:
                before_commit()
            if not changed:
                return state
            state["version"] = old+1
            text = dump(state)
            db.execute("UPDATE sessions SET state=? WHERE id=?",(text,sid))
            db.execute("INSERT INTO revisions VALUES(?,?,?)",(sid,old+1,text))
            return state

    @staticmethod
    def check_input(state,body):
        if body.get("input_version") != state["input_version"]:
            raise Conflict("证据已更新，请重新读取本场证据")

    def plan(self,sid,body):
        def apply(s,bank):
            editable(s)
            if s.get("interview_mode")==sources.SHORT_MODE:
                raise Conflict("本场按五道单选和三道问答组卷")
            if s["answers"]:
                raise Conflict("作答开始后题卷已冻结")
            if "input_version" in body:
                self.check_input(s,body)
            original = sources.make_plan(s["resume"],bank)
            by_id = {q["id"]:q for q in original}
            ids = body.get("question_ids")
            if not isinstance(ids,list) or not ids or not all(isinstance(i,str) and i in by_id for i in ids) or len(set(ids)) != len(ids):
                raise ValueError("应从本场预设题库选择且不得重复")
            if "EXPERIENCE" not in ids or any(f"{fid}-G" not in ids for fid in FACTOR_IDS):
                raise ValueError("须保留经历介绍及八因子的强制证据题")
            plan = [by_id[i] for i in ids]
            reason = required(body.get("reason"),"组卷依据")
            if plan != s["plan"] or reason != s.get("plan_reason"):
                s.update(plan=plan,plan_reason=reason)
                new_input(s)
        return self.change(sid,body,apply)

    def short_interview(self,sid,body):
        if type(body.get("version")) is not int:
            raise ValueError("切换题卷须提供场次版本")
        def apply(s,bank):
            if s.get("interview_mode")==sources.SHORT_MODE:
                return
            if s["status"]!="interview_open" or any(s.get(k) for k in ("answers","draft","review","teacher_assessment")):
                raise Conflict("只有尚未作答、尚未分析的面试可以切换题卷")
            s.update(interview_mode=sources.SHORT_MODE,expected_questions=sources.SCREENING_COUNT+sources.INTERVIEW_COUNT,
                     plan=sources.screening_plan(s["resume"],bank))
            s["operation_log"]={"policy":"submission.v1","errors":[]}
            s.pop("plan_reason",None)
            new_input(s)
        return self.change(sid,body,apply)

    def answer(self,sid,qid,body,*,media=None,before_commit=None):
        def apply(s,bank):
            editable(s)
            q = question_by_id(s,qid)
            short = s.get("interview_mode")==sources.SHORT_MODE
            screening = short and q.get("stage")=="screening"
            if screening and (media is not None or body.get("text")):
                raise ValueError("单选阶段请选择题库中的一个选项")
            if s["status"]=="awaiting_verification" and qid not in s.get("open_verifications",[]):
                raise Conflict("本轮只接受指定核实题")
            old = s["answers"].get(qid,{})
            answer = old.copy()
            correction = s.get("correction_request") if s["status"] == "awaiting_verification" else None
            if correction and qid not in correction["question_ids"]:
                raise Conflict("本轮只允许补正老师指定的原题")
            if correction and (type(body.get("version")) is not int or body["version"] != s["version"]):
                raise Conflict("补正作答须基于当前版本，请刷新")
            if media is not None:
                if (old.get("media") or {}).get("sha256") != media["sha256"]:
                    for key in ("processing","processing_transcribe","observations","transcript","state_estimate","transcription_provenance","text_source"):
                        answer.pop(key,None)
                    if correction and "text" not in body:
                        for key in ("text","value","answer_label"):
                            answer.pop(key,None)
                answer["media"]=media
            if media is None or "text" in body:
                if media is not None and q["kind"] != "text":
                    raise ValueError("只有文字作答题可随录制提交文字")
                set_answer_value(q,answer,body)
                if correction and answer.get("text") and answer.get("text") != old.get("text"):
                    for key in ("transcript","transcription_provenance"):
                        answer.pop(key,None)
                    answer["text_source"]="candidate_corrected_text"
                    if answer.get("processing"):
                        answer["processing"]=copy.deepcopy(answer["processing"])
                        answer["processing"]["transcript"]=""
                        answer["processing"]["transcript_segments"]=[]
            if screening:
                answer["sjt_score"]=assessment.sjt_score(qid,answer["value"],bank)
            if answer==old:
                if correction and qid not in correction.get("completed_question_ids", []):
                    raise ValueError("请保存新的录制或修正后的原话，再提交补正")
                return
            if screening and any(p.get("stage")=="interview" and has_answer(p,s["answers"].get(p["id"],{}))
                                 for p in s["plan"]):
                raise Conflict("问答已经开始保存，作为组卷依据的单选回答已锁定")
            s["answers"][qid]=answer
            if correction:
                corrected_input(s,q["factor_id"])
                if qid not in correction["completed_question_ids"]:
                    correction["completed_question_ids"].append(qid)
                s.setdefault("input_corrections", []).append({"kind":"candidate_resubmission", "question_id":qid,
                    "reviewer":correction["reviewer"], "reason":correction["reason"], "before_version":s["version"],
                    "media_sha256":(answer.get("media") or {}).get("sha256"), "created":time.time()})
            else:
                new_input(s)
            if short:
                if screening:
                    choices=[p for p in s["plan"] if p["stage"]=="screening"]
                    s["plan"]=choices+sources.interview_questions(choices,s["answers"],s["resume"],bank)
                return
            if answer.get("value") is None or old.get("value")==answer["value"]:
                return
            cleaned=answer["value"]
            children = sources.conditional_questions(q,cleaned,bank)
            new_ids={c["id"] for c in children}
            old_ids={p["id"] for p in s["plan"] if p.get("condition_parent")==qid}
            for child_id in old_ids-new_ids:
                s["answers"].pop(child_id,None)
            if old_ids!=new_ids:
                plan=[p for p in s["plan"] if p.get("condition_parent")!=qid]
                at=next(i for i,p in enumerate(plan) if p["id"]==qid)+1
                plan[at:at]=[dict(c,condition_parent=qid) for c in children]
                s["plan"]=plan
        return self.change(sid,body,apply,before_commit=before_commit)

    def add_media(self,sid,qid,meta,*,text=None,version=None,before_commit=None):
        body={"version":version}
        if text is not None:
            body["text"]=text
        return self.answer(sid,qid,body,media=meta,before_commit=before_commit)

    @staticmethod
    def check_correction(state,qid,body):
        if type(body.get("version")) is not int or body["version"] != state["version"]:
            raise Conflict("补正须基于当前报告版本，请刷新")
        Workflow.check_input(state,body)
        question=question_by_id(state,qid)
        if (state.get("interview_mode") != sources.SHORT_MODE or question.get("stage") != "interview"
                or question.get("kind") != "text"):
            raise ValueError("只能补正本场三道正式问答，不改变题目和单选")
        if state["status"] not in ("interview_finished","draft","reviewed","review_changes","awaiting_verification"):
            raise Conflict("请先提交本场面试，再由老师发起补正")
        if "media_sha256" not in body or body["media_sha256"] != (state["answers"].get(qid,{}).get("media") or {}).get("sha256"):
            raise Conflict("原片已变化，请核对当前原片")
        return question, required(body.get("reviewer"),"补正老师姓名"), required(body.get("reason"),"补正原因")

    def correct_transcript(self,sid,qid,body):
        def apply(s,bank):
            q, reviewer, reason=self.check_correction(s,qid,body)
            if s["status"] == "awaiting_verification":
                raise Conflict("学生正在补正，请待其提交后再校正原话")
            answer=s["answers"].get(qid,{})
            if not answer.get("media"):
                raise ValueError("请基于本题录音录像校正转写；文字作答可发起原题补正")
            transcript=required(body.get("transcript"),"核对原片后的转写原文")
            if len(transcript)>25000:
                raise ValueError("转写原文不可超过25000字")
            before=s["version"]
            corrected_input(s,q["factor_id"])
            # One authoritative current text. Prior typed/transcribed text is in revisions.
            for key in ("text","value","answer_label"):
                answer.pop(key,None)
            answer.update(transcript=transcript,text_source="teacher_corrected_transcript",
                transcription_provenance={"provider":"human_review","status":"corrected","reviewer":reviewer,
                    "reason":reason,"media_sha256":body["media_sha256"],"created":time.time(),"word_timestamps_available":False})
            if answer.get("processing"):
                answer["processing"]["transcript"]=transcript
                answer["processing"]["transcript_segments"]=[]
            s.setdefault("input_corrections",[]).append({"kind":"transcript_correction","question_id":qid,
                "reviewer":reviewer,"reason":reason,"before_version":before,"media_sha256":body["media_sha256"],"created":time.time()})
            s["status"]="draft" if s.get("manual_scoring_required") else "interview_finished"
            s["auto_scoring"] = ({"status":"manual_review_required", "issues":["原话已补正，已有人工评价已保留，请老师重新复核相关分项。"]}
                if s.get("manual_scoring_required") else {"status":"queued","run_id":uuid4().hex})
        return self.change(sid,body,apply)

    def reopen_answer(self,sid,qid,body):
        def apply(s,bank):
            _, reviewer, reason=self.check_correction(s,qid,body)
            if s["status"]=="awaiting_verification":
                raise Conflict("已有补正进行中，请先完成当前原题补正")
            s["correction_request"]={"question_ids":[qid],"completed_question_ids":[],"reviewer":reviewer,
                "reason":reason,"requested_version":s["version"],"created":time.time()}
            s.update(status="awaiting_verification",open_verifications=[qid])
            s.pop("auto_scoring",None)
        return self.change(sid,body,apply)

    def introduction(self,sid,body,*,media=None,before_commit=None):
        """Save optional warm-up separately from scored answers and scoring input versions."""
        if type(body.get("version")) is not int:
            raise ValueError("请提供当前场次版本")
        def apply(s,bank):
            if s["status"] != "interview_open":
                raise Conflict("本场已结束或进入补答，自我介绍已归档")
            old = s.get("introduction") or {}
            if body.get("skip") is True:
                if media is not None or body.get("text"):
                    raise ValueError("跳过暖场时不提交作答")
                if old.get("status") == "saved":
                    return
                s["introduction"] = {"status":"skipped"}
                return
            answer = dict(old)
            if media is not None:
                answer["media"] = media
            if "text" in body:
                text = body["text"]
                if not isinstance(text,str) or len(text) > 25000:
                    raise ValueError("自我介绍应为不超过25000字的文本")
                answer["text"] = text.strip()
            if not (answer.get("text") or answer.get("media")):
                raise ValueError("请填写自我介绍、录制音视频，或选择跳过暖场")
            answer["status"] = "saved"
            s["introduction"] = answer
        return self.change(sid,body,apply,before_commit=before_commit)

    def process(self,sid,qid,transcribe=True,refresh=False):
        from .media import analyze_recording, propose_states
        if type(transcribe) is not bool or type(refresh) is not bool:
            raise ValueError("处理参数须为布尔值")
        start=self.session(sid)
        a=start["answers"].get(qid,{})
        media=a.get("media")
        if not media:
            raise ValueError("此题没有已上传的媒体")
        if not refresh and a.get("processing") and a.get("processing_transcribe")==transcribe and a["processing"].get("status") not in ("failed","error"):
            return start
        result=analyze_recording(self.media_path(media),transcribe=transcribe)
        examples=[]
        with self.connection() as db:
            for (payload,) in db.execute("SELECT state FROM sessions WHERE id<>?",(sid,)):
                previous=json.loads(payload)
                if previous["status"]!="reviewed":
                    continue
                for answer in previous["answers"].values():
                    annotation=answer.get("observations") or {}
                    processing=answer.get("processing") or {}
                    if (answer.get("media") or {}).get("source")=="synthetic_fixture" or annotation.get("reviewer")=="流程演练脚本（已知标签）":
                        continue
                    if processing.get("media_sha256") and annotation.get("media_sha256")==processing["media_sha256"]:
                        examples.append((processing,annotation["segments"]))
        result["suggestions"]=propose_states(result,examples)
        def apply(s,bank):
            a=s["answers"].get(qid,{})
            if (a.get("media") or {}).get("sha256") != media["sha256"]:
                raise Conflict("录制已替换，请重新分析")
            previous_audit=(a.get("processing") or {}).get("v1") or {}
            if previous_audit.get("media_sha256")==media["sha256"]:
                result["v1"]=previous_audit
            if result.get("duration_seconds"):
                a["media"]["duration_seconds"]=result["duration_seconds"]
            # A features-only refresh or a failed retranscription must preserve quoted evidence.
            if a.get("text_source") == "teacher_corrected_transcript":
                result["transcript"] = a["transcript"]
                result["transcript_segments"] = []
            elif not result.get("transcript") and a.get("transcript"):
                result["transcript"]=a["transcript"]
                result["transcript_segments"]=(a.get("processing") or {}).get("transcript_segments",[])
            transcript=result.get("transcript") or ""
            if transcript!=a.get("transcript",""):
                new_input(s)
                if s["status"] in ("draft","reviewed","review_changes"):
                    s["status"]="interview_finished"
            a.update(processing=result,processing_transcribe=transcribe,transcript=transcript)
        return self.change(sid,{},apply)

    def media_path(self,meta):
        name=meta["stored_name"]
        if Path(name).name!=name or "/" in name or "\\" in name:
            raise ValueError("媒体标识无效")
        return self.root / "media" / name

    def historical_media(self,sid,qid,fingerprint):
        if len(fingerprint)!=64 or any(c not in "0123456789abcdef" for c in fingerprint):
            raise ValueError("原片版本无效")
        with self.connection() as db:
            match=db.execute("""
                SELECT json_extract(a.value,'$.media') FROM revisions AS r,
                    json_each(r.state,'$.answers') AS a
                WHERE r.session_id=? AND a.key=? AND json_extract(a.value,'$.media.sha256')=?
                ORDER BY r.version DESC LIMIT 1
            """,(sid,qid,fingerprint)).fetchone()
        if not match:
            raise KeyError("该题没有此版本的原片")
        return json.loads(match[0])

    def observations(self,sid,qid,body):
        def apply(s,bank):
            q=question_by_id(s,qid)
            a=s["answers"].get(qid,{})
            media=a.get("media")
            if not media or body.get("confirmed") is not True:
                raise ValueError("请基于本题媒体确认标注")
            if body.get("media_sha256") is not None and body["media_sha256"]!=media["sha256"]:
                raise Conflict("原片已替换，请复核当前录制")
            seconds=body.get("duration_seconds",media.get("duration_seconds"))
            if type(seconds) not in (float,int) or not math.isfinite(seconds) or seconds<=0:
                raise ValueError("请提供已核实的录制时长")
            known=media.get("duration_seconds")
            if known and abs(known-seconds)>max(.25,known*.001):
                raise ValueError("标注时长与原片不符")
            annotation={"reviewer":required(body.get("reviewer"),"标注人"),"duration_seconds":seconds,
                        "segments":body.get("segments"),"media_sha256":media["sha256"]}
            test=media_question(q,{**a,"observations":annotation})
            calculate_report({"purpose":"simulation","candidate":"标注校验",
                             "components":dict.fromkeys(("basic_info","experience","potential","operation")),
                             "questions":[test]})
            if a.get("observations")!=annotation:
                a["observations"]=annotation
                s["review"]=None
                if s["status"]=="reviewed":
                    s["status"]="draft"
        return self.change(sid,body,apply)

    def operation_event(self,sid,code,qid=None):
        """Count each verifiable omission once, excluding technical failures and retries."""
        def apply(s,bank):
            log=s.get("operation_log")
            if log is None or s["status"]!="interview_open":
                return
            if code=="empty_save":
                q=question_by_id(s,qid)
                if has_answer(q,s["answers"].get(qid,{})):
                    return
            elif code=="incomplete_submit":
                if len(s["plan"])==s["expected_questions"] and all(has_answer(q,s["answers"].get(q["id"],{})) for q in s["plan"]):
                    return
            else:
                raise ValueError("未知的操作记录")
            event={"code":code,"question_id":qid}
            if event not in log["errors"]:
                log["errors"].append(event)
        return self.change(sid,{},apply)

    def finish(self,sid,body):
        def apply(s,bank):
            if s["status"] not in ("interview_open","awaiting_verification"):
                return
            correction=s.get("correction_request") if s["status"]=="awaiting_verification" else None
            if correction and set(correction["question_ids"])-set(correction["completed_question_ids"]):
                raise ValueError("请先完成指定原题的补正保存")
            missing=[q["id"] for q in s["plan"] if not has_answer(q,s["answers"].get(q["id"],{}))]
            if s.get("interview_mode")==sources.SHORT_MODE and (len(s["plan"])!=s["expected_questions"] or missing):
                raise ValueError("请先完成五道单选和三道问答，再提交面试")
            outstanding=missing
            if s["status"]=="awaiting_verification":
                outstanding=[qid for qid in missing if qid in s.get("open_verifications",[])]
            if outstanding and body.get("allow_incomplete") is not True:
                raise ValueError("尚有未完成题目："+"、".join(outstanding))
            s.update(status="interview_finished",missing_questions=missing,open_verifications=[])
            if correction:
                s.setdefault("correction_requests_history",[]).append({**s.pop("correction_request"),"completed":time.time()})
            if s.get("interview_mode")==sources.SHORT_MODE:
                if s.get("manual_scoring_required"):
                    s["status"]="draft"
                    s["auto_scoring"]={"status":"manual_review_required","issues":["补正已提交，保留未受影响的人工评分；请老师复核补正因子与综合意见。"]}
                else:
                    s["auto_scoring"]={"status":"queued","run_id":uuid4().hex}
        return self.change(sid,body,apply)

    def clarify(self,sid,body):
        def apply(s,bank):
            if s.get("interview_mode")==sources.SHORT_MODE:
                raise Conflict("本场固定八道题，后续核实建议可记录到下一轮")
            source=question_by_id(s,required(body.get("source_question_id"),"来源题号"))
            factor=source.get("factor_id")
            if factor not in FACTOR_IDS:
                raise ValueError("来源题须属于某一潜力因子")
            options=sources.verification_questions(factor,bank)
            selected=body.get("question_id") or options[0]["id"]
            chosen=next((q for q in options if q["id"]==selected),None)
            if not chosen:
                raise ValueError("只能使用该因子的预设核实题")
            if any(q["id"]==selected for q in s["plan"]):
                return
            self.check_input(s,body)
            if s["status"]=="reviewed":
                raise Conflict("复核已完成")
            s["plan"].append(dict(chosen,parent_question_id=source["id"],
                                   verification_reason=required(body.get("reason"),"核实依据")))
            if s["status"]!="interview_open":
                s["status"]="awaiting_verification"
                s.setdefault("open_verifications",[]).append(selected)
            new_input(s)
        return self.change(sid,body,apply)

    @staticmethod
    def checked_factor(state,entry):
        if not isinstance(entry,dict):
            raise ValueError("因子记录须为对象")
        fid=entry.get("factor_id")
        if fid not in FACTOR_IDS:
            raise ValueError("因子编号无效")
        rating=entry.get("rating")
        if rating is not None and (type(rating) is not int or not 1<=rating<=5):
            raise ValueError("因子评级应为1–5的整数或证据不足")
        reason=required(entry.get("reason"),"评分理由或证据缺口")
        refs=entry.get("evidence",[])
        if not isinstance(refs,list) or (rating is not None and not refs):
            raise ValueError("评分须引用对应因子的实际回答")
        evidence=[]
        for ref in refs:
            if not isinstance(ref,dict):
                raise ValueError("证据引用无效")
            q=question_by_id(state,required(ref.get("question_id"),"证据题号"))
            if q.get("factor_id")!=fid:
                raise ValueError("证据题与因子不一致")
            quote=required(ref.get("quote"),"作答原话")
            a=state["answers"].get(q["id"],{})
            if state.get("interview_mode")==sources.SHORT_MODE and q["kind"]=="choice":
                texts=[a.get("answer_label") or ""]
            else:
                texts=answer_texts(a)
            if not any(quote in text for text in texts):
                raise ValueError(f"{q['id']} 的回答或转写中找不到引文")
            evidence.append({"question_id":q["id"],"quote":quote})
        result={"factor_id":fid,"rating":rating,"reason":reason,"evidence":evidence}
        if rating==5:
            checks=entry.get("verification",{})
            if not isinstance(checks,dict) or not set(checks)<={"action","cost","experience"}:
                raise ValueError("高分核实应分别引用本人行动、付出代价和相似经历的作答")
            verified={}
            for kind in ("action","cost","experience"):
                ref=checks.get(kind)
                if ref is None:
                    qid=f"{fid}-verify-{kind}"
                    answer=state["answers"].get(qid,{})
                    text=answer.get("text") or answer.get("transcript")
                    if text:
                        verified[kind]={"question_id":qid,"quote":text}
                    continue
                if not isinstance(ref,dict):
                    raise ValueError("高分核实须提供题号和原话")
                q=question_by_id(state,required(ref.get("question_id"),"核实题号"))
                quote=required(ref.get("quote"),"核实原话")
                a=state["answers"].get(q["id"],{})
                if q.get("factor_id")!=fid or not any(quote in text for text in (a.get("text") or "",a.get("transcript") or "")):
                    raise ValueError(f"{fid} 的{kind}核实须引用本因子的实际叙述或转写")
                verified[kind]={"question_id":q["id"],"quote":quote}
            result.update(verification=verified,verification_gaps=[k for k in ("action","cost","experience") if k not in verified])
        return result

    def draft(self,sid,body):
        return self.change(sid,body,lambda s,bank: self.apply_draft(s,body))

    def apply_draft(self,s,body,*,automatic=False):
        self.check_input(s,body)
        if s["status"] not in ("interview_finished","draft","review_changes"):
            raise Conflict("须先结束作答，再保存分析草稿")
        source=body.get("source","unrecorded")
        if source not in ("harness","local_review","scripted_demo","unrecorded") and not (automatic and source in ("local_auto","bailian_auto")):
            raise ValueError("未知的草稿撰写来源")
        author=required(body.get("author"),"草稿录入人") if source=="local_review" else str(body.get("author") or "")
        actor={"source":source,"author":author}
        current=draft_metadata(s) or {"input_version":s["input_version"],"factors":[],
                              "experience_score":None,"operation_score":None,"summary":"","next_steps":[]}
        current.setdefault("provenance",{})
        before={key:current.get(key) for key in DRAFT_FIELDS}
        factors={f["factor_id"]:f for f in current["factors"]}
        supplied=body.get("factors",[])
        if not isinstance(supplied,list) or not all(isinstance(f,dict) for f in supplied):
            raise ValueError("因子应为记录列表")
        if not all(f.get("factor_id") in FACTOR_IDS for f in supplied):
            raise ValueError("因子编号无效")
        if len({f.get("factor_id") for f in supplied})!=len(supplied):
            raise ValueError("存在重复因子")
        for item in supplied:
            checked=self.checked_factor(s,item)
            existing=factors.get(checked["factor_id"],{})
            previous={k:v for k,v in existing.items() if k not in ("source","author")}
            if checked!=previous:
                factors[checked["factor_id"]]={**checked,**actor}
        current["factors"]=[factors[f] for f in FACTOR_IDS if f in factors]
        if "experience_score" in body or "experience_evidence" in body:
            score=bounded_score(body.get("experience_score",current.get("experience_score")),"经历介绍分")
            if s.get("interview_mode")==sources.SHORT_MODE:
                if score is None:
                    current.pop("experience_evidence",None)
                else:
                    ref=body.get("experience_evidence",current.get("experience_evidence"))
                    if not isinstance(ref,dict):
                        raise ValueError("经历评分须引用本场问答的题号和经历原话")
                    q=question_by_id(s,required(ref.get("question_id"),"经历证据题号"))
                    quote=required(ref.get("quote"),"经历原话")
                    a=s["answers"].get(q["id"],{})
                    if q["kind"]!="text" or not any(quote in (a.get(key) or "") for key in ("text","transcript")):
                        raise ValueError("经历引文须来自所选问答的作答原文或转写")
                    current["experience_evidence"]={"question_id":q["id"],"quote":quote}
            elif score is not None and not answer_texts(s["answers"].get("EXPERIENCE",{})):
                raise ValueError("经历评分须有经历介绍原话或转写")
            if score is not None and source=="local_review":
                required(body.get("experience_reason",current.get("experience_reason")),"经历评分依据")
            current["experience_score"]=score
        if "experience_reason" in body:
            if not isinstance(body["experience_reason"],str):
                raise ValueError("经历评分依据须为文本")
            current["experience_reason"]=body["experience_reason"].strip()
        if "operation_score" in body:
            score=bounded_score(body["operation_score"],"流程操作分")
            reason=body.get("operation_reason",current.get("operation_reason",""))
            if score is not None:
                reason=required(reason,"操作记录依据")
            current.update(operation_score=score,operation_reason=reason)
        if "summary" in body:
            if not isinstance(body["summary"],str):
                raise ValueError("综合意见须为文本")
            current["summary"]=body["summary"].strip()
        if "next_steps" in body:
            steps=body["next_steps"]
            if isinstance(steps,str):
                steps=[steps] if steps.strip() else []
            if not isinstance(steps,list) or not all(isinstance(t,str) for t in steps):
                raise ValueError("后续建议应为文字列表")
            current["next_steps"]=steps
        for key in DRAFT_FIELDS:
            if key in body and before[key]!=current.get(key):
                current["provenance"][key]=actor.copy()
        if automatic:
            if "model_provenance" in body:
                current["model_provenance"] = copy.deepcopy(body["model_provenance"])
            if "operation_score" in body:
                for key in ("operation_score", "operation_reason"):
                    current["provenance"][key] = {"source":"local_auto", "author":"操作记录确定性计算"}
        current=draft_metadata({**s,"draft":current})
        if current!=s["draft"]:
            s.update(draft=current,review=None,status="draft")

    def teacher_assessment(self,sid,body):
        def apply(s,bank):
            self.check_input(s,body)
            if s["status"] in ("interview_open","awaiting_verification","reviewed"):
                raise Conflict("独立评价需在作答结束、最终复核前提交")
            reviewer=required(body.get("reviewer"),"独立评价人")
            entries=body.get("factors")
            if not isinstance(entries,list) or not entries:
                raise ValueError("请提交至少一个独立评价因子")
            saved=s.get("teacher_assessment") or {"factors":[]}
            if saved.get("reviewer",reviewer)!=reviewer:
                raise Conflict("该场独立评价已有署名，请使用同一评价人或在最终复核记录意见")
            factors={f["factor_id"]:f for f in saved["factors"]}
            for entry in entries:
                checked=self.checked_factor(s,entry)
                factors[checked["factor_id"]]=checked
            s["teacher_assessment"]={"reviewer":reviewer,"input_version":s["input_version"],
                                     "factors":[factors[f] for f in FACTOR_IDS if f in factors]}
        return self.change(sid,body,apply)

    def review(self,sid,body):
        def apply(s,bank):
            decision=body.get("decision")
            if decision not in ("approve","request_changes"):
                raise ValueError("复核结果无效")
            reviewer=required(body.get("reviewer"),"复核人")
            review={"reviewer":reviewer,"decision":decision,"notes":str(body.get("notes") or ""),
                    "draft_input_version":s["input_version"]}
            if review==s["review"]:
                return
            if body.get("version")!=s["version"]:
                raise Conflict("复核须基于当前报告版本")
            draft=s["draft"]
            if not draft or draft["input_version"]!=s["input_version"]:
                raise Conflict("缺少基于当前证据的草稿")
            if decision=="approve":
                expected=set(FACTOR_IDS)
                if s.get("interview_mode")==sources.SHORT_MODE:
                    expected={q["factor_id"] for q in s["plan"] if q["kind"]=="text" and q.get("factor_id")}
                analyzed={f["factor_id"] for f in draft["factors"]}
                if expected-analyzed or not draft["summary"]:
                    raise ValueError("批准前须逐项分析本场考察的因子或说明证据缺口，并提供综合意见")
                outstanding=[f["factor_id"] for f in draft["factors"] if f.get("verification_gaps") and f["factor_id"] in expected]
                if outstanding:
                    raise ValueError("以下5分因子仍需完成行动、代价、相似经历核实："+"、".join(outstanding))
                proposed={f["factor_id"]:f["rating"] for f in draft["factors"]}
                if s.get("interview_mode")==sources.SHORT_MODE:
                    for fid,signal in assessment.choice_signals(s["plan"],s["answers"],bank).items():
                        if proposed.get(fid) is None:
                            proposed[fid]=signal["score"]
                independent=(s.get("teacher_assessment") or {}).get("factors",[])
                if any(f["factor_id"] in proposed and proposed[f["factor_id"]]!=f["rating"] for f in independent):
                    required(review["notes"],"独立评分与拟采用分数有分歧时的复核意见")
            s.update(review=review,status="reviewed" if decision=="approve" else "review_changes")
        return self.change(sid,body,apply)

    def evidence(self,sid,factor=None):
        with self.connection() as db:
            row=db.execute("SELECT bank,state FROM sessions WHERE id=?",(sid,)).fetchone()
            if not row:
                raise KeyError("场次不存在")
        state=json.loads(row["state"])
        bank=json.loads(row["bank"])
        if factor and factor not in (*FACTOR_IDS,"EXPERIENCE"):
            raise ValueError("未知因子")
        questions=[q for q in state["plan"] if not factor or q.get("factor_id")==factor or q["id"]==factor]
        evidence={k:state[k] for k in ("id","candidate","resume","status","version","input_version","bank_version","draft","review")}
        evidence.update({k:state[k] for k in ("interview_mode","expected_questions") if k in state})
        evidence["draft"]=draft_metadata(state)
        evidence.update(plan=questions, factors=[f for f in bank["factors"] if not factor or f["id"]==factor],
                        rules=bank.get("rules",{}), answers={q["id"]:state["answers"][q["id"]] for q in questions
                                                         if q["id"] in state["answers"]})
        factors_by_id={f["id"]:f for f in bank["factors"]}
        for q in questions:
            a=evidence["answers"].get(q["id"])
            if a is None:
                continue
            if a.get("value") is not None:
                a["sjt_score"]=assessment.sjt_score(q["id"],a["value"],bank)
            if q["kind"]=="text" and q.get("factor_id") in factors_by_id:
                a["word_signals"]=assessment.evidence_signals(factors_by_id[q["factor_id"]],
                                                           a.get("text") or a.get("transcript") or "")
        return evidence

    def report_from(self,s,bank):
        draft=draft_metadata(s) or {}
        factor_scores={f["factor_id"]:f for f in draft.get("factors",[])}
        short=s.get("interview_mode")==sources.SHORT_MODE
        signals=assessment.choice_signals(s["plan"],s["answers"],bank) if short else {}
        review=s.get("review") or {}
        factors=[dict(id=f["id"],factor_id=f["id"],name=f["name"],dimension=f.get("dimension"),
                      anchors=f.get("anchors",""),
                      anchor_source=next(("八因子提问清单!"+cell for cell in f.get("source_cells",{}) if cell.startswith("H")),""),
                      rating=None,reason="",evidence=[]) for f in bank["factors"]]
        for f in factors:
            f.update(factor_scores.get(f["id"],{}))
            signal=signals.get(f["id"])
            if signal:
                proposed=f["rating"]
                approved=review.get("decision")=="approve" and review.get("reviewer") and proposed is not None and f.get("evidence")
                f.update(choice_signal=signal,assessment_rating=proposed,verification_gaps=[],
                         rating=proposed if approved else signal["score"],
                         rating_source="reviewed_adjustment" if approved and proposed!=signal["score"] else "choice_key")
        ratings={f["id"]:f["rating"] if not f.get("verification_gaps") else None for f in factors}
        inputs={"basic_info":assessment.basic_info_score(s["resume"]),"experience":draft.get("experience_score"),
                "potential":assessment.potential_score(ratings),"operation":draft.get("operation_score")}
        questions=[media_question(q,s["answers"].get(q["id"],{})) for q in s["plan"]]
        result=calculate_report(dict(purpose="simulation",candidate=s["candidate"],components=inputs,questions=questions,
            coverage_policy=s.get("scoring_policy", {"version":"legacy-any-valid","min_question_coverage":0,"min_session_coverage":0})))
        potential=result["components"]["potential"]
        rated=[rating for rating in ratings.values() if rating is not None]
        if potential["score"] is None and rated:
            potential.update(rated_count=len(rated), rated_raw=sum(rated),
                             partial_contribution=sum(rated) * (100 * potential["weight"]) / (5 * len(FACTOR_IDS)))
        result.update({k:s.get(k) for k in ("id","batch_id","batch_name","candidate_id","resume","status","version","input_version",
                                             "bank_version","plan","answers","draft","review","auto_scoring","operation_log","introduction",
                                             "input_corrections","correction_request","manual_scoring_required","review_history","teacher_assessment_history")})
        if short:
            result.update(interview_mode="short_5_3",expected_questions=s.get("expected_questions",8))
        result.update(factors=factors,draft=draft or None,summary=draft.get("summary",""),next_steps=draft.get("next_steps",[]),
                      basic_info_breakdown=assessment.basic_info_breakdown(s["resume"]),
                      teacher_assessment=s.get("teacher_assessment"))
        for q in result["questions"]:
            q["answer"]=s["answers"].get(q["id"],{})
        return result

    def report(self,sid,version=None):
        with self.connection() as db:
            row=db.execute("SELECT state,bank FROM sessions WHERE id=?",(sid,)).fetchone()
            if not row:
                raise KeyError("场次不存在")
            state=row["state"]
            if version is not None:
                v=db.execute("SELECT state FROM revisions WHERE session_id=? AND version=?",(sid,version)).fetchone()
                if not v:
                    raise KeyError("报告版本不存在")
                state=v[0]
            return self.report_from(json.loads(state),json.loads(row["bank"]))

    def batch(self,bid):
        with self.connection() as db:
            b=db.execute("SELECT * FROM batches WHERE id=?",(bid,)).fetchone()
            if not b:
                raise KeyError("批次不存在")
            rows=db.execute("""SELECT c.id,c.resume,s.state,s.bank FROM candidates c LEFT JOIN sessions s ON s.candidate_id=c.id
                            WHERE c.batch_id=? ORDER BY c.rowid""",(bid,)).fetchall()
        people=[]
        stats={"count":len(rows),"interview_count":0,"finished_count":0,"reviewed_count":0,"complete_count":0,
               "ranked_count":0,"education":{},"school_tier":{},"english":{},"gender":{}}
        for row in rows:
            resume=json.loads(row["resume"])
            person={"id":row["id"],"name":resume["name"],"resume":resume,"session_id":None,"status":"unstarted","total":None,
                    "display_score":None,"score_kind":"pending","rank":None,"rated_factor_count":0,
                    "answered_questions":0,"expected_questions":None}
            if row["state"]:
                s=json.loads(row["state"])
                r=self.report_from(s,json.loads(row["bank"]))
                person.update(session_id=s["id"],status=s["status"],version=s["version"],total=r["total"],
                              factors=r["factors"],components=r["components"],review=r.get("review"),
                              analysis_source=(r.get("draft") or {}).get("source"),
                              reviewer=(r.get("review") or {}).get("reviewer"))
                person.update(score_summary(r),
                              student_url="/v2/c/"+s["token"],
                              rated_factor_count=sum(f.get("rating") is not None and not f.get("verification_gaps") for f in r["factors"]),
                              answered_questions=sum(has_answer(q,s["answers"].get(q["id"],{})) for q in s["plan"]),
                              expected_questions=s.get("expected_questions",len(s["plan"])))
                stats["interview_count"]+=1
                stats["finished_count"]+=s["status"] not in ("interview_open","awaiting_verification")
                stats["reviewed_count"]+=s["status"]=="reviewed"
                stats["complete_count"]+=r["total"] is not None
            people.append(person)
            for key in ("education","school_tier","english","gender"):
                v=resume.get(key)
                if isinstance(v,list):
                    vals=[a.get("exam") or a.get("type") or a.get("name") or "未知" if isinstance(a,dict) else str(a) for a in v]
                elif isinstance(v,dict):
                    vals=[v.get("level") or v.get("value") or "未知"]
                else:
                    vals=[str(v)] if v else []
                for label in set(vals or ["未提供"]):
                    stats[key][label]=stats[key].get(label,0)+1
        ranked = [p for p in people if p["score_kind"] in ("total","estimate")
                  and p["status"] not in ("interview_open","awaiting_verification")]
        ranked.sort(key=lambda p: round(p["display_score"],2), reverse=True)
        for index, person in enumerate(ranked):
            tied = index and round(person["display_score"],2) == round(ranked[index-1]["display_score"],2)
            person["rank"] = ranked[index-1]["rank"] if tied else index+1
        stats["ranked_count"] = len(ranked)
        people = ranked + [p for p in people if p["rank"] is None]
        return dict(**dict(b),candidates=people,stats=stats)
