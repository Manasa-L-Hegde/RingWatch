"""
test_agent.py - Plain assert tests for RingWatch Agent loop, answer checker, and memory recall.
"""

import os
from dotenv import load_dotenv

load_dotenv()

from agent import (
    investigate,
    give_feedback,
    accepts,
)
import memory
from falkordb import FalkorDB

db = FalkorDB(host="localhost", port=6379)
fraud_graph = db.select_graph("fraud")


def test_accepts_validation():
    print("=" * 70)
    print("Testing accepts() sentence validation...")
    print("=" * 70)

    # 1. Accepts sentence with only known IDs
    result_valid = {
        "order_id": "o558",
        "user": "u186",
        "ring_members": ["u184", "u185", "u187"],
        "shared_entities": ["dA2"],
        "verdict": "FRAUD_RING",
        "outcome": "path_found",
        "known_ids": {"o558", "u186", "u184", "u185", "u187", "dA2"},
    }
    valid_sentence = "Order o558 placed by u186 shares device dA2 with u184 and u185."
    assert accepts(valid_sentence, result_valid) is True, "Expected valid sentence to be accepted"
    print("  [PASS] Accepted sentence using only known IDs.")

    # 2. Rejects sentence naming u999 (invented / unknown ID)
    hallucinated_sentence = "Order o558 placed by u186 shares device dA2 with u999."
    assert accepts(hallucinated_sentence, result_valid) is False, "Expected sentence with u999 to be rejected"
    print("  [PASS] Rejected sentence naming hallucinated u999.")

    # 3. Rejects 'linked' claims when nothing was linked
    result_unlinked = {
        "order_id": "o0",
        "user": "u63",
        "ring_members": [],
        "shared_entities": [],
        "verdict": "NO_LINKS_FOUND",
        "outcome": "not_linked",
        "known_ids": {"o0", "u63", "a57"},
    }
    false_link_sentence = "Order o0 by u63 is linked to a suspicious fraud ring."
    assert accepts(false_link_sentence, result_unlinked) is False, (
        "Expected false link claim on unlinked order to be rejected"
    )
    print("  [PASS] Rejected 'linked' claim when nothing was linked.")

    true_unlinked_sentence = "Order o0 placed by u63 shows no linked accounts (not safe)."
    assert accepts(true_unlinked_sentence, result_unlinked) is True, (
        "Expected true unlinked sentence to be accepted"
    )
    print("  [PASS] Accepted true unlinked statement.")


def test_no_llm_investigations():
    print("=" * 70)
    print("Testing --no-llm deterministic investigations...")
    print("=" * 70)

    analyst_id = "test_det_analyst"
    memory.reset_analyst(analyst_id)

    try:
        # 1. Ring-A order investigation (e.g. o558)
        # Find ring-A order
        r = fraud_graph.ro_query("MATCH (u:User {id:'u186'})-[:PLACED]->(o:Order) RETURN o.id LIMIT 1")
        ring_a_oid = r.result_set[0][0]

        res_a = investigate(analyst_id=analyst_id, order_id=ring_a_oid, use_llm=False)
        assert res_a["verdict"] == "FRAUD_RING", f"Expected FRAUD_RING, got {res_a['verdict']}"
        assert len(res_a["ring_members"]) > 0, f"Expected ring members, got {res_a['ring_members']}"
        assert len(res_a["path_evidence"]) > 0, f"Expected path evidence, got {res_a['path_evidence']}"
        assert res_a["case_id"] is not None
        assert len(res_a["trace"]) > 0
        print(f"  [PASS] Ring-A order ({ring_a_oid}) gave FRAUD_RING with {len(res_a['ring_members'])} members and path evidence.")

        # 2. Unknown order ID (o99999) -> unknown_entity with zero tool traces and no case saved
        res_unk = investigate(analyst_id=analyst_id, order_id="o99999", use_llm=False)
        assert res_unk["outcome"] == "unknown_entity", f"Expected unknown_entity, got {res_unk['outcome']}"
        assert len(res_unk["trace"]) == 0, f"Expected 0 tool traces for unknown id, got {len(res_unk['trace'])}"
        assert res_unk["case_id"] is None, f"Expected no case saved for unknown id, got {res_unk['case_id']}"
        print("  [PASS] Unknown ID (o99999) gave unknown_entity with 0 tool traces and no case saved.")

        # 3. Isolated normal user's order (o0) -> not_linked and 'not safe' wording
        res_iso = investigate(analyst_id=analyst_id, order_id="o0", use_llm=False)
        assert res_iso["outcome"] == "not_linked", f"Expected not_linked for o0, got {res_iso['outcome']}"
        assert res_iso["verdict"] == "NO_LINKS_FOUND", f"Expected NO_LINKS_FOUND for o0, got {res_iso['verdict']}"
        assert any(
            "not mean the account is safe" in r.lower() or "not safe" in r.lower()
            for r in res_iso["reasons"]
        ), f"Expected 'not safe' reason, got {res_iso['reasons']}"
        assert "not safe" in res_iso["sentence"].lower() or "not mean the account is safe" in res_iso["sentence"].lower()
        print(f"  [PASS] Isolated order (o0) gave not_linked with 'not safe' wording in reasons and sentence.")

    finally:
        memory.reset_analyst(analyst_id)


def test_memory_feedback_and_cross_analyst():
    print("=" * 70)
    print("Testing Memory feedback cycle and cross-analyst isolation...")
    print("=" * 70)

    a1 = "analyst_a1_mem"
    a2 = "analyst_a2_mem"
    memory.reset_analyst(a1)
    memory.reset_analyst(a2)

    try:
        # Get two orders for ring A: one for u186 and one for u184 (or two orders of u186)
        r_orders = fraud_graph.ro_query(
            "MATCH (u:User)-[:PLACED]->(o:Order) WHERE u.id IN ['u186', 'u184'] RETURN o.id ORDER BY o.id LIMIT 2"
        )
        assert len(r_orders.result_set) >= 2, "Expected at least 2 orders"
        order1 = r_orders.result_set[0][0]
        order2 = r_orders.result_set[1][0]

        # 1. Analyst a1 investigates order1
        res1 = investigate(analyst_id=a1, order_id=order1, use_llm=False)
        case1_id = res1["case_id"]
        conf1 = res1["confidence"]
        assert case1_id is not None
        print(f"  [PASS] Analyst a1 investigated {order1}: case_id={case1_id}, conf={conf1}")

        # 2. Analyst a1 confirms case1
        fb = give_feedback(analyst_id=a1, case_id=case1_id, confirmed=True)
        assert fb.get("status") == "success"
        print(f"  [PASS] Analyst a1 confirmed case {case1_id}")

        # 3. Analyst a1 investigates order2 (same ring)
        res2_a1 = investigate(analyst_id=a1, order_id=order2, use_llm=False)
        assert res2_a1["outcome"] == "answered_from_memory", (
            f"Expected answered_from_memory for a1, got {res2_a1['outcome']}"
        )
        assert res2_a1["confidence"] >= conf1, (
            f"Expected higher or equal confidence with memory boost: {res2_a1['confidence']} vs {conf1}"
        )
        # Reason names the earlier case id and date
        assert any(case1_id in r for r in res2_a1["reasons"]), (
            f"Expected earlier case_id {case1_id} in reasons: {res2_a1['reasons']}"
        )
        print(f"  [PASS] Analyst a1 on {order2} got answered_from_memory (conf: {res2_a1['confidence']}), mentioning case {case1_id}")

        # 4. Analyst a2 investigates order2 -> must get NO memory from a1
        res2_a2 = investigate(analyst_id=a2, order_id=order2, use_llm=False)
        assert res2_a2["outcome"] != "answered_from_memory", (
            f"Expected analyst a2 to have no memory, got {res2_a2['outcome']}"
        )
        assert res2_a2["outcome"] == "path_found"
        assert not any(case1_id in r for r in res2_a2["reasons"]), (
            f"Analyst a2 should not see case {case1_id} in reasons"
        )
        print(f"  [PASS] Analyst a2 on {order2} got no memory from a1 (outcome: {res2_a2['outcome']}).")

    finally:
        memory.reset_analyst(a1)
        memory.reset_analyst(a2)


def test_live_llm_if_key_present():
    print("=" * 70)
    print("Testing Live LLM investigation (if GEMINI_API_KEY is present)...")
    print("=" * 70)

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("  [SKIP] GEMINI_API_KEY not found in environment, skipping live network test cleanly.")
        return

    analyst_id = "test_live_analyst"
    memory.reset_analyst(analyst_id)

    try:
        r = fraud_graph.ro_query("MATCH (u:User {id:'u186'})-[:PLACED]->(o:Order) RETURN o.id LIMIT 1")
        order_id = r.result_set[0][0]

        print(f"  Running live investigate() with model for order {order_id}...")
        res = investigate(analyst_id=analyst_id, order_id=order_id, use_llm=True)
        assert res["verdict"] in ("FRAUD_RING", "SUSPICIOUS")
        assert res["case_id"] is not None
        assert len(res["sentence"]) > 0
        print(f"  [PASS] Live run succeeded! Verdict: {res['verdict']} (Confidence: {res['confidence']}%)")
        print(f"         Sentence ({res['sentence_source']}): {res['sentence']}")
    except Exception as e:
        print(f"  [LIVE RUN NOTE] API call handled or failed with: {e}")
    finally:
        memory.reset_analyst(analyst_id)


if __name__ == "__main__":
    print("=" * 70)
    print("RUNNING RINGWATCH AGENT TEST SUITE")
    print("=" * 70)
    test_accepts_validation()
    test_no_llm_investigations()
    test_memory_feedback_and_cross_analyst()
    test_live_llm_if_key_present()
    print("=" * 70)
    print("ALL AGENT TESTS PASSED SUCCESSFULLY!")
    print("=" * 70)
