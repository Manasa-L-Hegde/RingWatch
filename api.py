"""
api.py - FastAPI Backend for RingWatch Fraud-Ring Investigator.
Provides RESTful APIs for investigation, graph subgraphs, memory management,
WCC detected rings, and join-vs-graph comparisons.
Never logs or prints secrets.
"""

import collections
import os
import re
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

import agent
import db as db_module
import memory
import tools
import verdict

app = FastAPI(title="RingWatch API", version="1.0.0")

# Enable CORS for local development and SPA
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ANALYST_PATTERN = re.compile(r"^[a-z0-9_-]{1,20}$")

# Rate limiting data structures for PUBLIC_DEMO
PUBLIC_DEMO = os.getenv("PUBLIC_DEMO", "false").lower() in ("true", "1", "yes")
global_llm_requests = collections.deque()  # max 20 per 3600s
ip_llm_requests = collections.defaultdict(collections.deque)  # max 3 per 60s per IP


def validate_analyst_id(analyst: str) -> str:
    """Validate analyst ID against strict regex ^[a-z0-9_-]{1,20}$."""
    if not analyst or not isinstance(analyst, str) or not ANALYST_PATTERN.match(analyst):
        raise HTTPException(
            status_code=400,
            detail="Invalid analyst ID. Must match ^[a-z0-9_-]{1,20}$",
        )
    return analyst


def check_public_demo_rate_limit(client_ip: str) -> Tuple[bool, Optional[str]]:
    """
    Check if LLM investigation limit is reached in PUBLIC_DEMO mode.
    Returns (allowed_llm, fallback_note).
    """
    if not PUBLIC_DEMO:
        return True, None

    now = time.time()

    # Clean old global requests (> 1 hour)
    while global_llm_requests and now - global_llm_requests[0] > 3600:
        global_llm_requests.popleft()

    # Clean old IP requests (> 1 minute)
    ip_queue = ip_llm_requests[client_ip]
    while ip_queue and now - ip_queue[0] > 60:
        ip_queue.popleft()

    if len(global_llm_requests) >= 20 or len(ip_queue) >= 3:
        return False, "demo limit reached, using template mode"

    global_llm_requests.append(now)
    ip_queue.append(now)
    return True, None


# Global exception handler to prevent leaking stack traces
@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred while processing the request."},
    )


# =============================================================================
# API MODELS
# =============================================================================

class InvestigateRequest(BaseModel):
    analyst: str
    order_id: str
    use_llm: bool = True


class FeedbackRequest(BaseModel):
    analyst: str
    case_id: str
    confirmed: bool


# =============================================================================
# ENDPOINTS
# =============================================================================

@app.get("/api/health")
async def health_check():
    """Health check endpoint returning DB connectivity and model metadata."""
    node_count = 0
    db_ok = False
    try:
        g = db_module.get_db("fraud")
        r = g.ro_query("MATCH (n) RETURN count(n) LIMIT 1").result_set
        node_count = r[0][0] if r else 0
        db_ok = node_count > 0
    except Exception:
        node_count = 0
        db_ok = False

    has_gemini = bool(os.getenv("GEMINI_API_KEY"))

    if not db_ok or node_count == 0:
        return {
            "status": "no_data",
            "hint": "Fraud graph is missing or empty. Please run python build_graph.py to populate it.",
            "database": "empty" if db_ok else "disconnected",
            "nodes": 0,
            "model": agent.GEMINI_MODEL,
            "public_demo": PUBLIC_DEMO,
            "has_gemini_key": has_gemini,
        }

    return {
        "status": "ok",
        "database": "connected",
        "nodes": node_count,
        "model": agent.GEMINI_MODEL,
        "public_demo": PUBLIC_DEMO,
        "has_gemini_key": has_gemini,
    }


@app.get("/api/examples")
async def get_examples():
    """Returns order IDs for 4 representative test examples."""
    g = db_module.get_db("fraud")
    examples = {
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
        ra = g.ro_query(qa).result_set
        if ra and ra[0][0]:
            examples["ring_a"] = ra[0][0]

        qc = (
            "MATCH (u:User)-[:PLACED]->(o:Order) "
            "WHERE u.id IN ['u193','u194','u195','u196','u197','u198','u199'] "
            "RETURN o.id LIMIT 1"
        )
        rc = g.ro_query(qc).result_set
        if rc and rc[0][0]:
            examples["ring_c"] = rc[0][0]
    except Exception:
        pass

    return examples


@app.post("/api/investigate")
async def run_investigate(req: InvestigateRequest, request: Request):
    """
    Run fraud investigation for an order.
    Returns agent result plus interactive graph payload {nodes, edges}.
    """
    analyst = validate_analyst_id(req.analyst)

    # Check memory graph cap for new analysts
    if memory.is_memory_cap_reached(analyst):
        return {
            "outcome": "memory_full",
            "message": "demo memory is full, try again later",
            "error": "demo memory is full, try again later",
            "detail": "demo memory is full, try again later",
            "sentence": "demo memory is full, try again later",
            "sentence_source": "template",
            "verdict": "UNKNOWN",
            "confidence": 0,
            "reasons": ["demo memory is full, try again later"],
            "tool_calls": 0,
            "trace": [],
            "graph": {"nodes": [], "edges": []},
            "note": "demo memory is full, try again later",
        }

    # Client IP for rate limiting
    client_ip = request.client.host if request.client else "127.0.0.1"

    has_gemini_key = bool(os.getenv("GEMINI_API_KEY"))
    use_llm = req.use_llm and has_gemini_key
    demo_limit_note = None

    if req.use_llm and not has_gemini_key:
        demo_limit_note = "template mode, no API key"

    if use_llm and PUBLIC_DEMO:
        allowed, note = check_public_demo_rate_limit(client_ip)
        if not allowed:
            use_llm = False
            demo_limit_note = note

    res = agent.investigate(analyst_id=analyst, order_id=req.order_id, use_llm=use_llm)

    # Build interactive subgraph for UI Cytoscape
    order_user = res.get("user")
    ring_members = res.get("ring_members", [])
    user_ids = ([order_user] + ring_members) if order_user else ring_members
    subgraph = tools.ring_subgraph(
        user_ids=user_ids,
        order_id=req.order_id,
        evidence_path=res.get("path_evidence", []),
    )
    res["graph"] = subgraph

    if not has_gemini_key:
        res["note"] = "template mode, no API key"
        res["sentence_source_note"] = "template mode, no API key"

    if demo_limit_note:
        res["demo_limit_note"] = demo_limit_note
        if res.get("sentence_source") == "fallback":
            res["sentence_source"] = "template"

    return res


@app.post("/api/feedback")
async def record_feedback_endpoint(req: FeedbackRequest):
    """Record analyst feedback on an episodic memory case."""
    analyst = validate_analyst_id(req.analyst)
    if memory.is_memory_cap_reached(analyst):
        return {"status": "error", "message": "demo memory is full, try again later"}
    res = agent.give_feedback(analyst_id=analyst, case_id=req.case_id, confirmed=req.confirmed)
    return res


@app.get("/api/memory/{analyst}")
async def get_analyst_memory(analyst: str):
    """Retrieve that analyst's saved cases and pattern counters."""
    analyst = validate_analyst_id(analyst)
    if memory.is_memory_cap_reached(analyst):
        return {
            "analyst": analyst,
            "cases": [],
            "patterns": [],
            "total_cases": 0,
            "message": "demo memory is full, try again later",
        }
    mem_graph = db_module.get_db(f"memory_{analyst}")

    cases = []
    patterns = []
    try:
        q_cases = (
            "MATCH (c:Case) "
            "OPTIONAL MATCH (c)-[:MATCHES]->(p:Pattern) "
            "RETURN c.id, c.order_id, c.at, c.verdict, c.confidence, c.status, p.name "
            "ORDER BY c.at DESC"
        )
        c_res = mem_graph.query(q_cases).result_set
        for row in (c_res or []):
            cases.append({
                "case_id": row[0],
                "order_id": row[1],
                "date": row[2],
                "verdict": row[3],
                "confidence": row[4],
                "status": row[5],
                "pattern": row[6] or "unclassified",
            })

        q_pat = "MATCH (p:Pattern) RETURN p.name, p.confirmed_count, p.rejected_count"
        p_res = mem_graph.query(q_pat).result_set
        for row in (p_res or []):
            patterns.append({
                "name": row[0],
                "confirmed_count": int(row[1] or 0),
                "rejected_count": int(row[2] or 0),
            })
    except Exception:
        pass

    return {
        "analyst": analyst,
        "cases": cases,
        "patterns": patterns,
        "total_cases": len(cases),
    }


@app.post("/api/memory/{analyst}/reset")
async def reset_analyst_memory(analyst: str):
    """Reset the episodic memory graph for an analyst."""
    analyst = validate_analyst_id(analyst)
    memory.reset_analyst(analyst)
    return {"status": "success", "message": f"Memory for analyst '{analyst}' has been reset."}


@app.get("/api/rings")
async def get_detected_rings():
    """Run WCC algorithm to detect fraud ring components and return summary stats."""
    wcc_res = tools.find_rings(min_size=3)
    components = wcc_res.get("data", [])

    rings = []
    for i, comp in enumerate(components):
        stats = tools.ring_stats(comp)
        sdata = stats.get("data", {})
        v = verdict.verdict(
            ring_stats=sdata,
            shared_kinds=sdata.get("connecting_entity_kinds", []),
        )
        rings.append({
            "component_id": i,
            "size": len(comp),
            "members": comp,
            "order_count": sdata.get("order_count", 0),
            "flagged_count": sdata.get("flagged_count", 0),
            "total_amount": sdata.get("total_amount", 0.0),
            "connecting_kinds": sdata.get("connecting_entity_kinds", []),
            "verdict": v.get("verdict"),
            "confidence": v.get("confidence"),
            "score_display": v.get("score_display", str(v.get("confidence"))),
            "reasons": v.get("reasons", []),
        })

    return {"count": len(rings), "rings": rings}


@app.get("/api/ring/{component}/graph")
async def get_ring_graph(component: str):
    """Generate Cytoscape graph payload for a specific detected ring."""
    # Check if component is integer index or user list
    wcc_res = tools.find_rings(min_size=3)
    components = wcc_res.get("data", [])
    members = []
    if component.isdigit():
        idx = int(component)
        if 0 <= idx < len(components):
            members = components[idx]
    if not members:
        # Try finding matching component containing member
        for comp in components:
            if component in comp:
                members = comp
                break

    if not members and components:
        members = components[0]

    graph = tools.ring_subgraph(user_ids=members)
    return {"component": component, "members": members, "graph": graph}


@app.get("/api/join-vs-graph")
async def get_join_vs_graph(user: str = "u193", max_hops: int = 2):
    """
    Compare member discovery between fixed 2-hop join and variable-length traversal.
    max_hops must be between 2 and 12.
    """
    if max_hops < 2 or max_hops > 12:
        raise HTTPException(status_code=400, detail="max_hops must be between 2 and 12")

    if not tools.resolve("user", user):
        raise HTTPException(status_code=404, detail=f"User '{user}' not found in graph")

    res = tools.detect_ring(user, max_hops=max_hops)
    members = res.get("data", {}).get("members", [])
    all_users = [user] + members

    graph = tools.ring_subgraph(user_ids=all_users)

    return {
        "user": user,
        "max_hops": max_hops,
        "member_count": len(members),
        "members": members,
        "graph": graph,
        "caption": "fixed 2-hop join vs variable-length traversal",
    }


# =============================================================================
# STATIC ASSETS & SINGLE PAGE APP
# =============================================================================

# Mount static files directory
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def serve_index():
    """Serve single-page application frontend."""
    index_path = os.path.join("static", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "RingWatch API is running. Frontend static/index.html not found."}
