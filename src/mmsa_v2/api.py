"""Local API for the V2 management-trainee simulation workbench."""
from hashlib import sha256
import asyncio
import json
from contextlib import asynccontextmanager
import os
from pathlib import Path
import tempfile
from urllib.parse import quote

from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .access import install_access
from .automatic import LocalScoring, scoring_engine
from .sources import SHORT_MODE, load_bank
from .workflow import Conflict, ROOT, Workflow, editable, has_answer, question_by_id

PREFIX = "/v2/api"
WEB = ROOT / "web/v2"
FORMATS = {".webm":"video/webm",".mp4":"video/mp4",".mov":"video/quicktime",".wav":"audio/wav",
           ".mp3":"audio/mpeg",".ogg":"audio/ogg",".m4a":"audio/mp4",".flac":"audio/flac"}


def demo_resume_files():
    return sorted(p for p in (ROOT / "模拟简历").glob("*.docx")
                  if p.is_file() and not p.name.startswith("~$"))


def public_session(state):
    projection={k:state.get(k) for k in ("id","candidate","status","version","plan","input_version","open_verifications")}
    projection["content_provider"] = scoring_engine()
    if state.get("correction_request"):
        projection["correction_request"]={k:state["correction_request"][k] for k in
            ("question_ids","completed_question_ids","reason")}
    if state.get("auto_scoring"):
        projection["auto_scoring"]=state["auto_scoring"]["status"]
    projection.update({k:state[k] for k in ("interview_mode","expected_questions") if k in state})
    keys=("id","prompt","factor_id","factor_name","kind","options","fields","parent_id","condition_parent",
          "stage","resume_hint","answer_hint")
    projection["plan"]=[{k:q[k] for k in keys if k in q} for q in state["plan"]]
    projection["answers"]={}
    for qid,answer in state["answers"].items():
        item={k:answer[k] for k in ("value","text") if k in answer}
        if answer.get("media"):
            media=answer["media"]
            item["media"]={k:media[k] for k in ("name","mime","duration_seconds","sha256") if k in media}
            item["media"]["url"]=f"{PREFIX}/c/{state['token']}/answers/{quote(qid,safe='')}/media?sha256={media['sha256']}"
        projection["answers"][qid]=item
    introduction=state.get("introduction")
    if introduction:
        item={k:introduction[k] for k in ("text","status") if k in introduction}
        media=introduction.get("media")
        if media:
            item["media"]={k:media[k] for k in ("name","mime","duration_seconds","sha256") if k in media}
            item["media"]["url"]=f"{PREFIX}/c/{state['token']}/introduction/media?sha256={media['sha256']}"
        projection["introduction"]=item
    return projection


def create_app(data_dir=None):
    store=Workflow(data_dir)
    scoring=LocalScoring(store)

    @asynccontextmanager
    async def lifespan(app):
        recovery=asyncio.create_task(asyncio.to_thread(scoring.recover))
        try:
            yield
        finally:
            await recovery

    app=FastAPI(title="真才实鉴 V2",version="2.0",docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.workflow=store
    install_access(app,store.root)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail":str(exc.args[0])},404)

    @app.exception_handler(Conflict)
    async def stale(request, exc):
        return JSONResponse({"detail":str(exc)},409)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"detail":str(exc)},422)

    @app.get("/")
    def home():
        return RedirectResponse("/v2/")

    @app.get("/v2/")
    @app.get("/v2/c/{token}")
    def index():
        return FileResponse(WEB/"index.html")

    @app.get("/v2/app.js")
    def script():
        return FileResponse(WEB/"app.js",media_type="text/javascript")

    @app.get("/v2/style.css")
    def style():
        return FileResponse(WEB/"style.css",media_type="text/css")

    app.mount("/v2/ui",StaticFiles(directory=WEB,check_dir=False),name="v2_ui")

    @app.get(PREFIX+"/status")
    def status():
        bank=load_bank()
        return {"service":"真才实鉴 V2","status":"ok","purpose":"simulation","version":"2.0",
                "bank_version":bank["version"],"factors":len(bank["factors"]),"paid_cloud_tools_exposed":False,
                "demo_resume_count":len(demo_resume_files()),
                "content_provider":scoring_engine(), "automatic_cloud_scoring":scoring_engine() != "local_demo"}

    @app.get(PREFIX+"/batches")
    def batches():
        return {"batches":store.batches()}

    @app.post(PREFIX+"/batches")
    def new_batch(body:dict=Body(...)):
        return store.new_batch(body.get("name"))

    @app.get(PREFIX+"/batches/{bid}")
    def batch(bid:str):
        return store.batch(bid)

    @app.post(PREFIX+"/batches/{bid}/resumes")
    def resume_file(bid:str,file:UploadFile=File(...)):
        name=Path(file.filename or "").name
        if not name.lower().endswith(".docx"):
            raise ValueError("请上传 DOCX 简历")
        raw=file.file.read(16*1024*1024+1)
        if len(raw)>16*1024*1024:
            raise ValueError("简历文件超过16 MB")
        return store.import_files(bid,[(name,raw)])

    @app.post(PREFIX+"/batches/{bid}/import-demo")
    def import_demo(bid:str):
        files=demo_resume_files()
        if not files:
            raise KeyError("本机没有模拟简历，请选择自己的 DOCX 简历导入")
        return store.import_files(bid,((p.name,p.read_bytes()) for p in files))

    @app.post(PREFIX+"/batches/{bid}/sessions")
    def start(bid:str,body:dict=Body(...)):
        return store.create_session(bid,body.get("candidate_id"),body.get("interview_mode",SHORT_MODE))

    @app.put(PREFIX+"/batches/{bid}/candidates/{cid}/resume")
    def correct_resume(bid:str,cid:str,body:dict=Body(...)):
        return store.correct_resume(bid,cid,body)

    @app.get(PREFIX+"/batches/{bid}/candidates/{cid}/resume.docx")
    def original_resume(bid:str,cid:str):
        with store.connection() as db:
            row=db.execute("SELECT digest FROM candidates WHERE id=? AND batch_id=?",(cid,bid)).fetchone()
        if not row:
            raise KeyError("简历不存在")
        path=store.root/"resumes"/(row["digest"]+".docx")
        if not path.is_file():
            raise KeyError("缺少原件，可重新导入相同简历以补齐")
        return FileResponse(path,filename="简历原件.docx",
                            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    @app.get(PREFIX+"/sessions/{sid}")
    def session(sid:str,version:int|None=None):
        return store.session(sid,version)

    @app.get(PREFIX+"/sessions/{sid}/workbench")
    def teacher_workbench(sid:str):
        state = store.session(sid)
        # Pin the embedded document and the review controls to the same revision.
        url = f"{PREFIX}/sessions/{quote(sid,safe='')}/report.html?version={state['version']}&workbench=true"
        embed = f'<iframe title="个人面试评价报告" src="{url.replace("&","&amp;")}"></iframe>'
        return {"session": state, "report_url": url, "report_html": embed}

    @app.get(PREFIX+"/sessions/{sid}/progress")
    def progress(sid:str):
        state=store.session(sid)
        answered=sum(has_answer(q,state["answers"].get(q["id"],{})) for q in state["plan"])
        result={"version":state["version"],"status":state["status"],"answered":answered,
                "total":state.get("expected_questions",len(state["plan"]))}
        if state.get("auto_scoring"):
            result["auto_scoring"]=state["auto_scoring"]["status"]
        return result

    @app.post(PREFIX+"/sessions/{sid}/short-interview")
    def short_interview(sid:str,body:dict=Body(...)):
        return store.short_interview(sid,body)

    @app.post(PREFIX+"/sessions/{sid}/plan")
    def plan(sid:str,body:dict=Body(...)):
        return store.plan(sid,body)

    @app.post(PREFIX+"/sessions/{sid}/answers/{qid}")
    def answer(sid:str,qid:str,body:dict=Body(...)):
        return store.answer(sid,qid,body)

    def save_media(sid,qid,file,text,version,*,introduction=False):
        from .media import media_info
        state=store.session(sid)
        editable(state)
        if introduction:
            if state["status"] != "interview_open":
                raise Conflict("本场自我介绍已归档")
        else:
            question_by_id(state,qid)
        if not introduction and state["status"]=="awaiting_verification" and qid not in state.get("open_verifications",[]):
            raise Conflict("本轮只接受指定核实题")
        name=Path(file.filename or "").name
        suffix=Path(name).suffix.lower()
        if suffix not in FORMATS:
            raise ValueError("不支持的录制格式")
        folder=store.root/"media"
        folder.mkdir(exist_ok=True)
        path=None
        try:
            size=0
            digest=sha256()
            with tempfile.NamedTemporaryFile(dir=folder,delete=False,prefix="upload-",suffix=suffix) as f:
                path=Path(f.name)
                while chunk:=file.file.read(1024*1024):
                    size+=len(chunk)
                    if size>100*1024*1024:
                        raise ValueError("录制超过100 MB")
                    digest.update(chunk)
                    f.write(chunk)
            meta=media_info(path)
            fingerprint=digest.hexdigest()
            final=folder/(fingerprint+suffix)
            mime=FORMATS[suffix]
            if not meta.get("has_video") and suffix in (".webm",".mp4"):
                mime="audio/"+suffix[1:]
            endpoint="introduction" if introduction else "answers/"+quote(qid,safe='')
            meta.update(name=name,mime=mime,sha256=fingerprint,stored_name=final.name,
                        url=f"{PREFIX}/sessions/{sid}/{endpoint}/media?sha256={fingerprint}",bytes=size)
            def publish_media():
                if not final.exists():
                    os.replace(path,final)
            if introduction:
                body={"version":version}
                if text is not None:
                    body["text"]=text
                return store.introduction(sid,body,media=meta,before_commit=publish_media)
            return store.add_media(sid,qid,meta,text=text,version=version,before_commit=publish_media)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    async def submitted_media_text(request:Request):
        # Optional scalar Form fields coerce "" to None. Only omission preserves the old text.
        return (await request.form()).get("text")

    @app.post(PREFIX+"/sessions/{sid}/answers/{qid}/media")
    def media_upload(sid:str,qid:str,file:UploadFile=File(...),text=Depends(submitted_media_text),version:int|None=Form(None)):
        return save_media(sid,qid,file,text,version)

    def serve_media(sid,qid,expected,allow_history=False):
        state=store.session(sid)
        media=state["answers"].get(qid,{}).get("media")
        if expected and allow_history and (media or {}).get("sha256")!=expected:
            media=store.historical_media(sid,qid,expected)
        if not media:
            raise KeyError("本题未录制")
        if expected and expected!=media["sha256"]:
            raise Conflict("原片已更新，请打开该版本最新报告")
        return FileResponse(store.media_path(media),media_type=media["mime"])

    @app.get(PREFIX+"/sessions/{sid}/answers/{qid}/media")
    def media(sid:str,qid:str,sha256:str|None=None):
        return serve_media(sid,qid,sha256,allow_history=True)

    def serve_introduction(sid,expected,allow_history=False):
        state=store.session(sid)
        media=(state.get("introduction") or {}).get("media")
        if expected and allow_history and (media or {}).get("sha256") != expected:
            with store.connection() as db:
                row=db.execute("""SELECT json_extract(state,'$.introduction.media') FROM revisions
                    WHERE session_id=? AND json_extract(state,'$.introduction.media.sha256')=?
                    ORDER BY version DESC LIMIT 1""",(sid,expected)).fetchone()
            if not row:
                raise KeyError("本场没有该版本的自我介绍原片")
            media=json.loads(row[0])
        if not media:
            raise KeyError("本场没有自我介绍录制")
        if expected and expected != media["sha256"]:
            raise Conflict("自我介绍已更新，请刷新")
        return FileResponse(store.media_path(media),media_type=media["mime"])

    @app.get(PREFIX+"/sessions/{sid}/introduction/media")
    def teacher_introduction_media(sid:str,sha256:str|None=None):
        return serve_introduction(sid,sha256,allow_history=True)

    @app.post(PREFIX+"/sessions/{sid}/answers/{qid}/process")
    def process(sid:str,qid:str,body:dict=Body(default={})):
        return store.process(sid,qid,body.get("transcribe",True),body.get("refresh",False))

    @app.post(PREFIX+"/sessions/{sid}/answers/{qid}/observations")
    def observations(sid:str,qid:str,body:dict=Body(...)):
        return store.observations(sid,qid,body)

    def schedule(state,tasks):
        if (state.get("auto_scoring") or {}).get("status")=="queued":
            tasks.add_task(scoring.run,state["id"])
        return state

    @app.post(PREFIX+"/sessions/{sid}/finish")
    def finish(sid:str,tasks:BackgroundTasks,body:dict=Body(default={})):
        return schedule(store.finish(sid,body),tasks)

    @app.post(PREFIX+"/sessions/{sid}/auto-score")
    def auto_score(sid:str,tasks:BackgroundTasks,body:dict=Body(...)):
        if type(body.get("version")) is not int:
            raise ValueError("请提供当前场次版本")
        return schedule(scoring.queue(sid,body),tasks)

    @app.post(PREFIX+"/sessions/{sid}/answers/{qid}/correct-transcript")
    def correct_transcript(sid:str,qid:str,tasks:BackgroundTasks,body:dict=Body(...)):
        return schedule(store.correct_transcript(sid,qid,body),tasks)

    @app.post(PREFIX+"/sessions/{sid}/answers/{qid}/reopen-answer")
    def reopen_answer(sid:str,qid:str,body:dict=Body(...)):
        return store.reopen_answer(sid,qid,body)

    @app.get(PREFIX+"/sessions/{sid}/evidence")
    def evidence(sid:str,factor_id:str|None=None):
        return store.evidence(sid,factor_id)

    @app.post(PREFIX+"/sessions/{sid}/clarifications")
    def clarification(sid:str,body:dict=Body(...)):
        return store.clarify(sid,body)

    @app.post(PREFIX+"/sessions/{sid}/draft")
    def draft(sid:str,request:Request,body:dict=Body(...)):
        if body.get("source")=="scripted_demo":
            raise ValueError("演练种子须通过本机专用示例流程生成")
        if body.get("source")=="harness" and not request.headers.get("authorization","").startswith("Bearer "):
            raise ValueError("此来源须由已鉴权的本机接口提交")
        return store.draft(sid,body)

    @app.post(PREFIX+"/sessions/{sid}/local-draft")
    def local_draft(sid:str,body:dict=Body(...)):
        return store.draft(sid,{**body,"source":"local_review"})

    @app.post(PREFIX+"/sessions/{sid}/review")
    def review(sid:str,body:dict=Body(...)):
        return store.review(sid,body)

    @app.post(PREFIX+"/sessions/{sid}/teacher-assessment")
    def teacher_assessment(sid:str,body:dict=Body(...)):
        return store.teacher_assessment(sid,body)

    @app.get(PREFIX+"/sessions/{sid}/report")
    def report(sid:str,version:int|None=None):
        return store.report(sid,version)

    @app.get(PREFIX+"/sessions/{sid}/report.html",response_class=HTMLResponse)
    def html_report(sid:str,version:int|None=None,workbench:bool=False):
        from .product_html import personal_html
        return personal_html(store.report(sid,version),workbench=workbench)

    @app.get(PREFIX+"/batches/{bid}/report.html",response_class=HTMLResponse)
    def html_batch(bid:str):
        from .product_html import batch_html
        return batch_html(store.batch(bid))

    def export_result(file,kind):
        return {"file":str(file),"bytes":file.stat().st_size,"kind":kind}

    @app.post(PREFIX+"/sessions/{sid}/export")
    def export_individual(sid:str,body:dict=Body(default={})):
        from .export_html import export_personal
        version=body.get("version")
        if version is not None and (type(version) is not int or version<1):
            raise ValueError("导出版本无效")
        return export_result(export_personal(store,sid,store.root/"exports",version),"个人报告")

    @app.post(PREFIX+"/batches/{bid}/export")
    def export_cohort(bid:str):
        from .export_html import export_batch
        return export_result(export_batch(store,bid,store.root/"exports"),"同期报告")

    @app.get(PREFIX+"/c/{token}")
    def candidate(token:str):
        return public_session(store.session(store.resolve_token(token)))

    @app.post(PREFIX+"/c/{token}/preflight")
    def candidate_preflight(token:str,file:UploadFile=File(...)):
        from .preflight import inspect_trial
        state=store.session(store.resolve_token(token))
        editable(state)
        suffix=Path(file.filename or "").suffix.lower()
        if suffix not in FORMATS:
            raise ValueError("请选择支持的试录格式")
        path=None
        try:
            with tempfile.NamedTemporaryFile(delete=False,suffix=suffix,prefix="mmsa-trial-") as out:
                path=Path(out.name)
                size=0
                while chunk:=file.file.read(1024*1024):
                    size+=len(chunk)
                    if size>5*1024*1024:
                        raise ValueError("设备试录不能超过5MB")
                    out.write(chunk)
            return inspect_trial(path)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    @app.post(PREFIX+"/c/{token}/introduction")
    def candidate_introduction(token:str,body:dict=Body(...)):
        return public_session(store.introduction(store.resolve_token(token),body))

    @app.post(PREFIX+"/c/{token}/introduction/media")
    def candidate_introduction_upload(token:str,file:UploadFile=File(...),text=Depends(submitted_media_text),version:int|None=Form(None)):
        return public_session(save_media(store.resolve_token(token),None,file,text,version,introduction=True))

    @app.get(PREFIX+"/c/{token}/introduction/media")
    def candidate_introduction_media(token:str,sha256:str|None=None):
        return serve_introduction(store.resolve_token(token),sha256)

    @app.post(PREFIX+"/c/{token}/answers/{qid}")
    def candidate_answer(token:str,qid:str,body:dict=Body(...)):
        return public_session(store.answer(store.resolve_token(token),qid,body))

    @app.post(PREFIX+"/c/{token}/answers/{qid}/media")
    def candidate_upload(token:str,qid:str,file:UploadFile=File(...),text=Depends(submitted_media_text),version:int|None=Form(None)):
        return public_session(save_media(store.resolve_token(token),qid,file,text,version))

    @app.get(PREFIX+"/c/{token}/answers/{qid}/media")
    def candidate_media(token:str,qid:str,sha256:str|None=None):
        return serve_media(store.resolve_token(token),qid,sha256)

    @app.post(PREFIX+"/c/{token}/finish")
    def candidate_finish(token:str,tasks:BackgroundTasks,body:dict=Body(default={})):
        sid=store.resolve_token(token)
        try:
            state=store.finish(sid,{"version":body.get("version")})
        except Conflict:
            raise
        except ValueError:
            store.operation_event(sid,"incomplete_submit")
            raise
        return public_session(schedule(state,tasks))

    @app.post(PREFIX+"/c/{token}/operation-event")
    def operation_event(token:str,body:dict=Body(...)):
        if body.get("code")!="empty_save" or not isinstance(body.get("question_id"),str):
            raise ValueError("未知的操作记录")
        state=store.operation_event(store.resolve_token(token),"empty_save",body["question_id"])
        return {"version":state["version"]}

    return app


app=create_app()
