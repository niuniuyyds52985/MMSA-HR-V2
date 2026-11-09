"""Durable per-question analysis; retries preserve successful evidence and scores."""
import copy
import hashlib
import json
import logging
import os
from threading import Lock
from uuid import uuid4

from . import sources
from .checkpoints import Checkpoints
from .scoring import COVERAGE_POLICY
from .workflow import Conflict, automatic_draft

PIPELINE_VERSION = "bailian-nodes-v2"


def scoring_engine():
    return "local_demo" if os.environ.get("MMSA_V2_CONTENT_PROVIDER") == "local_demo" else PIPELINE_VERSION


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()


def media_checkpoint_key(media_sha256):
    from .auto_media import affect_method_fingerprint
    from .state_rules import METHOD
    return fingerprint({"media": media_sha256, "method": "v2-local-media-checkpoint.2",
                        "state_method": METHOD, "method_fingerprint": affect_method_fingerprint()})


class Superseded(Exception):
    pass


class LocalScoring:
    def __init__(self,store):
        self.store=store
        self.lock=Lock()
        self.checkpoints=Checkpoints(store)

    def queue(self,sid,body):
        from .auto_media import affect_method_fingerprint
        from .state_rules import METHOD
        def enqueue(s,bank):
            if s.get("interview_mode")!=sources.SHORT_MODE or s["status"] not in ("interview_finished","draft"):
                raise Conflict("请先完成八题短卷，再生成评分草稿。")
            job=s.get("auto_scoring") or {}
            if job.get("status") in ("queued","running"):
                return
            if s.get("review") or s.get("manual_scoring_required") or not automatic_draft(s.get("draft")):
                raise Conflict("已保存其他来源的评价，请在评分细则中复核。")
            if (job.get("status")=="done" and not job.get("issues") and job.get("engine")==scoring_engine()
                    and job.get("state_method")==METHOD and job.get("method_fingerprint")==affect_method_fingerprint()):
                return
            s["auto_scoring"]={"status":"queued","run_id":uuid4().hex}
        return self.store.change(sid,body,enqueue)

    def recover(self):
        with self.store.connection() as db:
            rows=db.execute("SELECT id FROM sessions WHERE json_extract(state,'$.auto_scoring.status') IN ('queued','running')").fetchall()
        for row in rows:
            self.run(row[0],recovering=True)

    def run(self,sid,recovering=False):
        with self.lock:
            claimed=False
            def claim(s,bank):
                nonlocal claimed
                job=s.get("auto_scoring") or {}
                if job.get("status")=="queued" or (recovering and job.get("status")=="running"):
                    if s.get("review") or s.get("manual_scoring_required") or not automatic_draft(s.get("draft")):
                        job["status"]="superseded"
                    else:
                        job.update(status="running",engine=scoring_engine(),issues=[])
                        job.setdefault("nodes",{})
                        s["scoring_policy"]=dict(COVERAGE_POLICY)
                        claimed=True
            state=self.store.change(sid,{},claim)
            if not claimed:
                return
            run_id=state["auto_scoring"]["run_id"]
            try:
                self._score(sid,state)
            except Superseded:
                return
            except Exception as exc:
                logging.getLogger(__name__).error("自动分析未完成 %s (%s)",sid,type(exc).__name__)
                def fail(s,bank):
                    job=s.get("auto_scoring") or {}
                    if job.get("run_id")==run_id and job.get("status")=="running":
                        job.update(status="error",issues=["分析未完成，已成功的结果保留；可从失败步骤重试。"])
                self.store.change(sid,{},fail)

    def _commit(self,sid,state,mutate):
        accepted=False
        def apply(current,bank):
            nonlocal accepted
            job=current.get("auto_scoring") or {}
            if job.get("run_id")!=state["auto_scoring"]["run_id"] or job.get("status")!="running":
                return
            if (current["version"]!=state["version"] or current.get("review") or
                    current.get("manual_scoring_required") or not automatic_draft(current.get("draft"))):
                job.update(status="superseded",issues=["资料或教师评价已改变，较新的记录已保留。已完成分析缓存可在相同证据下复用。"])
                return
            mutate(current,bank)
            accepted=True
        updated=self.store.change(sid,{},apply)
        if not accepted:
            raise Superseded()
        return updated

    @staticmethod
    def _safe_error(exc):
        from .cloud_content import ProviderError
        from .cloud_asr import CloudASRError
        from .auto_media import AffectAnalysisError
        return str(exc) if isinstance(exc,(ProviderError,CloudASRError,AffectAnalysisError)) else "该步骤未完成，请稍后重试；已成功结果保留。"

    @staticmethod
    def _merge_provenance(existing,node,metadata):
        old=copy.deepcopy(existing or {})
        nodes=old.setdefault("nodes",{})
        nodes[node]=copy.deepcopy(metadata)
        for key in ("provider","model","returned_model","request_id","prompt_version","validation"):
            if key in metadata:
                old[key]=metadata[key]
        old.update(pipeline_version=PIPELINE_VERSION,status=metadata.get("status","succeeded"))
        details={}
        for key,value in nodes.items():
            if key.startswith("question:"):
                for item in value.get("factor_details",[]):
                    details[item["factor_id"]]=item
        old["factor_details"]=list(details.values())
        if "experience_details" in metadata:
            old["experience_details"]=metadata["experience_details"]
        return old

    def _score(self,sid,state):
        from .auto_media import analyze_answer,estimate_from_cached_answer,AffectAnalysisError,affect_method_fingerprint
        from .cloud_asr import transcribe_answer,METHOD as ASR_METHOD
        from .cloud_content import evaluate_question,summarize_interview,question_input_key,summary_input_key
        from .state_rules import METHOD as STATE_METHOD
        with self.store.connection() as db:
            bank=json.loads(db.execute("SELECT bank FROM sessions WHERE id=?",(sid,)).fetchone()[0])
        issues=[]
        engine=scoring_engine()

        def commit(mutate):
            nonlocal state
            state=self._commit(sid,state,mutate)

        def node(name,key,operation):
            result=self.checkpoints.get(sid,name,key)
            cached=result is not None
            try:
                if not cached:
                    result=operation()
                    self.checkpoints.save(sid,name,key,result)
            except Exception as exc:
                error=self._safe_error(exc)
                trace=copy.deepcopy(getattr(exc,"provenance",{}))
                self.checkpoints.failed(sid,name,key,error,trace)
                commit(lambda s,b:s["auto_scoring"]["nodes"].__setitem__(name,{"status":"failed","input_key":key,"error":error,"provenance":trace}))
                issues.append(name+"："+error)
                return None
            commit(lambda s,b:s["auto_scoring"]["nodes"].__setitem__(name,{"status":"succeeded","input_key":key,"cached":cached}))
            return result

        def publish_answer(qid,changes):
            def apply(s,b):
                answer=s["answers"][qid]
                if changes.get("transcript") and changes["transcript"]!=answer.get("transcript"):
                    s["input_version"]+=1
                answer.update(copy.deepcopy(changes))
                if answer.get("text_source") in ("teacher_corrected_transcript", "candidate_corrected_text") and answer.get("processing"):
                    # Cached measurements remain usable, but old ASR words/timestamps do not.
                    answer["processing"]["transcript"] = (answer.get("text") if answer["text_source"] == "candidate_corrected_text" else answer.get("transcript")) or ""
                    answer["processing"]["transcript_segments"] = []
                audit=(answer.get("processing") or {}).get("v1") or {}
                audio=(audit.get("modules") or {}).get("audio") or {}
                summary=audio.get("summary")
                if isinstance(summary,dict) and answer.get("transcript"):
                    from interview_multimodal import speech_units
                    units=speech_units(answer["transcript"])
                    duration=audit.get("duration_ms")
                    summary.update(transcript_units=units,speech_rate_units_per_min=round(units*60000/duration,2)
                                   if units and duration and summary.get("speech_ms") else None)
                if s.get("draft"):
                    s["draft"]["input_version"]=s["input_version"]
            commit(apply)

        for q in state["plan"]:
            qid=q["id"]
            a=state["answers"].get(qid,{})
            if not a.get("media"):
                continue
            path=self.store.media_path(a["media"])
            try:
                with path.open("rb") as media:
                    if hashlib.file_digest(media,"sha256").hexdigest()!=a["media"]["sha256"]:
                        raise ValueError("原片指纹不符")
            except (OSError,ValueError):
                issues.append(qid+" 原片校验失败，未使用缓存或发送转写。")
                continue
            key=media_checkpoint_key(a["media"]["sha256"])
            def local_analysis():
                if a.get("processing_transcribe"):
                    try:
                        estimate=estimate_from_cached_answer(a)
                        return {"processing":a["processing"],"state_estimate":estimate,
                                "transcript":a.get("transcript") or a["processing"].get("transcript","")}
                    except ValueError:
                        pass
                try:
                    return analyze_answer(path,a)
                except AffectAnalysisError as exc:
                    # A failed new model must not leave an obsolete automatic score active.
                    # Completed native measurements and transcripts remain available for retry.
                    changes={"state_estimate":None,"state_estimate_error":{
                        "method":STATE_METHOD,"message":str(exc),"provenance":exc.provenance}}
                    if exc.processing is not None:
                        changes.update(processing=exc.processing,processing_transcribe=True)
                    if exc.transcript and not (a.get("text") or a.get("transcript")):
                        changes["transcript"]=exc.transcript
                    publish_answer(qid,changes)
                    raise
            result=node("media:"+qid,key,local_analysis)
            if result:
                changes={"processing":result["processing"],"processing_transcribe":True,"state_estimate_error":None}
                if result.get("state_estimate"):
                    changes["state_estimate"]=result["state_estimate"]
                if result.get("transcript") and not (a.get("text") or a.get("transcript")):
                    changes["transcript"]=result["transcript"]
                publish_answer(qid,changes)
            a=state["answers"][qid]
            if engine!="local_demo" and not (a.get("text") or a.get("transcript")):
                key=fingerprint({"media":a["media"]["sha256"],"method":ASR_METHOD})
                def asr():
                    prior=a.get("transcription_provenance") or {}
                    if prior.get("status")=="empty" and prior.get("method")==ASR_METHOD and prior.get("media_sha256")==a["media"]["sha256"]:
                        return {"transcript":"","provenance":prior}
                    return transcribe_answer(path,a)
                spoken=node("transcript:"+qid,key,asr)
                if spoken is not None:
                    changes={"transcription_provenance":spoken.get("provenance",{})}
                    if spoken.get("transcript","").strip():
                        changes["transcript"]=spoken["transcript"].strip()
                    publish_answer(qid,changes)

        def publish_draft(result,name):
            def apply(s,b):
                body=copy.deepcopy(result)
                body.pop("issues",None)
                body["input_version"]=s["input_version"]
                if body.get("model_provenance"):
                    body["model_provenance"]=self._merge_provenance((s.get("draft") or {}).get("model_provenance"),name,body["model_provenance"])
                self.store.apply_draft(s,body,automatic=True)
            commit(apply)

        if engine=="local_demo":
            from .auto_content import content_draft
            key=fingerprint({"bank":bank,"answers":{qid:{k:a.get(k) for k in ("text","transcript")} for qid,a in state["answers"].items()}})
            result=node("offline-content",key,lambda:content_draft(state,bank))
            if result:
                publish_draft(result,"offline-content")
        else:
            factor_results=[]
            for q in state["plan"]:
                if q.get("kind")!="text" or not q.get("factor_id"):
                    continue
                qid=q["id"]
                result=node("question:"+qid,question_input_key(state,bank,qid),lambda:evaluate_question(state,bank,qid))
                if result is not None:
                    factor_results.append(result)
                    issues.extend(result.get("issues",[]))
                    publish_draft(result,"question:"+qid)
            if len(factor_results)==sum(q.get("kind")=="text" and bool(q.get("factor_id")) for q in state["plan"]):
                result=node("summary",summary_input_key(state,bank,factor_results),lambda:summarize_interview(state,bank,factor_results))
                if result is not None:
                    issues.extend(result.get("issues",[]))
                    publish_draft(result,"summary")

        def finish(s,b):
            if s.get("operation_log") is not None:
                count=len(s["operation_log"]["errors"])
                self.store.apply_draft(s,{"source":"local_auto","author":"操作记录确定性计算","input_version":s["input_version"],
                    "operation_score":10*max(0,10-count),"operation_reason":f"评分!C8。每个不同操作错误扣1个原始分，本场 {count} 项；技术故障和正常修改不扣分。"},automatic=True)
            draft=s.get("draft") or {}
            for q in s["plan"]:
                if q.get("kind")!="text" or not q.get("factor_id"):
                    continue
                a=s["answers"].get(q["id"],{})
                entry=next((f for f in draft.get("factors",[]) if f["factor_id"]==q["factor_id"]),{})
                if not (a.get("text") or a.get("transcript")):
                    issues.append(q["id"]+" 缺少有效转写原话，不表示该因子能力低；可在老师页校正或申请原题重录。")
                elif entry.get("rating") is None or entry.get("verification_gaps"):
                    issues.append(q["id"]+" 问答评分证据待核实。")
            report=self.store.report_from(s,b)
            if not report["session"].get("meets_coverage_threshold"):
                issues.append("多模态每题及整场有效覆盖须至少70%，当前未达标，观测分不计入综合分与排名。")
            if draft.get("experience_score") is None:
                issues.append("基本情况缺少可计分的经历证据。")
            if report.get("total") is None and report.get("estimated_total") is None and not issues:
                issues.append("报告仍有分项缺口，请核对评分明细。")
            s["auto_scoring"].update(status="partial" if issues else "done",issues=list(dict.fromkeys(issues)),engine=engine,
                state_method=STATE_METHOD,method_fingerprint=affect_method_fingerprint(),
                content_rated=sum(f.get("rating") is not None and not f.get("verification_gaps") for f in draft.get("factors",[])))
        commit(finish)

