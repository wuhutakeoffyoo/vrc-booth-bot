"""B01–B13 的离线回归：禁止真实 AI、网络和 QQ 发送。"""
import asyncio
import json
import sys
import threading
import unittest
import urllib.parse
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
from booth_search import booth_client as bc, image_download as images, qcache, rank, vision, webfind
from booth_search.config import Config


def entry(adult=False, tags=None):
    return {"id": 1000001, "name": "衣装", "price": 100,
            "url": "https://booth.pm/ja/items/1000001", "shop": {},
            "is_adult": adult, "tags": ["VRChat"] if tags is None else tags}


class TestConfigAndPolicy(unittest.TestCase):
    def test_documented_fallback_names_and_legacy(self):
        from nonebot.config import Config as DriverConfig
        driver = DriverConfig(_env_file=None, ai_fallback_api_key="placeholder",
                              ai_fallback_model="review-model",
                              ai_fallback_base_url="https://fallback.invalid/v1")
        cfg = Config.model_validate(driver.model_dump())
        self.assertEqual(cfg.fallback_api_key, "placeholder")
        self.assertEqual(cfg.fallback_model, "review-model")
        self.assertEqual(Config(fallback_api_key="legacy").fallback_api_key, "legacy")

    def test_parent_default_is_explicitly_disabled(self):
        with mock.patch.object(bc, "call_booth", return_value={}) as call:
            bc.search("x", tag=None)
        self.assertIs(call.call_args.args[1]["no_vrc"], True)
        self.assertNotIn("tag", call.call_args.args[1])
        with mock.patch.object(bc, "call_booth", return_value={}) as call:
            bc.search("x", tag="VRChat")
        self.assertEqual(call.call_args.args[1]["tag"], "VRChat")
        self.assertIs(call.call_args.args[1]["no_vrc"], True)

    def test_compatibility_states_are_honest(self):
        negative = dict(entry(), _desc="Rexouiumには対応していません。")
        self.assertEqual(rank.description_status(negative, ["Rexouium"]), "unsupported")
        self.assertFalse(rank.desc_hit(negative, ["Rexouium"]))
        self.assertEqual(rank.description_status(
            {"name": "Rexouium", "detail_status": "unavailable"}, ["Rexouium"]), "title_only")

    def test_candidate_evidence_is_unique_and_in_range(self):
        titles = ["1. A", "2. B", "3. C"]
        for hits in ([], ["1", "1", "1"], ["1", "2", "99"], ["A"]):
            self.assertEqual(vision.validate_evaluation(
                {"verdict": "ok", "hits": hits}, titles)["verdict"], "retry")
        self.assertEqual(vision.validate_evaluation(
            {"verdict": "ok", "hits": ["1", "2", "3"]}, titles)["verdict"], "ok")

    def test_web_links_require_exact_booth_host(self):
        urls = ["https://evilbooth.pm/items/1000001",
                "https://booth.pm.evil.invalid/items/1000002",
                "https://evil.invalid/?url=https://booth.pm/items/1000003",
                "https://booth.pm/ja/items/1000004", "https://[invalid/"]
        self.assertEqual(webfind.item_ids_from_urls(urls), [1000004])
        self.assertEqual(webfind.ids_from_ddg_html(
            '<a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fbooth.pm%2Fja%2Fitems%2F1000004&amp;rut=x">x</a>'),
            [1000004])


class TestBotFlows(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.cfg = Config(ai_mode="api", vision_api_key="", fallback_api_key="",
                          vision_base_url="https://api.invalid/v1", vision_model="review-model",
                          fallback_base_url="https://fallback.invalid/v1",
                          recall_enabled=False, websearch_fallback=False, plan_cache_ttl=0)
        patches = [
            mock.patch.object(bs, "plugin_config", self.cfg),
            mock.patch.object(vision, "resolve_cli_bin", side_effect=RuntimeError("no CLI")),
            mock.patch.object(bs, "_enrich_entries", new=mock.AsyncMock()),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    async def _nonblocking(self, operation, callback):
        started, finished, release = threading.Event(), threading.Event(), threading.Event()
        def slow(*a, **kw):
            started.set()
            release.wait(2)
            finished.set()
            return callback()
        async def heartbeat():
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            try:
                self.assertFalse(finished.is_set(), "CLI blocked unrelated coroutine")
            finally:
                release.set()
        with operation(slow):
            await asyncio.gather(self.run_flow(), heartbeat())

    def admit_image_fixture(self):
        patch = mock.patch.object(bs, "_verified_image_backend", new=mock.AsyncMock(
            return_value={"base_url":"https://api.invalid/v1", "api_key":"placeholder", "model":"m"}))
        patch.start()
        self.addCleanup(patch.stop)

    async def test_image_cli_does_not_block_event_loop(self):
        self.admit_image_fixture()
        self.run_flow = lambda: bs._handle_image_bytes(b"fixture", "")
        await self._nonblocking(
            lambda slow: mock.patch.object(bc, "imgsearch", side_effect=slow),
            lambda: {"matches": [entry()]})

    async def test_image_keyword_cli_does_not_block_event_loop(self):
        self.admit_image_fixture()
        self.run_flow = lambda: bs._handle_image_bytes(b"fixture", "")
        with mock.patch.object(bc, "imgsearch", return_value={"derived_query": "衣装"}):
            await self._nonblocking(
                lambda slow: mock.patch.object(bc, "search", side_effect=slow),
                lambda: {"items": [entry()]})

    async def test_web_details_do_not_block_event_loop(self):
        self.cfg.websearch_fallback = True
        self.run_flow = lambda: bs._webfind_entries("衣装", [], "exclude")
        with mock.patch.object(webfind, "find_booth_item_ids", new=mock.AsyncMock(return_value=[1])):
            await self._nonblocking(lambda slow: mock.patch.object(bc, "item", side_effect=slow), entry)

    async def test_image_results_are_filtered(self):
        self.admit_image_fixture()
        self.cfg.r18_mode = "exclude"
        with mock.patch.object(bc, "imgsearch", return_value={
                "matches": [entry(True, ["Illustration"])]}):
            result = await bs._handle_image_bytes(b"fixture", "")
        self.assertEqual(result["entries"], [])

    async def test_web_results_obey_adult_and_vrc(self):
        self.cfg.websearch_fallback = True
        with mock.patch.object(webfind, "find_booth_item_ids", new=mock.AsyncMock(return_value=[1])):
            for adult, result in (("exclude", entry(True)), ("only", entry(False)),
                                  ("include", entry(False, ["Illustration"]))):
                with mock.patch.object(bc, "item", return_value=result):
                    self.assertEqual(await bs._webfind_entries("x", [], adult), [])

    async def test_fallback_only_plans_without_empty_primary_call(self):
        self.cfg.fallback_api_key = "placeholder"
        with mock.patch.object(vision, "plan_search",
                               new=mock.AsyncMock(return_value=(["鈴"], [], True))) as plan:
            self.assertEqual(bs._ai_backend()[0], "api")
            self.assertEqual((await bs._ai_plan("铃铛"))[0], ["鈴"])
        self.assertEqual(plan.await_count, 1)
        self.assertEqual(plan.call_args.kwargs["api_key"], "placeholder")

    async def test_failed_primary_calls_fallback(self):
        self.cfg.vision_api_key = "primary"
        self.cfg.fallback_api_key = "fallback"
        with mock.patch.object(vision, "plan_search", new=mock.AsyncMock(
                side_effect=[RuntimeError("offline failure"), (["鈴"], [], True)])) as plan:
            self.assertEqual((await bs._ai_plan("铃铛"))[0], ["鈴"])
        self.assertEqual(plan.await_count, 2)
        self.assertEqual(plan.call_args.kwargs["api_key"], "fallback")

    async def test_cli_mode_runs_active_plan_and_evaluate(self):
        self.cfg.ai_mode = "cli"
        with mock.patch.object(vision, "resolve_cli_bin", return_value="review-cli"), \
                mock.patch.object(vision, "plan_search_cli",
                                  new=mock.AsyncMock(return_value=(["鈴"], [], True))) as plan, \
                mock.patch.object(vision, "evaluate_results_cli",
                                  new=mock.AsyncMock(return_value={"verdict": "retry"})) as evaluate:
            await bs._ai_plan("铃铛")
            await bs._ai_evaluate("铃铛", ["鈴"], ["1. 鈴"])
        plan.assert_awaited_once()
        evaluate.assert_awaited_once()

    async def test_cli_wrappers_use_plan_and_evaluation_protocol(self):
        with mock.patch.object(vision, "_cli_run_sync", side_effect=[
                '{"keywords":["鈴"],"translated":true}',
                '{"verdict":"ok","hits":["1","2","3"]}']) as run:
            self.assertEqual((await vision.plan_search_cli(
                "铃铛", bin_path="review", model="review"))[0], ["鈴"])
            self.assertEqual((await vision.evaluate_results_cli(
                "铃铛", ["鈴"], ["1. A", "2. B", "3. C"],
                bin_path="review", model="review"))["verdict"], "ok")
        self.assertIn("制定站内搜索方案", run.call_args_list[0].args[1][-1])
        self.assertIn("至少列出 3 条", run.call_args_list[1].args[1][-1])

    async def test_forward_receives_page_and_mode(self):
        result = {"text": "x", "entries": [entry(True)], "page": 2, "qhint": "衣装",
                  "total": 100, "command": "r18", "has_next": True}
        bot = SimpleNamespace(self_id="1")
        with mock.patch.object(bs.forward, "build_result_nodes",
                               new=mock.AsyncMock(return_value=[{}])) as build, \
                mock.patch.object(bs.forward, "send", new=mock.AsyncMock(return_value=True)):
            await bs._send_result(bot, SimpleNamespace(), result)
        self.assertEqual(build.call_args.kwargs["page"], 2)
        self.assertEqual(build.call_args.kwargs["query_hint"], "衣装")
        self.assertEqual(build.call_args.kwargs["total"], 100)
        self.assertEqual(build.call_args.kwargs["command"], "r18")

    async def test_forward_r18_hint_and_last_page(self):
        with mock.patch.object(bs.forward, "_image_segment", new=mock.AsyncMock(return_value=None)):
            nodes = await bs.forward.build_result_nodes(
                1, "x", [], [entry()], page=2, query_hint="衣装",
                command="r18", has_next=True)
            self.assertIn("/vrc r18 衣装 3", str(nodes[0]))
            nodes = await bs.forward.build_result_nodes(
                1, "x", [], [entry()], page=2, total=100, has_next=False)
            self.assertNotIn("翻下一页", str(nodes[0]))

    async def test_query_cache_changes_with_configuration(self):
        cache = {}
        event = SimpleNamespace(get_plaintext=lambda: "/vrc search 衣装", get_message=lambda: [])
        def put(key, result, **kw):
            cache[key] = result
        with mock.patch.object(qcache, "get", side_effect=lambda key, ttl: cache.get(key)), \
                mock.patch.object(qcache, "put", side_effect=put), \
                mock.patch.object(bs, "_send_result", new=mock.AsyncMock()), \
                mock.patch.object(bs.matcher, "send", new=mock.AsyncMock()), \
                mock.patch.object(bs, "_handle_text", new=mock.AsyncMock(
                    return_value={"text": "x", "entries": [entry()]})) as search:
            await bs._do_search(SimpleNamespace(), event)
            await bs._do_search(SimpleNamespace(), event)
            self.assertEqual(search.await_count, 1)
            for name, value in (("booth_sort", "new"), ("vrc_tag", ""), ("booth_limit", 1),
                                ("vision_base_url", "https://other.invalid/v1")):
                setattr(self.cfg, name, value)
                await bs._do_search(SimpleNamespace(), event)
            self.assertEqual(search.await_count, 5)

    async def test_all_search_failures_are_not_no_matches(self):
        with mock.patch.object(bc, "search", side_effect=bc.BoothCliError("HTTP 503")):
            result = await bs._handle_text("衣装")
        self.assertIn("搜索服务暂不可用", result["text"])
        self.assertNotIn("没搜到", result["text"])

    async def test_successful_empty_query_remains_no_matches(self):
        with mock.patch.object(bc, "search", return_value={"items": [], "total": 0}):
            result = await bs._handle_text("衣装")
        self.assertIn("没搜到", result["text"])

    async def test_image_backend_failure_is_not_no_matches(self):
        self.admit_image_fixture()
        with mock.patch.object(bc, "imgsearch", side_effect=bc.BoothCliError("HTTP 503")):
            result = await bs._handle_image_bytes(b"fixture", "")
        self.assertIn("搜索服务暂不可用", result["text"])
        self.assertNotIn("没找到", result["text"])

    async def test_negative_or_missing_description_is_never_confirmed(self):
        for item in (dict(entry(), _desc="Rexouiumには対応していません。"),
                     dict(entry(), name="Rexouium", detail_status="unavailable")):
            result = await bs._merged_zh_result(
                "Rexouium 衣装", [item], {}, ["衣装"], ["Rexouium"], 1, "")
            self.assertNotIn("已按商品说明核实", result["header"])
            self.assertNotIn("已确认", result["header"])

    async def test_second_round_empty_hits_is_unconfirmed(self):
        self.cfg.vision_api_key = "placeholder"
        with mock.patch.object(bs, "_ai_plan", new=mock.AsyncMock(return_value=(["衣装"], [], True))), \
                mock.patch.object(bs, "_search_merged", new=mock.AsyncMock(
                    side_effect=[([entry()], {"total": 1}), ([entry()], {"total": 1})])), \
                mock.patch.object(bs, "_ai_evaluate", new=mock.AsyncMock(side_effect=[
                    {"verdict": "retry", "keywords": ["ドレス"]},
                    {"verdict": "ok", "hits": []}])):
            result = await bs._handle_text("衣装")
        self.assertNotIn("第二轮结果已按需求确认", result["header"])
        self.assertIn("未完全确认", result["header"])

    async def test_ddg_post_form_is_encoded_once(self):
        import httpx
        values = []
        def respond(request):
            values.append(urllib.parse.parse_qs(request.content.decode())["q"][0])
            return httpx.Response(200, text="")
        client = httpx.AsyncClient
        with mock.patch.object(webfind.httpx, "AsyncClient",
                               side_effect=lambda **kw: client(transport=httpx.MockTransport(respond), **kw)):
            await webfind.ddg_find("尻尾 + 50%")
        self.assertEqual(values, ["尻尾 + 50% booth.pm"])

    async def test_authenticated_http_clients_never_follow_redirects(self):
        import httpx
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(302, headers={"Location": "https://evil.invalid/collect"})
        client = httpx.AsyncClient
        with mock.patch.object(httpx, "AsyncClient", side_effect=lambda **kw:
                client(transport=httpx.MockTransport(respond), **kw)), \
                mock.patch.object(vision.provider_api.socket, "getaddrinfo",
                    return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]):
            with self.assertRaises(httpx.HTTPStatusError):
                await vision._api_post("https://api.invalid/v1/chat/completions", {}, "placeholder")
            self.assertEqual(await webfind.exa_find("衣装", "placeholder"), [])
        self.assertEqual(len(requests), 2)
        self.assertTrue(all(req.url.host != "evil.invalid" for req in requests))

    async def test_message_file_url_never_reads_local_file(self):
        with mock.patch.object(bs, "_handle_image_bytes", new=mock.AsyncMock()) as consume:
            result = await bs._handle_image("file:///private/review", "")
        consume.assert_not_awaited()
        self.assertEqual(result["entries"], [])


class TestImageBoundaries(unittest.TestCase):
    def _response(self, status=200, data=None, headers=None):
        data = data if data is not None else b"\x89PNG\r\n\x1a\n" + b"x" * 100
        resp = mock.Mock(status=status)
        resp.getheader.side_effect = lambda key: (headers or {}).get(key)
        resp.read.side_effect = lambda limit: data[:limit]
        return resp

    def _dns(self, ip="93.184.216.34"):
        return [(2, 1, 6, "", (ip, 443))]

    def test_private_dns_and_unapproved_hosts_are_rejected(self):
        for url, hosts, ip in (
            ("http://gchat.qpic.cn/a", images.DEFAULT_HOSTS, "93.184.216.34"),
            ("https://evil.invalid/a", images.DEFAULT_HOSTS, "93.184.216.34"),
            ("https://gchat.qpic.cn/a", images.DEFAULT_HOSTS, "127.0.0.1"),
            ("https://gchat.qpic.cn/a", images.DEFAULT_HOSTS, "192.168.1.1"),
            ("https://gchat.qpic.cn/a", images.DEFAULT_HOSTS, "::1"),
        ):
            with mock.patch.object(images.socket, "getaddrinfo", return_value=self._dns(ip)), \
                    mock.patch.object(images, "_PinnedHTTPSConnection") as connection:
                with self.assertRaises(images.ImageDownloadError):
                    images._download_sync(url, hosts, 1, 1000)
                connection.assert_not_called()

    def test_private_or_http_redirect_is_rejected_before_second_connection(self):
        for target in ("http://127.0.0.1/review", "https://private.invalid/review"):
            conn = mock.Mock()
            conn.getresponse.return_value = self._response(302, headers={"Location": target})
            def resolve(host, *a, **kw):
                return self._dns("127.0.0.1" if host == "private.invalid" else "93.184.216.34")
            with mock.patch.object(images.socket, "getaddrinfo", side_effect=resolve), \
                    mock.patch.object(images, "_PinnedHTTPSConnection", return_value=conn) as connection:
                with self.assertRaises(images.ImageDownloadError):
                    images._download_sync("https://gchat.qpic.cn/a",
                        (*images.DEFAULT_HOSTS, "private.invalid"), 1, 1000)
                self.assertEqual(connection.call_count, 1)
                conn.close.assert_called()

    def test_legal_image_size_and_magic(self):
        for data, headers, expected in (
            (b"\x89PNG\r\n\x1a\n" + b"x" * 100, {}, True),
            (b"not an image" * 20, {}, False),
            (b"\xff\xd8\xff" + b"x" * 1100, {}, False),
            (b"\xff\xd8\xff" + b"x" * 100, {"Content-Length": "2000"}, False),
        ):
            conn = mock.Mock()
            conn.getresponse.return_value = self._response(data=data, headers=headers)
            with mock.patch.object(images.socket, "getaddrinfo", return_value=self._dns()), \
                    mock.patch.object(images, "_PinnedHTTPSConnection", return_value=conn) as connection:
                if expected:
                    self.assertEqual(images._download_sync(
                        "https://gchat.qpic.cn/a", images.DEFAULT_HOSTS, 1, 1000), data)
                    self.assertEqual(connection.call_args.args[1], "93.184.216.34")
                else:
                    with self.assertRaises(images.ImageDownloadError):
                        images._download_sync("https://gchat.qpic.cn/a", images.DEFAULT_HOSTS, 1, 1000)
                conn.close.assert_called()

    def test_tls_uses_original_host_and_validated_ip(self):
        conn = images._PinnedHTTPSConnection("gchat.qpic.cn", "93.184.216.34", 1)
        self.assertTrue(conn._context.check_hostname)
        raw = mock.Mock()
        with mock.patch.object(images.socket, "create_connection", return_value=raw) as connect, \
                mock.patch.object(conn._context, "wrap_socket", return_value=mock.Mock()) as tls:
            conn.connect()
        self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 443))
        self.assertEqual(tls.call_args.kwargs["server_hostname"], "gchat.qpic.cn")
