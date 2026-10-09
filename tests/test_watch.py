# -*- coding: utf-8 -*-
"""watch（关注清单）纯逻辑单测：ID 解析/消息格式化/正则匹配。不联网。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init()
import booth_search as bs  # noqa: E402


class TestWatchParsing(unittest.TestCase):
    def test_watch_re(self):
        for ok_text in ("/vrc watch 123", "vrc watch https://booth.pm/ja/items/456",
                        "/vrc watch list", "/VRC WATCH check", "/vrc watch"):
            self.assertTrue(bs.WATCH_RE.match(ok_text), ok_text)
        for bad in ("/vrc search 猫耳", "/vrc r18 x", "/vrcwatch", "vrc watcher"):
            self.assertFalse(bs.WATCH_RE.match(bad), bad)

    def test_parse_ids(self):
        self.assertEqual(bs._parse_watch_ids("123 456"), [123, 456])
        self.assertEqual(
            bs._parse_watch_ids("https://booth.pm/ja/items/3368697 shop.booth.pm/items/100"),
            [3368697, 100])
        self.assertEqual(bs._parse_watch_ids("123 123"), [123])  # 去重
        with self.assertRaises(ValueError):
            bs._parse_watch_ids("abc")

    def test_format_changes(self):
        text = bs.format_watch_changes({"checked": 2, "changed": [
            {"id": 1, "name": "商品A", "price": 800, "changes": ["价格 1,000 → 800 円（↓）"]}],
            "errors": []})
        self.assertIn("商品A", text)
        self.assertIn("https://booth.pm/ja/items/1", text)
        self.assertIn("↓", text)
        quiet = bs.format_watch_changes({"checked": 3, "changed": [], "errors": []})
        self.assertIn("暂无变动", quiet)
        err = bs.format_watch_changes({"checked": 1, "changed": [],
                                       "errors": [{"id": 9, "error": "HTTP 429"}]})
        self.assertIn("429", err)

    def test_item_and_for_regexes(self):
        for ok in ("/vrc item 123", "vrc item https://booth.pm/ja/items/3368697",
                   "/VRC ITEM 456"):
            self.assertTrue(bs.ITEM_CMD_RE.match(ok), ok)
        for bad in ("/vrc itemx 1", "/vrcitem 1", "/vrc search x"):
            self.assertFalse(bs.ITEM_CMD_RE.match(bad), bad)
        for ok in ("/vrc for Rexouium", "vrc for 桔梗", "/VRC FOR Kikyo"):
            self.assertTrue(bs.FOR_RE.match(ok), ok)
        for bad in ("/vrc force", "/vrcfor x", "/vrc search x"):
            self.assertFalse(bs.FOR_RE.match(bad), bad)


if __name__ == "__main__":
    unittest.main()
