"""
tools.py - Graph query tool catalogue for RingWatch (fraud-ring investigator).
Connects to FalkorDB graph 'fraud' on localhost:6379.
"""

import time
from typing import Any, Dict, List, Optional
from falkordb import FalkorDB

# Initialize FalkorDB connection and graph
db = FalkorDB(host="localhost", port=6379)
graph = db.select_graph("fraud")

NOT_LINKED_MSG = "No shared device or card was found (this does NOT indicate the account is safe)"


class DataList(list):
    """
    Hybrid list that also supports dictionary-style key access and attributes.
    Allows callers to treat data either as a list or as a structured object/dict.
    """
    def __init__(self, items: list, **kwargs: Any):
        super().__init__(items)
        self._extra = dict(kwargs)
        self.__dict__.update(kwargs)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str):
            return self._extra[key]
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:
        return self._extra.get(key, default)

    def __contains__(self, item: Any) -> bool:
        if isinstance(item, str) and item in self._extra:
            return True
        return super().__contains__(item)


def resolve(kind: str, entity_id: str) -> bool:
    """
    Check if an entity id exists in the graph.
    Supported kinds: 'order', 'user'
    """
    kind_lower = kind.strip().lower()
    if kind_lower == "order":
        q = "MATCH (o:Order {id:$id}) RETURN o.id"
    elif kind_lower == "user":
        q = "MATCH (u:User {id:$id}) RETURN u.id"
    else:
        return False

    res = graph.ro_query(q, {"id": str(entity_id)})
    return bool(res.result_set)


def _make_unknown_entity_response(tool_name: str, args: Dict[str, Any], start_time: float, message: str) -> Dict[str, Any]:
    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)
    return {
        "outcome": "unknown_entity",
        "data": {"message": message},
        "evidence": [],
        "trace": {
            "tool": tool_name,
            "args": args,
            "ms": elapsed_ms,
            "rows": 0,
        },
    }


def order_facts(order_id: str) -> Dict[str, Any]:
    """
    Tool 1: order_facts(order_id)
    Returns: user, amount, flagged, shipping address.
    """
    start_time = time.perf_counter()
    args = {"order_id": order_id}

    if not resolve("order", order_id):
        return _make_unknown_entity_response(
            "order_facts", args, start_time, f"Order '{order_id}' does not exist in graph."
        )

    q = (
        "MATCH (u:User)-[r1:PLACED]->(o:Order {id:$oid})-[r2:SHIPPED_TO]->(a:Address) "
        "RETURN u.id AS user, o.amount AS amount, o.flagged AS flagged, a.id AS shipping_address"
    )
    res = graph.ro_query(q, {"oid": str(order_id)})
    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

    if not res.result_set:
        return {
            "outcome": "not_linked",
            "data": {"message": NOT_LINKED_MSG},
            "evidence": [],
            "trace": {"tool": "order_facts", "args": args, "ms": elapsed_ms, "rows": 0},
        }

    row = res.result_set[0]
    user, amount, flagged, shipping_address = row[0], row[1], bool(row[2]), row[3]

    data = {
        "order_id": order_id,
        "user": user,
        "amount": amount,
        "flagged": flagged,
        "shipping_address": shipping_address,
    }
    evidence = [
        f"{user} PLACED {order_id}",
        f"{order_id} SHIPPED_TO {shipping_address}",
    ]

    return {
        "outcome": "path_found",
        "data": data,
        "evidence": evidence,
        "trace": {"tool": "order_facts", "args": args, "ms": elapsed_ms, "rows": 1},
    }


def find_linked_accounts(order_id: str) -> Dict[str, Any]:
    """
    Tool 2: find_linked_accounts(order_id)
    Returns: linked users with via (Device/Card) and shared_entity.
    """
    start_time = time.perf_counter()
    args = {"order_id": order_id}

    if not resolve("order", order_id):
        return _make_unknown_entity_response(
            "find_linked_accounts", args, start_time, f"Order '{order_id}' does not exist in graph."
        )

    q = (
        "MATCH (o:Order {id:$oid})<-[rp:PLACED]-(u:User)"
        "-[r1:USED_DEVICE|PAID_WITH]->(x)<-[r2:USED_DEVICE|PAID_WITH]-(v:User) "
        "WHERE v <> u "
        "RETURN DISTINCT u.id AS order_user, v.id AS linked_user, "
        "labels(x)[0] AS via, x.id AS shared_entity, type(r1) AS rel1, type(r2) AS rel2"
    )
    res = graph.ro_query(q, {"oid": str(order_id)})
    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

    rows = res.result_set or []
    if not rows:
        return {
            "outcome": "not_linked",
            "data": DataList(
                [],
                order_id=order_id,
                linked_accounts=[],
                linked_users=[],
                shared_entities=[],
                count=0,
                message=NOT_LINKED_MSG,
            ),
            "evidence": [],
            "trace": {"tool": "find_linked_accounts", "args": args, "ms": elapsed_ms, "rows": 0},
        }

    items = []
    evidence_set = []
    order_user = rows[0][0]
    linked_users_set = set()
    shared_entities_set = set()

    for r in rows:
        ou, lu, via, se, rel1, rel2 = r[0], r[1], r[2], r[3], r[4], r[5]
        linked_users_set.add(lu)
        shared_entities_set.add(se)
        items.append({
            "order_user": ou,
            "linked_user": lu,
            "via": via,
            "shared_entity": se,
        })
        e1 = f"{ou} {rel1} {se}"
        e2 = f"{lu} {rel2} {se}"
        if e1 not in evidence_set:
            evidence_set.append(e1)
        if e2 not in evidence_set:
            evidence_set.append(e2)

    data = DataList(
        items,
        order_id=order_id,
        order_user=order_user,
        linked_accounts=items,
        linked_users=sorted(list(linked_users_set)),
        shared_entities=sorted(list(shared_entities_set)),
        count=len(items),
    )

    return {
        "outcome": "path_found",
        "data": data,
        "evidence": evidence_set,
        "trace": {"tool": "find_linked_accounts", "args": args, "ms": elapsed_ms, "rows": len(rows)},
    }


def detect_ring(user_id: str, max_hops: int = 12) -> Dict[str, Any]:
    """
    Tool 3: detect_ring(user_id, max_hops=12)
    Returns: users reachable via USED_DEVICE|PAID_WITH within max_hops edges (reject max_hops > 12),
    excluding the user.
    """
    start_time = time.perf_counter()
    args = {"user_id": user_id, "max_hops": max_hops}

    if max_hops > 12:
        raise ValueError(f"max_hops ({max_hops}) cannot exceed 12")
    if max_hops < 1:
        raise ValueError(f"max_hops ({max_hops}) must be at least 1")

    if not resolve("user", user_id):
        return _make_unknown_entity_response(
            "detect_ring", args, start_time, f"User '{user_id}' does not exist in graph."
        )

    q = (
        "MATCH p = (u:User {id:$uid})-[r:USED_DEVICE|PAID_WITH*1..12]-(v:User) "
        "WHERE v <> u AND length(p) <= $max_hops "
        "RETURN DISTINCT v.id AS user_id"
    )
    res = graph.ro_query(q, {"uid": str(user_id), "max_hops": int(max_hops)})
    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

    members = sorted([row[0] for row in (res.result_set or [])])

    if not members:
        return {
            "outcome": "not_linked",
            "data": DataList([], members=[], count=0, user_id=user_id, message=NOT_LINKED_MSG),
            "evidence": [],
            "trace": {"tool": "detect_ring", "args": args, "ms": elapsed_ms, "rows": 0},
        }

    # Fetch shared evidence edges connecting user and members
    all_uids = [user_id] + members
    q_ev = (
        "MATCH (u:User)-[r:USED_DEVICE|PAID_WITH]->(x)<-[:USED_DEVICE|PAID_WITH]-(v:User) "
        "WHERE u.id IN $uids AND v.id IN $uids AND u <> v "
        "RETURN DISTINCT u.id AS uid, type(r) AS rel, x.id AS xid"
    )
    ev_res = graph.ro_query(q_ev, {"uids": all_uids})
    evidence = [f"{r[0]} {r[1]} {r[2]}" for r in (ev_res.result_set or [])]

    data = DataList(
        members,
        user_id=user_id,
        members=members,
        count=len(members),
        max_hops=max_hops,
    )

    return {
        "outcome": "path_found",
        "data": data,
        "evidence": evidence,
        "trace": {"tool": "detect_ring", "args": args, "ms": elapsed_ms, "rows": len(members)},
    }


def explain_path(user_a: str, user_b: str) -> Dict[str, Any]:
    """
    Tool 4: explain_path(user_a, user_b)
    Returns: hops, ordered path nodes, evidence strings, or not_linked.
    """
    start_time = time.perf_counter()
    args = {"user_a": user_a, "user_b": user_b}

    if not resolve("user", user_a):
        return _make_unknown_entity_response(
            "explain_path", args, start_time, f"User '{user_a}' does not exist in graph."
        )
    if not resolve("user", user_b):
        return _make_unknown_entity_response(
            "explain_path", args, start_time, f"User '{user_b}' does not exist in graph."
        )

    q = (
        "MATCH (a:User {id:$a}),(b:User {id:$b}) "
        "WITH shortestPath((a)-[:USED_DEVICE|PAID_WITH*..15]-(b)) AS p "
        "RETURN p"
    )
    res = graph.ro_query(q, {"a": str(user_a), "b": str(user_b)})
    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

    path_obj = res.result_set[0][0] if res.result_set and len(res.result_set[0]) > 0 else None

    if path_obj is None:
        return {
            "outcome": "not_linked",
            "data": {
                "user_a": user_a,
                "user_b": user_b,
                "hops": None,
                "nodes": [],
                "message": NOT_LINKED_MSG,
            },
            "evidence": [],
            "trace": {"tool": "explain_path", "args": args, "ms": elapsed_ms, "rows": 0},
        }

    nodes = [n.properties.get("id") for n in path_obj.nodes()]
    edges = [e.relation for e in path_obj.edges()]
    hops = len(edges)

    evidence = [f"{nodes[i]} {edges[i]} {nodes[i+1]}" for i in range(len(edges))]

    data = {
        "user_a": user_a,
        "user_b": user_b,
        "hops": hops,
        "nodes": nodes,
    }

    return {
        "outcome": "path_found",
        "data": data,
        "evidence": evidence,
        "trace": {"tool": "explain_path", "args": args, "ms": elapsed_ms, "rows": 1},
    }


def find_rings(min_users: int = 3) -> Dict[str, Any]:
    """
    Tool 5: find_rings(min_users=3)
    Returns: WCC components with at least min_users User nodes.
    """
    start_time = time.perf_counter()
    args = {"min_users": min_users}

    q = (
        "CALL algo.WCC({relationshipTypes:['USED_DEVICE','PAID_WITH']}) "
        "YIELD node, componentId "
        "WHERE 'User' IN labels(node) "
        "WITH componentId, count(node) AS user_count, collect(node.id) AS users "
        "WHERE user_count >= $min_users "
        "RETURN componentId, user_count, users "
        "ORDER BY user_count DESC"
    )
    res = graph.ro_query(q, {"min_users": int(min_users)})
    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

    rows = res.result_set or []
    if not rows:
        return {
            "outcome": "not_linked",
            "data": DataList([], components=[], count=0, message=NOT_LINKED_MSG),
            "evidence": [],
            "trace": {"tool": "find_rings", "args": args, "ms": elapsed_ms, "rows": 0},
        }

    components = []
    evidence = []
    for r in rows:
        comp_id, user_count, users = r[0], r[1], sorted(r[2])
        components.append({
            "component_id": comp_id,
            "user_count": user_count,
            "users": users,
        })
        sample = ", ".join(users[:4]) + ("..." if len(users) > 4 else "")
        evidence.append(f"Component {comp_id}: {user_count} users [{sample}]")

    data = DataList(
        components,
        components=components,
        count=len(components),
    )

    return {
        "outcome": "path_found",
        "data": data,
        "evidence": evidence,
        "trace": {"tool": "find_rings", "args": args, "ms": elapsed_ms, "rows": len(rows)},
    }


def ring_stats(user_ids: List[str]) -> Dict[str, Any]:
    """
    Tool 6: ring_stats(user_ids)
    Returns: order count, flagged count, total amount, and which entity kinds (Device/Card/Address) connect the group.
    """
    start_time = time.perf_counter()
    args = {"user_ids": list(user_ids)}

    # Validate each user ID exists
    for uid in user_ids:
        if not resolve("user", uid):
            return _make_unknown_entity_response(
                "ring_stats", args, start_time, f"User '{uid}' does not exist in graph."
            )

    uids = list(user_ids)

    # 1. Order stats
    q_orders = (
        "MATCH (u:User)-[r:PLACED]->(o:Order) "
        "WHERE u.id IN $uids "
        "RETURN count(o) AS order_count, "
        "sum(CASE WHEN o.flagged THEN 1 ELSE 0 END) AS flagged_count, "
        "sum(o.amount) AS total_amount"
    )
    o_res = graph.ro_query(q_orders, {"uids": uids})
    o_row = o_res.result_set[0] if o_res.result_set else [0, 0, 0]
    order_count = int(o_row[0] or 0)
    flagged_count = int(o_row[1] or 0)
    total_amount = float(o_row[2] or 0.0)

    # 2. Connecting entity kinds
    q_conn = (
        "OPTIONAL MATCH (u1:User)-[r1:USED_DEVICE]->(d:Device)<-[r2:USED_DEVICE]-(v1:User) "
        "WHERE u1.id IN $uids AND v1.id IN $uids AND u1 <> v1 "
        "WITH count(DISTINCT d) AS dev_count "
        "OPTIONAL MATCH (u2:User)-[r3:PAID_WITH]->(c:Card)<-[r4:PAID_WITH]-(v2:User) "
        "WHERE u2.id IN $uids AND v2.id IN $uids AND u2 <> v2 "
        "WITH dev_count, count(DISTINCT c) AS card_count "
        "OPTIONAL MATCH (u3:User)-[r5:PLACED]->(o1:Order)-[r6:SHIPPED_TO]->(a:Address)<-[r7:SHIPPED_TO]-(o2:Order)<-[r8:PLACED]-(v3:User) "
        "WHERE u3.id IN $uids AND v3.id IN $uids AND u3 <> v3 "
        "RETURN dev_count, card_count, count(DISTINCT a) AS addr_count"
    )
    conn_res = graph.ro_query(q_conn, {"uids": uids})
    c_row = conn_res.result_set[0] if conn_res.result_set else [0, 0, 0]
    dev_count = int(c_row[0] or 0)
    card_count = int(c_row[1] or 0)
    addr_count = int(c_row[2] or 0)

    connecting_kinds = []
    if dev_count > 0:
        connecting_kinds.append("Device")
    if card_count > 0:
        connecting_kinds.append("Card")
    if addr_count > 0:
        connecting_kinds.append("Address")

    # Evidence strings for connecting entities
    evidence = []
    if dev_count > 0:
        q_dev = (
            "MATCH (u:User)-[r:USED_DEVICE]->(d:Device)<-[:USED_DEVICE]-(v:User) "
            "WHERE u.id IN $uids AND v.id IN $uids AND u <> v "
            "RETURN DISTINCT u.id AS uid, d.id AS did"
        )
        r_dev = graph.ro_query(q_dev, {"uids": uids})
        for r in (r_dev.result_set or []):
            evidence.append(f"{r[0]} USED_DEVICE {r[1]}")

    if card_count > 0:
        q_card = (
            "MATCH (u:User)-[r:PAID_WITH]->(c:Card)<-[:PAID_WITH]-(v:User) "
            "WHERE u.id IN $uids AND v.id IN $uids AND u <> v "
            "RETURN DISTINCT u.id AS uid, c.id AS cid"
        )
        r_card = graph.ro_query(q_card, {"uids": uids})
        for r in (r_card.result_set or []):
            evidence.append(f"{r[0]} PAID_WITH {r[1]}")

    if addr_count > 0:
        q_addr = (
            "MATCH (u:User)-[:PLACED]->(o:Order)-[:SHIPPED_TO]->(a:Address)<-[:SHIPPED_TO]-(:Order)<-[:PLACED]-(v:User) "
            "WHERE u.id IN $uids AND v.id IN $uids AND u <> v "
            "RETURN DISTINCT u.id AS uid, o.id AS oid, a.id AS aid"
        )
        r_addr = graph.ro_query(q_addr, {"uids": uids})
        for r in (r_addr.result_set or []):
            evidence.append(f"{r[0]} SHIPPED_TO {r[2]} (order {r[1]})")

    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

    data = {
        "user_ids": uids,
        "order_count": order_count,
        "flagged_count": flagged_count,
        "total_amount": total_amount,
        "connecting_entity_kinds": connecting_kinds,
    }

    if not connecting_kinds and order_count == 0:
        data["message"] = NOT_LINKED_MSG
        outcome = "not_linked"
    else:
        outcome = "path_found"

    return {
        "outcome": outcome,
        "data": data,
        "evidence": evidence,
        "trace": {"tool": "ring_stats", "args": args, "ms": elapsed_ms, "rows": len(uids)},
    }
