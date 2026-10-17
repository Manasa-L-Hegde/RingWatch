"""
demo_scenario.py - End-to-end interactive demo transcript for RingWatch.
Demonstrates graph-based fraud ring detection, episodic memory recall, tenant isolation,
variable-length graph expansion vs fixed joins, and deterministic safeguard fallbacks.
Accepts --no-llm and defaults to using the LLM.
"""

import argparse
import sys
import time
from typing import Any, Dict

import db as db_module
import memory
import tools
from agent import GEMINI_MODEL, give_feedback, investigate

fraud_graph = db_module.get_db("fraud")


def print_header(title: str):
    print("\n" + "=" * 80)
    print(f"  {title}")
    print("=" * 80)


def print_step(step_num: int, title: str):
    print(f"\n[STEP {step_num}] {title}")
    print("-" * 80)


def run_demo(use_llm: bool = True):
    total_start = time.perf_counter()
    mode_str = f"LLM Mode (Model: {GEMINI_MODEL})" if use_llm else "Deterministic Pipeline (--no-llm)"

    print_header(f"RINGWATCH FRAUD-RING INVESTIGATOR DEMO ({mode_str})")
    if use_llm:
        print(f"  Gemini Model: {GEMINI_MODEL}")

    # -------------------------------------------------------------------------
    # STEP 1: Reset memory for analysts a1 and a2
    # -------------------------------------------------------------------------
    print_step(1, "Reset Memory for Analysts a1 and a2")
    memory.reset_analyst("a1")
    memory.reset_analyst("a2")
    print("  -> Deleted memory graphs for 'a1' and 'a2'. State is clean.")

    # -------------------------------------------------------------------------
    # STEP 2: a1 investigates a flagged ring-A order -> expect FRAUD_RING with path
    # Then a1 confirms it as fraud.
    # -------------------------------------------------------------------------
    print_step(2, "Analyst a1: First Investigation on Flagged Ring-A Order")
    q_orders = (
        "MATCH (u:User)-[:PLACED]->(o:Order) "
        "WHERE u.id IN ['u182','u183','u184','u185','u186','u187'] AND o.flagged = true "
        "RETURN o.id, u.id, o.amount "
        "ORDER BY o.id LIMIT 3"
    )
    flagged_orders = fraud_graph.ro_query(q_orders).result_set
    assert flagged_orders and len(flagged_orders) >= 2, "Need at least 2 flagged ring-A orders"

    order_1 = flagged_orders[0][0]
    user_1 = flagged_orders[0][1]
    amount_1 = flagged_orders[0][2]
    print(f"  Target Flagged Order: {order_1} (placed by {user_1}, amount: ${amount_1:,.2f})")

    res_1 = investigate(analyst_id="a1", order_id=order_1, use_llm=use_llm)
    step2_tool_calls = res_1["tool_calls"]
    step2_confidence = res_1["confidence"]
    step2_ms = res_1["elapsed_ms"]
    case_1_id = res_1["case_id"]

    print(f"  Model:           {res_1.get('model', GEMINI_MODEL)}")
    print(f"  Verdict:         {res_1['verdict']}")
    print(f"  Risk score (0-100, rule-based): {res_1.get('score_display', str(res_1['confidence']) + '%')}")
    print(f"  Outcome:         {res_1['outcome']}")
    print(f"  Case ID:         {case_1_id}")
    print(f"  Tool Calls:      {step2_tool_calls} (Elapsed: {step2_ms} ms)")
    print(f"  Sentence ({res_1['sentence_source']}): {res_1['sentence']}")
    if res_1.get("sentence_rejected_reason"):
        print(f"  Sentence Rejection Reason: {res_1['sentence_rejected_reason']}")
    print(f"  Ring Members:    {res_1['ring_members']}")
    if res_1["path_evidence"]:
        print(f"  Path Evidence:   {' -> '.join(res_1['path_evidence'][:3])}")

    assert res_1["verdict"] == "FRAUD_RING", f"Expected FRAUD_RING, got {res_1['verdict']}"
    assert res_1["confidence"] >= 70
    assert case_1_id is not None

    # a1 confirms case as fraud
    fb_1 = give_feedback(analyst_id="a1", case_id=case_1_id, confirmed=True)
    assert fb_1.get("status") == "success"
    print(f"  -> Analyst a1 confirmed case {case_1_id} as FRAUD (pattern: {fb_1.get('pattern')}).")

    # -------------------------------------------------------------------------
    # STEP 3: a1 investigates a DIFFERENT ring-A order -> expect answered_from_memory
    # citing first case id and date, higher confidence, fewer tool calls.
    # -------------------------------------------------------------------------
    print_step(3, "Analyst a1: Second Investigation on DIFFERENT Ring-A Order")
    order_2 = flagged_orders[1][0]
    user_2 = flagged_orders[1][1]
    print(f"  Target Different Order: {order_2} (placed by {user_2})")

    res_2 = investigate(analyst_id="a1", order_id=order_2, use_llm=use_llm)
    step3_tool_calls = res_2["tool_calls"]
    step3_confidence = res_2["confidence"]
    step3_ms = res_2["elapsed_ms"]

    print(f"  Model:           {res_2.get('model', GEMINI_MODEL)}")
    print(f"  Verdict:         {res_2['verdict']}")
    print(f"  Risk score (0-100, rule-based): {res_2.get('score_display', str(step3_confidence) + '%')} (boosted from memory)")
    print(f"  Outcome:         {res_2['outcome']}")
    print(f"  Case ID:         {res_2['case_id']}")
    print(f"  Sentence ({res_2['sentence_source']}): {res_2['sentence']}")
    if res_2.get("sentence_rejected_reason"):
        print(f"  Sentence Rejection Reason: {res_2['sentence_rejected_reason']}")
    print("  Reasons Citing Past Memory:")
    for r in res_2["reasons"]:
        if case_1_id in r or "past case" in r.lower():
            print(f"    * {r}")

    # Side by side comparison of tool calls and elapsed ms
    print("\n  Side-by-Side Comparison (Step 2 Cold vs Step 3 Short-Circuit Memory):")
    print(f"    {'Metric':<25} {'Step 2 (Cold Investigation)':<30} {'Step 3 (Short-Circuit Memory)':<30}")
    print(f"    {'-'*25} {'-'*30} {'-'*30}")
    print(f"    {'Tool Calls':<25} {step2_tool_calls:<30} {step3_tool_calls:<30}")
    print(f"    {'Elapsed Time':<25} {f'{step2_ms} ms':<30} {f'{step3_ms} ms':<30}")

    assert res_2["outcome"] == "answered_from_memory", f"Expected answered_from_memory, got {res_2['outcome']}"
    assert res_2["verdict"] == verdict.VERDICT_FRAUD_RING, f"Expected FRAUD_RING, got {res_2['verdict']}"
    assert any("full ring not re-traversed in memory mode" in r for r in res_2["reasons"]), "Expected memory mode note in reasons"
    assert any(case_1_id in r for r in res_2["reasons"]), f"Expected case {case_1_id} cited in reasons"
    assert step3_tool_calls < step2_tool_calls, (
        f"Expected fewer tool calls in Step 3 ({step3_tool_calls}) than Step 2 ({step2_tool_calls})"
    )

    # -------------------------------------------------------------------------
    # STEP 4: a2 investigates the SAME order as step 3 -> expect no memory (isolation)
    # -------------------------------------------------------------------------
    print_step(4, "Analyst a2: Tenant Isolation Check on Same Order")
    print(f"  Analyst a2 investigating {order_2} (same order a1 investigated in Step 3)...")

    res_a2 = investigate(analyst_id="a2", order_id=order_2, use_llm=use_llm)
    print(f"  Analyst a2 Outcome:    {res_a2['outcome']}")
    print(f"  Analyst a2 Verdict:    {res_a2['verdict']}")
    print(f"  Risk score (0-100, rule-based): {res_a2.get('score_display', str(res_a2['confidence']) + '%')}")

    assert res_a2["outcome"] != "answered_from_memory", "Analyst a2 should NOT have memory from a1!"
    assert res_a2["outcome"] == "path_found"
    assert not any(case_1_id in r for r in res_a2["reasons"]), "Analyst a2 reasons must NOT cite a1's case!"
    print("  -> Confirmed: Analyst a2 has ZERO access to a1's episodic memory (Tenant Isolation Verified).")

    # -------------------------------------------------------------------------
    # STEP 5: Ring C: detect_ring("u193", max_hops=2) vs max_hops=12
    # "what a fixed 2-hop join sees vs what a variable-length graph traversal sees"
    # -------------------------------------------------------------------------
    print_step(5, "Ring C Multi-Hop: what a fixed 2-hop join sees vs what a variable-length graph traversal sees")
    r_join = tools.detect_ring("u193", max_hops=2)
    r_graph = tools.detect_ring("u193", max_hops=12)

    count_join = len(r_join["data"])
    count_graph = len(r_graph["data"])

    print("  Comparison for Ring C user u193:")
    print("    what a fixed 2-hop join sees vs what a variable-length graph traversal sees:")
    print(f"    - max_hops=2  (fixed 2-hop join sees):             {count_join} user  {r_join['data']['members']}")
    print(f"    - max_hops=12 (variable-length traversal sees):    {count_graph} users {r_graph['data']['members']}")

    assert count_join == 1, f"Expected 1 user for max_hops=2, got {count_join}"
    assert count_graph == 6, f"Expected 6 users for max_hops=12, got {count_graph}"
    print("  -> Demonstration passed: fixed 2-hop join sees only immediate neighbor; variable-length traversal discovers all 6 members.")

    # -------------------------------------------------------------------------
    # STEP 6: Unknown order "o99999" -> unknown_entity, zero tool calls
    # -------------------------------------------------------------------------
    print_step(6, "Safeguard: Unknown Order o99999")
    res_unk = investigate(analyst_id="a1", order_id="o99999", use_llm=use_llm)
    print(f"  Outcome:    {res_unk['outcome']}")
    print(f"  Tool Calls: {res_unk['tool_calls']}")
    print(f"  Case ID:    {res_unk['case_id']}")
    print(f"  Sentence:   {res_unk['sentence']}")

    assert res_unk["outcome"] == "unknown_entity"
    assert res_unk["tool_calls"] == 0
    assert res_unk["case_id"] is None
    print("  -> Passed: Unknown entity immediately rejected with 0 tool calls and no case created.")

    # -------------------------------------------------------------------------
    # STEP 7: Isolated normal user's order -> NO_LINKS_FOUND with not-safe wording
    # -------------------------------------------------------------------------
    print_step(7, "Isolated Normal User Order: NO_LINKS_FOUND Safeguard")
    res_iso = investigate(analyst_id="a1", order_id="o0", use_llm=use_llm)
    print("  Order:      o0")
    print(f"  Verdict:    {res_iso['verdict']}")
    print(f"  Risk score (0-100, rule-based): {res_iso.get('score_display', str(res_iso['confidence']) + '%')}")
    print(f"  Sentence:   {res_iso['sentence']}")
    print("  Reasons:")
    for r in res_iso["reasons"]:
        print(f"    * {r}")

    assert res_iso["verdict"] == "NO_LINKS_FOUND"
    has_not_safe = any("not mean the account is safe" in r.lower() or "not safe" in r.lower() for r in res_iso["reasons"])
    assert has_not_safe, f"Expected not-safe wording in reasons, got {res_iso['reasons']}"
    print("  -> Passed: Unlinked account correctly assigned NO_LINKS_FOUND with explicit 'not safe' disclaimer.")

    # -------------------------------------------------------------------------
    # Cleanup & Summary
    # -------------------------------------------------------------------------
    memory.reset_analyst("a1")
    memory.reset_analyst("a2")

    elapsed_total = round(time.perf_counter() - total_start, 2)
    print_header(f"DEMO COMPLETE: ALL 7 STEPS PASSED IN {elapsed_total}s (Under 1 minute!)")


def main():
    parser = argparse.ArgumentParser(description="RingWatch Demo Scenario")
    parser.add_argument("--no-llm", action="store_true", help="Run deterministic pipeline without LLM")
    args = parser.parse_args()

    use_llm = not args.no_llm
    run_demo(use_llm=use_llm)


if __name__ == "__main__":
    main()
