"""Regression cases for original-target drift and unsupported result padding."""
import unittest

try:
    from booth_search import search_evidence as evidence, vision as terminology
except ImportError:
    import search_evidence as evidence
    import smart_search as terminology


class TestTargetSelection(unittest.TestCase):
    def test_original_target_survives_generic_plan_without_more_slots(self):
        for query in ("猫毛ウルフ", "みかんバード", "Moon Knot Hair"):
            terms = evidence.retrieval_terms(query, ["VRChat", "アバター", "3Dモデル", "獣耳"])
            self.assertEqual(terms, [query, "獣耳"])
        self.assertEqual(len(evidence.retrieval_terms("目標", list("abcdefg"))), 6)

    def test_retry_rejects_generic_drift_and_already_fetched_terms(self):
        terms = evidence.retrieval_terms("Moon Knot Hair", ["ＭＯＯＮ　ＫＮＯＴ　ＨＡＩＲ", "VRChat", "ウィッグ"],
                                         previous=["Moon Knot Hair"], limit=3)
        self.assertEqual(terms, ["ウィッグ"])

    def test_broad_user_query_is_still_a_valid_search(self):
        self.assertEqual(evidence.retrieval_terms("アバター", ["アバター"]), ["アバター"])
        self.assertEqual(evidence.retrieval_terms("みかんバード", ["VRChat アバター", "衣装"]),
                         ["みかんバード"])

    def test_negated_and_compatibility_prose_is_not_a_positive_literal(self):
        for query in ("不要猫耳，想要尾巴", "without cat ears", "适用于Rexouium的服装"):
            self.assertEqual(evidence.literal_query(query), "")
        self.assertEqual(evidence.retrieval_terms("铃铛", ["鈴", "ベル"], terminology.INDUSTRY_SYNONYMS),
                         ["鈴", "ベル"])

    def test_literal_match_normalizes_width_but_not_ascii_substrings(self):
        self.assertTrue(evidence.literal_match({"name": "Ｍｏｏｎ　Ｋｎｏｔ　Ｈａｉｒ"}, "Moon Knot Hair"))
        self.assertFalse(evidence.literal_match({"name": "Bella Hair"}, "Bell"))
        self.assertTrue(evidence.literal_match({"name": "Kitten Bell Choker"}, "Bell"))

    def test_complete_identity_allows_metadata_but_not_theme_or_substring(self):
        for name in ("Target", "【VRChat】Target [model edition]", "Ｔａｒｇｅｔ（v2）"):
            self.assertTrue(evidence.identity_match({"name": name}, "Target"))
        for name in ("Target earrings", "Target themed clothes", "Targeted", "【Target】Dress"):
            self.assertFalse(evidence.identity_match({"name": name}, "Target"))

    def test_exact_positive_name_is_not_a_negative_evidence_source(self):
        item = {"id": 1, "name": "Target【model edition】"}
        value = {"verdict": "retry", "hits": [], "evidence": [{"item_id": "1", "field": "name",
                 "quote": item["name"], "status": "unsupported", "relation": "thematic"}]}
        parsed = evidence.grounded_evaluation(value, [item], target_query="Target")
        self.assertEqual(parsed["hits"], [])
        self.assertEqual(item["relevance_status"], "unknown")
        selected, quality = evidence.select_results([item, {"id": 2, "name": "noise"}],
                                                   "Target", assessed=True)
        self.assertEqual([it["id"] for it in selected], [1])
        self.assertEqual(quality["unverified"], 1)

    def test_name_only_reversal_preserves_prior_grounded_evidence(self):
        original = {"field": "name", "quote": "Target"}
        item = {"id": 1, "name": "Target", "relevance_status": "related",
                "relevance_evidence": original.copy()}
        value = {"verdict": "retry", "evidence": [{"item_id": "1", "field": "name",
                 "quote": "Target", "status": "unsupported"}]}
        evidence.grounded_evaluation(value, [item], target_query="Target")
        self.assertEqual(item["relevance_status"], "related")
        self.assertEqual(item["relevance_evidence"], original)

    def test_category_conflict_is_not_overridden_by_complete_name(self):
        item = {"id": 1, "name": "Target", "category": "2D素材"}
        value = {"verdict": "retry", "evidence": [{"item_id": "1", "field": "category",
                 "quote": "2D素材", "status": "unsupported"}]}
        evidence.grounded_evaluation(value, [item], target_query="Target")
        self.assertEqual(item["relevance_status"], "unsupported")

    def test_explicit_negative_name_is_not_protected(self):
        item = {"id": 1, "name": "Target【非対応】"}
        value = {"verdict": "retry", "evidence": [{"item_id": "1", "field": "name",
                 "quote": item["name"], "status": "unsupported"}]}
        evidence.grounded_evaluation(value, [item], target_query="Target")
        self.assertEqual(item["relevance_status"], "unsupported")

    def test_compatibility_requirement_still_cannot_be_overridden_by_name(self):
        item = {"id": 1, "name": "Target"}
        value = {"verdict": "retry", "evidence": [{"item_id": "1", "field": "name",
                 "quote": "Target", "status": "unsupported"}]}
        evidence.grounded_evaluation(value, [item], ["model"], target_query="Target")
        self.assertEqual(item["relevance_status"], "unsupported")

    def test_second_round_noise_cannot_replace_exact_first_round_target(self):
        original = {"id": 1, "name": "みかんバード", "_desc": "full source", "detail_status": "available"}
        second = [{"id": 2, "name": "AvatarPoseSystem"}, {"id": 1, "name": "みかんバード"}]
        merged = evidence.merge_rounds([original], second, "みかんバード")
        self.assertEqual([item["id"] for item in merged], [1, 2])
        self.assertEqual(merged[0]["_desc"], "full source")

    def test_positive_evidence_is_preserved_across_rounds(self):
        original = {"id": 1, "name": "genuine synonym", "relevance_status": "related"}
        merged = evidence.merge_rounds([original], [{"id": 2, "name": "noise"}], "专有目标")
        self.assertEqual(merged[0]["id"], 1)

    def test_exact_target_does_not_get_unknown_padding(self):
        items = [{"id": 1, "name": "みかんバード"}] + [{"id": i, "name": "noise"} for i in range(2, 7)]
        selected, quality = evidence.select_results(items, "みかんバード", assessed=True)
        self.assertEqual([it["id"] for it in selected], [1])
        self.assertEqual(quality, {"related": 0, "unverified": 1, "omitted": 5})
        self.assertNotIn("relevance_status", selected[0])

    def test_related_candidates_are_not_padded_with_unknown_or_unsupported(self):
        items = [{"id": 1, "name": "known", "relevance_status": "related"},
                 {"id": 2, "name": "unknown"},
                 {"id": 3, "name": "blocked", "relevance_status": "unsupported"}]
        selected, quality = evidence.select_results(items, "a long requested target", assessed=True)
        self.assertEqual([it["id"] for it in selected], [1])
        self.assertEqual(quality["omitted"], 2)

    def test_compatibility_cannot_be_proven_by_literal_title(self):
        items = [{"id": 1, "name": "Rexouium 衣装"},
                 {"id": 2, "name": "服", "relevance_status": "related"}]
        selected, _ = evidence.select_results(items, "Rexouium 衣装", required_terms=["Rexouium"], assessed=True)
        self.assertEqual([it["id"] for it in selected], [2])

    def test_evaluator_outage_keeps_bounded_unverified_suggestions(self):
        items = [{"id": i, "name": "candidate"} for i in range(6)]
        selected, quality = evidence.select_results(items, "未知目标", assessed=True)
        self.assertEqual(len(selected), 3)
        self.assertEqual(quality["related"], 0)
        direct, _ = evidence.select_results(items, "未知目标")
        self.assertEqual(len(direct), 6)

    def test_unsupported_literal_is_never_restored(self):
        item = {"id": 1, "name": "Exact", "relevance_status": "unsupported"}
        selected, _ = evidence.select_results([item], "Exact", assessed=True)
        self.assertEqual(selected, [])

    def test_verified_synonym_precedes_unverified_literal_title(self):
        items = [{"id": 1, "name": "ローラースケートウェア"},
                 {"id": 2, "name": "roller skates", "relevance_status": "related"}]
        selected, _ = evidence.select_results(items, "ローラースケート", assessed=True)
        self.assertEqual([it["id"] for it in selected], [2, 1])

    def test_quality_counts_displayed_results_not_the_retained_pool(self):
        items = [{"id": i, "name": "Exact", "relevance_status": "related"} for i in range(10)]
        selected, quality = evidence.select_results(items, "Exact", assessed=True, display_limit=6)
        self.assertEqual(len(selected), 6)
        self.assertEqual(quality, {"related": 6, "unverified": 0, "omitted": 0})

    def test_themed_literal_with_real_quote_cannot_be_restored_as_exact(self):
        item = {"id": 1, "name": "Target patterned earrings"}
        value = {"verdict": "ok", "hits": ["1"], "evidence": [{"item_id": "1", "field": "name",
                 "quote": item["name"], "status": "related", "relation": "thematic"}]}
        parsed = evidence.grounded_evaluation(value, [item], target_query="Target")
        self.assertEqual(parsed["hits"], [])
        selected, _ = evidence.select_results([item], "Target", assessed=True)
        self.assertEqual(selected, [])

    def test_invented_theme_quote_cannot_block_a_genuine_candidate(self):
        item = {"id": 1, "name": "Target"}
        value = {"verdict": "retry", "hits": [], "evidence": [{"item_id": "1", "field": "name",
                 "quote": "fabricated", "status": "related", "relation": "thematic"}]}
        evidence.grounded_evaluation(value, [item])
        self.assertEqual(item["relevance_status"], "unknown")
        selected, _ = evidence.select_results([item], "Target", assessed=True)
        self.assertEqual(len(selected), 1)


if __name__ == "__main__":
    unittest.main()
