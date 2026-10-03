"""与真实 booth-cli 的离线契约检查（BOOTH_CLI_TEST_PATH 必须显式指定）。"""
import importlib
import os
import tempfile
import sys
import unittest
from pathlib import Path
from unittest import mock

import nonebot

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))
nonebot.init(log_level="ERROR")
from booth_search import booth_client as bc, execution

CLI = Path(os.environ["BOOTH_CLI_TEST_PATH"]).resolve()
sys.path.insert(0, str(CLI.parent))
parent = importlib.import_module("booth")


class TestParentContract(unittest.TestCase):
    def test_real_subprocess_version_envelope(self):
        data = bc.call_booth("version", cli_path=str(CLI))
        self.assertEqual(data["version"], parent.__version__)
        self.assertGreaterEqual(tuple(map(int, data["version"].split("."))), (1, 6, 0))
        self.assertIn("shared_request_budget", data["capabilities"])
        self.assertIn("verified_image_input", data["capabilities"])
        self.assertIn("caller_workflow", data["capabilities"])
        self.assertEqual(data["ai_execution"]["default"], "caller")
        self.assertEqual(len(data["semantic_fingerprint"]), 64)

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

    def test_real_subprocess_query_context_and_cached_wire_count(self):
        with tempfile.TemporaryDirectory() as directory:
            shim = Path(directory) / "cached_cli.py"
            shim.write_text("import sys\nsys.path.insert(0, " + repr(str(CLI.parent)) + ")\n"
                            "import booth\nbooth.cache_get=lambda url,ttl:(b'{\\\"id\\\":1,\\\"name\\\":\\\"cached\\\"}',url)\n"
                            "booth.main()\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"BOOTH_REQUEST_BUDGET_DB": str(Path(directory) / "budget.db")}), \
                    execution.query_scope() as context:
                data = bc.item(1, cli_path=str(shim))
                self.assertEqual(data["name"], "cached")
                self.assertEqual(context["wire_count"], 0)
            self.assertFalse((Path(directory) / "budget.db").exists())

    def test_source_evidence_contract_matches_parent(self):
        self.assertEqual((CLI.parent / "search_evidence.py").read_text(encoding="utf-8"),
                         (ROOT / "src/plugins/booth_search/search_evidence.py").read_text(encoding="utf-8"))

    def test_real_subprocess_workflow_schema_and_flags(self):
        data = bc.call_booth("workflow", {"schema": True}, cli_path=str(CLI))
        self.assertEqual(data["ai_execution"], "caller")
        self.assertEqual(data["result_contract"]["ai_calls"], 0)
        with mock.patch.object(bc, "call_booth", return_value={}) as call:
            bc.workflow("桔梗用衣装", keywords=["桔梗 衣装"], require_terms=["桔梗"], desc_len=-1)
        action, params = call.call_args.args
        args = parent.build_parser().parse_args(parent.bot_params_to_argv(action, params))
        self.assertEqual(args.keyword, ["桔梗 衣装"])
        self.assertEqual(args.require_term, ["桔梗"])
        self.assertEqual(args.desc_len, -1)

    def test_image_delegation_is_explicit_through_parent_parser(self):
        for opt_in in (False, True):
            with mock.patch.object(bc, "call_booth", return_value={}) as call:
                bc.imgsearch("fixture.jpg", delegate_ai=opt_in)
            action, params = call.call_args.args
            args = parent.build_parser().parse_args(parent.bot_params_to_argv(action, params))
            self.assertIs(args.delegate_ai, opt_in)

    def test_provider_contract_matches_parent(self):
        self.assertEqual((CLI.parent / "provider_api.py").read_text(encoding="utf-8"),
                         (ROOT / "src/plugins/booth_search/provider_api.py").read_text(encoding="utf-8"))

    def test_search_adapter_contract_matches_parent(self):
        self.assertEqual((CLI.parent / "search_api.py").read_text(encoding="utf-8"),
                         (ROOT / "src/plugins/booth_search/search_api.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
