import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_media import MediaError, analyze_media, inspect_media


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def silence(self, seconds):
        path = self.folder / 'silent.wav'
        with wave.open(str(path), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b'\0\0' * (16000 * seconds))
        return path

    def test_silent_audio_has_no_fabricated_transcript(self):
        result = analyze_media(self.silence(2))
        self.assertEqual(result['transcription']['status'], 'silent')
        self.assertEqual(result['transcription']['text'], '')
        self.assertEqual(result['model_runs'], [])
        self.assertFalse(result['has_video'])
        self.assertEqual(result['duration_ms'], 2000)
        self.assertEqual([(w['start_ms'], w['end_ms']) for w in result['audio']['windows']],
                         [(0, 1000), (1000, 2000)])

    def test_reject_long_media_without_truncating_original(self):
        path = self.silence(181)
        size = path.stat().st_size
        with self.assertRaises(MediaError):
            inspect_media(path)
        self.assertEqual(path.stat().st_size, size)

    def test_reject_corrupt_media(self):
        path = self.folder / 'corrupt.mp4'
        path.write_bytes(b'not a movie')
        with self.assertRaises(MediaError):
            inspect_media(path)

    def test_reject_extension_and_empty_file(self):
        for name in ['playlist.m3u8', 'empty.webm']:
            path = self.folder / name
            path.write_bytes(b'')
            with self.assertRaises(MediaError):
                inspect_media(path)

    def test_real_sample_result_times_are_bounded(self):
        evidence = Path(__file__).resolve().parents[1] / 'local-data/interview-validation/asr-sample.json'
        if not evidence.exists():
            self.skipTest('Run the documented real ASR sample before this evidence check.')
        result = json.loads(evidence.read_text(encoding='utf-8'))
        self.assertEqual(result['transcription']['status'], 'ok')
        self.assertTrue(result['transcription']['segments'])
        for row in result['transcription']['segments']:
            self.assertLess(row['start_ms'], row['end_ms'])
            self.assertGreaterEqual(row['start_ms'], 0)
            self.assertLessEqual(row['end_ms'], result['duration_ms'])
        self.assertEqual(len(result['transcription']['model_sha256']), 64)


if __name__ == '__main__':
    unittest.main()
