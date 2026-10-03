"""Offline image gates and native API integration; no live keys, QQ or HTTP."""
import asyncio
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx
import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/plugins"))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()
import booth_search as bs
from booth_search import booth_client as bc, vision, webfind
from booth_search.config import Config

api = vision.provider_api


class TestGenericBot(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = Config(ai_mode="api", ai_api_key="test", ai_base_url="https://provider.invalid/v1",
                          ai_model="text", recall_enabled=False, websearch_fallback=False)
        patch = mock.patch.object(bs, "plugin_config", self.cfg)
        patch.start()
        self.addCleanup(patch.stop)
        api._CACHE.clear()

    async def test_all_image_entries_close_before_download_file_ai_or_reverse_search(self):
        for state in ("unsupported", "unknown"):
            with mock.patch.object(api, "image_capability", return_value={
                    "state":state, "model":"text", "reason":"不支持或暂不可用"}), \
                    mock.patch.object(bs, "_download_image", new=mock.AsyncMock()) as download, \
                    mock.patch.object(bs.tempfile, "NamedTemporaryFile") as file, \
                    mock.patch.object(vision, "_api_post", new=mock.AsyncMock()) as visual, \
                    mock.patch.object(bc, "imgsearch") as reverse:
                for result in (await bs._handle_image("https://gchat.qpic.cn/user.png", "hint"),
                               await bs._handle_image_bytes(b"user-image", "hint")):
                    self.assertEqual(result["entries"], [])
                    self.assertIn("请使用文字搜索", result["text"])
                download.assert_not_awaited()
                file.assert_not_called()
                visual.assert_not_awaited()
                reverse.assert_not_called()

    async def test_default_and_benchmark_startup_make_no_ai_calls(self):
        for cfg in (Config(), Config(run_profile="benchmark", ai_api_key="test", ai_base_url=self.cfg.vision_base_url)):
            with mock.patch.object(bs, "plugin_config", cfg), \
                    mock.patch.object(api, "request_json") as wire:
                await bs.check_image_capability()
                self.assertEqual((await bs._handle_image_bytes(b"image", ""))["entries"], [])
                wire.assert_not_called()

    async def test_pure_text_model_still_runs_text_pipeline_without_vision_probe(self):
        with mock.patch.object(api, "image_capability") as capability, \
                mock.patch.object(bs, "_ai_plan", new=mock.AsyncMock(return_value=(["鈴"],[],True))), \
                mock.patch.object(bs, "_search_merged", new=mock.AsyncMock(return_value=([], {"total":0}))) as search:
            result = await bs._handle_text("铃铛")
        self.assertIn("没搜到", result["text"])
        search.assert_awaited()
        capability.assert_not_called()

    async def test_low_level_vision_and_old_cli_image_function_cannot_bypass_gate(self):
        with mock.patch.object(api, "image_capability", return_value={
                "state":"unsupported", "model":"text", "reason":"仅文字"}), \
                mock.patch.object(vision, "_api_post", new=mock.AsyncMock()) as wire, \
                mock.patch.object(vision, "_cli_run_sync") as cli:
            with self.assertRaisesRegex(api.ProviderError, "文字搜索"):
                await vision.extract_keywords(b"user-image", base_url=self.cfg.vision_base_url, api_key="test", model="text")
            with self.assertRaisesRegex(api.ProviderError, "仅允许文字"):
                await vision.extract_keywords_cli("user.png", bin_path="fake", model="multi")
            wire.assert_not_awaited()
            cli.assert_not_called()

    async def test_reply_image_finishes_with_notice_before_search_progress(self):
        event = SimpleNamespace(get_plaintext=lambda:"/vrc search hint", get_message=lambda:[],
                                reply=SimpleNamespace(message=[SimpleNamespace(type="image", data={"url":"https://gchat.qpic.cn/user.png"})]))
        with mock.patch.object(api, "image_capability", return_value={
                "state":"unsupported", "model":"text", "reason":"仅文字"}), \
                mock.patch.object(bs.matcher, "finish", new=mock.AsyncMock()) as finish, \
                mock.patch.object(bs.matcher, "send", new=mock.AsyncMock()) as progress, \
                mock.patch.object(bs, "_handle_image", new=mock.AsyncMock()) as search:
            await bs._do_search(SimpleNamespace(), event)
        self.assertIn("文字搜索", finish.call_args.args[0])
        progress.assert_not_awaited()
        search.assert_not_awaited()

    async def test_secondary_multimodal_is_only_used_after_its_own_probe(self):
        self.cfg.fallback_api_key = "secondary"
        self.cfg.fallback_base_url = "https://secondary.invalid/v1"
        self.cfg.fallback_model = "multi"
        with mock.patch.object(api, "image_capability", side_effect=[
                {"state":"unsupported", "model":"text", "reason":"仅文字"},
                {"state":"supported", "model":"multi"}]) as capability:
            result = await bs._verified_image_backend()
        self.assertEqual(result["api_key"], "secondary")
        self.assertEqual(result["model"], "multi")
        self.assertEqual(capability.call_count, 2)

    async def test_real_image_rejection_closes_reverse_search_and_revokes_capability(self):
        request = httpx.Request("POST", self.cfg.vision_base_url)
        response = httpx.Response(400, text="model does not support image input", request=request)
        with mock.patch.object(api, "image_capability", return_value={"state":"supported", "model":"text"}), \
                mock.patch.object(vision, "_api_post", new=mock.AsyncMock(side_effect=
                    httpx.HTTPStatusError("bad", request=request, response=response))), \
                mock.patch.object(bc, "imgsearch") as reverse:
            result = await bs._handle_image_bytes(b"\xff\xd8\xfffixture", "")
        self.assertIn("图片功能未启用", result["text"])
        reverse.assert_not_called()
        cached = api._CACHE[api._key(self.cfg.vision_base_url, "test", "vision:text")][1]
        self.assertEqual(cached["state"], "unsupported")

    async def test_explicit_api_mode_never_starts_implicit_cli(self):
        with mock.patch.object(vision, "plan_search", new=mock.AsyncMock(side_effect=api.ProviderError("no model; AI_MODEL"))), \
                mock.patch.object(vision, "resolve_cli_bin") as resolve, \
                mock.patch.object(vision, "plan_search_cli", new=mock.AsyncMock()) as cli:
            with self.assertRaisesRegex(bs.BoothUnavailable, "AI_MODEL"):
                await bs._ai_plan("铃铛")
        resolve.assert_not_called()
        cli.assert_not_awaited()

    async def test_verified_image_empty_keywords_retry_once(self):
        with mock.patch.object(bs, "_verified_image_backend", new=mock.AsyncMock(
                return_value={"base_url":self.cfg.vision_base_url,"api_key":"test","model":"multi"})), \
                mock.patch.object(bs.Path, "read_bytes", return_value=b"fixture"), \
                mock.patch.object(vision, "extract_keywords", new=mock.AsyncMock(
                    side_effect=[([], ""), (["鈴"], "アクセサリ")])) as extract:
            self.assertEqual((await bs._ai_vision("fixture.png", ""))[0], ["鈴"])
        self.assertEqual(extract.await_count, 2)

    async def test_two_fields_text_planning_and_evaluation_on_all_protocols(self):
        for base, protocol in (
            ("https://provider.invalid/v1", "openai"),
            ("https://provider.invalid/v1/messages", "anthropic"),
            ("https://provider.invalid/v1beta", "gemini"),
        ):
            api._CACHE.clear()
            self.cfg.vision_base_url, self.cfg.vision_model = base, ""
            seen = []
            def respond(request):
                seen.append(request)
                text = ('{"keywords":["鈴"],"translated":true}' if len(seen)==1 else
                        '{"verdict":"retry","reason":"not enough","hits":[],"keywords":[]}')
                response = ({"choices":[{"message":{"content":text}}]} if protocol=="openai" else
                            {"content":[{"type":"text","text":text}]} if protocol=="anthropic" else
                            {"candidates":[{"content":{"parts":[{"text":text}]}}]})
                return httpx.Response(200, json=response)
            client = httpx.AsyncClient
            with mock.patch.object(api, "request_json", return_value={
                    "data":[{"id":"generic"}], "models":[{"name":"models/generic", "supportedGenerationMethods":["generateContent"]}]}), \
                    mock.patch.object(httpx, "AsyncClient", side_effect=lambda **kw:
                        client(transport=httpx.MockTransport(respond), **kw)):
                self.assertEqual((await bs._ai_plan("铃铛"))[0], ["鈴"])
                self.assertEqual((await bs._ai_evaluate("铃铛", ["鈴"], ["1. 鈴"]))["verdict"], "retry")
            self.assertEqual(len(seen), 2)
            self.assertTrue(all("x-opencode-session" not in request.headers for request in seen))

    async def test_openai_unsupported_option_retries_without_brand_assumptions(self):
        requests = []
        def respond(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(400, text="temperature is not supported")
            return httpx.Response(200, json={"choices":[{"message":{"content":"ok"}}]})
        client = httpx.AsyncClient
        with mock.patch.object(httpx, "AsyncClient", side_effect=lambda **kw:
                client(transport=httpx.MockTransport(respond), **kw)):
            text = await vision._api_post(self.cfg.vision_base_url,
                {"model":"unbranded", "messages":[{"role":"user","content":"q"}], "temperature":0.2}, "test")
        self.assertEqual(text, "ok")
        self.assertIn("temperature", requests[0])
        self.assertNotIn("temperature", requests[1])

    async def test_custom_exa_endpoint_and_private_dns_block(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"results":[{"url":"https://booth.pm/items/7"},
                                                       {"url":"https://evilbooth.pm/items/8"}]})
        client = httpx.AsyncClient
        with mock.patch.object(httpx, "AsyncClient", side_effect=lambda **kw:
                client(transport=httpx.MockTransport(respond), **kw)), \
                mock.patch.object(api.socket, "getaddrinfo", return_value=[(2,1,6,"",("93.184.216.34",443))]):
            self.assertEqual(await webfind.exa_find("鈴", "search-key", base_url="https://search.invalid/api"), [7])
        self.assertEqual(str(requests[0].url), "https://search.invalid/api/search")
        self.assertEqual(requests[0].headers["x-api-key"], "search-key")
        with mock.patch.object(api.socket, "getaddrinfo", return_value=[(2,1,6,"",("127.0.0.1",443))]), \
                mock.patch.object(httpx, "AsyncClient") as wire:
            self.assertEqual(await webfind.exa_find("q", "key", base_url="https://search.invalid/api"), [])
            wire.assert_not_called()

    async def test_new_search_connection_never_inherits_legacy_key(self):
        with mock.patch.object(webfind, "ddg_find", new=mock.AsyncMock()) as ddg, \
                mock.patch.object(webfind, "api_find", new=mock.AsyncMock(return_value=[3, 3, 4])) as api_find, \
                mock.patch.object(webfind, "exa_find", new=mock.AsyncMock()) as legacy:
            self.assertEqual(await webfind.find_booth_item_ids("q", search_base_url="https://custom.invalid/find",
                             exa_api_key="stale-key", ddg_enabled=False), [3, 4])
        api_find.assert_awaited_once_with("q", "", 15, "https://custom.invalid/find", "auto")
        legacy.assert_not_awaited()
        ddg.assert_not_awaited()

    async def test_partial_new_ai_connection_never_mixes_legacy_credentials(self):
        old = dict(vision_api_key="old-key", vision_base_url="https://old.invalid/v1", vision_model="old-model",
                   fallback_api_key="old-fallback", fallback_base_url="https://old-fallback.invalid/v1")
        cfg = Config(**old, ai_base_url="https://new.invalid/v1", ai_fallback_base_url="https://new-fallback.invalid/v1")
        self.assertEqual(cfg.vision_api_key, "")
        self.assertEqual(cfg.vision_model, "")
        self.assertEqual(cfg.fallback_api_key, "")
        cfg = Config(**old, ai_api_key="new-key", ai_fallback_api_key="new-fallback")
        self.assertEqual(cfg.vision_base_url, "")
        self.assertEqual(cfg.fallback_base_url, "")

    async def test_selected_search_api_reaches_native_get_and_filters_results(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"web":{"results":[{"url":"https://booth.pm/items/5"},
                                    {"url":"https://evilbooth.pm/items/6"}]}})
        client = httpx.AsyncClient
        with mock.patch.object(httpx, "AsyncClient", side_effect=lambda **kw:
                client(transport=httpx.MockTransport(respond), **kw)), \
                mock.patch.object(api.socket, "getaddrinfo", return_value=[(2,1,6,"",("93.184.216.34",443))]):
            self.assertEqual(await webfind.api_find("q", "user-key", base_url="https://custom.invalid", provider="brave"), [5])
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[0].headers["x-subscription-token"], "user-key")

    async def test_search_endpoint_changes_invalidate_query_cache(self):
        from booth_search import qcache
        cfg = Config(search_base_url="https://custom.invalid/find", search_provider="json")
        first = qcache.configuration_key(cfg)
        cfg.search_provider = "tavily"
        self.assertNotEqual(first, qcache.configuration_key(cfg))
        cfg.search_provider = "json"
        cfg.search_ddg_enabled = False
        self.assertNotEqual(first, qcache.configuration_key(cfg))


class TestGenericConfig(unittest.TestCase):
    def test_defaults_and_nonempty_alias_precedence(self):
        cfg = Config()
        self.assertEqual(cfg.ai_mode, "caller")
        self.assertEqual((cfg.vision_api_key,cfg.vision_base_url,cfg.vision_model), ("","",""))
        cfg = Config(ai_api_key="new", ai_base_url="https://new.invalid/v1", vision_api_key="old",
                     vision_base_url="https://old.invalid/v1", vision_model="old-model")
        self.assertEqual(cfg.vision_api_key, "new")
        self.assertEqual(cfg.vision_base_url, "https://new.invalid/v1")
        self.assertEqual(cfg.vision_model, "")
        self.assertEqual(Config(ai_api_key="",vision_api_key="old").vision_api_key, "old")

    def test_nonebot_driver_config_loads_new_names(self):
        from nonebot.config import Config as DriverConfig
        driver = DriverConfig(_env_file=None, ai_api_key="test", ai_base_url="https://provider.invalid/v1")
        cfg = Config.model_validate(driver.model_dump())
        self.assertEqual(cfg.vision_api_key, "test")
        self.assertEqual(cfg.vision_base_url, "https://provider.invalid/v1")
        self.assertEqual(cfg.vision_model, "")

    def test_subprocess_key_only_in_environment_never_argv_or_envelope(self):
        with mock.patch.object(bc, "_verify_cli_supports_bot"), \
                mock.patch.object(bc, "resolve_cli_path", return_value="booth"), \
                mock.patch.object(bc.subprocess, "run", return_value=
                    subprocess.CompletedProcess([], 0, '{"ok":true,"data":{}}', "")) as run:
            bc.imgsearch("user.png", api_env={"AI_API_KEY":"sensitive-test", "AI_BASE_URL":"https://provider.invalid/v1", "AI_MODEL":"multi"})
        self.assertNotIn("sensitive-test", repr(run.call_args.args))
        self.assertEqual(run.call_args.kwargs["env"]["AI_API_KEY"], "sensitive-test")

    def test_image_mime_matches_actual_signature(self):
        self.assertTrue(vision.build_data_url(b"\x89PNG\r\n\x1a\nfixture").startswith("data:image/png"))
        self.assertTrue(vision.build_data_url(b"GIF89afixture").startswith("data:image/gif"))
        self.assertTrue(vision.build_data_url(b"RIFF0000WEBPfixture").startswith("data:image/webp"))


if __name__ == "__main__":
    unittest.main()
