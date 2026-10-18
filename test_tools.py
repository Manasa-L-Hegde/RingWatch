"""
test_tools.py - Plain assert tests for RingWatch tools.py catalogue.
"""

import time
import db as db_module
from tools import (
    order_facts,
    find_linked_accounts,
    detect_ring,
    explain_path,
    find_rings,
    ring_stats,
    resolve,
)

graph = db_module.get_db("fraud")


def test_detect_ring_u193():
    print("Testing detect_ring('u193')...")
    res = detect_ring("u193")
    assert res["outcome"] == "path_found", f"Expected path_found, got {res['outcome']}"
    assert len(res["data"]) == 6, f"Expected 6 members, got {len(res['data'])}"
    assert len(res["data"]["members"]) == 6, f"Expected 6 members in dict access"
    print(f"  [PASS] detect_ring('u193') has 6 members: {res['data']['members']}")

    print("Testing detect_ring('u193', max_hops=2)...")
    res_hops2 = detect_ring("u193", max_hops=2)
    assert res_hops2["outcome"] == "path_found", f"Expected path_found, got {res_hops2['outcome']}"
    assert len(res_hops2["data"]) == 1, f"Expected 1 member, got {len(res_hops2['data'])}"
    assert res_hops2["data"][0] == "u194", f"Expected member u194, got {res_hops2['data'][0]}"
    print(f"  [PASS] detect_ring('u193', max_hops=2) has exactly 1 member: {res_hops2['data'][0]}")


def test_detect_ring_u184_and_explain_path():
    print("Testing detect_ring('u184')...")
    res_a = detect_ring("u184")
    assert res_a["outcome"] == "path_found", f"Expected path_found, got {res_a['outcome']}"
    assert len(res_a["data"]) == 5, f"Expected 5 members, got {len(res_a['data'])}"
    print(f"  [PASS] detect_ring('u184') has 5 members: {res_a['data']['members']}")

    print("Testing explain_path('u193', 'u199')...")
    res_p = explain_path("u193", "u199")
    assert res_p["outcome"] == "path_found", f"Expected path_found, got {res_p['outcome']}"
    assert res_p["data"]["hops"] == 12, f"Expected 12 hops, got {res_p['data']['hops']}"
    assert len(res_p["evidence"]) == 12, f"Expected 12 evidence steps, got {len(res_p['evidence'])}"
    print(f"  [PASS] explain_path('u193', 'u199') has hops == 12")


def test_explain_path_unlinked():
    print("Testing explain_path('u0', 'u199')...")
    res = explain_path("u0", "u199")
    if res["outcome"] == "not_linked":
        assert "not indicate the account is safe" in res["data"]["message"].lower(), (
            "Expected not_linked message to state account is not safe"
        )
        assert res["data"]["hops"] is None, f"Expected None hops for not_linked"
        print(f"  [PASS] explain_path('u0', 'u199') is not_linked as expected.")
    else:
        print(f"  [REPORT] Noise connects 'u0' and 'u199': outcome={res['outcome']}, hops={res['data']['hops']}")


def test_find_linked_accounts_ring_a():
    print("Testing find_linked_accounts for a ring-A order...")
    # Find an order placed by u186 via Cypher
    r = graph.ro_query("MATCH (u:User {id:'u186'})-[:PLACED]->(o:Order) RETURN o.id LIMIT 1")
    assert r.result_set, "No orders found for u186"
    ring_a_order = r.result_set[0][0]
    print(f"  Found order for u186: {ring_a_order}")

    res = find_linked_accounts(ring_a_order)
    assert res["outcome"] == "path_found", f"Expected path_found, got {res['outcome']}"
    linked_users = set(res["data"]["linked_users"])
    assert linked_users == {"u184", "u185", "u187"}, f"Expected dA2 sharers {{u184, u185, u187}}, got {linked_users}"
    assert "dA2" in res["data"]["shared_entities"], f"Expected dA2 in shared_entities"
    print(f"  [PASS] find_linked_accounts({ring_a_order}) lists dA2 sharers: {sorted(list(linked_users))}")


def test_unknown_ids():
    print("Testing unknown ids return unknown_entity...")
    # Unknown order
    res_o = order_facts("o99999")
    assert res_o["outcome"] == "unknown_entity", f"Expected unknown_entity for o99999, got {res_o['outcome']}"
    assert res_o["trace"]["rows"] == 0

    res_lo = find_linked_accounts("o99999")
    assert res_lo["outcome"] == "unknown_entity", f"Expected unknown_entity for o99999, got {res_lo['outcome']}"
    assert res_lo["trace"]["rows"] == 0

    # Unknown user
    res_u = detect_ring("u99999")
    assert res_u["outcome"] == "unknown_entity", f"Expected unknown_entity for u99999, got {res_u['outcome']}"
    assert res_u["trace"]["rows"] == 0

    res_pa = explain_path("u99999", "u193")
    assert res_pa["outcome"] == "unknown_entity", f"Expected unknown_entity for u99999, got {res_pa['outcome']}"

    res_pb = explain_path("u193", "u99999")
    assert res_pb["outcome"] == "unknown_entity", f"Expected unknown_entity for u99999, got {res_pb['outcome']}"

    res_rs = ring_stats(["u99999"])
    assert res_rs["outcome"] == "unknown_entity", f"Expected unknown_entity for u99999, got {res_rs['outcome']}"

    print("  [PASS] All unknown ids correctly return unknown_entity without running additional queries.")


def test_find_rings():
    print("Testing find_rings(3)...")
    res = find_rings(3)
    assert res["outcome"] == "path_found", f"Expected path_found, got {res['outcome']}"

    component_sizes = [c["user_count"] for c in res["data"]]
    print(f"  Component sizes found: {component_sizes}")

    # Check for components of 6, 5 and 7 users
    assert 6 in component_sizes, f"Expected component with 6 users, got sizes {component_sizes}"
    assert 5 in component_sizes, f"Expected component with 5 users, got sizes {component_sizes}"
    assert 7 in component_sizes, f"Expected component with 7 users, got sizes {component_sizes}"

    extras = [s for s in component_sizes if s not in (5, 6, 7)]
    if extras:
        print(f"  [REPORT] Extra components with min_users >= 3: {extras}")
    else:
        print("  [PASS] Exactly components of sizes 5, 6, and 7 returned. No extras.")


def test_order_facts_and_ring_stats():
    print("Testing order_facts and ring_stats...")
    # Test order_facts
    r = graph.ro_query("MATCH (o:Order) RETURN o.id LIMIT 1")
    oid = r.result_set[0][0]
    facts = order_facts(oid)
    assert facts["outcome"] == "path_found"
    assert facts["data"]["user"] is not None
    assert facts["data"]["amount"] > 0
    print(f"  [PASS] order_facts({oid}): user={facts['data']['user']}, amount={facts['data']['amount']}")

    # Test ring_stats on Ring A
    stats_a = ring_stats(["u182", "u183", "u184", "u185", "u186", "u187"])
    assert stats_a["outcome"] == "path_found"
    assert stats_a["data"]["order_count"] == 18
    assert "Device" in stats_a["data"]["connecting_entity_kinds"]
    print(f"  [PASS] ring_stats(Ring A): orders={stats_a['data']['order_count']}, connecting={stats_a['data']['connecting_entity_kinds']}")


def test_shortest_path_methods():
    print("Testing shortest path methods separately ('algo.SPpaths' and 'fallback')...")
    methods = ["algo.SPpaths", "fallback"]
    hops_182 = {}
    timings = {}

    for m in methods:
        t0 = time.perf_counter()

        # 1. Assert hops == 12 and 13 path nodes for u193 -> u199
        res_193 = explain_path("u193", "u199", method=m)
        assert res_193["outcome"] == "path_found", f"[{m}] Expected path_found, got {res_193['outcome']}"
        assert res_193["data"]["hops"] == 12, f"[{m}] Expected 12 hops, got {res_193['data']['hops']}"
        assert len(res_193["data"]["nodes"]) == 13, f"[{m}] Expected 13 nodes, got {len(res_193['data']['nodes'])}"
        assert res_193["trace"]["method"] == m, f"[{m}] Expected trace method {m}, got {res_193['trace']['method']}"

        # 2. Hop count for u182 -> u187
        res_182 = explain_path("u182", "u187", method=m)
        assert res_182["outcome"] == "path_found", f"[{m}] Expected path_found for u182->u187"
        hops_182[m] = res_182["data"]["hops"]
        assert res_182["trace"]["method"] == m

        # 3. not_linked for u0 -> u199
        res_unlinked = explain_path("u0", "u199", method=m)
        assert res_unlinked["outcome"] == "not_linked", f"[{m}] Expected not_linked for u0->u199, got {res_unlinked['outcome']}"
        assert res_unlinked["data"]["hops"] is None, f"[{m}] Expected hops None for not_linked"
        assert res_unlinked["trace"]["method"] == m

        # 4. unknown_entity for u99999
        res_unknown_a = explain_path("u99999", "u193", method=m)
        assert res_unknown_a["outcome"] == "unknown_entity", f"[{m}] Expected unknown_entity for u99999"

        res_unknown_b = explain_path("u193", "u99999", method=m)
        assert res_unknown_b["outcome"] == "unknown_entity", f"[{m}] Expected unknown_entity for u99999"

        elapsed_ms = (time.perf_counter() - t0) * 1000
        timings[m] = elapsed_ms
        print(f"  [PASS] Method '{m}': u193->u199 (hops=12, nodes=13), u182->u187 (hops={hops_182[m]}), u0->u199 (not_linked), u99999 (unknown_entity)")

    # Assert identical hop count for u182 -> u187
    assert hops_182["algo.SPpaths"] == hops_182["fallback"], (
        f"Hop counts differ for u182->u187: algo.SPpaths={hops_182['algo.SPpaths']}, fallback={hops_182['fallback']}"
    )
    print(f"  [PASS] Identical hop count ({hops_182['algo.SPpaths']}) for u182 -> u187 across both methods.")

    # Timing report
    print("  Timing Report:")
    for m in methods:
        print(f"    - {m:<15}: {timings[m]:.2f} ms")


if __name__ == "__main__":
    print("=" * 70)
    print("Running RingWatch Tools Test Suite")
    print("=" * 70)
    test_detect_ring_u193()
    test_detect_ring_u184_and_explain_path()
    test_explain_path_unlinked()
    test_find_linked_accounts_ring_a()
    test_unknown_ids()
    test_find_rings()
    test_order_facts_and_ring_stats()
    test_shortest_path_methods()
    print("=" * 70)
    print("ALL TESTS PASSED SUCCESSFULLY!")
    print("=" * 70)
