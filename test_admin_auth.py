import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import admin_auth


class AdminAuthTests(unittest.TestCase):
    def test_env_key_priority(self):
        with mock.patch.dict(os.environ, {admin_auth.ENV_KEY: "env-secret"}):
            self.assertEqual(admin_auth.load_admin_secret(), "env-secret")
            self.assertTrue(admin_auth.verify_admin_key("env-secret"))
            self.assertFalse(admin_auth.verify_admin_key("wrong"))

    def test_file_secret_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_file = Path(tmp) / "admin_secret.txt"
            with mock.patch.object(admin_auth, "SECRET_FILE", secret_file):
                with mock.patch.dict(os.environ, {}, clear=True):
                    first = admin_auth.load_admin_secret()
                    self.assertEqual(len(first), 12)
                    # 第二次读取复用同一密钥，不重新生成
                    self.assertEqual(admin_auth.load_admin_secret(), first)
                    self.assertTrue(admin_auth.verify_admin_key(first))
                    self.assertFalse(admin_auth.verify_admin_key(""))

    def test_generated_secret_is_random(self):
        with tempfile.TemporaryDirectory() as tmp:
            secret_file = Path(tmp) / "admin_secret.txt"
            with mock.patch.object(admin_auth, "SECRET_FILE", secret_file):
                with mock.patch.dict(os.environ, {}, clear=True):
                    first = admin_auth.load_admin_secret()
                    os.remove(secret_file)
                    second = admin_auth.load_admin_secret()
                    self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
