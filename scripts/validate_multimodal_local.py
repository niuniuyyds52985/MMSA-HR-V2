"""Recompute one existing simulation locally, preserving the live session by default.

Writes only a comparison and reusable analysis output under local validation/.
The optional --apply saves an audited media-only revision through Workflow.change;
existing text scores are preserved and no cloud operations are invoked.
"""
import argparse
import copy
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.mmsa_v2.auto_media import analyze_answer
from src.mmsa_v2.scoring import STATE_POINTS


def summary(estimate):
    seconds = {state: 0.0 for state in STATE_POINTS}
    for segment in estimate.get("segments", []):
        seconds[segment["state"]] += segment["end"] - segment["start"]
    valid = sum(seconds.values())
    duration = estimate["duration_seconds"]
    return {"method": estimate["method"], "duration_seconds": duration,
            "classified_seconds": valid, "coverage": valid / duration,
            "seconds": seconds, "unclassified_seconds": duration - valid,
            "classified_proportions": {s: t / valid if valid else None for s, t in seconds.items()},
            "score": sum(STATE_POINTS[s] * t for s, t in seconds.items()) / valid if valid else None}


def read_session(directory, sid):
    with sqlite3.connect((directory / "workflow.sqlite3").as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT state,bank FROM sessions WHERE id=?", (sid,)).fetchone()
    if not row:
        raise ValueError("Unknown session")
    return json.loads(row[0]), json.loads(row[1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("session_id")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    directory = ROOT / "suit-demo/local-data/v2"
    output = directory / "validation" / ("affect-" + args.session_id + ".json")
    state, bank = read_session(directory, args.session_id)
    if args.apply:
        from src.mmsa_v2.automatic import media_checkpoint_key
        from src.mmsa_v2.auto_media import affect_method_fingerprint, estimate_from_cached_answer
        from src.mmsa_v2.checkpoints import Checkpoints
        from src.mmsa_v2.state_rules import METHOD
        from src.mmsa_v2.workflow import Workflow
        record = json.loads(output.read_text(encoding="utf-8"))
        if state["version"] != record["source_version"]:
            raise ValueError("Session changed after comparison; regenerate before applying")
        if state.get("review") or state.get("manual_scoring_required"):
            raise ValueError("Reviewed scores must not be overwritten")
        store = Workflow(directory)
        checkpoints = Checkpoints(store)
        if (state.get("auto_scoring") or {}).get("status") in ("queued", "running"):
            raise ValueError("Analysis is running; do not overwrite concurrent work")
        for qid, result in record["results"].items():
            verified = estimate_from_cached_answer({**state["answers"][qid], **result})
            if verified != result["state_estimate"]:
                raise ValueError("Saved model results do not match the current fusion policy")
        for qid, result in record["results"].items():
            checkpoints.save(args.session_id, "media:" + qid,
                             media_checkpoint_key(state["answers"][qid]["media"]["sha256"]), result)
        before = copy.deepcopy(state)
        def apply(current, frozen_bank):
            for qid, result in record["results"].items():
                current["answers"][qid].update(processing=copy.deepcopy(result["processing"]),
                    state_estimate=copy.deepcopy(result["state_estimate"]), state_estimate_error=None,
                    processing_transcribe=True)
            job = current.setdefault("auto_scoring", {})
            nodes = job.setdefault("nodes", {})
            for qid in record["results"]:
                nodes["media:"+qid] = {"status":"succeeded", "cached":True,
                    "input_key":media_checkpoint_key(current["answers"][qid]["media"]["sha256"])}
            # Only remove the old multimodal coverage issue; preserve unrelated issues.
            issues = [x for x in job.get("issues", []) if not x.startswith("多模态每题及整场有效覆盖")]
            report = store.report_from(current, frozen_bank)
            if not report["session"].get("meets_coverage_threshold"):
                issues.append("多模态每题及整场有效覆盖须至少70%，当前未达标，观测分不计入综合分与排名。")
            if report.get("total") is None and report.get("estimated_total") is None and not issues:
                issues.append("报告仍有分项缺口，请核对评分明细。")
            job.update(issues=issues, status="partial" if issues else "done", state_method=METHOD,
                       method_fingerprint=affect_method_fingerprint())
            current["multimodal_revision"] = {"source_version":record["source_version"], "method":METHOD,
                                               "text_scores_preserved":True, "cloud_calls":0}
        after = store.change(args.session_id, {"version":state["version"]}, apply)
        assert before["input_version"] == after["input_version"], "Text input version changed"
        for qid, answer in before["answers"].items():
            for key in ("text", "transcript", "media", "value"):
                assert answer.get(key) == after["answers"][qid].get(key), "Original answer changed"
        for key in ("factors", "experience_score", "operation_score"):
            assert (before.get("draft") or {}).get(key) == (after.get("draft") or {}).get(key), "Text score changed"
        report = store.report(args.session_id)
        result = {"source_version": before["version"], "applied_version": after["version"],
                  "status": after.get("auto_scoring", {}).get("status"), "issues": after.get("auto_scoring", {}).get("issues"),
                  "session": report["session"], "text_scores_preserved": True, "cloud_calls": 0}
        output.with_name(output.stem + "-applied.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False))
        return
    results, comparison = {}, []
    for qid, answer in state["answers"].items():
        if not answer.get("media"):
            continue
        name = answer["media"]["stored_name"]
        if Path(name).name != name:
            raise ValueError("Invalid stored media name")
        print("Analyzing", qid, flush=True)
        result = analyze_answer(directory / "media" / name, answer)
        results[qid] = result
        item = {"question_id": qid, "old": summary(answer["state_estimate"]), "new": summary(result["state_estimate"])}
        comparison.append(item)
        print(json.dumps(item, ensure_ascii=False), flush=True)
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps({"session_id": args.session_id, "source_version": state["version"],
                                 "network_used": False, "comparison": comparison, "results": results},
                                ensure_ascii=False, allow_nan=False), encoding="utf-8")
    print("Saved local comparison:", output)


if __name__ == "__main__":
    main()
