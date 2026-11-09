import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from src.mmsa_v2 import speech_emotion as speech


class SpeechEmotionTests(unittest.TestCase):
    def test_silence_is_unmeasured_without_loading_a_model(self):
        with patch("src.mmsa_v2.media.media_info", return_value={"has_audio": True, "duration_seconds": 6}), \
             patch("src.mmsa_v2.media._audio_samples", return_value=np.zeros(96000, dtype=np.int16)), \
             patch.object(speech, "predict_samples", side_effect=AssertionError("silence cannot be inferred")):
            result = speech.analyze_speech_emotion(Path(__file__))
        self.assertEqual(result["status"], "no_valid_observations")
        self.assertEqual([(r["start_ms"], r["end_ms"]) for r in result["observations"]], [(0, 5000), (5000, 6000)])
        self.assertTrue(all("scores" not in r["values"] for r in result["observations"]))
        self.assertFalse(result["provenance"]["network_used"])

    def test_vad_intervals_retain_original_boundaries(self):
        class FakeVAD:
            count = 0
            def is_speech(self, pcm, rate):
                self.count += 1
                return 3 <= self.count <= 12
        sample = np.full(16000, .1, dtype=np.float32)
        with patch("webrtcvad.Vad", return_value=FakeVAD()):
            values, spans, flags = speech._voice_quality(sample, 5000)
        self.assertEqual(spans, [{"start_ms": 5060, "end_ms": 5360}])
        self.assertEqual(values["speech_ms"], 300)
        self.assertEqual(flags, [])

    def test_no_audio_and_invalid_sample_do_not_fake_neutral(self):
        with patch("src.mmsa_v2.media.media_info", return_value={"has_audio": False, "duration_seconds": 1}), \
             patch.object(speech, "_load_model", side_effect=AssertionError("no audio cannot load a model")):
            result = speech.analyze_speech_emotion(Path(__file__))
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["status"], "no_audio")
        self.assertEqual(result["duration_ms"], 1000)
        self.assertEqual(result["method"], speech.METHOD)
        self.assertEqual(len(result["provenance"]["media_sha256"]), 64)
        with self.assertRaises(speech.SpeechEmotionError):
            speech.predict_samples(np.full(16000, float("nan")))


if __name__ == "__main__":
    unittest.main()
