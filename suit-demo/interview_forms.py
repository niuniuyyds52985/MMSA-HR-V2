"""Reproducible, source-preserving research forms with draft content rubrics.

This module performs no scoring and no I/O other than reading public config files.
The returned config is a complete snapshot for the caller to freeze with a session.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any


CONFIG_DIR = Path(__file__).resolve().parent / "config"
FORM_VERSION = "mt_bank_forms_v1_draft_20261027"


def _load(name: str) -> dict[str, Any]:
    return json.loads((CONFIG_DIR / name).read_text(encoding="utf-8-sig"))


def load_bank() -> dict[str, Any]:
    return _load("question-bank.v1.json")


def load_rubrics() -> dict[str, Any]:
    return _load("form-rubrics.v1.json")


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _index(items: list[dict], key: str, label: str) -> dict[str, dict]:
    if not isinstance(items, list) or not items:
        raise ValueError(f"{label} must be a nonempty list")
    result = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get(key), str) or not item[key]:
            raise ValueError(f"invalid {label} {key}")
        if item[key] in result:
            raise ValueError(f"duplicate {label} {item[key]}")
        result[item[key]] = item
    return result


def _source_refs(refs: Any, question: dict, factor: dict) -> None:
    if not isinstance(refs, list) or not refs:
        raise ValueError("missing source_refs")
    for ref in refs:
        if (not isinstance(ref, dict)
                or ref.get("source_cell") not in factor["source_cells"]
                or ref.get("source_sheet") != question["source_sheet"]
                or ref.get("source_file_hash") != question["source_file_hash"]):
            raise ValueError("source_refs do not match original workbook")


def _requirements(factor: dict, source_file_hash: str) -> list[dict]:
    cell = factor["assessment_content_source_cell"]
    return [{"requirement_id": f"factor:{factor['factor_id']}:{cell}:{index}",
             "text": text.strip(), "source_cell": cell,
             "source_sheet": factor["source_sheet"], "source_file_hash": source_file_hash,
             "status": "source_reference", "scoring_scope": "answer_content_only"}
            for index, text in enumerate(
                [line for line in factor["assessment_content"].splitlines() if line.strip()], 1)]


def compile_bank(bank: dict | None = None, rubrics: dict | None = None) -> dict:
    """Compile all 24 original main questions and all 36 original followups.

    Cues are deliberately literal, bounded alternatives, not executable expressions
    from user input. A match needs contextual review; absence is only an unresolved
    evidence gap. Generated anchors remain proposed until substantive review.
    """
    bank = copy.deepcopy(load_bank() if bank is None else bank)
    rubrics = copy.deepcopy(load_rubrics() if rubrics is None else rubrics)
    if bank.get("schema_version") != "question-bank.v1":
        raise ValueError("unsupported question bank")
    if rubrics.get("schema_version") != "form-rubrics.v1":
        raise ValueError("unsupported form rubrics")
    if rubrics.get("source_bank_version") != bank.get("bank_version"):
        raise ValueError("rubrics target a different bank version")
    questions = _index(bank.get("questions"), "question_id", "questions")
    branches = _index(bank.get("branches"), "branch_id", "branches")
    factors = _index(bank.get("factors"), "factor_id", "factors")
    dimensions = _index(bank.get("dimensions"), "dimension_id", "dimensions")
    if (len(questions), len(branches), len(factors), len(dimensions)) != (24, 36, 8, 3):
        raise ValueError("expected source bank with 24 questions, 36 branches, 8 factors, 3 dimensions")
    question_rubrics = rubrics.get("questions", {})
    if set(question_rubrics) != set(questions):
        raise ValueError("every original question needs exactly one item rubric")
    for factor in factors.values():
        if factor.get("dimension_id") not in dimensions:
            raise ValueError("unknown factor dimension")
        if len([q for q in questions.values() if q.get("factor_id") == factor["factor_id"]]) != 3:
            raise ValueError("each source factor must retain its three original questions")
        if factor.get("assessment_content") != factor.get("source_cells", {}).get(
                factor.get("assessment_content_source_cell")):
            raise ValueError("factor requirements differ from original cell")

    compiled_questions, compiled_branches, seen_branches = [], [], set()
    factor_requirements: dict[str, list[dict]] = {}
    for qid, question in questions.items():
        factor = factors.get(question.get("factor_id"))
        if factor is None or question.get("dimension_id") != factor["dimension_id"]:
            raise ValueError("question factor/dimension mismatch")
        raw = factor["source_cells"].get(question.get("source_cell"))
        if (not isinstance(raw, str) or raw != question.get("source_raw_text")
                or not question.get("stem") or question["stem"] not in raw):
            raise ValueError("question no longer matches its original source cell")
        rubric = question_rubrics[qid]
        if hashlib.sha256(question["stem"].encode("utf-8")).hexdigest() != rubric.get("source_stem_sha256"):
            raise ValueError("rubric does not match the original question text")
        if rubric.get("status") != "proposed":
            raise ValueError("this rubric version is an unapproved draft")
        _source_refs(rubric.get("source_refs"), question, factor)
        requirements = _requirements(factor, question["source_file_hash"])
        factor_requirements[factor["factor_id"]] = requirements
        requirement_ids = {x["requirement_id"] for x in requirements}
        criteria = _index(rubric.get("criteria"), "criterion_id", "criteria")
        for cid, criterion in criteria.items():
            if not cid.startswith(qid + ":") or not criterion.get("name") or not criterion.get("description"):
                raise ValueError("criterion must be explicitly scoped to its question")
            if criterion.get("status") != "proposed" or criterion.get("scoring_scope") != "answer_content_only":
                raise ValueError("criteria must be proposed content-only mappings")
            mapped = criterion.get("requirement_ids")
            if not isinstance(mapped, list) or not mapped or not set(mapped).issubset(requirement_ids):
                raise ValueError("criterion maps to unknown factor requirements")
            _source_refs(criterion.get("source_refs"), question, factor)
        if set(rubric.get("anchors", {})) != {"1", "2", "3", "4", "5"} or not all(
                isinstance(x, str) and x.strip() for x in rubric["anchors"].values()):
            raise ValueError("question needs all five draft scoring anchors")
        branch_ids = question.get("branch_ids", [])
        if len(branch_ids) != len(set(branch_ids)) or set(rubric.get("branches", {})) != set(branch_ids):
            raise ValueError("question followup mappings must match original branches")
        question_rules = {}
        for bid in branch_ids:
            original_branch = branches.get(bid)
            if original_branch is None or original_branch.get("parent_question_id") != qid or bid in seen_branches:
                raise ValueError("followup parent does not match original question")
            if (original_branch.get("source_cell") != question["source_cell"]
                    or original_branch.get("source_raw_text") != raw
                    or not original_branch.get("template")
                    or original_branch["template"] not in question.get("original_followup_text", "")
                    or original_branch.get("source_file_hash") != question["source_file_hash"]):
                raise ValueError("followup no longer matches original source")
            mapped = rubric["branches"][bid]
            _source_refs(mapped.get("source_refs"), question, factor)
            ids = mapped.get("criterion_ids")
            rule = mapped.get("rule", {})
            if (not isinstance(ids, list) or not ids or not set(ids).issubset(criteria)
                    or not mapped.get("target_evidence_text")
                    or rule.get("criterion_id") not in ids
                    or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", rule.get("gap_type", ""))):
                raise ValueError("followup lacks explicit criterion target")
            if rule.get("prerequisite_criterion_id") and rule["prerequisite_criterion_id"] not in criteria:
                raise ValueError("followup prerequisite must belong to the same question")
            cues, patterns = rule.get("lexical_cues"), rule.get("lexical_patterns")
            if (not isinstance(cues, list) or not 1 <= len(cues) <= 32
                    or any(not isinstance(c, str) or not 1 <= len(c) <= 64 for c in cues)
                    or patterns != [re.escape(c) for c in cues]):
                raise ValueError("followup patterns must be safe escaped literal cues")
            if rule.get("absence_interpretation") != "unresolved_not_inability":
                raise ValueError("cue absence must not imply inability")
            branch = copy.deepcopy(original_branch)
            branch.update(copy.deepcopy(mapped))
            branch.update({"text": original_branch["template"],
                           "target_evidence": mapped["target_evidence_text"],
                           "priority": original_branch["source_followup_index"],
                           "review_status": "proposed_dev", "business_approval_status": "not_reviewed"})
            compiled_branches.append(branch)
            question_rules[bid] = copy.deepcopy(rule)
            seen_branches.add(bid)
        compiled = copy.deepcopy(question)
        compiled.update({"text": question["stem"], "anchors": copy.deepcopy(rubric["anchors"]),
                         "criteria": copy.deepcopy(list(criteria.values())),
                         "rubric_id": rubric["rubric_id"], "rubric_version": rubrics["rubric_version"],
                         "rubric_status": "proposed", "rubric_source_refs": copy.deepcopy(rubric["source_refs"]),
                         "factor_requirements": copy.deepcopy(requirements),
                         "followup_rules": question_rules, "activation_status": "proposed_dev",
                         "business_approval_status": "not_reviewed"})
        compiled_questions.append(compiled)
    if seen_branches != set(branches):
        raise ValueError("orphaned original followup")
    return {"bank_version": bank["bank_version"], "rubric_version": rubrics["rubric_version"],
            "bank_sha256": _digest(bank), "rubrics_sha256": _digest(rubrics),
            "dimensions": copy.deepcopy(list(dimensions.values())),
            "factors": copy.deepcopy(list(factors.values())),
            "factor_requirements": copy.deepcopy(factor_requirements),
            "questions": compiled_questions, "branches": compiled_branches,
            "status": "proposed", "scoring_policy": copy.deepcopy(rubrics["scoring_policy"])}


def _offset(seed: str, label: str) -> int:
    return int(hashlib.sha256((seed + "\0" + label).encode("utf-8")).hexdigest()[:16], 16)


def build_form(base_config: dict, bank: dict | None = None, mode: str = "quick",
               seed: str | int = "default", *, rotation_index: int = 0,
               fixed_question_ids: list[str] | None = None, rubrics: dict | None = None) -> dict:
    """Return an independent frozen config, preserving the caller's policy_version.

    Quick rotates factors within each of three dimensions, then questions within
    each factor. Full rotates each factor's three source questions. The caller
    supplies/stores rotation_index; reopening a session must use its saved config.
    Fixed lists are for explicit research comparisons and must meet coverage rules.
    """
    if not isinstance(base_config, dict):
        raise ValueError("base_config must be an object")
    if mode not in {"quick", "full"}:
        raise ValueError("mode must be quick or full")
    if isinstance(seed, bool) or not isinstance(seed, (str, int)) or not 1 <= len(str(seed)) <= 256:
        raise ValueError("seed must be a nonempty string or integer of at most 256 characters")
    if isinstance(rotation_index, bool) or not isinstance(rotation_index, int) or not 0 <= rotation_index <= 10**9:
        raise ValueError("rotation_index must be a nonnegative integer <= 1000000000")
    seed = str(seed)
    compiled = compile_bank(bank, rubrics)
    questions = {q["question_id"]: q for q in compiled["questions"]}
    factors = compiled["factors"]
    selected_ids = []
    if fixed_question_ids is not None:
        if (not isinstance(fixed_question_ids, list)
                or any(not isinstance(qid, str) for qid in fixed_question_ids)
                or len(fixed_question_ids) != len(set(fixed_question_ids))
                or any(qid not in questions for qid in fixed_question_ids)):
            raise ValueError("fixed_question_ids must contain distinct known question IDs")
        selected_ids = list(fixed_question_ids)
    elif mode == "full":
        for factor in factors:
            choices = [q for q in compiled["questions"] if q["factor_id"] == factor["factor_id"]]
            selected_ids.append(choices[(_offset(seed, factor["factor_id"]) + rotation_index) % len(choices)]["question_id"])
    else:
        for dimension in compiled["dimensions"]:
            dimension_factors = [f for f in factors if f["dimension_id"] == dimension["dimension_id"]]
            factor = dimension_factors[(_offset(seed, dimension["dimension_id"]) + rotation_index) % len(dimension_factors)]
            choices = [q for q in compiled["questions"] if q["factor_id"] == factor["factor_id"]]
            selected_ids.append(choices[(_offset(seed, factor["factor_id"]) + rotation_index // len(dimension_factors)) % len(choices)]["question_id"])
    selected = [questions[qid] for qid in selected_ids]
    expected_count = 3 if mode == "quick" else 8
    coverage_key = "dimension_id" if mode == "quick" else "factor_id"
    if len(selected) != expected_count or len({q[coverage_key] for q in selected}) != expected_count:
        raise ValueError("quick requires three distinct dimensions; full requires eight distinct factors")
    covered = [f["factor_id"] for f in factors if any(q["factor_id"] == f["factor_id"] for q in selected)]
    metadata = {"form_version": FORM_VERSION, "mode": mode, "seed": seed,
                "rotation_index": rotation_index, "selection_method": "fixed" if fixed_question_ids is not None else "sha256_stratified_rotation",
                "selected_question_ids": list(selected_ids), "covered_factor_ids": covered,
                "unmeasured_factor_ids": [f["factor_id"] for f in factors if f["factor_id"] not in covered],
                "covered_dimension_ids": [d["dimension_id"] for d in compiled["dimensions"]],
                "bank_version": compiled["bank_version"], "rubric_version": compiled["rubric_version"],
                "bank_sha256": compiled["bank_sha256"], "rubrics_sha256": compiled["rubrics_sha256"],
                "status": "proposed", "business_approval_status": "not_reviewed",
                "forms_are_equated": False, "freeze_policy": "persist_complete_config_at_session_creation",
                "interpretation": "抽到某因子的一题仅表示本场初步覆盖；未问要求、无引用证据及技术失败均不推定不满足。不同卷尚未做等值验证。"}
    metadata["form_id"] = "form_" + _digest(metadata)[:24]
    selected_branches = [copy.deepcopy(b) for b in compiled["branches"] if b["parent_question_id"] in selected_ids]
    for branch in selected_branches:
        # Legacy imported stop/enabled fields are historical provenance. Runtime
        # budgets come solely from this frozen form, including a full form's four.
        branch["current_form_policy"] = {
            "form_id": metadata["form_id"], "form_version": FORM_VERSION, "mode": mode,
            "max_followup": 1 if mode == "quick" else 4, "max_followup_per_main": 1,
            "legacy_activation_metadata_is_authoritative": False,
        }
    result = copy.deepcopy(base_config)
    result.update({"max_main": expected_count, "max_followup": 1 if mode == "quick" else 4,
                   "max_followup_per_main": 1, "max_total": 4 if mode == "quick" else 12,
                   "questions": copy.deepcopy(selected),
                   "branches": selected_branches,
                   "factor_ids": covered, "form": metadata,
                   "factor_requirements": copy.deepcopy(compiled["factor_requirements"]),
                   "rubric_status": "proposed", "form_scoring_policy": copy.deepcopy(compiled["scoring_policy"])})
    return result
