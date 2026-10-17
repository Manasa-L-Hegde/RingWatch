"""
agent.py - RingWatch Fraud Investigation Agent.
Integrates episodic analyst memory, graph query tool catalogue, and deterministic verdict engine.
Supports LLM-guided tool calling via official Google Gemini SDK and deterministic fallback pipeline.
Never logs or prints GEMINI_API_KEY.
"""

import argparse
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from dotenv import load_dotenv

# Load environment variables (never print/log keys)
load_dotenv()

import db as db_module
import memory
import tools
import verdict

# Model selection overridable by environment variable
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")

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


def check_sentence(sentence: str, result: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """
    Checks that every id-like token in sentence appears in known ids from results.
    Also rejects if the sentence claims links when there were none.
    Returns (is_accepted, rejected_reason).
    """
    if not sentence or not isinstance(sentence, str) or not sentence.strip():
        return False, "Sentence is empty or not a string."

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
    if "recall" in result and result["recall"]:
        for c in result["recall"].get("cases", []):
            if c.get("case_id"):
                known_ids.add(str(c["case_id"]))
            for ent in c.get("overlapping_entities", []):
                known_ids.add(str(ent))
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
    invalid_tokens = [tok for tok in tokens if tok not in known_ids]
    if invalid_tokens:
        return False, f"Unknown/unsupported ID tokens: {invalid_tokens} (not in known IDs: {sorted(list(known_ids))})"

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
                    return False, f"Claimed link/connection ('{word}') when no links were found"

    return True, None


def accepts(sentence: str, result: Dict[str, Any], return_reason: bool = False) -> Union[bool, Tuple[bool, Optional[str]]]:
    """
    Checks that every id-like token in sentence appears in known ids from results.
    Also rejects if the sentence claims links when there were none.
    """
    ok, reason = check_sentence(sentence, result)
    if return_reason:
        return ok, reason
    return ok


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


def _run_deterministic_pipeline(order_id: str, analyst_id: str, has_memory: bool = False, cached_call=None) -> Dict[str, Any]:
    """Execute the deterministic pipeline with tools in a fixed order, using cached_call if provided."""
    traces = []
    known_ids = {order_id}

    def _call(tool_name: str, args: Dict[str, Any], func):
        if cached_call:
            res, trace_entry, _ = cached_call(tool_name, args, func)
            traces.append(trace_entry)
            return res
        res = func(**args)
        traces.append(res["trace"])
        return res

    # 1. order_facts
    f_res = _call("order_facts", {"order_id": order_id}, tools.order_facts)
    order_user = f_res["data"]["user"]
    known_ids.add(order_user)
    if f_res["data"].get("shipping_address"):
        known_ids.add(f_res["data"]["shipping_address"])

    # 2. find_linked_accounts
    la_res = _call("find_linked_accounts", {"order_id": order_id}, tools.find_linked_accounts)
    direct_linked = la_res["data"].get("linked_users", [])
    direct_shared = la_res["data"].get("shared_entities", [])
    known_ids.update(direct_linked)
    known_ids.update(direct_shared)

    # 3. detect_ring
    dr_res = _call("detect_ring", {"user_id": order_user, "max_hops": 12}, tools.detect_ring)
    ring_members = dr_res["data"].get("members", [])
    known_ids.update(ring_members)

    # 4. explain_path (skipped if memory already confirmed the fraud ring)
    path_evidence = []
    if ring_members and not has_memory:
        target_member = ring_members[-1]
        ep_res = _call("explain_path", {"user_a": order_user, "user_b": target_member}, tools.explain_path)
        if ep_res["outcome"] == "path_found":
            path_evidence = ep_res["evidence"]
            known_ids.update(ep_res["data"].get("nodes", []))

    # 5. ring_stats
    all_users = [order_user] + ring_members if ring_members else [order_user]
    rs_res = _call("ring_stats", {"user_ids": all_users}, tools.ring_stats)
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


def _run_llm_pipeline(order_id: str, analyst_id: str, initial_data: Dict[str, Any], cached_call=None) -> Tuple[Dict[str, Any], str, str]:
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

    def _call(tool_name: str, args: Dict[str, Any], func):
        nonlocal tool_call_count
        if cached_call:
            res, trace_entry, is_hit = cached_call(tool_name, args, func)
            if not is_hit:
                tool_call_count += 1
            traces.append(trace_entry)
            return res
        tool_call_count += 1
        res = func(**args)
        traces.append(res["trace"])
        return res

    # Wrapped tool catalogue for LLM
    def order_facts(order_id: str) -> dict:
        """Get order details including user, amount, flagged status, and shipping address."""
        res = _call("order_facts", {"order_id": order_id}, tools.order_facts)
        if res.get("data"):
            if res["data"].get("user"):
                known_ids.add(res["data"]["user"])
            if res["data"].get("shipping_address"):
                known_ids.add(res["data"]["shipping_address"])
        return res

    def find_linked_accounts(order_id: str) -> dict:
        """Find users directly linked to an order via shared Device or Card."""
        res = _call("find_linked_accounts", {"order_id": order_id}, tools.find_linked_accounts)
        if res.get("data"):
            known_ids.update(res["data"].get("linked_users", []))
            known_ids.update(res["data"].get("shared_entities", []))
        return res

    def detect_ring(user_id: str, max_hops: int = 12) -> dict:
        """Detect accounts reachable via shared devices/cards within max_hops (max 12)."""
        nonlocal ring_members
        res = _call("detect_ring", {"user_id": user_id, "max_hops": min(max_hops, 12)}, tools.detect_ring)
        if res.get("data"):
            members = res["data"].get("members", [])
            ring_members = sorted(list(set(ring_members + members)))
            known_ids.update(members)
        return res

    def explain_path(user_a: str, user_b: str) -> dict:
        """Find the shortest path and connecting evidence between two users."""
        nonlocal path_evidence
        res = _call("explain_path", {"user_a": user_a, "user_b": user_b}, tools.explain_path)
        if res.get("data") and res["outcome"] == "path_found":
            path_evidence = res.get("evidence", [])
            known_ids.update(res["data"].get("nodes", []))
        return res

    def ring_stats(user_ids: list[str]) -> dict:
        """Compute aggregate order count, flagged count, total amount, and connecting kinds."""
        nonlocal shared_kinds
        res = _call("ring_stats", {"user_ids": user_ids}, tools.ring_stats)
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
    rs_res = _call("ring_stats", {"user_ids": all_users}, tools.ring_stats)
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
    Main investigation entrypoint with caching and memory short-circuit.
    Returns:
        {"outcome": "path_found"|"answered_from_memory"|"not_linked"|"unknown_entity",
         "verdict", "confidence", "raw_score", "score_display", "reasons", "ring_members", "path_evidence",
         "recall", "sentence", "raw_sentence", "sentence_source", "sentence_rejected_reason", "model", "case_id",
         "trace": [...], "tool_calls": int, "cache_hits": int, "elapsed_ms": float}
    """
    start_time = time.perf_counter()

    # Rule 2: unknown order id -> unknown_entity immediately, no other tool runs, no case saved.
    if not tools.resolve("order", order_id):
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
        return {
            "outcome": "unknown_entity",
            "verdict": None,
            "confidence": 0,
            "raw_score": 0,
            "score_display": "0",
            "reasons": ["Order not found in graph"],
            "ring_members": [],
            "path_evidence": [],
            "recall": None,
            "sentence": f"Order {order_id} could not be resolved in the graph.",
            "raw_sentence": f"Order {order_id} could not be resolved in the graph.",
            "sentence_source": "fallback",
            "sentence_rejected_reason": None,
            "model": GEMINI_MODEL,
            "case_id": None,
            "trace": [],
            "tool_calls": 0,
            "cache_hits": 0,
            "elapsed_ms": elapsed_ms,
        }

    # In-investigation tool result cache for deduplication
    tool_cache: Dict[Tuple[str, Tuple], Any] = {}
    cache_hits: int = 0
    executed_tool_calls: int = 0

    def cached_call(tool_name: str, args_dict: Dict[str, Any], tool_func) -> Tuple[Any, Dict[str, Any], bool]:
        nonlocal cache_hits, executed_tool_calls
        norm_args = {}
        for k, v in args_dict.items():
            if isinstance(v, list):
                norm_args[k] = tuple(v)
            else:
                norm_args[k] = v
        key = (tool_name, tuple(sorted(norm_args.items())))

        if key in tool_cache:
            cache_hits += 1
            cached_res = tool_cache[key]
            trace_entry = {
                "tool": tool_name,
                "args": args_dict,
                "ms": 0.0,
                "rows": cached_res.get("trace", {}).get("rows", 0),
                "cache_hit": True,
            }
            return cached_res, trace_entry, True

        executed_tool_calls += 1
        res = tool_func(**args_dict)
        trace_entry = dict(res.get("trace", {}))
        trace_entry["cache_hit"] = False
        tool_cache[key] = res
        return res, trace_entry, False

    # Cheap initial tools: order_facts and find_linked_accounts
    f_res, f_trace, _ = cached_call("order_facts", {"order_id": order_id}, tools.order_facts)
    la_res, la_trace, _ = cached_call("find_linked_accounts", {"order_id": order_id}, tools.find_linked_accounts)

    initial_traces = [f_trace, la_trace]
    order_user = f_res["data"]["user"]
    linked_users = la_res["data"].get("linked_users", [])
    shared_entities = la_res["data"].get("shared_entities", [])

    seed_entities = [order_user] + linked_users + shared_entities
    recall_result = memory.recall_similar(analyst_id, seed_entities)

    # Memory short-circuit check:
    # When recall finds a CONFIRMED past case sharing >= 3 entities with the current order,
    # skip the LLM tool loop. Run only the cheap verification tools, compute verdict with memory boost,
    # return outcome "answered_from_memory".
    confirmed_case = None
    if recall_result and recall_result.get("cases"):
        for c in recall_result["cases"]:
            if str(c.get("status", "")).lower() == "confirmed" and c.get("overlap_count", 0) >= 3:
                confirmed_case = c
                break

    if confirmed_case is not None:
        # Short-circuit triggered!
        cid = confirmed_case.get("case_id", "unknown")
        cdate_raw = confirmed_case.get("date", "unknown")
        cdate = tools.format_display_date(cdate_raw) if cdate_raw != "unknown" else "unknown"
        overlap = confirmed_case.get("overlap_count", 0)

        # Derive connecting entity kinds from shared_entities observed in cheap tools
        shared_kinds = []
        for se in shared_entities:
            if se.startswith("d") and "Device" not in shared_kinds:
                shared_kinds.append("Device")
            elif se.startswith("c") and "Card" not in shared_kinds:
                shared_kinds.append("Card")
            elif se.startswith("a") and "Address" not in shared_kinds:
                shared_kinds.append("Address")

        all_ring_users = [order_user] + linked_users
        cheap_stats = {
            "user_ids": all_ring_users,
            "order_count": max(len(all_ring_users), 3),
            "flagged_count": 1 if f_res["data"].get("flagged") else 0,
            "total_amount": float(f_res["data"].get("amount", 0.0)),
            "connecting_entity_kinds": shared_kinds,
        }

        v_res = verdict.verdict(
            ring_stats=cheap_stats,
            shared_kinds=shared_kinds,
            recall_result=recall_result,
        )

        final_conf = v_res["confidence"]
        raw_score = v_res.get("raw_score", final_conf)
        score_disp = v_res.get("score_display", str(final_conf))
        observed_reasons = list(v_res["reasons"]) + [
            f"linked users observed: {len(all_ring_users)} (full ring not re-traversed in memory mode)"
        ]

        shape = "mixed"
        if shared_kinds == ["Device"]:
            shape = "shared_device_chain"
        elif shared_kinds == ["Card"]:
            shape = "shared_card_ring"

        memory_note = None
        try:
            new_case_id = memory.save_case(
                analyst_id=analyst_id,
                order_id=order_id,
                ring_members=all_ring_users,
                shared_entities=shared_entities + shared_kinds,
                verdict=verdict.VERDICT_FRAUD_RING,
                confidence=final_conf,
                shape=shape,
            )
        except memory.MemoryCapExceeded as e:
            new_case_id = None
            memory_note = str(e)

        sentence = f"Order {order_id} answered from memory: matches confirmed case {cid} on {cdate} sharing {overlap} entities."
        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

        res = {
            "outcome": "answered_from_memory",
            "verdict": verdict.VERDICT_FRAUD_RING,
            "confidence": final_conf,
            "raw_score": raw_score,
            "score_display": score_disp,
            "reasons": observed_reasons,
            "ring_members": linked_users,
            "path_evidence": [],
            "recall": recall_result,
            "sentence": sentence,
            "raw_sentence": sentence,
            "sentence_source": "memory",
            "sentence_rejected_reason": None,
            "model": GEMINI_MODEL,
            "case_id": new_case_id,
            "trace": initial_traces,
            "tool_calls": 2,  # Exactly 2 cheap calls!
            "cache_hits": cache_hits,
            "elapsed_ms": elapsed_ms,
        }
        if memory_note:
            res["memory_note"] = memory_note
        return res

    # Cold investigation: proceed with full loop
    initial_known_ids = {order_id, order_user}
    initial_known_ids.update(linked_users)
    initial_known_ids.update(shared_entities)
    if f_res["data"].get("shipping_address"):
        initial_known_ids.add(f_res["data"]["shipping_address"])

    confirmed_cases = [
        c for c in (recall_result.get("cases", []) if recall_result else [])
        if str(c.get("status", "")).lower() == "confirmed"
    ]
    has_memory = bool(confirmed_cases)

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

    # Fast-path: if no linked accounts and no memory, check detect_ring directly to avoid unnecessary LLM latency
    if not linked_users and not shared_entities and not has_memory:
        dr_res, dr_trace, _ = cached_call("detect_ring", {"user_id": order_user, "max_hops": 12}, tools.detect_ring)
        initial_traces.append(dr_trace)
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
                "tool_calls": executed_tool_calls,
            }
            sentence_source = "fallback"

    # LLM tool calling loop with retry and deterministic fallback
    if pipeline_data is None and use_llm and os.getenv("GEMINI_API_KEY"):
        for attempt in range(3):
            try:
                pipeline_data, raw_sentence, sentence_source = _run_llm_pipeline(
                    order_id, analyst_id, initial_data, cached_call=cached_call
                )
                break
            except Exception:
                if attempt < 2:
                    time.sleep(1.0 * (attempt + 1))
                else:
                    pipeline_data = None

    if pipeline_data is None:
        det_data = _run_deterministic_pipeline(order_id, analyst_id, has_memory=has_memory, cached_call=cached_call)
        pipeline_data = det_data
        sentence_source = "fallback"

    # Compute verdict from results
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
        "recall": recall_result,
        "trace": all_traces,
    }

    # Check sentence with accepts()
    sentence_rejected_reason = None
    if raw_sentence:
        is_valid, reject_reason = check_sentence(raw_sentence, result_summary)
        if not is_valid:
            sentence_rejected_reason = reject_reason
            final_sentence = build_template_sentence(result_summary)
            sentence_source = "fallback"
        else:
            final_sentence = raw_sentence
            sentence_source = "llm"
    else:
        final_sentence = build_template_sentence(result_summary)
        sentence_source = "template"

    # Determine overall outcome
    if has_memory:
        outcome = "answered_from_memory"
    elif v_res["verdict"] == verdict.VERDICT_NO_LINKS_FOUND or not ring_members:
        outcome = "not_linked"
    else:
        outcome = "path_found"

    # Save case to memory
    shape = "mixed"
    if shared_kinds == ["Device"]:
        shape = "shared_device_chain"
    elif shared_kinds == ["Card"]:
        shape = "shared_card_ring"

    memory_note = None
    try:
        case_id = memory.save_case(
            analyst_id=analyst_id,
            order_id=order_id,
            ring_members=([order_user] + ring_members) if ring_members else [order_user],
            shared_entities=list(pipeline_data.get("direct_shared", [])) + shared_kinds,
            verdict=v_res["verdict"],
            confidence=v_res["confidence"],
            shape=shape,
        )
    except memory.MemoryCapExceeded as e:
        case_id = None
        memory_note = str(e)

    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

    has_gemini = bool(os.getenv("GEMINI_API_KEY"))
    res_dict = {
        "outcome": outcome,
        "verdict": v_res["verdict"],
        "confidence": v_res["confidence"],
        "raw_score": v_res.get("raw_score", v_res["confidence"]),
        "score_display": v_res.get("score_display", str(v_res["confidence"])),
        "reasons": v_res["reasons"],
        "ring_members": ring_members,
        "path_evidence": path_evidence,
        "recall": recall_result,
        "sentence": final_sentence,
        "raw_sentence": raw_sentence,
        "sentence_source": sentence_source,
        "sentence_rejected_reason": sentence_rejected_reason,
        "model": GEMINI_MODEL,
        "case_id": case_id,
        "trace": all_traces,
        "tool_calls": executed_tool_calls,
        "cache_hits": cache_hits,
        "elapsed_ms": elapsed_ms,
    }
    if not has_gemini:
        res_dict["note"] = "template mode, no API key"
        res_dict["sentence_source_note"] = "template mode, no API key"
    if memory_note:
        res_dict["memory_note"] = memory_note

    return res_dict


def give_feedback(analyst_id: str, case_id: str, confirmed: bool) -> Dict[str, Any]:
    """Record analyst feedback on a previously saved case."""
    return memory.record_feedback(analyst_id, case_id, confirmed)


def print_cli_summary(res: Dict[str, Any]) -> None:
    """Print formatted investigation result to terminal."""
    print()
    print("=" * 80)
    print("  RINGWATCH INVESTIGATION REPORT")
    print("=" * 80)
    print(f"Model:           {res.get('model', GEMINI_MODEL)}")
    print(f"Outcome:         {res.get('outcome')}")
    print(f"Verdict:         {res.get('verdict')}")
    print(f"Risk score (0-100, rule-based): {res.get('score_display', str(res.get('confidence')) + '%')}")
    print(f"Case ID:         {res.get('case_id')}")
    print(f"Sentence Source: {res.get('sentence_source')}")
    if res.get("sentence_rejected_reason"):
        print(f"Sentence Rejection Reason: {res.get('sentence_rejected_reason')}")
    print(f"Elapsed Time:    {res.get('elapsed_ms')} ms (Tool calls: {res.get('tool_calls')}, Cache hits: {res.get('cache_hits', 0)})")
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
    print(f"{'Tool':<22} {'Args':<32} {'ms':>8} {'Rows':>6} {'Cache':>6}")
    print("-" * 80)
    for t in res.get("trace", []):
        tool_name = str(t.get("tool", ""))
        args_str = str(t.get("args", ""))
        if len(args_str) > 31:
            args_str = args_str[:28] + "..."
        ms = str(t.get("ms", 0))
        rows = str(t.get("rows", 0))
        cache_str = "HIT" if t.get("cache_hit") else "MISS"
        print(f"{tool_name:<22} {args_str:<32} {ms:>8} {rows:>6} {cache_str:>6}")
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
