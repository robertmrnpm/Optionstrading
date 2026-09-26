"use strict";
// Options Agents dashboard — vanilla JS, no build step.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (v, sign = false) => v == null ? "—" : (sign && v > 0 ? "+" : v < 0 ? "−" : "") + "$" + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
const money2 = (v) => v == null ? "—" : "$" + Number(v).toFixed(2);
const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const hhmm = (iso) => iso ? new Date(iso).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour: "2-digit", minute: "2-digit", hour12: false }) : "";

let TOKEN = null;
try { TOKEN = localStorage.getItem("optrader_token"); } catch (e) { /* storage unavailable */ }
let STATUS = null;
let seenPending = new Set();
let firstLoad = true;
const qtySel = {};  // user-chosen quantities survive re-renders

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  if (TOKEN) headers.Authorization = "Bearer " + TOKEN;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) { askToken(); throw new Error("unauthorized"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function askToken() {
  const t = prompt("Dashboard token (DASHBOARD_TOKEN from your .env):");
  if (t) { TOKEN = t.trim(); try { localStorage.setItem("optrader_token", TOKEN); } catch (e) {} location.reload(); }
}

function toast(msg, ms = 3500) {
  const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}

// ---------------- status / header ----------------
function renderStatus(s) {
  STATUS = s;
  const badge = $("#modeBadge");
  badge.textContent = s.mode.toUpperCase() + (s.mode === "sim" ? ` ${s.sim_speed}×` : "");
  badge.className = "badge " + s.mode;
  $("#clock").textContent = new Date(s.now).toLocaleTimeString("en-US", { timeZone: "America/New_York", hour12: false });
  $("#mktDot").className = "dot" + (s.market_open ? " open" : "");
  $$("#tradeMode button").forEach((b) => b.classList.toggle("active", b.dataset.mode === s.trade_mode));
  const kill = $("#killBtn"); kill.classList.toggle("on", s.kill_switch); kill.textContent = s.kill_switch ? "Kill switch ON" : "Kill switch";

  const banner = [];
  if (s.broker_live) banner.push("LIVE TRADING — real orders go to your Webull account.");
  if (s.live_auto_blocked) banner.push("Auto mode is running as Approve: set ALLOW_LIVE_AUTO_TRADING=true in .env to let agents trade real money on their own.");
  if (s.kill_switch) banner.push("Kill switch is ON — no new trades will be opened.");
  if (s.last_error) banner.push("Agent error: " + s.last_error);
  const b = $("#banner"); b.innerHTML = banner.map(esc).join("<br>"); b.classList.toggle("hidden", !banner.length);

  const a = s.account || {};
  $("#kEquity").textContent = money(a.equity);
  $("#kBp").textContent = a.buying_power != null ? `Buying power ${money(a.buying_power)} • ${s.broker}` : "";
  const t = s.today;
  $("#kPnl").textContent = money(t.total, true); $("#kPnl").className = "kpi-value " + cls(t.total);
  $("#kPnlSub").textContent = `realized ${money(t.realized, true)} • open ${money(t.unrealized, true)}`;
  const p = s.pdt;
  $("#kPdtLabel").textContent = s.account_type === "cash" ? "Settled cash available" : "Day trades left (PDT)";
  if (s.account_type === "cash") {
    $("#kPdt").textContent = money(s.available_funds); $("#kPdt").className = "kpi-value";
    $("#kPdtSub").textContent = "cash account • no PDT limit • sale proceeds settle next day";
  } else if (p.applies) {
    const left = Math.max(p.remaining_for_new_entries ?? 0, 0);
    $("#kPdt").textContent = `${left}`; $("#kPdt").className = "kpi-value " + (left === 0 ? "down" : left === 1 ? "warn" : "");
    $("#kPdtSub").textContent = `${p.used}/${p.max} used in rolling 5 days`;
  } else {
    $("#kPdt").textContent = "∞"; $("#kPdt").className = "kpi-value";
    $("#kPdtSub").textContent = p.applies === false ? "PDT limit not applicable" : "";
  }
  $("#kTrades").textContent = t.entries;
  $("#kTradesSub").textContent = `${t.wins}W / ${t.losses}L` + (t.consecutive_losses ? ` • ${t.consecutive_losses} loss streak` : "");
  if (t.profit_target) {
    const prog = Math.max(t.total, 0) / t.profit_target;
    $("#kGoal").textContent = `${money(t.total, true)} / ${money(t.profit_target)}`;
    $("#kGoal").className = "kpi-value " + (t.goal_reached ? "up" : "");
    const gb = $("#kGoalBar"); gb.style.width = Math.min(prog * 100, 100).toFixed(0) + "%"; gb.style.background = "var(--accent)";
    $("#kGoalSub").textContent = t.goal_reached ? "goal reached — no new trades today" : `${(prog * 100).toFixed(0)}% of goal`;
  } else {
    $("#kGoal").textContent = "off"; $("#kGoalSub").textContent = "set risk.daily_profit_target in Settings";
  }
  const lim = t.daily_loss_limit || 0;
  const used = lim ? Math.min(Math.max(-t.total, 0) / lim, 1) : 0;
  $("#kLoss").textContent = lim ? `${money(Math.max(-t.total, 0))} / ${money(lim)}` : "—";
  const bar = $("#kLossBar"); bar.style.width = (used * 100).toFixed(0) + "%";
  bar.style.background = used > 0.75 ? "var(--down)" : used > 0.4 ? "var(--warn)" : "var(--up)";

  const hot = s.hot_list || [];
  $("#hotCount").textContent = `${hot.length} symbols`;
  $("#hotTable tbody").innerHTML = hot.map((h) => `<tr>
    <td><b>${esc(h.symbol)}</b>${h.earnings ? ' <span class="chip">ER</span>' : ""}</td>
    <td class="r">${h.price != null ? h.price.toFixed(2) : "—"}</td>
    <td class="r ${cls(h.change_pct)}">${h.change_pct != null ? h.change_pct.toFixed(2) : "—"}</td>
    <td class="r ${h.rvol >= 2 ? "warn" : ""}">${h.rvol != null ? h.rvol.toFixed(1) + "×" : "—"}</td>
    <td>${h.above_vwap == null ? "—" : h.above_vwap ? '<span class="up">above</span>' : '<span class="down">below</span>'}</td>
    <td>${h.trend ? (h.trend === "up" ? '<span class="up">▲</span>' : '<span class="down">▼</span>') : "—"}</td></tr>`).join("");

  const uoa = s.uoa_hits || [];
  $("#uoa").innerHTML = uoa.length ? `<div class="table-wrap"><table class="tbl"><thead><tr><th>Time</th><th>Contract</th><th class="r">Vol</th><th class="r">OI</th><th class="r">Vol/OI</th><th class="r">Premium</th></tr></thead><tbody>` +
    uoa.map((u) => `<tr><td>${hhmm(u.ts)}</td><td>${esc(u.label)}</td><td class="r">${u.volume.toLocaleString()}</td><td class="r">${u.open_interest.toLocaleString()}</td><td class="r">${u.vol_oi}×</td><td class="r">${money(u.premium)}</td></tr>`).join("") +
    "</tbody></table></div>" : '<div class="empty">No unusual prints yet.</div>';

  $("#propHint").textContent = s.effective_trade_mode === "approval" ? "click Approve to send the order" :
    s.effective_trade_mode === "auto" ? "auto mode: agents execute within risk limits" : "alerts only: nothing is executed";
}

// ---------------- proposals ----------------
function propCard(p) {
  const c = p.contract, s = p.signal, x = p.exit_plan;
  const dir = s.direction === "bullish";
  const pending = p.status === "pending";
  const secsLeft = Math.max(0, Math.round((new Date(p.expires) - new Date(STATUS ? STATUS.now : Date.now())) / 1000 / (STATUS ? STATUS.sim_speed || 1 : 1)));
  const ai = p.ai_review ? `<div class="ai ${esc(p.ai_review.verdict)}"><b>AI analyst: ${esc(p.ai_review.verdict.toUpperCase())}</b> (${p.ai_review.confidence}%) — ${esc(p.ai_review.summary)}
      ${p.ai_review.risks?.length ? `<div class="muted">Risks: ${p.ai_review.risks.map(esc).join("; ")}</div>` : ""}
      ${p.ai_review.catalysts?.length ? `<div class="muted">Catalysts: ${p.ai_review.catalysts.map(esc).join("; ")}</div>` : ""}</div>`
    : (pending && STATUS?.ai_enabled ? '<div class="ai muted">AI analyst reviewing…</div>' : "");
  return `<div class="prop ${p.status}" data-id="${p.id}">
    <div class="prop-top">
      <div class="prop-title">BUY ${esc(c.underlying)} ${esc(c.expiry.slice(5))} ${c.strike}${c.right} <span class="muted">@ ${money2(p.limit_price)}</span></div>
      <div><span class="chip ${dir ? "bull" : "bear"}">${dir ? "▲ bullish" : "▼ bearish"}</span><span class="chip">${esc(s.strategy)}/${esc(s.meta?.setup || "")}</span><span class="chip">score ${s.score.toFixed(0)}</span><span class="chip status-${p.status}">${esc(p.status)}</span></div>
    </div>
    <div class="prop-grid">
      <div>Qty <b>${p.qty}</b> • cost <b>${money(p.est_cost)}</b></div>
      <div>Max loss plan <b class="down">${money(p.risk_dollars)}</b></div>
      <div>Stop <b>${money2(x.stop_price)}</b></div>
      <div>T1 <b>${money2(x.target1_price)}</b>${x.target1_qty ? ` ×${x.target1_qty}` : ""} • T2 <b>${money2(x.target2_price)}</b></div>
      <div>Δ ${(c.delta ?? 0).toFixed(2)} • IV ${((c.iv ?? 0) * 100).toFixed(0)}%</div>
      <div>Bid/ask ${c.bid.toFixed(2)}/${c.ask.toFixed(2)} • OI ${c.open_interest.toLocaleString()}</div>
    </div>
    <ul class="reasons">${s.reasons.slice(0, 4).map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
    ${p.risk_notes?.length ? `<div class="muted" style="font-size:12px">${p.risk_notes.map(esc).join(" • ")}</div>` : ""}
    ${p.status_reason ? `<div class="${p.status === "blocked" || p.status === "failed" ? "down" : "muted"}" style="font-size:12px">${esc(p.status_reason)}</div>` : ""}
    ${ai}
    ${pending ? `<div class="prop-actions">
        <div class="qty"><button data-q="-1" aria-label="fewer">−</button><span data-max="${p.qty}">${Math.min(qtySel[p.id] ?? p.qty, p.qty)}</span><button data-q="1" aria-label="more">+</button></div>
        <button class="btn up approve">Approve &amp; buy</button>
        <button class="btn ghost reject">Reject</button>
        <span class="countdown" data-expires="${p.expires}">expires in ${secsLeft}s</span></div>` : ""}
  </div>`;
}

async function loadProposals() {
  const list = await api("/api/proposals?limit=40");
  const visible = list.filter((p) => ["pending", "approved", "submitted", "alert", "filled", "blocked", "cancelled", "failed", "rejected", "expired"].includes(p.status)).slice(0, 12);
  const el = $("#proposals");
  el.innerHTML = visible.length ? visible.map(propCard).join("") : '<div class="empty">Agents are scanning. Proposals appear here.</div>';
  // new pending proposal alert (sound + browser notification)
  for (const p of list.filter((x) => x.status === "pending")) {
    if (!seenPending.has(p.id)) {
      seenPending.add(p.id);
      if (!firstLoad) alertNewProposal(p);
    }
  }
  firstLoad = false;
}

function alertNewProposal(p) {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator(); const g = ctx.createGain();
    o.frequency.value = 880; g.gain.value = 0.05; o.connect(g); g.connect(ctx.destination); o.start(); o.stop(ctx.currentTime + 0.15);
  } catch (e) {}
  if ("Notification" in window && Notification.permission === "granted") {
    new Notification(`Trade proposal: ${p.contract.underlying} ${p.contract.strike}${p.contract.right}`, { body: p.signal.reasons[0] || "" });
  }
}

$("#proposals").addEventListener("click", async (e) => {
  const card = e.target.closest(".prop"); if (!card) return;
  const id = card.dataset.id;
  const qEl = card.querySelector(".qty span");
  if (e.target.dataset.q && qEl) {
    const max = parseInt(qEl.dataset.max, 10);
    const q = Math.min(max, Math.max(1, parseInt(qEl.textContent, 10) + parseInt(e.target.dataset.q, 10)));
    qEl.textContent = q; qtySel[id] = q;
    return;
  }
  if (e.target.classList.contains("approve")) {
    e.target.disabled = true;
    try { await api(`/api/proposals/${id}/approve`, { method: "POST", body: JSON.stringify({ qty: parseInt(qEl.textContent, 10) }) }); toast("Order sent — working the entry"); }
    catch (err) { toast("Approve failed: " + err.message); }
    refreshSoon();
  } else if (e.target.classList.contains("reject")) {
    try { await api(`/api/proposals/${id}/reject`, { method: "POST", body: "{}" }); } catch (err) { toast(err.message); }
    refreshSoon();
  }
});

// ---------------- positions ----------------
async function loadPositions() {
  const d = await api("/api/positions");
  $("#positions").innerHTML = d.open.length ? `<div class="table-wrap"><table class="tbl"><thead><tr>
      <th>Contract</th><th class="r">Qty</th><th class="r">Entry</th><th class="r">Mark</th><th class="r">P&amp;L</th><th class="r">Stop</th><th class="r">T1/T2</th><th></th></tr></thead><tbody>` +
    d.open.map((p) => `<tr>
      <td><b>${esc(p.contract.underlying)} ${esc(p.contract.expiry.slice(5))} ${p.contract.strike}${p.contract.right}</b><div class="muted">${esc(p.strategy)} • ${hhmm(p.entry_time)}${p.status === "closing" ? " • closing…" : ""}</div></td>
      <td class="r">${p.qty}</td><td class="r">${money2(p.entry_price)}</td><td class="r">${money2(p.last_price)}</td>
      <td class="r ${cls(p.unrealized_pnl)}">${money(p.unrealized_pnl, true)}<div>${p.pnl_pct.toFixed(1)}%</div></td>
      <td class="r">${money2(p.stop_price)}</td><td class="r">${money2(p.exit_plan.target1_price)}${p.target1_done ? " ✓" : ""}<div>${money2(p.exit_plan.target2_price)}</div></td>
      <td><button class="btn sm danger close" data-id="${p.id}" ${p.status !== "open" ? "disabled" : ""}>Close</button></td></tr>`).join("") +
    "</tbody></table></div>" : '<div class="empty">No open positions.</div>';
  $("#closed").innerHTML = d.closed_today.length ? `<div class="table-wrap"><table class="tbl"><thead><tr><th>Contract</th><th class="r">Qty</th><th class="r">Entry</th><th>Exit(s)</th><th class="r">P&amp;L</th></tr></thead><tbody>` +
    d.closed_today.map((p) => `<tr><td>${esc(p.contract.underlying)} ${p.contract.strike}${p.contract.right} <span class="muted">${esc(p.strategy)}</span></td><td class="r">${p.initial_qty}</td><td class="r">${money2(p.entry_price)}</td>
      <td>${p.exits.map((x) => `${x.qty}@${x.price.toFixed(2)} <span class="muted">${esc(x.reason)}</span>`).join("<br>")}</td>
      <td class="r ${cls(p.realized_pnl)}">${money(p.realized_pnl, true)}</td></tr>`).join("") + "</tbody></table></div>"
    : '<div class="empty">Nothing closed yet today.</div>';
}

$("#positions").addEventListener("click", async (e) => {
  if (!e.target.classList.contains("close")) return;
  if (!confirm("Close this position at the market (marketable limit)?")) return;
  try { await api(`/api/positions/${e.target.dataset.id}/close`, { method: "POST" }); toast("Closing…"); } catch (err) { toast(err.message); }
  refreshSoon();
});

// ---------------- activity feed ----------------
function addEvent(ev, prepend = true) {
  const div = document.createElement("div");
  div.className = "ev " + ev.kind;
  div.innerHTML = `<time>${hhmm(ev.ts)}</time><span>${esc(ev.message)}</span>`;
  const feed = $("#feed");
  prepend ? feed.prepend(div) : feed.append(div);
  while (feed.children.length > 200) feed.lastChild.remove();
}

async function loadEvents() {
  const evs = await api("/api/events?limit=120");
  $("#feed").innerHTML = "";
  evs.forEach((e) => addEvent(e, false));
}

function connectStream() {
  const url = "/api/stream" + (TOKEN ? "?token=" + encodeURIComponent(TOKEN) : "");
  const es = new EventSource(url);
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data);
    addEvent(ev);
    if (["proposal", "ai_review", "fill", "position_opened", "position_exit", "order"].includes(ev.kind)) refreshSoon();
  };
  es.onerror = () => { /* browser auto-reconnects */ };
}

// ---------------- header controls ----------------
$("#tradeMode").addEventListener("click", async (e) => {
  const mode = e.target.dataset.mode; if (!mode) return;
  if (mode === "auto" && !confirm("Switch to AUTO? Agents will place trades on their own within your risk limits.")) return;
  try { const r = await api("/api/trade-mode", { method: "POST", body: JSON.stringify({ trade_mode: mode }) }); toast(`Trade mode: ${r.trade_mode}` + (r.effective !== r.trade_mode ? ` (running as ${r.effective})` : "")); }
  catch (err) { toast(err.message); }
  refreshSoon();
});
$("#killBtn").addEventListener("click", async () => {
  const on = !(STATUS && STATUS.kill_switch);
  let flatten = false;
  if (on) flatten = confirm("Kill switch ON: no new trades.\n\nAlso close ALL open positions now? (OK = close all, Cancel = keep them)");
  try { await api("/api/kill-switch", { method: "POST", body: JSON.stringify({ on, flatten }) }); } catch (err) { toast(err.message); }
  refreshSoon();
});
$("#flattenBtn").addEventListener("click", async () => {
  if (!confirm("Close ALL open positions now?")) return;
  try { const r = await api("/api/flatten", { method: "POST" }); toast(`Closing ${r.closing} position(s)`); } catch (err) { toast(err.message); }
});

// ---------------- tabs ----------------
$$(".tab").forEach((t) => t.addEventListener("click", () => {
  $$(".tab").forEach((x) => x.classList.toggle("active", x === t));
  $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === "tab-" + t.dataset.tab));
  if (t.dataset.tab === "journal") loadJournal();
  if (t.dataset.tab === "settings") loadSettings();
}));

// ---------------- journal ----------------
function statsTable(obj) {
  const rows = Object.entries(obj);
  if (!rows.length) return '<div class="empty">No closed trades yet.</div>';
  return `<div class="table-wrap"><table class="tbl"><thead><tr><th></th><th class="r">Trades</th><th class="r">Win%</th><th class="r">Net</th><th class="r">PF</th><th class="r">Expectancy</th></tr></thead><tbody>` +
    rows.map(([k, s]) => `<tr><td>${esc(k)}</td><td class="r">${s.trades}</td><td class="r">${s.win_rate}%</td><td class="r ${cls(s.net_pnl)}">${money(s.net_pnl, true)}</td><td class="r">${s.profit_factor ?? "—"}</td><td class="r ${cls(s.expectancy)}">${money(s.expectancy, true)}</td></tr>`).join("") +
    "</tbody></table></div>";
}

function equitySvg(points) {
  if (points.length < 2) return '<div class="empty">Need at least two closed trades for a curve.</div>';
  const W = 800, H = 220, P = 28;
  const ys = points.map((p) => p.pnl); const min = Math.min(0, ...ys), max = Math.max(0, ...ys);
  const x = (i) => P + (i / (points.length - 1)) * (W - 2 * P);
  const y = (v) => H - P - ((v - min) / (max - min || 1)) * (H - 2 * P);
  const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.pnl).toFixed(1)}`).join(" ");
  const last = ys[ys.length - 1];
  const color = last >= 0 ? "var(--up)" : "var(--down)";
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Equity curve">
    <line x1="${P}" x2="${W - P}" y1="${y(0)}" y2="${y(0)}" stroke="var(--border)" stroke-dasharray="4 4"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="2" vector-effect="non-scaling-stroke"/>
    <text x="${P}" y="16" fill="var(--muted)" font-size="12">${money(max, true)}</text>
    <text x="${P}" y="${H - 8}" fill="var(--muted)" font-size="12">${money(min, true)}</text>
    <text x="${W - P}" y="16" fill="${color}" font-size="13" text-anchor="end" font-weight="700">${money(last, true)}</text></svg>`;
}

async function loadJournal() {
  const j = await api("/api/journal?days=30");
  const o = j.stats.overall;
  $("#jKpis").innerHTML = [
    ["Net P&L", money(o.net_pnl, true), cls(o.net_pnl)], ["Trades", o.trades, ""], ["Win rate", o.win_rate + "%", ""],
    ["Profit factor", o.profit_factor ?? "—", ""], ["Expectancy / trade", money(o.expectancy, true), cls(o.expectancy)],
    ["Avg win / loss", `${money(o.avg_win)} / ${money(o.avg_loss)}`, ""], ["Max drawdown", money(o.max_drawdown), "down"],
  ].map(([l, v, c]) => `<div class="kpi"><div class="kpi-label">${l}</div><div class="kpi-value ${c}">${v}</div></div>`).join("");
  $("#equityChart").innerHTML = equitySvg(j.stats.equity_curve);
  $("#byStrategy").innerHTML = statsTable(j.stats.by_strategy);
  $("#byExit").innerHTML = statsTable(j.stats.by_exit_reason);
  $("#tradesTable").innerHTML = j.trades.length ? `<table class="tbl"><thead><tr><th>Date</th><th>Contract</th><th>Strategy</th><th class="r">Qty</th><th class="r">Entry</th><th>Exits</th><th class="r">P&amp;L</th></tr></thead><tbody>` +
    j.trades.map((p) => `<tr><td>${esc(p.entry_time.slice(0, 10))} ${hhmm(p.entry_time)}</td><td>${esc(p.contract.underlying)} ${esc(p.contract.expiry.slice(5))} ${p.contract.strike}${p.contract.right}</td><td>${esc(p.strategy)}</td><td class="r">${p.initial_qty}</td><td class="r">${money2(p.entry_price)}</td>
      <td>${p.exits.map((x) => `${x.qty}@${x.price.toFixed(2)} <span class="muted">${esc(x.reason)}</span>`).join("<br>")}</td><td class="r ${cls(p.realized_pnl)}">${money(p.realized_pnl, true)}</td></tr>`).join("") + "</tbody></table>"
    : '<div class="empty">No closed trades yet.</div>';
}

// ---------------- settings ----------------
const EDITABLE = ["execution", "risk", "exits", "contracts", "strategies", "ai", "notify"];
const ENUMS = { trade_mode: ["alerts", "approval", "auto"], account_type: ["margin", "cash"], effort: ["low", "medium", "high", "xhigh", "max"] };
let SETTINGS = null;

async function loadSettings() {
  SETTINGS = await api("/api/settings");
  $("#settingsForm").innerHTML = EDITABLE.map((sec) => `<fieldset><legend>${sec}</legend>` +
    Object.entries(SETTINGS[sec]).map(([k, v]) => {
      const id = `${sec}.${k}`; const label = k.replace(/_/g, " ");
      let input;
      if (typeof v === "boolean") input = `<input type="checkbox" name="${id}" ${v ? "checked" : ""}>`;
      else if (ENUMS[k]) input = `<select name="${id}">${ENUMS[k].map((o) => `<option ${o === v ? "selected" : ""}>${o}</option>`).join("")}</select>`;
      else if (typeof v === "number") input = `<input type="number" step="any" name="${id}" value="${v}">`;
      else if (Array.isArray(v)) return "";
      else input = `<input type="text" name="${id}" value="${esc(v)}">`;
      return `<label class="field"><span>${esc(label)}</span>${input}</label>`;
    }).join("") + "</fieldset>").join("");
}

$("#saveSettings").addEventListener("click", async (e) => {
  e.preventDefault();
  const patch = {};
  $$("#settingsForm [name]").forEach((el) => {
    const [sec, key] = el.name.split(".");
    const old = SETTINGS[sec][key];
    let v = el.type === "checkbox" ? el.checked : el.type === "number" ? parseFloat(el.value) : el.value;
    if (typeof old === "number" && Number.isInteger(old) && Number.isInteger(v) === false && !String(el.value).includes(".")) v = parseInt(el.value, 10);
    if (v !== old) (patch[sec] ||= {})[key] = v;
  });
  if (!Object.keys(patch).length) { $("#saveMsg").textContent = "No changes."; return; }
  try { await api("/api/settings", { method: "PATCH", body: JSON.stringify(patch) }); $("#saveMsg").textContent = "Saved."; loadSettings(); }
  catch (err) { $("#saveMsg").textContent = "Error: " + err.message; }
});

// ---------------- refresh loop ----------------
let refreshTimer = null;
function refreshSoon() { clearTimeout(refreshTimer); refreshTimer = setTimeout(refreshAll, 250); }
async function refreshAll() {
  try {
    const [s] = await Promise.all([api("/api/status"), loadProposals(), loadPositions()]);
    renderStatus(s);
  } catch (e) { if (e.message !== "unauthorized") console.warn(e); }
}

function tickCountdowns() {
  if (!STATUS) return;
  const speed = STATUS.sim_speed || 1;
  $$(".countdown").forEach((el) => {
    const left = Math.max(0, Math.round((new Date(el.dataset.expires) - new Date(STATUS.now)) / 1000 / speed));
    el.textContent = left > 0 ? `expires in ~${left}s` : "expired";
  });
}

(async function init() {
  const h = await fetch("/api/health").then((r) => r.json()).catch(() => ({}));
  if (h.auth_required && !TOKEN) askToken();
  if ("Notification" in window && Notification.permission === "default") {
    document.body.addEventListener("click", () => Notification.requestPermission(), { once: true });
  }
  await refreshAll();
  await loadEvents().catch(() => {});
  connectStream();
  setInterval(refreshAll, 2000);
  setInterval(tickCountdowns, 1000);
})();
