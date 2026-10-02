"""Offline adapter, discovery, capability and authenticated transport contracts."""
import base64
import io
import json
import struct
import sys
import unittest
import urllib.error
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "provider_api.py").exists():
    sys.path.insert(0, str(ROOT / "src/plugins/booth_search"))
import provider_api as api


class TestProvider(unittest.TestCase):
    def setUp(self):
        api._CACHE.clear()
        self.image, self.colors = api._probe_image()
        self.reply = {"choices": [{"message": {"content": json.dumps({"colors": self.colors})}}]}
        self.base = "https://provider.invalid/v1"

    def capability(self, **kwargs):
        return api.image_capability(kwargs.get("base", self.base), kwargs.get("key", "test-key"),
                                    kwargs.get("model", "chosen"), timeout=1)

    def test_root_and_full_endpoints(self):
        cases = [
            (self.base, "openai", self.base + "/chat/completions"),
            (self.base + "/chat/completions/", "openai", self.base + "/chat/completions"),
            ("https://api.anthropic.com", "anthropic", "https://api.anthropic.com/v1/messages"),
            ("https://claude-proxy.invalid/v1/messages", "anthropic", "https://claude-proxy.invalid/v1/messages"),
            ("https://generativelanguage.googleapis.com/v1beta", "gemini",
             "https://generativelanguage.googleapis.com/v1beta/models/chosen:generateContent"),
            ("https://gemini-proxy.invalid/v1/models/chosen:generateContent", "gemini",
             "https://gemini-proxy.invalid/v1/models/chosen:generateContent"),
            ("https://generativelanguage.googleapis.com/v1beta/openai", "openai",
             "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
        ]
        for base, protocol, url in cases:
            with self.subTest(base=base):
                result = api.endpoint(base, "chosen")
                self.assertEqual((result[0], result[2]), (protocol, url))
                self.assertEqual(api.endpoint(url, "chosen")[2], url)

    def test_native_auth_is_separate_and_no_vendor_session_leak(self):
        for base, field in (
            (self.base, "Authorization"),
            ("https://claude-proxy.invalid/v1/messages", "x-api-key"),
            ("https://gemini-proxy.invalid/v1beta", "x-goog-api-key"),
        ):
            result = api.headers(base, "test-key")
            self.assertIn(field, result)
            self.assertNotIn("x-opencode-session", result)
            self.assertEqual(sum(name in result for name in ("Authorization", "x-api-key", "x-goog-api-key")), 1)
        self.assertIn("x-opencode-session", api.headers("https://opencode.ai/zen/go/v1", "test-key"))
        self.assertEqual(api.headers(self.base, "test-key", "explicit")["x-opencode-session"], "explicit")

    def test_native_text_and_image_payloads(self):
        payload = {"model": "chosen", "messages": [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": [{"type": "text", "text": "query"},
                                       {"type": "image_url", "image_url": {"url": self.image}}]}],
            "max_tokens": 100, "thinking": {"type": "disabled"}, "response_format": {"type": "json_object"}}
        _, claude, _ = api.prepare("https://proxy.invalid/v1/messages", payload, "test-key")
        self.assertEqual(claude["system"], "rules")
        source = claude["messages"][0]["content"][1]["source"]
        self.assertEqual(source["media_type"], "image/png")
        self.assertNotIn("thinking", claude)
        _, gemini, _ = api.prepare("https://proxy.invalid/v1beta", payload, "test-key")
        self.assertEqual(gemini["contents"][0]["parts"][0]["text"], "query")
        self.assertEqual(gemini["contents"][0]["parts"][1]["inlineData"]["data"], source["data"])
        self.assertEqual(gemini["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(gemini["systemInstruction"]["parts"][0]["text"], "rules")

    def test_response_parsing_for_three_protocols(self):
        for response in (
            {"choices": [{"message": {"content": "answer"}}]},
            {"choices": [{"message": {"content": [{"type": "text", "text": "answer"}]}}]},
            {"content": [{"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "answer"}]},
            {"candidates": [{"content": {"parts": [{"text": "hidden", "thought": True}, {"text": "answer"}]}}]},
        ):
            self.assertEqual(api.content(response), "answer")

    def test_two_fields_discover_service_default(self):
        with mock.patch.object(api, "request_json", return_value={
                "data": [{"id": "embedding-only"}, {"id": "other"}, {"id": "selected", "is_default": True}]}) as wire:
            self.assertEqual(api.resolve_model(self.base, "test-key"), "selected")
            self.assertEqual(api.resolve_model(self.base, "test-key"), "selected")
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(wire.call_args.args[0], self.base + "/models")

    def test_auto_prefers_declared_image_support_not_model_name(self):
        with mock.patch.object(api, "request_json", return_value={"data": [
                {"id": "vision-sounding-name", "input_modalities": ["text"]},
                {"id": "unbranded", "capabilities": {"vision": True}}]}):
            self.assertEqual(api.resolve_model(self.base, "test-key", "auto"), "unbranded")

    def test_native_model_catalogs_and_embedded_model(self):
        with mock.patch.object(api, "request_json", return_value={"models": [
                {"name": "models/embedding", "supportedGenerationMethods": ["embedContent"]},
                {"name": "models/native", "supportedGenerationMethods": ["generateContent"]}]}):
            self.assertEqual(api.resolve_model("https://proxy.invalid/v1beta", "test-key"), "native")
        self.assertEqual(api.resolve_model("https://proxy.invalid/v1/models/embedded:generateContent",
                                          "test-key"), "embedded")

    def test_model_override_skips_discovery_and_missing_list_is_actionable(self):
        with mock.patch.object(api, "request_json", side_effect=api.ProviderError("no list")) as wire:
            self.assertEqual(api.resolve_model(self.base, "test-key", "manual"), "manual")
            wire.assert_not_called()
            with self.assertRaisesRegex(api.ProviderError, "AI_MODEL"):
                api.resolve_model(self.base, "test-key")

    def test_model_name_never_proves_image_support(self):
        with mock.patch.object(api, "catalog", return_value=[{"id": "vision-model", "input_modalities": ["text"]}]), \
                mock.patch.object(api, "request_json") as wire:
            result = self.capability(model="vision-model")
        self.assertEqual(result["state"], "unsupported")
        wire.assert_not_called()
        self.assertIn("文字", api.image_notice(result))

    def test_declared_support_still_requires_actual_probe(self):
        with mock.patch.object(api, "catalog", return_value=[{"id": "chosen", "capabilities": {"vision": True}}]), \
                mock.patch.object(api, "request_json", return_value={"choices": [{"message": {"content": "{}"}}]}):
            self.assertEqual(self.capability()["state"], "unknown")

    def test_successful_probe_uses_synthetic_image_and_is_cached(self):
        with mock.patch.object(api, "catalog", return_value=[]), \
                mock.patch.object(api, "_probe_image", return_value=(self.image, self.colors)), \
                mock.patch.object(api, "request_json", return_value=self.reply) as wire:
            self.assertEqual(self.capability()["state"], "supported")
            self.assertEqual(self.capability()["state"], "supported")
        self.assertEqual(wire.call_count, 1)
        sent = wire.call_args.args[2]["messages"][0]["content"]
        self.assertEqual(sent[1]["image_url"]["url"], self.image)

    def test_ignored_image_http_200_is_not_support(self):
        with mock.patch.object(api, "catalog", return_value=[]), \
                mock.patch.object(api, "_probe_image", return_value=(self.image, ["red", "blue"] * 4)), \
                mock.patch.object(api, "request_json", return_value={
                    "choices": [{"message": {"content": '{"colors":["red","red","red","red","red","red","red","red"]}'}}]}):
            self.assertEqual(self.capability()["state"], "unknown")

    def test_rejection_is_unsupported_but_auth_network_quota_stay_unknown(self):
        for error, state in (
            (api.ProviderError("rejected", code=400, image_rejected=True), "unsupported"),
            (api.ProviderError("authentication", code=401), "unknown"),
            (api.ProviderError("quota", code=429), "unknown"),
            (api.ProviderError("network"), "unknown"),
        ):
            api._CACHE.clear()
            with mock.patch.object(api, "catalog", return_value=[]), \
                    mock.patch.object(api, "request_json", side_effect=error):
                self.assertEqual(self.capability()["state"], state)

    def test_unknown_short_cache_expires_and_rechecks(self):
        with mock.patch.object(api, "catalog", return_value=[]), \
                mock.patch.object(api.time, "monotonic", return_value=0), \
                mock.patch.object(api, "request_json", side_effect=api.ProviderError("network")) as wire:
            self.capability()
            self.capability()
            self.assertEqual(wire.call_count, 1)
        with mock.patch.object(api, "catalog", return_value=[]), \
                mock.patch.object(api.time, "monotonic", return_value=61), \
                mock.patch.object(api, "_probe_image", return_value=(self.image, self.colors)), \
                mock.patch.object(api, "request_json", return_value=self.reply) as wire:
            self.assertEqual(self.capability()["state"], "supported")
            wire.assert_called_once()

    def test_cache_is_scoped_to_credentials_endpoint_and_model(self):
        with mock.patch.object(api, "catalog", return_value=[]), \
                mock.patch.object(api, "_probe_image", return_value=(self.image, self.colors)), \
                mock.patch.object(api, "request_json", return_value=self.reply) as wire:
            for kwargs in ({}, {"key": "different"}, {"base": "https://other.invalid/v1"}, {"model": "different"}):
                self.assertEqual(self.capability(**kwargs)["state"], "supported")
        self.assertEqual(wire.call_count, 4)
        self.assertNotIn("test-key", repr(list(api._CACHE)))

    def test_real_rejection_revokes_cached_success(self):
        with mock.patch.object(api, "catalog", return_value=[]), \
                mock.patch.object(api, "_probe_image", return_value=(self.image, self.colors)), \
                mock.patch.object(api, "request_json", return_value=self.reply) as wire:
            self.capability()
            api.reject_image(self.base, "test-key", "chosen")
            self.assertEqual(self.capability()["state"], "unsupported")
        self.assertEqual(wire.call_count, 1)

    def test_probe_option_retry_is_bounded_and_does_not_open_on_failure(self):
        with mock.patch.object(api, "catalog", return_value=[]), \
                mock.patch.object(api, "_probe_image", return_value=(self.image, self.colors)), \
                mock.patch.object(api, "request_json", side_effect=[
                    api.ProviderError("unsupported max_tokens", code=400, retry_payload={"model":"chosen"}),
                    self.reply]) as wire:
            self.assertEqual(self.capability()["state"], "supported")
        self.assertEqual(wire.call_count, 2)

    def test_synthetic_png_dimensions_and_expected_colors(self):
        png = base64.b64decode(self.image.split(",", 1)[1])
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (128, 64))
        self.assertEqual(len(self.colors), 8)
        self.assertTrue(set(self.colors) <= {"red", "green", "blue", "yellow", "magenta", "cyan"})

    def test_invalid_endpoint_never_echoes_url_credentials(self):
        for url in ("http://example.com/v1", "https://127.0.0.1/v1", "https://[::1]/v1",
                    "https://localhost/v1", "https://user:secret@example.com/v1", "https://example.com/v1?key=secret"):
            with self.subTest(url=url):
                with self.assertRaises(api.ProviderError) as error:
                    api.guard_url(url)
                self.assertNotIn("secret", str(error.exception))

    def test_custom_exa_endpoint_has_public_dns_requirement(self):
        for ip, allowed in (("93.184.216.34", True), ("127.0.0.1", False), ("192.168.0.1", False), ("::1", False)):
            with mock.patch.object(api.socket, "getaddrinfo", return_value=[(2, 1, 6, "", (ip, 443))]):
                if allowed:
                    self.assertEqual(api.exa_url("https://search.invalid/api"), "https://search.invalid/api/search")
                    self.assertEqual(api.exa_url("https://search.invalid/api/search"), "https://search.invalid/api/search")
                else:
                    with self.assertRaises(api.ProviderError):
                        api.exa_url("https://search.invalid/api")

    def test_authenticated_probe_transport_does_not_follow_redirect(self):
        seen = []
        class Wire(urllib.request.HTTPSHandler):
            def https_open(self, req):
                seen.append(req)
                headers = Message()
                headers["Location"] = "https://evil.invalid/collect"
                response = urllib.response.addinfourl(io.BytesIO(b""), headers, req.full_url, 302)
                response.msg = "Found"
                return response
        with mock.patch.object(api, "_OPENER", urllib.request.build_opener(api._NoRedirect, Wire)):
            with self.assertRaises(api.ProviderError):
                api.request_json(self.base, {"Authorization":"Bearer test-key"}, {})
        self.assertEqual(len(seen), 1)

    def test_http_error_message_is_sanitized(self):
        error = urllib.error.HTTPError(self.base, 401, "bad", {}, io.BytesIO(b"secret key test-key"))
        with mock.patch.object(api._OPENER, "open", side_effect=error):
            with self.assertRaises(api.ProviderError) as caught:
                api.request_json(self.base, {"Authorization":"Bearer test-key"}, {})
        self.assertNotIn("test-key", str(caught.exception))
        self.assertEqual(caught.exception.code, 401)

    def test_openai_optional_parameter_retry_is_narrow(self):
        data = {"messages": [], "max_tokens": 100, "temperature":0.2, "response_format":{"type":"json_object"}}
        replacement = api.compatible_retry(data, 400, "Unsupported max_tokens; use max_completion_tokens")
        self.assertEqual(replacement["max_completion_tokens"], 100)
        self.assertNotIn("max_tokens", replacement)
        self.assertIn("max_tokens", data)
        self.assertNotIn("temperature", api.compatible_retry(data, 400, "temperature is not supported"))
        self.assertNotIn("response_format", api.compatible_retry(data, 422, "unsupported response_format"))
        self.assertIsNone(api.compatible_retry(data, 429, "temperature is not supported"))
        self.assertIsNone(api.compatible_retry(data, 400, "invalid credentials"))


if __name__ == "__main__":
    unittest.main()
