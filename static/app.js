"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { data: null, tab: "today", range: 30, calMonth: null, selectedDay: null };

const RISK = {
  high: { icon: "!", title: "Likely fertile", text: "Highest chance of pregnancy. Use protection." },
  possible: { icon: "?", title: "Possibly fertile",
    text: "Ovulation hasn't been confirmed by her temperature yet, so this day can't be treated as low risk." },
  low: { icon: "✓", title: "Low risk (not zero)",
    text: "A sustained temperature rise confirmed ovulation this cycle. This is the lowest-risk phase until her next period." },
  unknown: { icon: "–", title: "Not enough data", text: "Log the first day of her period to start tracking." },
};

// ------------------------------------------------------------------ helpers

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c !== null && c !== undefined && c !== false) node.append(c.nodeType ? c : String(c));
  }
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

const parseDay = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const isoDay = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const fmtDay = (s, opts = { month: "short", day: "numeric" }) => parseDay(s).toLocaleDateString(undefined, opts);
const fmtNum = (v, digits = 0) => v === null || v === undefined ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits });
const fmtTemp = (v) => v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(2)} °C`;
const fmtHours = (h) => h === null || h === undefined ? "–" : `${Math.floor(h)}h ${String(Math.round((h % 1) * 60)).padStart(2, "0")}m`;

async function api(path, body) {
  const opts = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await fetch(path, { credentials: "same-origin", ...opts });
  const json = await res.json().catch(() => ({}));
  if (res.status === 401 && path !== "/api/login") { showLogin(); throw new Error("login required"); }
  if (!res.ok) throw new Error(json.error || `HTTP ${res.status}`);
  return json;
}

// ------------------------------------------------------------------ login & boot

function showLogin() { $("#app").hidden = true; $("#login").hidden = false; $("#pin").focus(); }

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", { pin: $("#pin").value.trim() });
    $("#pin").value = "";
    await load();
  } catch (err) { $("#login-error").textContent = err.message; }
});

async function boot() {
  const { authed } = await api("/api/session");
  if (authed) await load(); else showLogin();
}

async function load() {
  state.data = await api("/api/dashboard");
  $("#login").hidden = true;
  $("#app").hidden = false;
  if (!state.calMonth) { const t = parseDay(state.data.today); state.calMonth = new Date(t.getFullYear(), t.getMonth(), 1); }
  if (!state.selectedDay) state.selectedDay = state.data.today;
  render();
}

function render() {
  renderSync();
  renderToday();
  renderCycle();
  renderTrends();
  showTab(state.tab);
}

function showTab(tab) {
  state.tab = tab;
  for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const name of ["today", "cycle", "trends"]) $(`#tab-${name}`).hidden = name !== tab;
  $("#title").textContent = { today: "Today", cycle: "Cycle", trends: "Trends" }[tab];
  if (tab === "trends") renderTrends();  // charts need a visible container to measure
  if (tab === "cycle") renderTempChart();
}
for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => showTab(b.dataset.tab));

function renderSync() {
  const s = state.data.sync;
  const times = s.endpoints.map((e) => e.last_success_ms).filter(Boolean);
  const last = times.length ? new Date(Math.max(...times)) : null;
  let text = s.running ? "Syncing with Oura…" : last ? `Updated ${last.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" })}` : "Not synced yet";
  if (s.error) text += ` · ${s.error}`;
  $("#sync-line").textContent = text;
}

$("#sync-btn").addEventListener("click", async () => {
  const btn = $("#sync-btn");
  btn.disabled = true;
  try {
    await api("/api/sync", {});
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      await load();
      if (!state.data.sync.running) break;
    }
  } finally { btn.disabled = false; }
});

// ------------------------------------------------------------------ today

function deltaLine(value, base, { unit, digits = 0, higherIsGood }) {
  if (value === null || value === undefined) return null;
  if (!base || !base.ready) return el("div", { class: "delta" }, `Learning her normal (${base ? base.nights : 0}/14 nights)`);
  const diff = value - base.median;
  if (Math.abs(diff) < (digits ? 0.05 : 0.5)) return el("div", { class: "delta" }, "Right at her normal");
  const good = higherIsGood ? diff > 0 : diff < 0;
  const arrow = diff > 0 ? "▲" : "▼";
  return el("div", { class: `delta ${good ? "good" : ""}` }, `${arrow} ${fmtNum(Math.abs(diff), digits)}${unit} vs her normal`);
}

function tile(label, value, unit, delta) {
  return el("div", { class: "card" },
    el("div", { class: "label" }, label),
    el("div", { class: "tile-value" }, value, unit ? el("span", { class: "tile-unit" }, ` ${unit}`) : null),
    delta);
}

function riskCard(entry, { big = false } = {}) {
  const r = RISK[entry.risk];
  const cd = entry.cycle_day ? `Cycle day ${entry.cycle_day} · ` : "";
  return el("div", { class: "card" },
    el("div", { class: "status" },
      el("div", { class: `status-icon risk-${entry.risk}`, "aria-hidden": "true" }, r.icon),
      el("div", {},
        el("div", { class: "label" }, `${cd}${fmtDay(entry.day, { weekday: "long", month: "short", day: "numeric" })}`),
        el("div", { class: "status-title" }, r.title))),
    big ? el("p", { class: "muted", style: "margin:10px 0 0" }, r.text) : null);
}

function renderToday() {
  const root = $("#tab-today");
  root.replaceChildren();
  const { latest, baselines: b, strain, cycle } = state.data;

  root.append(riskCard(cycle.today));
  if (strain) root.append(el("div", { class: "card banner" }, el("h2", {}, "Heads up"), el("p", { class: "muted", style: "margin:0" }, strain.text)));

  if (!latest) {
    root.append(el("div", { class: "card empty" },
      el("h2", {}, "Waiting for her first night"),
      "Scores show up after she sleeps with the ring and opens the Oura app so it can upload."));
    return;
  }
  const asOf = latest.day === state.data.today ? "Today" : fmtDay(latest.day, { weekday: "long" });
  root.append(el("div", { class: "card" },
    el("div", { class: "label" }, `Readiness · ${asOf}`),
    el("div", { class: "hero" }, fmtNum(latest.readiness)),
    deltaLine(latest.readiness, b.readiness, { unit: "", higherIsGood: true })));

  root.append(el("div", { class: "tiles" },
    tile("Sleep score", fmtNum(latest.sleep_score), "", deltaLine(latest.sleep_score, b.sleep_score, { unit: "", higherIsGood: true })),
    tile("Time asleep", fmtHours(latest.sleep_hours), "", deltaLine(latest.sleep_hours, b.sleep_hours, { unit: "h", digits: 1, higherIsGood: true })),
    tile("HRV", fmtNum(latest.hrv), "ms", deltaLine(latest.hrv, b.hrv, { unit: " ms", higherIsGood: true })),
    tile("Resting HR", fmtNum(latest.rhr), "bpm", deltaLine(latest.rhr, b.rhr, { unit: " bpm", higherIsGood: false })),
    tile("Body temp", fmtTemp(latest.temp), "", el("div", { class: "delta" }, "vs her baseline")),
    tile("Steps", fmtNum(latest.steps), "", deltaLine(latest.steps, b.steps, { unit: "", higherIsGood: true }))));

  if (latest.stress) root.append(el("div", { class: "card" }, el("div", { class: "label" }, "Daytime stress"),
    el("div", { class: "tile-value" }, latest.stress[0].toUpperCase() + latest.stress.slice(1))));
}

// ------------------------------------------------------------------ cycle

function renderCycle() {
  const root = $("#tab-cycle");
  root.replaceChildren();
  const c = state.data.cycle;
  const byDay = Object.fromEntries(c.days.map((d) => [d.day, d]));
  const selected = byDay[state.selectedDay] || c.today;

  root.append(riskCard(selected, { big: true }));
  const trend = state.data.trends.find((t) => t.day === selected.day);
  if (trend && trend.temp !== null) root.lastChild.append(el("p", { class: "small muted", style: "margin:8px 0 0" }, `Body temp ${fmtTemp(trend.temp)} vs her baseline`));

  for (const note of c.notes) root.append(el("div", { class: "card banner" }, el("p", { style: "margin:0" }, note)));

  // Calendar
  const cal = el("div", { class: "card" });
  const m = state.calMonth;
  const shift = (n) => { state.calMonth = new Date(m.getFullYear(), m.getMonth() + n, 1); renderCycle(); renderTempChart(); };
  cal.append(el("div", { class: "cal-head" },
    el("button", { class: "ghost", "aria-label": "Previous month", onclick: () => shift(-1) }, "‹"),
    el("h2", { style: "margin:0" }, m.toLocaleDateString(undefined, { month: "long", year: "numeric" })),
    el("button", { class: "ghost", "aria-label": "Next month", onclick: () => shift(1) }, "›")));
  const grid = el("div", { class: "cal", role: "grid" });
  for (const d of ["M", "T", "W", "T", "F", "S", "S"]) grid.append(el("div", { class: "dow" }, d));
  const lead = (m.getDay() + 6) % 7;
  for (let i = 0; i < lead; i++) grid.append(el("div"));
  const daysInMonth = new Date(m.getFullYear(), m.getMonth() + 1, 0).getDate();
  for (let d = 1; d <= daysInMonth; d++) {
    const key = isoDay(new Date(m.getFullYear(), m.getMonth(), d));
    const entry = byDay[key];
    const risk = entry ? entry.risk : "unknown";
    const glyph = entry?.period_start ? "🩸" : entry?.ovulation_est ? "🥚" : "";
    const cls = ["cell", key === state.data.today ? "today" : "", key > state.data.today ? "future" : ""].join(" ");
    grid.append(el("button", {
      class: cls, "aria-label": `${fmtDay(key)}: ${RISK[risk].title}${entry?.period_start ? ", period started" : ""}`,
      onclick: () => { state.selectedDay = key; renderCycle(); renderTempChart(); },
    }, el("span", { class: "glyph" }, glyph), d, el("span", { class: `bar risk-${risk}` })));
  }
  cal.append(grid);
  cal.append(el("div", { class: "legend" },
    ...["high", "possible", "low", "unknown"].map((r) => el("span", {}, el("i", { class: `swatch risk-${r}` }), RISK[r].title)),
    el("span", {}, "🩸 Period start"), el("span", {}, "🥚 Est. ovulation")));
  cal.append(el("p", { class: "small muted", style: "margin:8px 0 0" }, "Faded days are a forecast."));
  root.append(cal);

  // Prediction summary
  const p = c.prediction;
  const lines = [];
  if (p.next_period) lines.push(`Next period expected around ${fmtDay(p.next_period, { weekday: "short", month: "short", day: "numeric" })}`);
  const current = c.cycles[c.cycles.length - 1];
  if (current && current.confirmed_on) lines.push(`Ovulation this cycle: around ${fmtDay(current.ovulation)} (confirmed ${fmtDay(current.confirmed_on)})`);
  else if (p.fertile_window) lines.push(`Predicted fertile window: ${fmtDay(p.fertile_window[0])} – ${fmtDay(p.fertile_window[1])}`);
  if (p.avg_cycle_length) lines.push(`Average cycle: ${p.avg_cycle_length} days over ${p.cycles_logged} cycle(s)`);
  if (lines.length) root.append(el("div", { class: "card" }, el("h2", {}, "Forecast"), ...lines.map((l) => el("div", {}, l))));

  // Temperature chart
  root.append(el("div", { class: "card" },
    el("div", { class: "chart-head" }, el("h2", {}, "Body temperature"), el("span", { class: "label" }, "°C vs her baseline")),
    el("div", { id: "temp-chart" }),
    el("p", { class: "small muted", style: "margin:6px 0 0" }, "After ovulation her temperature rises and stays up until her period. That rise is what confirms the low-risk phase.")));

  // Period logging
  const dateInput = el("input", { type: "date", max: state.data.today, value: state.data.today, "aria-label": "Period start date" });
  const msg = el("p", { class: "error small", role: "alert" });
  const log = async (day) => {
    try { await api("/api/period", { day }); state.selectedDay = day; await load(); }
    catch (err) { msg.textContent = err.message; }
  };
  const list = el("ul", { class: "period-list" }, ...c.periods.slice().reverse().map((d) =>
    el("li", {}, fmtDay(d, { weekday: "short", month: "short", day: "numeric", year: "numeric" }),
      el("button", { class: "danger", onclick: async () => {
        if (!confirm(`Remove period start on ${fmtDay(d)}?`)) return;
        await api("/api/period/delete", { day: d }); await load();
      } }, "Remove"))));
  root.append(el("div", { class: "card" },
    el("h2", {}, "Log her period"),
    el("p", { class: "small muted" }, "Tap on the first day of bleeding (not spotting). This is what the cycle math is built on."),
    el("button", { style: "width:100%;margin-bottom:10px", onclick: () => log(state.data.today) }, "🩸 Period started today"),
    el("div", { class: "row" }, dateInput, el("button", { class: "ghost", onclick: () => log(dateInput.value) }, "Log date")),
    msg,
    c.periods.length ? list : el("p", { class: "small muted" }, "No periods logged yet.")));

  root.append(el("p", { class: "small muted" },
    "This is an estimate from temperature, not birth control. Keep using condoms. For a certified option that uses her ring, see Natural Cycles."));
}

function renderTempChart() {
  const host = $("#temp-chart");
  if (!host || $("#tab-cycle").hidden) return;
  const c = state.data.cycle;
  const points = state.data.trends.slice(-60).map((t) => ({ day: t.day, value: t.temp }));
  const markers = [
    ...c.periods.map((d) => ({ day: d, label: "🩸" })),
    ...c.cycles.filter((x) => x.ovulation).map((x) => ({ day: x.ovulation, label: "🥚" })),
  ];
  lineChart(host, points, { format: fmtTemp, ref: { value: 0, label: "baseline" }, markers, title: "Body temperature" });
}

// ------------------------------------------------------------------ trends

const METRICS = [
  { key: "readiness", title: "Readiness", fmt: (v) => fmtNum(v) },
  { key: "sleep_score", title: "Sleep score", fmt: (v) => fmtNum(v) },
  { key: "hrv", title: "HRV", unit: "ms", fmt: (v) => `${fmtNum(v)} ms`, base: "hrv" },
  { key: "rhr", title: "Resting heart rate", unit: "bpm", fmt: (v) => `${fmtNum(v)} bpm`, base: "rhr" },
  { key: "sleep_hours", title: "Time asleep", fmt: fmtHours, base: "sleep_hours" },
  { key: "temp", title: "Body temperature", unit: "°C vs baseline", fmt: fmtTemp, ref0: true },
  { key: "steps", title: "Steps", fmt: (v) => fmtNum(v), bars: true },
];

function renderTrends() {
  const root = $("#tab-trends");
  if (root.hidden) return;
  root.replaceChildren();
  const seg = el("div", { class: "seg", role: "group", "aria-label": "Time range" },
    ...[7, 30, 90].map((n) => el("button", { "aria-pressed": String(state.range === n), onclick: () => { state.range = n; renderTrends(); } }, `${n}d`)));
  root.append(seg);
  const rows = state.data.trends.slice(-state.range);
  if (!rows.some((r) => METRICS.some((m) => r[m.key] !== null))) {
    root.append(el("div", { class: "card empty" }, "Trends fill in as she wears the ring. Check back in a few days."));
    return;
  }
  for (const m of METRICS) {
    const points = rows.map((r) => ({ day: r.day, value: r[m.key] }));
    const valid = points.filter((p) => p.value !== null);
    const last = valid[valid.length - 1];
    const host = el("div");
    root.append(el("div", { class: "card" },
      el("div", { class: "chart-head" }, el("h2", {}, m.title), el("span", { class: "now" }, last ? m.fmt(last.value) : "–")),
      host,
      valid.length ? el("details", {}, el("summary", {}, "Show numbers"),
        el("table", {}, el("tbody", {}, ...valid.slice().reverse().map((p) => el("tr", {}, el("td", {}, fmtDay(p.day, { weekday: "short", month: "short", day: "numeric" })), el("td", {}, m.fmt(p.value))))))) : null));
    const base = m.base && state.data.baselines[m.base];
    const ref = m.ref0 ? { value: 0, label: "baseline" } : base && base.ready ? { value: base.median, label: "her normal" } : null;
    (m.bars ? barChart : lineChart)(host, points, { format: m.fmt, ref, title: m.title });
  }
}

// ------------------------------------------------------------------ charts

const H = 160, PAD = { l: 36, r: 10, t: 14, b: 22 };

function niceScale(values, ref) {
  let lo = Math.min(...values), hi = Math.max(...values);
  if (ref !== undefined) { lo = Math.min(lo, ref); hi = Math.max(hi, ref); }
  if (lo === hi) { lo -= 1; hi += 1; }
  const span = hi - lo;
  const step = Math.pow(10, Math.floor(Math.log10(span / 3)));
  const nice = [1, 2, 5, 10].map((m) => m * step).find((s) => span / s <= 4);
  return { lo: Math.floor(lo / nice) * nice, hi: Math.ceil(hi / nice) * nice, step: nice };
}

function compact(v) {
  const a = Math.abs(v);
  if (a >= 1000) return `${+(v / 1000).toFixed(1)}K`;
  return +v.toFixed(2) + "";
}

function frame(host, points, opts, extraValues = []) {
  const W = Math.max(host.clientWidth, 260);
  const s = svg("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `${opts.title} chart` });
  const vals = points.map((p) => p.value).filter((v) => v !== null).concat(extraValues);
  const sc = niceScale(vals, opts.ref?.value);
  const n = points.length;
  const x = (i) => PAD.l + (n === 1 ? (W - PAD.l - PAD.r) / 2 : (i * (W - PAD.l - PAD.r)) / (n - 1));
  const y = (v) => PAD.t + ((sc.hi - v) * (H - PAD.t - PAD.b)) / (sc.hi - sc.lo);
  for (let v = sc.lo; v <= sc.hi + 1e-9; v += sc.step) {
    s.append(svg("line", { class: v === sc.lo ? "axis" : "grid", x1: PAD.l, x2: W - PAD.r, y1: y(v), y2: y(v) }));
    const t = svg("text", { x: PAD.l - 6, y: y(v) + 4, "text-anchor": "end" }); t.textContent = compact(v); s.append(t);
  }
  const ticks = n <= 1 ? [0] : [0, Math.floor((n - 1) / 2), n - 1];
  for (const i of ticks) {
    const t = svg("text", { x: x(i), y: H - 6, "text-anchor": i === 0 ? "start" : i === n - 1 ? "end" : "middle" });
    t.textContent = fmtDay(points[i].day); s.append(t);
  }
  if (opts.ref) {
    s.append(svg("line", { class: "ref", x1: PAD.l, x2: W - PAD.r, y1: y(opts.ref.value), y2: y(opts.ref.value) }));
    const t = svg("text", { class: "ref-label", x: W - PAD.r, y: y(opts.ref.value) - 4, "text-anchor": "end" }); t.textContent = opts.ref.label; s.append(t);
  }
  for (const mk of opts.markers || []) {
    const i = points.findIndex((p) => p.day === mk.day);
    if (i < 0) continue;
    s.append(svg("line", { class: "grid", x1: x(i), x2: x(i), y1: PAD.t, y2: H - PAD.b }));
    const t = svg("text", { x: x(i), y: PAD.t - 2, "text-anchor": "middle" }); t.textContent = mk.label; s.append(t);
  }
  return { s, x, y, W, n };
}

function attachHover(host, f, points, opts, onMove) {
  const tip = $("#tooltip");
  const cross = svg("line", { class: "cross", y1: PAD.t, y2: H - PAD.b, visibility: "hidden" });
  const dot = svg("circle", { class: "dot", r: 5, visibility: "hidden" });
  f.s.append(cross, dot);
  const overlay = svg("rect", { x: 0, y: 0, width: f.W, height: H, fill: "transparent" });
  f.s.append(overlay);
  const move = (e) => {
    const box = f.s.getBoundingClientRect();
    const px = ((e.clientX - box.left) / box.width) * f.W;
    const i = Math.max(0, Math.min(f.n - 1, Math.round(((px - PAD.l) / (f.W - PAD.l - PAD.r)) * (f.n - 1))));
    const p = points[i];
    cross.setAttribute("x1", f.x(i)); cross.setAttribute("x2", f.x(i)); cross.setAttribute("visibility", "visible");
    if (p.value !== null && !onMove) { dot.setAttribute("cx", f.x(i)); dot.setAttribute("cy", f.y(p.value)); dot.setAttribute("visibility", "visible"); }
    else dot.setAttribute("visibility", "hidden");
    tip.textContent = `${fmtDay(p.day, { weekday: "short", month: "short", day: "numeric" })} · ${p.value === null ? "no data" : opts.format(p.value)}`;
    tip.hidden = false;
    const tx = Math.min(window.innerWidth - tip.offsetWidth - 8, Math.max(8, e.clientX - tip.offsetWidth / 2));
    tip.style.left = `${tx}px`;
    tip.style.top = `${box.top - tip.offsetHeight - 6}px`;
  };
  const leave = () => { cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); tip.hidden = true; };
  overlay.addEventListener("pointermove", move);
  overlay.addEventListener("pointerdown", move);
  overlay.addEventListener("pointerleave", leave);
  overlay.addEventListener("pointercancel", leave);
  overlay.addEventListener("pointerup", (e) => { if (e.pointerType !== "mouse") setTimeout(leave, 1500); });
}

function lineChart(host, points, opts) {
  host.replaceChildren();
  if (!points.some((p) => p.value !== null)) { host.append(el("div", { class: "empty" }, "No data yet")); return; }
  const f = frame(host, points, opts);
  let d = "", pen = false;
  points.forEach((p, i) => {
    if (p.value === null) { pen = false; return; }
    d += `${pen ? "L" : "M"}${f.x(i).toFixed(1)},${f.y(p.value).toFixed(1)}`;
    pen = true;
  });
  f.s.append(svg("path", { class: "line", d }));
  // Isolated points (no neighbours) and the latest point get a dot.
  points.forEach((p, i) => {
    if (p.value === null) return;
    const lonely = (points[i - 1]?.value ?? null) === null && (points[i + 1]?.value ?? null) === null;
    const lastValid = points.slice(i + 1).every((q) => q.value === null);
    if (lonely || lastValid) f.s.append(svg("circle", { class: "dot", cx: f.x(i), cy: f.y(p.value), r: 4 }));
  });
  attachHover(host, f, points, opts);
  host.append(f.s);
}

function barChart(host, points, opts) {
  host.replaceChildren();
  if (!points.some((p) => p.value !== null)) { host.append(el("div", { class: "empty" }, "No data yet")); return; }
  const f = frame(host, points, opts, [0]);
  const slot = (f.W - PAD.l - PAD.r) / Math.max(points.length, 1);
  const w = Math.max(2, Math.min(24, slot - 2));
  points.forEach((p, i) => {
    if (!p.value) return;
    const top = f.y(p.value), base = f.y(0), r = Math.min(4, w / 2, base - top);
    const x0 = f.x(i) - w / 2;
    // Rounded data end, square at the baseline.
    f.s.append(svg("path", { class: "bar", d: `M${x0},${base}V${top + r}Q${x0},${top} ${x0 + r},${top}H${x0 + w - r}Q${x0 + w},${top} ${x0 + w},${top + r}V${base}Z` }));
  });
  attachHover(host, f, points, opts, true);
  host.append(f.s);
}

window.addEventListener("resize", () => { if (state.data) { if (state.tab === "trends") renderTrends(); if (state.tab === "cycle") renderTempChart(); } });
document.addEventListener("visibilitychange", () => { if (!document.hidden && state.data) load().catch(() => {}); });

boot().catch((err) => { document.body.prepend(el("p", { class: "error" }, `Could not reach the server: ${err.message}`)); });
