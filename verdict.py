"""
verdict.py - Deterministic scoring and verdict engine for RingWatch.
Evaluates graph findings and analyst memory to classify fraud rings without LLM dependencies.
"""

from typing import Any, Dict, List, Optional, Set, Union
from tools import format_display_date

# =============================================================================
# CONSTANTS & THRESHOLDS
# =============================================================================

# Scoring increments
SCORE_RING_SIZE_3 = 35          # Ring size (users incl. order user) >= 3
SCORE_RING_SIZE_6_MORE = 10     # Additional boost if ring size >= 6
SCORE_DEVICE_SHARED = 10        # Device shared
SCORE_CARD_SHARED = 10          # Payment Card shared
SCORE_ADDRESS_SHARED = 5        # Shipping Address shared
SCORE_FLAGGED_SHARE = 20        # Flagged share of ring orders >= 30%
SCORE_AVG_AMOUNT = 10           # Average order amount >= $3000
SCORE_CONFIRMED_CASE_BOOST = 15 # Overlapping confirmed past case boost (capped)
SCORE_REJECTED_CASE_PENALTY = -25 # Overlapping rejected past case penalty

# Thresholds
THRESHOLD_RING_SIZE_BASE = 3
THRESHOLD_RING_SIZE_LARGE = 6
THRESHOLD_FLAGGED_SHARE = 0.30
THRESHOLD_AVG_AMOUNT = 3000.0
THRESHOLD_FRAUD_RING = 70

SCORE_MAX = 100
SCORE_MIN = 0

# Verdict Strings
VERDICT_FRAUD_RING = "FRAUD_RING"
VERDICT_SUSPICIOUS = "SUSPICIOUS"
VERDICT_NO_LINKS_FOUND = "NO_LINKS_FOUND"

NO_LINKS_REASON = "No linked accounts found (this does not mean the account is safe)"


def verdict(
    ring_stats: Optional[Dict[str, Any]] = None,
    shared_kinds: Optional[Union[List[str], Set[str]]] = None,
    recall_result: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Deterministic verdict scoring engine.
    Computes score and reasons from graph ring stats, shared entity kinds, and memory recall.

    Returns:
        {"verdict": "FRAUD_RING"|"SUSPICIOUS"|"NO_LINKS_FOUND", "confidence": int, "reasons": [...]}
    """
    reasons: List[str] = []

    # Unpack ring_stats if wrapped in data
    stats = ring_stats or {}
    if isinstance(stats, dict) and "data" in stats and isinstance(stats["data"], dict):
        stats = stats["data"]

    # Extract user IDs / ring size
    user_ids = stats.get("user_ids", [])
    if isinstance(user_ids, (list, tuple, set)):
        ring_size = len(user_ids)
    elif "ring_size" in stats:
        ring_size = int(stats["ring_size"])
    elif "count" in stats:
        ring_size = int(stats["count"])
    else:
        ring_size = 0

    # Extract shared entity kinds
    kinds_set = set()
    if shared_kinds:
        for k in shared_kinds:
            kinds_set.add(str(k).strip().capitalize())
    elif "connecting_entity_kinds" in stats:
        for k in stats["connecting_entity_kinds"]:
            kinds_set.add(str(k).strip().capitalize())

    # Check for NO_LINKS_FOUND condition:
    # If no accounts are linked (ring size <= 1 and no shared kinds)
    if ring_size <= 1 and not kinds_set:
        return {
            "verdict": VERDICT_NO_LINKS_FOUND,
            "confidence": 0,
            "reasons": [NO_LINKS_REASON],
        }

    score = 0

    # 1. Ring Size scoring
    if ring_size >= THRESHOLD_RING_SIZE_BASE:
        score += SCORE_RING_SIZE_3
        reasons.append(f"Ring size ({ring_size} users) >= {THRESHOLD_RING_SIZE_BASE} (+{SCORE_RING_SIZE_3})")

    if ring_size >= THRESHOLD_RING_SIZE_LARGE:
        score += SCORE_RING_SIZE_6_MORE
        reasons.append(f"Large ring size ({ring_size} users) >= {THRESHOLD_RING_SIZE_LARGE} (+{SCORE_RING_SIZE_6_MORE})")

    # 2. Shared entity kinds
    if "Device" in kinds_set:
        score += SCORE_DEVICE_SHARED
        reasons.append(f"Shared device detected (+{SCORE_DEVICE_SHARED})")

    if "Card" in kinds_set:
        score += SCORE_CARD_SHARED
        reasons.append(f"Shared payment card detected (+{SCORE_CARD_SHARED})")

    if "Address" in kinds_set:
        # Count a shared Address only when >= 3 ring members use it
        addr_members = int(stats.get("address_member_count", stats.get("address_users_count", 0)))
        if addr_members == 0 and "user_ids" in stats and stats["user_ids"]:
            try:
                import db as db_module
                g = db_module.get_db("fraud")
                q = (
                    "MATCH (u:User)-[:PLACED]->(:Order)-[:SHIPPED_TO]->(a:Address) "
                    "WHERE u.id IN $uids "
                    "WITH a, count(DISTINCT u.id) AS u_cnt "
                    "WHERE u_cnt >= 3 "
                    "RETURN max(u_cnt) AS max_m"
                )
                r = g.ro_query(q, {"uids": list(stats["user_ids"])})
                if r.result_set and r.result_set[0][0]:
                    addr_members = int(r.result_set[0][0])
            except Exception:
                pass
        if addr_members >= 3:
            score += SCORE_ADDRESS_SHARED
            reasons.append(f"Shared shipping address detected (+{SCORE_ADDRESS_SHARED})")

    # 3. Flagged share of the ring's orders
    order_count = int(stats.get("order_count", 0))
    flagged_count = int(stats.get("flagged_count", 0))
    total_amount = float(stats.get("total_amount", 0.0))

    if order_count > 0:
        flagged_share = flagged_count / order_count
        if flagged_share >= THRESHOLD_FLAGGED_SHARE:
            score += SCORE_FLAGGED_SHARE
            reasons.append(
                f"Flagged order share ({flagged_share:.1%}) >= {THRESHOLD_FLAGGED_SHARE:.0%} (+{SCORE_FLAGGED_SHARE})"
            )

    # 4. Average order amount
    if order_count > 0:
        avg_amount = total_amount / order_count
        if avg_amount >= THRESHOLD_AVG_AMOUNT:
            score += SCORE_AVG_AMOUNT
            reasons.append(
                f"Average order amount (${avg_amount:,.2f}) >= ${THRESHOLD_AVG_AMOUNT:,.0f} (+{SCORE_AVG_AMOUNT})"
            )

    # 5. Episodic Memory recall
    cases = []
    if isinstance(recall_result, dict):
        cases = recall_result.get("cases", [])
    elif isinstance(recall_result, list):
        cases = recall_result

    confirmed_cases = [c for c in cases if str(c.get("status", "")).lower() == "confirmed"]
    rejected_cases = [c for c in cases if str(c.get("status", "")).lower() == "rejected"]

    if confirmed_cases:
        score += SCORE_CONFIRMED_CASE_BOOST
        best_c = confirmed_cases[0]
        cid = best_c.get("case_id", "unknown")
        cdate_raw = best_c.get("date", "unknown")
        cdate = format_display_date(cdate_raw) if cdate_raw != "unknown" else "unknown"
        reasons.append(f"Overlapping CONFIRMED past case: {cid} on {cdate} (+{SCORE_CONFIRMED_CASE_BOOST})")

    if rejected_cases:
        score += SCORE_REJECTED_CASE_PENALTY
        best_rej = rejected_cases[0]
        cid = best_rej.get("case_id", "unknown")
        cdate_raw = best_rej.get("date", "unknown")
        cdate = format_display_date(cdate_raw) if cdate_raw != "unknown" else "unknown"
        reasons.append(f"Overlapping REJECTED past case: {cid} on {cdate} ({SCORE_REJECTED_CASE_PENALTY})")

    # Cap score at [SCORE_MIN, SCORE_MAX]
    raw_score = score
    final_score = min(SCORE_MAX, max(SCORE_MIN, raw_score))
    if raw_score > SCORE_MAX:
        score_display = f"{SCORE_MAX} (raw {raw_score}, capped)"
    else:
        score_display = str(final_score)

    # 6. Verdict outcome
    if final_score >= THRESHOLD_FRAUD_RING:
        verdict_str = VERDICT_FRAUD_RING
    else:
        verdict_str = VERDICT_SUSPICIOUS

    return {
        "verdict": verdict_str,
        "confidence": final_score,
        "raw_score": raw_score,
        "score_display": score_display,
        "reasons": reasons,
    }
