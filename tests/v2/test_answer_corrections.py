"""Signed source corrections retain immutable history and unaffected work."""
from pathlib import Path
from tempfile import TemporaryDirectory
import copy
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from src.mmsa_v2.access import operator_key
from src.mmsa_v2.api import create_app
from src.mmsa_v2.workflow import Workflow
from tests.v2.test_auto_submission import simulation_docx, wav_fixture


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        directory=TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.client=TestClient(create_app(directory.name))
        self.addCleanup(self.client.close)
        self.store=self.client.app.state.workflow
        self.headers={"Authorization":"Bearer "+operator_key(Path(directory.name))}
        worker=patch("src.mmsa_v2.automatic.LocalScoring.run")
        self.worker=worker.start();self.addCleanup(worker.stop)
        batch=self.store.new_batch("原题补正测试")
        person=self.store.import_files(batch["id"],[("模拟学生.docx",simulation_docx())])["candidates"][0]
        state=self.store.create_session(batch["id"],person["id"])
        self.sid=state["id"]
        self.student="/v2/api/c/"+state["token"]
        self.teacher="/v2/api/sessions/"+self.sid
        for q in state["plan"]:
            state=self.store.answer(self.sid,q["id"],{"value":q["options"][0]["id"]})
        self.orals=[q for q in state["plan"] if q["kind"]=="text"]
        self.qid=self.orals[0]["id"]
        for q in self.orals:
            self.store.answer(self.sid,q["id"],{"value":"在模拟项目中，我组织讨论，调整分工并完成了交付。"})
        response=self.client.post(self.student+"/answers/"+self.qid+"/media",files={"file":("trial.wav",wav_fixture(),"audio/wav")})
        response.raise_for_status()
        self.post(self.student+"/finish",{})
        self.seed_draft()

    def post(self,path,body,*,teacher=False,expected=200):
        response=self.client.post(path,json=body,headers=self.headers if teacher else {})
        self.assertEqual(response.status_code,expected,response.text[:250])
        return response.json()

    def state(self):
        return self.store.session(self.sid)

    def seed_draft(self,manual=False):
        def seed(s,bank):
            factors=[{"factor_id":q["factor_id"],"rating":3,"reason":"老师核实后支持中档。" if manual else "测试原话支持中档。",
                "evidence":[{"question_id":q["id"],"quote":s["answers"][q["id"]]["text"]}]} for q in self.orals]
            draft={"input_version":s["input_version"],"source":"local_review" if manual else "local_auto",
                   "author":"测试老师" if manual else "测试自动草稿","factors":factors,
                   "experience_score":40,"experience_reason":"测试经历理由。",
                   "experience_evidence":factors[0]["evidence"][0],"operation_score":100,
                   "operation_reason":"操作记录测试。","summary":"原有综合意见。","next_steps":["原有建议。"]}
            self.store.apply_draft(s,draft,automatic=not manual)
            s["auto_scoring"]={"status":"done","engine":"fixture"}
        self.store.change(self.sid,{},seed)

    def body(self,qid=None):
        s=self.state();qid=qid or self.qid
        return {"version":s["version"],"input_version":s["input_version"],
            "media_sha256":(s["answers"][qid].get("media") or {}).get("sha256"),
            "reviewer":"核对老师","reason":"回听后修正识别错误。"}

    def test_corrected_transcript_has_one_source_and_preserves_other_factors(self):
        before=self.state();body=self.body();body["transcript"]="我实际是先协调两位同学，再调整各自任务。"
        result=self.post(self.teacher+"/answers/"+self.qid+"/correct-transcript",body,teacher=True)
        answer=result["answers"][self.qid]
        self.assertEqual(answer["transcript"],body["transcript"])
        for old in ("value","text","answer_label"):
            self.assertNotIn(old,answer)
        self.assertEqual(answer["transcription_provenance"]["provider"],"human_review")
        self.assertEqual(result["auto_scoring"]["status"],"queued")
        self.assertEqual(result["input_version"],before["input_version"]+1)
        retained=[f for f in before["draft"]["factors"] if f["factor_id"]!=self.orals[0]["factor_id"]]
        self.assertEqual(result["draft"]["factors"],retained)
        self.assertNotIn("experience_score",result["draft"])
        self.assertNotIn("summary",result["draft"])
        self.assertEqual(self.store.session(self.sid,before["version"])["answers"],before["answers"])
        self.assertEqual(result["input_corrections"][-1]["before_version"],before["version"])

    def test_corrections_require_operator_version_media_and_signature(self):
        body=self.body();body["transcript"]="核对后的真实原话。"
        url=self.teacher+"/answers/"+self.qid+"/correct-transcript"
        self.post(url,body,expected=401)
        for field,value,code in (("version",0,409),("input_version",0,409),("media_sha256","wrong",409),("reviewer","",422)):
            invalid={**body,field:value};self.post(url,invalid,teacher=True,expected=code)
        choice=self.state()["plan"][0]["id"]
        self.post(self.teacher+"/answers/"+choice+"/reopen-answer",self.body(choice),teacher=True,expected=422)

    def test_reopen_scopes_original_question_and_requires_new_answer(self):
        before=self.state()
        reopened=self.post(self.teacher+"/answers/"+self.qid+"/reopen-answer",self.body(),teacher=True)
        self.assertEqual(reopened["plan"],before["plan"])
        self.assertEqual(reopened["open_verifications"],[self.qid])
        self.post(self.student+"/finish",{"version":reopened["version"]},expected=422)
        other=self.orals[1]["id"]
        self.post(self.student+"/answers/"+other,{"value":"不能改其他题。","version":reopened["version"]},expected=409)
        self.post(self.student+"/answers/"+self.qid,{"value":"新原话。","version":before["version"]},expected=409)
        self.post(self.student+"/answers/"+self.qid,{"value":before["answers"][self.qid]["text"],"version":reopened["version"]},expected=422)
        updated=self.post(self.student+"/answers/"+self.qid,{"value":"我重新说明这段经历的本人行动和结果。","version":reopened["version"]})
        self.assertEqual(updated["correction_request"]["completed_question_ids"],[self.qid])
        final=self.post(self.student+"/finish",{"version":updated["version"]})
        self.assertEqual(final["status"],"interview_finished")
        self.assertEqual(final["auto_scoring"],"queued")
        self.assertEqual(len(final["plan"]),8)
        self.assertNotIn("correction_request",final)

    def test_manual_grades_hold_automatic_rescoring_and_review_is_archived(self):
        self.seed_draft(manual=True)
        self.store.change(self.sid,{},lambda s,b:s.update(status="reviewed",review={"reviewer":"老师","decision":"approve"}))
        body=self.body();body["transcript"]="老师重新听到的原话。"
        result=self.post(self.teacher+"/answers/"+self.qid+"/correct-transcript",body,teacher=True)
        self.assertTrue(result["manual_scoring_required"])
        self.assertEqual(result["auto_scoring"]["status"],"manual_review_required")
        self.assertEqual(len(result["draft"]["factors"]),2)
        self.assertTrue(all(f["source"]=="local_review" for f in result["draft"]["factors"]))
        self.assertIsNone(result["review"])
        self.assertEqual(result["review_history"][-1]["reviewer"],"老师")

    def test_rerecorded_media_clears_old_cloud_and_human_transcripts(self):
        self.store.change(self.sid,{},lambda s,b:s["answers"][self.qid].update(transcript="旧转写",transcription_provenance={"status":"empty"}))
        s=self.post(self.teacher+"/answers/"+self.qid+"/reopen-answer",self.body(),teacher=True)
        data=bytearray(wav_fixture());data[-2:]=b'\x01\x00'
        response=self.client.post(self.student+"/answers/"+self.qid+"/media",files={"file":("new.wav",bytes(data),"audio/wav")},data={"version":s["version"],"text":""})
        self.assertEqual(response.status_code,200,response.text[:200])
        a=self.state()["answers"][self.qid]
        for key in ("transcript","transcription_provenance","text"):
            self.assertNotIn(key,a)

    def test_approved_automatic_draft_requires_human_review_after_correction(self):
        self.store.change(self.sid,{},lambda s,b:s.update(status="reviewed",review={
            "reviewer":"复核老师","decision":"approve","draft_input_version":s["input_version"]}))
        before=self.state()
        self.assertTrue(all(f["source"]=="local_auto" for f in before["draft"]["factors"]))
        body={**self.body(),"transcript":"经回听后核实的新原话。"}
        result=self.post(self.teacher+"/answers/"+self.qid+"/correct-transcript",body,teacher=True)
        self.assertTrue(result["manual_scoring_required"])
        self.assertEqual(result["auto_scoring"]["status"],"manual_review_required")
        self.assertIsNone(result["review"])
        self.assertEqual(result["review_history"][-1]["reviewer"],"复核老师")
        self.assertEqual(self.store.session(self.sid,before["version"])["review"],before["review"])
        self.post(self.teacher+"/auto-score",{"version":result["version"]},teacher=True,expected=409)

    def test_independent_assessment_requires_human_review_after_rerecording(self):
        def assessed(s,bank):
            s["teacher_assessment"]={"reviewer":"独立老师","input_version":s["input_version"],
                "factors":copy.deepcopy(s["draft"]["factors"])}
        self.store.change(self.sid,{},assessed)
        before=self.state()
        opened=self.post(self.teacher+"/answers/"+self.qid+"/reopen-answer",self.body(),teacher=True)
        saved=self.post(self.student+"/answers/"+self.qid,{"version":opened["version"],"value":"重新叙述本人行动和结果。"})
        self.post(self.student+"/finish",{"version":saved["version"]})
        result=self.state()
        self.assertTrue(result["manual_scoring_required"])
        self.assertEqual(result["auto_scoring"]["status"],"manual_review_required")
        self.assertEqual(len(result["teacher_assessment"]["factors"]),2)
        old=result["teacher_assessment_history"][-1]
        self.assertEqual(old["reviewer"],"独立老师")
        self.assertEqual(old["factors"],before["teacher_assessment"]["factors"])
        self.post(self.teacher+"/auto-score",{"version":result["version"]},teacher=True,expected=409)

    def test_preflight_is_temporary_and_not_an_answer(self):
        s=self.post(self.teacher+"/answers/"+self.qid+"/reopen-answer",self.body(),teacher=True)
        before=copy.deepcopy(self.state());paths=[]
        def inspect(path):
            paths.append(path);self.assertTrue(path.exists())
            return {"status":"pass","local_only":True,"network_used":False,"transcript":"试录人声"}
        with patch("src.mmsa_v2.preflight.inspect_trial",side_effect=inspect):
            response=self.client.post(self.student+"/preflight",files={"file":("trial.wav",wav_fixture(),"audio/wav")})
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(all(not path.exists() for path in paths))
        self.assertEqual(self.state(),before)

    def seed_coverage(self,seconds):
        """Complete non-media scores and only vary observed media duration."""
        def seed(s,bank):
            media=copy.deepcopy(s["answers"][self.qid]["media"])
            for q,valid in zip(self.orals,seconds):
                answer=s["answers"][q["id"]]
                answer["media"]={**media,"duration_seconds":10}
                answer["state_estimate"]={"duration_seconds":10,"source":"local_estimate",
                    "segments":[{"start":0,"end":valid,"state":"饱满"}]}
        return self.store.change(self.sid,{},seed)

    def test_under_70_percent_coverage_does_not_enter_cohort_ranking(self):
        for seconds in ((6.9,6.9,6.9),(6.9,10,10)):
            with self.subTest(valid_seconds=seconds):
                state=self.seed_coverage(seconds)
                report=self.client.get(self.teacher+"/report",headers=self.headers).json()
                self.assertEqual(report["coverage_policy"]["min_question_coverage"],0.7)
                self.assertFalse(report["session"]["meets_coverage_threshold"])
                self.assertIn(self.qid,report["session"]["insufficient_question_ids"])
                self.assertEqual(report["components"]["multimodal"]["score"],75)
                self.assertIsNone(report["components"]["multimodal"]["contribution"])
                self.assertNotIn("estimated_contribution",report["components"]["multimodal"])
                self.assertIsNone(report["total"])
                self.assertIsNone(report["estimated_total"])
                cohort=self.client.get("/v2/api/batches/"+state["batch_id"],headers=self.headers).json()
                self.assertEqual(cohort["stats"]["ranked_count"],0)
                self.assertIsNone(cohort["candidates"][0]["rank"])
                self.assertEqual(cohort["candidates"][0]["score_kind"],"partial")
        state=self.seed_coverage((7,7,7))
        report=self.store.report(self.sid)
        self.assertTrue(report["session"]["meets_coverage_threshold"])
        self.assertIsNotNone(report["estimated_total"])
        cohort=self.store.batch(state["batch_id"])
        self.assertEqual(cohort["stats"]["ranked_count"],1)
        self.assertEqual(cohort["candidates"][0]["rank"],1)

    def test_legacy_coverage_revision_stays_readable_after_new_policy(self):
        self.seed_coverage((1,1,1))
        old=self.store.change(self.sid,{},lambda s,b:s.pop("scoring_policy"))
        legacy=self.store.report(self.sid,old["version"])
        self.assertEqual(legacy["coverage_policy"]["version"],"legacy-any-valid")
        self.assertIsNotNone(legacy["estimated_total"])
        from src.mmsa_v2.scoring import COVERAGE_POLICY
        self.store.change(self.sid,{},lambda s,b:s.update(scoring_policy=dict(COVERAGE_POLICY)))
        self.assertIsNone(self.store.report(self.sid)["estimated_total"])
        archived=self.store.report(self.sid,old["version"])
        self.assertEqual(archived["estimated_total"],legacy["estimated_total"])
        self.assertEqual(archived["coverage_policy"],legacy["coverage_policy"])

    def test_startup_migrates_current_coverage_once_and_retains_old_report(self):
        self.seed_coverage((1,1,1))
        old=self.store.change(self.sid,{},lambda s,b:s.pop("scoring_policy"))
        legacy=self.store.report(self.sid,old["version"])
        self.assertIsNotNone(legacy["estimated_total"])
        self.assertEqual(self.store.batch(old["batch_id"])["stats"]["ranked_count"],1)
        rebuilt=Workflow(self.store.root)
        current=rebuilt.session(self.sid)
        self.assertEqual(current["version"],old["version"]+1)
        self.assertEqual(current["input_version"],old["input_version"])
        self.assertEqual(current["answers"],old["answers"])
        self.assertEqual(current["draft"],old["draft"])
        self.assertEqual(current["scoring_policy"]["version"],"v2.coverage.70.1")
        self.assertIsNone(rebuilt.report(self.sid)["estimated_total"])
        self.assertIsNone(rebuilt.report(self.sid,current["version"])["estimated_total"])
        self.assertEqual(rebuilt.batch(old["batch_id"])["stats"]["ranked_count"],0)
        self.assertEqual(rebuilt.session(self.sid,old["version"]),old)
        self.assertEqual(rebuilt.report(self.sid,old["version"]),legacy)
        with rebuilt.connection() as db:
            count=db.execute("SELECT count(*) FROM revisions WHERE session_id=?",(self.sid,)).fetchone()[0]
        again=Workflow(self.store.root)
        self.assertEqual(again.session(self.sid),current)
        with again.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM revisions WHERE session_id=?",(self.sid,)).fetchone()[0],count)


if __name__=="__main__":
    unittest.main()
