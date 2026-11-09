"""Production AU/SER routing and caches; no production files or cloud calls."""
import copy
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.mmsa_v2 import auto_media
from src.mmsa_v2 import speech_emotion
from src.mmsa_v2.state_rules import METHOD, LEGACY_METHOD


def raw_modules(duration=400):
    aus = {f"AU{x:02d}": 0.0 for x in (1, 2, 4, 5, 6, 7, 9, 10, 12, 15, 23, 26)}
    aus.update(AU06=1.0, AU12=3.0)
    presence = {key: int(value > 0) for key, value in aus.items()}
    au = [{"start_ms": start, "end_ms": min(start + 200, duration), "quality": "valid",
           "quality_flags": [], "values": {"intensity": aus, "presence": presence}}
          for start in range(0, duration, 200)]
    return {"au": {"status": "complete", "observations": au},
            "expression": {"status": "complete", "observations": [
                {"start_ms": 0, "end_ms": duration, "quality": "valid",
                 "values": {"label": "disgust", "raw_scores": {"disgust": 99, "happy": -99}}}]},
            "audio": {"status": "complete", "observations": [
                {"start_ms": 0, "end_ms": duration, "quality": "valid",
                 "values": {"rms_dbfs": -28, "speech_fraction": 1}}],
                "summary": {"speech_intervals": [{"start_ms": 0, "end_ms": duration}],
                            "speech_ms": duration}}}


def speech_module(sha, duration=400):
    return {"model_id": speech_emotion.MODEL_ID, "method": speech_emotion.METHOD,
            "version": speech_emotion.METHOD, "duration_ms": duration, "status": "complete",
            "provenance": {"media_sha256": sha, "weights_sha256": speech_emotion.FILES["model.pt"]},
            "observations": [{"start_ms": 0, "end_ms": duration, "quality": "valid",
                              "values": {"label": "neutral", "scores": {"neutral": .9}}}]}


class AffectPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "fixture.webm"
        self.path.write_bytes(b"not decoded: inference is isolated in this contract test")
        self.sha = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.info = {"duration_seconds": .4, "has_audio": True, "has_video": True}
        self.answer = {"media": {"sha256": self.sha, "duration_seconds": .4},
            "processing_transcribe": True, "transcript": "已有原话保留。",
            "state_estimate": {"method": LEGACY_METHOD, "duration_seconds": .4, "media_sha256": self.sha},
            "processing": {"media_sha256": self.sha, "duration_seconds": .4,
                "v1": {"media_sha256": self.sha, "duration_ms": 400, "modules": raw_modules()}}}

    def analyze(self, answer=None):
        with patch.object(auto_media, "media_info", return_value=self.info), \
             patch.object(auto_media, "analyze_recording", side_effect=AssertionError("Do not repeat ASR")), \
             patch.object(auto_media, "run_multimodal", side_effect=AssertionError("Reuse complete OpenFace")), \
             patch.object(speech_emotion, "analyze_speech_emotion", return_value=speech_module(self.sha)) as voice, \
             patch.object(auto_media, "estimate_observations", side_effect=AssertionError("No legacy fallback")):
            result = auto_media.analyze_answer(self.path, answer or self.answer)
        voice.assert_called_once_with(self.path.resolve())
        return result

    def test_existing_native_observations_reuse_without_fer_veto_or_repeating_asr(self):
        before = copy.deepcopy(self.answer)
        result = self.analyze()
        self.assertEqual(self.answer, before)
        estimate = result["state_estimate"]
        self.assertEqual(estimate["method"], METHOD)
        self.assertEqual(estimate["segments"], [{"start": 0, "end": .4, "state": "洋溢"}])
        self.assertEqual(estimate["models"]["speech"]["method"], speech_emotion.METHOD)
        self.assertEqual(result["processing"]["affect"]["method_fingerprint"], auto_media.affect_method_fingerprint())
        self.assertEqual(result["transcript"], "已有原话保留。")

    def test_old_cache_cannot_masquerade_as_au_speech_inference(self):
        with self.assertRaisesRegex(ValueError, "需运行新算法"):
            auto_media.estimate_from_cached_answer(self.answer)
        result = self.analyze()
        ready = {**self.answer, **result}
        recomputed = auto_media.estimate_from_cached_answer(ready)
        self.assertEqual(recomputed, result["state_estimate"])
        for location, key, value in (("affect", "media_sha256", "wrong"),
                ("affect", "method_fingerprint", "obsolete"), ("affect", "duration_ms", 401)):
            bad = copy.deepcopy(ready)
            bad["processing"][location][key] = value
            with self.assertRaises(ValueError):
                auto_media.estimate_from_cached_answer(bad)
        bad = copy.deepcopy(ready)
        bad["processing"]["affect"]["modules"]["speech"]["provenance"]["weights_sha256"] = "wrong"
        with self.assertRaises(ValueError):
            auto_media.estimate_from_cached_answer(bad)
        with self.assertRaises(ValueError):
            auto_media.estimate_from_cached_answer({**ready, "observations": {"reviewer": "老师"}})

    def test_failure_keeps_native_cache_but_does_not_return_a_successful_estimate(self):
        with patch.object(auto_media, "media_info", return_value=self.info), \
             patch.object(auto_media, "analyze_recording", side_effect=AssertionError("No ASR")), \
             patch.object(auto_media, "run_multimodal", side_effect=AssertionError("Reuse AU")), \
             patch.object(speech_emotion, "analyze_speech_emotion", side_effect=speech_emotion.SpeechEmotionError("模型未安装")), \
             patch.object(auto_media, "_pack_estimate", side_effect=AssertionError("No legacy substitute")):
            with self.assertRaises(auto_media.AffectAnalysisError) as caught:
                auto_media.analyze_answer(self.path, self.answer)
        self.assertEqual(caught.exception.processing["v1"], self.answer["processing"]["v1"])
        self.assertEqual(caught.exception.transcript, self.answer["transcript"])
        self.assertEqual(caught.exception.provenance["status"], "failed")

    def test_stale_or_thinned_native_cache_triggers_measurement_without_requesting_fer(self):
        stale = copy.deepcopy(self.answer)
        stale["processing"]["v1"]["media_sha256"] = "old"
        run = {"status": "complete", "modules": raw_modules()}
        with patch.object(auto_media, "media_info", return_value=self.info), \
             patch.object(auto_media, "analyze_recording", side_effect=AssertionError("Transcription intact")), \
             patch.object(auto_media, "run_multimodal", return_value=run) as native, \
             patch.object(speech_emotion, "analyze_speech_emotion", return_value=speech_module(self.sha)):
            result = auto_media.analyze_answer(self.path, stale)
        self.assertNotIn("expression", native.call_args.kwargs["modules"])
        self.assertEqual(result["state_estimate"]["method"], METHOD)
        with self.assertRaises(ValueError):
            auto_media._cached_native_modules({"v1": {"media_sha256": self.sha, "duration_ms": 120001,
                "modules": raw_modules()}}, self.sha, 120001, self.info)


if __name__ == "__main__":
    unittest.main()
