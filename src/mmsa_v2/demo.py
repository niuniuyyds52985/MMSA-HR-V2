"""Opt-in local walkthrough with scripted answers, predetermined labels and synthetic video cards."""

import argparse
import base64
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from tempfile import NamedTemporaryFile, TemporaryDirectory
import textwrap
from urllib.parse import quote

from .media import media_info
from .product_html import batch_html, personal_html
from .workflow import ROOT, Workflow


BATCH = "流程演练 · 合成作答与分数"
PERSONAL = "V2个人面试评价报告.html"
BATCH_REPORT = "V2管培生同期报告.html"
# Digest of the 30 SHA-256 receipts ordered by filename in the original synthetic-resume intake.
RESUME_SET_HASH = "5ae2a3b5644659e0f61c644bd2adac1fb2f6c2b5d2bd2b13cd3b3b9c6997346e"
REVIEWER = "流程演练脚本（示例复核）"
LABELER = "流程演练脚本（已知标签）"
SUMMARY = ("以下为脚本生成的流程演练。简历取自已导入的模拟 DOCX，作答和八因子评价均为编写的示例；"
           "视频为带字幕的合成色卡，状态标签随色卡预设。复核动作由演练脚本执行。"
           "本组示例中，组织协调和结果复盘有具体行动与数字；培养他人和接纳反馈仍缺少长期结果。")
# Each record gives an authored answer, a demonstrative rating and the judge's reason.
CASES = {
    "F1": ("模拟联合国大会临近时两组进度不同，我先单独询问负责人，再把报名和会务分开排期。"
           "我多投入了两个晚上跟进供应商，最终六个会场按时开放。",
           4, "能指出分工、承担额外时间和交付结果；其他需要无头衔牵头的场合尚待核实。"),
    "F2": ("组内一位新同学不会写议程，我示范了一次后请他独立起草第二份，我只批注。"
           "后来他独立主持了一次小组会。关于与自己竞争的人分享资源，我缺少实例。",
           3, "有从示范到独立的具体转变；对竞争关系中的分享尚缺有效实例。"),
    "F3": ("会务组希望留出布场时间，学术组希望增加讨论。我分别听了两方意见，"
           "把一场讨论转到下午，保留二十分钟布场，并在会后询问两边是否还有困难。",
           4, "描述了双方顾虑、实际调整和关系跟进；仍可核对经调整后的议程执行记录。"),
    "F4": ("实习时我给自己定了周五前清理一百条库存异常的目标。接口暂时不可用时改查小时快照，"
           "最终核对九十二条，周五下午就把剩余八条与原因同步给负责人。",
           4, "目标、替代路径与差距都可核查；九十二条的核对标准还可由下一轮验证。"),
    "F5": ("生成仓网方案时我只算了仓库的配送成本。运营同事提醒售后压力后，我才补列退换货节点。"
           "我没有拿到完整的跨部门收益数据。",
           2, "最初判断集中于本组成本，经提醒才补充售后视角，整体收益的证据仍缺失。"),
    "F6": ("客户调整了汇报范围，我用半小时重列三个假设，先交付能确认的内容，"
           "把缺失口径放进问题清单。得到反馈后我在当天修正了最高库存周的估计。",
           4, "展示了变化后的响应时间、推进假设与纠偏动作；该策略在不同情况中的稳定性仍待核实。"),
    "F7": ("导师指出看板只有数字没有解释。我第二天补上异常原因栏，"
           "但还没有回访使用这个看板的人，所以我不知道修改是否真正解决了他们的问题。",
           3, "有具体的采纳反馈行为，验证使用效果的步骤尚缺。"),
    "F8": ("我花三周学会 SQL 窗口函数，独立完成收益归因看板，"
           "后来把同一套分组比较思路用在库存异常排查，把人工核对时间从两个小时减到一小时。",
           4, "给出了学习周期、独立交付和跨任务迁移的例子；节省时长的测量方法仍需核实。"),
}
EXPERIENCE = ("我在投研实习中维护收益归因看板，在供应链实习中排查库存异常。"
              "前者让我学会解释数据变化，后者让我看到执行和业务成本的关系。"
              "我希望把这两类经历用于管培轮岗中的经营分析和跨部门沟通。")
FOLLOWUPS = {
    "F1-E": "我在大会协调和一次校际商赛都承担过分工。商赛时我排定交付时间，大会时我协调会场。",
    "F2-E": "我先示范，再要求每位学员独立提交第二份议程，随后一起看修订记录。",
    "F3-E": "我把双方担忧写在同一张排期表上，先确认底线，再找到能调整的时段。",
    "F3-F": "先分别听意见，再确认同一截止时间，最后现场商定布场时间。",
    "F4-F": "我先试批量接口，再改查小时快照。截止时完成九十二条，剩余八条另列原因。",
    "F5-E": "上游是各仓库的登记人员，下游是要解释退换货的售后同事。",
    "F5-F": "为了补齐售后数据，我推迟了第一版表格一天，事先向负责人说明。",
    "F6-E": "变化当天我先花半小时确认关键假设，两个小时内形成第一版备选方案。",
    "F6-F": "第一版高估了一个时段的库存；收到反馈后，我当天按新口径重算。",
    "F7-E": "我问导师哪些结论难用，增加了异常原因；还没用业务人员的回访验证效果。",
    "F8-F": "我把 SQL 窗口分组的办法从收益归因迁移到库存异常复核，核查用时缩短一半。",
}

STATES = ("洋溢", "饱满", "平和", "略微 down", "非常不好")
COLORS = ("#008b76", "#3387ac", "#6e86a6", "#bc9151", "#a86677")


def _script_value(q):
    fid, qid = q["factor_id"], q["id"]
    if qid == "EXPERIENCE":
        return EXPERIENCE
    if qid.endswith("-followup"):
        return FOLLOWUPS[qid.removesuffix("-followup")]
    if qid.endswith("-G"):
        return CASES[fid][0]
    if q["kind"] == "ranking":
        return [o["id"] for o in q["options"]]
    if q["kind"] == "compound":
        return {f["id"]: f["options"][2 if f["id"] != "count" else 1]["id"] for f in q["fields"]}
    # Simulated selections refer to the visible bank wording; SJT choices use the frozen display order.
    idx = {"F1-E": 2, "F2-E": 1, "F3-E": 1, "F5-E": 1, "F6-E": 1, "F7-E": 1, "F8-F": 0,
           "F1-F": 1, "F2-F": 1, "F5-F": 1, "F6-F": 0, "F7-F": 1}.get(qid, 0)
    if qid.endswith("-I"):
        idx = int(fid[1:]) % len(q["options"])
    return q["options"][idx]["id"]


def _make_clip(destination, qid, answer):
    import av
    from PIL import Image, ImageDraw, ImageFont

    font_file = next((p for p in (Path(os.environ.get("WINDIR", "C:/Windows"))/"Fonts/msyh.ttc",
                                  Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")) if p.is_file()), None)
    if not font_file:
        raise RuntimeError("生成中文演示字幕需要微软雅黑或 Noto Sans CJK 字体")
    font = ImageFont.truetype(str(font_file), 21)
    title = ImageFont.truetype(str(font_file), 29)
    offset = sha256(qid.encode()).digest()[0] % 5
    # Manufacture two labelled windows of unequal lengths; the media is a test stimulus.
    pattern = [(offset, 2), ((offset+1) % 5, 3)]
    segments = []
    seconds = 0
    with av.open(str(destination), "w", format="mp4", options={"movflags":"+faststart"}) as out:
        track = out.add_stream("libx264", rate=2)
        track.width, track.height, track.pix_fmt = 800, 360, "yuv420p"
        track.options = {"preset":"ultrafast", "crf":"25"}
        for state, duration in pattern:
            img = Image.new("RGB", (800,360), "#f7fafc")
            paint = ImageDraw.Draw(img)
            paint.rectangle((0,0,800,67), fill=COLORS[state])
            paint.text((22,13), "流程演练  /  合成状态色卡", font=title, fill="white")
            paint.text((22,86), f"{qid}    预设标签：{STATES[state]}    {seconds}–{seconds+duration} 秒",
                       font=font, fill="#28465a")
            for line, text in enumerate(textwrap.wrap(answer, width=33)):
                paint.text((22,137+line*36), text, font=font, fill="#27394c")
            for _ in range(duration*2):
                for packet in track.encode(av.VideoFrame.from_image(img)):
                    out.mux(packet)
            segments.append({"start":seconds, "end":seconds+duration, "state":STATES[state]})
            seconds += duration
        for packet in track.encode():
            out.mux(packet)
    return segments


def _simulate(store, resumes):
    batch = store.new_batch(BATCH)
    batch = store.import_files(batch["id"], ((f.name, f.read_bytes()) for f in resumes))
    # This archived fixture demonstrates the original full bank and quotes all eight G and I items.
    sid = store.create_session(batch["id"], batch["candidates"][0]["id"], interview_mode="legacy_full")["id"]
    media_dir = store.root / "media"
    media_dir.mkdir(exist_ok=True)
    while True:
        current = store.session(sid)
        q = next((q for q in current["plan"] if q["id"] not in current["answers"]), None)
        if q is None:
            break
        qid = q["id"]
        value = _script_value(q)
        store.answer(sid, qid, {"value":value})
        if q["kind"] == "text":
            clip = media_dir/"pending.mp4"
            segments = _make_clip(clip, qid, value)
            digest = sha256(clip.read_bytes()).hexdigest()
            meta = media_info(clip)
            target = media_dir / f"{digest}.mp4"
            os.replace(clip,target)
            meta.update(sha256=digest, stored_name=target.name, name=f"{qid}-模拟色卡.mp4",
                        mime="video/mp4", bytes=target.stat().st_size, source="synthetic_fixture")
            store.add_media(sid,qid,meta)
            store.observations(sid,qid,{"confirmed":True,"duration_seconds":segments[-1]["end"],
                                        "reviewer":LABELER,"segments":segments})
    state = store.finish(sid,{})
    factors = []
    for fid,(answer,rating,reason) in CASES.items():
        sjt = state["answers"][fid+"-I"]
        evidence = [{"question_id":fid+"-G","quote":answer}]
        if sjt.get("answer_label"):
            evidence.append({"question_id":fid+"-I","quote":sjt["answer_label"]})
        factors.append({"factor_id":fid,"rating":rating,"reason":reason,"evidence":evidence})
    draft = store.draft(sid,{"input_version":state["input_version"], "factors":factors,
            "experience_score":80, "operation_score":100, "operation_reason":"脚本按题卷顺序提交了所有必答项。",
            "summary":SUMMARY, "next_steps":["请用一个实例说明为了下游业务而放弃局部收益时的具体成本。",
                "请给出看板使用者的回访记录，说明修改后哪项决策发生了变化。",
                "询问被带教者独立完成任务后的表现，并核对提供帮助的具体方式。"]})
    store.review(sid,{"version":draft["version"],"decision":"approve","reviewer":REVIEWER,
                      "notes":"仅检验复核与报告流程。全部状态标注对应合成色卡，评语为演示脚本。"})
    return batch["id"], sid


def _publish(target, generated):
    media_folder = target.root/"media"
    media_folder.mkdir(exist_ok=True)
    existing_files = (generated.root/"media").glob("*.mp4")
    for clip in existing_files:
        dest = media_folder/clip.name
        if not dest.exists():
            with NamedTemporaryFile(dir=media_folder,delete=False,suffix=".tmp") as temp:
                path = Path(temp.name)
            try:
                shutil.copyfile(clip,path)
                os.replace(path,dest)
            finally:
                path.unlink(missing_ok=True)
    # Publish every complete demo record in one transaction, preserving any pre-existing batch.
    with target.connection(True) as db:
        if db.execute("SELECT 1 FROM batches WHERE name=?", (BATCH,)).fetchone():
            return
        db.execute("ATTACH DATABASE ? AS prepared",(str(generated.db),))
        for table in ("batches","candidates","sessions","revisions"):
            db.execute(f"INSERT INTO main.{table} SELECT * FROM prepared.{table}")


def _write_atomic(path, text):
    with NamedTemporaryFile(mode="w",encoding="utf-8",dir=path.parent,delete=False) as tmp:
        pending = Path(tmp.name)
        tmp.write(text)
    try:
        os.replace(pending,path)
    finally:
        pending.unlink(missing_ok=True)


def generate(root=None, output=None):
    live = Workflow(root)
    batch = next((b for b in live.batches() if b["name"] == BATCH), None)
    if not batch:
        resumes = sorted(p for p in (ROOT/"模拟简历").glob("*.docx") if not p.name.startswith("~$"))
        if len(resumes) != 30:
            raise ValueError("演练需要原先登记的 30 份模拟 DOCX")
        fingerprint = "".join(sha256(p.read_bytes()).hexdigest() for p in resumes)
        if sha256(fingerprint.encode("ascii")).hexdigest() != RESUME_SET_HASH:
            raise ValueError("简历与登记的演练样本不一致，请使用真实批次入口导入新的材料")
        with TemporaryDirectory(prefix="mmsa-v2-demo-") as folder:
            temporary = Workflow(folder)
            _simulate(temporary,resumes)
            _publish(live,temporary)
        batch = next(b for b in live.batches() if b["name"] == BATCH)
    b = live.batch(batch["id"])
    complete = [c for c in b["candidates"] if c.get("session_id") and c.get("status") == "reviewed"]
    scripted = [r for c in complete if (r:=live.report(c["session_id"])).get("review",{}).get("reviewer") == REVIEWER]
    if len(scripted) != 1:
        raise ValueError("同名批次已存在且没有唯一可导出的演练报告，已保留现有数据")
    r = scripted[0]
    folder = Path(output or ROOT/"suit-demo/local-data/v2-demo")
    folder.mkdir(parents=True, exist_ok=True)
    person = personal_html(r)
    for qid,a in r["answers"].items():
        m = a.get("media")
        if m and m.get("source") == "synthetic_fixture":
            url = f'/v2/api/sessions/{r["id"]}/answers/{quote(qid,safe="")}/media?sha256={m["sha256"]}'
            data = base64.b64encode(live.media_path(m).read_bytes()).decode("ascii")
            person = person.replace(url, "data:video/mp4;base64,"+data)
    group = batch_html(b).replace(f'/v2/api/sessions/{r["id"]}/report.html?version={r["version"]}', quote(PERSONAL))
    _write_atomic(folder/PERSONAL,person)
    _write_atomic(folder/BATCH_REPORT,group)
    return {"batch_id":b["id"],"session_id":r["id"],"version":r["version"],"candidate_count":len(b["candidates"]),
            "question_count":len(r["plan"]),"media_count":sum("media" in a for a in r["answers"].values()),
            "score":r["total"],"personal":str(folder/PERSONAL),"batch":str(folder/BATCH_REPORT)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir",type=Path)
    parser.add_argument("--output",type=Path)
    args = parser.parse_args()
    print(json.dumps(generate(args.data_dir,args.output),ensure_ascii=False,indent=2))
