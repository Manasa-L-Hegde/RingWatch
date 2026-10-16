/**
 * static/app.js - RingWatch SPA Client Logic
 * Interacts with FastAPI backend, manages Cytoscape.js graphs, tabs, and analyst memory.
 */

// State
let currentAnalyst = "visitor-" + Math.floor(1000 + Math.random() * 9000);
try {
    const saved = localStorage.getItem("ringwatch_analyst");
    if (saved) {
        currentAnalyst = saved;
    } else {
        localStorage.setItem("ringwatch_analyst", currentAnalyst);
    }
} catch (e) {
    console.warn("localStorage is not accessible:", e);
}

let cyMain = null;
let cyJoin = null;
let exampleOrders = {
    ring_a: "o546",
    ring_c: "o592",
    isolated: "o0",
    unknown: "o99999"
};
let currentInvestigationResult = null;

const NODE_COLORS = {
    User: "#3B82F6",
    Device: "#F59E0B",
    Card: "#10B981",
    Address: "#8B5CF6",
    Order: "#EF4444",
    Default: "#6B7280"
};

const OUTCOME_MEANINGS = {
    path_found: "Network Discovered — Connected fraud ring detected across multiple graph hops.",
    answered_from_memory: "Answered from Memory — Matched confirmed fraud ring in analyst episodic memory.",
    not_linked: "No Links Found — No shared device or card detected (this does NOT indicate the account is safe).",
    unknown_entity: "Unknown Entity — Order does not exist in the database graph."
};

// =============================================================================
// INITIALIZATION
// =============================================================================

document.addEventListener("DOMContentLoaded", () => {
    // Populate Analyst ID
    const analystInput = document.getElementById("analystIdInput");
    if (analystInput) {
        analystInput.value = currentAnalyst;
        analystInput.addEventListener("change", (e) => {
            const val = e.target.value.trim();
            if (val) {
                currentAnalyst = val;
                try { localStorage.setItem("ringwatch_analyst", val); } catch (_) {}
            }
        });
    }

    // Init Cytoscape graphs
    initCytoscape();

    // Fetch health and examples
    fetchHealth();
    fetchExamples();
});

function initCytoscape() {
    const container = document.getElementById("cy");
    if (!container) return;

    cyMain = cytoscape({
        container: container,
        boxSelectionEnabled: false,
        style: [
            {
                selector: "node",
                style: {
                    "background-color": "data(color)",
                    "label": "data(label)",
                    "color": "#ffffff",
                    "font-size": "11px",
                    "font-family": "Helvetica, Arial, sans-serif",
                    "text-valign": "center",
                    "text-halign": "center",
                    "text-wrap": "wrap",
                    "width": "42px",
                    "height": "42px",
                    "border-width": 2,
                    "border-color": "#1e293b"
                }
            },
            {
                selector: "node[kind = 'Device']",
                style: { "color": "#000000" }
            },
            {
                selector: "edge",
                style: {
                    "width": 2,
                    "line-color": "#475569",
                    "target-arrow-color": "#475569",
                    "target-arrow-shape": "triangle",
                    "curve-style": "bezier",
                    "label": "data(rel)",
                    "font-size": "9px",
                    "color": "#94a3b8",
                    "text-background-color": "#0f172a",
                    "text-background-opacity": 0.8,
                    "text-background-padding": "2px"
                }
            },
            {
                selector: "edge[?on_path]",
                style: {
                    "width": 4,
                    "line-color": "#EF4444",
                    "target-arrow-color": "#EF4444",
                    "z-index": 20
                }
            }
        ],
        layout: { name: "cose", padding: 25 }
    });

    cyMain.on("tap", "node", (evt) => {
        const node = evt.target;
        showNodeInspector(node.data("id"), node.data("kind"), node.data("color"));
    });
}

function showNodeInspector(id, kind, color) {
    const card = document.getElementById("nodeInspectorCard");
    const badge = document.getElementById("inspectNodeBadge");
    const idEl = document.getElementById("inspectNodeId");
    const kindEl = document.getElementById("inspectNodeKind");

    if (card && badge && idEl && kindEl) {
        card.classList.remove("hidden");
        badge.style.backgroundColor = color || NODE_COLORS[kind] || NODE_COLORS.Default;
        idEl.textContent = id;
        kindEl.textContent = kind;
    }
}

// =============================================================================
// API CALLS
// =============================================================================

async function fetchHealth() {
    try {
        const res = await fetch("/api/health");
        if (!res.ok) return;
        const data = await res.json();
        const badge = document.getElementById("modelBadge");
        if (badge && data.model) {
            badge.textContent = `Model: ${data.model}`;
        }
    } catch (err) {
        console.warn("Could not fetch health:", err);
    }
}

async function fetchExamples() {
    try {
        const res = await fetch("/api/examples");
        if (!res.ok) return;
        exampleOrders = await res.json();
        const orderInput = document.getElementById("orderIdInput");
        if (orderInput && !orderInput.value) {
            orderInput.value = exampleOrders.ring_a || "o546";
        }
    } catch (err) {
        console.warn("Could not fetch examples:", err);
    }
}

function setAnalyst(id) {
    currentAnalyst = id;
    const input = document.getElementById("analystIdInput");
    if (input) input.value = id;
    try { localStorage.setItem("ringwatch_analyst", id); } catch (_) {}
}

function loadExample(key) {
    const orderInput = document.getElementById("orderIdInput");
    if (orderInput && exampleOrders[key]) {
        orderInput.value = exampleOrders[key];
        triggerInvestigation();
    }
}

// =============================================================================
// INVESTIGATION FLOW
// =============================================================================

async function triggerInvestigation() {
    dismissError();
    const orderInput = document.getElementById("orderIdInput");
    const orderId = orderInput ? orderInput.value.trim() : "";
    const useGemini = document.getElementById("useGeminiToggle")?.checked ?? true;

    if (!orderId) {
        showError("Please enter an Order ID to investigate.");
        return;
    }

    // Validate Analyst ID format
    if (!/^[a-z0-9_-]{1,20}$/.test(currentAnalyst)) {
        showError("Analyst ID must contain only lowercase letters, digits, underscore or hyphen (1-20 characters).");
        return;
    }

    setInvestigateLoading(true);

    try {
        const res = await fetch("/api/investigate", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                analyst: currentAnalyst,
                order_id: orderId,
                use_llm: useGemini
            })
        });

        if (!res.ok) {
            const errData = await res.json().catch(() => ({ detail: "Investigation request failed" }));
            throw new Error(errData.detail || "Server returned an error");
        }

        const data = await res.json();
        currentInvestigationResult = data;
        renderInvestigationResult(data);
    } catch (err) {
        showError(err.message || "Failed to complete investigation.");
    } finally {
        setInvestigateLoading(false);
    }
}

function renderInvestigationResult(data) {
    const resultCard = document.getElementById("resultCard");
    if (!resultCard) return;
    resultCard.classList.remove("hidden");

    // Demo limit warning banner
    const limitBanner = document.getElementById("limitBanner");
    if (limitBanner) {
        if (data.demo_limit_note) {
            limitBanner.classList.remove("hidden");
            limitBanner.textContent = `⚠️ ${data.demo_limit_note}`;
        } else {
            limitBanner.classList.add("hidden");
        }
    }

    // Outcome Badge & Meaning
    const outcome = data.outcome || "unknown_entity";
    const badge = document.getElementById("outcomeBadge");
    const meaning = document.getElementById("outcomeMeaning");
    if (badge) {
        badge.className = `text-xs font-bold px-2.5 py-1 rounded-md uppercase tracking-wider badge-${outcome}`;
        badge.textContent = outcome.replace(/_/g, " ");
    }
    if (meaning) {
        meaning.textContent = OUTCOME_MEANINGS[outcome] || "";
    }

    // Verdict
    const verdictEl = document.getElementById("verdictText");
    if (verdictEl) {
        const v = data.verdict || "NONE";
        verdictEl.textContent = v;
        if (v === "FRAUD_RING") {
            verdictEl.className = "text-lg font-extrabold text-red-500";
        } else if (v === "SUSPICIOUS") {
            verdictEl.className = "text-lg font-extrabold text-amber-500";
        } else {
            verdictEl.className = "text-lg font-extrabold text-emerald-500";
        }
    }

    // Risk Score
    const scoreVal = data.confidence || 0;
    const scoreDisplay = data.score_display || `${scoreVal}`;
    const scoreEl = document.getElementById("riskScoreDisplay");
    const scoreBar = document.getElementById("riskScoreBar");
    if (scoreEl) scoreEl.textContent = scoreDisplay;
    if (scoreBar) {
        scoreBar.style.width = `${Math.min(100, Math.max(0, scoreVal))}%`;
        if (scoreVal >= 70) scoreBar.className = "bg-red-500 h-2.5 rounded-full transition-all duration-300";
        else if (scoreVal >= 40) scoreBar.className = "bg-amber-500 h-2.5 rounded-full transition-all duration-300";
        else scoreBar.className = "bg-emerald-500 h-2.5 rounded-full transition-all duration-300";
    }

    // Explanation Sentence
    const sentenceEl = document.getElementById("sentenceText");
    const sourceTag = document.getElementById("sentenceSourceTag");
    const rejNote = document.getElementById("sentenceRejectedNote");

    if (sentenceEl) sentenceEl.textContent = data.sentence || "";
    if (sourceTag) {
        if (data.sentence_source === "llm") {
            sourceTag.className = "px-2 py-0.5 rounded text-[11px] font-bold bg-blue-600 text-white";
            sourceTag.textContent = "written by Gemini";
        } else {
            sourceTag.className = "px-2 py-0.5 rounded text-[11px] font-bold bg-gray-700 text-gray-200";
            sourceTag.textContent = "template";
        }
    }

    if (rejNote) {
        if (data.sentence_rejected_reason) {
            rejNote.classList.remove("hidden");
            rejNote.textContent = `Note: Model sentence rejected by rule checker: ${data.sentence_rejected_reason}`;
        } else {
            rejNote.classList.add("hidden");
        }
    }

    // Reasons List
    const reasonsList = document.getElementById("reasonsList");
    if (reasonsList) {
        reasonsList.innerHTML = "";
        (data.reasons || []).forEach(r => {
            const li = document.createElement("li");
            li.className = "flex items-start gap-1.5";
            li.innerHTML = `<span class="text-blue-400 font-bold">•</span> <span>${escapeHtml(r)}</span>`;
            reasonsList.appendChild(li);
        });
    }

    // Feedback section visibility
    const fbSection = document.getElementById("feedbackSection");
    if (fbSection) {
        if (data.case_id) {
            fbSection.classList.remove("hidden");
        } else {
            fbSection.classList.add("hidden");
        }
    }

    // Trace Table
    const traceBody = document.getElementById("traceTableBody");
    const traceSummary = document.getElementById("traceSummary");
    if (traceBody) {
        traceBody.innerHTML = "";
        (data.trace || []).forEach(t => {
            const tr = document.createElement("tr");
            const isHit = Boolean(t.cache_hit);
            tr.innerHTML = `
                <td class="p-2 font-medium text-white">${escapeHtml(t.tool || "")}</td>
                <td class="p-2 text-gray-400 max-w-[200px] truncate" title="${escapeHtml(JSON.stringify(t.args || {}))}">${escapeHtml(JSON.stringify(t.args || {}))}</td>
                <td class="p-2 text-right text-gray-300">${t.ms ?? 0}</td>
                <td class="p-2 text-right text-gray-300">${t.rows ?? 0}</td>
                <td class="p-2 text-center">${isHit ? '<span class="text-emerald-400 font-bold">HIT</span>' : '<span class="text-gray-500">MISS</span>'}</td>
            `;
            traceBody.appendChild(tr);
        });
    }
    if (traceSummary) {
        traceSummary.textContent = `${data.tool_calls || 0} tool calls (${data.cache_hits || 0} cache hits) in ${data.elapsed_ms || 0} ms`;
    }

    // Render Cytoscape Graph
    renderCytoscapeGraph(data.graph);
}

function renderCytoscapeGraph(graphData) {
    if (!cyMain || !graphData) return;

    cyMain.elements().remove();

    const elements = [];
    const nodes = graphData.nodes || [];
    const edges = graphData.edges || [];

    nodes.forEach(n => {
        const color = NODE_COLORS[n.kind] || NODE_COLORS.Default;
        elements.push({
            group: "nodes",
            data: {
                id: n.id,
                label: `${n.label || n.id}\n(${n.kind})`,
                kind: n.kind,
                color: color
            }
        });
    });

    edges.forEach((e, idx) => {
        elements.push({
            group: "edges",
            data: {
                id: `e-${idx}-${e.source}-${e.target}`,
                source: e.source,
                target: e.target,
                rel: e.rel || "",
                on_path: Boolean(e.on_path)
            }
        });
    });

    cyMain.add(elements);
    cyMain.layout({
        name: "cose",
        padding: 30,
        animate: false,
        nodeRepulsion: 6000,
        idealEdgeLength: 70
    }).run();
    cyMain.fit();
}

async function sendFeedback(confirmed) {
    if (!currentInvestigationResult || !currentInvestigationResult.case_id) return;
    dismissError();

    const caseId = currentInvestigationResult.case_id;
    try {
        const res = await fetch("/api/feedback", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                analyst: currentAnalyst,
                case_id: caseId,
                confirmed: confirmed
            })
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: "Feedback error" }));
            throw new Error(err.detail);
        }

        const data = await res.json();
        const actionStr = confirmed ? "CONFIRMED as fraud" : "REJECTED (False Positive)";
        showSuccess(`Feedback saved for case ${caseId}: ${actionStr} (pattern: ${data.pattern || 'recorded'}).`);
    } catch (err) {
        showError(err.message || "Failed to record feedback.");
    }
}

// =============================================================================
// TAB 2: ANALYST MEMORY
// =============================================================================

async function fetchAnalystMemory() {
    dismissError();
    const analystLabel = document.getElementById("memoryAnalystDisplay");
    if (analystLabel) analystLabel.textContent = currentAnalyst;

    try {
        const res = await fetch(`/api/memory/${encodeURIComponent(currentAnalyst)}`);
        if (!res.ok) throw new Error("Could not load memory cases");
        const data = await res.json();

        // Render Pattern Counters
        const pContainer = document.getElementById("patternCountersContainer");
        if (pContainer) {
            pContainer.innerHTML = "";
            const patterns = data.patterns || [];
            if (patterns.length === 0) {
                pContainer.innerHTML = '<p class="text-xs text-gray-400 italic col-span-3">No pattern feedback recorded yet for this analyst.</p>';
            } else {
                patterns.forEach(p => {
                    const card = document.createElement("div");
                    card.className = "bg-darkbg border border-bordercolor rounded-lg p-3";
                    card.innerHTML = `
                        <div class="text-xs font-semibold uppercase text-gray-400">${escapeHtml(p.name)}</div>
                        <div class="flex items-center gap-4 mt-2">
                            <span class="text-emerald-400 font-bold text-sm">✓ ${p.confirmed_count} confirmed</span>
                            <span class="text-red-400 font-bold text-sm">✗ ${p.rejected_count} rejected</span>
                        </div>
                    `;
                    pContainer.appendChild(card);
                });
            }
        }

        // Render Cases Table
        const cBody = document.getElementById("memoryCasesTableBody");
        if (cBody) {
            cBody.innerHTML = "";
            const cases = data.cases || [];
            if (cases.length === 0) {
                cBody.innerHTML = '<tr><td colspan="7" class="p-4 text-center text-gray-400 italic">No saved cases in this analyst memory graph.</td></tr>';
            } else {
                cases.forEach(c => {
                    const tr = document.createElement("tr");
                    tr.className = "hover:bg-gray-800/40";
                    tr.innerHTML = `
                        <td class="p-2.5 font-mono text-gray-400">${escapeHtml(c.date || "")}</td>
                        <td class="p-2.5 font-mono font-medium text-white">${escapeHtml(c.case_id || "")}</td>
                        <td class="p-2.5 font-mono text-blue-400">${escapeHtml(c.order_id || "")}</td>
                        <td class="p-2.5 font-semibold ${c.verdict === 'FRAUD_RING' ? 'text-red-400' : 'text-emerald-400'}">${escapeHtml(c.verdict || "")}</td>
                        <td class="p-2.5 text-right font-mono">${c.confidence ?? 0}</td>
                        <td class="p-2.5"><span class="px-2 py-0.5 rounded text-[11px] font-bold ${c.status === 'confirmed' ? 'bg-red-500/20 text-red-300' : 'bg-gray-700 text-gray-300'}">${escapeHtml(c.status || "")}</span></td>
                        <td class="p-2.5 text-gray-300">${escapeHtml(c.pattern || "unclassified")}</td>
                    `;
                    cBody.appendChild(tr);
                });
            }
        }
    } catch (err) {
        showError(err.message || "Failed to retrieve analyst memory.");
    }
}

async function resetAnalystMemory() {
    dismissError();
    try {
        const res = await fetch(`/api/memory/${encodeURIComponent(currentAnalyst)}/reset`, { method: "POST" });
        if (!res.ok) throw new Error("Reset failed");
        showSuccess(`Memory for analyst '${currentAnalyst}' has been reset.`);
        fetchAnalystMemory();
    } catch (err) {
        showError(err.message || "Failed to reset memory.");
    }
}

// =============================================================================
// TAB 3: DETECTED RINGS (WCC)
// =============================================================================

async function fetchDetectedRings() {
    dismissError();
    const container = document.getElementById("detectedRingsContainer");
    if (container) container.innerHTML = '<p class="text-xs text-gray-400 italic col-span-3">Scanning graph for rings via WCC...</p>';

    try {
        const res = await fetch("/api/rings");
        if (!res.ok) throw new Error("Failed to scan rings");
        const data = await res.json();
        const rings = data.rings || [];

        if (container) {
            container.innerHTML = "";
            if (rings.length === 0) {
                container.innerHTML = '<p class="text-xs text-gray-400 italic col-span-3">No fraud rings detected in graph.</p>';
                return;
            }

            rings.forEach(r => {
                const card = document.createElement("div");
                card.className = "bg-darkbg border border-bordercolor rounded-xl p-4 space-y-3";
                card.innerHTML = `
                    <div class="flex items-center justify-between">
                        <span class="font-bold text-sm text-white">Ring #${r.component_id + 1} (${r.size} users)</span>
                        <span class="text-xs font-bold px-2 py-0.5 rounded ${r.verdict === 'FRAUD_RING' ? 'bg-red-500/20 text-red-300' : 'bg-amber-500/20 text-amber-300'}">${escapeHtml(r.verdict || "")}</span>
                    </div>
                    <div class="text-xs text-gray-400 space-y-1">
                        <div>Members: <span class="text-gray-200 font-mono">${escapeHtml(r.members.join(', '))}</span></div>
                        <div>Orders: ${r.order_count} (${r.flagged_count} flagged) &bull; Amount: $${Number(r.total_amount).toLocaleString()}</div>
                        <div>Connecting Kinds: <span class="text-blue-300">${escapeHtml(r.connecting_kinds.join(', ') || 'None')}</span></div>
                        <div>Risk score: <span class="font-bold text-white">${r.score_display}</span></div>
                    </div>
                    <button onclick="visualizeRingComponent('${r.component_id}')" class="w-full mt-2 py-1.5 bg-blue-600/30 hover:bg-blue-600/50 text-blue-300 text-xs font-medium rounded-lg border border-blue-500/30 transition-colors">
                        Draw Ring Subgraph
                    </button>
                `;
                container.appendChild(card);
            });
        }
    } catch (err) {
        showError(err.message || "Failed to scan detected rings.");
    }
}

async function visualizeRingComponent(componentId) {
    dismissError();
    try {
        const res = await fetch(`/api/ring/${encodeURIComponent(componentId)}/graph`);
        if (!res.ok) throw new Error("Could not load ring graph");
        const data = await res.json();
        switchTab("investigate");
        renderCytoscapeGraph(data.graph);
        showSuccess(`Displaying graph for Ring Component #${Number(componentId) + 1}.`);
    } catch (err) {
        showError(err.message || "Failed to visualize ring component.");
    }
}

// =============================================================================
// TAB 4: JOIN VS GRAPH
// =============================================================================

function onHopsSliderChange(val) {
    const label = document.getElementById("hopsValueDisplay");
    const currHopsLabel = document.getElementById("currHopsLabel");
    if (label) label.textContent = `${val} hops`;
    if (currHopsLabel) currHopsLabel.textContent = val;
    fetchJoinVsGraph();
}

async function fetchJoinVsGraph() {
    dismissError();
    const user = document.getElementById("joinUserSelect")?.value.trim() || "u193";
    const maxHops = document.getElementById("maxHopsSlider")?.value || 2;

    try {
        // Fetch 2-hop baseline
        const res2 = await fetch(`/api/join-vs-graph?user=${encodeURIComponent(user)}&max_hops=2`);
        const data2 = await res2.json();

        // Fetch current hops
        const resCurr = await fetch(`/api/join-vs-graph?user=${encodeURIComponent(user)}&max_hops=${maxHops}`);
        const dataCurr = await resCurr.json();

        const joinCount = document.getElementById("joinCountDisplay");
        const joinMembers = document.getElementById("joinMembersDisplay");
        const graphCount = document.getElementById("graphCountDisplay");
        const graphMembers = document.getElementById("graphMembersDisplay");

        if (joinCount) joinCount.textContent = data2.member_count;
        if (joinMembers) joinMembers.textContent = JSON.stringify(data2.members);

        if (graphCount) graphCount.textContent = dataCurr.member_count;
        if (graphMembers) graphMembers.textContent = JSON.stringify(dataCurr.members);

        renderJoinCytoscape(dataCurr.graph);
    } catch (err) {
        showError(err.message || "Failed to compare join vs graph.");
    }
}

function renderJoinCytoscape(graphData) {
    const container = document.getElementById("cyJoin");
    if (!container || !graphData) return;

    if (!cyJoin) {
        cyJoin = cytoscape({
            container: container,
            boxSelectionEnabled: false,
            style: [
                {
                    selector: "node",
                    style: {
                        "background-color": "data(color)",
                        "label": "data(label)",
                        "color": "#ffffff",
                        "font-size": "11px",
                        "text-valign": "center",
                        "text-halign": "center",
                        "width": "40px",
                        "height": "40px",
                        "border-width": 2,
                        "border-color": "#1e293b"
                    }
                },
                {
                    selector: "node[kind = 'Device']",
                    style: { "color": "#000000" }
                },
                {
                    selector: "edge",
                    style: {
                        "width": 2,
                        "line-color": "#475569",
                        "target-arrow-color": "#475569",
                        "target-arrow-shape": "triangle",
                        "curve-style": "bezier",
                        "label": "data(rel)",
                        "font-size": "9px",
                        "color": "#94a3b8"
                    }
                }
            ],
            layout: { name: "cose", padding: 20 }
        });
    }

    cyJoin.elements().remove();
    const elements = [];
    (graphData.nodes || []).forEach(n => {
        elements.push({
            group: "nodes",
            data: {
                id: n.id,
                label: n.id,
                kind: n.kind,
                color: NODE_COLORS[n.kind] || NODE_COLORS.Default
            }
        });
    });
    (graphData.edges || []).forEach((e, idx) => {
        elements.push({
            group: "edges",
            data: {
                id: `ej-${idx}`,
                source: e.source,
                target: e.target,
                rel: e.rel || ""
            }
        });
    });

    cyJoin.add(elements);
    cyJoin.layout({ name: "cose", padding: 20, animate: false }).run();
    cyJoin.fit();
}

// =============================================================================
// TAB NAVIGATION & UTILITIES
// =============================================================================

function switchTab(tabId) {
    const tabs = ["investigate", "memory", "rings", "join", "about"];
    tabs.forEach(t => {
        const pane = document.getElementById(`tab${capitalize(t)}`);
        const btn = document.getElementById(`tabBtn${capitalize(t)}`);
        if (pane) pane.classList.add("hidden");
        if (btn) {
            btn.className = "py-3 border-b-2 border-transparent text-gray-400 hover:text-gray-200 focus:outline-none flex items-center gap-1.5";
        }
    });

    const activePane = document.getElementById(`tab${capitalize(tabId)}`);
    const activeBtn = document.getElementById(`tabBtn${capitalize(tabId)}`);
    if (activePane) activePane.classList.remove("hidden");
    if (activeBtn) {
        activeBtn.className = "py-3 border-b-2 border-blue-500 text-blue-400 font-semibold focus:outline-none flex items-center gap-1.5";
    }

    // Tab-specific fetch on activation
    if (tabId === "memory") fetchAnalystMemory();
    if (tabId === "rings") fetchDetectedRings();
    if (tabId === "join") fetchJoinVsGraph();
    if (tabId === "investigate" && cyMain) {
        setTimeout(() => { cyMain.resize(); cyMain.fit(); }, 50);
    }
}

function setInvestigateLoading(isLoading) {
    const btn = document.getElementById("btnInvestigate");
    const icon = document.getElementById("btnInvestigateIcon");
    const text = document.getElementById("btnInvestigateText");

    if (btn) btn.disabled = isLoading;
    if (isLoading) {
        if (icon) icon.textContent = "⏳";
        if (text) text.textContent = "Investigating...";
        if (btn) btn.classList.add("opacity-70", "cursor-not-allowed");
    } else {
        if (icon) icon.textContent = "🚀";
        if (text) text.textContent = "Investigate Order";
        if (btn) btn.classList.remove("opacity-70", "cursor-not-allowed");
    }
}

function showError(msg) {
    const banner = document.getElementById("errorBanner");
    const el = document.getElementById("errorMessage");
    if (banner && el) {
        el.textContent = msg;
        banner.classList.remove("hidden");
    }
}

function showSuccess(msg) {
    const banner = document.getElementById("errorBanner");
    const el = document.getElementById("errorMessage");
    if (banner && el) {
        el.textContent = msg;
        banner.className = "max-w-7xl mx-auto mt-4 px-6 w-full";
        banner.firstElementChild.className = "bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 px-4 py-3 rounded-lg flex items-center justify-between text-sm";
        banner.classList.remove("hidden");
        setTimeout(dismissError, 4000);
    }
}

function dismissError() {
    const banner = document.getElementById("errorBanner");
    if (banner) banner.classList.add("hidden");
}

function capitalize(s) {
    return s.charAt(0).toUpperCase() + s.slice(1);
}

function escapeHtml(str) {
    if (!str) return "";
    return String(str)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}
