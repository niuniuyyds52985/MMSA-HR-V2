"""Reviewed choice wording changes presentation, never scores or frozen sessions."""
import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.mmsa_v2 import assessment, sources
from src.mmsa_v2.api import public_session
from src.mmsa_v2.auto_content import _rules_compatible, content_draft
from src.mmsa_v2.workflow import Workflow
from tests.v2.test_auto_submission import simulation_docx


class ChoiceWordingTests(unittest.TestCase):
    def setUp(self):
        self.original = json.loads(sources.BANK_FILE.read_text(encoding="utf-8"))
        self.revisions = json.loads(sources.CHOICE_WORDING_FILE.read_text(encoding="utf-8"))

    def test_all_choices_keep_behavior_score_ids_and_cite_reviewed_rewording(self):
        bank = sources.load_bank()
        self.assertNotEqual(bank["version"], self.original["version"])
        self.assertEqual(bank["base_version"], self.original["version"])
        self.assertEqual(bank["sources"][sources.CHOICE_WORDING_SOURCE],
                         hashlib.sha256(sources.CHOICE_WORDING_FILE.read_bytes()).hexdigest())
        for old_factor, factor in zip(self.original["factors"], bank["factors"], strict=True):
            self.assertEqual({k: v for k, v in old_factor.items() if k != "questions"},
                             {k: v for k, v in factor.items() if k != "questions"})
            for old, new in zip(old_factor["questions"], factor["questions"], strict=True):
                if not old["id"].endswith("-I"):
                    self.assertEqual(old, new)
                    continue
                self.assertEqual(new["prompt"], old["prompt"])
                self.assertEqual(new["answer_key"], old["answer_key"])
                self.assertEqual(new["score_source"], old["score_source"])
                self.assertEqual([o["id"] for o in new["options"]], [o["id"] for o in old["options"]])
                self.assertIn(old["source"], new["source"])
                self.assertIn("题库等义改写", new["source"])
                audit = new["wording_provenance"]
                self.assertEqual(audit["original_source"], old["source"])
                self.assertEqual(audit["version"], self.revisions["version"])
                mapped = {o["id"]: o for o in audit["options"]}
                for original_option, option in zip(old["options"], new["options"], strict=True):
                    provenance = mapped[option["id"]]
                    self.assertEqual(provenance["original"], original_option["text"])
                    self.assertEqual(provenance["revised"], option["text"])
                    self.assertTrue(provenance["rationale"])
                    self.assertEqual(assessment.sjt_score(new["id"], option["id"], bank),
                                     assessment.sjt_score(old["id"], option["id"], self.original))
        for key in ("rules", "required_fields", "original_weights_sheet"):
            self.assertEqual(bank[key], self.original[key])

    def test_length_and_presentation_do_not_identify_high_score(self):
        for factor in sources.load_bank()["factors"]:
            question = next(q for q in factor["questions"] if q["id"].endswith("-I"))
            lengths = [len(o["text"]) for o in question["options"]]
            with self.subTest(question=question["id"]):
                self.assertLessEqual(max(lengths) / min(lengths), 1.25)
                self.assertLessEqual(max(lengths) - min(lengths), 8)
                highest = next(o for o in question["options"] if question["answer_key"][o["id"]] == 5)
                self.assertTrue(any(len(o["text"]) >= len(highest["text"])
                                    for o in question["options"] if o["id"] != highest["id"]))
                for option in question["options"]:
                    self.assertEqual(option["text"].count("；"), 1)
                    self.assertNotIn("→", option["text"])
                    self.assertNotRegex(option["text"], r"（[1-5]分）")

    def test_version_changes_with_wording_and_rejects_mismatched_originals(self):
        current = sources.load_bank()
        self.assertEqual(sources.load_bank(), current)
        with TemporaryDirectory() as folder:
            path = Path(folder) / "wording.json"
            revised = copy.deepcopy(self.revisions)
            revised["questions"][0]["options"][0]["revised"] += "（测试修订）"
            path.write_text(json.dumps(revised, ensure_ascii=False), encoding="utf-8")
            with patch.object(sources, "CHOICE_WORDING_FILE", path):
                self.assertNotEqual(sources.load_bank()["version"], current["version"])
            for field, value in (("id", "not-the-same-option"), ("original", "not the source text"), ("score", 5)):
                with self.subTest(field=field):
                    tampered = copy.deepcopy(self.revisions)
                    tampered["questions"][0]["options"][0][field] = value
                    path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
                    with patch.object(sources, "CHOICE_WORDING_FILE", path), self.assertRaises(ValueError):
                        sources.load_bank()
            tampered = copy.deepcopy(self.revisions)
            tampered["base_bank_version"] = "an-unreviewed-new-workbook"
            path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
            with patch.object(sources, "CHOICE_WORDING_FILE", path), self.assertRaises(ValueError):
                sources.load_bank()

    def test_old_session_answers_and_bank_are_frozen_and_new_session_gets_revision(self):
        with TemporaryDirectory() as folder:
            store = Workflow(folder)

            def create(label):
                batch = store.new_batch(label)
                imported = store.import_files(batch["id"], [("测试学生.docx", simulation_docx())])
                return store.create_session(batch["id"], imported["candidates"][0]["id"])

            with patch.object(sources, "load_bank", return_value=copy.deepcopy(self.original)):
                old = create("原题库测试")
            first = old["plan"][0]
            selected = first["options"][0]["id"]
            saved = store.answer(old["id"], first["id"], {"value": selected})
            old_answer = copy.deepcopy(saved["answers"][first["id"]])
            new = create("改写题库测试")
            self.assertNotEqual(old["bank_version"], new["bank_version"])
            self.assertEqual(store.session(old["id"]), saved)
            self.assertEqual(store.session(old["id"], old["version"]), old)
            self.assertEqual(store.session(old["id"])["plan"], old["plan"])
            with store.connection() as db:
                frozen = json.loads(db.execute("SELECT bank FROM sessions WHERE id=?", (old["id"],)).fetchone()[0])
            self.assertEqual(frozen, self.original)
            for question in old["plan"][1:]:
                later = store.answer(old["id"], question["id"], {"value": question["options"][0]["id"]})
            self.assertEqual(later["answers"][first["id"]], old_answer)
            self.assertEqual(later["bank_version"], self.original["version"])
            self.assertEqual(later["plan"][:5], old["plan"])
            self.assertEqual([q["id"] for q in new["plan"]], [q["id"] for q in old["plan"]])
            for original_question, revised_question in zip(old["plan"], new["plan"], strict=True):
                self.assertNotEqual(original_question["options"], revised_question["options"])
                self.assertIn("题库等义改写", revised_question["source"])
                self.assertNotIn("options", revised_question["wording_provenance"])
            public = public_session(new)
            for question in public["plan"]:
                self.assertNotIn("wording_provenance", question)
                self.assertNotIn("answer_key", question)
                self.assertNotIn("score_source", question)

    def test_offline_rules_accept_display_revision_but_reject_changed_scoring_material(self):
        current = sources.load_bank()
        self.assertTrue(_rules_compatible(self.original))
        self.assertTrue(_rules_compatible(current))
        replies = json.loads((Path(__file__).parent / "fixtures/short_oral_replies.json").read_text(encoding="utf-8"))
        question_map = {q["id"]: q for f in current["factors"] for q in f["questions"]}
        state = {"plan": [question_map[qid] for qid in replies],
                 "answers": {qid: {"transcript": text} for qid, text in replies.items()}}
        self.assertEqual(content_draft(state, current), content_draft(state, self.original))
        self.assertTrue(all(f["rating"] is not None for f in content_draft(state, current)["factors"]))
        changes = {
            "anchor": lambda b: b["factors"][0].__setitem__("anchors", "changed scoring anchor"),
            "indicator": lambda b: b["factors"][0].__setitem__("indicators", "changed indicator"),
            "oral_prompt": lambda b: b["factors"][0]["questions"][2].__setitem__("prompt", "changed G question"),
            "choice_score": lambda b: b["factors"][0]["questions"][3]["answer_key"].update({"1b732ee1dd19": 5}),
            "choice_id": lambda b: b["factors"][0]["questions"][3]["options"][0].update(id="changed option"),
            "rules": lambda b: b["rules"].update(B14="changed high-score verification"),
            "weights": lambda b: b["original_weights_sheet"].update(C6="changed experience rule"),
            "raw_cells": lambda b: b["factors"][0]["source_cells"].update(H3="changed workbook cell"),
            "source_hash": lambda b: b["sources"].update({"提问清单.xlsx": "unreviewed-source"}),
            "unrecognized_origin": lambda b: b.update(base_version="unreviewed-version"),
        }
        for label, mutate in changes.items():
            with self.subTest(change=label):
                changed = copy.deepcopy(current)
                mutate(changed)
                self.assertFalse(_rules_compatible(changed))
                draft = content_draft(state, changed)
                self.assertTrue(all(f["rating"] is None for f in draft["factors"]))
                self.assertIsNone(draft["experience_score"])
        # Even a forged original version string cannot bypass changed scoring material.
        forged = copy.deepcopy(self.original)
        forged["rules"]["B14"] = "unreviewed rule under old version label"
        self.assertFalse(_rules_compatible(forged))


if __name__ == "__main__":
    unittest.main()
