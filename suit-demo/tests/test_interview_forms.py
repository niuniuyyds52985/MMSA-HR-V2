"""Source, coverage and freezing contracts; no participant or cloud data."""
import copy
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_forms import build_form, compile_bank, load_bank, load_rubrics


class InterviewFormTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bank = load_bank()
        cls.rubrics = load_rubrics()
        cls.compiled = compile_bank(cls.bank, cls.rubrics)
        cls.base = {"version": "interview.v4", "policy_version": "semantic-bank.v1",
                    "analysis_plan": {"audio": ["vad", "pitch"]},
                    "questions": [{"question_id": "obsolete"}], "branches": []}

    def form(self, mode="quick", **kwargs):
        return build_form(self.base, self.bank, mode, rubrics=self.rubrics, **kwargs)

    def test_all_original_questions_and_followups_are_preserved(self):
        self.assertEqual((len(self.compiled["questions"]), len(self.compiled["branches"])), (24, 36))
        original_questions = {q["question_id"]: q for q in self.bank["questions"]}
        original_branches = {b["branch_id"]: b for b in self.bank["branches"]}
        self.assertEqual(set(original_questions), {q["question_id"] for q in self.compiled["questions"]})
        self.assertEqual(set(original_branches), {b["branch_id"] for b in self.compiled["branches"]})
        for q in self.compiled["questions"]:
            original = original_questions[q["question_id"]]
            for key in ("source_file_hash", "source_cell", "source_sheet", "source_raw_text", "branch_ids"):
                self.assertEqual(q[key], original[key])
            self.assertEqual(q["text"], original["stem"])
        for b in self.compiled["branches"]:
            original = original_branches[b["branch_id"]]
            self.assertEqual(b["text"], original["template"])
            self.assertEqual(b["parent_question_id"], original["parent_question_id"])
            self.assertEqual(b["source_raw_text"], original["source_raw_text"])

    def test_criteria_requirements_sources_and_draft_status(self):
        criterion_ids = []
        for q in self.compiled["questions"]:
            self.assertEqual(len(q["criteria"]), 4)
            self.assertEqual(set(q["anchors"]), {"1", "2", "3", "4", "5"})
            self.assertEqual(q["rubric_status"], "proposed")
            reqs = {r["requirement_id"] for r in q["factor_requirements"]}
            for c in q["criteria"]:
                criterion_ids.append(c["criterion_id"])
                self.assertTrue(c["criterion_id"].startswith(q["question_id"] + ":"))
                self.assertTrue(c["description"])
                self.assertEqual(c["status"], "proposed")
                self.assertEqual(c["scoring_scope"], "answer_content_only")
                self.assertTrue(c["requirement_ids"])
                self.assertTrue(set(c["requirement_ids"]).issubset(reqs))
                self.assertIn(q["source_cell"], [ref["source_cell"] for ref in c["source_refs"]])
        self.assertEqual(len(set(criterion_ids)), 96)
        self.assertEqual(len(self.compiled["factor_requirements"]), 8)

    def test_each_branch_has_a_specific_same_parent_rule(self):
        questions = {q["question_id"]: q for q in self.compiled["questions"]}
        for b in self.compiled["branches"]:
            q = questions[b["parent_question_id"]]
            cids = {c["criterion_id"] for c in q["criteria"]}
            self.assertTrue(set(b["criterion_ids"]).issubset(cids))
            rule = b["rule"]
            self.assertEqual(rule, q["followup_rules"][b["branch_id"]])
            self.assertIn(rule["criterion_id"], b["criterion_ids"])
            self.assertTrue(b["target_evidence_text"])
            self.assertTrue(rule["gap_type"])
            for pattern, cue in zip(rule["lexical_patterns"], rule["lexical_cues"]):
                self.assertEqual(pattern, re.escape(cue))
                self.assertIsNotNone(re.search(pattern, "原话：" + cue))
            self.assertEqual(rule["absence_interpretation"], "unresolved_not_inability")
            if rule.get("prerequisite_criterion_id"):
                self.assertIn(rule["prerequisite_criterion_id"], cids)

    def test_old_three_rubric_identifiers_remain_compatible(self):
        expected = {
            "leadership_F3_Q1": {"initiative", "coordination", "outcome_cost", "reflection"},
            "results_K6": {"goal_constraint", "bottleneck_options", "plan_checkpoints", "adjustment_risk"},
            "learning_F10_Q2": {"experience", "method", "transfer", "effect_boundary"},
        }
        for q in self.compiled["questions"]:
            if q["question_id"] in expected:
                self.assertEqual({c["criterion_id"].split(":")[-1] for c in q["criteria"]}, expected[q["question_id"]])

    def test_quick_is_stratified_and_full_covers_all_factors(self):
        quick, full = self.form("quick"), self.form("full")
        self.assertEqual(len({q["dimension_id"] for q in quick["questions"]}), 3)
        self.assertEqual(len({q["factor_id"] for q in full["questions"]}), 8)
        self.assertEqual((quick["max_main"], quick["max_followup"], quick["max_total"]), (3, 1, 4))
        self.assertEqual((full["max_main"], full["max_followup"], full["max_total"]), (8, 4, 12))
        for form in (quick, full):
            self.assertEqual(form["max_followup_per_main"], 1)
            self.assertEqual(form["policy_version"], "semantic-bank.v1")
            self.assertEqual(form["version"], "interview.v4")
            self.assertFalse(form["form"]["forms_are_equated"])
            selected_ids = set(form["form"]["selected_question_ids"])
            self.assertTrue(all(b["parent_question_id"] in selected_ids for b in form["branches"]))
            for branch in form["branches"]:
                self.assertEqual(branch["current_form_policy"]["max_followup"], form["max_followup"])
                self.assertEqual(branch["current_form_policy"]["max_followup_per_main"], 1)
                self.assertEqual(branch["current_form_policy"]["form_id"], form["form"]["form_id"])
                self.assertFalse(branch["current_form_policy"]["legacy_activation_metadata_is_authoritative"])
            self.assertEqual(form["form"]["status"], "proposed")
        self.assertEqual(len(quick["form"]["unmeasured_factor_ids"]), 5)
        self.assertEqual(full["form"]["unmeasured_factor_ids"], [])

    def test_all_24_are_reachable_in_both_modes(self):
        all_ids = {q["question_id"] for q in self.bank["questions"]}
        for seed in ("research-A", "中文固定种子", 17):
            for mode, rotations in (("quick", 9), ("full", 3)):
                reached = set()
                for index in range(rotations):
                    reached.update(self.form(mode, seed=seed, rotation_index=index)["form"]["selected_question_ids"])
                self.assertEqual(reached, all_ids, (seed, mode))

    def test_full_each_factor_question_rotates_without_repeating(self):
        forms = [self.form("full", seed="experiment", rotation_index=i) for i in range(3)]
        for factor in self.bank["factors"]:
            ids = [next(q["question_id"] for q in form["questions"] if q["factor_id"] == factor["factor_id"]) for form in forms]
            self.assertEqual(len(set(ids)), 3)

    def test_reproducibility_and_independent_snapshots(self):
        base_before, bank_before, rubric_before = copy.deepcopy(self.base), copy.deepcopy(self.bank), copy.deepcopy(self.rubrics)
        first = self.form(seed="same", rotation_index=7)
        second = self.form(seed="same", rotation_index=7)
        self.assertEqual(first, second)
        self.assertNotEqual(first["form"]["form_id"], self.form(seed="same", rotation_index=8)["form"]["form_id"])
        first["questions"][0]["criteria"][0]["description"] = "changed"
        first["factor_requirements"]["leadership"][0]["text"] = "changed"
        first["analysis_plan"]["audio"].append("changed")
        self.assertEqual(self.base, base_before)
        self.assertEqual(self.bank, bank_before)
        self.assertEqual(self.rubrics, rubric_before)
        self.assertEqual(second, self.form(seed="same", rotation_index=7))
        self.assertEqual(second, json.loads(json.dumps(second, ensure_ascii=False)))

    def test_fixed_forms_require_exact_mode_coverage(self):
        quick_ids = ["leadership_F3_Q1", "results_K6", "learning_F10_Q2"]
        fixed = self.form(fixed_question_ids=quick_ids)
        self.assertEqual(fixed["form"]["selected_question_ids"], quick_ids)
        self.assertEqual(fixed["form"]["selection_method"], "fixed")
        full_ids = self.form("full")["form"]["selected_question_ids"]
        self.assertEqual(self.form("full", fixed_question_ids=full_ids)["form"]["selected_question_ids"], full_ids)
        for ids in ([], quick_ids[:2], [quick_ids[0]] * 3, ["unknown"] + quick_ids[1:],
                    ["leadership_F3_Q1", "develop_others_F4_Q1", "learning_F10_Q2"]):
            with self.assertRaises(ValueError):
                self.form(fixed_question_ids=ids)
        full_ids[1] = "leadership_F3_Q2" if full_ids[0] != "leadership_F3_Q2" else "leadership_K3"
        with self.assertRaises(ValueError):
            self.form("full", fixed_question_ids=full_ids)

    def test_invalid_parameters_are_rejected(self):
        for kwargs in ({"mode": "other"}, {"seed": None}, {"seed": True}, {"seed": 1.5},
                       {"seed": ""}, {"seed": "x" * 257}, {"rotation_index": -1},
                       {"rotation_index": True}, {"rotation_index": 0.5}, {"rotation_index": 10**9 + 1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.form(**kwargs)

    def test_invalid_source_and_orphaned_followups_are_rejected(self):
        for mutate in (
            lambda b: b["questions"][0].update(stem="invented question"),
            lambda b: b["branches"][0].update(parent_question_id="results_K6"),
            lambda b: b["branches"][0].update(template="invented followup"),
            lambda b: b["questions"][0].update(source_raw_text="changed"),
            lambda b: b["factors"][0].update(assessment_content="changed"),
        ):
            bank = copy.deepcopy(self.bank)
            mutate(bank)
            with self.assertRaises(ValueError):
                compile_bank(bank, self.rubrics)

    def test_incomplete_or_unsafe_rubric_mappings_are_rejected(self):
        qid, bid = "leadership_F3_Q1", "leadership_F3_Q1_action"
        for mutate in (
            lambda r: r["questions"].pop(qid),
            lambda r: r["questions"][qid]["criteria"][0].update(requirement_ids=["other"]),
            lambda r: r["questions"][qid]["criteria"][0].update(status="approved"),
            lambda r: r["questions"][qid]["branches"][bid]["rule"].update(criterion_id="results_K6:goal_constraint"),
            lambda r: r["questions"][qid]["branches"][bid]["rule"].update(lexical_patterns=["(a+)+$"]),
            lambda r: r["questions"][qid]["branches"][bid]["rule"].update(prerequisite_criterion_id="other"),
        ):
            rubric = copy.deepcopy(self.rubrics)
            mutate(rubric)
            with self.assertRaises(ValueError):
                compile_bank(self.bank, rubric)

    def test_no_default_score_or_coverage_claim_from_cues_or_modalities(self):
        policy = self.compiled["scoring_policy"]
        self.assertEqual(policy["missing_evidence"], "unassessed")
        self.assertTrue(policy["lexical_cues_are_not_scores"])
        self.assertTrue(policy["voice_face_gaze_excluded_from_score"])
        self.assertTrue(policy["failure_result_is_not_low_score"])
        self.assertIsNone(policy["approved_threshold"])
        q = next(q for q in self.compiled["questions"] if q["question_id"] == "adaptability_F8_Q1")
        mapped = {rid for c in q["criteria"] for rid in c["requirement_ids"]}
        self.assertEqual(mapped, {"factor:adaptability:E8:1"})
        self.assertEqual(len(q["factor_requirements"]), 3)


if __name__ == "__main__":
    unittest.main()
