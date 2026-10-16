"""
test_memory_verdict.py - Test suite for memory.py and verdict.py in RingWatch.
"""

from memory import (
    save_case,
    record_feedback,
    recall_similar,
    reset_analyst,
)
from verdict import (
    verdict,
    VERDICT_FRAUD_RING,
    VERDICT_SUSPICIOUS,
    VERDICT_NO_LINKS_FOUND,
    NO_LINKS_REASON,
)
from tools import (
    order_facts,
    find_linked_accounts,
    detect_ring,
    ring_stats,
)
from falkordb import FalkorDB

db = FalkorDB(host="localhost", port=6379)
fraud_graph = db.select_graph("fraud")


def test_memory_isolation_and_feedback():
    print("=" * 70)
    print("Testing Analyst Memory: Isolation, Feedback, and Pattern Stats")
    print("=" * 70)

    analyst_1 = "a1"
    analyst_2 = "a2"

    # Clean up before testing
    reset_analyst(analyst_1)
    reset_analyst(analyst_2)

    try:
        # 1. Analyst a1 saves a case
        case_id = save_case(
            analyst_id=analyst_1,
            order_id="o560",
            ring_members=["u184", "u185", "u186"],
            shared_entities=["dA2"],
            verdict="FRAUD_RING",
            confidence=85.0,
            shape="shared_device_chain",
        )
        assert case_id and len(case_id) >= 6, f"Invalid case_id returned: {case_id}"
        print(f"  [PASS] Saved case {case_id} for analyst '{analyst_1}'")

        # 2. Analyst a1 confirms the case
        feedback_res = record_feedback(analyst_id=analyst_1, case_id=case_id, confirmed=True)
        assert feedback_res.get("status") == "success", f"Feedback failed: {feedback_res}"
        assert feedback_res.get("case_status") == "confirmed"
        assert feedback_res.get("pattern") == "shared_device_chain"
        print(f"  [PASS] Feedback recorded for {case_id}: status=confirmed, pattern=shared_device_chain")

        # 3. Analyst a1 recalls with overlapping entities (u186, dA2)
        recall_a1 = recall_similar(analyst_id=analyst_1, entity_ids=["u186", "dA2"])
        assert recall_a1["outcome"] == "answered_from_memory", f"Expected answered_from_memory, got {recall_a1}"
        assert len(recall_a1["cases"]) == 1, f"Expected 1 case, got {len(recall_a1['cases'])}"
        c1 = recall_a1["cases"][0]
        assert c1["case_id"] == case_id
        assert c1["status"] == "confirmed"
        assert c1["overlap_count"] == 2
        assert c1["pattern_stats"]["name"] == "shared_device_chain"
        assert c1["pattern_stats"]["confirmed_count"] == 1
        assert c1["pattern_stats"]["rejected_count"] == 0
        print(f"  [PASS] Analyst '{analyst_1}' recalled case {case_id} with overlap={c1['overlap_count']} and pattern stats")

        # 4. Analyst a2 recalls the exact same entities -> must return no_memory (isolation)
        recall_a2 = recall_similar(analyst_id=analyst_2, entity_ids=["u186", "dA2"])
        assert recall_a2["outcome"] == "no_memory", f"Expected no_memory for analyst 2, got {recall_a2}"
        assert len(recall_a2["cases"]) == 0
        print(f"  [PASS] Analyst '{analyst_2}' got no_memory (memory isolation verified)")

        # 5. Analyst a1 saves and rejects a second case with shared card
        case_id_2 = save_case(
            analyst_id=analyst_1,
            order_id="o999",
            ring_members=["u188", "u189"],
            shared_entities=["cB"],
            verdict="FRAUD_RING",
            confidence=80.0,
            shape="shared_card_ring",
        )
        fb2 = record_feedback(analyst_id=analyst_1, case_id=case_id_2, confirmed=False)
        assert fb2.get("status") == "success"
        assert fb2.get("case_status") == "rejected"
        assert fb2.get("pattern") == "shared_card_ring"
        print(f"  [PASS] Rejected feedback recorded for {case_id_2}: pattern=shared_card_ring")

        # Verify pattern counts
        recall_card = recall_similar(analyst_id=analyst_1, entity_ids=["cB"])
        assert recall_card["outcome"] == "answered_from_memory"
        c2 = recall_card["cases"][0]
        assert c2["pattern_stats"]["name"] == "shared_card_ring"
        assert c2["pattern_stats"]["confirmed_count"] == 0
        assert c2["pattern_stats"]["rejected_count"] == 1
        print(f"  [PASS] Pattern counts updated: shared_card_ring rejected_count=1")

        # 6. Unknown case_id returns an error object, not an exception
        err_res = record_feedback(analyst_id=analyst_1, case_id="unknown_xyz", confirmed=True)
        assert isinstance(err_res, dict), f"Expected error dict, got {err_res}"
        assert "error" in err_res, f"Expected 'error' in response, got {err_res}"
        assert err_res.get("status") == "error"
        print(f"  [PASS] Unknown case_id returned error object: {err_res['error']}")

    finally:
        # Clean up
        reset_analyst(analyst_1)
        reset_analyst(analyst_2)
        print("  [CLEANUP] Successfully reset and cleaned up analyst memory graphs.")


def test_verdict_engine():
    print("=" * 70)
    print("Testing Verdict Engine: Ring-A, Isolated User, and Memory Influence")
    print("=" * 70)

    # 1. Test Ring-A Order -> Expected: FRAUD_RING
    print("Testing a ring-A order...")
    # Find an order placed by ring-A user u186
    r = fraud_graph.ro_query("MATCH (u:User {id:'u186'})-[:PLACED]->(o:Order) RETURN o.id LIMIT 1")
    ring_a_order_id = r.result_set[0][0]

    # Gather ring evidence
    ring_users = detect_ring("u186")["data"]["members"]
    all_ring_users = ["u186"] + ring_users
    stats_a = ring_stats(all_ring_users)
    shared_kinds_a = stats_a["data"]["connecting_entity_kinds"]

    v_a = verdict(ring_stats=stats_a, shared_kinds=shared_kinds_a, recall_result=None)
    assert v_a["verdict"] == VERDICT_FRAUD_RING, f"Expected FRAUD_RING for Ring A, got {v_a['verdict']}"
    assert v_a["confidence"] >= 70, f"Expected confidence >= 70, got {v_a['confidence']}"
    assert len(v_a["reasons"]) > 0
    print(f"  [PASS] Ring-A order ({ring_a_order_id}) -> {v_a['verdict']} (confidence: {v_a['confidence']})")
    print(f"         Reasons: {v_a['reasons']}")

    # 2. Test Isolated normal user's order -> Expected: NO_LINKS_FOUND
    print("Testing an isolated normal user's order...")
    facts_o0 = order_facts("o0")
    user_o0 = facts_o0["data"]["user"]
    linked_o0 = find_linked_accounts("o0")
    assert linked_o0["outcome"] == "not_linked", "Expected o0 to have no linked accounts"

    stats_normal = ring_stats([user_o0])
    v_normal = verdict(ring_stats=stats_normal, shared_kinds=[], recall_result=None)
    assert v_normal["verdict"] == VERDICT_NO_LINKS_FOUND, f"Expected NO_LINKS_FOUND, got {v_normal['verdict']}"
    assert v_normal["confidence"] == 0
    assert any("does not mean the account is safe" in r.lower() for r in v_normal["reasons"]), (
        f"Expected reason to state it does not mean account is safe, got {v_normal['reasons']}"
    )
    print(f"  [PASS] Isolated order (o0 / {user_o0}) -> {v_normal['verdict']}")
    print(f"         Reason: {v_normal['reasons'][0]}")

    # 3. Test Memory Boost applies (confirmed past case boost +15)
    print("Testing memory boost from confirmed past case...")
    # Construct a baseline case: ring size 3 (+35), device shared (+10), avg amount (+10) -> score 55 (SUSPICIOUS)
    base_stats = {
        "user_ids": ["u1", "u2", "u3"],
        "order_count": 5,
        "flagged_count": 1,        # 20% < 30%
        "total_amount": 20000.0,   # avg 4000 >= 3000 (+10)
        "connecting_entity_kinds": ["Device"],
    }
    v_base = verdict(ring_stats=base_stats, shared_kinds=["Device"], recall_result=None)
    assert v_base["verdict"] == VERDICT_SUSPICIOUS
    assert v_base["confidence"] == 55, f"Expected base confidence 55, got {v_base['confidence']}"
    print(f"  Base case without memory: score={v_base['confidence']}, verdict={v_base['verdict']}")

    # Now add confirmed memory recall (+15 boost -> 70 -> FRAUD_RING)
    confirmed_recall = {
        "outcome": "answered_from_memory",
        "cases": [
            {
                "case_id": "c_boost_1",
                "status": "confirmed",
                "date": "2026-10-15T10:00:00Z",
                "overlap_count": 2,
            }
        ],
    }
    v_boosted = verdict(ring_stats=base_stats, shared_kinds=["Device"], recall_result=confirmed_recall)
    assert v_boosted["confidence"] == 70, f"Expected boosted confidence 70, got {v_boosted['confidence']}"
    assert v_boosted["verdict"] == VERDICT_FRAUD_RING, f"Expected FRAUD_RING after boost, got {v_boosted['verdict']}"
    assert any("c_boost_1" in r and "2026-10-15" in r for r in v_boosted["reasons"]), (
        f"Expected reason mentioning case id and date: {v_boosted['reasons']}"
    )
    print(f"  [PASS] Memory boost (+15) applied: score {v_base['confidence']} -> {v_boosted['confidence']} ({v_boosted['verdict']})")

    # 4. Test Rejected past case lowers the score (-25 penalty)
    print("Testing rejected past case lowers the score...")
    # Base case with score 85 (FRAUD_RING)
    fraud_stats = {
        "user_ids": ["u1", "u2", "u3", "u4", "u5", "u6"], # 35 + 10 = 45
        "order_count": 10,
        "flagged_count": 4,                                # 40% >= 30% -> +20
        "total_amount": 40000.0,                           # avg 4000 >= 3000 -> +10
        "connecting_entity_kinds": ["Device"],             # +10 => Total 85
    }
    v_fraud = verdict(ring_stats=fraud_stats, shared_kinds=["Device"], recall_result=None)
    assert v_fraud["confidence"] == 85
    assert v_fraud["verdict"] == VERDICT_FRAUD_RING

    # Now add rejected memory recall (-25 penalty -> 60 -> SUSPICIOUS)
    rejected_recall = {
        "outcome": "answered_from_memory",
        "cases": [
            {
                "case_id": "c_rej_99",
                "status": "rejected",
                "date": "2026-10-14T08:30:00Z",
                "overlap_count": 3,
            }
        ],
    }
    v_penalized = verdict(ring_stats=fraud_stats, shared_kinds=["Device"], recall_result=rejected_recall)
    assert v_penalized["confidence"] == 60, f"Expected penalized confidence 60, got {v_penalized['confidence']}"
    assert v_penalized["verdict"] == VERDICT_SUSPICIOUS, f"Expected SUSPICIOUS after rejection, got {v_penalized['verdict']}"
    assert any("c_rej_99" in r and "2026-10-14" in r for r in v_penalized["reasons"]), (
        f"Expected reason mentioning case id and date: {v_penalized['reasons']}"
    )
    print(f"  [PASS] Rejected case penalty (-25) applied: score {v_fraud['confidence']} -> {v_penalized['confidence']} ({v_penalized['verdict']})")


if __name__ == "__main__":
    test_memory_isolation_and_feedback()
    test_verdict_engine()
    print("=" * 70)
    print("ALL MEMORY AND VERDICT TESTS PASSED SUCCESSFULLY!")
    print("=" * 70)
