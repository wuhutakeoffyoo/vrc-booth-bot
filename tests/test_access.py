# -*- coding: utf-8 -*-
"""访问控制单测（需 nonebot 运行时，用 booth-bot venv 跑）。"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "plugins"))

import nonebot  # noqa: E402

nonebot.init()

# 直接构造被测配置对象（不经过插件元数据系统）
from booth_search.config import Config  # noqa: E402
from booth_search import access  # noqa: E402


def make_cfg(**kw):
    base = dict(
        group_whitelist=["100000001", "200000002"],
        admin_users=["300000003", 400000004],  # 故意混用 int/str 验证规范化
        allow_private=True,
    )
    base.update(kw)
    return Config(**base)


class TestAccessControl(unittest.TestCase):
    def test_admin_normalization_int_and_str(self):
        cfg = make_cfg()
        self.assertTrue(access.is_admin(cfg, 300000003))
        self.assertTrue(access.is_admin(cfg, "400000004"))
        self.assertFalse(access.is_admin(cfg, 999))

    def test_group_whitelist(self):
        cfg = make_cfg()
        self.assertTrue(access.access_ok(cfg, user_id=100, group_id=100000001))
        self.assertTrue(access.access_ok(cfg, user_id=100, group_id="200000002"))
        self.assertFalse(access.access_ok(cfg, user_id=100, group_id=999))
        self.assertFalse(access.access_ok(cfg, user_id=300000003, group_id=999))  # 管理员也受群限制

    def test_private_policy(self):
        cfg = make_cfg()
        self.assertTrue(access.access_ok(cfg, user_id=300000003, group_id=None))
        self.assertTrue(access.access_ok(cfg, user_id=100, group_id=None))  # allow_private
        cfg2 = make_cfg(allow_private=False)
        self.assertFalse(access.access_ok(cfg2, user_id=100, group_id=None))
        self.assertTrue(access.access_ok(cfg2, user_id=300000003, group_id=None))

    def test_sensitive_admin_only(self):
        cfg = make_cfg()
        self.assertTrue(access.sensitive_allowed(cfg, 400000004))
        self.assertFalse(access.sensitive_allowed(cfg, 100))

    def test_empty_whitelist_allows_all_groups(self):
        cfg = make_cfg(group_whitelist=[])
        self.assertTrue(access.access_ok(cfg, user_id=100, group_id=12345))


class TestSplitPage(unittest.TestCase):
    """页码剥离（导入插件包需 nonebot 已 init，故放在本文件）。"""

    def _bs(self):
        import booth_search as bs  # noqa: E402  # 插件包导入会注册 matcher
        return bs

    @classmethod
    def setUpClass(cls):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "plugins"))

    def test_split_page(self):
        bs = self._bs()
        self.assertEqual(bs._split_page("猫娘女仆装 2"), ("猫娘女仆装", 2))
        self.assertEqual(bs._split_page("シエル 12"), ("シエル", 12))
        self.assertEqual(bs._split_page("猫娘女仆装"), ("猫娘女仆装", 1))
        self.assertEqual(bs._split_page("2"), ("2", 1))          # 纯数字=关键词本身
        self.assertEqual(bs._split_page("Bloom Phone"), ("Bloom Phone", 1))


if __name__ == "__main__":
    unittest.main()
