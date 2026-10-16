"""
agent.py - RingWatch Fraud Investigation Agent.
Integrates episodic analyst memory, graph query tool catalogue, and deterministic verdict engine.
Supports LLM-guided tool calling via official Google Gemini SDK and deterministic fallback pipeline.
Never logs or prints GEMINI_API_KEY.
"""

import argparse
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv

# Load environment variables (never print/log keys)
load_dotenv()

import memory
import tools
import verdict

# Model selection overridable by environment variable
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")

SYSTEM_PROMPT = (
    "You investigate suspected fraud using only the provided tools. "
    "Never invent ids. Use detect_ring and explain_path to gather evidence. "
    "Do not state a verdict; a separate engine does."
)

TOKEN_PATTERN = re.compile(
    r"\b(?:u\d+|o\d+|d[A-Z]\w*|d\d+|c[A-Z]\w*|c\d+|a[A-Z]\w*|a\d+)\b"
)


def extract_id_tokens(text: str) -> List[str]:
    """Extract all entity-like IDs: u<digits>, o<digits>, dA1, dC0, cB, aB, d12, c12, a5."""
    if not text or not isinstance(text, str):
        return []
    return TOKEN_PATTERN.findall(text)


def accepts(sentence: str, result: Dict[str, Any]) -> bool:
    """
    Checks that every id-like token in sentence appears in known ids from results.
    Also rejects if the sentence claims links when there were none.
    """
    if not sentence or not isinstance(sentence, str):
        return False

    tokens = extract_id_tokens(sentence)

    # Gather known IDs from result
    known_ids: Set[str] = set()
    if "known_ids" in result and result["known_ids"]:
        known_ids.update(result["known_ids"])
    if "order_id" in result and result["order_id"]:
        known_ids.add(str(result["order_id"]))
    if "user" in result and result["user"]:
        known_ids.add(str(result["user"]))
    if "ring_members" in result and result["ring_members"]:
        known_ids.update(str(x) for x in result["ring_members"])
    if "shared_entities" in result and result["shared_entities"]:
        known_ids.update(str(x) for x in result["shared_entities"])
    if "path_evidence" in result and result["path_evidence"]:
        for ev in result["path_evidence"]:
            for tok in extract_id_tokens(ev):
                known_ids.add(tok)
    if "trace" in result and result["trace"]:
        for t in result["trace"]:
            args = t.get("args", {})
            for v in args.values():
                if isinstance(v, (list, tuple, set)):
                    known_ids.update(str(x) for x in v)
                elif isinstance(v, str):
                    for tok in extract_id_tokens(v):
                        known_ids.add(tok)

    # 1. Every extracted token must be in known_ids
    for tok in tokens:
        if tok not in known_ids:
            return False

    # 2. Rejects if claiming links when there were none
    verdict_val = result.get("verdict")
    outcome_val = result.get("outcome")
    ring_members = result.get("ring_members", [])

    is_unlinked = (
        verdict_val == verdict.VERDICT_NO_LINKS_FOUND
        or outcome_val == "not_linked"
        or len(ring_members) == 0
    )

    if is_unlinked:
        sentence_lower = sentence.lower()
        link_words = ["linked", "shares", "shared", "ring", "connected", "collaborating", "colluding"]
        for word in link_words:
            if word in sentence_lower:
                negations = [
                    "no linked",
                    "not linked",
                    "no shared",
                    "not shared",
                    "no connection",
                    "without links",
                    "not safe",
                ]
                if not any(neg in sentence_lower for neg in negations):
                    return False

    return True


def build_template_sentence(result: Dict[str, Any]) -> str:
    """Build a deterministic template explanation sentence from investigation findings."""
    order_id = result.get("order_id", "Unknown")
    user = result.get("user", "Unknown")
    verdict_val = result.get("verdict", "")
    outcome_val = result.get("outcome", "")
    ring_members = result.get("ring_members", [])
    shared_kinds = result.get("shared_kinds", [])

    if verdict_val == verdict.VERDICT_NO_LINKS_FOUND or outcome_val == "not_linked" or not ring_members:
        return f"Order {order_id} placed by {user} shows no shared device or card links (this does not mean the account is safe)."
    elif verdict_val == verdict.VERDICT_FRAUD_RING:
        kinds_str = ", ".join(shared_kinds) if shared_kinds else "devices and payment methods"
        return f"Order {order_id} by {user} is part of a fraud ring with {len(ring_members)} members linked via {kinds_str}."
    else:
        return f"Order {order_id} by {user} exhibits suspicious shared connections with {len(ring_members)} other accounts."


def _run_deterministic_pipeline(order_id: str, analyst_id: str, has_memory: bool = False) -> Dict[str, Any]:
    """Execute the deterministic pipeline with tools in a fixed order."""
    traces = []
    known_ids = {order_id}

    # 1. order_facts
    f_res = tools.order_facts(order_id)
    traces.append(f_res["trace"])
    order_user = f_res["data"]["user"]
    known_ids.add(order_user)
    if f_res["data"].get("shipping_address"):
        known_ids.add(f_res["data"]["shipping_address"])

    # 2. find_linked_accounts
    la_res = tools.find_linked_accounts(order_id)
    traces.append(la_res["trace"])
    direct_linked = la_res["data"].get("linked_users", [])
    direct_shared = la_res["data"].get("shared_entities", [])
    known_ids.update(direct_linked)
    known_ids.update(direct_shared)

    # 3. detect_ring
    dr_res = tools.detect_ring(order_user, max_hops=12)
    traces.append(dr_res["trace"])
    ring_members = dr_res["data"].get("members", [])
    known_ids.update(ring_members)

    # 4. explain_path (skipped if memory already confirmed the fraud ring)
    path_evidence = []
    if ring_members and not has_memory:
        target_member = ring_members[-1]
        ep_res = tools.explain_path(order_user, target_member)
        traces.append(ep_res["trace"])
        if ep_res["outcome"] == "path_found":
            path_evidence = ep_res["evidence"]
            known_ids.update(ep_res["data"].get("nodes", []))

    # 5. ring_stats
    all_users = [order_user] + ring_members if ring_members else [order_user]
    rs_res = tools.ring_stats(all_users)
    traces.append(rs_res["trace"])
    shared_kinds = rs_res["data"].get("connecting_entity_kinds", [])

    return {
        "order_user": order_user,
        "ring_members": ring_members,
        "direct_shared": direct_shared,
        "shared_kinds": shared_kinds,
        "path_evidence": path_evidence,
        "ring_stats": rs_res["data"],
        "traces": traces,
        "known_ids": known_ids,
    }


def _run_llm_pipeline(order_id: str, analyst_id: str, initial_data: Dict[str, Any]) -> Tuple[Dict[str, Any], str, str]:
    """
    Execute Gemini function calling loop with max 6 tool calls.
    Returns (pipeline_data, explanation_sentence, sentence_source).
    """
    from google import genai
    from google.genai import types

    client = genai.Client()

    traces = list(initial_data.get("traces", []))
    known_ids = set(initial_data.get("known_ids", set()))
    path_evidence = list(initial_data.get("path_evidence", []))
    ring_members = list(initial_data.get("ring_members", []))
    shared_kinds = list(initial_data.get("shared_kinds", []))
    order_user = initial_data.get("order_user", "")

    tool_call_count = 0
    max_tool_calls = 6

    # Wrapped tool catalogue for LLM
    def order_facts(order_id: str) -> dict:
        """Get order details including user, amount, flagged status, and shipping address."""
        nonlocal tool_call_count
        tool_call_count += 1
        res = tools.order_facts(order_id)
        traces.append(res["trace"])
        if res.get("data"):
            if res["data"].get("user"):
                known_ids.add(res["data"]["user"])
            if res["data"].get("shipping_address"):
                known_ids.add(res["data"]["shipping_address"])
        return res

    def find_linked_accounts(order_id: str) -> dict:
        """Find users directly linked to an order via shared Device or Card."""
        nonlocal tool_call_count
        tool_call_count += 1
        res = tools.find_linked_accounts(order_id)
        traces.append(res["trace"])
        if res.get("data"):
            known_ids.update(res["data"].get("linked_users", []))
            known_ids.update(res["data"].get("shared_entities", []))
        return res

    def detect_ring(user_id: str, max_hops: int = 12) -> dict:
        """Detect accounts reachable via shared devices/cards within max_hops (max 12)."""
        nonlocal tool_call_count, ring_members
        tool_call_count += 1
        res = tools.detect_ring(user_id, max_hops=min(max_hops, 12))
        traces.append(res["trace"])
        if res.get("data"):
            members = res["data"].get("members", [])
            ring_members = sorted(list(set(ring_members + members)))
            known_ids.update(members)
        return res

    def explain_path(user_a: str, user_b: str) -> dict:
        """Find the shortest path and connecting evidence between two users."""
        nonlocal tool_call_count, path_evidence
        tool_call_count += 1
        res = tools.explain_path(user_a, user_b)
        traces.append(res["trace"])
        if res.get("data") and res["outcome"] == "path_found":
            path_evidence = res.get("evidence", [])
            known_ids.update(res["data"].get("nodes", []))
        return res

    def ring_stats(user_ids: list[str]) -> dict:
        """Compute aggregate order count, flagged count, total amount, and connecting kinds."""
        nonlocal tool_call_count, shared_kinds
        tool_call_count += 1
        res = tools.ring_stats(user_ids)
        traces.append(res["trace"])
        if res.get("data"):
            shared_kinds = res["data"].get("connecting_entity_kinds", [])
        return res

    tool_list = [order_facts, find_linked_accounts, detect_ring, explain_path, ring_stats]

    # Create chat session with AFC enabled
    chat = client.chats.create(
        model=GEMINI_MODEL,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=tool_list,
        ),
    )

    has_mem = initial_data.get("has_memory")
    if has_mem:
        prompt = (
            f"Investigate order {order_id} placed by {order_user}. "
            "Note: Past confirmed fraud cases already exist in memory for this entity ring. "
            "Verify group stats with ring_stats and summarize concisely without redundant path explorations. "
            "Conclude with ONE short sentence explaining the findings using only observed IDs."
        )
    else:
        prompt = (
            f"Investigate suspected fraud for order {order_id} placed by {order_user}. "
            "Use detect_ring and explain_path to discover connected ring members and evidence. "
            "Conclude with ONE short sentence explaining the findings using only observed IDs."
        )

    resp = chat.send_message(prompt)
    sentence = resp.text.strip() if resp and resp.text else ""

    # Ensure all user ids are in ring_stats
    all_users = [order_user] + ring_members if ring_members else [order_user]
    rs_res = tools.ring_stats(all_users)
    traces.append(rs_res["trace"])
    shared_kinds = rs_res["data"].get("connecting_entity_kinds", [])

    pipeline_data = {
        "order_user": order_user,
        "ring_members": ring_members,
        "shared_kinds": shared_kinds,
        "path_evidence": path_evidence,
        "ring_stats": rs_res["data"],
        "traces": traces,
        "known_ids": known_ids,
        "tool_calls": tool_call_count,
    }

    return pipeline_data, sentence, "llm"


def investigate(
    analyst_id: str,
    order_id: str,
    use_llm: bool = True,
) -> Dict[str, Any]:
    """
    Main investigation entrypoint.
    Returns:
        {"outcome": "path_found"|"answered_from_memory"|"not_linked"|"unknown_entity",
         "verdict", "confidence", "reasons", "ring_members", "path_evidence",
         "recall", "sentence", "sentence_source", "case_id",
         "trace": [...], "tool_calls": int, "elapsed_ms": float}
    """
    start_time = time.perf_counter()

    # Rule 2: unknown order id -> unknown_entity immediately, no other tool runs, no case saved.
    if not tools.resolve("order", order_id):
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return {
            "outcome": "unknown_entity",
            "verdict": None,
            "confidence": 0,
            "reasons": ["Order not found in graph"],
            "ring_members": [],
            "path_evidence": [],
            "recall": None,
            "sentence": f"Order {order_id} could not be resolved in the graph.",
            "sentence_source": "fallback",
            "case_id": None,
            "trace": [],
            "tool_calls": 0,
            "elapsed_ms": elapsed_ms,
        }

    # Step 1a: quick initial facts and links to seed recall
    f_res = tools.order_facts(order_id)
    la_res = tools.find_linked_accounts(order_id)

    order_user = f_res["data"]["user"]
    linked_users = la_res["data"].get("linked_users", [])
    shared_entities = la_res["data"].get("shared_entities", [])

    seed_entities = [order_user] + linked_users + shared_entities
    recall_result = memory.recall_similar(analyst_id, seed_entities)

    initial_traces = [f_res["trace"], la_res["trace"]]
    initial_known_ids = {order_id, order_user}
    initial_known_ids.update(linked_users)
    initial_known_ids.update(shared_entities)
    if f_res["data"].get("shipping_address"):
        initial_known_ids.add(f_res["data"]["shipping_address"])

    has_memory = bool(
        recall_result
        and recall_result.get("outcome") == "answered_from_memory"
        and len(recall_result.get("cases", [])) > 0
    )

    initial_data = {
        "order_user": order_user,
        "ring_members": linked_users,
        "shared_entities": shared_entities,
        "shared_kinds": [],
        "path_evidence": [],
        "traces": initial_traces,
        "known_ids": initial_known_ids,
        "has_memory": has_memory,
        "recall_result": recall_result,
    }

    pipeline_data = None
    raw_sentence = ""
    sentence_source = "fallback"
    tool_call_count = len(initial_traces)

    # Fast-path: if no linked accounts and no memory, check detect_ring directly to avoid unnecessary LLM latency
    if not linked_users and not shared_entities and not has_memory:
        dr_res = tools.detect_ring(order_user, max_hops=12)
        initial_traces.append(dr_res["trace"])
        if not dr_res["data"].get("members"):
            pipeline_data = {
                "order_user": order_user,
                "ring_members": [],
                "direct_shared": [],
                "shared_kinds": [],
                "path_evidence": [],
                "ring_stats": {"order_count": 1, "flagged_count": 0, "total_amount": f_res["data"]["amount"], "connecting_entity_kinds": []},
                "traces": initial_traces,
                "known_ids": initial_known_ids,
            }
            tool_call_count = len(initial_traces)
            sentence_source = "fallback"

    # Step 1b & 3: Run LLM tool calling loop with retry and deterministic fallback
    if pipeline_data is None and use_llm and os.getenv("GEMINI_API_KEY"):
        # Retry with backoff up to 2 retries
        for attempt in range(3):
            try:
                pipeline_data, raw_sentence, sentence_source = _run_llm_pipeline(
                    order_id, analyst_id, initial_data
                )
                tool_call_count += pipeline_data.get("tool_calls", 0)
                break
            except Exception:
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                else:
                    pipeline_data = None  # Fall back to deterministic

    if pipeline_data is None:
        # Fallback deterministic pipeline
        det_data = _run_deterministic_pipeline(order_id, analyst_id, has_memory=has_memory)
        pipeline_data = det_data
        tool_call_count = len(det_data["traces"])
        sentence_source = "fallback"

    # Step 1c: Compute verdict from results
    ring_members = pipeline_data["ring_members"]
    shared_kinds = pipeline_data["shared_kinds"]
    path_evidence = pipeline_data["path_evidence"]
    all_traces = pipeline_data["traces"]
    known_ids = pipeline_data["known_ids"]

    v_res = verdict.verdict(
        ring_stats=pipeline_data["ring_stats"],
        shared_kinds=shared_kinds,
        recall_result=recall_result,
    )

    result_summary = {
        "order_id": order_id,
        "user": order_user,
        "ring_members": ring_members,
        "shared_entities": pipeline_data.get("direct_shared", []),
        "shared_kinds": shared_kinds,
        "path_evidence": path_evidence,
        "verdict": v_res["verdict"],
        "confidence": v_res["confidence"],
        "known_ids": known_ids,
        "trace": all_traces,
    }

    # Step 1d & 1e: Check sentence with accepts()
    final_sentence = raw_sentence
    if not accepts(final_sentence, result_summary):
        final_sentence = build_template_sentence(result_summary)
        sentence_source = "fallback"

    # Determine overall outcome
    has_memory = (
        recall_result
        and recall_result.get("outcome") == "answered_from_memory"
        and len(recall_result.get("cases", [])) > 0
    )
    if has_memory:
        outcome = "answered_from_memory"
    elif v_res["verdict"] == verdict.VERDICT_NO_LINKS_FOUND or not ring_members:
        outcome = "not_linked"
    else:
        outcome = "path_found"

    # Step 1f: Save case to memory
    shape = "mixed"
    if shared_kinds == ["Device"]:
        shape = "shared_device_chain"
    elif shared_kinds == ["Card"]:
        shape = "shared_card_ring"

    case_id = memory.save_case(
        analyst_id=analyst_id,
        order_id=order_id,
        ring_members=([order_user] + ring_members) if ring_members else [order_user],
        shared_entities=list(pipeline_data.get("direct_shared", [])) + shared_kinds,
        verdict=v_res["verdict"],
        confidence=v_res["confidence"],
        shape=shape,
    )

    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

    return {
        "outcome": outcome,
        "verdict": v_res["verdict"],
        "confidence": v_res["confidence"],
        "reasons": v_res["reasons"],
        "ring_members": ring_members,
        "path_evidence": path_evidence,
        "recall": recall_result,
        "sentence": final_sentence,
        "sentence_source": sentence_source,
        "case_id": case_id,
        "trace": all_traces,
        "tool_calls": tool_call_count,
        "elapsed_ms": elapsed_ms,
    }


def give_feedback(analyst_id: str, case_id: str, confirmed: bool) -> Dict[str, Any]:
    """Record analyst feedback on a previously saved case."""
    return memory.record_feedback(analyst_id, case_id, confirmed)


def print_cli_summary(res: Dict[str, Any]) -> None:
    """Print formatted investigation result to terminal."""
    print()
    print("=" * 80)
    print("  RINGWATCH INVESTIGATION REPORT")
    print("=" * 80)
    print(f"Outcome:         {res.get('outcome')}")
    print(f"Verdict:         {res.get('verdict')}")
    print(f"Confidence:      {res.get('confidence')}%")
    print(f"Case ID:         {res.get('case_id')}")
    print(f"Sentence Source: {res.get('sentence_source')}")
    print(f"Elapsed Time:    {res.get('elapsed_ms')} ms (Tool calls: {res.get('tool_calls')})")
    print()
    print("Explanation:")
    print(f"  {res.get('sentence')}")
    print()
    print("Reasons:")
    for r in res.get("reasons", []):
        print(f"  - {r}")
    print()
    if res.get("path_evidence"):
        print("Path Evidence:")
        for ev in res.get("path_evidence", []):
            print(f"  -> {ev}")
        print()

    print("Trace Table:")
    print(f"{'Tool':<25} {'Args':<35} {'ms':>8} {'Rows':>6}")
    print("-" * 80)
    for t in res.get("trace", []):
        tool_name = str(t.get("tool", ""))
        args_str = str(t.get("args", ""))
        if len(args_str) > 34:
            args_str = args_str[:31] + "..."
        ms = str(t.get("ms", 0))
        rows = str(t.get("rows", 0))
        print(f"{tool_name:<25} {args_str:<35} {ms:>8} {rows:>6}")
    print("=" * 80)
    print()


def main():
    parser = argparse.ArgumentParser(description="RingWatch Fraud Investigation Agent")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Investigate command
    inv_parser = subparsers.add_parser("investigate", help="Investigate an order")
    inv_parser.add_argument("--analyst", default="a1", help="Analyst ID")
    inv_parser.add_argument("--order", required=True, help="Order ID to investigate")
    inv_parser.add_argument("--no-llm", action="store_true", help="Run deterministic pipeline without LLM")

    # Feedback command
    fb_parser = subparsers.add_parser("feedback", help="Record feedback on a case")
    fb_parser.add_argument("--analyst", default="a1", help="Analyst ID")
    fb_parser.add_argument("--case", required=True, help="Case ID to review")
    fb_group = fb_parser.add_mutually_exclusive_group(required=True)
    fb_group.add_argument("--confirm", action="store_true", help="Confirm fraud verdict")
    fb_group.add_argument("--reject", action="store_true", help="Reject fraud verdict")

    args = parser.parse_args()

    if args.command == "investigate":
        use_llm = not args.no_llm
        res = investigate(analyst_id=args.analyst, order_id=args.order, use_llm=use_llm)
        print_cli_summary(res)
    elif args.command == "feedback":
        confirmed = True if args.confirm else False
        res = give_feedback(analyst_id=args.analyst, case_id=args.case, confirmed=confirmed)
        print(f"Feedback recorded: {res}")


if __name__ == "__main__":
    main()
