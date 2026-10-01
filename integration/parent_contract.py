"""与真实 booth-cli 的离线契约检查（BOOTH_CLI_TEST_PATH 必须显式指定）。"""
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import nonebot

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))
nonebot.init(log_level="ERROR")
from booth_search import booth_client as bc

CLI = Path(os.environ["BOOTH_CLI_TEST_PATH"]).resolve()
sys.path.insert(0, str(CLI.parent))
parent = importlib.import_module("booth")


class TestParentContract(unittest.TestCase):
    def test_real_subprocess_version_envelope(self):
        data = bc.call_booth("version", cli_path=str(CLI))
        self.assertEqual(data["version"], parent.__version__)
        self.assertGreaterEqual(tuple(map(int, data["version"].split("."))), (1, 3, 3))

    def test_bot_search_flags_reach_parent_parser(self):
        for tag, adult in ((None, "exclude"), ("VRChat", "only")):
            with self.subTest(tag=tag, adult=adult):
                with mock.patch.object(bc, "call_booth", return_value={}) as call:
                    bc.search("衣装", tag=tag, adult=adult, page=3)
                action, params = call.call_args.args
                args = parent.build_parser().parse_args(parent.bot_params_to_argv(action, params))
                url = parent.build_search_url(args)
                self.assertEqual(args.page, 3)
                self.assertEqual(args.adult, adult)
                self.assertFalse(args.vrc)
                self.assertEqual("tags%5B%5D=VRChat" in url, tag == "VRChat")


if __name__ == "__main__":
    unittest.main()
