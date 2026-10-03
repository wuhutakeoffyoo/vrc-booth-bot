"""Real bot text flow, with all external dependencies mocked."""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/plugins"))
import nonebot
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init(log_level="ERROR")
import booth_search as bs
from booth_search import execution
from booth_search.config import Config


class TestBotTargetFlow(unittest.IsolatedAsyncioTestCase):
    async def run_search(self, evaluate):
        cfg = Config(vision_api_key="placeholder", vision_base_url="https://ai.invalid/v1", vrc_tag="")
        original = dict(id=1, name="みかんバード", price=0, shop={}, tags=[], is_adult=False,
                        url="https://booth.pm/items/1", detail_status="available", _desc="original source")
        noise = dict(original, id=2, name="AvatarPoseSystem", url="https://booth.pm/items/2")
        with mock.patch.object(bs, "plugin_config", cfg), \
                mock.patch.object(bs, "_cached_plan", new=mock.AsyncMock(return_value=(["VRChat", "アバター"], [], False))), \
                mock.patch.object(bs, "_search_merged", new=mock.AsyncMock(side_effect=[([noise, original], {"total": 2}),
                                                                                     ([noise], {"total": 1})])) as search, \
                mock.patch.object(bs, "_enrich_entries", new=mock.AsyncMock()), \
                mock.patch.object(bs, "_ai_evaluate", new=mock.AsyncMock(side_effect=evaluate)), \
                execution.query_scope():
            value = await bs._handle_text("みかんバード")
        return value, search

    async def test_original_axis_and_candidate_survive_second_round(self):
        value, search = await self.run_search([{"verdict": "retry", "keywords": ["VRChat", "オレンジ鳥"]},
                                              {"verdict": "retry", "keywords": []}])
        self.assertEqual(search.call_args_list[0].args[0], ["みかんバード"])
        self.assertNotIn("VRChat", search.call_args_list[1].args[0])
        self.assertEqual([it["id"] for it in value["entries"]], [1])
        self.assertIn("显示前 1", value["header"])

    async def test_evaluator_outage_keeps_exact_target_with_unverified_notice(self):
        value, search = await self.run_search(RuntimeError("offline evaluator outage"))
        self.assertEqual(search.await_count, 1)
        self.assertEqual([it["id"] for it in value["entries"]], [1])
        self.assertEqual(value["entries"][0]["relevance_status"], "unknown")
        self.assertIn("尚未核实", value["header"])


if __name__ == "__main__":
    unittest.main()
