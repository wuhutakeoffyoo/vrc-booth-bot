"""Inherited credentials cannot enable internal AI in the default tool mode."""
import sys
import unittest
from pathlib import Path
from unittest import mock

import nonebot

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/plugins"))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(log_level="ERROR")
import booth_search as bs
from booth_search import booth_client, vision, qcache
from booth_search.config import Config


class TestCallerMode(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = Config(ai_api_key="inherited", ai_base_url="https://ai.invalid/v1",
                          ai_fallback_api_key="fallback", ai_fallback_base_url="https://fallback.invalid/v1",
                          websearch_fallback=False, vrc_tag="")
        patch = mock.patch.object(bs, "plugin_config", self.cfg)
        patch.start()
        self.addCleanup(patch.stop)

    async def test_default_text_uses_local_terms_no_plan_evaluation_or_api(self):
        candidate = dict(id=1, name="鈴", price=0, shop={}, url="https://booth.pm/items/1",
                         detail_status="available", _desc="鈴のアクセサリーです")
        with mock.patch.object(bs, "_cached_plan", new=mock.AsyncMock()) as plan, \
                mock.patch.object(bs, "_ai_evaluate", new=mock.AsyncMock()) as evaluate, \
                mock.patch.object(vision.provider_api, "request_json") as api, \
                mock.patch.object(vision, "_api_post", new=mock.AsyncMock()) as wire, \
                mock.patch.object(vision, "resolve_cli_bin") as cli, \
                mock.patch.object(bs, "_search_merged", new=mock.AsyncMock(return_value=([candidate], {"total": 1}))) as lookup, \
                mock.patch.object(bs, "_enrich_entries", new=mock.AsyncMock()):
            result = await bs._handle_text("铃铛")
        self.assertEqual(self.cfg.ai_mode, "caller")
        self.assertIn("鈴", lookup.call_args.args[0])
        self.assertEqual(result["entries"][0]["relevance_status"], "unknown")
        for call in (plan, evaluate, wire):
            call.assert_not_awaited()
        api.assert_not_called()
        cli.assert_not_called()

    async def test_startup_and_image_paths_do_not_probe_or_read_even_with_keys(self):
        with mock.patch.object(vision.provider_api, "image_capability") as probe, \
                mock.patch.object(bs, "_download_image", new=mock.AsyncMock()) as download, \
                mock.patch.object(bs.tempfile, "NamedTemporaryFile") as file, \
                mock.patch.object(booth_client, "imgsearch") as reverse:
            await bs.check_image_capability()
            result = await bs._handle_image("https://gchat.qpic.cn/private.png", "hint")
            injected = await bs._handle_image_bytes(b"image", "hint")
        for value in (result, injected):
            self.assertEqual(value["entries"], [])
            self.assertIn("文字搜索", value["text"])
        probe.assert_not_called()
        download.assert_not_awaited()
        file.assert_not_called()
        reverse.assert_not_called()

    async def test_internal_helpers_cannot_bypass_caller_policy(self):
        with mock.patch.object(vision, "_api_post", new=mock.AsyncMock()) as wire, \
                mock.patch.object(vision, "resolve_cli_bin") as cli:
            for call in (bs._ai_plan("hint"), bs._ai_evaluate("hint", [], []),
                         bs._ai_translate("hint"), bs._ai_recall("hint")):
                with self.assertRaises(bs.BoothUnavailable):
                    await call
            self.assertEqual(bs._configured_apis(), [])
            self.assertEqual(bs._ai_backend(), ("", ""))
        wire.assert_not_awaited()
        cli.assert_not_called()

    async def test_explicit_api_mode_preserves_selected_provider_and_cache_isolation(self):
        default_key = qcache.configuration_key(self.cfg)
        self.cfg.ai_mode = "api"
        self.assertEqual(bs._ai_backend(), ("api", ""))
        self.assertNotEqual(default_key, qcache.configuration_key(self.cfg))
        with mock.patch.object(vision, "plan_search", new=mock.AsyncMock(return_value=(["鈴"], [], True))) as plan:
            self.assertEqual((await bs._ai_plan("铃铛"))[0], ["鈴"])
        plan.assert_awaited_once()


class TestToolClient(unittest.TestCase):
    def test_workflow_adapter_only_forwards_caller_terms(self):
        with mock.patch.object(booth_client, "call_booth", return_value={"ai_calls": 0}) as call:
            result = booth_client.workflow("桔梗衣装", keywords=["桔梗 衣装"], require_terms=["桔梗"], desc_len=-1)
        self.assertEqual(result["ai_calls"], 0)
        self.assertEqual(call.call_args.args[0], "workflow")
        self.assertEqual(call.call_args.args[1]["keyword"], ["桔梗 衣装"])
        self.assertNotIn("api_env", call.call_args.kwargs)

    def test_image_adapter_opt_in_is_false_by_default(self):
        for selected in (False, True):
            with mock.patch.object(booth_client, "call_booth", return_value={}) as call:
                booth_client.imgsearch("fixture.png", delegate_ai=selected)
            self.assertIs(call.call_args.args[1]["delegate_ai"], selected)


if __name__ == "__main__":
    unittest.main()
