# -*- coding: utf-8 -*-
"""booth-bot 纯逻辑单测（不联网、不依赖 nonebot 运行时）。"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "plugins" / "booth_search"))

import booth_client  # noqa: E402
import format as fmt  # noqa: E402
import vision  # noqa: E402


class TestBoothClient(unittest.TestCase):
    def test_build_cmd(self):
        self.assertEqual(booth_client.build_cmd("D:/x/booth.py"),
                         [sys.executable, "-X", "utf8", "D:/x/booth.py"])
        self.assertEqual(booth_client.build_cmd("booth"), ["booth"])

    def _fake_run(self, stdout="", stderr="", returncode=0, timeout_exc=False):
        def run(cmd=None, *args, **kwargs):
            if timeout_exc:
                raise subprocess.TimeoutExpired(cmd="x", timeout=1)
            if cmd and "--version" in cmd:
                return subprocess.CompletedProcess(args=[], returncode=0,
                                                   stdout="booth 1.2.0", stderr="")
            return subprocess.CompletedProcess(args=[], returncode=returncode,
                                               stdout=stdout, stderr=stderr)
        return run

    def test_call_booth_ok(self):
        envelope = json.dumps({"ok": True, "action": "search", "data": {"count": 1}})
        with mock.patch("subprocess.run", self._fake_run(stdout=envelope)):
            data = booth_client.call_booth("search", {"query": "x"})
        self.assertEqual(data, {"count": 1})

    def test_call_booth_error_envelope(self):
        envelope = json.dumps({"ok": False, "error": "参数错了"}, ensure_ascii=False)
        with mock.patch("subprocess.run", self._fake_run(stdout=envelope)):
            with self.assertRaises(booth_client.BoothCliError) as cm:
                booth_client.call_booth("item", {})
        self.assertIn("参数错了", str(cm.exception))

    def test_call_booth_bad_json_and_timeout(self):
        with mock.patch("subprocess.run", self._fake_run(stdout="not json")):
            with self.assertRaises(booth_client.BoothCliError):
                booth_client.call_booth("search", {})
        with mock.patch("subprocess.run", self._fake_run(timeout_exc=True)):
            with self.assertRaises(booth_client.BoothCliError) as cm:
                booth_client.call_booth("search", {}, timeout=5)
        self.assertIn("超时", str(cm.exception))

    def test_resolve_cli_path_missing(self):
        with self.assertRaises(booth_client.BoothCliError):
            booth_client.resolve_cli_path("Z:/no/such/file.py")


class TestFormat(unittest.TestCase):
    def test_price_str(self):
        self.assertEqual(fmt.price_str(5000), "¥5,000")
        self.assertEqual(fmt.price_str("300~"), "300~")
        self.assertEqual(fmt.price_str(None), "价格未知")

    def test_format_results(self):
        items = [
            {"id": 1, "name": "A", "price": 1000, "url": "u1", "via": "bing",
             "is_adult": True, "shop": {"name": "S1"}, "category": "3Dモデル"},
            {"id": 2, "name": "B", "price": None, "shop": {}},
        ]
        out = fmt.format_results(items, max_n=5, title="T")
        self.assertIn("T", out)
        self.assertIn("A  ¥1,000  [bing|R-18]", out)
        self.assertIn("店铺: S1 · 3Dモデル", out)
        self.assertIn("B  价格未知", out)
        self.assertIn("https://booth.pm/ja/items/2", out)  # 缺 URL 时兜底


class TestVision(unittest.TestCase):
    def test_parse_keywords_json(self):
        kws, t = vision.parse_keywords(
            '好的，结果如下：\n```json\n{"keywords": ["シエル", "Ciel"], '
            '"item_type": "3Dモデル", "found_text": "Ciel"}\n```')
        self.assertEqual(kws, ["シエル", "Ciel"])
        self.assertEqual(t, "3Dモデル")

    def test_parse_keywords_fallback(self):
        kws, _ = vision.parse_keywords("シエル\nCiel Vtuber\n3Dモデル")
        self.assertIn("Ciel Vtuber", kws)

    def test_build_messages(self):
        msgs = vision.build_messages("紫色头发", b"\xff\xd8fake")
        content = msgs[0]["content"]
        self.assertIn("用户补充提示：紫色头发", content[0]["text"])
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))

    def test_looks_chinese(self):
        self.assertTrue(vision._looks_chinese("猫娘女仆装"))
        self.assertTrue(vision._looks_chinese("3D头像"))
        self.assertTrue(vision._looks_chinese("信浓 原创VRChat模型"))   # 汉字+英文
        self.assertTrue(vision._looks_chinese("手枪道具 3Dギミック"))    # 简体字特有形优先于假名
        self.assertFalse(vision._looks_chinese("シエル 3Dモデル"))       # 假名=日语
        self.assertFalse(vision._looks_chinese("Ciel avatar"))           # 纯英文

    def test_parse_plan(self):
        kws, dkws, translated = vision.parse_plan(
            '```json\n{"keywords": ["鈴", "ベル"], "desc_keywords": ["Rexouium"], '
            '"translated": true}\n```')
        self.assertEqual(kws, ["鈴", "ベル"])
        self.assertEqual(dkws, ["Rexouium"])
        self.assertTrue(translated)
        kws, dkws, translated = vision.parse_plan(
            '{"keywords": ["シエル"], "translated": false}')
        self.assertEqual(kws, ["シエル"])
        self.assertFalse(translated)

    def test_parse_evaluation(self):
        ev = vision.parse_evaluation(
            '{"verdict": "retry", "reason": "返回的是鸟居", '
            '"keywords": ["鈴", "ベル"]}')
        self.assertEqual(ev["verdict"], "retry")
        self.assertIn("鸟居", ev["reason"])
        self.assertEqual(ev["keywords"], ["鈴", "ベル"])
        ev = vision.parse_evaluation('{"verdict": "ok", "reason": "命中"}')
        self.assertEqual(ev["verdict"], "ok")
        self.assertEqual(ev["keywords"], [])
        with self.assertRaises(RuntimeError):
            vision.parse_evaluation("没有 json 的输出")

    def test_translate_keywords_parse(self):
        # mock HTTP：校验 payload 与关键词解析
        import asyncio

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"choices": [{"message": {"content":
                        '{"keywords": ["猫耳", "メイド服", "3D衣装"]}'}}]}

        class FakeClient:
            def __init__(self, **kw):
                self.payload = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, url, json=None, headers=None):
                self.last_json = json
                return FakeResp()

        captured = {}

        async def run():
            client = FakeClient()
            async def fake_post(url, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                return FakeResp()
            client.post = fake_post
            import booth_search.vision as v
            orig = v.httpx.AsyncClient
            v.httpx.AsyncClient = lambda **kw: client
            try:
                return await v.translate_keywords(
                    "猫娘女仆装", base_url="https://example.test/v1",
                    api_key="test", model="test-model")
            finally:
                v.httpx.AsyncClient = orig

        kws, dkws = asyncio.run(run())
        self.assertEqual(kws, ["猫耳", "メイド服", "3D衣装"])
        self.assertEqual(dkws, [])  # 旧式无 desc_keywords 的输出兼容
        self.assertIn("/chat/completions", captured["url"])
        self.assertEqual(captured["json"]["model"], "test-model")
        self.assertIn("猫娘女仆装", captured["json"]["messages"][0]["content"])


    def test_cli_backend_translate(self):
        # mock subprocess：CLI 后端翻译（含 ANSI 清理与 JSON 提取）
        import asyncio

        class P:
            returncode = 0
            stdout = '\x1b[0m\n> build · mimo\n\x1b[0m\n{"keywords": ["猫耳", "メイド服"]}\n'
            stderr = ""

        def fake_run(cmd, **kw):
            assert cmd[0] == "/fake/opencode" and cmd[1] == "run"
            assert "-m" in cmd and "opencode/mimo-v2.6-flash-free" in cmd
            return P()

        with mock.patch("subprocess.run", fake_run):
            kws, dkws = asyncio.run(vision.translate_keywords_cli(
                "猫娘女仆装", bin_path="/fake/opencode",
                model="opencode/mimo-v2.6-flash-free", timeout=30))
        self.assertEqual(kws, ["猫耳", "メイド服"])
        self.assertEqual(dkws, [])

    def test_cli_backend_failure_raises(self):
        import asyncio

        class P:
            returncode = 1
            stdout = ""
            stderr = "Error: Upstream request failed: Insufficient account funds"

        def fake_run(cmd, **kw):
            return P()

        with mock.patch("subprocess.run", fake_run):
            with self.assertRaises(RuntimeError) as cm:
                asyncio.run(vision.translate_keywords_cli(
                    "x", bin_path="/fake/opencode", model="m", timeout=5))
        self.assertIn("Insufficient", str(cm.exception))

    def test_cli_backend_nonzero_json_missing(self):
        import asyncio

        class P:
            returncode = 0
            stdout = "\x1b[0m no json here"
            stderr = ""

        with mock.patch("subprocess.run", lambda cmd, **kw: P()):
            with self.assertRaises(RuntimeError):
                asyncio.run(vision.translate_keywords_cli(
                    "x", bin_path="/fake/opencode", model="m", timeout=5))


    def test_guard_api_base(self):
        self.assertEqual(vision.guard_api_base("https://opencode.ai/zen/go/v1"),
                         "https://opencode.ai/zen/go/v1")
        for bad in ("http://opencode.ai/zen/go/v1", "https://localhost/v1",
                    "https://127.0.0.1/v1", "https://192.168.1.1/v1",
                    "https://10.0.0.5/v1", "ftp://opencode.ai/v1"):
            with self.assertRaises(RuntimeError):
                vision.guard_api_base(bad)

    def test_api_content_fallback_to_reasoning(self):
        self.assertEqual(vision._api_content(
            {"choices": [{"message": {"content": '{"keywords":["x"]}'}}]}),
            '{"keywords":["x"]}')
        self.assertEqual(vision._api_content(
            {"choices": [{"message": {"content": None,
                                      "reasoning_content": "推理里含答案"}}]}),
            "推理里含答案")


    def test_friendly_ai_error_classification(self):
        import httpx

        def mk_status(code, body=""):
            req = httpx.Request("POST", "https://x.test/v1/chat/completions")
            resp = httpx.Response(code, text=body, request=req)
            return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=resp)

        cases = [
            (mk_status(402), "额度不足"),
            (mk_status(429, "5 hour usage limit exceeded"), "5 小时"),
            (mk_status(429, "weekly usage limit exceeded"), "每周"),
            (mk_status(429, "monthly usage limit exceeded"), "每月"),
            (mk_status(429, "rate limited"), "限流"),
            (mk_status(401), "key 无效"),
            (mk_status(403), "拦截"),
            (mk_status(502), "上游故障"),
        ]
        for exc, expect in cases:
            self.assertIn(expect, vision.friendly_ai_error(exc))
        self.assertIn("网络异常", vision.friendly_ai_error(
            httpx.ConnectError("connection refused")))
        self.assertIn("超时", vision.friendly_ai_error(httpx.ReadTimeout("t")))
        self.assertIn("额度不足", vision.friendly_ai_error(
            RuntimeError("opencode run 失败: Insufficient account funds")))


    def test_parse_translation_desc_keywords(self):
        kws, dkws = vision.parse_translation(
            '```json\n{"keywords": ["ショコラドレス", "衣装"], '
            '"desc_keywords": ["Rexouium", "対応素体"]}\n```')
        self.assertEqual(kws, ["ショコラドレス", "衣装"])
        self.assertEqual(dkws, ["Rexouium", "対応素体"])

    def test_parse_translation_no_desc(self):
        kws, dkws = vision.parse_translation('{"keywords": ["尻尾"]}')
        self.assertEqual(kws, ["尻尾"])
        self.assertEqual(dkws, [])

    def test_parse_translation_fallback_no_json(self):
        kws, dkws = vision.parse_translation("尻尾\nしっぽ\nテイル")
        self.assertIn("尻尾", kws)
        self.assertEqual(dkws, [])

    def test_parse_translation_desc_capped(self):
        _, dkws = vision.parse_translation(
            '{"keywords": ["x"], "desc_keywords": ["a", "b", "c", "d", "e"]}')
        self.assertEqual(len(dkws), 3)

    def test_expand_reading_variants(self):
        has_pykakasi = True
        try:
            import pykakasi  # noqa: F401
        except ImportError:
            has_pykakasi = False
        if not has_pykakasi:
            self.skipTest("pykakasi not installed")
        out = vision.expand_reading_variants(["信濃 3Dモデル"])
        self.assertEqual(out[0], "信濃 3Dモデル")
        self.assertIn("しなの 3Dモデル", out[1:])
        # 无汉字词不产生变体
        self.assertEqual(vision.expand_reading_variants(["ツインテール"]), ["ツインテール"])


if __name__ == "__main__":
    unittest.main()
