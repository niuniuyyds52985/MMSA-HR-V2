"""Development-time compiler. Runtime loads the checked-in JSON bank using the standard library."""

import argparse
import hashlib
import json
from pathlib import Path
import re

from openpyxl import load_workbook


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "suit-demo/local-data"
TITLE = "八因子提问清单"


def option_id(question, letter):
    return hashlib.sha256(f"{question}:{letter}".encode()).hexdigest()[:12]


def options(line, qid):
    parts = re.split(r"(?:^|\s+)([A-D])[.．]\s*", line.strip())
    return [(letter, {"id": option_id(qid, letter), "text": text.strip()})
            for letter, text in zip(parts[1::2], parts[2::2])]


def compile_question(row, fid, column, raw, sjt_options):
    qid = f"{fid}-{column}"
    lines = raw.splitlines()
    question = {"id": qid, "factor_id": fid, "source": f"{TITLE}!{column}{row}",
                "prompt": re.sub(r"^Q[12][：:]\s*", "", lines[0]), "kind": "choice",
                "options": [], "fields": []}
    if column == "G":
        question.update(kind="text", prompt=raw)
        return question
    probe = lines.pop().strip("（）") if lines[-1].startswith("（") else None
    if column == "I":
        keyed = options(sjt_options.replace("\n", " "), qid)
        scores = {}
        for _, opt in keyed:
            match = re.search(r"（([1-5])分）$", opt["text"])
            if not match:
                raise ValueError(f"missing SJT points at J{row}")
            scores[opt["id"]] = int(match[1])
            opt["text"] = opt["text"][:match.start()]
        question.update(prompt=raw, answer_key=scores, score_source=f"{TITLE}!J{row}")
    elif column == "E" and row in (6, 10):
        question["kind"] = "compound"
        for key, line in zip(("count", "rate" if row == 6 else "period"), lines[1:], strict=True):
            title, values = line.split("：", 1)
            opts = options(values, qid + key)
            question["fields"].append({"id": key, "prompt": title, "options": [v for _, v in opts]})
        return question
    elif column == "F" and row == 5:
        question["kind"] = "ranking"
        texts = [v.strip() for v in re.split(r"[①②③]", lines[1]) if v.strip()]
        keyed = [(str(i), {"id": option_id(qid, str(i)), "text": v}) for i, v in enumerate(texts)]
    else:
        keyed = options(lines[1], qid)
    if len(keyed) < 3:
        raise ValueError(f"incomplete options for {qid}")
    # Keep natural numeric ranges in order. Shuffle SJT display deterministically for the frozen form.
    question["options"] = [o for _, o in keyed]
    if column == "I":
        question["options"].sort(key=lambda opt: opt["id"])
    if probe:
        selected = re.match(r"选([A-D](?:/[A-D])*)者", probe)
        wording = re.sub(r"^(?:选[A-D/]+者)?(?:追问)?[：:]?", "", probe)
        if wording == "细节":
            wording = "请说明方法名称、迁移过程和实际效果。"
        if wording.startswith("必须各举1例"):
            wording = "请就你提到的牵头经历举例说明。"
        trigger = [o["id"] for key, o in keyed if selected and key in selected[1].split("/")]
        question["probe"] = {"when_any": trigger, "prompt": wording, "source_text": probe}
    return question


def capture_rows(sheet, max_row, max_col):
    return {c.coordinate: c.value for row in sheet.iter_rows(max_row=max_row, max_col=max_col)
            for c in row if c.value is not None}


def compile_bank(question_file, positive_file, risk_file):
    files = [question_file, positive_file, risk_file]
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    books = [load_workbook(p, read_only=True, data_only=False) for p in files]
    try:
        main, positive, risk = books
        lex = [[list(row) for row in b.active.iter_rows(min_row=2, max_row=9, max_col=6, values_only=True)]
               for b in (positive, risk)]
        factors = []
        for i, row in enumerate(main[TITLE].iter_rows(min_row=3, max_row=10, max_col=13, values_only=True)):
            fid = f"F{i+1}"
            pr, rr = lex[0][i], lex[1][i]
            if not (pr[0] == rr[0] == fid and pr[1] == rr[1] == row[1]):
                raise ValueError(f"factor mismatch for {fid}")
            factor = {"id": fid, "name": row[1], "dimension": row[0], "crosswalk": row[2],
                      "indicators": row[3], "anchors": row[7], "theory": row[12],
                      "source_cells": {f"{col}{i+3}": v for col, v in zip("ABCDEFGHIJKLM", row)},
                      "positive_terms": {"base": pr[3], "revised": pr[4], "auxiliary": pr[5],
                                         "source": f"{positive_file.name}!D{i+2}:F{i+2}"},
                      "risk_terms": {"base": rr[3], "revised": rr[4], "neutral": rr[5],
                                     "source": f"{risk_file.name}!D{i+2}:F{i+2}"}}
            factor["questions"] = [compile_question(i+3, fid, col, row["ABCDEFGHIJKLM".index(col)], row[9])
                                   for col in ("E", "F", "G", "I")]
            factors.append(factor)
        payload = {"sources": hashes, "factors": factors, "rules": capture_rows(main["评分与记录规则"], 28, 3),
                   "required_fields": capture_rows(main["所需信息"], 16, 3),
                   "original_weights_sheet": capture_rows(main["评分"], 9, 4)}
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {"version": "v2." + digest[:16], **payload}
    finally:
        for book in books:
            book.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main", type=Path, default=DATA / "requirements-20261105/提问清单.xlsx")
    parser.add_argument("--positive", type=Path, default=DATA / "requirements-20261101/【得分点词-K列】_修订对照表_v3.xlsx")
    parser.add_argument("--risk", type=Path, default=DATA / "requirements-20261101/【风险扣分词-L列】_修订对照表_v3.xlsx")
    parser.add_argument("--output", type=Path, default=Path(__file__).with_name("bank.json"))
    args = parser.parse_args()
    bank = compile_bank(args.main, args.positive, args.risk)
    args.output.write_text(json.dumps(bank, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(bank["version"], len(bank["factors"]), "factors")
