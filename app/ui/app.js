// AgentOffice chat island: a floating pill that opens into a chat panel (Ctrl+Shift+Space / Ctrl+K).
// Talks to the API in run.py: /config, /samples, /uploads, /chat, /jobs/{id}, /tasks/{id}/decision.
"use strict";

const $ = (id) => document.getElementById(id);
const island = $("island"), pill = $("pill"), pillText = $("pill-text"), pillBadge = $("pill-badge");
const messages = $("messages"), input = $("input"), form = $("composer"), fileInput = $("file");
const attachmentEl = $("attachment"), drop = $("drop"), sendBtn = $("send");

const desktop = new URLSearchParams(location.search).get("shell") === "desktop";
const state = { expanded: false, run: "idle", config: null, samples: [], attachment: null, busy: false,
                pickingFile: false, quitting: false };

const AGENTS = {
  Orchestrator: "Orchestrator", FrontDeskAgent: "Front desk", DataProfilerAgent: "Data profiler", PMAgent: "PM",
  SpecCritic: "Spec critic", RetrieverAgent: "Retriever", EngineerAgent: "Engineer", QAAgent: "QA",
  ManagerAgent: "Manager", MemoryKeeperAgent: "Memory keeper", OperatorAgent: "Operator",
  SourceMapperAgent: "Source mapper", ReconcilerAgent: "Reconciler", ConflictCriticAgent: "Conflict critic",
  ResearchPlannerAgent: "Research planner", ReportWriterAgent: "Report writer", InsightCriticAgent: "Insight critic",
  PriorityCritic: "Priority critic",
};
const ACTIONS = {
  run_started: "Started", route: "Read your request", plan_run: "Planned the run", profile_file: "Profiled your file",
  data_query: "Queried the data", hmem_retrieve: "Recalled memory", plan_task: "Wrote the plan",
  revise_plan: "Revised the plan", critique_spec: "Checked the plan against your data", read_sheet: "Read a sheet",
  handoff: "Handed off", write_code: "Wrote the automation", revise_code: "Fixed the code",
  design_tests: "Designed tests", critique_output: "Tested the code", review_code: "Reviewed the code",
  decide: "Wrote the decision", request_approval: "Asked for your approval", approval_on_record: "Found your earlier approval",
  world_model_write: "Updated the World Model", mine_memories: "Drew lessons", hmem_write: "Saved to memory",
  run_automation: "Ran it on your file", run_completed: "Finished", run_failed: "Failed",
  decision_rule_override: "Applied the rule gate", plan_task_rejected: "Plan rejected, retrying",
  propose_plan: "Ranked what to automate", critique_priority: "Checked the savings claims",
  memory_applied: "Applied a past decision", map_sources: "Mapped the sources", reconcile: "Linked the sources",
  review_conflicts: "Checked for conflicts", plan_research: "Planned the research", write_report: "Wrote the report",
  critique_report: "Checked the report",
};
const HIDDEN = new Set(["zetaris_query"]);

// ------------------------------------------------------------------ helpers

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function el(tag, cls, html) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html != null) n.innerHTML = html;
  return n;
}
function store(key, value) {
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch (_) { return null; }
  return null;
}
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch (_) { body = null; }
  if (!res.ok || (body && body.status === "error")) {
    const err = body && body.error ? body.error : { type: "http_" + res.status, message: res.statusText };
    throw Object.assign(new Error(err.message), { info: err });
  }
  return body;
}
function scrollDown() { messages.scrollTop = messages.scrollHeight; }
function fmt(v) {
  if (v === null || v === undefined) return "";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toLocaleString(undefined, { maximumFractionDigits: 4 });
  return String(v);
}
function agentName(a) { return AGENTS[a] || a.replace(/Agent$/, ""); }

// ------------------------------------------------------------------ island state

function render() {
  island.dataset.state = state.expanded ? "expanded" : state.run;
  pill.setAttribute("aria-expanded", String(state.expanded));
  // Desktop shell: scripts/island.py reads the state and the browser's own title-bar height from the title.
  const mode = state.quitting ? "quit" : state.expanded ? "panel" : "island";
  document.title = desktop
    ? `AgentOffice · ${mode} · ${Math.max(0, window.outerHeight - window.innerHeight)}`
    : "AgentOffice";
}
function setRun(run, text, badge) {
  state.run = run;
  pillText.textContent = text || "AgentOffice";
  pillBadge.hidden = !badge;
  pillBadge.textContent = badge || "";
  render();
}
function expand() {
  if (state.expanded) return;
  state.expanded = true;
  render();
  setTimeout(() => input.focus(), 180);
}
function collapse() {
  if (!state.expanded) return;
  state.expanded = false;
  render();
  if (!desktop) pill.focus();
}
function toggle() { state.expanded ? collapse() : expand(); }

pill.addEventListener("click", expand);
$("collapse").addEventListener("click", collapse);
document.addEventListener("keydown", (e) => {
  const k = e.key.toLowerCase();
  if ((e.ctrlKey && e.shiftKey && (k === " " || e.code === "Space")) || (e.ctrlKey && !e.shiftKey && k === "k")) {
    e.preventDefault();
    toggle();
  } else if (k === "escape" && state.expanded) {
    collapse();
  }
});
if (desktop) {
  document.body.classList.add("shell-desktop");
  window.addEventListener("resize", render);
  // scripts/island.py focuses the window on the global hotkey; focus opens, losing focus folds it back.
  window.addEventListener("focus", () => { state.pickingFile = false; expand(); });
  window.addEventListener("blur", () => { if (!state.pickingFile) collapse(); });
  // the desktop app closes itself (window and background service) when the page asks to quit
  $("quit").hidden = false;
  $("quit").addEventListener("click", () => {
    if (state.busy && !confirm("The team is still working. Quit anyway?")) return;
    state.quitting = true;
    render();
  });
}

// ------------------------------------------------------------------ attachments

function setAttachment(meta) {
  state.attachment = meta;
  attachmentEl.innerHTML = "";
  attachmentEl.hidden = !meta;
  if (!meta) return;
  const rows = meta.sheets.map((s) => `${s.name}: ${s.rows} rows`).join(" · ");
  const chip = el("div", "file-chip", `<span aria-hidden="true">📄</span><span class="name">${esc(meta.filename)}</span>` +
    `<span class="meta">${esc(rows)}</span><button type="button" aria-label="Remove attachment">✕</button>`);
  chip.querySelector("button").addEventListener("click", () => setAttachment(null));
  attachmentEl.appendChild(chip);
}
async function uploadFile(file) {
  if (!file) return;
  if (!/\.(csv|xlsx|xlsm)$/i.test(file.name)) { bot(`I can read .csv and .xlsx files; ${esc(file.name)} is neither.`, "error"); return; }
  attachmentEl.hidden = false;
  attachmentEl.innerHTML = `<div class="file-chip"><span class="name">Uploading ${esc(file.name)}…</span></div>`;
  try {
    const meta = await api(`/uploads?filename=${encodeURIComponent(file.name)}`, { method: "POST", body: file });
    setAttachment(meta);
    input.focus();
  } catch (e) {
    setAttachment(null);
    bot(`Upload failed: ${esc(e.message)}`, "error");
  }
}
$("attach").addEventListener("click", () => { state.pickingFile = true; fileInput.click(); });
fileInput.addEventListener("change", () => { state.pickingFile = false; uploadFile(fileInput.files[0]); fileInput.value = ""; });
let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  if (![...(e.dataTransfer?.types || [])].includes("Files")) return;
  dragDepth++; expand(); drop.hidden = false;
});
window.addEventListener("dragleave", () => { dragDepth = Math.max(0, dragDepth - 1); if (!dragDepth) drop.hidden = true; });
window.addEventListener("dragover", (e) => e.preventDefault());
window.addEventListener("drop", (e) => {
  e.preventDefault(); dragDepth = 0; drop.hidden = true;
  uploadFile(e.dataTransfer?.files?.[0]);
});

// ------------------------------------------------------------------ messages

function bot(html, extra) {
  const m = el("div", "msg bot" + (extra ? " " + extra : ""), html);
  messages.appendChild(m);
  scrollDown();
  return m;
}
function user(text, meta) {
  const m = el("div", "msg user");
  m.appendChild(document.createTextNode(text));
  if (meta) m.appendChild(el("div", "file-chip", `<span aria-hidden="true">📄</span><span class="name">${esc(meta.filename)}</span>`));
  messages.appendChild(m);
  scrollDown();
}
function welcome() {
  const w = bot(`<div class="welcome"><h2>What should the team do?</h2><p>Attach a spreadsheet and describe the
    repetitive work: filling columns, tidying text, fixing dates, flagging rows, removing duplicates, totals.
    Or ask where your team's time goes.</p><div class="samples"></div></div>`);
  const box = w.querySelector(".samples");
  for (const s of state.samples) {
    const b = el("button", "sample-card", `<b>Try: ${esc(s.filename)}</b><span>${esc(s.suggested_prompt || "")}</span>`);
    b.type = "button";
    b.addEventListener("click", () => { setAttachment(s); input.value = s.suggested_prompt || ""; autosize(); input.focus(); });
    box.appendChild(b);
  }
  const q = el("button", "sample-card", `<b>Ask: where is the finance team losing time?</b><span>Runs the full analysis on the workforce data in Zetaris.</span>`);
  q.type = "button";
  q.addEventListener("click", () => { setAttachment(null); input.value = "Where is the finance team losing time, and what should we automate first?"; autosize(); input.focus(); });
  box.appendChild(q);
}

// ------------------------------------------------------------------ sending + live runs

function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 140) + "px"; }
input.addEventListener("input", autosize);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || state.busy) return;
  const attachment = state.attachment;
  user(text, attachment);
  input.value = ""; autosize(); setAttachment(null);
  state.busy = true; sendBtn.disabled = true;
  setRun("busy", "Front desk · reading your request", "working");
  try {
    const body = { message: text, upload_id: attachment ? attachment.upload_id : null,
                   options: { sample_mode: $("sample-toggle").checked } };
    const job = await api("/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    await follow(job.job_id);
  } catch (err) {
    bot(`Something went wrong: ${esc(err.message)}`, "error");
    setRun("error", "Something went wrong", "error");
  } finally {
    state.busy = false; sendBtn.disabled = false;
  }
});

function runCard() {
  const card = el("div", "card msg", `<h3>Team at work</h3><ol class="timeline"></ol>`);
  messages.appendChild(card);
  return { card, list: card.querySelector(".timeline"), steps: [] };
}
function stepClass(ev) {
  if (["error", "needs_revision", "truncated"].includes(ev.status)) return ev.status === "error" ? "bad" : "warn";
  if (ev.status === "retry" || ev.status === "escalated" || ev.status === "overridden") return "warn";
  return "ok";
}
function stepDetail(ev) {
  let s = ev.summary || "";
  try { const j = JSON.parse(s); if (typeof j === "object") s = Array.isArray(j) ? j.join(" · ") : Object.entries(j).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`).join(" · "); } catch (_) { /* plain text */ }
  if (ev.action === "data_query") s = `${s}${ev.target === "Zetaris" ? " from Zetaris" : " from sample data"}`;
  return s;
}
function addStep(run, ev) {
  if (HIDDEN.has(ev.action) || ev.action === "run_started") return;
  run.list.querySelectorAll(".step.live").forEach((n) => n.classList.remove("live"));
  const who = agentName(ev.agent);
  const what = ACTIONS[ev.action] || ev.action.replace(/_/g, " ");
  const li = el("li", `step ${stepClass(ev)} live`, `<span class="tick" aria-hidden="true">${stepClass(ev) === "ok" ? "✓" : "!"}</span>` +
    `<span><span class="who">${esc(who)}</span> <span class="what">${esc(what)}</span>` +
    `<span class="detail" title="${esc(stepDetail(ev))}">${esc(stepDetail(ev))}</span></span>`);
  run.list.appendChild(li);
  run.steps.push(li);
  if (run.steps.length > 9) run.steps[run.steps.length - 10].hidden = true;
  setRun("busy", `${who} · ${what.toLowerCase()}`, ev.model && ev.model !== "offline-fallback" ? "live" : "working");
  scrollDown();
}
function finishCard(run) {
  run.list.querySelectorAll(".step.live").forEach((n) => n.classList.remove("live"));
  const hidden = run.steps.filter((s) => s.hidden).length;
  if (hidden) {
    const more = el("button", "more", `Show all ${run.steps.length} steps`);
    more.type = "button";
    more.addEventListener("click", () => { run.steps.forEach((s) => (s.hidden = false)); more.remove(); });
    run.card.appendChild(more);
  }
}

async function follow(jobId) {
  const run = runCard();
  let after = 0;
  for (;;) {
    const view = await api(`/jobs/${jobId}?after=${after}`);
    view.events.forEach((ev) => addStep(run, ev));
    after = view.next;
    if (view.status !== "running") {
      finishCard(run);
      if (view.status === "failed") {
        bot(`<p><b>The run stopped.</b> ${esc(view.error?.message)}</p><p class="muted">Error type: ${esc(view.error?.type)}. Trace: logs/trace-${esc(view.trace_id)}.jsonl</p>`, "error");
        setRun("error", "Run stopped", "error");
      } else {
        showResult(view.result, view.trace_id);
      }
      return;
    }
    await new Promise((r) => setTimeout(r, 700));
  }
}

function showResult(result, traceId) {
  if (result.type === "reply") {
    bot(`<p>${esc(result.text)}</p>`);
    setRun("idle", "AgentOffice");
  } else if (result.type === "task") {
    taskCard(result.task, traceId);
  } else if (result.type === "analysis") {
    analysisCard(result.analysis, traceId);
  }
}

// ------------------------------------------------------------------ task card

function cond(c) {
  const ops = { eq: "=", ne: "≠", gt: ">", gte: "≥", lt: "<", lte: "≤", in: "in", contains: "contains", starts_with: "starts with", blank: "is blank", not_blank: "is not blank" };
  const v = c.value === undefined ? "" : " " + (Array.isArray(c.value) ? c.value.join(", ") : JSON.stringify(c.value));
  return `<code>${esc(c.column)}</code> ${esc(ops[c.op] || c.op)}${esc(v)}`;
}
function describeStep(s, refs) {
  const cols = (a) => a.map((c) => `<code>${esc(c)}</code>`).join(", ");
  switch (s.op) {
    case "clean_text": return `Tidy ${cols(s.columns)}: trim spaces${s.collapse_spaces ? ", collapse inner spaces" : ""}${s.case && s.case !== "none" ? `, ${esc(s.case)} case` : ""}`;
    case "fill_blank": return `Fill blank ${cols([s.column])} with ${"from_column" in s ? cols([s.from_column]) : esc(JSON.stringify(s.value))}`;
    case "map_values": return `Replace values in ${cols([s.column])} (${Object.keys(s.mapping).length} mappings)`;
    case "lookup": {
      const r = refs[s.reference] || {};
      const src = r.source === "zetaris" ? `Zetaris: <code>${esc((r.sql || "").replace(/\s+/g, " ").slice(0, 80))}</code>` : `sheet <code>${esc(r.sheet)}</code>`;
      return `Look up ${cols(Object.keys(s.fields))} by ${cols([s.key])} from ${src}${s.status_column ? `; status in ${cols([s.status_column])}` : ""}`;
    }
    case "compute": return `${cols([s.column])} = <code>${esc(s.expression)}</code>${s.round != null ? `, rounded to ${s.round}` : ""}`;
    case "concat": return `${cols([s.column])} = <code>${esc(s.template)}</code>`;
    case "split": return `Split ${cols([s.column])} into ${cols(s.into)}`;
    case "format_date": return `Rewrite ${cols([s.column])} dates as <code>${esc(s.output_format)}</code>`;
    case "flag": return `Set ${cols([s.column])} to ${esc(JSON.stringify(s.then))} when ${s.when.map(cond).join(s.match === "any" ? " or " : " and ")}${"else" in s ? `, else ${esc(JSON.stringify(s.else))}` : ""}`;
    case "filter": return `Keep rows where ${s.keep_when.map(cond).join(s.match === "any" ? " or " : " and ")}`;
    case "dedupe": return `Remove duplicates by ${cols(s.columns)} (keep ${esc(s.keep || "first")})`;
    case "sort": return `Sort by ${s.by.map((b) => `<code>${esc(b.column)}</code>${b.descending ? " ↓" : " ↑"}`).join(", ")}`;
    case "summarize": return `New sheet <code>${esc(s.sheet)}</code>: ${s.values.map((v) => `${esc(v.agg)} of ${esc(v.column)}`).join(", ")} by ${cols(s.group_by)}`;
    default: return esc(s.op);
  }
}
function table(rows, inputCols) {
  if (!rows || !rows.length) return `<p class="muted">No rows.</p>`;
  const cols = Object.keys(rows[0]);
  const isNew = (c) => inputCols && !inputCols.includes(c);
  const head = cols.map((c) => `<th class="${isNew(c) ? "new" : ""}">${esc(c)}</th>`).join("");
  const body = rows.map((r) => `<tr>${cols.map((c) => `<td class="${isNew(c) ? "new" : ""} ${typeof r[c] === "number" ? "num" : ""}" title="${esc(fmt(r[c]))}">${esc(fmt(r[c]))}</td>`).join("")}</tr>`).join("");
  return `<div class="table-wrap"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function resultBlock(task) {
  const o = task.outputs;
  if (!o) return "";
  const links = o.files.map((f) => `<a class="btn primary" href="/files/${encodeURIComponent(f.file_id)}" download title="${esc(f.file_id)}">⬇ Filled spreadsheet (.${esc(f.format)})</a>`).join("");
  return `<div class="result"><b>Done.</b> <span class="muted">${o.rows_out} rows written${o.cells_changed != null ? `, ${o.cells_changed} cells changed or added` : ""}${o.sheets.length ? `, plus ${o.sheets.map(esc).join(", ")}` : ""}. Approved code ${esc(task.code_hash.slice(0, 12))} ran on your file.</span>
    <div class="links">${links}<a class="btn" href="/tasks/${encodeURIComponent(task.task_id)}/code" download>⬇ Automation (.py)</a></div></div>`;
}
function taskCard(task, traceId) {
  const qa = task.qa, memo = task.memo || {};
  const refs = task.spec.references || {};
  const checks = qa.checks.map((c) => `<span class="chip ${c.passed ? "" : "fail"}" title="${esc(c.detail)}">${c.passed ? "✓" : "✕"} ${esc(c.name.replace(/_/g, " "))}</span>`).join("");
  const sheets = Object.entries(qa.preview.sheets || {}).map(([name, rows]) => `<div class="section-label">Sheet: ${esc(name)}</div>${table(rows)}`).join("");
  const advisories = [...(task.advisories || []), ...(qa.advisories || []).map((a) => a.detail)].filter(Boolean);
  const card = el("div", "card msg");
  card.innerHTML = `
    <p class="task-title">${esc(task.spec.summary)}</p>
    <p class="muted">${esc(task.understanding || "")}${task.reused ? ` <b>Reused the automation you approved before (${esc(task.reused)}).</b>` : ""}</p>
    <ol class="plan">${task.spec.steps.map((s) => `<li>${describeStep(s, refs)}</li>`).join("")}</ol>
    <div class="stats">
      <div class="stat"><b>${Math.round(qa.pass_rate * 100)}%</b><span>QA pass (${qa.cases} cases)</span></div>
      <div class="stat"><b>${task.qa_rounds.length}</b><span>QA round${task.qa_rounds.length === 1 ? "" : "s"}</span></div>
      <div class="stat"><b>${esc(memo.confidence ?? "-")}</b><span>confidence</span></div>
      <div class="stat"><b>${esc(qa.output_rows ?? "-")}</b><span>output rows</span></div>
    </div>
    <div class="chips">${checks}<span class="chip info">${esc(task.code_origin)}</span><span class="chip info">${esc(task.data_source)}</span></div>
    <div class="section-label">Preview of your file after the change</div>
    ${table(qa.preview.rows, task.input_columns)}
    ${sheets}
    <div class="memo"><b>Manager:</b> ${esc(memo.rationale || "")}
      ${advisories.length ? `<ul>${advisories.slice(0, 5).map((a) => `<li>${esc(a)}</li>`).join("")}</ul>` : ""}</div>
    <details class="code"><summary>View the code (sha256 ${esc(task.code_hash.slice(0, 12))}, v${task.code_version}) · trace ${esc(traceId || task.run_id)}</summary><pre>${esc(task.code)}</pre></details>
    <div class="slot"></div>`;
  messages.appendChild(card);
  const slot = card.querySelector(".slot");
  if (task.status === "completed") {
    slot.innerHTML = resultBlock(task);
    setRun("done", "Your file is ready", "done");
  } else if (task.status === "awaiting_approval") {
    approvalBox(slot, task);
    setRun("attention", "Approval needed", "review");
  } else {
    slot.innerHTML = `<div class="result rejected"><b>Not sent for approval.</b> <span class="muted">Manager decision: ${esc(memo.decision)}. ${task.qa_failures?.length ? "First QA failure: " + esc(task.qa_failures[0].purpose + " – " + task.qa_failures[0].problem) : ""}</span></div>`;
    setRun("error", "QA did not pass", "stopped");
  }
  scrollDown();
}
function approvalBox(slot, task) {
  slot.innerHTML = `<div class="approve">
      <div class="muted">Approving runs exactly this code (sha256 ${esc(task.code_hash.slice(0, 12))}) on your file. Nothing runs before you approve.</div>
      <div class="approve-row"><input type="text" aria-label="Your name" placeholder="Your name" value="${esc(store("agentoffice.approver") || "")}"></div>
      <div class="approve-row"><button class="btn primary" data-d="approve" type="button">Approve &amp; run</button><button class="btn danger" data-d="reject" type="button">Reject</button></div>
    </div>`;
  const name = slot.querySelector("input");
  slot.querySelectorAll("button").forEach((b) => b.addEventListener("click", async () => {
    const approver = name.value.trim();
    if (!approver) { name.focus(); name.placeholder = "Your name is needed to approve"; return; }
    store("agentoffice.approver", approver);
    slot.querySelectorAll("button").forEach((x) => (x.disabled = true));
    setRun("busy", b.dataset.d === "approve" ? "Operator · running your file" : "Recording your decision", "working");
    try {
      const done = await api(`/tasks/${encodeURIComponent(task.task_id)}/decision`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision: b.dataset.d, approver, code_hash: task.code_hash }) });
      if (done.status === "completed") {
        slot.innerHTML = resultBlock(done) + `<p class="muted">Saved to memory: next time this exact automation can run without asking.</p>`;
        setRun("done", "Your file is ready", "done");
      } else {
        slot.innerHTML = `<div class="result rejected"><b>Rejected.</b> <span class="muted">Recorded in the approval ledger and H-MEM; the team will not re-propose this code.</span></div>`;
        setRun("idle", "AgentOffice");
      }
    } catch (err) {
      slot.querySelectorAll("button").forEach((x) => (x.disabled = false));
      bot(`Could not record the decision: ${esc(err.message)}`, "error");
      setRun("error", "Decision failed", "error");
    }
    scrollDown();
  }));
}

// ------------------------------------------------------------------ analysis card

function analysisCard(a, traceId) {
  const r = a.result;
  const ranking = (r.ranking || []).map((x) => `<tr><td>${esc(x.routine)}</td><td class="num">${esc(x.estimated_hours_saved_per_week)}</td><td>${esc(x.risk)}</td></tr>`).join("");
  const card = el("div", "card msg");
  card.innerHTML = `
    <p class="task-title">${esc(r.summary)}</p>
    <div class="section-label">Findings (each backed by data rows)</div>
    <ul class="plan">${r.findings.slice(0, 6).map((f) => `<li>${esc(f)}</li>`).join("")}</ul>
    ${ranking ? `<div class="section-label">What to automate first</div><div class="table-wrap"><table><thead><tr><th>Routine</th><th>h/week saved</th><th>Risk</th></tr></thead><tbody>${ranking}</tbody></table></div>` : ""}
    ${r.decision ? `<div class="memo"><b>Manager (${esc(r.decision.decision)}, confidence ${esc(r.confidence)}):</b> ${esc(r.decision.rationale)}</div>` : ""}
    <p class="muted">${esc(a.mode.model)} · ${esc(a.mode.data_source)} · ${a.usage.model_calls} model calls, ${a.usage.data_queries} queries · trace ${esc(traceId)}</p>
    <div class="slot"></div>`;
  messages.appendChild(card);
  const approval = a.approval;
  const slot = card.querySelector(".slot");
  if (approval && approval.status === "pending") {
    slot.innerHTML = `<div class="approve"><div class="muted">The ${esc(approval.automation)} automation passed QA. Approve it to keep it for the team (code ${esc(approval.code_hash.slice(0, 12))}).</div>
      <div class="approve-row"><input type="text" aria-label="Your name" placeholder="Your name" value="${esc(store("agentoffice.approver") || "")}"><button class="btn primary" data-d="approve" type="button">Approve</button><button class="btn danger" data-d="reject" type="button">Reject</button></div></div>`;
    const name = slot.querySelector("input");
    slot.querySelectorAll("button").forEach((b) => b.addEventListener("click", async () => {
      const approver = name.value.trim();
      if (!approver) { name.focus(); return; }
      store("agentoffice.approver", approver);
      try {
        const d = await api(`/approvals/${approval.approval_id}`, { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision: b.dataset.d, approver, code_hash: approval.code_hash }) });
        slot.innerHTML = `<div class="result ${d.status === "approved" ? "" : "rejected"}"><b>${esc(d.status)}.</b> <span class="muted">Saved to H-MEM (${esc(d.memory?.status)}); later runs respect it.</span></div>`;
        setRun("idle", "AgentOffice");
      } catch (err) { bot(`Could not record the decision: ${esc(err.message)}`, "error"); }
    }));
    setRun("attention", "Approval needed", "review");
  } else {
    setRun("done", "Analysis ready", "done");
  }
  scrollDown();
}

// ------------------------------------------------------------------ start

async function init() {
  render();
  try {
    state.config = await api("/config");
    const c = state.config;
    $("sample-toggle").checked = c.sample_mode;
    $("sample-toggle").disabled = c.forced_sample_mode || !c.default_mode_ready;
    const modeText = () => $("sample-toggle").checked
      ? `<span class="dot sample"></span>Sample mode · bundled data, no sponsor calls`
      : `<span class="dot"></span>Live · ${esc(c.model)} via Meterless Nightshift · Zetaris`;
    $("mode").innerHTML = modeText();
    $("sample-toggle").addEventListener("change", () => { $("mode").innerHTML = modeText(); });
  } catch (_) {
    $("mode").textContent = "API not reachable";
  }
  try { state.samples = (await api("/samples")).samples; } catch (_) { state.samples = []; }
  welcome();
  if (!desktop && new URLSearchParams(location.search).get("open") === "1") expand();
}
init();
