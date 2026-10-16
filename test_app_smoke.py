"""
test_app_smoke.py - Smoke test for RingWatch Streamlit UI non-UI helpers.
Tests build_dot_graph and find_example_orders.
"""

from app import build_dot_graph, find_example_orders, get_entity_kind, NODE_COLORS


def test_entity_kind_and_colors():
    print("Testing get_entity_kind and NODE_COLORS...")
    assert get_entity_kind("u182") == "User"
    assert get_entity_kind("dA1") == "Device"
    assert get_entity_kind("c12") == "Card"
    assert get_entity_kind("a73") == "Address"
    assert get_entity_kind("o546") == "Order"
    assert get_entity_kind("unknown") == "Default"

    for kind in ["User", "Device", "Card", "Address", "Order"]:
        assert kind in NODE_COLORS
    print("  [PASS] Entity kinds and color mapping verified.")


def test_build_dot_graph():
    print("Testing build_dot_graph helper...")
    evidence = [
        "u182 USED_DEVICE dA1",
        "dA1 USED_DEVICE u184",
        "u184 USED_DEVICE dA2",
    ]
    dot = build_dot_graph(
        path_evidence=evidence,
        order_id="o546",
        ring_members=["u182", "u184"],
    )

    assert "digraph G {" in dot
    assert "rankdir=LR;" in dot
    assert '"o546"' in dot
    assert '"u182"' in dot
    assert '"dA1"' in dot
    assert '"u184"' in dot
    assert '"dA2"' in dot
    assert "USED_DEVICE" in dot

    # Test empty fallback
    empty_dot = build_dot_graph()
    assert "digraph G {" in empty_dot
    assert "Empty" in empty_dot
    print("  [PASS] build_dot_graph generates valid colored DOT graphs.")


def test_find_example_orders():
    print("Testing find_example_orders...")
    examples = find_example_orders()
    assert isinstance(examples, dict)
    assert "ring_a" in examples
    assert "ring_c" in examples
    assert "isolated" in examples
    assert "unknown" in examples

    assert examples["unknown"] == "o99999"
    assert examples["isolated"] == "o0"
    assert examples["ring_a"].startswith("o")
    assert examples["ring_c"].startswith("o")
    print(f"  [PASS] find_example_orders returned: {examples}")


if __name__ == "__main__":
    print("=" * 70)
    print("RUNNING APP SMOKE TEST SUITE")
    print("=" * 70)
    test_entity_kind_and_colors()
    test_build_dot_graph()
    test_find_example_orders()
    print("=" * 70)
    print("ALL APP SMOKE TESTS PASSED!")
    print("=" * 70)
