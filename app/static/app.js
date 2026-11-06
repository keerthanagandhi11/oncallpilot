// OnCallPilot operator dashboard. Plain JS, no build step. Polls the API every 2 seconds.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const state = { config: null, filter: "active", selected: null, incidents: [] };

// ---------- API ----------
function token() { try { return localStorage.getItem("ocp_token") || ""; } catch { return ""; } }
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}) };
  if (token()) headers.Authorization = `Bearer ${token()}`;
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) { askToken(); throw new Error("API token needed"); }
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const d = body && body.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map(x => x.msg).join("; ") : `HTTP ${res.status}`);
  }
  return body;
}
function askToken() {
  const dlg = $("#token-dialog");
  if (dlg.open) return;
  dlg.showModal();
  $("#token-form").onsubmit = () => { try { localStorage.setItem("ocp_token", $("#token-input").value.trim()); } catch {} boot(); };
}

// ---------- helpers ----------
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const time = d => d ? new Date(d).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
const ago = d => {
  const s = Math.round((Date.now() - new Date(d)) / 1000);
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.floor(s / 60)}m ago` : `${Math.floor(s / 3600)}h ago`;
};
const until = d => {
  const s = Math.max(0, Math.round((new Date(d) - Date.now()) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
};
const ACTIVE = new Set(["new", "notified", "calling", "waiting"]);
function toast(msg, err = false) { const t = $("#toast"); t.textContent = msg; t.classList.toggle("err", err); }

function override() {
  const num = id => { const v = $(id).value; return v === "" ? undefined : Number(v); };
  const o = {
    interval_minutes: num("#ov-interval"), initial_wait_minutes: num("#ov-wait"),
    primary_attempts: num("#ov-primary"), secondary_attempts: num("#ov-secondary"),
    email_dl: $("#ov-email").value || undefined,
  };
  return Object.values(o).some(v => v !== undefined) ? o : undefined;
}

// ---------- rendering ----------
function renderBadges() {
  const c = state.config;
  const b = [];
  if (c.demo_mode) b.push(`<span class="badge demo">Demo mode: mock ${Object.entries(c.providers).filter(([, v]) => v === "mock").map(([k]) => k).join(", ")}</span>`);
  if (c.time_scale !== 1) b.push(`<span class="badge demo">Time ×${c.time_scale}: 1 policy min = ${(60 / c.time_scale).toFixed(1)} s</span>`);
  b.push(`<span class="badge ${c.llm_enabled ? "on" : ""}">AI triage: ${c.llm_enabled ? "LLM" : "rules (no key)"}</span>`);
  $("#badges").innerHTML = b.join("");
}

function renderList() {
  const ul = $("#incidents");
  ul.innerHTML = state.incidents.map(i => `
    <li class="inc ${i.id === state.selected ? "sel" : ""}" data-id="${esc(i.id)}" tabindex="0">
      <div class="inc-top"><span class="inc-name">${esc(i.task_name)}</span><span class="st ${esc(i.status)}">${esc(i.status)}</span></div>
      <div class="inc-sum">${esc(i.summary || "")}</div>
      <div class="inc-meta"><span class="sev ${esc(i.severity)}">${esc(i.severity)}</span><span>${esc(i.team_id)}</span>
        ${i.servicenow_number ? `<span>${esc(i.servicenow_number)}</span>` : ""}<span>${ago(i.created_at)}</span>${ACTIVE.has(i.status) && i.next_action_at ? `<span>next step in ${until(i.next_action_at)}</span>` : ""}</div>
    </li>`).join("");
  $("#empty").classList.toggle("hidden", state.incidents.length > 0);
  ul.querySelectorAll(".inc").forEach(li => {
    const open = () => select(li.dataset.id);
    li.onclick = open; li.onkeydown = e => { if (e.key === "Enter") open(); };
  });
}

function ladder(d) {
  const levels = d.policy.levels || [];
  return levels.map((lvl, idx) => {
    const tries = d.attempts.filter(a => a.level_index === idx);
    const contact = d.oncall?.contacts?.[lvl.role];
    const dots = Array.from({ length: lvl.attempts }, (_, n) => {
      const a = tries[n];
      return `<span class="dot ${a ? esc(a.outcome) : ""}" title="${a ? `${esc(a.outcome)} at ${time(a.started_at)}` : "not called"}"></span>`;
    }).join("");
    const current = ACTIVE.has(d.status) && d.level_index === idx;
    return `<div class="rung ${current ? "current" : ""}">
      <span class="role">${esc(lvl.role)}</span>
      <span class="who">${contact ? `${esc(contact.name)} · ${esc(contact.phone)}` : "not on rota"}<br>${lvl.attempts}× every ${lvl.interval_minutes} min</span>
      <span class="dots">${dots}</span></div>`;
  }).join("");
}

function nextStep(d) {
  if (!ACTIVE.has(d.status)) {
    if (d.status === "acknowledged") return `Acknowledged by <b>${esc(d.acknowledged_by)}</b> via ${esc(d.ack_channel)}.`;
    if (d.status === "exhausted") return `<b>Nobody responded.</b> DL emailed. SysOps must follow up by hand.`;
    if (d.status === "grouped") return `Grouped under <a href="#/incident/${esc(d.parent_id)}">${esc(d.parent_id)}</a>: that escalation covers this team.`;
    return `Closed (${esc(d.status)}).`;
  }
  const role = d.policy.levels[d.level_index]?.role || "";
  const map = {
    new: "Triage, on-call lookup and Teams alert are being prepared…",
    notified: `Waiting for a Teams acknowledgement. First call to <b>${esc(role)}</b> in ${until(d.next_action_at)}.`,
    calling: `Calling <b>${esc(role)}</b> now (attempt ${d.attempt_no})…`,
    waiting: `Next call: <b>${esc(role)}</b> attempt ${d.attempt_no} in ${until(d.next_action_at)}.`,
  };
  return map[d.status];
}

function renderDetail(d) {
  const t = d.triage || {};
  const active = ACTIVE.has(d.status);
  const demoAnswer = active && d.status === "calling" && state.config.providers.voice === "mock";
  $("#detail").innerHTML = `
    <div class="d-head">
      <div><div class="d-title">${esc(d.task_name)}</div>
        <div class="d-sub">${esc(d.workflow_name || "")} · team ${esc(d.team_id)} · ${esc(d.source)} · ID ${esc(d.id)}${d.servicenow_number ? ` · ServiceNow <b>${esc(d.servicenow_number)}</b>` : ""}</div></div>
      <div><span class="sev ${esc(d.severity)}">${esc(d.severity)}</span> <span class="st ${esc(d.status)}">${esc(d.status)}</span></div>
    </div>
    <div class="next">${nextStep(d)}</div>
    <div class="actions">
      ${active ? `<button class="btn small ok" data-act="ack">Acknowledge</button>
        <button class="btn small" data-act="resolve">Resolve</button>
        <button class="btn small" data-act="cancel">Cancel</button>` : ""}
      ${demoAnswer ? `<button class="btn small" data-act="answer">📞 Simulate pick-up</button>` : ""}
    </div>
    <div class="section"><h3>Escalation ladder</h3><div class="ladder">${ladder(d)}</div>
      <p class="hint">DL email: ${esc(d.policy.email_dl.replace("_", " "))}${d.email_sent ? " · <b>sent</b>" : ""}</p></div>
    ${t.summary ? `<div class="section"><h3>${t.source === "llm" ? "AI triage" : "Triage (keyword rules)"} · ${Math.round((t.confidence || 0) * 100)}% confidence</h3>
      <div class="triage"><p><b>${esc(t.summary)}</b></p><p><b>Probable cause:</b> ${esc(t.probable_cause)}</p>
      <p><b>First steps:</b> ${esc(t.recommended_action)}</p><p class="hint">Category: ${esc(t.category)}</p></div></div>` : ""}
    <div class="section"><h3>Failure</h3><dl class="kv">
      <dt>Error</dt><dd>${esc(d.error_message || "-")}</dd><dt>Agent</dt><dd>${esc(d.agent || "-")}</dd>
      <dt>Exit code</dt><dd>${esc(d.exit_code || "-")}</dd><dt>Failed at</dt><dd>${esc(new Date(d.failed_at).toLocaleString())}</dd></dl>
      ${d.log_excerpt ? `<pre class="log">${esc(d.log_excerpt)}</pre>` : ""}</div>
    <div class="section"><h3>Teams, email and ServiceNow</h3><div class="notes">
      ${d.notifications.map(n => `<details class="note ${esc(n.status)}"><summary><span class="ch">${{ teams: "Teams", email: "Email", servicenow: "ServiceNow" }[n.channel] || esc(n.channel)}</span>
        ${esc(n.subject || n.kind)} <span class="hint">→ ${esc(n.target)} · ${time(n.created_at)} · ${esc(n.status)}</span></summary>
        <pre>${esc(n.error ? `ERROR: ${n.error}\n\n` : "")}${esc(n.body)}</pre></details>`).join("") || `<p class="hint">None yet.</p>`}
    </div></div>
    <div class="section"><h3>Timeline</h3><ul class="timeline">
      ${d.events.map(e => `<li class="${esc(e.type)}"><time>${time(e.at)}</time>${esc(e.message)}</li>`).join("")}</ul></div>`;
  $("#detail").querySelectorAll("[data-act]").forEach(b => b.onclick = () => act(d.id, b.dataset.act, b));
}

// ---------- actions ----------
async function act(id, what, btn) {
  btn.disabled = true;
  try {
    if (what === "answer") await api(`/api/demo/answer/${id}`, { method: "POST" });
    else await api(`/api/incidents/${id}/${what}`, { method: "POST", json: { by: "SysOps operator", reason: `${what} from dashboard` } });
    toast(`Done: ${what}`);
    await refresh();
  } catch (e) { toast(e.message, true); btn.disabled = false; }
}

async function select(id) {
  state.selected = id;
  location.hash = `#/incident/${id}`;
  renderList();
  try { renderDetail(await api(`/api/incidents/${id}`)); } catch (e) { toast(e.message, true); }
}

async function refresh() {
  try {
    state.incidents = await api(`/api/incidents?limit=100${state.filter ? `&status=${state.filter}` : ""}`);
    renderList();
    if (state.selected) renderDetail(await api(`/api/incidents/${state.selected}`));
  } catch (e) { if (!String(e.message).includes("token")) toast(e.message, true); }
}

function wireForms() {
  document.querySelectorAll(".tab").forEach(tab => tab.onclick = () => {
    document.querySelectorAll(".tab").forEach(t => { t.classList.toggle("active", t === tab); t.setAttribute("aria-selected", t === tab); });
    document.querySelectorAll(".tabpane").forEach(p => p.classList.toggle("hidden", p.dataset.pane !== tab.dataset.tab));
  });
  document.querySelectorAll(".chip").forEach(c => c.onclick = () => {
    document.querySelectorAll(".chip").forEach(x => x.classList.toggle("active", x === c));
    state.filter = c.dataset.filter; refresh();
  });

  $("#form-sim").onsubmit = async e => {
    e.preventDefault();
    try {
      const r = await api("/api/demo/failure", { method: "POST", json: { scenario: $("#scenario").value || null, override: override() } });
      toast(`Simulated failure of ${r.task_name} → team ${r.team_id}`);
      await refresh(); select(r.id);
    } catch (err) { toast(err.message, true); }
  };

  $("#form-manual").onsubmit = async e => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(e.target).entries());
    Object.keys(f).forEach(k => { if (f[k] === "") delete f[k]; });
    try {
      const r = await api("/api/incidents", { method: "POST", json: { ...f, override: override() } });
      toast(r.created ? `Alert raised for team ${r.team_id}` : "Already raised (duplicate)");
      e.target.reset(); await refresh(); select(r.id);
    } catch (err) { toast(err.message, true); }
  };

  $("#form-shot").onsubmit = async e => {
    e.preventDefault();
    const file = $("#shot-file").files[0];
    if (!file) return toast("Choose a screenshot first", true);
    const fd = new FormData(); fd.append("file", file);
    toast("Reading screenshot…");
    try {
      const x = await api("/api/incidents/extract", { method: "POST", body: fd });
      const form = $("#form-manual");
      for (const k of ["task_name", "workflow_name", "agent", "exit_code", "error_message"]) if (x[k]) form.elements[k].value = x[k];
      document.querySelector('.tab[data-tab="manual"]').click();
      toast(`Read ${x.task_name || "the screenshot"}${x.other_failed_tasks?.length ? ` (+${x.other_failed_tasks.length} more failed)` : ""}. Check the details, then raise the alert.`);
    } catch (err) { toast(err.message, true); }
  };
}

async function boot() {
  try {
    state.config = await api("/api/config");
  } catch (e) { toast(e.message, true); return; }
  renderBadges();
  $("#scenario").innerHTML = `<option value="">Random</option>` + state.config.scenarios.map(s => `<option>${esc(s)}</option>`).join("");
  $("#team-select").innerHTML = `<option value="">Auto (match task name)</option>` + state.config.teams.map(t => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
  if (!state.config.demo_mode) { $("[data-tab=sim]").classList.add("hidden"); $("[data-tab=manual]").click(); }
  const m = location.hash.match(/incident\/(\w+)/);
  if (m) state.selected = m[1];
  await refresh();
}

wireForms();
boot();
setInterval(refresh, 2000);
