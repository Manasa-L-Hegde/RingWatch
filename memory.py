"""
memory.py - Analyst episodic memory module for RingWatch.
Manages isolated FalkorDB memory graphs per analyst: f"memory_{analyst_id}".
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Union
import db as db_module


def _get_analyst_graph(analyst_id: str):
    """Return the isolated FalkorDB graph for the analyst."""
    return db_module.get_db(f"memory_{analyst_id}")


def _normalize_entity(e: Any, default_kind: str = "Entity") -> Dict[str, str]:
    """Convert entity representation (dict, tuple, or string) into {'id': ..., 'kind': ...}."""
    if isinstance(e, dict):
        e_id = str(e.get("id", ""))
        e_kind = str(e.get("kind", default_kind))
    elif isinstance(e, (list, tuple)) and len(e) >= 2:
        e_id = str(e[0])
        e_kind = str(e[1])
    else:
        e_id = str(e)
        if e_id.startswith("u"):
            e_kind = "User"
        elif e_id.startswith("d"):
            e_kind = "Device"
        elif e_id.startswith("c"):
            e_kind = "Card"
        elif e_id.startswith("a"):
            e_kind = "Address"
        elif e_id.startswith("o"):
            e_kind = "Order"
        else:
            e_kind = default_kind
    return {"id": e_id, "kind": e_kind}


def save_case(
    analyst_id: str,
    order_id: str,
    ring_members: List[Any],
    shared_entities: List[Any],
    verdict: str,
    confidence: float,
    shape: Optional[str] = None,
) -> str:
    """
    Save a case to the analyst's isolated memory graph.
    Creates (:Case {id (short uuid), at (UTC ISO), order_id, verdict, confidence, status:'pending', shape})
    and MERGEs (:Entity {id, kind}) for every ring member and shared entity,
    linking (Case)-[:INVOLVES]->(Entity).
    Returns case_id.
    """
    g = _get_analyst_graph(analyst_id)
    case_id = uuid.uuid4().hex[:8]
    now_iso = datetime.now(timezone.utc).isoformat()

    # Collect and normalize all entities
    entities_map = {}
    for m in ring_members:
        norm = _normalize_entity(m, default_kind="User")
        if norm["id"]:
            entities_map[norm["id"]] = norm

    for s in shared_entities:
        norm = _normalize_entity(s, default_kind="Entity")
        if norm["id"]:
            entities_map[norm["id"]] = norm

    entities_list = list(entities_map.values())

    q = (
        "CREATE (c:Case {id: $case_id, at: $at, order_id: $order_id, "
        "verdict: $verdict, confidence: $confidence, status: 'pending', shape: $shape}) "
        "WITH c "
        "UNWIND $entities AS e "
        "MERGE (ent:Entity {id: e.id}) "
        "ON CREATE SET ent.kind = e.kind "
        "ON MATCH SET ent.kind = coalesce(ent.kind, e.kind) "
        "CREATE (c)-[:INVOLVES]->(ent)"
    )
    g.query(
        q,
        {
            "case_id": case_id,
            "at": now_iso,
            "order_id": str(order_id),
            "verdict": str(verdict),
            "confidence": float(confidence),
            "shape": str(shape) if shape else "",
            "entities": entities_list,
        },
    )

    return case_id


def record_feedback(
    analyst_id: str,
    case_id: str,
    confirmed: Union[bool, str],
) -> Dict[str, Any]:
    """
    Record feedback for a case:
    - sets status 'confirmed' or 'rejected'
    - updates (:Pattern {name}) confirmed_count/rejected_count linked (Case)-[:MATCHES]->(Pattern).
    Pattern names: 'shared_device_chain', 'shared_card_ring', 'mixed' (from shared entity kinds).
    Unknown case_id returns an error object, not an exception.
    """
    g = _get_analyst_graph(analyst_id)

    # Check graph exists and case exists
    try:
        check_q = (
            "MATCH (c:Case {id: $case_id}) "
            "OPTIONAL MATCH (c)-[:INVOLVES]->(e:Entity) WHERE e.kind <> 'User' "
            "RETURN c.id, c.shape, collect(DISTINCT e.kind) AS kinds"
        )
        res = g.query(check_q, {"case_id": str(case_id)})
        rows = res.result_set or []
    except Exception as e:
        return {"error": f"Case lookup failed: {str(e)}", "case_id": case_id, "status": "error"}

    if not rows or not rows[0][0]:
        return {"error": f"Case '{case_id}' not found in analyst memory", "case_id": case_id, "status": "error"}

    case_row = rows[0]
    case_shape = case_row[1] or ""
    kinds = set(case_row[2] or [])

    # Determine pattern name
    valid_patterns = {"shared_device_chain", "shared_card_ring", "mixed"}
    if case_shape in valid_patterns:
        pattern_name = case_shape
    elif kinds == {"Device"}:
        pattern_name = "shared_device_chain"
    elif kinds == {"Card"}:
        pattern_name = "shared_card_ring"
    elif len(kinds) > 1 or "Address" in kinds or ("Device" in kinds and "Card" in kinds):
        pattern_name = "mixed"
    elif "Device" in kinds:
        pattern_name = "shared_device_chain"
    elif "Card" in kinds:
        pattern_name = "shared_card_ring"
    else:
        pattern_name = "mixed"

    is_confirmed = (confirmed is True) or (str(confirmed).strip().lower() in ("true", "confirmed", "1"))
    status_str = "confirmed" if is_confirmed else "rejected"
    add_c = 1 if is_confirmed else 0
    add_r = 0 if is_confirmed else 1

    # Update case status
    g.query(
        "MATCH (c:Case {id: $case_id}) SET c.status = $status",
        {"case_id": str(case_id), "status": status_str},
    )

    # Merge pattern and increment count
    g.query(
        "MERGE (p:Pattern {name: $pname}) "
        "ON CREATE SET p.confirmed_count = 0, p.rejected_count = 0 "
        "SET p.confirmed_count = p.confirmed_count + $add_c, "
        "    p.rejected_count = p.rejected_count + $add_r",
        {"pname": pattern_name, "add_c": add_c, "add_r": add_r},
    )

    # Link Case to Pattern
    g.query(
        "MATCH (c:Case {id: $case_id}), (p:Pattern {name: $pname}) MERGE (c)-[:MATCHES]->(p)",
        {"case_id": str(case_id), "pname": pattern_name},
    )

    return {
        "status": "success",
        "case_id": case_id,
        "case_status": status_str,
        "pattern": pattern_name,
        "confirmed": is_confirmed,
    }


def recall_similar(analyst_id: str, entity_ids: List[str]) -> Dict[str, Any]:
    """
    Recall past cases sharing entities, with overlap count, verdict, status, date,
    plus pattern stats, best overlap first, max 5.
    Return {"outcome":"answered_from_memory", ...} or {"outcome":"no_memory"}.
    """
    g = _get_analyst_graph(analyst_id)

    if not entity_ids:
        return {"outcome": "no_memory", "cases": []}

    try:
        q = (
            "MATCH (c:Case)-[:INVOLVES]->(e:Entity) "
            "WHERE e.id IN $entity_ids "
            "OPTIONAL MATCH (c)-[:MATCHES]->(p:Pattern) "
            "WITH c, count(DISTINCT e.id) AS overlap_count, collect(DISTINCT e.id) AS overlapping_entities, p "
            "RETURN c.id AS case_id, "
            "       c.order_id AS order_id, "
            "       c.verdict AS verdict, "
            "       c.status AS status, "
            "       c.at AS date, "
            "       c.confidence AS confidence, "
            "       overlap_count, "
            "       overlapping_entities, "
            "       p.name AS pattern_name, "
            "       p.confirmed_count AS pattern_confirmed, "
            "       p.rejected_count AS pattern_rejected "
            "ORDER BY overlap_count DESC "
            "LIMIT 5"
        )
        res = g.query(q, {"entity_ids": [str(x) for x in entity_ids]})
        rows = res.result_set or []
    except Exception:
        # If memory graph doesn't exist yet or is empty
        return {"outcome": "no_memory", "cases": []}

    if not rows:
        return {"outcome": "no_memory", "cases": []}

    cases = []
    for r in rows:
        cases.append({
            "case_id": r[0],
            "order_id": r[1],
            "verdict": r[2],
            "status": r[3],
            "date": r[4],
            "confidence": r[5],
            "overlap_count": int(r[6] or 0),
            "overlapping_entities": r[7] or [],
            "pattern_stats": {
                "name": r[8],
                "confirmed_count": int(r[9] or 0),
                "rejected_count": int(r[10] or 0),
            } if r[8] else None,
        })

    return {
        "outcome": "answered_from_memory",
        "cases": cases,
        "count": len(cases),
    }


def reset_analyst(analyst_id: str) -> None:
    """
    Delete that analyst's memory graph.
    Used by tests and demos for cleanup.
    """
    graph_name = f"memory_{analyst_id}"
    try:
        db_module.get_db(graph_name).delete()
    except Exception:
        pass
