// AgentOffice chat island: a floating pill that opens into a chat panel (Ctrl+Shift+Space / Ctrl+K).
// Talks to the API in run.py: /config, /samples, /uploads, /chat, /jobs/{id}, /tasks/{id}/decision.
"use strict";

const $ = (id) => document.getElementById(id);
const island = $("island"), pill = $("pill"), pillText = $("pill-text"), pillBadge = $("pill-badge");
const messages = $("messages"), input = $("input"), form = $("composer"), fileInput = $("file");
const attachmentEl = $("attachment"), drop = $("drop"), sendBtn = $("send");

const desktop = new URLSearchParams(location.search).get("shell") === "desktop";
const state = { expanded: false, large: false, side: store("agentoffice.side") !== "0", sideNews: false, run: "idle",
                config: null, samples: [], attachment: null, busy: false, pickingFile: false, quitting: false };

const AGENTS = {
  Orchestrator: "Orchestrator", FrontDeskAgent: "Front desk", DataProfilerAgent: "Data profiler", PMAgent: "PM",
  SpecCritic: "Spec critic", RetrieverAgent: "Retriever", EngineerAgent: "Engineer", QAAgent: "QA",
  ManagerAgent: "Manager", MemoryKeeperAgent: "Memory keeper", OperatorAgent: "Operator",
  SourceMapperAgent: "Source mapper", ReconcilerAgent: "Reconciler", ConflictCriticAgent: "Conflict critic",
  ResearchPlannerAgent: "Research planner", ReportWriterAgent: "Report writer", InsightCriticAgent: "Insight critic",
  PriorityCritic: "Priority critic", AssistantAgent: "Assistant", GroundingCritic: "Grounding critic",
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
  critique_report: "Checked the report", critique_claims: "Checked the claims",
  critique_sources: "Judged source conflicts", cell_writes: "Wrote cells", audit_sheet: "Wrote audit sheet",
  plan_help: "Worked out what to look up", write_sql: "Wrote the query", fix_sql: "Fixed the query",
  write_answer: "Wrote the answer", revise_answer: "Revised the answer", write_email: "Drafted the email",
  revise_email: "Revised the draft", critique_grounding: "Checked every fact", assist_done: "Draft ready",
  review_wording: "Checked each statement against the rows", ranking_trimmed: "Kept the ranking to this question",
  model_retry: "Asked the model again", model_fallback: "Switched to another model",
};
const HIDDEN = new Set(["zetaris_query"]);

// ------------------------------------------------------------------ helpers

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
/** True when the text has Arabic-script letters (UAE/Saudi instructions and sheet cells). */
function hasArabic(text) {
  return /[\u0600-\u06FF]/.test(String(text ?? ""));
}
function applyRtl(node, text) {
  if (!node) return;
  if (hasArabic(text)) {
    node.setAttribute("dir", "rtl");
    node.setAttribute("lang", "ar");
  } else {
    node.removeAttribute("dir");
    node.removeAttribute("lang");
  }
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
  if (state.large && state.expanded) island.dataset.size = "large";
  else delete island.dataset.size;
  pill.setAttribute("aria-expanded", String(state.expanded));
  const enlargeBtn = $("enlarge");
  if (enlargeBtn) {
    enlargeBtn.setAttribute("aria-pressed", String(state.large));
    enlargeBtn.title = state.large ? "Shrink window" : "Enlarge window";
    enlargeBtn.setAttribute("aria-label", enlargeBtn.title);
    // Desktop shell already fills the viewport; enlarge is a browser-only control.
    enlargeBtn.hidden = desktop;
  }
  island.dataset.side = state.side ? "open" : "closed";
  const sideBtn = $("side-toggle");
  sideBtn.setAttribute("aria-pressed", String(state.side));
  sideBtn.title = state.side ? "Hide Team and Sheet" : "Show Team and Sheet";
  sideBtn.setAttribute("aria-label", sideBtn.title + (state.sideNews ? " (new activity)" : ""));
  sideBtn.classList.toggle("news", state.sideNews && !state.side);
  // Desktop shell: scripts/island.py reads the state and the browser's own title-bar height from the title.
  const mode = state.quitting ? "quit" : !state.expanded ? "island" : state.side ? "panel" : "chat";
  document.title = desktop
    ? `AgentOffice · ${mode} · ${Math.max(0, window.outerHeight - window.innerHeight)}`
    : "AgentOffice";
}
function setLarge(on) {
  state.large = !!on;
  store("agentoffice.large", state.large ? "1" : "0");
  render();
}
function toggleLarge() { setLarge(!state.large); }
function setSide(open) {
  state.side = !!open;
  if (state.side) state.sideNews = false;
  store("agentoffice.side", state.side ? "1" : "0");
  render();
  if (state.side) refitCaptions();
}
/** The Team/Sheet pane changed; flag it on the toggle when the pane is hidden. */
function sideActivity() {
  if (state.side || state.sideNews) return;
  state.sideNews = true;
  render();
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
  setTimeout(refitCaptions, 600);   // after the panel's open transition, measure captions for real
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
$("enlarge").addEventListener("click", toggleLarge);
$("side-toggle").addEventListener("click", () => setSide(!state.side));
document.addEventListener("keydown", (e) => {
  const k = e.key.toLowerCase();
  if ((e.ctrlKey && e.shiftKey && (k === " " || e.code === "Space")) || (e.ctrlKey && !e.shiftKey && k === "k")) {
    e.preventDefault();
    toggle();
  } else if (k === "escape" && state.expanded) {
    if (state.large) setLarge(false);
    else collapse();
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

async function loadUpload(meta) {
  if (!meta) return null;
  if (meta.rows && meta.columns) return meta;
  try { return await api(`/uploads/${encodeURIComponent(meta.upload_id)}`); } catch (_) { return meta; }
}
function setAttachment(meta) {
  state.attachment = meta;
  attachmentEl.innerHTML = "";
  attachmentEl.hidden = !meta;
  if (!meta) { sheetLoad(null); return; }
  const rows = meta.sheets.map((s) => `${s.name}: ${s.rows} rows`).join(" · ");
  const chip = el("div", "file-chip", `<span aria-hidden="true">📄</span><span class="name">${esc(meta.filename)}</span>` +
    `<span class="meta">${esc(rows)}</span><button type="button" aria-label="Remove attachment">✕</button>`);
  chip.querySelector("button").addEventListener("click", () => setAttachment(null));
  attachmentEl.appendChild(chip);
  loadUpload(meta).then((full) => { state.attachment = full; sheetLoad(full); });
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
  applyRtl(m, text);
  m.appendChild(document.createTextNode(text));
  if (meta) m.appendChild(el("div", "file-chip", `<span aria-hidden="true">📄</span><span class="name">${esc(meta.filename)}</span>`));
  messages.appendChild(m);
  scrollDown();
}
function welcome() {
  const w = bot(`<div class="welcome"><h2>What should the team do?</h2><p>Attach a spreadsheet and describe the
    repetitive work: filling columns, tidying text, fixing dates, flagging rows, removing duplicates, totals.
    Or ask where your team's time goes, ask a question about your data, or have an email drafted from it.</p>
    <div class="samples"></div></div>`);
  const box = w.querySelector(".samples");
  for (const s of state.samples) {
    const b = el("button", "sample-card", `<b>Try: ${esc(s.filename)}</b><span>${esc(s.suggested_prompt || "")}</span>`);
    b.type = "button";
    b.addEventListener("click", () => { setAttachment(s); input.value = s.suggested_prompt || ""; autosize(); input.focus(); });
    box.appendChild(b);
  }
  const q = el("button", "sample-card", `<b>Ask: where is the finance team losing time?</b><span>Ranks routines from the connected Zetaris activity log (demo corpus in sample mode — not a customer hours claim).</span>`);
  q.type = "button";
  q.addEventListener("click", () => {
    setAttachment(null); teamSetMode("analysis");
    input.value = "Where is the finance team losing time, and what should we automate first?"; autosize(); input.focus();
  });
  box.appendChild(q);
  const d = el("button", "sample-card", `<b>Draft: an email to the finance team</b><span>Grounded in the workforce data; every number is checked. Needs the model (not sample mode).</span>`);
  d.type = "button";
  d.addEventListener("click", () => {
    setAttachment(null); teamSetMode("assist");
    input.value = "Draft an email to the finance team summarising which task categories took the most hours per week."; autosize(); input.focus();
  });
  box.appendChild(d);
}

// ------------------------------------------------------------------ sending + live runs

function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 140) + "px"; }
input.addEventListener("input", () => {
  applyRtl(input, input.value);
  autosize();
});
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
});
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || state.busy) return;
  const attachment = state.attachment;
  user(text, attachment);
  input.value = ""; applyRtl(input, ""); autosize();
  // Keep the sheet preview for the right pane; clear only the composer chip.
  state.attachment = null;
  attachmentEl.innerHTML = "";
  attachmentEl.hidden = true;
  teamReset(attachment ? "task" : "analysis");
  if (attachment) sheetLoad(attachment);
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
  if (ev.action === "data_query") {
    const z = ev.target === "Zetaris" || String(ev.data_source || "").toLowerCase() === "zetaris";
    s = `${s}${z ? " from Zetaris" : " from sample data"}`;
  }
  return s;
}
function addStep(run, ev) {
  teamOnEvent(ev, false);
  sheetOnEvent(ev, false);
  if (HIDDEN.has(ev.action) || ev.action === "run_started" || ev.action === "cell_writes" || ev.action === "audit_sheet") return;
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
  replayStop();
  team.replay = false;
  const banner = $("replay-label");
  if (banner) banner.hidden = true;
  const run = runCard();
  let after = 0;
  for (;;) {
    const view = await api(`/jobs/${jobId}?after=${after}`);
    if (view.trace_id) teamSetRunId(view.trace_id);
    view.events.forEach((ev) => addStep(run, ev));
    after = view.next;
    if (view.status !== "running") {
      finishCard(run);
      teamFinish();
      loadReplayList();   // this run is now replayable
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
    if (result.task.status === "completed" && result.task.outputs?.cell_writes) {
      sheetShowOutput(result.task.outputs.cell_writes, result.task.outputs.execution_run_id || traceId,
                      result.task.outputs.cells_changed, result.task.outputs.sheet_layout);
    }
    taskCard(result.task, traceId);
  } else if (result.type === "analysis") {
    teamSetMode("analysis");
    analysisCard(result.analysis, traceId);
  } else if (result.type === "assist") {
    teamSetMode("assist");
    assistCard(result.assist, traceId);
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
    teamAwaitApproval(task.code_hash);
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
        teamApprove(done.code_hash);
        if (done.outputs?.cell_writes) {
          sheetShowOutput(done.outputs.cell_writes, done.outputs.execution_run_id || done.execution_run_id,
                          done.outputs.cells_changed, done.outputs.sheet_layout);
        }
        slot.innerHTML = resultBlock(done) + `<p class="muted">Saved to memory: next time this exact automation can run without asking.</p>`;
        loadReplayList();
        setRun("done", "Your file is ready", "done");
      } else {
        teamReject();
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
    ${ranking ? `<div class="section-label">What to automate first <span class="muted">(measured h/week on connected activity log — Priority Critic ceiling)</span></div><div class="table-wrap"><table><thead><tr><th>Routine</th><th>measured h/week</th><th>Risk</th></tr></thead><tbody>${ranking}</tbody></table></div>` : ""}
    ${r.decision ? `<div class="memo"><b>Manager (${esc(r.decision.decision)}, confidence ${esc(r.confidence)}):</b> ${esc(r.decision.rationale)}</div>` : ""}
    <p class="muted">${esc(a.mode.model)} · ${esc(a.mode.data_source)} · ${a.usage.model_calls} model calls, ${a.usage.data_queries} queries · trace ${esc(traceId)}</p>
    <div class="slot"></div>`;
  messages.appendChild(card);
  const approval = a.approval;
  const slot = card.querySelector(".slot");
  if (approval && approval.status === "pending") {
    teamAwaitApproval(approval.code_hash);
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
        if (d.status === "approved") teamApprove(approval.code_hash); else teamReject();
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

// ------------------------------------------------------------------ answer / email card

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch (_) { /* fall back below */ }
  const t = el("textarea");
  t.value = text; t.setAttribute("readonly", ""); t.style.position = "fixed"; t.style.opacity = "0";
  document.body.appendChild(t); t.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) { ok = false; }
  t.remove();
  return ok;
}
function assistCard(a, traceId) {
  const g = a.grounding || {};
  const rows = (a.evidence || []).reduce((n, e) => n + e.rows, 0);
  const checked = g.verified
    ? `<p class="muted">✓ Grounding critic: every number and person traces to the data or your request${g.rounds ? ` (after ${g.rounds} revision${g.rounds > 1 ? "s" : ""})` : ""}.</p>`
    : `<div class="memo warn"><b>Check before using:</b> the grounding critic still objects after ${esc(g.rounds)} revisions: ${(g.objections || []).map(esc).join("; ")}</div>`;
  const evidence = (a.evidence || []).length
    ? `<details class="code"><summary>Based on ${a.evidence.length} ${a.evidence.length === 1 ? "query" : "queries"} (${rows} rows) on ${esc(a.data_source)}</summary><ul class="plan">${a.evidence.map((e) => `<li>${esc(e.question)} · ${e.rows} rows<pre>${esc(e.sql)}</pre></li>`).join("")}</ul></details>`
    : `<p class="muted">No company data was needed or found for this.</p>`;
  const card = el("div", "card msg");
  if (a.kind === "email") {
    card.innerHTML = `
      <div class="section-label">Email draft</div>
      <div class="email">
        <div><b>To:</b> ${esc((a.to || []).join(", ") || "(add recipients)")}</div>
        <div><b>Subject:</b> ${esc(a.subject)}</div>
        <div class="email-body">${esc(a.body)}</div>
      </div>
      <div class="approve-row"><button class="btn primary" type="button" data-copy>Copy email</button></div>
      ${checked}${evidence}
      <p class="muted">Draft only: AgentOffice never sends email. ${esc(a.model)} · ${a.personal_values_masked} personal values masked from the model · trace ${esc(traceId)}</p>`;
    const btn = card.querySelector("[data-copy]");
    btn.addEventListener("click", async () => {
      const ok = await copyText(`Subject: ${a.subject}\n\n${a.body}`);
      btn.textContent = ok ? "Copied" : "Copy failed: select the text";
    });
  } else {
    card.innerHTML = `
      <p class="answer">${esc(a.answer)}</p>
      ${checked}${evidence}
      <p class="muted">${esc(a.model)} · ${a.personal_values_masked} personal values masked from the model · trace ${esc(traceId)}</p>`;
  }
  messages.appendChild(card);
  setRun("done", a.kind === "email" ? "Draft ready" : "Answer ready", "done");
  scrollDown();
}

// ------------------------------------------------------------------ Team canvas + Sheet pane

const NS = "http://www.w3.org/2000/svg";
const TASK_LAYOUT = {
  stages: [{ label: "Plan", y: 24 }, { label: "Build", y: 220 }, { label: "Decide", y: 390 }],
  nodes: [
    { id: "DataProfilerAgent", label: "Data profiler", tag: "Zetaris SQL", x: 24, y: 44, w: 118, h: 58 },
    { id: "RetrieverAgent", label: "Retriever", tag: "Zetaris SQL", x: 156, y: 44, w: 100, h: 58 },
    { id: "PMAgent", label: "PM", tag: "model", x: 290, y: 44, w: 88, h: 58 },
    { id: "MemoryKeeperAgent", label: "Memory keeper", tag: "H-MEM", x: 24, y: 122, w: 124, h: 58 },
    { id: "SpecCritic", label: "Spec critic", tag: "rules", x: 274, y: 122, w: 114, h: 58 },
    { id: "EngineerAgent", label: "Engineer", tag: "model", x: 70, y: 250, w: 110, h: 58 },
    { id: "QAAgent", label: "QA", tag: "reference runtime · model", x: 210, y: 250, w: 168, h: 58 },
    { id: "ManagerAgent", label: "Manager", tag: "model · rule gate", x: 80, y: 430, w: 110, h: 58 },
    { id: "Human", label: "Approval", x: 250, y: 430, w: 110, h: 58 },
  ],
  edges: [
    ["DataProfilerAgent", "PMAgent"], ["MemoryKeeperAgent", "PMAgent"],
    ["PMAgent", "RetrieverAgent"], ["PMAgent", "SpecCritic"], ["SpecCritic", "PMAgent"],
    ["PMAgent", "EngineerAgent"], ["EngineerAgent", "QAAgent"], ["QAAgent", "EngineerAgent"],
    ["QAAgent", "ManagerAgent"], ["ManagerAgent", "Human"],
  ],
  loops: new Set(["SpecCritic→PMAgent", "QAAgent→EngineerAgent"]),
};
const ANALYSIS_LAYOUT = {
  stages: [
    { label: "1 Discover · World Model", y: 20 }, { label: "2 Explain", y: 170 }, { label: "3 Decide & build", y: 320 },
  ],
  nodes: [
    { id: "SourceMapperAgent", label: "Source mapper", tag: "Zetaris", x: 16, y: 40, w: 118, h: 56 },
    { id: "ReconcilerAgent", label: "Reconciler", tag: "World Model", x: 150, y: 40, w: 108, h: 56 },
    { id: "ConflictCriticAgent", label: "Conflict critic", tag: "World Model", x: 274, y: 40, w: 124, h: 56 },
    { id: "ResearchPlannerAgent", label: "Research planner", tag: "model", x: 16, y: 188, w: 124, h: 56 },
    { id: "RetrieverAgent", label: "Retriever", tag: "model · Zetaris", x: 156, y: 188, w: 100, h: 56 },
    { id: "ReportWriterAgent", label: "Report writer", tag: "model", x: 274, y: 188, w: 118, h: 56 },
    { id: "InsightCriticAgent", label: "Insight critic", tag: "model", x: 274, y: 256, w: 118, h: 56 },
    { id: "MemoryKeeperAgent", label: "Memory keeper", tag: "H-MEM", x: 10, y: 348, w: 118, h: 56 },
    { id: "PMAgent", label: "PM", tag: "model", x: 140, y: 348, w: 78, h: 56 },
    { id: "PriorityCritic", label: "Priority critic", tag: "rules", x: 236, y: 348, w: 118, h: 56 },
    { id: "EngineerAgent", label: "Engineer", tag: "model", x: 16, y: 418, w: 100, h: 56 },
    { id: "QAAgent", label: "QA", tag: "reference runtime · model", x: 130, y: 418, w: 168, h: 56 },
    { id: "ManagerAgent", label: "Manager", tag: "model · rule gate", x: 310, y: 418, w: 100, h: 56 },
    { id: "Human", label: "Approval", x: 310, y: 486, w: 100, h: 42 },
  ],
  edges: [
    ["SourceMapperAgent", "ReconcilerAgent"], ["ReconcilerAgent", "ConflictCriticAgent"],
    ["ConflictCriticAgent", "ResearchPlannerAgent"], ["ResearchPlannerAgent", "RetrieverAgent"],
    ["RetrieverAgent", "ReportWriterAgent"], ["ReportWriterAgent", "InsightCriticAgent"],
    ["InsightCriticAgent", "ReportWriterAgent"], ["InsightCriticAgent", "PMAgent"],
    ["MemoryKeeperAgent", "PMAgent"], ["PMAgent", "PriorityCritic"], ["PriorityCritic", "PMAgent"],
    ["PMAgent", "EngineerAgent"], ["EngineerAgent", "QAAgent"], ["QAAgent", "EngineerAgent"],
    ["QAAgent", "ManagerAgent"], ["ManagerAgent", "Human"],
  ],
  loops: new Set(["InsightCriticAgent→ReportWriterAgent", "PriorityCritic→PMAgent", "QAAgent→EngineerAgent"]),
};
const ASSIST_LAYOUT = {
  stages: [{ label: "Look it up", y: 24 }, { label: "Write and check", y: 220 }],
  nodes: [
    { id: "FrontDeskAgent", label: "Front desk", tag: "model", x: 24, y: 44, w: 118, h: 58 },
    { id: "AssistantAgent", label: "Assistant", tag: "model", x: 160, y: 44, w: 118, h: 58 },
    { id: "RetrieverAgent", label: "Retriever", tag: "model · Zetaris", x: 296, y: 44, w: 128, h: 58 },
    { id: "GroundingCritic", label: "Grounding critic", tag: "rules · model", x: 160, y: 250, w: 140, h: 58 },
  ],
  edges: [
    ["FrontDeskAgent", "AssistantAgent"], ["AssistantAgent", "RetrieverAgent"], ["RetrieverAgent", "AssistantAgent"],
    ["AssistantAgent", "GroundingCritic"], ["GroundingCritic", "AssistantAgent"],
  ],
  loops: new Set(["RetrieverAgent→AssistantAgent", "GroundingCritic→AssistantAgent"]),
};
const CRITIC_ACTIONS = new Set(["critique_spec", "critique_output", "critique_priority", "critique_claims",
                                "critique_sources", "critique_grounding"]);

const team = {
  mode: "task", runId: null, nodes: {}, edges: {}, edgeMeta: {}, attempts: {}, last: {},
  models: {}, providers: {}, sources: {}, challenges: [], evidence: { claims: [], conflicts: [] },
  working: null, codeHash: null, replay: false, replayTimer: null, replayEvents: [], replayIndex: -1,
  sponsors: null,
};
const sheet = {
  meta: null, active: null, data: {}, totals: {}, columns: {},
  changed: 0, audit: false, cellMap: {}, cellMeta: {},
};

function svgEl(tag, attrs) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
  return n;
}
const LAYOUTS = { task: TASK_LAYOUT, analysis: ANALYSIS_LAYOUT, assist: ASSIST_LAYOUT };
function layout() { return LAYOUTS[team.mode] || TASK_LAYOUT; }
function nodeCenter(n) { return { x: n.x + n.w / 2, y: n.y + n.h / 2 }; }
/** Where the ray from n's center toward (tx, ty) hits n's border (inset keeps arrow outside the box). */
function borderPoint(n, tx, ty, inset) {
  const cx = n.x + n.w / 2, cy = n.y + n.h / 2;
  const dx = tx - cx, dy = ty - cy;
  if (dx === 0 && dy === 0) return { x: cx, y: cy };
  const hw = n.w / 2 - (inset || 0), hh = n.h / 2 - (inset || 0);
  const sx = Math.abs(dx) > 1e-6 ? hw / Math.abs(dx) : Infinity;
  const sy = Math.abs(dy) > 1e-6 ? hh / Math.abs(dy) : Infinity;
  const s = Math.min(sx, sy);
  return { x: cx + dx * s, y: cy + dy * s };
}
function edgePath(A, B, loop) {
  const ac = nodeCenter(A), bc = nodeCenter(B);
  if (loop) {
    // Critic loops arc around the side so they don't sit on the forward edge.
    const vertical = Math.abs(ac.x - bc.x) <= Math.abs(ac.y - bc.y);
    let p1, p2, mx, my;
    if (vertical) {
      p1 = { x: A.x + A.w, y: ac.y };
      p2 = { x: B.x + B.w, y: bc.y };
      mx = Math.max(A.x + A.w, B.x + B.w) + 34;
      my = (p1.y + p2.y) / 2;
    } else {
      p1 = { x: ac.x, y: A.y + A.h };
      p2 = { x: bc.x, y: B.y + B.h };
      mx = (p1.x + p2.x) / 2;
      my = Math.max(A.y + A.h, B.y + B.h) + 28;
    }
    return `M ${p1.x} ${p1.y} Q ${mx} ${my} ${p2.x} ${p2.y}`;
  }
  const p1 = borderPoint(A, bc.x, bc.y, 1);
  // Pull the tip back so the arrowhead sits on the border, not over the label.
  const p2 = borderPoint(B, ac.x, ac.y, 8);
  return `M ${p1.x} ${p1.y} L ${p2.x} ${p2.y}`;
}

function teamDraw() {
  const svg = $("team-svg");
  if (!svg) return;
  svg.innerHTML = "";
  const L = layout();
  team.nodes = {}; team.edges = {};
  for (const s of L.stages) {
    const t = svgEl("text", { x: 16, y: s.y, class: "stage-label" });
    t.textContent = s.label;
    svg.appendChild(t);
  }
  const byId = Object.fromEntries(L.nodes.map((n) => [n.id, n]));
  const defs = svgEl("defs");
  const m = svgEl("marker", { id: "edge-arrow", viewBox: "0 0 10 10", refX: "8", refY: "5",
    markerWidth: "7", markerHeight: "7", orient: "auto" });
  m.appendChild(svgEl("path", { d: "M 0 1.2 L 8 5 L 0 8.8 z", fill: "rgba(220, 228, 240, 0.95)" }));
  defs.appendChild(m);
  const mw = svgEl("marker", { id: "edge-arrow-warn", viewBox: "0 0 10 10", refX: "8", refY: "5",
    markerWidth: "7", markerHeight: "7", orient: "auto" });
  mw.appendChild(svgEl("path", { d: "M 0 1.2 L 8 5 L 0 8.8 z", fill: "#fbbf24" }));
  defs.appendChild(mw);
  svg.appendChild(defs);

  const edgeLayer = svgEl("g", { class: "edge-layer" });
  svg.appendChild(edgeLayer);
  for (const [a, b] of L.edges) {
    const A = byId[a], B = byId[b];
    if (!A || !B) continue;
    const key = `${a}→${b}`;
    const loop = L.loops.has(key);
    const d = edgePath(A, B, loop);
    const path = svgEl("path", {
      d, class: "edge" + (loop ? " loop" : ""), "data-edge": key,
      "marker-end": "url(#edge-arrow)",
    });
    path.addEventListener("click", (ev) => { ev.stopPropagation(); teamShowEdge(key); });
    edgeLayer.appendChild(path);
    // Short reason label sits next to the loop arc (placed when a critic fires, see setReasonLabel).
    let reasonLabel = null;
    if (loop) {
      reasonLabel = svgEl("text", { class: "edge-reason", "data-edge": key, visibility: "hidden" });
      reasonLabel.addEventListener("click", (ev) => { ev.stopPropagation(); teamShowEdge(key); });
      edgeLayer.appendChild(reasonLabel);
    }
    team.edges[key] = path;
    team.edgeMeta[key] = { short: null, full: null, ...team.edgeMeta[key], label: reasonLabel, ends: [A, B] };
    if (team.edgeMeta[key].short && reasonLabel) {
      setReasonLabel(team.edgeMeta[key]);
      path.classList.add("loop-fired", "has-reason");
    }
  }

  const nodeLayer = svgEl("g", { class: "node-layer" });
  svg.appendChild(nodeLayer);
  for (const n of L.nodes) {
    const g = svgEl("g", { class: "node", "data-agent": n.id, transform: `translate(${n.x},${n.y})` });
    g.appendChild(svgEl("rect", { class: "node-body", width: n.w, height: n.h, rx: 10, ry: 10 }));
    const label = svgEl("text", { class: "node-label", x: 8, y: 15 });
    label.textContent = n.label;
    g.appendChild(label);
    if (n.tag) {
      const tag = svgEl("text", { class: "node-tag", x: 8, y: 27 });
      tag.textContent = n.tag;
      g.appendChild(tag);
    }
    const modelLine = svgEl("text", { class: "node-sub", x: 8, y: n.tag ? 38 : 28, "data-model": "1" });
    g.appendChild(modelLine);
    const sub = svgEl("text", { class: "node-sub", x: 8, y: n.tag ? 48 : 38, "data-sub": "1" });
    g.appendChild(sub);
    g.appendChild(svgEl("rect", { class: "node-hit", width: n.w, height: n.h }));
    g.addEventListener("click", () => teamShowDetail(n.id));
    nodeLayer.appendChild(g);
    team.nodes[n.id] = { el: g, sub, modelLine, def: n };
    fitSvgText(modelLine, teamModelCaption(n.id), n.w - 14);
    fitSvgText(sub, "idle", n.w - 14);
  }
  $("agent-detail").hidden = true;
  challengesRender();
}

function teamSetNode(id, cls, subText) {
  const n = team.nodes[id];
  if (!n) return;
  n.el.classList.remove("working", "done", "wait", "rejected");
  if (cls) n.el.classList.add(cls);
  if (subText != null) fitSvgText(n.sub, subText, n.def.w - 14);
  if (n.modelLine) fitSvgText(n.modelLine, teamModelCaption(id), n.def.w - 14);
}
/** Set an SVG text, shortened with an ellipsis to fit maxW canvas units (estimated while the pane is hidden). */
function fitSvgText(el, text, maxW) {
  let s = String(text ?? "");
  el.dataset.full = s;                           // kept so refitCaptions() can measure again once visible
  el.textContent = s;
  const fontPx = parseFloat(getComputedStyle(el).fontSize) || 9;
  const measured = el.getComputedTextLength();
  if (!measured) {                               // not laid out yet (pane hidden): a conservative estimate
    el.textContent = shortText(s, Math.max(4, Math.floor(maxW / (fontPx * 0.62))));
    return;
  }
  while (el.getComputedTextLength() > maxW && s.length > 4) {
    s = s.slice(0, -1);
    el.textContent = s.trimEnd() + "…";
  }
}
/** Re-fit node captions to their measured width once the Team pane is visible again. */
function refitCaptions() {
  requestAnimationFrame(() => {
    for (const n of Object.values(team.nodes)) {
      for (const el of [n.modelLine, n.sub]) if (el && el.dataset.full != null) fitSvgText(el, el.dataset.full, n.def.w - 14);
    }
  });
}
/** Place a critic's reason beside its loop arc: inside the 440 x 540 canvas and clear of every node. */
function setReasonLabel(meta) {
  const label = meta.label;
  if (!label || !meta.short || !meta.ends) return;
  const [A, B] = meta.ends;
  const ac = nodeCenter(A), bc = nodeCenter(B);
  const text = shortText(meta.short, 24);
  const est = text.length * 4.8;                 // 8.5px bold text, about 4.8 canvas units per character
  const right = Math.max(A.x + A.w, B.x + B.w) + 8, left = Math.min(A.x, B.x) - 6, midY = (ac.y + bc.y) / 2;
  const candidates = Math.abs(ac.x - bc.x) > Math.abs(ac.y - bc.y)
    ? [[(ac.x + bc.x) / 2, Math.max(A.y + A.h, B.y + B.h) + 34, "middle"],   // side by side: under the arc,
       [(ac.x + bc.x) / 2, Math.min(A.y, B.y) - 8, "middle"]]                // else above the pair
    : [0, 14, -14].flatMap((dy) => [[right, midY + dy, "start"], [left, midY + dy, "end"]]);  // stacked: beside
  const box = ([x, y, anchor]) => {
    const x0 = anchor === "middle" ? x - est / 2 : anchor === "end" ? x - est : x;
    return { x0, x1: x0 + est, y0: y - 9, y1: y + 2 };
  };
  const clear = (b) => b.x0 >= 2 && b.x1 <= 438 && b.y0 >= 0 && b.y1 <= 538 && layout().nodes.every(
    (n) => b.x1 < n.x - 2 || b.x0 > n.x + n.w + 2 || b.y1 < n.y - 2 || b.y0 > n.y + n.h + 2);
  const [x, y, anchor] = candidates.find((c) => clear(box(c))) || candidates[0];
  label.textContent = text;
  label.setAttribute("x", x);
  label.setAttribute("y", y);
  label.setAttribute("text-anchor", anchor);
  label.setAttribute("visibility", "visible");
}
function shortModel(m) {
  let s = String(m || "");
  s = s.replace(/^nvidia\//i, "").replace(/^meterless\//i, "");
  return shortText(s, 26);
}
function shortProvider(p) {
  const s = String(p || "");
  if (/meterless/i.test(s)) return "Meterless Nightshift";
  return shortText(s, 28);
}
function teamModelCaption(id) {
  // Node captions drop a model's size suffix (nemotron-3-ultra-550b-a55b -> nemotron-3-ultra); details keep it.
  const m = team.models[id] && String(team.models[id]).replace(/-\d+(\.\d+)?b(-a\d+(\.\d+)?b)?$/i, "");
  const src = team.sources[id];
  const parts = [];
  if (m) parts.push(m);
  else if (id === "QAAgent") parts.push("reference runtime");
  if (src) parts.push(src);
  if (parts.length) return parts.join(" · ");
  const n = team.nodes[id];
  return (n && n.def && n.def.tag) || "idle";
}
function teamLitEdge(from, to, warn, reason) {
  if (!from || !to) return;
  const key = `${from}→${to}`;
  const path = team.edges[key];
  if (!path) return;
  path.classList.add(warn ? "warn" : "lit");
  path.setAttribute("marker-end", warn ? "url(#edge-arrow-warn)" : "url(#edge-arrow)");
  if (warn && reason) {
    path.classList.add("loop-fired", "has-reason");
    const meta = team.edgeMeta[key] || (team.edgeMeta[key] = {});
    meta.short = reason.short;
    meta.full = reason.full;
    setReasonLabel(meta);
  }
  if (!warn) setTimeout(() => path.classList.remove("lit"), 900);
}
function teamAttemptLine(id) {
  const a = team.attempts[id];
  if (!a || !a.length) return null;
  return a.map((x) => {
    if (x.ok) return `v${x.v} ✓` + (x.score ? ` ${x.score}` : "");
    return `v${x.v} ✗`;
  }).join(" → ");
}
function shortText(v, n) {
  const s = typeof v === "string" ? v : (v == null ? "" : JSON.stringify(v));
  const one = s.replace(/\s+/g, " ").trim();
  return one.length <= n ? one : one.slice(0, n - 1) + "…";
}
function parseMaybeJson(v) {
  if (v == null) return null;
  if (typeof v === "object") return v;
  try { return JSON.parse(v); } catch (_) { return null; }
}
function challengeFromEvent(ev) {
  const who = agentName(ev.agent);
  const out = ev.summary || "";
  const full = typeof out === "string" ? out : JSON.stringify(out);
  if (ev.action === "critique_claims" && ev.status === "needs_revision") {
    const m = full.match(/claim\s+(\d+)\s*:\s*([^"]+?)(?:\.|"|$)/i);
    const claim = m ? `#${m[1]}` : "";
    const why = m ? shortText(m[2], 70) : shortText(full, 70);
    return { ok: false, line: `Insight Critic dropped claim ${claim}: ${why} → Report Writer revised`,
             full, edge: "InsightCriticAgent→ReportWriterAgent",
             reason: { short: shortText(why, 24), full } };
  }
  if (ev.action === "drop_claims") {
    return { ok: false, line: `Insight Critic ${shortText(full, 90)}`, full,
             edge: "InsightCriticAgent→ReportWriterAgent",
             reason: { short: "claims dropped", full } };
  }
  if (ev.action === "critique_sources") {
    const assessments = parseMaybeJson(out) || parseMaybeJson(full);
    let trusted = "", task = "", detail = shortText(full, 90);
    if (Array.isArray(assessments) && assessments[0]) {
      task = assessments[0].task || "";
      trusted = (assessments[0].trusted_source || "").replace(/^[^:]+:/, "");
      detail = shortText(assessments[0].explanation || full, 80);
    } else if (/timesheets|tickets|activity/i.test(full)) {
      detail = shortText(full, 90);
    }
    const line = task
      ? `Conflict Critic: sources disagree on ${task} → trusted ${trusted || "one source"}`
      : `Conflict Critic: ${detail}`;
    return { ok: ev.status === "approved", line, full,
             edge: null, // forward handoff, not a loop
             evidence: assessments,
             reason: { short: shortText(`trust ${trusted || "source"}`, 24), full } };
  }
  if (ev.action === "critique_priority" && ev.status === "needs_revision") {
    const m = full.match(/["']([a-z0-9_]+)["']/i) || String(ev.input || "").match(/chosen["']?\s*:\s*["']([^"']+)/i);
    const routine = m ? m[1] : "ranking";
    return { ok: false, line: `Priority Critic rejected "${routine}" ranking → PM re-ranked`, full,
             edge: "PriorityCritic→PMAgent",
             reason: { short: `reject ${routine}`, full } };
  }
  if (ev.action === "critique_output" && ev.status === "needs_revision") {
    const fail = (full.match(/FAIL[^"]*/i) || [shortText(full, 60)])[0];
    const ver = (String(ev.input || "").match(/\bv(\d+)\b/i) || [])[1] || "?";
    return { ok: false, line: `QA rejected code v${ver}: ${shortText(fail, 70)} → Engineer revised`, full,
             edge: "QAAgent→EngineerAgent",
             reason: { short: shortText(fail, 24), full } };
  }
  if (ev.action === "critique_grounding" && ev.status === "needs_revision") {
    return { ok: false, line: `Grounding Critic: ${shortText(full, 80)} → Assistant revised`, full,
             edge: "GroundingCritic→AssistantAgent",
             reason: { short: shortText(full, 24), full } };
  }
  if (ev.action === "critique_spec" && ev.status === "needs_revision") {
    return { ok: false, line: `Spec Critic: ${shortText(full, 80)} → PM revised`, full,
             edge: "SpecCritic→PMAgent",
             reason: { short: shortText(full, 24), full } };
  }
  return null;
}
function challengesRender() {
  const list = $("challenges-list"), sum = $("challenges-sum");
  if (!list || !sum) return;
  list.innerHTML = "";
  const challenged = team.challenges.length;
  const dropped = team.challenges.filter((c) => /dropped claim|claim\(s\) dropped/i.test(c.line)).length;
  const revised = team.challenges.filter((c) => /revised|re-ranked/i.test(c.line)).length;
  if (!challenged) {
    sum.textContent = team.replay
      ? "Replay ready — scrub or press play to see critiques."
      : "No critiques yet — run the team or replay a past run.";
    return;
  }
  sum.textContent = `${challenged} claim${challenged === 1 ? "" : "s"} challenged · ${dropped} dropped · ${revised} revised.`;
  for (const c of team.challenges.slice(-8)) {
    const li = el("li", c.ok ? "ok" : "", esc(c.line));
    li.title = c.full || c.line;
    list.appendChild(li);
  }
}
function teamCaptureEvidence(ev) {
  if (ev.action === "write_report" || ev.action === "revise_report") {
    const draft = parseMaybeJson(ev.summary);
    if (draft && Array.isArray(draft.claims)) team.evidence.claims = draft.claims;
  }
  if (ev.action === "critique_claims") {
    const texts = parseMaybeJson(ev.input);
    if (Array.isArray(texts) && texts.length) {
      // Prefer richer claim objects from write_report; otherwise keep the critic's claim list.
      if (!team.evidence.claims?.length || !team.evidence.claims[0]?.evidence) {
        team.evidence.claims = texts.map((t) => (typeof t === "string" ? { text: t, evidence: [] } : t));
      }
    }
  }
  if (ev.action === "critique_sources") {
    const assessments = parseMaybeJson(ev.summary);
    const conflicts = parseMaybeJson(ev.input);
    if (Array.isArray(assessments) || Array.isArray(conflicts)) {
      team.evidence.conflicts = {
        assessments: Array.isArray(assessments) ? assessments : [],
        conflicts: Array.isArray(conflicts) ? conflicts : [],
      };
    }
  }
}
function teamOnEvent(ev, replay) {
  if (!ev || !ev.agent) return;
  sideActivity();
  if (ev.agent === "SourceMapperAgent" || ev.agent === "ResearchPlannerAgent" || ev.agent === "ConflictCriticAgent") {
    teamSetMode("analysis");
  } else if (ev.agent === "AssistantAgent" || ev.agent === "GroundingCritic"
             || (ev.action === "route" && /"?intent"?\s*[:=]\s*"?'?(email|question)/.test(String(ev.summary || "")))) {
    teamSetMode("assist");
  }
  if (ev.model && ev.model !== "offline-fallback") {
    team.models[ev.agent] = shortModel(ev.model);
    if (ev.provider) team.providers[ev.agent] = shortProvider(ev.provider);
  } else if (ev.model === "offline-fallback") {
    if (!team.models[ev.agent]) team.models[ev.agent] = "offline-fallback";
  } else if (ev.agent === "QAAgent" && !team.models[ev.agent]) {
    team.models[ev.agent] = "reference runtime";
  } else if (CRITIC_ACTIONS.has(ev.action) && !team.models[ev.agent]) {
    team.models[ev.agent] = "rules";
  }
  if (ev.action === "data_query") {
    const z = ev.target === "Zetaris" || String(ev.data_source || "").toLowerCase() === "zetaris";
    team.sources[ev.agent] = z ? "Zetaris" : (team.sources[ev.agent] || "sample");
  }

  teamCaptureEvidence(ev);
  const ch = challengeFromEvent(ev);
  if (ch) {
    team.challenges.push(ch);
    if (ch.edge && ch.reason) teamLitEdge(ch.edge.split("→")[0], ch.edge.split("→")[1], true, ch.reason);
    challengesRender();
  }

  const id = ev.agent === "ManagerAgent" && ev.action === "request_approval" ? "Human" : ev.agent;
  if (team.nodes[ev.agent] || id === "Human") {
    if (team.working && team.working !== ev.agent && team.nodes[team.working]) {
      const line = teamAttemptLine(team.working);
      teamSetNode(team.working, "done", line || "done");
    }
    if (ev.action !== "cell_writes" && ev.action !== "audit_sheet" && ev.action !== "run_completed") {
      if (team.nodes[ev.agent]) {
        team.working = ev.agent;
        const attempt = teamAttemptLine(ev.agent);
        teamSetNode(ev.agent, "working", attempt || ACTIONS[ev.action] || ev.action);
      }
      const warn = CRITIC_ACTIONS.has(ev.action) && (ev.status === "needs_revision" || ev.status === "needs_attention");
      teamLitEdge(ev.agent, ev.target, warn, ch && ch.edge === `${ev.agent}→${ev.target}` ? ch.reason : null);
    }
  }
  team.last[ev.agent] = ev;
  if (CRITIC_ACTIONS.has(ev.action) && ev.target) {
    const m = String(ev.input || "").match(/\bv(\d+)\b/i);
    const v = m ? Number(m[1]) : (ev.retry || 0) + 1;
    const ok = ev.status === "approved";
    const score = (ev.cases != null && ev.cases_matched != null) ? `${ev.cases_matched}/${ev.cases}` : null;
    if (ev.action === "critique_output") {
      (team.attempts[ev.target] || (team.attempts[ev.target] = [])).push({ v, ok, score });
      const byV = {};
      for (const x of team.attempts[ev.target]) byV[x.v] = x;
      team.attempts[ev.target] = Object.keys(byV).sort((a, b) => a - b).map((k) => byV[k]);
      if (team.nodes[ev.target]) teamSetNode(ev.target, ok ? "done" : "rejected", teamAttemptLine(ev.target));
    }
  }
  if (ev.action === "request_approval") {
    team.codeHash = ev.code_hash || (String(ev.summary || "").match(/code\s+([a-f0-9]{8,12})/i) || [])[1] || null;
    teamAwaitApproval(team.codeHash);
  }
  if (ev.action === "run_completed" || ev.action === "run_automation") {
    if (team.working && team.nodes[team.working]) teamSetNode(team.working, "done", teamAttemptLine(team.working) || "done");
    team.working = null;
  }
}
function teamFinish() {
  if (team.working && team.nodes[team.working]) {
    teamSetNode(team.working, "done", teamAttemptLine(team.working) || "done");
  }
  team.working = null;
  for (const id of Object.keys(team.nodes)) {
    if (id === "Human") continue;
    const n = team.nodes[id];
    if (n.el.classList.contains("working")) teamSetNode(id, "done", teamAttemptLine(id) || "done");
  }
  challengesRender();
}
function teamSetMode(mode) {
  if (team.mode === mode && Object.keys(team.nodes).length) return;
  team.mode = LAYOUTS[mode] ? mode : "task";
  teamDraw();
}
function teamReset(mode) {
  team.mode = LAYOUTS[mode] ? mode : "task";
  team.runId = null; team.attempts = {}; team.last = {}; team.working = null; team.codeHash = null;
  team.models = {}; team.providers = {}; team.sources = {}; team.challenges = []; team.edgeMeta = {};
  team.evidence = { claims: [], conflicts: [] };
  teamDraw();
  challengesRender();
}
function teamSetRunId(id) { team.runId = id; }
function teamAwaitApproval(hash) {
  team.codeHash = hash || team.codeHash;
  const sub = team.codeHash ? `waiting · ${String(team.codeHash).slice(0, 12)}` : "waiting";
  teamSetNode("Human", "wait", sub);
}
function teamApprove(hash) {
  team.codeHash = hash || team.codeHash;
  teamSetNode("Human", "done", team.codeHash ? `approved · ${String(team.codeHash).slice(0, 12)}` : "approved");
}
function teamReject() { teamSetNode("Human", "rejected", "rejected"); }
function teamShowEdge(key) {
  const meta = team.edgeMeta[key];
  const pop = $("edge-pop");
  if (!pop) return;
  if (!meta || !meta.full) { pop.hidden = true; return; }
  pop.innerHTML = `<button type="button" class="pop-close" aria-label="Close">✕</button>
    <b>Loop</b><div>${esc(key.replace("→", " → "))}</div>
    <b>Reason</b><div class="pop-note">${esc(meta.short || "")}</div>
    <b>Full</b><pre style="white-space:pre-wrap;font-size:11px;max-height:160px;overflow:auto">${esc(meta.full)}</pre>`;
  pop.hidden = false;
  pop.querySelector(".pop-close").addEventListener("click", () => { pop.hidden = true; });
  pop.style.left = "12px";
  pop.style.top = "72px";
}
function evidenceHtml(id) {
  if (id === "ReportWriterAgent" && team.evidence.claims?.length) {
    return `<div class="evidence-block"><b>Track 2 · findings with SQL evidence</b>` +
      team.evidence.claims.slice(0, 4).map((c, i) => {
        const text = typeof c === "string" ? c : (c.text || "");
        const ev = (c && c.evidence) || [];
        const rows = Array.isArray(ev) ? ev : [];
        return `<div style="margin-top:6px"><button type="button" class="btn" data-claim="${i}" style="padding:4px 8px;font-size:11px">Finding ${i + 1}</button>
          <div class="muted">${esc(shortText(text, 120))}</div>
          <div class="claim-rows" id="claim-rows-${i}" hidden></div></div>`;
      }).join("") + `</div>`;
  }
  if (id === "ConflictCriticAgent" && team.evidence.conflicts) {
    const { assessments = [], conflicts = [] } = team.evidence.conflicts;
    const rows = (conflicts.length ? conflicts : assessments).slice(0, 4);
    if (!rows.length) return "";
    return `<div class="evidence-block"><b>Track 1 · disagreeing sources</b>` +
      rows.map((c) => {
        const task = c.task || "?";
        const values = c.values || {};
        const trust = c.trusted_source ? `<div class="muted">Trusted: ${esc(c.trusted_source)}</div>` : "";
        const vals = Object.keys(values).length
          ? `<table><tbody>${Object.entries(values).map(([k, v]) =>
            `<tr><td>${esc(k)}</td><td class="num">${esc(v)}</td></tr>`).join("")}</tbody></table>`
          : `<div class="muted">${esc(shortText(c.explanation || "", 160))}</div>`;
        return `<div style="margin-top:6px"><b>${esc(task)}</b>${trust}${vals}</div>`;
      }).join("") + `</div>`;
  }
  return "";
}
function teamShowDetail(id) {
  const panel = $("agent-detail"), body = $("agent-detail-body");
  if (!panel || !body) return;
  const ev = team.last[id] || (id === "Human" ? team.last.ManagerAgent : null);
  const title = id === "Human" ? "Approval" : agentName(id);
  const attempt = teamAttemptLine(id === "Human" ? "EngineerAgent" : id);
  const model = team.models[id] || (ev?.model ? shortModel(ev.model) : null)
    || (id === "QAAgent" ? "reference runtime" : "—");
  const provider = team.providers[id] || (ev?.provider ? shortProvider(ev.provider) : null);
  const source = team.sources[id] || (ev?.data_source === "zetaris" || ev?.target === "Zetaris" ? "Zetaris" : null);
  const sql = ev?.executed_sql ? shortText(ev.executed_sql, 220) : null;
  body.innerHTML = `<h4>${esc(title)}</h4>` +
    (attempt ? `<p class="muted">${esc(attempt)}</p>` : "") +
    (team.codeHash && id === "Human" ? `<p class="muted">code ${esc(String(team.codeHash).slice(0, 12))}</p>` : "") +
    `<div class="detail-block"><b>Model</b><pre>${esc(model)}</pre></div>` +
    (provider ? `<div class="detail-block"><b>Provider</b><pre>${esc(provider)}</pre></div>` : "") +
    (source ? `<div class="detail-block"><b>Data</b><pre>${esc(source)}</pre></div>` : "") +
    (sql ? `<div class="detail-block"><b>SQL</b><pre>${esc(sql)}</pre></div>` : "") +
    (ev ? `<div class="detail-block"><b>Latest input</b><pre>${esc(ev.input || "—")}</pre></div>
      <div class="detail-block"><b>Latest output</b><pre>${esc(ev.summary || "—")}</pre></div>` :
      `<p class="muted">No events yet for this agent.</p>`) +
    evidenceHtml(id);
  panel.hidden = false;
  body.querySelectorAll("[data-claim]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const i = Number(btn.dataset.claim);
      const box = body.querySelector(`#claim-rows-${i}`);
      const claim = team.evidence.claims[i];
      if (!box || !claim) return;
      if (!box.hidden) { box.hidden = true; return; }
      const evd = claim.evidence || [];
      box.innerHTML = Array.isArray(evd) && evd.length
        ? `<pre style="white-space:pre-wrap;font-size:11px">${esc(JSON.stringify(evd, null, 1))}</pre>`
        : `<span class="muted">No SQL row citations on this claim in the saved event.</span>`;
      box.hidden = false;
    });
  });
}
function teamCloseDetail(e) {
  e?.preventDefault?.();
  e?.stopPropagation?.();
  const panel = $("agent-detail");
  if (panel) panel.hidden = true;
}
$("agent-detail-close")?.addEventListener("click", teamCloseDetail);
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("agent-detail") && !$("agent-detail").hidden) {
    // Prefer closing the side panel before collapsing the island.
    e.stopPropagation();
    teamCloseDetail(e);
  }
}, true);

function sheetStatus() {
  const el = $("sheet-status");
  if (!el) return;
  const parts = [];
  if (sheet.changed) parts.push(`${sheet.changed} cells changed or added`);
  if (sheet.audit) parts.push("audit sheet written");
  el.textContent = parts.join(" · ") || (sheet.meta ? "original preview" : "No sheet attached");
}
function sheetCellClass(key, value) {
  const meta = sheet.cellMeta[key];
  const parts = [];
  if (meta?.kind === "blank_by_design") parts.push("blank-design", "has-meta");
  else if (sheet.cellMap[key]) parts.push("changed", "has-meta");
  else if (meta) parts.push("has-meta");
  if (typeof value === "number") parts.push("num");
  return parts.join(" ");
}
function sheetCellTitle(key, value) {
  const meta = sheet.cellMeta[key];
  if (meta?.note) return meta.note;
  if (meta?.source) return meta.source;
  return fmt(value);
}
function sheetHidePop() { const p = $("cell-pop"); if (p) p.hidden = true; }
function sheetShowPop(key, anchor) {
  const pop = $("cell-pop");
  const meta = sheet.cellMeta[key];
  if (!pop || !meta) return;
  const oldText = meta.old === undefined || meta.old === null || meta.old === "" ? "∅" : fmt(meta.old);
  const newText = meta.value === undefined || meta.value === null || meta.value === "" ? "∅" : fmt(meta.value);
  // "moved" cells only redraw the output's row order; "written"/"added" cells have no input value to compare.
  const change = meta.kind === "moved" ? `<b>Moved</b><div class="pop-change">${esc(newText)}</div>`
    : meta.kind === "written" || meta.kind === "added" ? `<b>Written</b><div class="pop-change">${esc(newText)}</div>`
    : `<b>Change</b><div class="pop-change">${esc(oldText)} → ${esc(newText)}</div>`;
  pop.innerHTML = `<button type="button" class="pop-close" aria-label="Close">✕</button>` + change +
    (meta.source_row && meta.kind !== "moved" ? `<b>Row</b><div>row ${esc(meta.source_row)} of your file</div>` : "") +
    (meta.step_label || meta.step ? `<b>Plan</b><div>${esc(meta.step_label || ("Plan step " + meta.step + (meta.step_op ? ": " + meta.step_op : "")))}</div>` : "") +
    (meta.source ? `<b>Source</b><div>${esc(meta.source)}</div>` : "") +
    (meta.note ? `<b>Note</b><div class="pop-note">${esc(meta.note)}</div>` : "") +
    (meta.code_hash ? `<b>Code</b><div class="pop-change">${esc(meta.code_hash)}</div>` : "");
  pop.hidden = false;
  pop.querySelector(".pop-close").addEventListener("click", sheetHidePop);
  const host = $("view-sheet");
  const hr = host.getBoundingClientRect();
  const ar = anchor.getBoundingClientRect();
  let left = ar.left - hr.left + host.scrollLeft;
  let top = ar.bottom - hr.top + host.scrollTop + 6;
  pop.style.left = Math.min(left, host.clientWidth - 220) + "px";
  pop.style.top = Math.min(top, host.clientHeight - 40) + "px";
}
function sheetRender() {
  const host = $("sheet-grid"), tabs = $("sheet-tabs");
  if (!host || !tabs) return;
  sheetHidePop();
  tabs.innerHTML = "";
  const names = Object.keys(sheet.data);
  if (!names.length) {
    host.innerHTML = `<p class="muted" style="padding:12px">Attach a spreadsheet to preview it here.</p>`;
    sheetStatus();
    return;
  }
  if (!sheet.active || !sheet.data[sheet.active]) sheet.active = names[0];
  for (const name of names) {
    const b = el("button", "sheet-tab", esc(name));
    b.type = "button"; b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(name === sheet.active));
    b.addEventListener("click", () => { sheet.active = name; sheetRender(); });
    tabs.appendChild(b);
  }
  const cols = sheet.columns[sheet.active] || [];
  const rows = sheet.data[sheet.active] || [];
  const total = sheet.totals[sheet.active] ?? rows.length;
  const shown = rows.slice(0, 200);
  // Mirror the grid like an Arabic Excel sheet only when most headers are Arabic; a mostly English sheet
  // with an Arabic column stays left-to-right (those cells are right-aligned on their own below).
  applyRtl(host, cols.filter((c) => hasArabic(c)).length * 2 > cols.length ? "ع" : "");
  let html = `<table><thead><tr><th class="row-num">#</th>${cols.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>`;
  for (let i = 0; i < shown.length; i++) {
    const r = shown[i];
    html += `<tr><td class="row-num">${i + 1}</td>`;
    for (const c of cols) {
      const key = `${sheet.active}|${i + 1}|${c}`;
      const cls = sheetCellClass(key, r[c]);
      const cellText = fmt(r[c]);
      const dirAttr = hasArabic(cellText) ? ' dir="rtl" lang="ar"' : "";
      html += `<td data-cell="${esc(key)}" class="${cls}" title="${esc(sheetCellTitle(key, r[c]))}"${dirAttr}>${esc(cellText)}</td>`;
    }
    html += `</tr>`;
  }
  html += `</tbody></table>`;
  if (total > shown.length) html += `<div class="sheet-more">+${total - shown.length} more rows</div>`;
  host.innerHTML = html;
  host.querySelectorAll("td.has-meta, td.blank-design").forEach((td) => {
    td.addEventListener("click", (e) => {
      e.stopPropagation();
      sheetShowPop(td.getAttribute("data-cell"), td);
    });
  });
  sheetStatus();
}
function sheetLoad(meta) {
  sheet.meta = meta; sheet.changed = 0; sheet.audit = false;
  sheet.cellMap = {}; sheet.cellMeta = {};
  sheet.data = {}; sheet.columns = {}; sheet.totals = {};
  $("sheet-replay").hidden = true;
  sheetHidePop();
  if (!meta) { sheet.active = null; sheetRender(); return; }
  const cols = meta.columns || {};
  const rows = meta.rows || {};
  const totals = meta.row_totals || {};
  const names = meta.sheets ? meta.sheets.map((s) => s.name) : Object.keys(rows);
  for (const name of names) {
    sheet.columns[name] = cols[name] || (rows[name] && rows[name][0] ? Object.keys(rows[name][0]) : []);
    sheet.data[name] = (rows[name] || []).map((r) => ({ ...r }));
    sheet.totals[name] = totals[name] != null ? totals[name] : sheet.data[name].length;
  }
  sheet.active = names[0] || null;
  sheetRender();
}
function sheetApplyCells(cells, flash) {
  if (!cells || !cells.length) return;
  sideActivity();
  for (const c of cells) {
    const sheetName = c.sheet || sheet.active;
    if (!sheetName) continue;
    if (!sheet.data[sheetName]) {
      sheet.data[sheetName] = [];
      sheet.columns[sheetName] = sheet.columns[sheetName] || [];
      sheet.totals[sheetName] = sheet.totals[sheetName] || 0;
    }
    if (c.col && !sheet.columns[sheetName].includes(c.col)) sheet.columns[sheetName].push(c.col);
    const idx = (c.row || 1) - 1;
    while (sheet.data[sheetName].length <= idx) sheet.data[sheetName].push({});
    sheet.data[sheetName][idx][c.col] = c.value;
    const key = `${sheetName}|${c.row}|${c.col}`;
    sheet.cellMeta[key] = { ...sheet.cellMeta[key], ...c, sheet: sheetName };
    // Live tally until run_automation reports the authoritative count; only real edits count.
    if (c.kind === "changed" && !sheet.cellMap[key]) sheet.changed += 1;
    if (c.kind !== "moved") sheet.cellMap[key] = true;
  }
  const stay = sheet.active;
  sheetRender();
  sheet.active = stay;
  if (flash) {
    for (const c of cells) {
      const key = `${c.sheet || sheet.active}|${c.row}|${c.col}`;
      const td = $("sheet-grid")?.querySelector(`[data-cell="${CSS.escape(key)}"]`);
      if (td && c.kind !== "moved") {
        td.classList.add("flash");
        if (c.kind === "blank_by_design") td.classList.add("blank-design");
        else td.classList.add("changed");
      }
    }
  }
  sheetStatus();
}
function sheetSetRows(name, rows) {
  // filter/dedupe can leave fewer output rows than the file had.
  if (!name || rows == null) return;
  if (!sheet.data[name]) { sheet.data[name] = []; sheet.columns[name] = sheet.columns[name] || []; }
  if (sheet.data[name].length > rows) sheet.data[name].length = rows;
  sheet.totals[name] = rows;
}
function sheetOnEvent(ev, replay) {
  if (!ev) return;
  if (ev.action === "cell_writes" && ev.cells) {
    sheetSetRows(ev.sheet, ev.rows_out);
    const cells = ev.cells.map((c) => ({ ...c, sheet: c.sheet || ev.sheet }));
    sheetApplyCells(cells, !replay);
    if (ev.sheet && sheet.data[ev.sheet]) sheet.active = ev.sheet;
  }
  if (ev.action === "audit_sheet" || ev.audit_written) {
    sheet.audit = true; sheetStatus();
  }
  if (ev.action === "run_automation" && ev.cells_changed != null) {
    sheet.changed = ev.cells_changed;
    sheetStatus();
  }
}
function sheetShowOutput(writes, runId, cellsChanged, layout) {
  // The finished run's cells with real values (trace events mask personal-looking columns).
  const banner = $("sheet-replay");
  if (banner) {
    banner.hidden = false;
    banner.textContent = `Output of run ${runId || "unknown"}`;
  }
  selectSideTab("sheet");
  if (layout) sheetSetRows(layout.sheet, layout.rows);
  if (Array.isArray(writes) && writes.length) {
    // Animate in small batches so the flash is visible even on a sync response.
    let i = 0;
    const step = () => {
      const batch = writes.slice(i, i + 40);
      i += 40;
      sheetApplyCells(batch, true);
      if (i < writes.length) setTimeout(step, 40);
      else {
        if (cellsChanged != null) sheet.changed = cellsChanged;
        sheet.audit = true;
        sheetStatus();
      }
    };
    step();
  } else {
    if (cellsChanged != null) sheet.changed = cellsChanged;
    sheet.audit = true;
    sheetStatus();
  }
}

function selectSideTab(which) {
  const teamTab = $("tab-team"), sheetTab = $("tab-sheet");
  const teamView = $("view-team"), sheetView = $("view-sheet");
  const isTeam = which !== "sheet";
  teamTab.setAttribute("aria-selected", String(isTeam));
  sheetTab.setAttribute("aria-selected", String(!isTeam));
  teamTab.tabIndex = isTeam ? 0 : -1;
  sheetTab.tabIndex = isTeam ? -1 : 0;
  teamView.hidden = !isTeam;
  sheetView.hidden = isTeam;
  if (isTeam) refitCaptions();
}

function toUiEvent(e) {
  // Live job events are already flattened (agent/summary); replay traces use agent_name/output_summary.
  const summary = e.summary != null ? e.summary : e.output_summary;
  const input = e.input != null ? e.input : e.input_summary;
  return {
    i: e.i, at: e.at || e.timestamp, agent: e.agent || e.agent_name, action: e.action, status: e.status,
    target: e.target || e.target_agent, model: e.model,
    provider: e.provider, data_source: e.data_source, purpose: e.purpose, executed_sql: e.executed_sql,
    summary: typeof summary === "string" ? summary : (summary == null ? "" : JSON.stringify(summary)),
    input: typeof input === "string" ? input : (input == null ? "" : JSON.stringify(input)),
    retry: e.retry != null ? e.retry : (e.retry_count || 0), confidence: e.confidence,
    cases: e.cases, cases_matched: e.cases_matched, cells: e.cells, sheet: e.sheet,
    rows_out: e.rows_out, cells_changed: e.cells_changed,
    code_hash: e.code_hash, audit_written: e.audit_written,
  };
}
function replaySponsorLabel(sponsors, runId) {
  if (!sponsors) return `Replay of run ${runId || "saved"}`;
  const bits = ["Replay"];
  if (sponsors.meterless) bits.push("Meterless Nightshift");
  const m = (sponsors.models || []).find((x) => x && x !== "offline-fallback");
  if (m) bits.push(shortModel(m));
  if (sponsors.zetaris) bits.push("Zetaris");
  if (bits.length === 1) bits.push(`run ${runId || "saved"}`);
  if (sponsors.chainIntact === false) bits.push("trace edited: hash chain broken");
  return bits.join(" · ");
}
function replayStop() {
  if (team.replayTimer) { clearTimeout(team.replayTimer); team.replayTimer = null; }
}
function replayApplyThrough(index) {
  const mode = team.replayMode || "analysis";
  teamReset(mode);
  team.replay = true;
  const banner = $("replay-label");
  if (banner) {
    banner.hidden = false;
    banner.textContent = replaySponsorLabel(team.sponsors, team.replayRunId);
  }
  for (let i = 0; i <= index && i < team.replayEvents.length; i++) {
    const ui = toUiEvent(team.replayEvents[i]);
    teamOnEvent(ui, true);
    if (ui.action === "cell_writes") sheetOnEvent(ui, true);
  }
  team.replayIndex = index;
  const scrub = $("replay-scrub");
  if (scrub) scrub.value = String(index);
  teamFinish();
}
function replayPlay() {
  replayStop();
  if (!team.replayEvents.length) return;
  team.replay = true;
  const banner = $("replay-label");
  const label = () => replaySponsorLabel(team.sponsors, team.replayRunId);   // teamReset() clears team.runId
  if (banner) {
    banner.hidden = false;
    banner.textContent = label();
  }
  selectSideTab("team");
  const step = () => {
    if (team.replayIndex >= team.replayEvents.length - 1) {
      teamFinish();
      return;
    }
    team.replayIndex += 1;
    const ui = toUiEvent(team.replayEvents[team.replayIndex]);
    // First event: reset canvas once.
    if (team.replayIndex === 0) {
      teamReset(team.replayMode || "analysis");
      team.replay = true;
      if (banner) { banner.hidden = false; banner.textContent = label(); }
    }
    teamOnEvent(ui, true);
    if (ui.action === "cell_writes") sheetOnEvent(ui, true);
    const scrub = $("replay-scrub");
    if (scrub) scrub.value = String(team.replayIndex);
    team.replayTimer = setTimeout(step, ui.action === "critique_output" || CRITIC_ACTIONS.has(ui.action) ? 700 : 220);
  };
  // Restart from beginning for a clean play.
  team.replayIndex = -1;
  teamReset(team.replayMode || "analysis");
  team.replay = true;
  if (banner) {
    banner.hidden = false;
    banner.textContent = label();
  }
  step();
}
async function loadReplayList() {
  const sel = $("replay-select");
  if (!sel) return;
  try {
    // Runs made on this machine, from their own traces in logs/.
    const data = await api("/traces");
    sel.innerHTML = "";
    for (const r of data.runs) {
      const opt = document.createElement("option");
      opt.value = r.id;
      opt.textContent = r.label;
      sel.appendChild(opt);
    }
    $("replay-btn").disabled = !sel.options.length;
    if (!sel.options.length) {
      const opt = document.createElement("option");
      opt.textContent = "No past runs yet";
      sel.appendChild(opt);
    }
  } catch (_) {
    sel.innerHTML = "<option>Replays unavailable</option>";
  }
}
async function startSelectedReplay() {
  const sel = $("replay-select");
  const id = sel?.value;
  if (!id) return;
  replayStop();
  const data = await api(`/traces/${encodeURIComponent(id)}`);
  team.replayEvents = data.events || [];
  team.replayMode = data.mode || "analysis";
  team.runId = team.replayRunId = data.run_id;
  team.sponsors = data.sponsors ? { ...data.sponsors, chainIntact: data.chain_intact } : null;
  team.replay = true;
  const scrub = $("replay-scrub");
  if (scrub) {
    scrub.disabled = false;
    scrub.min = "0";
    scrub.max = String(Math.max(0, team.replayEvents.length - 1));
    scrub.value = "0";
  }
  const banner = $("replay-label");
  if (banner) {
    banner.hidden = false;
    banner.textContent = replaySponsorLabel(team.sponsors, data.run_id);
  }
  replayPlay();
}
function initSideTabs() {
  const tabs = [$("tab-team"), $("tab-sheet")];
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => selectSideTab(tab.id === "tab-sheet" ? "sheet" : "team"));
    tab.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight" && e.key !== "Home" && e.key !== "End") return;
      e.preventDefault();
      const next = (e.key === "ArrowRight" || e.key === "End") ? $("tab-sheet") : $("tab-team");
      selectSideTab(next.id === "tab-sheet" ? "sheet" : "team");
      next.focus();
    });
  });
  selectSideTab("team");
  teamReset("task");
  sheetRender();
  loadReplayList();
  $("replay-btn")?.addEventListener("click", () => startSelectedReplay().catch((e) => bot(`Replay failed: ${esc(e.message)}`, "error")));
  $("replay-scrub")?.addEventListener("input", (e) => {
    replayStop();
    if (!team.replayEvents.length) return;
    replayApplyThrough(Number(e.target.value));
  });
}

// ------------------------------------------------------------------ start

async function init() {
  state.large = store("agentoffice.large") === "1";
  render();
  initSideTabs();
  try {
    state.config = await api("/config");
    const c = state.config;
    $("sample-toggle").checked = c.sample_mode;
    $("sample-toggle").disabled = c.forced_sample_mode || !c.default_mode_ready;
    const modeText = () => {
      if ($("sample-toggle").checked) {
        const liveHint = c.default_mode_ready
          ? "bundled data, no sponsor calls"
          : "bundled data · set Meterless Nightshift + Zetaris in .env for live";
        return `<span class="dot sample"></span>Sample mode · ${liveHint}`;
      }
      const model = (c.sponsors && c.sponsors.meterless && c.sponsors.meterless.model) || c.model;
      return `<span class="dot"></span>Live · ${esc(model)} via Meterless Nightshift · Zetaris`;
    };
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

