"""审计隐私姿态：中性 UA / 出口代理 / 静止加密 round-trip。"""

from __future__ import annotations

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "viewer"))

import web_snapshot  # noqa: E402


class UserAgentTests(unittest.TestCase):
    def test_default_ua_is_neutral_browser_string(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FCODE_AUDIT_UA", None)
            ua = web_snapshot.audit_user_agent()
        self.assertIn("Mozilla/5.0", ua)
        self.assertNotIn("FCode", ua, "默认 UA 不得暴露审计者身份")

    def test_ua_override(self) -> None:
        with mock.patch.dict(os.environ, {"FCODE_AUDIT_UA": "CustomUA/1.0"}):
            self.assertEqual(web_snapshot.audit_user_agent(), "CustomUA/1.0")


class EgressTests(unittest.TestCase):
    def test_proxy_env_honoured(self) -> None:
        with mock.patch.dict(os.environ, {"FCODE_AUDIT_PROXY": "http://127.0.0.1:8080"}):
            self.assertEqual(web_snapshot.audit_proxy(), "http://127.0.0.1:8080")

    def test_https_proxy_fallback_and_direct_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("FCODE_AUDIT_PROXY", "HTTPS_PROXY", "https_proxy"):
                os.environ.pop(name, None)
            self.assertEqual(web_snapshot.audit_proxy(), "")


class EncryptionTests(unittest.TestCase):
    def _module(self):
        import audit_privacy
        return audit_privacy

    def test_encrypt_decrypt_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = pathlib.Path(tmp) / "audit-key"
            with mock.patch.dict(os.environ, {
                "FCODE_AUDIT_KEY_FILE": str(key),
            }):
                self._module()
                target = pathlib.Path(tmp) / "snap"
                (target / "_next").mkdir(parents=True)
                (target / "index.html").write_text("<html>secret-target</html>")
                (target / "_next" / "a.js").write_text("console.log(1)")

                archive = self._module().encrypt_dir(target)
                self.assertFalse(target.exists(), "明文目录必须删除")
                self.assertTrue(archive.is_file())
                self.assertTrue((archive.parent / (archive.name + ".encrypted")).is_file())
                payload = archive.read_bytes()
                self.assertNotIn(b"secret-target", payload, "密文不得包含明文")

                restored = self._module().decrypt_dir(archive, tmp)
                self.assertEqual((restored / "index.html").read_text(),
                                 "<html>secret-target</html>")

    def test_enable_flag(self) -> None:
        with mock.patch.dict(os.environ, {"FCODE_AUDIT_ENCRYPT": "1"}):
            self.assertTrue(self._module().encryption_enabled())
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FCODE_AUDIT_ENCRYPT", None)
            self.assertFalse(self._module().encryption_enabled())


if __name__ == "__main__":
    unittest.main()
