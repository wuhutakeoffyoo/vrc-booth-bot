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


if __name__ == "__main__":
    unittest.main()
