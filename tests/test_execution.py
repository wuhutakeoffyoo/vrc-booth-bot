"""Cancellation, shared work, page-plan cache and real plugin-path regressions."""
import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "plugins"))
import nonebot
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()
import booth_search as bs
from booth_search import booth_client as bc, execution, qcache, vision
from booth_search.config import Config


def entry(i=1):
    return dict(id=i, name=f"衣装 {i}", price=100, shop={}, tags=["VRChat"],
                is_adult=False, url=f"https://booth.pm/items/{i}")


class TestSingleFlight(unittest.IsolatedAsyncioTestCase):
    async def test_six_waiters_share_one_producer_and_copy_results(self):
        flight = execution.SingleFlight()
        release = asyncio.Event()
        calls = []
        async def factory(notify):
            calls.append(1)
            await release.wait()
            return {"entries": [entry()]}
        tasks = [asyncio.create_task(flight.run("same", factory)) for _ in range(6)]
        await asyncio.sleep(0.01)
        release.set()
        values = await asyncio.gather(*tasks)
        self.assertEqual(len(calls), 1)
        values[0]["entries"][0]["name"] = "changed"
        self.assertEqual(values[1]["entries"][0]["name"], "衣装 1")
        await asyncio.sleep(0)
        self.assertEqual(flight.running, {})

    async def test_cancelled_waiter_does_not_cancel_producer_or_other_listener(self):
        flight = execution.SingleFlight()
        started, release = asyncio.Event(), asyncio.Event()
        notices = [[], []]
        calls = []
        async def factory(notify):
            calls.append(1)
            started.set()
            await release.wait()
            await notify("second round")
            return {"value": 1}
        async def first(message):
            notices[0].append(message)
        async def second(message):
            notices[1].append(message)
        tasks = [asyncio.create_task(flight.run("same", factory, listener=cb)) for cb in (first, second)]
        await started.wait()
        tasks[0].cancel()
        with self.assertRaises(asyncio.CancelledError):
            await tasks[0]
        release.set()
        self.assertEqual(await tasks[1], {"value": 1})
        self.assertEqual(calls, [1])
        self.assertEqual(notices, [[], ["second round"]])

    async def test_timeout_and_failure_clear_inflight_for_next_attempt(self):
        flight = execution.SingleFlight()
        async def failure(notify):
            raise RuntimeError("offline failure")
        for _ in range(2):
            with self.assertRaises(RuntimeError):
                await flight.run("same", failure)
            self.assertEqual(flight.running, {})
        async def slow(notify):
            await asyncio.sleep(1)
        with self.assertRaises(asyncio.TimeoutError):
            await flight.run("same", slow, timeout=0.01)
        self.assertEqual(flight.running, {})


class TestActiveSearch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = Config(ai_mode="api", vision_api_key="placeholder", fallback_api_key="",
                          vision_base_url="https://api.invalid/v1", vision_model="review-model",
                          recall_enabled=False, websearch_fallback=False)
        self.cache = {}
        def put(key, value, **kwargs):
            self.cache[key] = json.loads(json.dumps(value))
        for patch in (
            mock.patch.object(bs, "plugin_config", self.cfg),
            mock.patch.object(bs, "_plan_flights", execution.SingleFlight()),
            mock.patch.object(bs, "_result_flights", execution.SingleFlight()),
            mock.patch.object(bs, "_global_gate", bs.access.ConcurrencyGate(2)),
            mock.patch.object(qcache, "get", side_effect=lambda key, ttl: self.cache.get(key)),
            mock.patch.object(qcache, "put", side_effect=put),
            mock.patch.object(qcache, "semantic_fingerprint", return_value="offline-source"),
            mock.patch.object(vision, "expand_reading_variants", side_effect=lambda value: value),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    async def test_page_two_reuses_plan_but_executes_new_page_search(self):
        with mock.patch.object(bs, "_ai_plan", new=mock.AsyncMock(return_value=(["衣装"], [], True))) as plan, \
                mock.patch.object(bs, "_search_merged", new=mock.AsyncMock(return_value=([entry()], {"total": 100}))) as search, \
                mock.patch.object(bs, "_enrich_entries", new=mock.AsyncMock()), \
                mock.patch.object(bs, "_ai_evaluate", new=mock.AsyncMock(return_value={"verdict": "retry", "keywords": []})):
            await bs._handle_text("衣装", page=1)
            await bs._handle_text("衣装", page=2)
        self.assertEqual(plan.await_count, 1)
        self.assertEqual([call.args[2] for call in search.call_args_list], [1, 2])

    async def test_full_description_before_evaluation_and_no_repeat_details(self):
        items = [entry(i) for i in range(1, 9)]
        async def enrich(values, desc_len=200):
            self.assertEqual(desc_len, -1)
            for value in values:
                value.update(_desc="説明 " * 2000 + "Rexouium 対応", detail_status="available")
        async def evaluate(hint, kws, lines):
            self.assertIn("Rexouium 対応", "\n".join(lines))
            self.assertTrue(all(it.get("detail_status") == "available" for it in items[:6]))
            return {"verdict": "ok", "hits": ["1", "2", "3"], "evidence": [
                dict(item_id=str(i), field="description", quote="Rexouium 対応", status="related")
                for i in (1, 2, 3)]}
        with mock.patch.object(bs, "_ai_plan", new=mock.AsyncMock(return_value=(["衣装"], ["Rexouium"], True))), \
                mock.patch.object(bs, "_search_merged", new=mock.AsyncMock(return_value=(items, {"total": 8}))), \
                mock.patch.object(bs, "_enrich_entries", new=mock.AsyncMock(side_effect=enrich)) as detail, \
                mock.patch.object(bs, "_ai_evaluate", new=mock.AsyncMock(side_effect=evaluate)):
            value = await bs._handle_text("Rexouium 衣装")
        self.assertEqual(detail.await_count, 1)
        self.assertEqual(len(detail.call_args.args[0]), 6)
        self.assertIn("商品说明原文", value["text"])
        self.assertIn("相关性未核实", value["text"])
        self.assertNotIn("已确认", value["text"])

    async def test_second_round_normalizes_then_drops_previous_terms(self):
        searches = []
        async def search(kws, *args):
            searches.append(kws)
            return [entry()], {"total": 1}
        with mock.patch.object(bs, "_ai_plan", new=mock.AsyncMock(return_value=(["衣装"], [], True))), \
                mock.patch.object(bs, "_search_merged", new=mock.AsyncMock(side_effect=search)), \
                mock.patch.object(bs, "_enrich_entries", new=mock.AsyncMock()), \
                mock.patch.object(vision, "apply_industry_synonyms", wraps=vision.apply_industry_synonyms) as seeds, \
                mock.patch.object(bs, "_ai_evaluate", new=mock.AsyncMock(side_effect=[
                    {"verdict": "retry", "keywords": ["衣装", "銃", "ドレス"]},
                    {"verdict": "retry", "keywords": []}])):
            await bs._handle_text("衣装")
        self.assertEqual(seeds.call_count, 2)
        self.assertNotIn("衣装", searches[1])
        self.assertLessEqual(len(searches[1]), 3)

    async def test_repeated_retry_terms_do_not_send_another_search(self):
        with mock.patch.object(bs, "_ai_plan", new=mock.AsyncMock(return_value=(["衣装"], [], True))), \
                mock.patch.object(bs, "_search_merged", new=mock.AsyncMock(return_value=([entry()], {}))) as search, \
                mock.patch.object(bs, "_enrich_entries", new=mock.AsyncMock()), \
                mock.patch.object(bs, "_ai_evaluate", new=mock.AsyncMock(
                    return_value={"verdict": "retry", "keywords": ["衣装"]})):
            value = await bs._handle_text("衣装")
        self.assertEqual(search.await_count, 1)
        self.assertIn("新的有效检索词", value["text"])

    async def test_single_page_candidate_pool_does_not_request_more_pages(self):
        self.cfg.booth_limit = 6
        with mock.patch.object(bc, "search", return_value={"items": [entry(i) for i in range(1, 51)]}) as search:
            items, result = await bs._search_merged(["衣装"])
        self.assertEqual(search.call_count, 1)
        self.assertEqual(search.call_args.kwargs["limit"], 60)
        self.assertEqual(search.call_args.kwargs["page"], 1)
        self.assertEqual(len(items), 50)

    async def test_duplicate_events_share_gate_and_notify_their_own_event(self):
        started, release = asyncio.Event(), asyncio.Event()
        events = [SimpleNamespace(get_plaintext=lambda: "/vrc search 衣装", get_message=lambda: [], user_id=i)
                  for i in range(6)]
        bot = SimpleNamespace(send=mock.AsyncMock())
        async def search(hint, page=1, notify=None):
            started.set()
            await release.wait()
            await notify("second round")
            return {"text": "offline result", "entries": [entry()]}
        with mock.patch.object(bs, "_handle_text", new=mock.AsyncMock(side_effect=search)) as work, \
                mock.patch.object(bs.matcher, "send", new=mock.AsyncMock()), \
                mock.patch.object(bs, "_send_result", new=mock.AsyncMock()) as send:
            tasks = [asyncio.create_task(bs._do_search(bot, event)) for event in events]
            await started.wait()
            await asyncio.sleep(0.01)
            release.set()
            await asyncio.gather(*tasks)
        self.assertEqual(work.await_count, 1)
        self.assertEqual(send.await_count, 6)
        self.assertEqual(sorted(call.args[0].user_id for call in bot.send.call_args_list), list(range(6)))

    async def test_benchmark_blocks_all_ai_entrypoints_and_uses_other_cache_key(self):
        production_key = qcache.configuration_key(self.cfg)
        self.cfg.run_profile = "benchmark"
        self.assertNotEqual(production_key, qcache.configuration_key(self.cfg))
        with mock.patch.object(vision, "plan_search", new=mock.AsyncMock()) as plan, \
                mock.patch.object(vision, "recall_products", new=mock.AsyncMock()) as recall:
            self.assertEqual(bs._ai_backend(), ("", ""))
            with self.assertRaises(bs.BoothUnavailable):
                await bs._ai_plan("衣装")
            with self.assertRaises(bs.BoothUnavailable):
                await bs._ai_recall("衣装")
            with self.assertRaises(bs.BoothUnavailable):
                await bs._ai_evaluate("衣装", [], [])
            with self.assertRaises(bs.BoothUnavailable):
                await bs._ai_vision("fixture", "衣装")
        plan.assert_not_awaited()
        recall.assert_not_awaited()


class TestContextAndFingerprint(unittest.IsolatedAsyncioTestCase):
    async def test_query_context_survives_worker_thread_and_counts_failed_envelope(self):
        bc._cli_verified = None
        value = {"ok": False, "error": "预算耗尽", "request_budget": {"used": 12, "maximum": 12}}
        process = SimpleNamespace(stdout=json.dumps(value), stderr="", returncode=0)
        with mock.patch.object(bc, "resolve_cli_path", return_value="review-cli"), \
                mock.patch.object(bc, "_verify_cli_supports_bot"), \
                mock.patch.object(bc.subprocess, "run", return_value=process) as run, \
                execution.query_scope() as context:
            with self.assertRaises(bc.BoothCliError):
                await asyncio.to_thread(bc.search, "衣装")
            payload = json.loads(run.call_args.args[0][-1])
            self.assertEqual(payload["context"]["request_id"], context["request_id"])
            self.assertEqual(context["wire_count"], 12)
            self.assertLessEqual(run.call_args.kwargs["timeout"], 180)

    async def test_round_two_extends_cap_not_deadline(self):
        with execution.query_scope(12, 180):
            original = execution.request_context()
            execution.extend_budget(18)
            value = execution.request_context()
            self.assertEqual(value["request_id"], original["request_id"])
            self.assertEqual(value["deadline"], original["deadline"])
            self.assertEqual(value["max_requests"], 18)

    async def test_fingerprint_tracks_source_and_cli_not_docs_or_env(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bot, cli = root / "bot", root / "cli"
            bot.mkdir()
            cli.mkdir()
            botfile = bot / "qcache.py"
            clifile = cli / "booth.py"
            botfile.write_text("cache-v1", encoding="utf-8")
            clifile.write_text("cli-v1", encoding="utf-8")
            with mock.patch.object(qcache, "__file__", str(botfile)):
                def fingerprint():
                    qcache.semantic_fingerprint.cache_clear()
                    return qcache.semantic_fingerprint(str(clifile))
                first = fingerprint()
                (bot / ".env").write_text("PLACEHOLDER_ONLY", encoding="utf-8")
                (bot / "README.md").write_text("documentation", encoding="utf-8")
                self.assertEqual(fingerprint(), first)
                (bot / "vision.py").write_text("new-prompt", encoding="utf-8")
                second = fingerprint()
                self.assertNotEqual(second, first)
                clifile.write_text("cli-v2", encoding="utf-8")
                self.assertNotEqual(fingerprint(), second)
        qcache.semantic_fingerprint.cache_clear()


if __name__ == "__main__":
    unittest.main()
