# -*- coding: utf-8 -*-
"""booth_search 相关度重排单测（纯逻辑，不联网）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "plugins" / "booth_search"))

import rank  # noqa: E402


class TestDescHit(unittest.TestCase):
    def test_hit_in_desc(self):
        it = {"name": "チョコレートドレス", "_desc": "対応素体：Rexouium、 layoutParams"}
        self.assertTrue(rank.desc_hit(it, ["Rexouium"]))

    def test_hit_in_name(self):
        self.assertTrue(rank.desc_hit({"name": "Rexouium outfit", "_desc": ""}, ["rexouium"]))

    def test_miss(self):
        it = {"name": "ハートヘア", "_desc": "ユニティ対応"}
        self.assertFalse(rank.desc_hit(it, ["Rexouium"]))

    def test_empty_desc_kws(self):
        self.assertFalse(rank.desc_hit({"name": "x", "_desc": "y"}, []))


class TestDescBoost(unittest.TestCase):
    def test_three_tier_stable(self):
        items = [
            {"id": 1, "name": "衣装 A"},
            {"id": 2, "name": "B 対応衣装", "_desc": "対応素体: Rexouium"},
            {"id": 3, "name": "Rexouium 衣装"},
            {"id": 4, "name": "无关 C"},
        ]
        out = rank.desc_boost(items, ["衣装", "Rexouium"], ["Rexouium"])
        self.assertEqual([it["id"] for it in out], [2, 3, 1, 4])

    def test_no_desc_kws_falls_back_to_title(self):
        items = [{"id": 1, "name": "无关"}, {"id": 2, "name": "猫耳 衣装"}]
        out = rank.desc_boost(items, ["猫耳"], [])
        self.assertEqual([it["id"] for it in out], [2, 1])


if __name__ == "__main__":
    unittest.main()
