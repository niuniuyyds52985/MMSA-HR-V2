import unittest

from src.mmsa_v2.auto_media import _combine, estimate_legacy_from_cached_answer, estimate_observations
from src.mmsa_v2.state_rules import LEGACY_METHOD, METHOD_V1


def observed(start, end, values):
    return dict(start_ms=start, end_ms=end, quality="valid", quality_flags=[],
                track_segment_id="visible-face-run-001", values=values)


def measurements(label="happy", up=2, cheek=0.7, down=0):
    return {"expression":{"observations":[observed(0,200,{"label":label,"raw_scores":{label:2.0,"other":1.0}})]},
            "au":{"observations":[observed(0,200,{"intensity":{"AU12":up,"AU06":cheek,"AU15":down}})]},
            "audio":{"observations":[observed(0,1000,{"rms_dbfs":-28,"speech_fraction":0.51,"f0_hz":189})]}}


class AutoMediaTests(unittest.TestCase):
    def test_observable_states_require_matching_signals(self):
        choices = [("happy", 2, 0.7, 0, "饱满"), ("happy", 3, 2, 0.2, "洋溢"),
                   ("neutral", 0,0,0, "平和"), ("sad",0.2,0,1.8, "略微 down"),
                   ("sad",0.1,0,3, "非常不好")]
        for label,up,cheek,down,state in choices:
            with self.subTest(state):
                segments, evidence = estimate_observations(measurements(label,up,cheek,down),150)
                self.assertEqual(segments,[dict(start=0,end=0.15,state=state)])
                self.assertEqual(evidence[0]["values"]["AU12"],up)
        for label in ("angry","surprised","disgust","fearful"):
            self.assertFalse(estimate_observations(measurements(label,4,4,0),1000)[0])

    def test_missing_quality_audio_or_coincident_frame(self):
        tied = measurements()
        tied["expression"]["observations"][0]["values"]["raw_scores"]["other"]=1.99
        self.assertFalse(estimate_observations(tied,1000)[0])
        for key,field,value in [("au","start_ms",20),("expression","quality","missing"),
                                ("au","track_segment_id","another"),("audio","quality_flags",["audio_clipping"]),
                                ("audio","start_ms",100)]:
            modules=measurements()
            modules[key]["observations"][0][field]=value
            self.assertEqual(estimate_observations(modules,1000)[0],[])
        self.assertEqual(estimate_observations({"audio":measurements()["audio"]},1000),([],[]))

    def test_relaxed_smile_thresholds_keep_an_independent_gesture_and_quality_gate(self):
        for label, up, cheek, down, expected in [
            ("happy", 0.75, 0, 0.4, "饱满"), ("happy", 0.749, 2, 0, None),
            ("happy", 0.75, 1, 0.75, None), ("happy", 2.25, 0.75, 0, "洋溢"),
            ("happy", 2.25, 0.749, 0, "饱满"), ("happy", 2.249, 0.75, 0, "饱满"),
            ("neutral", 2.25, 2, 0.8, "平和"), ("sad", 0, 0.8, 0.75, None),
        ]:
            with self.subTest(label=label, up=up, cheek=cheek, down=down):
                _, rows = estimate_observations(measurements(label, up, cheek, down), 1000)
                self.assertEqual(rows[0]["state"], expected)

    def test_reclassification_uses_full_original_measurements_and_preserves_annotations(self):
        previous = {"method": METHOD_V1, "media_sha256": "source", "duration_seconds": 1,
                    "quality": {"sample_count": 1}}
        audit = {"duration_ms": 1000, "media_sha256": "source", "modules": measurements(up=0.85)}
        answer = {"media": {"sha256": "source"}, "processing": {"v1": audit}, "state_estimate": previous}
        updated = estimate_legacy_from_cached_answer(answer)
        self.assertEqual(updated["method"], LEGACY_METHOD)
        self.assertEqual(updated["quality"]["valid_seconds"], 0.2)
        self.assertEqual(updated["segments"][0]["state"], "饱满")
        self.assertEqual(answer["state_estimate"]["method"], METHOD_V1)
        for overrides in [
            {"media": {"sha256": "another"}},
            {"observations": {"segments": [], "reviewer": "老师"}},
            {"state_estimate": {**previous, "quality": {"sample_count": 2}}},
            {"processing": {"v1": {**audit, "modules": {"audio": {}}}}},
            {"processing": {"v1": {**audit, "duration_ms": 160000}},
             "state_estimate": {**previous, "duration_seconds": 160}},
        ]:
            with self.assertRaises(ValueError):
                estimate_legacy_from_cached_answer({**answer, **overrides})

    def test_long_clip_clamps_offsets_in_measurements_audio_and_geometry(self):
        modules=measurements()
        for mod in modules.values():
            mod["status"]="complete"
        modules["gaze"]={"status":"complete","observations":[],"geometry":{"observations":[
            {"start_ms":0,"end_ms":200,"quality":"valid","landmarks":[[0.3,0.4]]*68}]}}
        modules["audio"]["summary"]={"speech_intervals":[{"start_ms":0,"end_ms":1000}],"speech_ms":800}
        run={"modules":modules}
        combined=_combine([(0,120000,run),(120000,120125,run)],120125,"")
        geom=combined["gaze"]["geometry"]["observations"][-1]
        self.assertEqual((geom["start_ms"],geom["end_ms"],len(geom["landmarks"])),(120000,120125,68))
        self.assertEqual(combined["audio"]["summary"]["speech_intervals"][-1]["end_ms"],120125)
        self.assertEqual(combined["audio"]["observations"][-1]["start_ms"],120000)
