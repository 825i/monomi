/* eslint-disable */
"use strict";

// Browser poll cadence. Matches the server's INTERVAL by default so
// the page repaints as fast as the collector refreshes. Cheap because
// everything runs on the host now (no edge worker, no request budget).
const POLL_MS = 500;
const $ = (id) => document.getElementById(id);

document.getElementById("rate-ms") && (document.getElementById("rate-ms").textContent = POLL_MS + "ms");

let lastSeenTs = 0;
// totals so we can compute Top + Total for the net panel locally
let netTopDown = 0, netTopUp = 0;
let netTotalDown = 0, netTotalUp = 0;
let lastBytesTs = 0;

// ─────────────────────────────────────────────────────────────────────────────
// formatters
// ─────────────────────────────────────────────────────────────────────────────
function fmtBytes(v, digits) {
  if (v == null || !Number.isFinite(v)) return "-";
  const units = ["B","KiB","MiB","GiB","TiB","PiB"];
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  const d = digits != null ? digits : (i === 0 ? 0 : (v >= 100 ? 0 : (v >= 10 ? 1 : 2)));
  return v.toFixed(d) + " " + units[i];
}
function fmtRate(v) {
  if (v == null || !Number.isFinite(v)) return "-";
  // btop-ish: show as Mibps when large, else Kibps / Bps
  const units = ["Bps","Kibps","Mibps","Gibps"];
  let x = (v * 8); // bytes/s -> bits/s
  let i = 0;
  while (x >= 1024 && i < units.length - 1) { x /= 1024; i++; }
  const d = x >= 100 ? 0 : (x >= 10 ? 1 : 2);
  return x.toFixed(d) + " " + units[i];
}
function fmtRateBs(v) {
  if (v == null || !Number.isFinite(v)) return "-";
  return fmtBytes(v) + "/s";
}
function fmtPct(v) {
  if (v == null || !Number.isFinite(v)) return "-";
  return v.toFixed(0) + "%";
}
function fmtDuration(sec) {
  if (sec == null || !Number.isFinite(sec)) return "-";
  sec = Math.floor(sec);
  const d = Math.floor(sec / 86400); sec %= 86400;
  const h = Math.floor(sec / 3600);  sec %= 3600;
  const m = Math.floor(sec / 60);    sec %= 60;
  const pad = (n) => String(n).padStart(2, "0");
  if (d > 0) return d + "d " + pad(h) + ":" + pad(m) + ":" + pad(sec);
  return pad(h) + ":" + pad(m) + ":" + pad(sec);
}
function fmtClock(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  const time = d.toLocaleTimeString("en-GB", { hour12: false });
  // pull a short timezone abbreviation from the locale-aware formatter
  // (e.g. "AEST", "GMT+10"). Falls back to nothing if the platform
  // doesn't expose it.
  let tz = "";
  try {
    const parts = new Intl.DateTimeFormat("en-GB", { timeZoneName: "short" })
      .formatToParts(d);
    const found = parts.find(p => p.type === "timeZoneName");
    if (found) tz = " " + found.value;
  } catch {}
  return time + tz;
}
function escapeHTML(s) {
  return (s == null ? "" : String(s))
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function setText(el, txt) {
  if (!el) return;
  if (el.__last !== txt) {
    el.textContent = txt;
    el.__last = txt;
  }
}
function setHTML(el, html) {
  if (!el) return;
  if (el.__last !== html) {
    el.innerHTML = html;
    el.__last = html;
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// braille chart — N rows × W chars, each char = 2×4 dots
// ─────────────────────────────────────────────────────────────────────────────
const BRAILLE_BASE = 0x2800;
const BR_L = [0x01, 0x02, 0x04, 0x40];  // left column dots top→bottom
const BR_R = [0x08, 0x10, 0x20, 0x80];  // right column dots top→bottom

function brailleChart(values, opts) {
  const width  = opts.width  || 60;
  const height = opts.height || 1;
  const min    = opts.min    || 0;
  // mirror mode = chart split at the midline. Each sample lights dots in
  // both halves: upper grows up, lower grows down. (Water reflection.)
  const mirror = opts.mirror === true && height >= 2 && height % 2 === 0;
  // values2 (only valid with mirror) puts a SECOND series on the lower
  // half. Used for the dual download/upload net chart.
  const dual   = mirror && opts.values2 != null;
  // curve = power exponent applied to the normalized value. 1 = linear,
  // 0.5 = sqrt (amplifies low values so a 5% CPU still draws visible
  // dots without 100% saturating the whole half).
  const curve  = opts.curve != null ? opts.curve : 1;
  let max      = opts.max;
  const need = width * 2;
  const padUp = values.slice(-need);
  while (padUp.length < need) padUp.unshift(null);
  const padDn = dual ? opts.values2.slice(-need) : padUp;
  if (dual) while (padDn.length < need) padDn.unshift(null);
  if (max === "auto" || max == null) {
    const fu = padUp.filter(v => v != null && Number.isFinite(v));
    const fd = dual ? padDn.filter(v => v != null && Number.isFinite(v)) : [];
    max = Math.max(fu.length ? Math.max(...fu) : 0, fd.length ? Math.max(...fd) : 0, 1);
    if (opts.floor != null) max = Math.max(max, opts.floor);
    if (max <= 0) max = 1;
  }
  const halfH = mirror ? height / 2 : height;
  const halfDots = halfH * 4;
  const rows = [];
  for (let r = 0; r < height; r++) {
    let rowBot;
    const isLower = mirror && r >= height / 2;
    if (mirror) {
      if (isLower) rowBot = (r - height / 2) * 4;
      else         rowBot = (height / 2 - 1 - r) * 4;
    } else {
      rowBot = (height - r - 1) * 4;
    }
    const series = isLower ? padDn : padUp;
    let line = "";
    for (let c = 0; c < width; c++) {
      let code = BRAILLE_BASE;
      for (let s = 0; s < 2; s++) {
        const v = series[c * 2 + s];
        if (v == null || !Number.isFinite(v)) continue;
        const norm = Math.max(0, Math.min(1, (v - min) / (max - min || 1)));
        const shaped = curve === 1 ? norm : Math.pow(norm, curve);
        const dotH = Math.round(shaped * halfDots);
        const bits = s === 0 ? BR_L : BR_R;
        for (let d = 0; d < 4; d++) {
          const place = rowBot + d;
          if (place < dotH) {
            code |= isLower ? bits[d] : bits[3 - d];
          }
        }
      }
      line += String.fromCharCode(code);
    }
    rows.push(line);
  }
  return rows.join("\n");
}

// Horizontal fill bar in braille — used for disks rows.
function brailleHBar(ratio, width) {
  ratio = Math.max(0, Math.min(1, ratio || 0));
  width = width || 20;
  const ticks = Math.round(ratio * width * 2);
  let s = "";
  for (let c = 0; c < width; c++) {
    let code = BRAILLE_BASE;
    if (c * 2     < ticks) code |= 0x47;   // left  column (dots 1,2,3,7)
    if (c * 2 + 1 < ticks) code |= 0xB8;   // right column (dots 4,5,6,8)
    s += String.fromCharCode(code);
  }
  return s;
}

// 2-D bucket/glass fill: width chars × height rows of braille, filled
// from the bottom up to the given ratio. Used for the mem panel so each
// metric reads as a glass that's X% full.
function brailleBucket(ratio, width, height) {
  ratio = Math.max(0, Math.min(1, ratio || 0));
  width = width || 18;
  height = height || 4;
  // each braille row has 4 vertically-stacked dot levels per column.
  // total dot rows from bottom to top = height * 4.
  const totalLevels = height * 4;
  const filledLevels = Math.round(ratio * totalLevels);
  const rows = [];
  for (let r = 0; r < height; r++) {
    // r=0 is the TOP visual row, r=height-1 is the BOTTOM row.
    // dots in this row sit at levels [rowBottom .. rowBottom+3]
    // where rowBottom is the row's lowest dot level counted from the
    // bottom of the chart.
    const rowBottom = (height - 1 - r) * 4;
    let line = "";
    for (let c = 0; c < width; c++) {
      let code = BRAILLE_BASE;
      // BR_L / BR_R index 0 = top dot in a braille char, 3 = bottom dot.
      // For each of the 4 dot positions in this row, work out its level
      // from chart bottom and light it if it's below the fill line.
      for (let d = 0; d < 4; d++) {
        const dotLevel = rowBottom + (3 - d);   // bottom dot = lowest level
        if (dotLevel < filledLevels) {
          code |= BR_L[d];
          code |= BR_R[d];
        }
      }
      line += String.fromCharCode(code);
    }
    rows.push(line);
  }
  return rows.join("\n");
}
function barColorClass(ratio) {
  if (ratio >= 0.90) return "bar-bad";
  if (ratio >= 0.70) return "bar-warn";
  return "bar-good";
}

// ── three-stop colour ramp green → yellow → red (btop's cpu palette)
// Used to colour the CPU braille graph row-by-row so dots near the
// midline render in green (low load) and dots near the edges render
// in red (heavy load).
function cpuRampColor(t) {
  t = Math.max(0, Math.min(1, t));
  // stops: green #77ca9b → yellow #cbc06c → red #dc4c4c
  const lerp = (a, b, k) => a + (b - a) * k;
  let r, g, b;
  if (t <= 0.5) {
    const k = t * 2;
    r = lerp(0x77, 0xcb, k); g = lerp(0xca, 0xc0, k); b = lerp(0x9b, 0x6c, k);
  } else {
    const k = (t - 0.5) * 2;
    r = lerp(0xcb, 0xdc, k); g = lerp(0xc0, 0x4c, k); b = lerp(0x6c, 0x4c, k);
  }
  return "rgb(" + (r|0) + "," + (g|0) + "," + (b|0) + ")";
}

// Non-mirror gradient graph: bottom = low (green), top = high (red).
// Used for the mem history sparkline. Each braille row is wrapped in
// a span whose colour matches its vertical position.
function paintGradientGraph(el, values, opts) {
  if (!el) return;
  const text = brailleChart(values, opts);
  const lines = text.split("\n");
  const h = lines.length;
  let html = "";
  for (let i = 0; i < h; i++) {
    // i=0 is top of chart = high values (red), i=h-1 is bottom (green)
    const t = h > 1 ? 1 - (i / (h - 1)) : 0;
    html += "<span style='color:" + cpuRampColor(t) + "'>" +
      escapeHTML(lines[i]) + "</span>" +
      (i < h - 1 ? "\n" : "");
  }
  setHTML(el, html);
}

// Render a mirror-mode braille chart, then wrap each visual row in a
// span coloured along the green→red ramp. Rows nearest the midline
// represent low values (lots of headroom, green) and rows nearest the
// outer edges represent heavy load (red).
function paintGradientCpuGraph(el, values, opts) {
  if (!el) return;
  const text = brailleChart(values, opts);
  const lines = text.split("\n");
  const h = lines.length;
  const halfH = h / 2;
  let html = "";
  for (let i = 0; i < h; i++) {
    // distance from midline as a 0..1 normalised value
    const dist = i < halfH ? halfH - 1 - i : i - halfH;
    const t = halfH > 1 ? dist / (halfH - 1) : 0;
    html += "<span style='color:" + cpuRampColor(t) + "'>" +
      escapeHTML(lines[i]) + "</span>" +
      (i < h - 1 ? "\n" : "");
  }
  setHTML(el, html);
}

// width of the panel-area in monospace characters at our font size
function charsFor(el, sizePx) {
  if (!el) return 60;
  const px = el.clientWidth || el.parentElement?.clientWidth || 200;
  return Math.max(8, Math.floor((px - 6) / (sizePx || 9)));
}

// height of an element in braille rows. Used for sparklines that
// flex-grow to fill available space; the chart adapts to whatever
// vertical slack the panel hands it.
function rowsFor(el, lineHeightPx) {
  if (!el) return 4;
  const px = el.clientHeight || 56;
  return Math.max(2, Math.floor(px / (lineHeightPx || 14)));
}

// ─────────────────────────────────────────────────────────────────────────────
// memory rows — each metric is a "bucket" / "glass" that fills from the
// bottom up. Layout per metric:
//   Used:                          1.03 GiB
//   13%
//   ⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
//   ⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
//   ⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀⠀
//   ⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤⣤   ← 13% filled at bottom
// ─────────────────────────────────────────────────────────────────────────────
const MEM_BUCKET_W = 18;   // chars wide
const MEM_BUCKET_H = 3;    // rows of braille tall

function memRow(label, value, total, opts) {
  opts = opts || {};
  const ratio = total > 0 ? Math.max(0, Math.min(1, value / total)) : 0;
  const kind = opts.kind || "used";     // used/available/cached/free/swap-used/swap-free
  const bucket = brailleBucket(ratio, MEM_BUCKET_W, MEM_BUCKET_H);
  const valText = opts.text != null ? opts.text : fmtBytes(value);
  return (
    "<div class='mem-row " + kind + "'>" +
      "<div class='mem-row-top'>" +
        "<span class='lbl'>" + escapeHTML(label) + "</span>" +
        "<span class='val'>" + escapeHTML(valText) + "</span>" +
      "</div>" +
      "<div class='mem-row-pct'>" + fmtPct(ratio * 100) + "</div>" +
      "<pre class='bucket'>" + bucket + "</pre>" +
    "</div>"
  );
}

function plainRow(label, value) {
  return (
    "<div class='mem-row plain'>" +
      "<div class='mem-row-top'>" +
        "<span class='lbl'>" + escapeHTML(label) + "</span>" +
        "<span class='val'>" + escapeHTML(value) + "</span>" +
      "</div>" +
    "</div>"
  );
}

function renderMem(meminfo) {
  const wrap = $("mem-body");
  if (!wrap || !meminfo) return;
  const total = meminfo.total || 0;
  const used = meminfo.used || 0;
  const available = meminfo.available || 0;
  const cached = meminfo.cached || 0;
  const free = meminfo.free || 0;

  const html =
    plainRow("Total:", fmtBytes(total)) +
    memRow("Used:",      used,      total, { kind: "used" }) +
    memRow("Available:", available, total, { kind: "available" }) +
    memRow("Cached:",    cached,    total, { kind: "cached" }) +
    memRow("Free:",      free,      total, { kind: "free" });
  setHTML(wrap, html);
}

// swap lives at the bottom of the disks panel — same bucket UI as mem
// but kept out of the mem column so that column stays compact.
function renderSwap(meminfo) {
  const wrap = $("swap-body");
  if (!wrap || !meminfo) return;
  const swT = meminfo.swap_total || 0;
  const swU = meminfo.swap_used || 0;
  const swF = meminfo.swap_free || 0;
  if (swT <= 0) { setHTML(wrap, ""); return; }
  const html =
    "<div class='mem-section'>swap:</div>" +
    plainRow("Total:", fmtBytes(swT)) +
    memRow("Used:", swU, swT, { kind: "swap-used" }) +
    memRow("Free:", swF, swT, { kind: "swap-free" });
  setHTML(wrap, html);
}

// ─────────────────────────────────────────────────────────────────────────────
// disks
// ─────────────────────────────────────────────────────────────────────────────
function renderDisks(disks) {
  const wrap = $("disks-body");
  if (!wrap) return;
  // /boot/firmware was hidden in favour of the live i/o sparkline below.
  const filtered = (disks || []).filter(d =>
    d.mount === "/" || d.mount.startsWith("/srv/")
  );
  filtered.sort((a, b) => a.mount.length - b.mount.length);
  const html = filtered.map(d => {
    const ratio = d.percent != null ? Math.max(0, Math.min(1, d.percent / 100)) : 0;
    const label = (d.mount || "-")
      .replace(/^\/srv\/dev-disk-by-uuid-[a-f0-9-]+/, "/srv/pool");
    const usedBar = brailleHBar(ratio, 14);
    const freeBar = brailleHBar(1 - ratio, 14);
    return (
      "<div class='disk-row'>" +
        "<div class='disk-name'>" + escapeHTML(label) + "&nbsp;&nbsp;<span class='disk-total'>" + fmtBytes(d.total) + "</span></div>" +
        "<div class='disk-stats'>" +
          "<span class='lbl'>Used:</span>" +
          "<span class='hbar " + barColorClass(ratio) + "'>" + usedBar + "</span>" +
          "<span class='val'>" + fmtBytes(d.used) + "</span>" +
        "</div>" +
        "<div class='disk-stats'>" +
          "<span class='lbl'>Free:</span>" +
          "<span class='hbar bar-good'>" + freeBar + "</span>" +
          "<span class='val'>" + fmtBytes(d.available) + "</span>" +
        "</div>" +
      "</div>"
    );
  }).join("");
  setHTML(wrap, html);
}

// ─────────────────────────────────────────────────────────────────────────────
// processes — flat list, sorted strictly by CPU descending so the
// noisiest process always sits at the top. The collector already sorts
// by --sort=-pcpu but we re-sort on the client too in case anything
// shifts the order.
// ─────────────────────────────────────────────────────────────────────────────
const PROC_VISIBLE = 40;

function renderProc(procs, total) {
  const wrap = $("proc-rows");
  if (!wrap || !procs) return;
  const sorted = procs.slice().sort((a, b) => (b.cpu ?? 0) - (a.cpu ?? 0) || a.pid - b.pid);
  const visible = sorted.slice(0, PROC_VISIBLE);
  const rows = visible.map(p => {
    const cmd = p.cmd || p.program || "";
    const cpu = p.cpu != null ? p.cpu.toFixed(1) : "-";
    const cpuCls = p.cpu == null ? "cold" : (p.cpu > 50 ? "hot" : (p.cpu < 0.5 ? "cold" : ""));
    return (
      "<div class='pr'>" +
        "<span class='pid'>" + p.pid + "</span>" +
        "<span class='prog'>" + escapeHTML(p.program) + "</span>" +
        "<span class='cmd'>" + escapeHTML(cmd) + "</span>" +
        "<span class='thr'>" + (p.threads != null ? p.threads : "-") + "</span>" +
        "<span class='mem'>" + (p.rss_kb != null ? fmtBytes(p.rss_kb * 1024, 0) : "-") + "</span>" +
        "<span class='cpu " + cpuCls + "'>" + cpu + "</span>" +
      "</div>"
    );
  }).join("");
  setHTML(wrap, rows);
  // visible/sampled — e.g. "40/40" while the collector ships 40
  setText($("proc-count"), visible.length + "/" + (total ?? procs.length));
}

// ─────────────────────────────────────────────────────────────────────────────
// system info — slow-changing rows at the bottom of the proc panel.
// Pulls OS/kernel/arch/etc from snap.sysinfo, and UPS state from
// snap.ups (NUT 'upsc <name>' output keyed by the standard NUT vars).
// ─────────────────────────────────────────────────────────────────────────────
function fmtUps(ups) {
  if (!ups || typeof ups !== "object") return "-";
  const pct  = ups["battery.charge"];
  const run  = ups["battery.runtime"];
  const stat = ups["ups.status"];
  const parts = [];
  if (pct != null) parts.push(pct + "%");
  if (stat) parts.push(String(stat));
  if (run != null && Number.isFinite(+run)) {
    const m = Math.round(+run / 60);
    parts.push(m + "m");
  }
  return parts.length ? parts.join(" · ") : "-";
}

function renderNetinfo(snap) {
  const wrap = $("net-details");
  if (!wrap) return;
  const ni = snap.netinfo || {};
  // public IPv6 wears the .public-ip class so it blurs until hover.
  const pubV6 = ni.ipv6_public
    ? "<span class='public-ip' title='public IPv6'>" + escapeHTML(ni.ipv6_public) + "</span>"
    : "-";
  const rows = [
    ["IPv6 (local)",  escapeHTML(ni.ipv6_local || "-")],
    ["MTU",           escapeHTML(ni.mtu != null ? String(ni.mtu) : "-")],
    ["IPv6 (public)", pubV6],
    ["Gateway",       escapeHTML(ni.gateway || "-")],
    ["MAC",           escapeHTML(ni.mac || "-")],
    ["DNS",           escapeHTML(ni.dns || "-")],
  ];
  const html = rows.map(([lbl, val]) =>
    "<span class='lbl'>" + escapeHTML(lbl) + ":</span>" +
    "<span class='val'>" + val + "</span>"
  ).join("");
  setHTML(wrap, html);
}

function renderSysinfo(snap) {
  const wrap = $("sysinfo");
  if (!wrap) return;
  const si = snap.sysinfo || {};
  const rows = [
    ["OS",       si.os],
    ["Host",     si.host],
    ["Kernel",   si.kernel],
    ["Model",    si.model],
    ["Arch",     si.arch],
    ["GPU",      si.gpu],
    ["Shell",    si.shell],
    ["Packages", si.packages != null ? String(si.packages) : null],
    ["Last apt", si.last_apt],
    ["UPS",      fmtUps(snap.ups)],
  ];
  const html = rows.map(([lbl, val]) =>
    "<span class='lbl'>" + escapeHTML(lbl) + ":</span>" +
    "<span class='val'>" + escapeHTML(val != null && val !== "" ? String(val) : "-") + "</span>"
  ).join("");
  setHTML(wrap, html);
}

// ─────────────────────────────────────────────────────────────────────────────
// net top / totals
// ─────────────────────────────────────────────────────────────────────────────
function updateNetTotals(rxBps, txBps, ts) {
  if (rxBps != null && rxBps > netTopDown) netTopDown = rxBps;
  if (txBps != null && txBps > netTopUp)   netTopUp   = txBps;
  if (lastBytesTs && ts > lastBytesTs) {
    const dt = ts - lastBytesTs;
    if (rxBps != null) netTotalDown += rxBps * dt;
    if (txBps != null) netTotalUp   += txBps * dt;
  }
  lastBytesTs = ts;
}

// helper for the split / dual-coloured net graph
function paintNetGraph(elId, downHist, upHist, height, ifaceClass) {
  const ng = $(elId);
  if (!ng) return;
  const w = charsFor(ng, 9);
  const downMax = downHist.filter(Number.isFinite);
  const upMax   = upHist.filter(Number.isFinite);
  const sharedMax = Math.max(
    1,
    downMax.length ? Math.max(...downMax) : 0,
    upMax.length   ? Math.max(...upMax)   : 0,
  );
  const h = height; // must be even — 4 rows for down + 4 for up
  const full = brailleChart(downHist, {
    width: w, height: h, max: sharedMax, mirror: true, values2: upHist,
  });
  const lines = full.split("\n");
  const halfIdx = h / 2;
  const upper = lines.slice(0, halfIdx).join("\n");
  const lower = lines.slice(halfIdx).join("\n");
  setHTML(ng,
    "<span class='net-down-half'>" + escapeHTML(upper) + "</span>\n" +
    "<span class='net-up-half'>"   + escapeHTML(lower) + "</span>");
}

// ─────────────────────────────────────────────────────────────────────────────
// main render
// ─────────────────────────────────────────────────────────────────────────────
function paint(body) {
  if (!body) return;
  const snap = body.latest;
  if (!snap) return;
  const hist = body.history || {};
  const pm = snap.pironman || {};
  const upTime = snap.uptime || {};
  const mi = snap.meminfo || {};

  // ── header (clock) ──
  setText($("hdr-time"), fmtClock(snap.ts));

  // ── CPU readouts (table + per-core + load avg + uptime) ──
  setText($("v-cpu"), pm.cpu_percent != null ? pm.cpu_percent.toFixed(0) + "%" : "-");
  setText($("v-cpu-temp"), pm.cpu_temperature != null ? pm.cpu_temperature.toFixed(0) + "°C" : "-");
  setText($("v-c0"), pm.cpu_0_percent != null ? pm.cpu_0_percent.toFixed(0) + "%" : "-");
  setText($("v-c1"), pm.cpu_1_percent != null ? pm.cpu_1_percent.toFixed(0) + "%" : "-");
  setText($("v-c2"), pm.cpu_2_percent != null ? pm.cpu_2_percent.toFixed(0) + "%" : "-");
  setText($("v-c3"), pm.cpu_3_percent != null ? pm.cpu_3_percent.toFixed(0) + "%" : "-");
  setText($("cpu-freq"), pm.cpu_freq != null ? (pm.cpu_freq / 1000).toFixed(2) + " GHz" : "- GHz");
  setText($("v-load1"),  upTime.load_1  != null ? upTime.load_1.toFixed(2)  : "-");
  setText($("v-load5"),  upTime.load_5  != null ? upTime.load_5.toFixed(2)  : "-");
  setText($("v-load15"), upTime.load_15 != null ? upTime.load_15.toFixed(2) : "-");
  setText($("uptime"), "up " + fmtDuration(upTime.uptime_seconds));

  // ── CPU graph (main + per-core sparklines) ──
  // height 14 (7 up / 7 down) = 28 dot levels per half. Sqrt curve so 5%
  // still shows ~6 dots while 100% pegs at 28. Always 0–100 scale.
  // Each row is coloured along a green→yellow→red ramp by distance
  // from the midline, so dot density near the centre stays green and
  // dots near the outer edges (heavy load) trend red.
  const cpuEl = $("cpu-graph");
  if (cpuEl) {
    const w = charsFor(cpuEl, 9);
    paintGradientCpuGraph(cpuEl, hist.cpu_percent || [], {
      width: w, height: 14, max: 100, mirror: true, curve: 0.5,
    });
  }
  // 4 individual per-core sparklines. Auto-scale with a small floor
  // so light load still draws visible dots.
  for (let i = 0; i < 4; i++) {
    const el = $("core-spk-" + i);
    if (!el) continue;
    const w = charsFor(el, 9);
    const arr = hist["cpu_" + i + "_percent"] || [];
    setText(el, brailleChart(arr, { width: w, height: 1, max: "auto", floor: 8 }));
  }

  // ── memory ──
  renderMem(mi);
  // mem history sparkline at the bottom of the mem panel. Renders with
  // the same green→red ramp as the cpu graph: bottom rows are low %
  // (green = headroom), top rows are high % (red = pressure). Sized
  // dynamically so it flex-grows to make the mem panel match disks.
  const memGraphEl = $("mem-graph");
  if (memGraphEl) {
    const w = charsFor(memGraphEl, 9);
    const h = rowsFor(memGraphEl, 14);
    paintGradientGraph(memGraphEl, hist.memory_percent || [], {
      width: w, height: h, max: 100,
    });
  }
  setText($("mem-pct-now"),
    pm.memory_percent != null ? pm.memory_percent.toFixed(0) + "%" : "-");

  // ── disks (+ swap appended at the bottom) ──
  renderDisks(snap.disks);
  renderSwap(mi);

  // ── net ──
  // eth0 row shows "LAN-IP | PUBLIC-IP" — the LAN side is plain text,
  // the public side wears the .public-ip class so it blurs until hover.
  // wg0 row shows the PIA tunnel's exit IP with the same treatment.
  const ethLan = pm.ip_eth0 || "-";
  if (snap.public_ip) {
    setHTML($("eth-ip"),
      escapeHTML(ethLan) + " <span class='ip-sep'>|</span> " +
      "<span class='public-ip' title='public IP'>" +
      escapeHTML(snap.public_ip) + "</span>");
  } else {
    setText($("eth-ip"), ethLan);
  }

  // Prefer the collector's per-iface rates (snap.iface.eth0). Fall back to
  // pironman's system-wide number if the collector doesn't supply iface
  // counters (e.g. the very first sample).
  const ifaceNow = (snap.iface && snap.iface.eth0) || {};
  const ethDownNow = ifaceNow.down_Bps != null ? ifaceNow.down_Bps : pm.network_download_speed;
  const ethUpNow   = ifaceNow.up_Bps   != null ? ifaceNow.up_Bps   : pm.network_upload_speed;
  setText($("net-down-now"), fmtRate(ethDownNow) + " (" + fmtRateBs(ethDownNow) + ")");
  setText($("net-up-now"),   fmtRate(ethUpNow)   + " (" + fmtRateBs(ethUpNow)   + ")");
  updateNetTotals(ethDownNow, ethUpNow, snap.ts);
  setText($("net-down-top"),   fmtRate(netTopDown));
  setText($("net-up-top"),     fmtRate(netTopUp));
  setText($("net-down-total"), fmtBytes(netTotalDown));
  setText($("net-up-total"),   fmtBytes(netTotalUp));

  // wireguard 'now' values (top/total skipped — fewer numbers, cleaner)
  const wgNow = (snap.iface && snap.iface.wg0) || {};
  setText($("wg-down-now"), wgNow.down_Bps != null ? fmtRate(wgNow.down_Bps) : "(idle)");
  setText($("wg-up-now"),   wgNow.up_Bps   != null ? fmtRate(wgNow.up_Bps)   : "(idle)");
  // wg0 label: PIA's tunnel exit IP — wrapped in .public-ip so it
  // inherits the blur-until-hover treatment.
  if (snap.wg_public_ip) {
    setHTML($("wg-ip"),
      "<span class='public-ip' title='PIA exit IP'>" +
      escapeHTML(snap.wg_public_ip) + "</span>");
  } else {
    setText($("wg-ip"), "idle");
  }

  // separate graphs per interface, each is its own braille chart
  const eth0Down = hist.eth0_down && hist.eth0_down.length ? hist.eth0_down : hist.network_download_speed || [];
  const eth0Up   = hist.eth0_up   && hist.eth0_up.length   ? hist.eth0_up   : hist.network_upload_speed   || [];
  paintNetGraph("net-graph-eth0", eth0Down, eth0Up, 8, "eth0");
  paintNetGraph("net-graph-wg0",  hist.wg0_down || [], hist.wg0_up || [], 6, "wg0");

  // ── disk I/O sparkline (read on top, write below). Renders into
  //    the disks panel under the disk list. ──
  const ioEl = $("diskio-graph");
  if (ioEl) {
    const w = charsFor(ioEl, 9);
    const reads  = hist.disk_read  || [];
    const writes = hist.disk_write || [];
    const sharedMax = Math.max(
      1,
      reads.filter(Number.isFinite).length  ? Math.max(...reads.filter(Number.isFinite))  : 0,
      writes.filter(Number.isFinite).length ? Math.max(...writes.filter(Number.isFinite)) : 0,
    );
    const h = 6;
    const full = brailleChart(reads, {
      width: w, height: h, max: sharedMax, mirror: true, values2: writes,
    });
    const lines = full.split("\n");
    const half = h / 2;
    setHTML(ioEl,
      "<span class='read-half'>"  + escapeHTML(lines.slice(0, half).join("\n")) + "</span>\n" +
      "<span class='write-half'>" + escapeHTML(lines.slice(half).join("\n"))    + "</span>");
  }
  const dioNow = snap.disk_io || {};
  const ioR = Number.isFinite(dioNow.read_Bps)  ? dioNow.read_Bps  : null;
  const ioW = Number.isFinite(dioNow.write_Bps) ? dioNow.write_Bps : null;
  setText($("diskio-now"),
    "↓ " + (ioR != null ? fmtRateBs(ioR) : "-") +
    "  ↑ " + (ioW != null ? fmtRateBs(ioW) : "-"));

  // ── processes ──
  renderProc(snap.processes || [], (snap.processes || []).length);

  // ── slow-changing system info at the bottom of the proc panel ──
  renderSysinfo(snap);
  // ── eth0 details at the bottom of the net panel ──
  renderNetinfo(snap);

  lastSeenTs = snap.ts;
}

// ─────────────────────────────────────────────────────────────────────────────
// 物見 (monomi) logo. Render the kanji to an offscreen canvas at the
// browser's chosen Japanese font, then walk the pixel grid and emit a
// braille glyph for every 2×4 block where any pixel was painted. The
// result is the kanji in the same dot grammar as the rest of the
// dashboard's graphs. Static, rendered once on first paint.
// ─────────────────────────────────────────────────────────────────────────────
function kanjiToBraille(text, fontSize) {
  if (typeof document === "undefined") return "";
  // Canvas: each kanji square gets a fontSize × fontSize cell, with a
  // tiny right-side pad so adjacent glyphs don't touch.
  const cell = fontSize;
  const cw = cell * text.length;
  const ch = cell;
  const c = document.createElement("canvas");
  c.width = cw; c.height = ch;
  const ctx = c.getContext("2d");
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, cw, ch);
  ctx.fillStyle = "#fff";
  ctx.font = "700 " + cell + 'px "Noto Sans CJK JP","Hiragino Sans","Yu Gothic",sans-serif';
  ctx.textBaseline = "top";
  ctx.fillText(text, 0, 0);
  const data = ctx.getImageData(0, 0, cw, ch).data;
  const cols = Math.floor(cw / 2);
  const rows = Math.floor(ch / 4);
  const BL = [0x01, 0x02, 0x04, 0x40];
  const BR = [0x08, 0x10, 0x20, 0x80];
  let out = "";
  for (let y = 0; y < rows; y++) {
    let line = "";
    for (let x = 0; x < cols; x++) {
      let code = BRAILLE_BASE;
      for (let dy = 0; dy < 4; dy++) {
        const py = y * 4 + dy;
        if (data[(py * cw + x * 2)     * 4] > 128) code |= BL[dy];
        if (data[(py * cw + x * 2 + 1) * 4] > 128) code |= BR[dy];
      }
      line += String.fromCharCode(code);
    }
    out += line;
    if (y < rows - 1) out += "\n";
  }
  return out;
}

// ─────────────────────────────────────────────────────────────────────────────
// polling — self-rescheduling so requests can't overlap when the network
// round-trip happens to be longer than POLL_MS (which causes the cadence
// to feel inconsistent: 1s here, 2s there, 3s next).
// ─────────────────────────────────────────────────────────────────────────────
let pollTimer = null;

async function poll() {
  try {
    const r = await fetch("/api/stats", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const body = await r.json();
    paint(body);
  } catch (e) {
    console.warn("poll failed:", e);
  } finally {
    if (pollTimer) clearTimeout(pollTimer);
    pollTimer = setTimeout(poll, POLL_MS);
  }
}

(function init() {
  poll();   // kicks the self-rescheduling loop
  // one-shot: rasterise the monomi kanji into the logo slot.
  const logoEl = document.getElementById("logo");
  if (logoEl) logoEl.textContent = kanjiToBraille("物見", 32);
  let rzt = null;
  window.addEventListener("resize", () => {
    if (rzt) clearTimeout(rzt);
    rzt = setTimeout(() => {
      if (pollTimer) clearTimeout(pollTimer);
      poll();
    }, 100);
  });
})();
