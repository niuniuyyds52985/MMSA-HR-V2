import unittest

from src.mmsa_v2.visual_emotion import au_expression_evidence, PATTERNS


def row(start, end, active=(), quality="valid", flags=()):
    aus = {au for pattern in PATTERNS.values() for au in pattern} | {"AU09", "AU10"}
    return {"start_ms": start, "end_ms": end, "quality": quality, "quality_flags": list(flags),
            "values": {"presence": {au: int(au in active) for au in aus},
                       "intensity": {au: 2.5 if au in active else 0 for au in aus}}}


class VisualEmotionTests(unittest.TestCase):
    def test_au_happy_evidence_does_not_depend_on_fer(self):
        modules = {"au": {"observations": [row(0, 5000, ["AU06", "AU12"])]},
                   "expression": {"observations": [{"label": "neutral"}]}}
        result = au_expression_evidence(modules, 5000)
        happy = result["observations"][0]["values"]["patterns"]["happy"]
        self.assertEqual(happy["matched_fraction"], 1)
        self.assertEqual(happy["strength"], .5)
        self.assertTrue(result["provenance"]["no_fer_gate"])

    def test_missing_is_not_absent_and_smile_alone_is_not_happy(self):
        a = row(0, 1000, ["AU12"])
        del a["values"]["presence"]["AU06"]
        values = au_expression_evidence({"au": {"observations": [a]}}, 1000)["observations"][0]["values"]
        self.assertIsNone(values["patterns"]["happy"]["matched_fraction"])
        self.assertEqual(values["smile_motion"]["occurrence_fraction"], 1)
        self.assertEqual(values["smile_motion"]["mean_intensity"], 2.5)
        empty = au_expression_evidence({}, 1000)["observations"][0]
        self.assertEqual(empty["quality"], "missing")
        self.assertIsNone(empty["values"]["low_activation_fraction"])

    def test_duration_weighting_partial_windows_and_quality(self):
        r = au_expression_evidence({"au": {"observations": [row(0, 1000, ["AU06", "AU12"]),
            row(1000, 4000), row(4000, 5000, ["AU06", "AU12"], flags=["low_light"]),
            row(5000, 5500, ["AU01", "AU04", "AU15"])]}}, 6000)
        first, last = r["observations"]
        self.assertEqual(first["coverage"], .8)
        self.assertEqual(first["values"]["patterns"]["happy"]["matched_fraction"], .25)
        self.assertEqual(first["values"]["mean_intensity"]["AU12"], .625)
        self.assertEqual(last["coverage"], .5)
        self.assertEqual(last["values"]["patterns"]["sad"]["matched_fraction"], 1)

    def test_each_published_prototype_is_independent(self):
        rows = [row(i*1000, (i+1)*1000, aus) for i, aus in enumerate(PATTERNS.values())]
        values = au_expression_evidence({"au": {"observations": rows}}, 4000)["observations"][0]["values"]
        for name in PATTERNS:
            self.assertEqual(values["patterns"][name]["matched_fraction"], .25)

    def test_bad_intervals_do_not_inflate_coverage(self):
        with self.assertRaises(ValueError):
            au_expression_evidence({"au": {"observations": [row(0, 1000), row(500, 1500)]}}, 2000)
        with self.assertRaises(ValueError):
            au_expression_evidence({"au": {"observations": [row(0, 3000)]}}, 2000)


if __name__ == "__main__":
    unittest.main()
