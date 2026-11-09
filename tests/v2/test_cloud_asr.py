import base64
from hashlib import sha256
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np
import requests

from src.mmsa_v2 import cloud_asr as asr


class CloudASRTests(unittest.TestCase):
    def response(self, text="这是我的回答。", **overrides):
        value = {"choices": [{"finish_reason": "stop", "message": {"content": text}}],
                 "id": "chatcmpl-test", "model": asr.MODEL,
                 "usage": {"seconds": 1, "total_tokens": 25, "unexpected": "private"}}
        value.update(overrides)
        return Mock(status_code=200, content=b"{}", json=Mock(return_value=value))

    def test_audio_only_request_and_safe_provenance(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "answer.webm"
            path.write_bytes(b"video-container-local-only")
            media_hash = sha256(path.read_bytes()).hexdigest()
            with patch.object(asr, "media_info", return_value={"has_audio": True, "duration_seconds": 1}), \
                    patch.object(asr, "_audio_samples", return_value=np.arange(16000, dtype="<i2")), \
                    patch.object(asr, "public_settings", return_value={"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"}), \
                    patch.object(asr, "get_api_key", return_value="mock-secret"), \
                    patch.object(asr.requests, "post", return_value=self.response()) as post:
                result = asr.transcribe_answer(path, {"media": {"sha256": media_hash}})
            self.assertEqual(result["status"], "success")
            self.assertEqual(result["transcript"], "这是我的回答。")
            self.assertEqual(result["provenance"]["media_sha256"], media_hash)
            self.assertFalse(result["provenance"]["word_timestamps_available"])
            self.assertNotIn("mock-secret", str(result))
            self.assertNotIn("private", str(result))
            args = post.call_args.kwargs
            self.assertFalse(args["allow_redirects"])
            self.assertEqual(args["timeout"], (8, 120))
            body = args["json"]
            self.assertEqual(len(body["messages"]), 1)
            self.assertEqual(set(body["messages"][0]["content"][0]), {"type", "input_audio"})
            data = body["messages"][0]["content"][0]["input_audio"]["data"]
            wav = base64.b64decode(data.split(",", 1)[1])
            self.assertNotIn(b"video-container-local-only", wav)
            with wave.open(io.BytesIO(wav)) as recording:
                self.assertEqual((recording.getnchannels(), recording.getframerate(), recording.getsampwidth()), (1, 16000, 2))

    def test_empty_is_not_successful_content_or_invented_alignment(self):
        with patch.object(asr.requests, "post", return_value=self.response("")):
            result = asr._request(b"wave", "https://dashscope.aliyuncs.com/compatible-mode/v1", "fake")
        self.assertEqual(result["text"], "")
        self.assertNotIn("segments", result)

    def test_media_hash_mismatch_prevents_cloud_request(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "answer.webm"
            path.write_bytes(b"changed")
            with patch.object(asr, "media_info", return_value={"has_audio": True, "duration_seconds": 1}), \
                    patch.object(asr, "get_api_key") as key, patch.object(asr.requests, "post") as post:
                with self.assertRaises(asr.CloudASRError) as caught:
                    asr.transcribe_answer(path, {"media": {"sha256": "not-the-same"}})
            self.assertEqual(caught.exception.code, "media_changed")
            key.assert_not_called()
            post.assert_not_called()

    def test_cloud_errors_are_sanitized(self):
        for status in (302, 401, 403, 429, 500):
            with self.subTest(status=status), patch.object(asr.requests, "post", return_value=Mock(status_code=status, text="secret")):
                with self.assertRaises(asr.CloudASRError) as caught:
                    asr._request(b"wav", "https://dashscope.aliyuncs.com/compatible-mode/v1", "secret")
                self.assertNotIn("secret", str(caught.exception))
        with patch.object(asr.requests, "post", side_effect=requests.Timeout("secret")):
            with self.assertRaises(asr.CloudASRError) as caught:
                asr._request(b"wav", "https://dashscope.aliyuncs.com/compatible-mode/v1", "secret")
        self.assertEqual(caught.exception.code, "timeout")
        self.assertNotIn("secret", str(caught.exception))

    def test_unfinished_invalid_or_oversize_response_not_scored(self):
        bad = [{"choices": []}, {"choices": [{"finish_reason": "length", "message": {"content": "截断"}}]},
               {"choices": [{"finish_reason": "stop", "message": {"content": None}}]}]
        for value in bad:
            with patch.object(asr.requests, "post", return_value=self.response(**value)):
                with self.assertRaises(asr.CloudASRError):
                    asr._request(b"wav", "https://dashscope.aliyuncs.com/compatible-mode/v1", "fake")

    def test_chunking_and_partial_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "recording.webm"
            path.write_bytes(b"original")
            answer = {"media": {"sha256": sha256(path.read_bytes()).hexdigest()}}
            with patch.object(asr, "media_info", return_value={"has_audio": True, "duration_seconds": 181}), \
                    patch.object(asr, "_audio_samples", return_value=np.zeros(181 * 16000, dtype="<i2")), \
                    patch.object(asr, "_base_url", return_value="https://dashscope.aliyuncs.com/compatible-mode/v1"), \
                    patch.object(asr, "get_api_key", return_value="fake"), \
                    patch.object(asr, "_request", return_value={"text": "原话", "request_id": "id", "model": asr.MODEL, "usage": {}}) as send:
                result = asr.transcribe_answer(path, answer)
                self.assertEqual(send.call_count, 2)
                self.assertEqual([(c["start"], c["end"]) for c in result["chunks"]], [(0, 180), (180, 181)])
                self.assertEqual(result["chunks"][0]["alignment"], "audio_chunk_only")
                send.side_effect = [{"text": "第一段", "request_id": "id", "model": asr.MODEL, "usage": {}},
                                    asr.CloudASRError("timeout", "超时")]
                with self.assertRaises(asr.CloudASRError):
                    asr.transcribe_answer(path, answer)

    def test_only_configured_bailian_compatible_endpoint(self):
        for base in ("https://evil.example/compatible-mode/v1", "http://dashscope.aliyuncs.com/compatible-mode/v1",
                     "https://dashscope.aliyuncs.com/compatible-mode/v1?token=unsafe",
                     "https://dashscope-us.aliyuncs.com/compatible-mode/v1"):
            with patch.object(asr, "public_settings", return_value={"base_url": base}):
                with self.assertRaises(asr.CloudASRError):
                    asr._base_url()


if __name__ == "__main__":
    unittest.main()
