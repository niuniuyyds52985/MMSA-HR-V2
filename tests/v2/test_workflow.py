import os
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave

from fastapi.testclient import TestClient

from src.mmsa_v2.access import operator_key
from src.mmsa_v2.api import create_app
from src.mmsa_v2.workflow import ROOT


def sample_wav():
    with BytesIO() as buffer:
        with wave.open(buffer,"wb") as wav:
            wav.setparams((1,2,8000,0,"NONE","not compressed"))
            wav.writeframes(bytes(16000))
        return buffer.getvalue()


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        provider = patch.dict(os.environ, {"MMSA_V2_CONTENT_PROVIDER":"local_demo"})
        provider.start()
        self.addCleanup(provider.stop)
        temp=TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app=create_app(temp.name)
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)
        self.base="/v2/api"
        self.headers={"Authorization":"Bearer "+operator_key(Path(temp.name))}
        self.batch=self.post("/batches",{"name":"测试批次"})
        source=next((ROOT/"模拟简历").glob("*.docx"),None)
        if source is None:
            self.skipTest("需要本地模拟简历")
        url=self.base+"/batches/"+self.batch["id"]+"/resumes"
        upload={"file":(source.name,source.read_bytes())}
        first=self.client.post(url,files=upload,headers=self.headers)
        first.raise_for_status()
        second=self.client.post(url,files=upload,headers=self.headers)
        self.assertEqual(second.json()["stats"]["count"],1)
        cid=first.json()["candidates"][0]["id"]
        start_url="/batches/"+self.batch["id"]+"/sessions"
        # These cases exercise already-deployed full sessions and their conditional branches.
        self.session=self.post(start_url,{"candidate_id":cid,"interview_mode":"legacy_full"})
        self.assertEqual(self.post(start_url,{"candidate_id":cid})["id"],self.session["id"])
        self.url="/sessions/"+self.session["id"]
        self.c="/c/"+self.session["token"]

    def post(self,url,body):
        response=self.client.post(self.base+url,json=body,headers=self.headers)
        self.assertEqual(response.status_code,200,response.text[:700])
        return response.json()

    def test_end_to_end_evidence_review_and_history(self):
        plan=[q["id"] for q in self.session["plan"] if q["kind"]=="text"]
        s=self.post(self.url+"/plan",{"question_ids":plan,"reason":"保留经历介绍和八因子行为证据"})
        c=self.client.get(self.base+self.c).json()
        self.assertEqual(len(c["plan"]),9)
        self.assertNotIn("resume",c)
        self.assertNotIn("draft",c)
        self.assertEqual(self.client.get(self.base+self.url).status_code,401)
        for q in s["plan"]:
            text="在模拟项目中我记录了实际行动及结果。"
            response=self.client.post(self.base+self.c+"/answers/"+q["id"],json={"value":text})
            self.assertEqual(response.status_code,200,response.text[:400])
        finished=self.post(self.c+"/finish",{})
        self.assertEqual(finished["status"],"interview_finished")
        evidence=self.client.get(self.base+self.url+"/evidence?factor_id=F1",headers=self.headers).json()
        self.assertEqual(list(evidence["answers"]),["F1-G"])
        ver=evidence["input_version"]
        factor={"factor_id":"F1","rating":3,"reason":"有模拟项目行动记录",
                "evidence":[{"question_id":"F1-G","quote":"记录了实际行动"}]}
        bad={**factor,"evidence":[{"question_id":"F1-G","quote":"不存在的原话"}]}
        self.assertEqual(self.client.post(self.base+self.url+"/draft",json={"input_version":ver,"factors":[bad]},headers=self.headers).status_code,422)
        self.assertEqual(self.client.post(self.base+self.url+"/draft",json={"input_version":ver-1,"factors":[]},headers=self.headers).status_code,409)
        d=self.post(self.url+"/draft",{"input_version":ver,"factors":[factor]})
        self.assertEqual(self.post(self.url+"/draft",{"input_version":ver,"factors":[factor]})["version"],d["version"])
        rest=[{"factor_id":f"F{i}","rating":None,"reason":"作答证据仍不足","evidence":[]} for i in range(2,9)]
        d=self.post(self.url+"/draft",{"input_version":ver,"factors":rest,"summary":"已整理模拟回答。",
                    "next_steps":["核实角色与个人贡献"],"experience_score":30,"operation_score":100,
                    "operation_reason":"教师逐项复查模拟场流程记录"})
        teacher=self.post(self.url+"/teacher-assessment",{"input_version":ver,"reviewer":"模拟老师",
                          "factors":[{**factor,"rating":2}]})
        rejected=self.client.post(self.base+self.url+"/review",json={"version":teacher["version"],
                                  "reviewer":"模拟老师","decision":"approve"},headers=self.headers)
        self.assertEqual(rejected.status_code,422)
        rev={"version":teacher["version"],"reviewer":"模拟老师","decision":"approve","notes":"保留独立档次意见及七项缺口"}
        final=self.post(self.url+"/review",rev)
        self.assertEqual(final["status"],"reviewed")
        self.assertEqual(self.post(self.url+"/review",rev)["version"],final["version"])
        r=self.client.get(self.base+self.url+"/report",headers=self.headers).json()
        self.assertEqual(r["factors"][0]["rating"],3)
        self.assertIsNone(r["total"])
        older=self.client.get(self.base+self.url+"/report",params={"version":d["version"]},headers=self.headers).json()
        self.assertIsNone(older["review"])
        batch=self.client.get(self.base+"/batches/"+self.batch["id"],headers=self.headers).json()
        self.assertEqual(batch["stats"]["reviewed_count"],1)

    def test_conditional_questions_and_reopening(self):
        with self.app.state.workflow.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0],1)
        response=self.client.post(self.base+self.c+"/finish",json={"allow_incomplete":True})
        self.assertEqual(response.status_code,422)
        s=self.post(self.url+"/finish",{"allow_incomplete":True})
        opened=self.post(self.url+"/clarifications",{"input_version":s["input_version"],"source_question_id":"F1-G",
                         "question_id":"F1-verify-action","reason":"需补充本人行动"})
        self.assertEqual(opened["status"],"awaiting_verification")
        response=self.client.post(self.base+self.c+"/answers/F1-G",json={"value":"不能改写此前作答"})
        self.assertEqual(response.status_code,409)
        self.post(self.c+"/answers/F1-verify-action",{"value":"补充实际行动"})
        finished=self.post(self.c+"/finish",{})
        self.assertEqual(finished["status"],"interview_finished")

    def test_candidate_recording_and_text_reach_teacher_together(self):
        recording=sample_wav()
        url=self.base+self.c+"/answers/EXPERIENCE/media"
        progress_url=self.base+self.url+"/progress"
        def progress():
            response=self.client.get(progress_url,headers=self.headers)
            response.raise_for_status()
            return response.json()
        def upload(content,text,version):
            return self.client.post(url,files={"file":("回答.wav",content,"audio/wav")},
                                    data={"text":text,"version":version})
        version=self.session["version"]
        initial={"version":version,"status":"interview_open","answered":0,"total":len(self.session["plan"])}
        self.assertEqual(self.client.get(progress_url).status_code,401)
        self.assertEqual(progress(),initial)
        self.assertEqual(upload(b"invalid","我的回答",version).status_code,422)
        self.assertEqual(upload(recording,"我的回答",version+1).status_code,409)
        self.assertEqual(progress(),initial)
        self.assertEqual(list((self.app.state.workflow.root/"media").iterdir()),[])

        saved=upload(recording,"我的回答",version)
        self.assertEqual(saved.status_code,200,saved.text[:400])
        answer=saved.json()["answers"]["EXPERIENCE"]
        self.assertEqual(answer["text"],"我的回答")
        self.assertEqual(self.client.get(answer["media"]["url"]).content,recording)
        self.assertEqual(progress(),dict(initial,version=version+1,answered=1))
        retry=upload(recording,"我的回答",version)
        self.assertEqual(retry.json()["version"],version+1)
        self.assertEqual(upload(recording,"过期更新",version).status_code,409)
        teacher=self.client.get(self.base+self.url,headers=self.headers).json()
        self.assertEqual(teacher["answers"]["EXPERIENCE"]["text"],"我的回答")
        self.assertEqual(teacher["answers"]["EXPERIENCE"]["media"]["sha256"],answer["media"]["sha256"])
        self.assertEqual(teacher["input_version"],self.session["input_version"]+1)

    def test_clear_text_only_when_a_recorded_answer_remains(self):
        url=self.base+self.c+"/answers/EXPERIENCE"
        media={"file":("回答.wav",sample_wav(),"audio/wav")}
        self.post(self.c+"/answers/EXPERIENCE",{"value":"先写文字"})
        self.assertEqual(self.client.post(url,json={"value":""}).status_code,422)
        def upload(url,data):
            response=self.client.post(url+"/media",files=media,data=data)
            self.assertEqual(response.status_code,200,response.text[:400])
            return response.json()
        kept=upload(url,{})
        self.assertEqual(kept["answers"]["EXPERIENCE"]["text"],"先写文字")
        cleared=upload(url,{"text":"","version":kept["version"]})
        a=cleared["answers"]["EXPERIENCE"]
        self.assertFalse(a.get("text") or a.get("value"))
        self.assertEqual(a["media"],kept["answers"]["EXPERIENCE"]["media"])
        self.assertEqual(cleared["plan"],kept["plan"])
        updated=self.post(self.c+"/answers/EXPERIENCE",{"value":"修改笔记"})
        for _ in range(2):
            cleared=self.post(self.c+"/answers/EXPERIENCE",{"value":"","version":updated["version"]})
            self.assertEqual(cleared["version"],updated["version"]+1)
            self.assertEqual(cleared["answers"]["EXPERIENCE"],a)
        progress=self.client.get(self.base+self.url+"/progress",headers=self.headers).json()
        self.assertEqual(progress["answered"],1)
        choice=self.base+self.c+"/answers/F1-E"
        self.assertEqual(self.client.post(choice+"/media",files=media,data={"text":""}).status_code,422)
        upload(choice,{})
        self.assertEqual(self.client.post(choice,json={"value":""}).status_code,422)
        self.assertEqual(self.client.get(self.base+self.url+"/progress",headers=self.headers).json()["answered"],1)

    def test_progress_tracks_conditional_and_verification_questions(self):
        url=self.base+self.url+"/progress"
        def progress():
            return self.client.get(url,headers=self.headers).json()
        question=next(q for q in self.session["plan"] if q["id"]=="F1-E")
        total=len(self.session["plan"])
        self.post(self.c+"/answers/F1-E",{"value":question["options"][2]["id"]})
        self.post(self.c+"/answers/F1-E-followup",{"value":"模拟补充"})
        self.assertEqual((progress()["answered"],progress()["total"]),(2,total+1))
        self.post(self.c+"/answers/F1-E",{"value":question["options"][0]["id"]})
        self.assertEqual((progress()["answered"],progress()["total"]),(1,total))
        ended=self.post(self.url+"/finish",{"allow_incomplete":True})
        self.assertEqual(progress()["status"],"interview_finished")
        self.post(self.url+"/clarifications",{"input_version":ended["input_version"],"source_question_id":"F1-G",
                  "question_id":"F1-verify-action","reason":"核实模拟行动"})
        self.assertEqual(progress()["status"],"awaiting_verification")
        self.post(self.c+"/answers/F1-verify-action",{"value":"补充模拟行动"})
        self.post(self.c+"/finish",{})
        p=progress()
        self.assertEqual((p["answered"],p["total"],p["status"]),(2,total+1,"interview_finished"))

    def test_resume_correction_preserves_frozen_answered_session(self):
        url=self.base+"/batches/"+self.batch["id"]+"/candidates/"+self.session["candidate_id"]+"/resume"
        original=self.session["resume"]
        payload={"version":original.get("resume_version",1),"reviewer":"模拟老师","reason":"对照原文",
                 "changes":{"school":"模拟院校甲"}}
        put=self.client.put(url,json=payload,headers=self.headers)
        self.assertEqual(put.status_code,200,put.text[:400])
        changed=self.client.get(self.base+self.url,headers=self.headers).json()
        self.assertEqual(changed["resume"]["school"],"模拟院校甲")
        self.assertEqual(changed["input_version"],2)
        self.assertEqual(self.client.put(url,json=payload,headers=self.headers).status_code,200)
        self.post(self.c+"/answers/EXPERIENCE",{"value":"介绍模拟经历"})
        payload.update(version=2,changes={"school":"模拟院校乙"})
        r=self.client.put(url,json=payload,headers=self.headers)
        self.assertEqual(r.json()["candidates"][0]["resume"]["school"],"模拟院校乙")
        self.assertEqual(self.client.get(self.base+self.url,headers=self.headers).json()["resume"]["school"],"模拟院校甲")

    def test_high_rating_needs_three_verifiable_citations(self):
        text="我先协调会场，投入一晚修改方案。上学期组织班会时也实际做过一次。"
        self.post(self.c+"/answers/F1-G",{"value":text})
        s=self.post(self.url+"/finish",{"allow_incomplete":True})
        payload={"factor_id":"F1","rating":5,"reason":"模拟高分，待核实三个维度",
                 "evidence":[{"question_id":"F1-G","quote":"协调会场"}]}
        body={"input_version":s["input_version"],"summary":"演示分值核实","factors":[payload]+[
              {"factor_id":f"F{i}","rating":None,"reason":"无可用回答","evidence":[]} for i in range(2,9)]}
        d=self.post(self.url+"/draft",body)
        self.assertEqual(d["draft"]["factors"][0]["verification_gaps"],["action","cost","experience"])
        res=self.client.post(self.base+self.url+"/review",headers=self.headers,
                             json={"version":d["version"],"decision":"approve","reviewer":"测试老师","notes":"复核"})
        self.assertEqual(res.status_code,422)
        payload["verification"]={k:{"question_id":"F1-G","quote":v} for k,v in
                                  (("action","协调会场"),("cost","投入一晚"),("experience","组织班会"))}
        d=self.post(self.url+"/draft",body)
        self.assertEqual(d["draft"]["factors"][0]["verification_gaps"],[])

    def test_local_draft_provenance_and_offline_exports(self):
        finished=self.post(self.url+"/finish",{"allow_incomplete":True})
        version=finished["input_version"]
        first={"factor_id":"F1","rating":None,"reason":"历史原文缺失","evidence":[]}
        self.post(self.url+"/draft",{"input_version":version,"factors":[first]})
        second={"factor_id":"F2","rating":None,"reason":"本地核查后待补充","evidence":[]}
        d=self.post(self.url+"/local-draft",{"input_version":version,"source":"harness","author":"本机验收人",
                    "factors":[second],"summary":"本机复查意见"})
        self.assertEqual(d["draft"]["source"],"mixed")
        self.assertEqual(d["draft"]["factors"][0]["source"],"unrecorded")
        self.assertEqual(d["draft"]["factors"][1]["source"],"local_review")
        self.assertEqual(d["draft"]["provenance"]["summary"]["author"],"本机验收人")
        personal=self.post(self.url+"/export",{"version":d["version"]})
        file=Path(personal["file"])
        self.assertEqual(file.parent,self.app.state.workflow.root/"exports")
        self.assertEqual(file.stat().st_size,personal["bytes"])
        self.assertIn("本机复查意见",file.read_text(encoding="utf-8"))
        batch=self.post("/batches/"+self.batch["id"]+"/export",{})
        self.assertIn(file.name,Path(batch["file"]).read_text(encoding="utf-8"))

if __name__=="__main__":
    unittest.main()
