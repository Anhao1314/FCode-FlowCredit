"""Local provider: OpenAI-compatible transport and structured-output flags."""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.providers.local import PLAN_SCHEMA, LocalProvider  # noqa: E402


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class LocalProviderRequestTests(unittest.TestCase):
    def _request_body(self, provider: LocalProvider) -> dict:
        seen: dict = {}

        def fake_urlopen(request, timeout):  # noqa: ARG001 - signature match
            seen["body"] = json.loads(request.data.decode("utf-8"))
            return _Response({"choices": [{"message": {"content": "{}"}}], "usage": {}})

        with mock.patch("flowagent.providers.local.urllib.request.urlopen", fake_urlopen):
            provider._chat("system", "user")
        return seen["body"]

    def test_default_request_has_no_response_format(self) -> None:
        body = self._request_body(LocalProvider())
        self.assertNotIn("response_format", body)

    def test_json_mode_requests_json_object(self) -> None:
        body = self._request_body(LocalProvider(json_mode=True))
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_structured_mode_sends_plan_schema(self) -> None:
        body = self._request_body(LocalProvider(structured=True))
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertEqual(body["response_format"]["json_schema"]["schema"], PLAN_SCHEMA)


if __name__ == "__main__":
    unittest.main()
