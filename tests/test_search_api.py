"""Search adapters use multiple wire contracts, preserving one BOOTH boundary."""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "search_api.py").exists():
    sys.path.insert(0, str(ROOT / "src/plugins/booth_search"))
import search_api as search

PUBLIC_DNS = [(2, 1, 6, "", ("93.184.216.34", 443))]


class TestSearchAdapters(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(search.provider_api.socket, "getaddrinfo", return_value=PUBLIC_DNS)
        patch.start()
        self.addCleanup(patch.stop)

    def test_unknown_service_uses_exact_json_url_and_bearer(self):
        wire = search.prepare("鈴", "https://gateway.invalid/custom/find", "user-key")
        self.assertEqual(wire.provider, "json")
        self.assertEqual(wire.url, "https://gateway.invalid/custom/find")
        self.assertEqual(wire.headers["Authorization"], "Bearer user-key")
        self.assertNotIn("x-api-key", wire.headers)
        self.assertEqual(json.loads(wire.body)["include_domains"], ["booth.pm"])

    def test_auto_recognizes_native_protocols_from_exact_hosts(self):
        for url, provider, header in (
            ("https://api.exa.ai", "exa", "x-api-key"),
            ("https://api.tavily.com", "tavily", "Authorization"),
            ("https://api.search.brave.com", "brave", "X-Subscription-Token"),
        ):
            wire = search.prepare("鈴", url, "user-key")
            self.assertEqual(wire.provider, provider)
            self.assertIn(header, wire.headers)
            self.assertNotIn("user-key", wire.url)

    def test_native_gateway_can_select_protocol_explicitly(self):
        wire = search.prepare("鈴", "https://gateway.invalid/vendor/search", "user-key", "tavily")
        self.assertEqual(wire.url, "https://gateway.invalid/vendor/search")
        body = json.loads(wire.body)
        self.assertEqual(body["max_results"], 8)
        self.assertFalse(body["include_answer"])
        self.assertEqual(wire.headers["Authorization"], "Bearer user-key")

    def test_brave_uses_get_query_and_nested_response(self):
        wire = search.prepare("帽子 & 鈴", "https://gateway.invalid/res/v1", "user-key", "brave")
        self.assertEqual(wire.method, "GET")
        self.assertIsNone(wire.body)
        self.assertIn("/res/v1/web/search?", wire.url)
        raw = json.dumps({"web":{"results":[{"url":"https://booth.pm/ja/items/12"}]}}).encode()
        self.assertEqual(search.parse_results(wire, raw), [12])

    def test_searxng_can_work_without_key_and_requests_json(self):
        wire = search.prepare("鈴", "https://gateway.invalid/search", provider="searxng")
        self.assertEqual(wire.method, "GET")
        self.assertIn("format=json", wire.url)
        self.assertNotIn("Authorization", wire.headers)
        self.assertEqual(search.parse_results(wire, b'{"results":[{"url":"https://shop.booth.pm/items/9"}]}'), [9])

    def test_product_boundary_rejects_lookalikes_credentials_and_wrong_routes(self):
        self.assertEqual(search.item_ids_from_urls([
            "https://booth.pm/items/7", "https://shop.booth.pm/ja/items/7",
            "https://evilbooth.pm/items/8", "https://booth.pm.evil/items/9",
            "https://user:pass@booth.pm/items/10", "https://booth.pm/shop/11",
            "https://booth.pm:8080/items/12", None, {},
            "https://shop.booth.pm/items/13"]), [7, 13])

    def test_malformed_rows_do_not_discard_valid_results(self):
        wire = search.prepare("q", "https://gateway.invalid/find")
        raw = json.dumps({"results":[None, {}, {"url":{}}, {"url":"https://booth.pm/items/8"}]}).encode()
        self.assertEqual(search.parse_results(wire, raw), [8])

    def test_invalid_or_oversized_response_is_rejected(self):
        wire = search.prepare("q", "https://gateway.invalid/find")
        for raw in (b"[]", b"{", b"x" * (search.MAX_RESPONSE_BYTES + 1)):
            with self.assertRaises(search.provider_api.ProviderError):
                search.parse_results(wire, raw)

    def test_unknown_protocol_and_missing_auth_fail_before_dns(self):
        with mock.patch.object(search.provider_api.socket, "getaddrinfo") as dns:
            for name in ("missing", "exa", "tavily", "brave"):
                with self.assertRaises(search.provider_api.ProviderError):
                    search.prepare("q", "https://gateway.invalid/find", provider=name)
            dns.assert_not_called()

    def test_private_dns_cannot_send_key_or_build_adapter_request(self):
        with mock.patch.object(search.provider_api.socket, "getaddrinfo",
                               return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(search.provider_api.ProviderError):
                search.prepare("q", "https://gateway.invalid/find", "user-key")

    def test_custom_adapter_changes_request_and_result_shape(self):
        def build(query, key):
            return "POST", {"text":query, "top":5}, {"X-Custom-Key":key}
        def extract(data):
            return [row["href"] for row in data.get("hits", [])]
        search.register_adapter("example_custom", build, extract)
        self.addCleanup(search._ADAPTERS.pop, "example_custom")
        wire = search.prepare("鈴", "https://gateway.invalid/custom", "custom-key", "example_custom")
        self.assertEqual(json.loads(wire.body), {"text":"鈴", "top":5})
        self.assertEqual(wire.headers["X-Custom-Key"], "custom-key")
        self.assertEqual(search.parse_results(wire, b'{"hits":[{"href":"https://booth.pm/items/31"}]}'), [31])

    def test_adapter_registration_cannot_replace_existing_protocol(self):
        with self.assertRaises(ValueError):
            search.register_adapter("exa", lambda *_:None, lambda _:[])
        with self.assertRaises(TypeError):
            search.register_adapter("bad_callbacks", "code", "code")

    def test_invalid_endpoint_does_not_expose_embedded_secret(self):
        for url in ("http://gateway.invalid/find", "https://key:secret@gateway.invalid/find",
                    "https://gateway.invalid/find?key=secret"):
            with self.assertRaises(search.provider_api.ProviderError) as error:
                search.prepare("q", url)
            self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
