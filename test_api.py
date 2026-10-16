"""
test_api.py - Plain assert tests for FastAPI backend using TestClient.
Tests health, examples, investigation, unknown entity, invalid analyst ID (400),
tenant isolation, join-vs-graph multi-hop comparison, and graph payloads.
All tests use use_llm=False.
"""

from fastapi.testclient import TestClient
from api import app

client = TestClient(app)


def test_health_and_examples():
    print("Testing GET /api/health and GET /api/examples...")
    r_health = client.get("/api/health")
    assert r_health.status_code == 200
    h_data = r_health.json()
    assert h_data.get("status") in ("healthy", "degraded")
    assert "model" in h_data
    assert h_data.get("model") is not None

    r_ex = client.get("/api/examples")
    assert r_ex.status_code == 200
    ex_data = r_ex.json()
    assert "ring_a" in ex_data
    assert "ring_c" in ex_data
    assert "isolated" in ex_data
    assert "unknown" in ex_data
    assert ex_data["unknown"] == "o99999"
    print("  [PASS] Health and examples endpoints returned expected schema.")


def test_invalid_analyst_id():
    print("Testing invalid analyst ID rejection (400)...")
    bad_analysts = [
        "invalid!analyst",
        "analyst with spaces",
        "UPPERCASE",
        "toolonganalystnamethatexceedstwenty",
        "",
        "DROP TABLE users;",
    ]
    for bad in bad_analysts:
        resp = client.post("/api/investigate", json={
            "analyst": bad,
            "order_id": "o546",
            "use_llm": False,
        })
        assert resp.status_code == 400, f"Expected 400 for bad analyst '{bad}', got {resp.status_code}"

    # Also test memory endpoint with bad analyst
    resp_mem = client.get("/api/memory/bad!analyst")
    assert resp_mem.status_code == 400
    print("  [PASS] Invalid analyst IDs rejected with 400.")


def test_unknown_order_zero_tools():
    print("Testing unknown order ID safeguard...")
    resp = client.post("/api/investigate", json={
        "analyst": "a1",
        "order_id": "o99999",
        "use_llm": False,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["outcome"] == "unknown_entity"
    assert data["tool_calls"] == 0
    assert data["case_id"] is None
    print("  [PASS] Unknown order o99999 immediately returned unknown_entity with 0 tool calls.")


def test_investigate_ring_a_and_graph_payload():
    print("Testing Ring-A investigation and graph payload...")
    # Find a flagged ring-A order
    ex_res = client.get("/api/examples").json()
    ring_a_order = ex_res["ring_a"]

    resp = client.post("/api/investigate", json={
        "analyst": "test_api_analyst",
        "order_id": ring_a_order,
        "use_llm": False,
    })
    assert resp.status_code == 200
    data = resp.json()

    assert data["verdict"] == "FRAUD_RING"
    assert data["confidence"] >= 70
    assert data["outcome"] == "path_found"
    assert len(data["ring_members"]) >= 4

    # Verify graph payload
    assert "graph" in data
    graph = data["graph"]
    assert "nodes" in graph
    assert "edges" in graph
    assert len(graph["nodes"]) > 0
    assert len(graph["edges"]) > 0

    # Node structure
    sample_node = graph["nodes"][0]
    assert "id" in sample_node
    assert "kind" in sample_node
    assert "label" in sample_node

    # Edge structure
    sample_edge = graph["edges"][0]
    assert "source" in sample_edge
    assert "target" in sample_edge
    assert "rel" in sample_edge
    assert "on_path" in sample_edge

    # Path edges must be flagged
    path_edges = [e for e in graph["edges"] if e.get("on_path")]
    assert len(path_edges) > 0, "Expected at least one edge with on_path=True"

    # Clean up
    client.post("/api/memory/test_api_analyst/reset")
    print("  [PASS] Ring-A order investigated with full graph payload and on_path edges.")


def test_analyst_isolation():
    print("Testing tenant isolation between two analysts...")
    # Reset both analysts
    client.post("/api/memory/a1/reset")
    client.post("/api/memory/a2/reset")

    ex_res = client.get("/api/examples").json()
    order1 = ex_res["ring_a"]
    order2 = "o547"

    # 1. Analyst a1 investigates order1 and confirms it
    res1 = client.post("/api/investigate", json={
        "analyst": "a1",
        "order_id": order1,
        "use_llm": False,
    }).json()
    case1_id = res1["case_id"]
    assert case1_id is not None

    fb_res = client.post("/api/feedback", json={
        "analyst": "a1",
        "case_id": case1_id,
        "confirmed": True,
    })
    assert fb_res.status_code == 200

    # 2. Analyst a1 investigates order2 -> expect answered_from_memory
    res2_a1 = client.post("/api/investigate", json={
        "analyst": "a1",
        "order_id": order2,
        "use_llm": False,
    }).json()
    assert res2_a1["outcome"] == "answered_from_memory", f"Expected answered_from_memory for a1, got {res2_a1['outcome']}"

    # 3. Analyst a2 investigates order2 -> MUST have no memory from a1
    res2_a2 = client.post("/api/investigate", json={
        "analyst": "a2",
        "order_id": order2,
        "use_llm": False,
    }).json()
    assert res2_a2["outcome"] != "answered_from_memory", "Analyst a2 must NOT have memory from a1!"
    assert res2_a2["outcome"] == "path_found"

    # Clean up
    client.post("/api/memory/a1/reset")
    client.post("/api/memory/a2/reset")
    print("  [PASS] Tenant isolation between analysts verified.")


def test_join_vs_graph_counts():
    print("Testing GET /api/join-vs-graph with 2 vs 12 hops on u193...")
    r2 = client.get("/api/join-vs-graph?user=u193&max_hops=2")
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["user"] == "u193"
    assert d2["max_hops"] == 2
    assert d2["member_count"] == 1
    assert d2["members"] == ["u194"]
    assert "graph" in d2
    assert len(d2["graph"]["nodes"]) > 0

    r12 = client.get("/api/join-vs-graph?user=u193&max_hops=12")
    assert r12.status_code == 200
    d12 = r12.json()
    assert d12["user"] == "u193"
    assert d12["max_hops"] == 12
    assert d12["member_count"] == 6
    assert d12["members"] == ["u194", "u195", "u196", "u197", "u198", "u199"]
    assert len(d12["graph"]["nodes"]) > len(d2["graph"]["nodes"])

    # Test invalid hop bounds
    r_bad = client.get("/api/join-vs-graph?user=u193&max_hops=15")
    assert r_bad.status_code == 400
    print("  [PASS] Join-vs-graph counts matched 1 (2-hop) vs 6 (12-hop).")


if __name__ == "__main__":
    print("=" * 70)
    print("RUNNING RINGWATCH FASTAPI TEST SUITE")
    print("=" * 70)
    test_health_and_examples()
    test_invalid_analyst_id()
    test_unknown_order_zero_tools()
    test_investigate_ring_a_and_graph_payload()
    test_analyst_isolation()
    test_join_vs_graph_counts()
    print("=" * 70)
    print("ALL API TESTS PASSED SUCCESSFULLY!")
    print("=" * 70)
