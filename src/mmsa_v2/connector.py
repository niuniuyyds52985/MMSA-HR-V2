"""Local V2 tools, service startup and bounded report projections."""

from contextlib import redirect_stdout
import os
import sys
from urllib.parse import urlsplit, urlencode
import webbrowser

from .access import data_root, operator_key
from .scoring import score_summary


def job_summary(job):
    if not isinstance(job, dict):
        return None
    result = {k: job[k] for k in ("status", "engine", "state_method", "run_id") if k in job}
    result["issues"] = [str(v)[:300] for v in job.get("issues", [])[:20]]
    result["nodes"] = {str(k)[:100]: {field: value[field] if isinstance(value[field], bool) else str(value[field])[:300]
                       for field in ("status", "error", "cached") if field in value}
                       for k, value in list(job.get("nodes", {}).items())[:40] if isinstance(value, dict)}
    return result


def draft_summary(draft):
    if not isinstance(draft, dict):
        return None
    result = {k: draft[k] for k in ("source", "author", "input_version") if k in draft}
    meta = draft.get("model_provenance") or {}
    result["model_provenance"] = {k: v for k, v in meta.items()
        if k in ("provider", "model", "request_id", "status", "prompt_version", "schema_version", "input_version", "usage")}
    return result


def compact_report(report):
    fields = ("id", "session_id", "candidate", "version", "status", "input_version", "total",
              "estimated_total", "components", "session", "review_status", "coverage_policy")
    result = {k: report[k] for k in fields if k in report}
    result.update(score_summary(report))
    result["summary"] = str(report.get("summary", ""))[:2000]
    result["auto_scoring"] = job_summary(report.get("auto_scoring"))
    result["draft"] = draft_summary(report.get("draft"))
    review = report.get("review") or {}
    result["review"] = {k: str(review[k])[:500] for k in ("decision", "reviewer", "note") if k in review}
    result["factors"] = [{k: f[k] for k in ("id", "name", "rating", "rating_source", "verification_gaps") if k in f}
                         for f in report.get("factors", [])[:8]]
    result["transcriptions"] = [{"question_id": qid,
        "has_transcript": bool(a.get("transcript")),
        "provenance": {k: v for k, v in (a.get("transcription_provenance") or {}).items()
            if k in ("provider", "model", "status", "source", "request_id", "method")}}
        for qid, a in report.get("answers", {}).items() if isinstance(a, dict) and (a.get("media") or a.get("transcription_provenance"))][:16]
    return result


def v2_client(common):
    obj, tool = common.object_schema, common.tool
    ident = common.IDENTIFIER
    string = {"type": "string", "minLength": 1, "maxLength": 4000}
    number = {"type": "integer", "minimum": 0}
    batch = {"batch_id": ident}
    sid = {"session_id": ident}
    version = {"input_version": {"type": "integer", "minimum": 1}}
    factor = {"factor_id": {"type": "string", "enum": [f"F{i}" for i in range(1, 9)]}}
    citation = obj({"question_id": ident, "quote": string}, ["question_id", "quote"])
    specs = []

    def add(name, description, fields, required=None, write=False):
        specs.append(tool("v2_" + name, description, description, obj(fields, list(fields) if required is None else required), write=write))

    add("status", "检查 V2 工作台与题库", {})
    add("open_workbench", "在本机浏览器打开 HR 总控或指定批次/个人报告；自动启动本机服务，不返回登录密钥", {**batch, **sid}, [], write=True)
    add("batches", "查看 V2 批次", {})
    add("create_batch", "创建一个通用管培生模拟批次", {"name": string}, write=True)
    add("import_resumes", "仅导入项目模拟简历目录中实际存在的 DOCX；交付包不含原30份简历。自有简历请打开总控导入", batch, write=True)
    add("batch_people", "查看批次的人员、面试状态及报告入口", {**batch, "page": number}, ["batch_id"])
    add("start_interview", "用候选人简历和当前题库发起模拟面试", {**batch, "candidate_id": ident}, write=True)
    add("evidence", "读取实际题干、锚点与原话。EXPERIENCE 表示经历介绍；翻页时传首屏 input_version", {**sid,
        "factor_id":{"type":"string","enum":[*factor["factor_id"]["enum"],"EXPERIENCE"]}, **version,"page":number}, ["session_id","factor_id"])
    add("select_plan", "调整历史完整题卷；5+3 面试由系统分两阶段从题库组卷", {**sid, **version,
        "question_ids":{"type":"array", "items":ident, "minItems":1, "maxItems":64, "uniqueItems":True}, "reason":string}, write=True)
    add("session", "查看面试进度、输入版本、已发题目与选题来源", {**sid, "page":number}, ["session_id"])
    add("request_verification", "为历史完整题卷申请核实；5+3 面试将核实建议写入下一轮", {**sid, **version, "source_question_id":ident,"question_id":ident,"reason":string}, write=True)
    add("save_factor", "保存一个因子的证据草稿。证据不足可省略 rating，引用必须对应本因子原话", {**sid, **version, **factor,
        "rating":{"type":"integer","minimum":1,"maximum":5}, "reason":string,
        "evidence":{"type":"array","maxItems":12,"items":citation},
        "verification":obj({field:citation for field in ("action","cost","experience")},["action","cost","experience"])},
        ["session_id","input_version","factor_id","reason","evidence"], write=True)
    add("finish_report", "保存整场分析、经历评分及下一轮建议，保留教师复核入口", {**sid, **version, "summary":string,
        "experience_score":{"type":"integer","minimum":0,"maximum":100},
        "experience_evidence":citation, "experience_reason":string,
        "operation_score":{"type":"integer","minimum":0,"maximum":100},"operation_reason":string,
        "next_steps":{"type":"array","items":string,"maxItems":12}}, ["session_id","input_version","summary","next_steps"], write=True)
    add("report", "查看个人报告得分、覆盖及复核状态", sid)
    add("export_personal_html", "导出群内样式的个人 HTML 到本机固定报告目录", sid, write=True)
    add("export_batch_html", "导出同期批次 HTML 到本机固定报告目录", batch, write=True)
    by_name = {s["name"]:s for s in specs}

    class Client:
        tool_definitions = specs
        connector_version = "2.0.0"

        def __init__(self):
            self.v2 = common.WorkbenchClient(os.environ.get("MMSA_V2_BASE_URL","http://127.0.0.1:8767"),
                                             token_loader=lambda: operator_key(data_root()))

        def ensure_workbench(self):
            from .launch import start
            port = urlsplit(self.v2.base_url).port or 80
            try:
                # A connector must never restart an in-flight workbench merely
                # because source files changed. Explicit launcher handles updates.
                with redirect_stdout(sys.stderr):
                    return start(port, False, announce=False, restart_stale=False)
            except Exception:
                raise common.ToolError("workbench_start_failed", "本机服务未就绪，请运行安装器自检或双击启动面试工作台。") from None

        def call(self, name, args):
            if name not in by_name:
                raise common.ToolError("unknown_tool","未知 V2 工具")
            common.validate_arguments(args, by_name[name]["inputSchema"])
            if name == "v2_open_workbench" and args.get("batch_id") and args.get("session_id"):
                raise common.ToolError("invalid_arguments", "一次只能指定批次或个人场次")
            runtime = self.ensure_workbench()
            bid, session_id = args.get("batch_id"), args.get("session_id")
            prefix = "/v2/api"
            session_path = f"{prefix}/sessions/{session_id}"
            batch_path = f"{prefix}/batches/{bid}"
            key = self.v2._operator()

            def get(path):
                return self.v2._request("GET", path, key)

            def post(path, body):
                return self.v2._request("POST", path, key, body)

            action = name[3:]
            if action == "open_workbench":
                if session_id:
                    get(session_path)
                elif bid:
                    get(batch_path)
                query = urlencode({k: v for k, v in (("session", session_id), ("batch", bid)) if v})
                url = self.v2.base_url + "/v2/" + ("?" + query if query else "")
                if not webbrowser.open(url + "#access=" + key):
                    raise common.ToolError("browser_open_failed", "浏览器未接受打开请求，请双击本机启动器。")
                return {"browser_open_requested": True, "workbench_url": url, "local_only": True,
                        "update_available": runtime.get("update_available", False)}
            if action == "status":
                result = get(prefix+"/status")
                get(prefix+"/batches")
                return {"service":"zhenjian-interview-v2","authenticated":True,"status":result,
                        "runtime":runtime,"transport":"stdio","scope":"比赛模拟","paid_cloud_tools_exposed":False,
                        "verification":{"local_api":"verified", "models":"not_checked_by_this_tool", "bailian_api":"not_checked_by_this_tool"},
                        "cloud_processing":"面试提交后的百炼评分/转写按本机配置执行，与千问宿主模型分开。"}
            if action == "batches":
                return common.safe_output(get(prefix+"/batches"))
            if action == "create_batch":
                return common.safe_output(post(prefix+"/batches",{"name":args["name"]}))
            if action == "import_resumes":
                response = post(batch_path+"/import-demo",{})
                return {k:v for k,v in response.items() if not isinstance(v,(dict,list))} or {"batch_id":bid,"next":"v2_batch_people"}
            if action in ("batch_people","evidence","session"):
                if action == "evidence":
                    fid = args["factor_id"]
                    data = get(session_path+"/evidence?factor_id="+fid)
                    if "input_version" in args and args["input_version"] != data["input_version"]:
                        raise common.ToolError("stale_evidence","证据版本已更新，请从该因子第一页重读")
                    data = {k:v for k,v in data.items() if k in ("input_version","bank_version","plan","answers","rules","factors")}
                    if fid == "EXPERIENCE":
                        data = {**data,"plan":[q for q in data["plan"] if q["id"]=="EXPERIENCE"],
                                "answers":{"EXPERIENCE":data["answers"].get("EXPERIENCE",{})},"factors":[]}
                    data["answers"] = {qid:{k:v for k,v in a.items() if k in ("value","text","transcript","answer_label","observations","sjt_score","word_signals","transcription_provenance")}
                                       for qid,a in data["answers"].items()}
                elif action == "session":
                    data = get(session_path)
                    data = {**{k:v for k,v in data.items() if k in {"id","candidate","status","version","input_version","bank_version","resume","plan","open_verifications","interview_mode","expected_questions"}},
                            "auto_scoring": job_summary(data.get("auto_scoring")), "draft": draft_summary(data.get("draft"))}
                    data["resume"] = {k:v for k,v in data.get("resume",{}).items() if k in ("education","school","internship","english","experiences")}
                else:
                    data = get(batch_path)
                    fields = ("id","name","session_id","status","version","total","display_score","score_kind",
                              "rank","rated_factor_count","answered_questions","expected_questions","student_url","analysis_source","reviewer")
                    data["candidates"] = [{k:v for k,v in p.items() if k in fields} for p in data["candidates"]]
                records = []
                for field, value in common.safe_output(data).items():
                    if field in {"token","candidate_access","processing"}:
                        continue
                    if isinstance(value,list):
                        records.extend({"field":field,"item":item} for item in value)
                    elif isinstance(value,dict):
                        records.extend({"field":field,"key":k,"value":v} for k,v in value.items())
                    else:
                        records.append({"field":field,"value":value})
                return common._projection_page({"tool":name,"session_id":session_id,"batch_id":bid,
                          "input_version":data.get("input_version")},records,args.get("page",0))
            if action == "start_interview":
                response = post(batch_path+"/sessions", {"candidate_id":args["candidate_id"]})
                result = {k:response[k] for k in ("id","session_id","status","input_version") if k in response}
                if response.get("token"):
                    result["student_url"] = self.v2.base_url + "/v2/c/" + response["token"]
                result["local_only"] = True
                return common.safe_output(result)
            body = {k:v for k,v in args.items() if k != "session_id"}
            if action == "select_plan":
                result = post(session_path+"/plan",body)
            elif action == "request_verification":
                result = post(session_path+"/clarifications",body)
            elif action == "save_factor":
                body["rating"] = args.get("rating")
                result = post(session_path+"/draft",{"input_version":body.pop("input_version"),"factors":[body],"source":"harness","author":"千问办公接口"})
            elif action == "finish_report":
                result = post(session_path+"/draft",{**body,"factors":[],"source":"harness","author":"千问办公接口"})
            elif action == "report":
                result = get(session_path+"/report")
                result = compact_report(result)
            elif action in ("export_personal_html","export_batch_html"):
                return post((session_path if session_id else batch_path)+"/export",{})
            else:
                raise common.ToolError("unknown_tool","未知 V2 操作")
            if action != "report":
                return {k:v for k,v in common.safe_output(result).items() if not isinstance(v,(dict,list))} or {"saved":True}
            return common.safe_output(result)

    return Client()
