"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour12: false });

let S = null;              // latest snapshot
let modelInfo = null;
let whatif = null;         // {at, data}
let busy = false;

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  return r.json();
}

/* ------------------------------------------------------------ controls */
$$("#modeSeg button").forEach((b) => b.onclick = async () => {
  b.disabled = true;
  await api("/api/mode", { mode: b.dataset.mode });
  whatif = null; b.disabled = false; poll();
});
$("#autoHeal").onchange = (e) => api("/api/settings", { auto: e.target.checked });
$("#predictive").onchange = (e) => api("/api/settings", { predictive: e.target.checked });
$("#clearBtn").onclick = async () => { await api("/api/clear", {}); whatif = null; poll(); };
$("#approveBtn").onclick = async () => {
  const r = await api("/api/approve", {});
  if (!r.ok) alertBanner(r.error);
};
$("#whatifBtn").onclick = async () => {
  const code = $("#whatifSel").value;
  const r = await api("/api/whatif?code=" + encodeURIComponent(code));
  if (r.ok) { whatif = { at: Date.now(), data: r }; renderPlan(); } else alertBanner(r.error);
};
$("#rescanBtn").onclick = () => api("/api/ap/rescan", {});
$$("#tabs button").forEach((b) => b.onclick = () => {
  $$("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
  $$(".tab").forEach((x) => x.classList.toggle("on", x.id === "tab-" + b.dataset.tab));
  if (b.dataset.tab === "model") loadModel();
  render();
});

let bannerTimer = null;
function alertBanner(msg) {
  const el = $("#banner");
  el.textContent = msg; el.hidden = false;
  clearTimeout(bannerTimer);
  bannerTimer = setTimeout(() => { el.hidden = true; render(); }, 6000);
}

/* ------------------------------------------------------------ polling */
async function poll() {
  if (busy) return;
  busy = true;
  try { S = await api("/api/state"); render(); }
  catch (e) { /* server restarting */ }
  finally { busy = false; }
}
setInterval(poll, 1000);
poll();
window.addEventListener("resize", () => render());

function activeTab() { return ($("#tabs button.on") || {}).dataset?.tab || "overview"; }

function render() {
  if (!S) return;
  $("#ver").textContent = "v" + S.version;
  $$("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === S.mode));
  $("#autoHeal").checked = S.healer.auto;
  $("#predictive").checked = S.healer.predictive;
  $("#apsTab").hidden = S.mode !== "live";
  if (S.mode !== "live" && activeTab() === "aps") $("#tabs button").click();
  liveBanner();
  renderStatus(); renderPrediction(); renderHealer(); renderKpis(); drawChart();
  renderFaults(); renderIncidents(); renderLog();
  const t = activeTab();
  if (t === "twin") renderTwin();
  if (t === "xai") renderXai();
  if (t === "aps") renderAps();
}

function liveBanner() {
  const el = $("#banner");
  if (bannerTimer) return;
  if (S.mode === "live" && S.live) {
    const L = S.live;
    let msg = "";
    if (!L.windows) msg = "Live mode needs Windows - this server is not running on Windows, so real probes and actions are unavailable.";
    else if (L.location_blocked) msg = L.location_hint;
    else if (!L.admin) msg = "Running without Administrator rights: packet-loss, outage, DNS and adapter faults (and their fixes) are disabled. Start NetSage with Start-NetSage.bat to enable them.";
    el.textContent = msg; el.hidden = !msg;
  } else el.hidden = true;
}

/* ------------------------------------------------------------ overview */
function renderStatus() {
  const d = S.diagnosis;
  if (!d) return;
  const card = $("#statusCard");
  card.classList.remove("ok", "warn", "crit"); card.classList.add(d.severity);
  const pill = $("#statusPill");
  pill.className = "pill " + d.severity; pill.textContent = d.code.replace("_", " ");
  $("#statusTitle").textContent = d.title;
  $("#statusMeaning").textContent = d.meaning;
  $("#confVal").textContent = (d.confidence * 100).toFixed(1) + "%";
  $("#evidence").innerHTML = d.evidence.map((e) => `<li>${esc(e)}</li>`).join("");
}

function renderPrediction() {
  const p = S.prediction || {};
  const r = Math.round((p.risk || 0) * 100);
  const el = $("#riskVal");
  el.textContent = r + "%"; el.classList.toggle("hot", r >= 70);
  $("#riskBar").style.width = r + "%";
  $("#riskBar").style.background = r >= 70 ? "var(--crit)" : "var(--gold)";
  $("#horizon").textContent = "· next ~15 samples";
  const healthy = S.diagnosis && S.diagnosis.code === "HEALTHY";
  $("#riskForecast").textContent = !healthy ? "Fault in progress" : (r >= 70 ? `${p.forecast_title} expected` : (r >= 35 ? `Watching: ${p.forecast_title || "-"}` : "No fault expected"));
  $("#riskDrivers").textContent = (p.drivers || []).length ? "Drivers: " + p.drivers.map((x) => x.label).join(", ") : "";
}

function renderHealer() {
  const h = S.healer;
  const names = { MONITORING: "Monitoring", VERIFYING: "Verifying action", AWAITING_APPROVAL: "Awaiting approval", ESCALATED: "Escalated to operator" };
  $("#phase").textContent = (names[h.phase] || h.phase).toUpperCase();
  const inc = h.incident || h.incidents[0];
  const ol = $("#incidentSteps");
  if (!inc) ol.innerHTML = `<li class="muted">No incidents yet. Inject a fault to see the loop: detect, diagnose, simulate on the twin, act, verify.</li>`;
  else ol.innerHTML = (h.incident ? "" : `<li class="muted">Last incident #${inc.id} (${esc(inc.outcome)})</li>`) +
    inc.steps.map((s) => `<li><span class="t">${fmtTime(s.t)}</span>${esc(s.text)}</li>`).join("");
  $("#approveBox").hidden = h.phase !== "AWAITING_APPROVAL";
}

const KPI = [
  ["inet_latency", "Internet latency", "ms", (v) => v >= 999 ? "bad" : v > 150 ? "warn" : ""],
  ["inet_loss", "Packet loss", "%", (v) => v >= 20 ? "bad" : v > 2 ? "warn" : ""],
  ["gw_latency", "Gateway latency", "ms", (v) => v >= 999 ? "bad" : v > 50 ? "warn" : ""],
  ["dns_fail", "DNS failures", "%", (v) => v >= 50 ? "bad" : v > 10 ? "warn" : ""],
  ["signal", "Wi-Fi signal", "%", (v) => v <= 0 ? "bad" : v < 35 ? "warn" : ""],
  ["util", "Utilisation", "%", (v) => v > 85 ? "warn" : ""],
  ["cpu", "Device CPU", "%", (v) => v > 85 ? "warn" : ""],
];
function renderKpis() {
  const h = S.history.slice(-3);
  if (!h.length) { $("#kpis").innerHTML = ""; return; }
  $("#kpis").innerHTML = KPI.map(([k, label, unit, cls]) => {
    const v = h.reduce((a, r) => a + r[k], 0) / h.length;
    const txt = unit === "ms" && v >= 999 ? "timeout" : (v >= 100 || unit === "%" ? v.toFixed(0) : v.toFixed(1));
    return `<div class="kpi ${cls(v)}"><div class="k">${label}</div><div class="v">${txt}${txt === "timeout" ? "" : `<small>${unit}</small>`}</div></div>`;
  }).join("");
}

function setupCanvas(c) {
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.height / (c._dpr || 1);
  c._dpr = dpr; c.width = w * dpr; c.height = h * dpr; c.style.height = h + "px";
  const ctx = c.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return [ctx, w, h];
}

function drawChart() {
  const c = $("#chart");
  if (!c.clientWidth) return;
  const [ctx, W, H] = setupCanvas(c);
  ctx.clearRect(0, 0, W, H);
  const h = S.history;
  const pad = { l: 44, r: 40, t: 10, b: 22 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const lat = h.map((r) => Math.min(r.inet_latency, 999));
  let maxLat = Math.max(100, ...lat.filter((v) => v < 999).map((v) => v * 1.2));
  maxLat = Math.min(1000, Math.ceil(maxLat / 50) * 50);
  ctx.font = "11px Consolas, monospace"; ctx.fillStyle = "#6f6b64"; ctx.strokeStyle = "#1f1f1f"; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + ih * i / 4;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(W - pad.r, y); ctx.stroke();
    ctx.textAlign = "right"; ctx.fillText(Math.round(maxLat * (1 - i / 4)) + "", pad.l - 6, y + 4);
    ctx.textAlign = "left"; ctx.fillText(Math.round(100 * (1 - i / 4)) + "%", W - pad.r + 6, y + 4);
  }
  ctx.textAlign = "left"; ctx.fillText("ms", 4, pad.t + 8);
  const N = Math.max(h.length, 60);
  const X = (i) => pad.l + iw * (i + (N - h.length)) / (N - 1);
  const line = (vals, maxV, color, width, dash) => {
    ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = width; ctx.setLineDash(dash || []);
    vals.forEach((v, i) => { const y = pad.t + ih * (1 - Math.min(v, maxV) / maxV); i ? ctx.lineTo(X(i), y) : ctx.moveTo(X(i), y); });
    ctx.stroke(); ctx.setLineDash([]);
  };
  // fault shading
  h.forEach((r, i) => { if (r.code !== "HEALTHY") { ctx.fillStyle = "rgba(229,72,77,0.07)"; ctx.fillRect(X(i) - iw / N / 2, pad.t, iw / N + 1, ih); } });
  line(h.map((r) => r.risk * 100), 100, "rgba(243,241,236,0.55)", 1.2, [3, 3]);
  line(h.map((r) => r.inet_loss), 100, "#e5484d", 1.6);
  line(lat, maxLat, "#d4af37", 2);
}

function renderFaults() {
  const box = $("#faults");
  const live = S.mode === "live";
  $("#liveDur").hidden = !live;
  $("#faultTitle").textContent = live ? "Inject a real fault (this PC only)" : "Inject a fault";
  $("#faultHint").textContent = live
    ? "Each fault affects only this computer and is undone automatically when the timer ends."
    : "Gradual faults build up over 10-25 s, so the predictor can see them coming. Abrupt faults cannot be predicted.";
  $("#clearBtn").textContent = live ? "Clear fault now" : "Reset network";
  const list = live ? (S.live ? S.live.faults : []) : S.faults;
  const key = S.mode + JSON.stringify(list.map((f) => [f.id, f.active]));
  if (box.dataset.key !== key) {
    box.dataset.key = key;
    box.innerHTML = list.map((f) => `<button class="btn ${f.active ? "active" : ""}" data-f="${f.id}">${esc(f.label)}
      <small>${live ? (f.admin ? "needs admin" : "no admin needed") : (f.kind === "gradual" ? "gradual build-up" : "abrupt failure")}</small></button>`).join("");
    box.querySelectorAll("button").forEach((b) => b.onclick = async () => {
      const r = await api("/api/inject", { kind: b.dataset.f, seconds: +$("#dur").value });
      if (!r.ok) alertBanner(r.error || "could not inject");
      poll();
    });
  }
  let act = "";
  if (live && S.live && S.live.fault) act = `Active: <b>${esc(S.live.fault.label)}</b> - ${S.live.fault.remaining}s left`;
  if (!live) { const a = S.faults.filter((f) => f.active).map((f) => f.label); if (a.length) act = "Active: <b>" + a.map(esc).join(", ") + "</b> (root cause stays until reset)"; }
  $("#activeFault").innerHTML = act;
}

function renderIncidents() {
  const rows = S.healer.incidents.map((i) => `<tr><td class="num">${i.id}</td>
    <td>${i.kind === "preventive" ? "Predicted" : "Reactive"}</td><td>${esc(i.title)}</td>
    <td>${i.chosen ? esc(i.chosen.title) : "-"}</td><td><span class="tag ${esc(i.outcome)}">${esc(i.outcome.toUpperCase())}</span></td>
    <td class="num">${i.mttr_s != null ? i.mttr_s.toFixed(0) + " s" : "-"}</td></tr>`).join("");
  $("#incTable tbody").innerHTML = rows || `<tr><td colspan="6" class="muted">No incidents.</td></tr>`;
}

function renderLog() {
  $("#log").innerHTML = S.events.map((e) => `<div><span class="t">${fmtTime(e.t)}</span><span class="k ${esc(e.kind)}">${esc(e.kind)}</span><span>${esc(e.text)}</span></div>`).join("");
}

/* ------------------------------------------------------------ twin */
function renderTwin() {
  const sel = $("#whatifSel");
  if (!sel.options.length) sel.innerHTML = Object.entries(S.classes).filter(([c]) => c !== "HEALTHY").map(([c, t]) => `<option value="${c}">${esc(t)}</option>`).join("");
  if (S.mode === "sim") renderSimTopo(); else renderLiveTopo();
  renderPlan();
}

function renderSimTopo() {
  const v = S.sim;
  $("#twinMeta").textContent = `· synced ${v.syncs} times` + (v.fidelity ? ` · fidelity ${v.fidelity.toFixed(1)}%` : "");
  const N = Object.fromEntries(v.nodes.map((n) => [n.id, n]));
  let svg = "";
  v.links.forEach((l) => {
    const a = N[l.a], b = N[l.b];
    const cls = ["lnk", l.status, l.probe ? "probe" : "", l.standby ? "standby" : ""].join(" ");
    svg += `<line class="${cls}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}"/>`;
    if (l.util > 1 && l.status !== "down") svg += `<text class="util" x="${(a.x + b.x) / 2 + 6}" y="${(a.y + b.y) / 2 - 4}">${l.util.toFixed(0)}%</text>`;
  });
  v.nodes.forEach((n) => {
    const w = n.kind === "cloud" ? 130 : 112, h = 42;
    const sub = n.kind === "ap" ? n.extra : (n.cpu ? `CPU ${n.cpu.toFixed(0)}%` : "");
    svg += `<g class="nd ${n.status}"><rect x="${n.x - w / 2}" y="${n.y - h / 2}" width="${w}" height="${h}" rx="7"/>
      <text x="${n.x}" y="${n.y + (sub ? -3 : 5)}" text-anchor="middle">${esc(n.id)}</text>
      ${sub ? `<text class="sub" x="${n.x}" y="${n.y + 14}" text-anchor="middle">${esc(sub)}</text>` : ""}</g>`;
  });
  const groups = v.groups;
  const gy = 545;
  svg += `<text class="sub" x="20" y="${gy}">Monitored probe: ${esc(v.probe_group)} (dashed gold path)</text>`;
  $("#topo").innerHTML = svg;
  $("#twinKv").innerHTML = Object.entries(v.policy).map(([k, x]) => `<tr><td>${esc(k)}</td><td>${esc(x)}</td></tr>`).join("") +
    `<tr><td>Twin synchronisations</td><td>${v.syncs}</td></tr>` + (v.fidelity ? `<tr><td>Twin fidelity (latency)</td><td>${v.fidelity.toFixed(1)}%</td></tr>` : "");
  $("#groupTbl tbody").innerHTML = groups.map((g) => `<tr><td>${esc(g.name)}</td><td>${esc(g.ap)}</td><td class="num">${g.users}</td>
    <td class="num">${g.latency == null ? "-" : g.latency + " ms"}</td><td class="num" style="color:${g.ok ? "inherit" : "var(--crit)"}">${g.ok ? g.loss + "%" : "down"}</td></tr>`).join("");
}

function renderLiveTopo() {
  const L = S.live; if (!L) return;
  const c = L.context, t = L.twin || {}, d = S.diagnosis || { code: "HEALTHY" };
  const st = (bad, warn) => bad ? "down" : warn ? "warn" : "ok";
  const linkUp = c.connected;
  const nodes = [
    { id: "pc", x: 110, y: 260, t: "This PC", s: (c.name || "Wi-Fi") + (t.rules && Object.values(t.rules).some(Boolean) ? " · block rule" : ""), st: st(!t.adapter_on, t.rules && Object.values(t.rules).some(Boolean)) },
    { id: "ap", x: 360, y: 260, t: c.bssid || "no AP", s: (c.ssid || "not connected") + (c.signal != null ? ` · ${c.signal}%` : ""), st: st(!linkUp, d.code === "WEAK_SIGNAL" || t.current_mark === "bad") },
    { id: "gw", x: 600, y: 260, t: "Gateway", s: c.gateway || "-", st: st(!linkUp, false) },
    { id: "net", x: 860, y: 160, t: "Internet", s: c.target, st: st(!linkUp || t.upstream_down || (t.rules && t.rules.outage), d.code === "CONGESTION") },
    { id: "dns", x: 860, y: 360, t: "DNS", s: (c.dns || [])[0] || "-", st: st(!linkUp || d.code === "DNS_FAILURE", false) },
  ];
  const N = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const links = [["pc", "ap"], ["ap", "gw"], ["gw", "net"], ["gw", "dns"]];
  let svg = links.map(([a, b]) => `<line class="lnk probe ${N[b].st === "down" ? "down" : ""}" x1="${N[a].x}" y1="${N[a].y}" x2="${N[b].x}" y2="${N[b].y}"/>`).join("");
  svg += nodes.map((n) => `<g class="nd ${n.st}"><rect x="${n.x - 95}" y="${n.y - 26}" width="190" height="52" rx="8"/>
    <text x="${n.x}" y="${n.y - 3}" text-anchor="middle">${esc(n.t)}</text><text class="sub" x="${n.x}" y="${n.y + 15}" text-anchor="middle">${esc(n.s)}</text></g>`).join("");
  $("#topo").innerHTML = svg;
  $("#twinMeta").textContent = "· model of this PC's live path";
  const kv = {
    "Adapter": t.adapter_on === false ? "disabled" : "enabled", "Connected": c.connected ? "yes" : "no",
    "Access point mark": (t.current_mark || "none").toUpperCase(), "Host block rules": (L.rules || []).join(", ") || "none",
    "Bulk transfer": t.bulk ? "running" : "no", "Upstream fault inferred": t.upstream_down ? "yes" : "no",
    "Gateway answers ping": c.gw_pingable ? "yes" : "no", "Latency baseline": c.baseline ? c.baseline + " ms" : "learning",
  };
  $("#twinKv").innerHTML = Object.entries(kv).map(([k, x]) => `<tr><td>${esc(k)}</td><td>${esc(x)}</td></tr>`).join("");
  $("#groupTbl tbody").innerHTML = `<tr><td>This PC</td><td>${esc(c.bssid || "-")}</td><td class="num">1</td><td class="num">${S.history.length ? Math.round(S.history.at(-1).inet_latency) + " ms" : "-"}</td><td class="num">${S.history.length ? Math.round(S.history.at(-1).inet_loss) + "%" : "-"}</td></tr>`;
}

function renderPlan() {
  let src = null, data = null;
  const h = S.healer;
  if (whatif && Date.now() - whatif.at < 45000) { data = whatif.data; src = `What-if: ${whatif.data.title}` + (S.mode === "sim" ? " on the current network" : ""); }
  else {
    const inc = h.incident || h.incidents.find((i) => i.plan);
    if (inc && inc.plan) { data = inc.plan; src = `Incident #${inc.id}: ${inc.title}` + (inc.plan.projected ? " (on the projected future network)" : ""); }
  }
  $("#planSrc").textContent = src ? "· " + src : "";
  if (!data) {
    $("#plan").innerHTML = `<p class="muted">No plan yet. Inject a fault, or run a what-if on the twin.</p>`;
    $("#constraints").innerHTML = ""; return;
  }
  const chosen = (h.incident || h.incidents[0] || {}).chosen;
  $("#plan").innerHTML = data.candidates.map((c) => {
    const p = c.predicted;
    const pred = p ? `<div class="pred">${p.inet_latency != null ? `latency <b>${p.inet_latency} ms</b><br>` : ""}loss <b>${p.inet_loss}%</b> · DNS <b>${p.dns ? "ok" : "fail"}</b><br>${p.worst_util != null ? `busiest link <b>${p.worst_util}%</b><br>` : ""}disruption <b>${c.disruption}</b> ${S.mode === "sim" ? "user-min" : "min"}</div>` : `<div class="pred">not simulated</div>`;
    const isChosen = chosen && chosen.id === c.id && !(whatif && Date.now() - whatif.at < 45000);
    return `<div class="plan-row ${isChosen ? "chosen" : ""}"><div><span class="tag ${c.verdict}">${c.verdict}</span>${isChosen ? `<div class="small" style="margin-top:6px;color:var(--gold)">executed</div>` : ""}</div>
      <div><div class="title">${esc(c.title)}</div><div class="detail">${esc(c.detail)}</div>
      ${c.violations.length ? `<ul>${c.violations.slice(0, 4).map((v) => `<li>${esc(v)}</li>`).join("")}</ul>` : ""}
      ${c.notes.length ? `<ul class="notes">${c.notes.map((v) => `<li>${esc(v)}</li>`).join("")}</ul>` : ""}</div>${pred}</div>`;
  }).join("") || `<p class="muted">No automatic action exists for this fault - NetSage escalates it to the network team.</p>`;
  $("#constraints").innerHTML = (data.constraints || []).map((c) => `<li>${esc(c)}</li>`).join("");
}

/* ------------------------------------------------------------ XAI */
function hbar(label, value, max, cls, txt) {
  const w = Math.min(100, Math.abs(value) / (max || 1) * 100);
  return `<div class="hbar ${cls || ""}"><div>${esc(label)}</div><div class="track"><div class="fill ${value < 0 ? "neg" : ""}" style="width:${w}%"></div></div><div class="val">${esc(txt)}</div></div>`;
}
function renderXai() {
  const d = S.diagnosis; if (!d) return;
  const max = Math.max(...d.factors.map((f) => Math.abs(f.contribution)), 1e-6);
  $("#attr").innerHTML = `<p class="small muted">Diagnosis: <b style="color:var(--text)">${esc(d.title)}</b></p>` +
    d.factors.map((f, i) => hbar(`${f.label} = ${f.value}`, f.contribution, max, i === 0 ? "lead" : "", (f.contribution >= 0 ? "+" : "-") + f.share + "%")).join("");
  const probs = Object.entries(d.probabilities).sort((a, b) => b[1] - a[1]);
  $("#probs").innerHTML = probs.map(([c, p], i) => hbar(S.classes[c], p, 1, i === 0 ? "lead" : "", (p * 100).toFixed(1) + "%")).join("");
  $("#evidence2").innerHTML = d.evidence.map((e) => `<li>${esc(e)}</li>`).join("");
  const p = S.prediction;
  const dm = Math.max(...(p.drivers || []).map((x) => x.weight), 1e-6);
  $("#drivers").innerHTML = `<p class="small muted">Risk <b style="color:var(--text)">${Math.round(p.risk * 100)}%</b>${p.forecast_title && p.risk >= 0.35 ? ` · most likely: ${esc(p.forecast_title)}` : ""}</p>` +
    ((p.drivers || []).length ? p.drivers.map((x, i) => hbar(x.label, x.weight, dm, i === 0 ? "lead" : "", "+" + x.weight.toFixed(3))).join("") : `<p class="muted">Nothing is pushing the risk up right now.</p>`);
}

/* ------------------------------------------------------------ model */
async function loadModel() {
  if (!modelInfo) modelInfo = await api("/api/model");
  const m = modelInfo; if (!m.diagnoser) return;
  const D = m.diagnoser, P = m.predictor;
  const kv = (o) => Object.entries(o).map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
  $("#mDiag").innerHTML = kv({ "Architecture": D.architecture.join(" → "), "Activation": D.activation, "Optimiser": D.optimiser, "Parameters": D.params.toLocaleString(), "Training samples": D.train_samples.toLocaleString(), "Test samples": D.test_samples.toLocaleString(), "Test accuracy": (D.test_accuracy * 100).toFixed(2) + "%", "Fault classes": D.classes.length });
  $("#mPred").innerHTML = kv({ "Architecture": P.architecture.join(" → "), "Input": "12 features + 12 trends", "Activation": P.activation, "Parameters": P.params.toLocaleString(), "Training windows": P.train_samples.toLocaleString(), "Accuracy": (P.test_accuracy * 100).toFixed(2) + "%", "Precision": (P.precision * 100).toFixed(2) + "%", "Recall": (P.recall * 100).toFixed(2) + "%", "Horizon": P.horizon_ticks + " samples" });
  const cls = D.classes.map((c) => S ? S.classes[c] : c);
  let t = `<div class="scroll"><table class="cm"><tr><th class="r">actual \\ predicted</th>${cls.map((c) => `<th>${esc(c)}</th>`).join("")}<th>recall</th></tr>`;
  D.confusion.forEach((row, i) => {
    const tot = row.reduce((a, b) => a + b, 0) || 1;
    t += `<tr><th class="r">${esc(cls[i])}</th>` + row.map((v, j) => { const a = v / tot; const bg = i === j ? `rgba(212,175,55,${0.15 + 0.6 * a})` : (v ? `rgba(229,72,77,${0.15 + 0.6 * a})` : "transparent"); return `<td style="background:${bg}">${v}</td>`; }).join("") + `<td>${(row[i] / tot * 100).toFixed(1)}%</td></tr>`;
  });
  $("#confusion").innerHTML = t + "</table></div>";
  const c = $("#lossChart"); const [ctx, W, H] = setupCanvas(c);
  const draw = (vals, color) => { const mx = Math.max(...D.loss_curve, ...P.loss_curve); ctx.beginPath(); ctx.strokeStyle = color; ctx.lineWidth = 2; vals.forEach((v, i) => { const x = 30 + (W - 40) * i / (vals.length - 1), y = 10 + (H - 30) * (1 - v / mx); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke(); };
  ctx.clearRect(0, 0, W, H); draw(D.loss_curve, "#d4af37"); draw(P.loss_curve, "#f3f1ec");
  ctx.fillStyle = "#8e8a82"; ctx.font = "11px Segoe UI, sans-serif"; ctx.fillText("gold: diagnoser   white: predictor   (cross-entropy per epoch)", 30, H - 6);
}

/* ------------------------------------------------------------ access points */
function renderAps() {
  const L = S.live; if (!L) return;
  $("#apSsid").textContent = L.context.ssid || "-";
  $("#apTable tbody").innerHTML = (L.aps || []).map((a) => `<tr>
    <td class="num">${esc(a.bssid)}${a.current ? ' <span class="tag SAFE">CONNECTED</span>' : ""}${!a.visible ? ' <span class="muted small">(not in range)</span>' : ""}</td>
    <td class="num">${a.signal ?? "-"}%</td><td>${esc(a.band || "-")}</td><td class="num">${esc(a.channel || "-")}</td>
    <td class="num">${a.loss == null ? "-" : a.loss.toFixed(1) + "%"}</td><td class="num">${a.latency == null ? "-" : a.latency.toFixed(0) + " ms"}</td>
    <td class="num">${a.quality}</td>
    <td><span class="tag ${a.mark === "good" ? "SAFE" : a.mark === "bad" ? "UNSAFE" : ""}">${a.mark.toUpperCase()}</span></td>
    <td style="white-space:nowrap"><button class="btn small" data-m="good" data-b="${esc(a.bssid)}">Good</button>
      <button class="btn small" data-m="bad" data-b="${esc(a.bssid)}">Bad</button>
      <button class="btn small ghost" data-m="none" data-b="${esc(a.bssid)}">Clear</button>
      <button class="btn small ghost" data-c="${esc(a.bssid)}" ${a.current || a.mark === "bad" || !a.visible ? "disabled" : ""}>Connect</button></td></tr>`).join("")
    || `<tr><td colspan="9" class="muted">No access points yet - connect to Wi-Fi and wait for the first scan (about 20 s).</td></tr>`;
  $$("#apTable button[data-m]").forEach((b) => b.onclick = async () => { await api("/api/ap/mark", { bssid: b.dataset.b, mark: b.dataset.m }); poll(); });
  $$("#apTable button[data-c]").forEach((b) => b.onclick = async () => { const r = await api("/api/ap/connect", { bssid: b.dataset.c }); if (!r.ok) alertBanner(r.error); });
}
