"""
verify_graph.py  –  Day 1 Cypher verification for the ringwatch fraud graph.
Connects to FalkorDB graph "fraud" and runs 7 PASS/FAIL checks + 1 INFO check.
"""

import db as db_module

g = db_module.get_db("fraud")

results = []   # list of (check_id, description, actual, expected_str, status)

# ── helpers ──────────────────────────────────────────────────────────────────

def query_scalar(cypher, params=None):
    rs = g.query(cypher, params).result_set
    return rs[0][0] if rs else None


def row(check_id, desc, actual, expected_str, status):
    results.append((check_id, desc, actual, expected_str, status))


# ─────────────────────────────────────────────────────────────────────────────
# Check 1 – Total node count
# ─────────────────────────────────────────────────────────────────────────────
actual = query_scalar("MATCH (n) RETURN count(n)")
expected = 1292
status = "PASS" if actual == expected else "FAIL"
row(1, "Total nodes", actual, str(expected), status)

# ─────────────────────────────────────────────────────────────────────────────
# Check 2 – Total edge count  (base 1606, PASS if 1606..1630)
# ─────────────────────────────────────────────────────────────────────────────
actual = query_scalar("MATCH ()-[r]->() RETURN count(r)")
noise  = actual - 1606
status = "PASS" if 1606 <= actual <= 1630 else "FAIL"
row(2, f"Total edges  (noise={noise})", actual, "1606-1630", status)

# ─────────────────────────────────────────────────────────────────────────────
# Check 3 – Ring C expansion from u193
# ─────────────────────────────────────────────────────────────────────────────
actual = query_scalar(
    "MATCH (u:User {id:'u193'})-[:USED_DEVICE|PAID_WITH*1..12]-(v:User) "
    "WHERE v <> u RETURN count(DISTINCT v)"
)
expected = 6
status = "PASS" if actual == expected else "FAIL"
row(3, "Ring C expansion (u193)", actual, str(expected), status)

# ─────────────────────────────────────────────────────────────────────────────
# Check 4 – Ring A expansion from u184
# ─────────────────────────────────────────────────────────────────────────────
actual = query_scalar(
    "MATCH (u:User {id:'u184'})-[:USED_DEVICE|PAID_WITH*1..8]-(v:User) "
    "WHERE v <> u RETURN count(DISTINCT v)"
)
expected = 5
status = "PASS" if actual == expected else "FAIL"
row(4, "Ring A expansion (u184)", actual, str(expected), status)

# ─────────────────────────────────────────────────────────────────────────────
# Check 5 – Ring B shared card cB
# ─────────────────────────────────────────────────────────────────────────────
actual = query_scalar(
    "MATCH (:Card {id:'cB'})<-[:PAID_WITH]-(u:User) RETURN count(u)"
)
expected = 5
status = "PASS" if actual == expected else "FAIL"
row(5, "Ring B shared card (cB)", actual, str(expected), status)

# ─────────────────────────────────────────────────────────────────────────────
# Check 6 – Ring C shortest path length between u193 and u199
#            Attempt canonical form; on syntax error fall back to WITH form.
# ─────────────────────────────────────────────────────────────────────────────
try:
    actual = query_scalar(
        "MATCH (a:User {id:'u193'}), (b:User {id:'u199'}) "
        "MATCH p = shortestPath((a)-[:USED_DEVICE|PAID_WITH*..15]-(b)) "
        "RETURN length(p)"
    )
    sp_form = "shortestPath() inline"
except Exception as e1:
    try:
        actual = query_scalar(
            "MATCH (a:User {id:'u193'}), (b:User {id:'u199'}) "
            "WITH shortestPath((a)-[:USED_DEVICE|PAID_WITH*..15]-(b)) AS p "
            "RETURN length(p)"
        )
        sp_form = "WITH shortestPath() AS p (fallback)"
    except Exception as e2:
        actual = None
        sp_form = f"BOTH FORMS FAILED: {e1} | {e2}"

expected = 12
status = "PASS" if actual == expected else "FAIL"
row(6, f"Ring C shortest path [{sp_form}]", actual, str(expected), status)

# ─────────────────────────────────────────────────────────────────────────────
# Check 7 – Q1 neighbours of order o560's placer
# ─────────────────────────────────────────────────────────────────────────────
q1_cypher = (
    "MATCH (o:Order {id:'o560'})<-[:PLACED]-(u:User)"
    "-[:USED_DEVICE|PAID_WITH]->(x)<-[:USED_DEVICE|PAID_WITH]-(v:User) "
    "WHERE v <> u "
    "RETURN DISTINCT u.id AS order_user, v.id AS linked_user, "
    "labels(x)[0] AS via, x.id AS shared_entity"
)
rs = g.query(q1_cypher).result_set

# also grab flagged value for info (not part of pass/fail)
flagged_rs = g.query("MATCH (o:Order {id:'o560'}) RETURN o.flagged").result_set
flagged_val = flagged_rs[0][0] if flagged_rs else "not found"

expected_rows = {
    ("u186", "u184", "Device", "dA2"),
    ("u186", "u185", "Device", "dA2"),
    ("u186", "u187", "Device", "dA2"),
}
# normalise actual rows (order_user, linked_user, via, shared_entity)
actual_rows = set(tuple(row_r) for row_r in rs)
# also check symmetric: some engines may return (u184, u186, ...) so try both
sym_expected = set()
for (ou, lu, via, se) in expected_rows:
    sym_expected.add((ou, lu, via, se))
    sym_expected.add((lu, ou, via, se))

status = "PASS" if (actual_rows == expected_rows or (actual_rows.issubset(sym_expected) and len(actual_rows) == 3)) else "FAIL"
row(7, "Q1: o560 neighbours (3 rows)", len(actual_rows), "3 rows", status)

# ─────────────────────────────────────────────────────────────────────────────
# Check 8 – INFO: WCC algorithm
# ─────────────────────────────────────────────────────────────────────────────
wcc_note   = ""
wcc_cypher_used = None
# Try documented FalkorDB WCC form
wcc_forms = [
    ("algo.WCC with relationshipTypes",
     "CALL algo.WCC({relationshipTypes:['USED_DEVICE','PAID_WITH']}) "
     "YIELD node, componentId RETURN count(DISTINCT componentId) AS components"),
    ("graph.community.weaklyConnectedComponents",
     "CALL graph.community.weaklyConnectedComponents({relationshipTypes:['USED_DEVICE','PAID_WITH']}) "
     "YIELD node, componentId RETURN count(DISTINCT componentId) AS components"),
    ("algo.weaklyConnectedComponents bare",
     "CALL algo.weaklyConnectedComponents() "
     "YIELD node, componentId RETURN count(DISTINCT componentId) AS components"),
]

for form_name, wcc_cypher in wcc_forms:
    try:
        wcc_rs = g.query(wcc_cypher).result_set
        components = wcc_rs[0][0] if wcc_rs else "?"
        wcc_note = f"OK - {form_name} -> {components} components"
        wcc_cypher_used = wcc_cypher
        break
    except Exception as ex:
        wcc_note = f"ERROR ({form_name}): {ex}"

# ─────────────────────────────────────────────────────────────────────────────
# Print table
# ─────────────────────────────────────────────────────────────────────────────
HDR = f"{'#':<3}  {'Description':<52}  {'Actual':>8}  {'Expected':>12}  {'Status'}"
SEP = "-" * len(HDR)

print()
print("=" * len(HDR))
print("  RINGWATCH  -  Day 1 Verification Report")
print("=" * len(HDR))
print(HDR)
print(SEP)

for (cid, desc, actual, exp_str, st) in results:
    mark = "PASS" if st == "PASS" else "FAIL"
    print(f"{cid:<3}  {desc:<52}  {str(actual):>8}  {exp_str:>12}  [{mark}]")

print(SEP)
print()
print(f"  INFO  o560 flagged value (informational only): {flagged_val}")
print()
print(f"  INFO  Q1 raw rows returned:")
for r_row in rs:
    print(f"        {r_row}")
print()
print(f"  INFO  WCC: {wcc_note}")
if wcc_cypher_used:
    print(f"        Call used: {wcc_cypher_used}")
print()

# Summary
passes = sum(1 for r in results if r[4] == "PASS")
fails  = sum(1 for r in results if r[4] == "FAIL")
print(f"  TOTAL: {passes} PASS, {fails} FAIL out of {len(results)} checks")
print("=" * len(HDR))
print()
