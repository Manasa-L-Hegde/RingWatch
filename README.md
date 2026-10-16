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

---

## Running Locally

### 1. Run FastAPI Backend & UI
```bash
uvicorn api:app --reload
```
The application and interactive Cytoscape.js UI will be available at [http://localhost:8000](http://localhost:8000).

API docs are available at [http://localhost:8000/docs](http://localhost:8000/docs).

### 2. Run Test Suite
```bash
python test_api.py
python test_tools.py
python test_memory_verdict.py
python test_agent.py
python test_app_smoke.py
```

### 3. Run Streamlit UI (Alternative)
```bash
streamlit run app.py
```
