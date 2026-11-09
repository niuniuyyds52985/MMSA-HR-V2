import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_credentials import save_credential, get_api_key, public_settings, credential_available, cloud_inputs_allowed, CredentialError


@unittest.skipUnless(os.name == 'nt', 'Windows DPAPI integration')
class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'fixture.json'
        self.environment = patch.dict(os.environ, {'INTERVIEW_CREDENTIALS_FILE': str(self.path)}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.key = 'synthetic-secret-only-for-unit-test'
        self.base = 'https://dashscope.aliyuncs.com/compatible-mode/v1'

    def test_dpapi_roundtrip_without_plaintext_on_disk_or_public_settings(self):
        self.assertFalse(credential_available())
        save_credential(self.key, self.base)
        self.assertTrue(credential_available())
        self.assertEqual(get_api_key(self.base), self.key)
        self.assertNotIn(self.key, self.path.read_text())
        self.assertEqual(set(public_settings()), {'provider', 'base_url', 'model'})
        self.assertNotIn(self.key, json.dumps(public_settings()))

    def test_saved_key_is_never_sent_to_different_base_url(self):
        save_credential(self.key, self.base)
        with self.assertRaises(CredentialError):
            get_api_key('https://other.invalid/compatible-mode/v1')

    def test_corrupted_ciphertext_is_rejected_without_echo(self):
        save_credential(self.key, self.base)
        record = json.loads(self.path.read_text())
        record['encrypted_key'] = 'aW52YWxpZA=='
        self.path.write_text(json.dumps(record))
        with self.assertRaises(CredentialError) as failure:
            get_api_key(self.base)
        self.assertNotIn(self.key, str(failure.exception))

    def test_historical_answers_cannot_be_sent_implicitly(self):
        save_credential(self.key, self.base)
        self.assertFalse(cloud_inputs_allowed([{'created_at': '2020-01-01T00:00:00+00:00'}]))
        self.assertFalse(cloud_inputs_allowed([{}]))
        self.assertTrue(cloud_inputs_allowed([{'created_at': '2099-01-01T00:00:00+00:00'}]))
        self.assertFalse(cloud_inputs_allowed([{'created_at': '2099-01-01T00:00:00+00:00'}, {'created_at': '2020-01-01T00:00:00+00:00'}]))


if __name__ == '__main__':
    unittest.main()
