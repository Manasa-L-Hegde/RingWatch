"""
app.py - Streamlit UI for RingWatch Fraud-Ring Investigator.
Provides interactive fraud investigation, episodic analyst memory exploration,
WCC detected rings, and join vs graph multi-hop comparisons.
"""

import os
import re
from typing import Any, Dict, List, Optional
import streamlit as st

import db as db_module
import memory
import tools
import verdict
from agent import GEMINI_MODEL, give_feedback, investigate

# =============================================================================
# NON-UI HELPER FUNCTIONS (Imported by test_app_smoke.py)
# =============================================================================

NODE_COLORS = {
    "User": "#3B82F6",     # Blue
    "Device": "#F59E0B",   # Amber / Orange
    "Card": "#10B981",     # Green
    "Address": "#8B5CF6",  # Purple
    "Order": "#EF4444",    # Red
    "Default": "#6B7280",  # Gray
}


def get_entity_kind(node_id: str) -> str:
    """Determine entity kind from its ID prefix."""
    if not node_id or not isinstance(node_id, str):
        return "Default"
    nid = node_id.strip()
    if re.match(r"^u\d+$", nid):
        return "User"
    elif re.match(r"^d[A-Z0-9]\w*$", nid):
        return "Device"
    elif re.match(r"^c[A-Z0-9]\w*$", nid):
        return "Card"
    elif re.match(r"^a[A-Z0-9]\w*$", nid):
        return "Address"
    elif re.match(r"^o\d+$", nid):
        return "Order"
    return "Default"


def build_dot_graph(
    path_evidence: Optional[List[str]] = None,
    order_id: Optional[str] = None,
    ring_members: Optional[List[str]] = None,
) -> str:
    """
    Build a Graphviz DOT string with nodes colored by kind:
    User (#3B82F6), Device (#F59E0B), Card (#10B981), Address (#8B5CF6), Order (#EF4444).
    """
    lines = [
        'digraph G {',
        '  rankdir=LR;',
        '  node [fontname="Helvetica", shape=box, style="filled,rounded", margin="0.2,0.1"];',
        '  edge [fontname="Helvetica", fontsize=10, color="#64748B"];',
    ]

    nodes_seen = set()

    def add_node(nid: str):
        if nid not in nodes_seen:
            nodes_seen.add(nid)
            kind = get_entity_kind(nid)
            bg = NODE_COLORS.get(kind, NODE_COLORS["Default"])
            fg = "white" if kind != "Device" else "black"
            label = f"{nid}\\n({kind})"
            lines.append(f'  "{nid}" [label="{label}", fillcolor="{bg}", fontcolor="{fg}"];')

    if order_id:
        add_node(order_id)

    if ring_members:
        for m in ring_members:
            add_node(m)

    if path_evidence:
        edge_pattern = re.compile(r"(\w+)\s+([A-Za-z_]+)\s+(\w+)")
        for item in path_evidence:
            m = edge_pattern.search(item)
            if m:
                src, rel, tgt = m.group(1), m.group(2), m.group(3)
                add_node(src)
                add_node(tgt)
                lines.append(f'  "{src}" -> "{tgt}" [label="{rel}"];')
            else:
                # Fallback for plain tokens
                parts = [p.strip() for p in item.split("->") if p.strip()]
                for i in range(len(parts) - 1):
                    p1, p2 = parts[i], parts[i + 1]
                    add_node(p1)
                    add_node(p2)
                    lines.append(f'  "{p1}" -> "{p2}";')

    if not nodes_seen:
        lines.append('  "Empty" [label="No graph links found", fillcolor="#9CA3AF", fontcolor="white"];')

    lines.append('}')
    return "\n".join(lines)


def find_example_orders() -> Dict[str, str]:
    """Retrieve sample order IDs from graph for 1-click test buttons."""
    fraud_graph = db_module.get_db("fraud")
    out = {
        "ring_a": "o546",
        "ring_c": "o592",
        "isolated": "o0",
        "unknown": "o99999",
    }
    try:
        qa = (
            "MATCH (u:User)-[:PLACED]->(o:Order) "
            "WHERE u.id IN ['u182','u183','u184','u185','u186','u187'] AND o.flagged = true "
            "RETURN o.id LIMIT 1"
        )
        ra = fraud_graph.ro_query(qa).result_set
        if ra and ra[0][0]:
            out["ring_a"] = ra[0][0]
    except Exception:
        pass

    try:
        qc = (
            "MATCH (u:User)-[:PLACED]->(o:Order) "
            "WHERE u.id IN ['u193','u194','u195','u196','u197','u198','u199'] "
            "RETURN o.id LIMIT 1"
        )
        rc = fraud_graph.ro_query(qc).result_set
        if rc and rc[0][0]:
            out["ring_c"] = rc[0][0]
    except Exception:
        pass

    return out


# =============================================================================
# STREAMLIT UI APPLICATION
# =============================================================================

def render_ui():
    st.set_page_config(
        page_title="RingWatch Fraud-Ring Investigator",
        page_icon="🛡️",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Custom styling
    st.markdown(
        """
        <style>
        .badge-path_found { background-color: #EF4444; color: white; padding: 4px 10px; border-radius: 12px; font-weight: bold; }
        .badge-answered_from_memory { background-color: #8B5CF6; color: white; padding: 4px 10px; border-radius: 12px; font-weight: bold; }
        .badge-not_linked { background-color: #10B981; color: white; padding: 4px 10px; border-radius: 12px; font-weight: bold; }
        .badge-unknown_entity { background-color: #6B7280; color: white; padding: 4px 10px; border-radius: 12px; font-weight: bold; }
        .tag-gemini { background-color: #0284C7; color: white; padding: 2px 8px; border-radius: 8px; font-size: 0.85em; }
        .tag-template { background-color: #64748B; color: white; padding: 2px 8px; border-radius: 8px; font-size: 0.85em; }
        .verdict-FRAUD_RING { color: #DC2626; font-size: 1.4em; font-weight: 800; }
        .verdict-SUSPICIOUS { color: #D97706; font-size: 1.4em; font-weight: 800; }
        .verdict-NO_LINKS_FOUND { color: #059669; font-size: 1.4em; font-weight: 800; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Sidebar
    st.sidebar.title("🛡️ RingWatch")
    st.sidebar.caption("FalkorDB Graph Fraud-Ring Investigator")

    analyst_mode = st.sidebar.selectbox("Analyst", ["a1", "a2", "Custom Analyst"])
    if analyst_mode == "Custom Analyst":
        analyst_id = st.sidebar.text_input("Enter Analyst ID", value="a3").strip()
    else:
        analyst_id = analyst_mode

    use_gemini = st.sidebar.toggle("Use Gemini", value=True)
    st.sidebar.caption(f"Default Model: `{GEMINI_MODEL}`")
    st.sidebar.markdown("---")
    st.sidebar.info(
        "**Episodic Memory Isolation:**\nEach analyst maintains their own private memory graph in FalkorDB."
    )

    # Outcome plain meanings dictionary
    outcome_descriptions = {
        "path_found": "Network Discovered — Connected fraud ring detected across multiple graph hops.",
        "answered_from_memory": "Answered from Memory — Matched confirmed fraud ring in analyst episodic memory.",
        "not_linked": "No Links Found — No shared device or card detected (this does NOT indicate the account is safe).",
        "unknown_entity": "Unknown Entity — Order does not exist in the database graph.",
    }

    # Tabs
    tab1, tab2, tab3, tab4 = st.tabs([
        "🔍 Investigate",
        "🧠 Memory",
        "🕸️ Detected Rings",
        "📊 Join vs Graph",
    ])

    # -------------------------------------------------------------------------
    # TAB 1: Investigate
    # -------------------------------------------------------------------------
    with tab1:
        st.subheader("Order Fraud Investigation")
        examples = find_example_orders()

        col1, col2, col3, col4 = st.columns(4)
        if "order_input" not in st.session_state:
            st.session_state["order_input"] = examples.get("ring_a", "o546")

        if col1.button("🔴 Flagged Ring-A Order", use_container_width=True):
            st.session_state["order_input"] = examples.get("ring_a", "o546")
        if col2.button("🟠 Ring-C Multi-Hop Order", use_container_width=True):
            st.session_state["order_input"] = examples.get("ring_c", "o592")
        if col3.button("🟢 Isolated Normal Order", use_container_width=True):
            st.session_state["order_input"] = examples.get("isolated", "o0")
        if col4.button("⚪ Unknown Order (o99999)", use_container_width=True):
            st.session_state["order_input"] = examples.get("unknown", "o99999")

        c_in, c_btn = st.columns([4, 1])
        order_id = c_in.text_input("Order ID", value=st.session_state["order_input"])
        investigate_clicked = c_btn.button("Investigate", type="primary", use_container_width=True)

        if investigate_clicked or ("current_result" in st.session_state and st.session_state.get("last_order") == order_id):
            if investigate_clicked:
                with st.spinner("Investigating order graph..."):
                    res = investigate(analyst_id=analyst_id, order_id=order_id, use_llm=use_gemini)
                    st.session_state["current_result"] = res
                    st.session_state["last_order"] = order_id
            else:
                res = st.session_state["current_result"]

            # Result Card
            st.markdown("---")
            outcome = res.get("outcome", "unknown_entity")
            badge_class = f"badge-{outcome}"
            desc = outcome_descriptions.get(outcome, "")

            # Fallback note if Gemini was toggled on but fallback was used
            if use_gemini and res.get("sentence_source") == "fallback" and outcome not in ("unknown_entity", "not_linked"):
                st.warning("⚠️ Note: Gemini fell back to the deterministic pipeline for safeguard compliance.")

            r_col1, r_col2 = st.columns([2, 1])
            with r_col1:
                st.markdown(f'<span class="{badge_class}">{outcome.upper()}</span> &nbsp; <em>{desc}</em>', unsafe_allow_html=True)
                v = res.get("verdict") or "NONE"
                st.markdown(f'<div class="verdict-{v}">Verdict: {v}</div>', unsafe_allow_html=True)
                st.markdown(f"**Risk score (0-100, rule-based):** `{res.get('score_display', str(res.get('confidence')))}`")

            with r_col2:
                st.metric("Elapsed Time", f"{res.get('elapsed_ms')} ms")
                st.metric("Tool Calls / Cache Hits", f"{res.get('tool_calls')} calls ({res.get('cache_hits', 0)} hits)")

            st.markdown("### Explanation")
            src_tag = '<span class="tag-gemini">written by Gemini</span>' if res.get("sentence_source") == "llm" else '<span class="tag-template">template</span>'
            st.markdown(f"{src_tag} {res.get('sentence')}", unsafe_allow_html=True)
            if res.get("sentence_rejected_reason"):
                st.caption(f"Sentence Checker Note: {res.get('sentence_rejected_reason')}")

            st.markdown("### Decision Reasons")
            for r in res.get("reasons", []):
                st.markdown(f"- {r}")

            # Graphviz Visualization
            st.markdown("### Path Evidence Graph")
            nodes_for_dot = list(res.get("ring_members", []))
            dot_str = build_dot_graph(
                path_evidence=res.get("path_evidence", []),
                order_id=order_id,
                ring_members=nodes_for_dot,
            )
            st.graphviz_chart(dot_str)

            # Trace Table
            st.markdown("### Execution Trace")
            traces = res.get("trace", [])
            if traces:
                table_rows = []
                for t in traces:
                    table_rows.append({
                        "Tool": t.get("tool"),
                        "Args": str(t.get("args")),
                        "Duration (ms)": t.get("ms"),
                        "Rows": t.get("rows"),
                        "Cache Hit": "HIT" if t.get("cache_hit") else "MISS",
                    })
                st.dataframe(table_rows, use_container_width=True)

            # Feedback Action Buttons
            case_id = res.get("case_id")
            if case_id:
                st.markdown("### Analyst Feedback")
                f_col1, f_col2, _ = st.columns([1, 1, 3])
                if f_col1.button("✅ Confirm Fraud", key="btn_confirm"):
                    fb = give_feedback(analyst_id=analyst_id, case_id=case_id, confirmed=True)
                    st.success(f"Confirmed case {case_id} as fraud! Pattern: {fb.get('pattern')}")
                if f_col2.button("❌ Reject (False Positive)", key="btn_reject"):
                    fb = give_feedback(analyst_id=analyst_id, case_id=case_id, confirmed=False)
                    st.info(f"Recorded feedback as rejected for case {case_id}.")

    # -------------------------------------------------------------------------
    # TAB 2: Memory
    # -------------------------------------------------------------------------
    with tab2:
        st.subheader(f"Episodic Memory for Analyst: `{analyst_id}`")
        mem_graph = db_module.get_db(f"memory_{analyst_id}")

        m_col1, m_col2 = st.columns([4, 1])
        if m_col2.button("🗑️ Reset Analyst Memory", type="secondary"):
            memory.reset_analyst(analyst_id)
            st.success(f"Memory graph for '{analyst_id}' has been reset.")
            st.rerun()

        # Query cases
        try:
            q_cases = (
                "MATCH (c:Case) "
                "OPTIONAL MATCH (c)-[:MATCHES]->(p:Pattern) "
                "RETURN c.id, c.order_id, c.at, c.verdict, c.confidence, c.status, p.name "
                "ORDER BY c.at DESC"
            )
            c_res = mem_graph.query(q_cases).result_set
            if c_res:
                case_table = []
                for row in c_res:
                    case_table.append({
                        "Case ID": row[0],
                        "Order ID": row[1],
                        "Date (UTC)": row[2],
                        "Verdict": row[3],
                        "Risk Score": row[4],
                        "Status": row[5],
                        "Pattern": row[6] or "unclassified",
                    })
                st.dataframe(case_table, use_container_width=True)
            else:
                st.info("No cases currently recorded in this analyst's memory.")

            # Pattern stats
            q_pat = "MATCH (p:Pattern) RETURN p.name, p.confirmed_count, p.rejected_count"
            p_res = mem_graph.query(q_pat).result_set
            if p_res:
                st.markdown("### Pattern Memory Counters")
                pat_table = []
                for row in p_res:
                    pat_table.append({
                        "Pattern Name": row[0],
                        "Confirmed Count": row[1],
                        "Rejected Count": row[2],
                    })
                st.table(pat_table)
        except Exception:
            st.info("No memory graph initialized yet for this analyst.")

    # -------------------------------------------------------------------------
    # TAB 3: Detected Rings (WCC)
    # -------------------------------------------------------------------------
    with tab3:
        st.subheader("Graph-Wide Fraud Ring Detection (WCC)")
        st.caption("Identifies connected components using shared devices and payment cards.")

        if st.button("Scan Graph for Rings (WCC)", type="primary"):
            rings_res = tools.find_rings(min_size=3)
            components = rings_res.get("data", [])
            st.session_state["detected_rings"] = components

        if "detected_rings" in st.session_state:
            components = st.session_state["detected_rings"]
            st.write(f"Found **{len(components)}** fraud ring candidate components:")

            for i, comp in enumerate(components):
                with st.expander(f"Ring #{i+1} — {len(comp)} members", expanded=(i == 0)):
                    c1, c2 = st.columns([3, 2])
                    with c1:
                        st.write("**Members:**", comp)
                        stats = tools.ring_stats(comp)
                        sdata = stats.get("data", {})
                        st.write(f"- Total Orders: {sdata.get('order_count')}")
                        st.write(f"- Flagged Orders: {sdata.get('flagged_count')}")
                        st.write(f"- Total Amount: ${sdata.get('total_amount', 0):,.2f}")
                        st.write(f"- Connecting Kinds: {sdata.get('connecting_entity_kinds')}")

                    with c2:
                        v = verdict.verdict(ring_stats=sdata, shared_kinds=sdata.get('connecting_entity_kinds'))
                        st.markdown(f"**Verdict:** `{v['verdict']}`")
                        st.markdown(f"**Risk score (0-100, rule-based):** `{v['confidence']}`")
                        for r in v["reasons"]:
                            st.caption(f"• {r}")

                    if st.button(f"Draw Ring #{i+1}", key=f"btn_draw_{i}"):
                        dot = build_dot_graph(
                            path_evidence=sdata.get("evidence", []),
                            ring_members=comp,
                        )
                        st.graphviz_chart(dot)

    # -------------------------------------------------------------------------
    # TAB 4: Join vs Graph
    # -------------------------------------------------------------------------
    with tab4:
        st.subheader("Fixed 2-Hop Join vs Variable-Length Traversal")
        st.markdown(
            "Compare what a **fixed 2-hop join** sees vs what a **variable-length graph traversal** sees."
        )

        user_id = st.text_input("User ID", value="u193")
        max_hops = st.slider("Max Traversal Hops", min_value=2, max_value=12, value=2, step=1)

        res_join = tools.detect_ring(user_id, max_hops=2)
        res_current = tools.detect_ring(user_id, max_hops=max_hops)

        m_join = res_join.get("data", {}).get("members", [])
        m_curr = res_current.get("data", {}).get("members", [])

        col_j, col_g = st.columns(2)
        with col_j:
            st.markdown("#### Fixed 2-Hop Join (`max_hops=2`)")
            st.metric("Discovered Members", len(m_join))
            st.write(m_join)

        with col_g:
            st.markdown(f"#### Variable-Length Traversal (`max_hops={max_hops}`)")
            st.metric("Discovered Members", len(m_curr))
            st.write(m_curr)

        st.caption(
            "fixed 2-hop join sees only immediate shared connections; variable-length traversal follows transitive chains."
        )

    # Footer
    st.markdown("---")
    st.markdown(
        "<div style='text-align: center; color: #6B7280; font-size: 0.9em;'>"
        "Synthetic data only. AI assistants used: Claude, Gemini, Antigravity."
        "</div>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    render_ui()
