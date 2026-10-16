import random, json
from falkordb import FalkorDB

random.seed(42)
N_NORMAL, N_ADDR = 182, 100          # normal users u0..u181, ring users u182..u199

users = [{"id": f"u{i}", "name": f"User {i}"} for i in range(200)]
used_device, paid_with, orders = [], [], []

# normal users: own device + own card, ~5% share a second device (realistic noise)
for i in range(N_NORMAL):
    used_device.append({"u": f"u{i}", "d": f"d{i}"})
    paid_with.append({"u": f"u{i}", "c": f"c{i}"})
    if random.random() < 0.05:
        used_device.append({"u": f"u{i}", "d": f"d{random.randrange(N_NORMAL)}"})

# Ring A (u182-u187): two shared devices, bridged by u184
for i in (182, 183, 184): used_device.append({"u": f"u{i}", "d": "dA1"})
for i in (184, 185, 186, 187): used_device.append({"u": f"u{i}", "d": "dA2"})
# Ring B (u188-u192): one shared card + one shared address
for i in range(188, 193):
    used_device.append({"u": f"u{i}", "d": f"d{i}"})
    paid_with.append({"u": f"u{i}", "c": "cB"})
# Ring C (u193-u199): chain u193-dC0-u194-dC1-u195 ... (needs multi-hop)
for k, i in enumerate(range(193, 200)):
    if k > 0: used_device.append({"u": f"u{i}", "d": f"dC{k-1}"})
    if k < 6: used_device.append({"u": f"u{i}", "d": f"dC{k}"})
# ring A and C users still need a card each
for i in list(range(182, 188)) + list(range(193, 200)):
    paid_with.append({"u": f"u{i}", "c": f"c{i}"})

# orders: 546 normal (2% flagged = false positives) + 54 ring orders (~50% flagged)
oid = 0
for _ in range(546):
    orders.append({"id": f"o{oid}", "user": f"u{random.randrange(N_NORMAL)}",
                   "amount": random.randint(20, 2000), "flagged": random.random() < 0.02,
                   "addr": f"a{random.randrange(N_ADDR)}"}); oid += 1
for i in range(182, 200):
    for _ in range(3):
        orders.append({"id": f"o{oid}", "user": f"u{i}", "amount": random.randint(3000, 9000),
                       "flagged": random.random() < 0.5,
                       "addr": "aB" if 188 <= i <= 192 else f"a{random.randrange(N_ADDR)}"}); oid += 1

devices = sorted({r["d"] for r in used_device})
cards = sorted({r["c"] for r in paid_with})
addrs = [f"a{i}" for i in range(N_ADDR)] + ["aB"]

import db; db = db.get_db()
try: db.select_graph("fraud").delete()
except Exception: pass
g = db.select_graph("fraud")

g.query("UNWIND $r AS x CREATE (:User {id:x.id, name:x.name})", {"r": users})
g.query("UNWIND $r AS x CREATE (:Device {id:x})", {"r": devices})
g.query("UNWIND $r AS x CREATE (:Card {id:x})", {"r": cards})
g.query("UNWIND $r AS x CREATE (:Address {id:x})", {"r": addrs})
for label in ["User", "Device", "Card", "Address"]:
    g.query(f"CREATE INDEX FOR (n:{label}) ON (n.id)")
g.query("UNWIND $r AS x MATCH (u:User {id:x.u}),(d:Device {id:x.d}) CREATE (u)-[:USED_DEVICE]->(d)", {"r": used_device})
g.query("UNWIND $r AS x MATCH (u:User {id:x.u}),(c:Card {id:x.c}) CREATE (u)-[:PAID_WITH]->(c)", {"r": paid_with})
g.query("""UNWIND $r AS x MATCH (u:User {id:x.user}),(a:Address {id:x.addr})
           CREATE (u)-[:PLACED]->(:Order {id:x.id, amount:x.amount, flagged:x.flagged})-[:SHIPPED_TO]->(a)""",
        {"r": orders})
g.query("CREATE INDEX FOR (n:Order) ON (n.id)")

json.dump({"A": [f"u{i}" for i in range(182, 188)], "B": [f"u{i}" for i in range(188, 193)],
           "C": [f"u{i}" for i in range(193, 200)]}, open("ground_truth.json", "w"))
print(g.query("MATCH (n) RETURN count(n)").result_set, g.query("MATCH ()-[r]->() RETURN count(r)").result_set)
