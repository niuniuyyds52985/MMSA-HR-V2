"""Trial-audio quality gates. Generated signals are never candidate answers."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave

import numpy as np

from src.mmsa_v2.preflight import inspect_trial, MAX_BYTES


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "trial.wav"
        # Deterministic broad-band fixture, not a human voice or ASR benchmark.
        self.signal = np.random.default_rng(2).normal(0, .06, 80000)

    def save(self, signal):
        with wave.open(str(self.path), "wb") as output:
            output.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            output.writeframes((np.clip(signal, -.9999, .9999) * 32768).astype("<i2").tobytes())
        return self.path

    def test_silent_and_hum_are_not_sent_to_asr(self):
        with patch("src.mmsa_v2.preflight._transcribe") as asr:
            silence = inspect_trial(self.save(np.zeros(80000)))
            self.assertEqual(silence["status"], "fail")
            self.assertIn("near_silence", silence["codes"])
            hum = .03 * np.sin(2 * np.pi * 67 * np.arange(80000) / 16000)
            result = inspect_trial(self.save(hum))
            self.assertEqual(result["status"], "fail")
            self.assertIn("low_frequency_noise", result["codes"])
            self.assertGreater(result["metrics"]["low_frequency_ratio"], .9)
            asr.assert_not_called()

    def test_non_silent_pure_tone_is_not_verified_speech(self):
        tone = .12 * np.sin(2 * np.pi * 1000 * np.arange(80000) / 16000)
        with patch("src.mmsa_v2.preflight._transcribe") as asr:
            result = inspect_trial(self.save(tone))
        self.assertIn("narrowband_tone", result["codes"])
        self.assertGreater(result["metrics"]["rms_dbfs"], -30)
        self.assertEqual(result["status"], "fail")
        asr.assert_not_called()

    def test_clipped_track_cannot_pass(self):
        track = np.sign(np.sin(2 * np.pi * 300 * np.arange(80000) / 16000))
        with patch("src.mmsa_v2.preflight._transcribe") as asr:
            result = inspect_trial(self.save(track))
        self.assertIn("clipping", result["codes"])
        asr.assert_not_called()

    def test_empty_local_transcript_is_a_failed_trial(self):
        with patch("src.mmsa_v2.preflight._transcribe", return_value={"text": "", "segments": []}):
            result = inspect_trial(self.save(self.signal))
        self.assertEqual(result["status"], "fail")
        self.assertIn("empty_transcription", result["codes"])
        self.assertFalse(result["network_used"])
        self.assertFalse(result["scored"])

    def test_recognized_text_still_requires_listener_confirmation(self):
        with patch("src.mmsa_v2.preflight._transcribe", return_value={"text": "这是录音测试", "provenance": {"engine": "fixture"}}):
            result = inspect_trial(self.save(self.signal))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["transcript"], "这是录音测试")
        self.assertIn("回放", result["messages"][-1])
        self.assertTrue(result["local_only"])

    def test_asr_unavailable_is_not_presented_as_verified_text(self):
        with patch("src.mmsa_v2.preflight._transcribe", side_effect=RuntimeError("secret path should not appear")):
            result = inspect_trial(self.save(self.signal))
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["transcript"], "")
        self.assertIn("local_asr_unavailable", result["codes"])
        self.assertNotIn("secret", str(result))

    def test_trial_limits_and_no_audio(self):
        with self.assertRaisesRegex(ValueError, "最长 15"):
            inspect_trial(self.save(np.zeros(16 * 16000)))
        self.path.write_bytes(bytes(MAX_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "5 MB"):
            inspect_trial(self.path)
        self.save(np.zeros(16000))
        short = inspect_trial(self.path)
        self.assertEqual(short["codes"], ["too_short"])
        with patch("src.mmsa_v2.preflight.media_info", return_value={"duration_seconds":5, "has_audio":False}):
            no_audio = inspect_trial(self.path)
        self.assertEqual(no_audio["codes"], ["no_audio"])


if __name__ == "__main__":
    unittest.main()
