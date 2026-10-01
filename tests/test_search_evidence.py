"""Offline source-evidence and compound-query regression contract."""
import json
import unittest

try:
    from booth_search import search_evidence as evidence, vision as terminology
except ImportError:
    import search_evidence as evidence
    import smart_search as terminology


def candidates(description="Rexouium 対応"):
    return [dict(id=i, name=f"衣装 {i}", category="3D衣装", tags=["VRChat"],
                 _desc=description, detail_status="available") for i in (1, 2, 3)]


def decision(items, field="description", quote="Rexouium 対応"):
    return {"verdict": "ok", "hits": ["1", "2", "3"], "evidence": [
        {"item_id": str(it["id"]), "field": field, "quote": quote, "status": "related"}
        for it in items]}


class TestEvidence(unittest.TestCase):
    def test_full_title_and_tail_excerpt_reach_model(self):
        items = candidates("導入ガイド " * 1000 + "Rexouium 対応")
        items[0]["name"] = "名前" * 100 + "鈴"
        payload = json.loads(evidence.candidate_lines(items, ["Rexouium"])[0].split(". ", 1)[1])
        self.assertEqual(payload["name"], items[0]["name"])
        self.assertIn("Rexouium 対応", payload["description"])
        self.assertLessEqual(len(payload["description"]), 900)
        self.assertEqual(payload["item_id"], "1")
        self.assertEqual(len(payload["source_hash"]), 64)

    def test_real_description_evidence_passes(self):
        items = candidates()
        result = evidence.grounded_evaluation(decision(items), items, ["Rexouium"])
        self.assertEqual(result["verdict"], "ok")
        self.assertEqual(result["hits"], ["1", "2", "3"])
        self.assertTrue(all(it["relevance_status"] == "related" for it in items))

    def test_invented_quote_unknown_id_and_title_are_rejected(self):
        for alter in ("quote", "id", "title", "missing"):
            items = candidates()
            value = decision(items)
            if alter == "quote":
                for claim in value["evidence"]:
                    claim["quote"] = "Rexouium verified fully compatible"
            elif alter == "id":
                for claim in value["evidence"]:
                    claim["item_id"] = "999"
            elif alter == "title":
                for it, claim in zip(items, value["evidence"]):
                    it["name"] = "Rexouium 対応"
                    claim["field"] = "name"
            else:
                for it in items:
                    it["detail_status"] = "unavailable"
            self.assertEqual(evidence.grounded_evaluation(value, items, ["Rexouium"])["hits"], [])

    def test_unfetched_details_cannot_supply_description_evidence(self):
        items = candidates()
        for item in items:
            item.pop("detail_status")
        self.assertEqual(evidence.grounded_evaluation(decision(items), items, ["Rexouium"])["verdict"], "retry")

    def test_full_source_negative_wins_over_positive_excerpt(self):
        items = candidates("Rexouium 対応" + "。" + "導入 " * 2000 + "Rexouiumには対応しておりません。")
        result = evidence.grounded_evaluation(decision(items), items, ["Rexouium"])
        self.assertEqual(result["hits"], [])
        self.assertTrue(all(it["relevance_status"] == "unsupported" for it in items))

    def test_conflicting_claims_never_restore_blocked_hit(self):
        for reverse in (False, True):
            items = candidates()
            value = decision(items)
            negative = dict(value["evidence"][0], status="unsupported")
            value["evidence"] = ([negative] + value["evidence"] if reverse
                                 else value["evidence"] + [negative])
            result = evidence.grounded_evaluation(value, items, ["Rexouium"])
            self.assertNotIn("1", result["hits"])
            self.assertEqual(items[0]["relevance_status"], "unsupported")

    def test_fabricated_unsupported_quote_cannot_block(self):
        items = candidates()
        value = decision(items)
        value["evidence"].append(dict(value["evidence"][0], status="unsupported", quote="fabricated"))
        self.assertEqual(evidence.grounded_evaluation(value, items, ["Rexouium"])["verdict"], "ok")

    def test_three_hits_do_not_mark_other_items_related(self):
        items = candidates() + [dict(id=4, name="雑貨")]
        result = evidence.grounded_evaluation(decision(items[:3]), items, ["Rexouium"])
        self.assertEqual(result["verdict"], "ok")
        self.assertEqual(items[3]["relevance_status"], "unknown")
        self.assertIn("未核实", evidence.evidence_label(items[3]))
        self.assertNotIn("兼容", evidence.evidence_label(items[0]))

    def test_evidence_retained_by_active_evaluation_parser(self):
        value = decision(candidates())
        parsed = terminology.parse_evaluation(json.dumps(value))
        self.assertEqual(parsed["evidence"], value["evidence"])

    def test_compound_positive_query_keeps_seed_priority(self):
        words = terminology.apply_industry_synonyms("想找带铃铛的红色项链", ["衣装"])
        self.assertEqual(words[:3], ["鈴", "鈴チョーカー", "鈴付き"])
        self.assertIn("ネックレス", words)

    def test_negative_clause_does_not_inject_unwanted_item(self):
        for query in ("不要墨镜，想要铃铛", "想要铃铛但是不要墨镜"):
            words = terminology.apply_industry_synonyms(query, ["サングラス", "墨镜", "衣装"])
            self.assertEqual(words[:3], ["鈴", "鈴チョーカー", "鈴付き"])
            self.assertNotIn("サングラス", words)
            self.assertNotIn("墨镜", words)

    def test_unknown_phrase_and_dictionary_remain_stable(self):
        words = ["Custom", "衣装"]
        self.assertEqual(terminology.apply_industry_synonyms("自定义", words), words)
        self.assertEqual(len(terminology.INDUSTRY_SYNONYMS), 20)

    def test_promote_related_keep_unknown_and_mark_unsupported(self):
        items = [dict(id=1, relevance_status="unknown"),
                 dict(id=2, relevance_status="unsupported"),
                 dict(id=3, relevance_status="related")]
        self.assertEqual([it["id"] for it in evidence.promote_evidence(items)], [3, 1, 2])
        self.assertIn("不满足要求", evidence.evidence_label(items[1]))



    def test_forced_glm_thinking_has_low_effort_json_and_headroom(self):
        opts = evidence.structured_options("glm-5.3-flash", evaluation=True)
        self.assertEqual(opts["thinking"]["type"], "enabled")
        self.assertEqual(opts["reasoning_effort"], "low")
        self.assertEqual(opts["response_format"]["type"], "json_object")
        self.assertEqual(opts["max_tokens"], 4096)

    def test_older_glm_and_unknown_models_keep_valid_options(self):
        self.assertEqual(evidence.structured_options("glm-5")["thinking"]["type"], "disabled")
        self.assertEqual(evidence.structured_options("custom-provider-model"), {})
        self.assertEqual(evidence.structured_options("glm-4.5v"), {})


if __name__ == "__main__":
    unittest.main()
