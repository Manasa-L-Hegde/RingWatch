# ringwatch
Fraud-ring investigator agent on FalkorDB that explains verdicts with graph paths and remembers past cases.

---

## AI Disclosure
AI assistants used: Claude, Gemini and Antigravity (disclosed per hackathon rules).

---

## Graph Schema

**Nodes**

| Label     | Properties               |
|-----------|--------------------------|
| User      | id, name                 |
| Device    | id                       |
| Card      | id                       |
| Address   | id                       |
| Order     | id, amount, flagged      |

**Edges**

| Relationship              | Direction               |
|---------------------------|-------------------------|
| (User)-[:PLACED]->        | (Order)                 |
| (Order)-[:SHIPPED_TO]->   | (Address)               |
| (User)-[:USED_DEVICE]->   | (Device)                |
| (User)-[:PAID_WITH]->     | (Card)                  |

---

## Why a Graph

1. Fraud rings share devices, cards and addresses across multiple indirect hops.
2. In SQL, finding them needs rigid self-joins that break on variable-length chains.
3. A graph traversal finds the whole ring and returns the exact evidence path in one query.
