/* 博物馆光照剂量审计 — 原生 JS，无第三方依赖 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  exhibits: [],
  cases: [],
  audit: null,
  simulation: null,
};

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function toast(msg, isErr = false) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast" + (isErr ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.add("hidden"), 4000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch (_) {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  if (res.status === 204) return null;
  return res.json();
}

function qs(params) {
  const usp = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== "") usp.set(k, v);
  });
  const s = usp.toString();
  return s ? "?" + s : "";
}

function fmt(n, d = 1) {
  if (n === null || n === undefined) return "—";
  return Number(n).toLocaleString("zh-CN", { maximumFractionDigits: d, minimumFractionDigits: 0 });
}
function fmtDate(isoStr) {
  if (!isoStr) return "—";
  return isoStr.replace("T", " ").replace("+00:00", " UTC");
}

// ---------------------------------------------------------------------------
// canvas chart helper (plain Canvas 2D)
// ---------------------------------------------------------------------------

function drawChart(canvas, series, opts = {}) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth || 600;
  const h = Number(canvas.getAttribute("height")) || 260;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, w, h);

  const pad = { l: 62, r: 18, t: 16, b: 34 };
  const iw = w - pad.l - pad.r;
  const ih = h - pad.t - pad.b;

  const allPts = series.flatMap((s) => s.data);
  const allMarks = series.flatMap((s) => s.points || []);
  const allBands = series.flatMap((s) => s.bands || []);
  const allX = [
    ...allPts.map((p) => p.x),
    ...allMarks.map((p) => p.x),
    ...allBands.flatMap((b) => [b.x0, b.x1]),
  ];
  const allY = [...allPts.map((p) => p.y), ...allMarks.map((p) => p.y)];
  if (allX.length < 1) {
    ctx.fillStyle = "#888";
    ctx.font = "13px sans-serif";
    ctx.fillText("暂无数据", pad.l, pad.t + 20);
    return;
  }
  const tMin = Math.min(...allX);
  const tMax = Math.max(...allX);
  let vMin = Math.min(0, ...allY);
  let vMax = Math.max(...allY, 1);
  if (opts.yMax !== undefined) vMax = opts.yMax;
  if (vMax === vMin) vMax = vMin + 1;

  const X = (t) => pad.l + ((t - tMin) / Math.max(1, tMax - tMin)) * iw;
  const Y = (v) => pad.t + ih - ((v - vMin) / (vMax - vMin)) * ih;

  // grid + y labels
  ctx.strokeStyle = "#eee5d6";
  ctx.fillStyle = "#8a8177";
  ctx.font = "10px sans-serif";
  ctx.lineWidth = 1;
  const nY = 4;
  for (let i = 0; i <= nY; i++) {
    const v = vMin + ((vMax - vMin) * i) / nY;
    const y = Y(v);
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.fillText(fmt(v, 0), 6, y + 3);
  }

  // x labels (start, 1/4, 1/2, 3/4, end)
  const nX = 4;
  for (let i = 0; i <= nX; i++) {
    const t = tMin + ((tMax - tMin) * i) / nX;
    const x = X(t);
    const label = new Date(t).toISOString().slice(5, 10);
    ctx.fillText(label, x - 14, h - 12);
  }

  // axis unit
  ctx.fillStyle = "#8a8177";
  ctx.fillText(opts.yLabel || "", 6, 12);

  // optional horizontal reference lines (e.g. annual limit)
  (opts.refLines || []).forEach((r) => {
    if (r.value < vMin || r.value > vMax) return;
    const y = Y(r.value);
    ctx.save();
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = r.color || "#c0392b";
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
    ctx.restore();
    if (r.label) {
      ctx.fillStyle = r.color || "#c0392b";
      ctx.fillText(r.label, w - pad.r - ctx.measureText(r.label).width - 4, y - 3);
    }
  });

  // series
  series.forEach((s) => {
    if (s.bands) {
      s.bands.forEach((b) => {
        ctx.fillStyle = b.color;
        ctx.globalAlpha = b.alpha ?? 0.35;
        ctx.fillRect(X(b.x0), pad.t, Math.max(1, X(b.x1) - X(b.x0)), ih);
        ctx.globalAlpha = 1;
      });
    }
    if (s.points) {
      s.points.forEach((p) => {
        ctx.fillStyle = p.color || s.color;
        ctx.beginPath();
        ctx.arc(X(p.x), Y(p.y), p.r || 4, 0, Math.PI * 2);
        ctx.fill();
        if (p.label) {
          ctx.fillStyle = p.color || s.color;
          ctx.font = "bold 10px sans-serif";
          ctx.fillText(p.label, X(p.x) + 6, Y(p.y) - 6);
          ctx.font = "10px sans-serif";
        }
      });
    }
    if (s.data.length >= 2) {
      ctx.strokeStyle = s.color;
      ctx.lineWidth = s.width || 2;
      ctx.setLineDash(s.dash || []);
      ctx.beginPath();
      s.data.forEach((p, i) => {
        const x = X(p.x), y = Y(p.y);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
      });
      ctx.stroke();
      ctx.setLineDash([]);
    }
  });

  // legend
  if (opts.legend) {
    let lx = pad.l;
    ctx.font = "11px sans-serif";
    opts.legend.forEach((lg) => {
      ctx.fillStyle = lg.color;
      ctx.fillRect(lx, 2, 14, 4);
      ctx.fillStyle = "#555";
      ctx.fillText(lg.label, lx + 18, 8);
      lx += 28 + ctx.measureText(lg.label).width;
    });
  }
}

function toTimeSeries(curve) {
  return curve.map((p) => ({ x: Date.parse(p.ts), y: p.cumulative_lux_hours }));
}

// ---------------------------------------------------------------------------
// loaders / renderers
// ---------------------------------------------------------------------------

async function loadAll() {
  state.exhibits = await api("/api/exhibits");
  state.cases = await api("/api/cases");
  renderSelects();
  renderCasesInfo();
  await renderOccupancies();
}

function renderSelects() {
  const exSel = $("#exhibit-select");
  const exOpts = state.exhibits.length
    ? state.exhibits.map((e) => `<option value="${e.id}">${e.name}（上限 ${fmt(e.annual_limit_lux_hours, 0)} lux·h）</option>`).join("")
    : `<option value="">（无展品）</option>`;
  exSel.innerHTML = exOpts;

  const caseOpts = state.cases.length
    ? state.cases.map((c) => `<option value="${c.id}">${c.name}</option>`).join("")
    : `<option value="">（无展柜）</option>`;
  $("#occ-case").innerHTML = caseOpts;
  $("#upload-case").innerHTML = caseOpts;
  $("#target-case").innerHTML = state.cases.length
    ? state.cases.map((c, i) => `<option value="${c.id}" ${i === 1 ? "selected" : ""}>${c.name}</option>`).join("")
    : `<option value="">（无展柜）</option>`;
}

function renderCasesInfo() {
  $("#cases-info").innerHTML = state.cases.map((c) =>
    `<span class="chip">🗄️ <b>${c.name}</b> · 阈值 ${c.gap_threshold_minutes} 分钟 · ` +
    `${fmt(c.reading_count, 0)} 条读数` +
    (c.first_ts ? ` · ${c.first_ts.slice(0, 16).replace("T", " ")} → ${c.last_ts.slice(5, 16).replace("T", " ")}` : "（空）") +
    `</span>`).join("");
}

async function renderOccupancies() {
  const exId = $("#exhibit-select").value;
  const list = $("#occ-list");
  if (!exId) { list.innerHTML = "<li>请先创建并选择展品</li>"; return; }
  const occs = await api("/api/occupancies?" + qs({ exhibit_id: exId }));
  if (!occs.length) {
    list.innerHTML = "<li>暂无在柜时间段</li>";
    return;
  }
  const cname = Object.fromEntries(state.cases.map((c) => [c.id, c.name]));
  list.innerHTML = occs.map((o) =>
    `<li><span>${cname[o.case_id] || ("#" + o.case_id)}：${fmtDate(o.start_ts)} → ${o.end_ts ? fmtDate(o.end_ts) : "仍在柜"}</span>` +
    `<button data-occ-del="${o.id}">删除</button></li>`).join("");
}

function renderTotals(a) {
  const t = a.totals;
  const usedPct = (t.total_lux_hours / a.exhibit.annual_limit_lux_hours) * 100;
  $("#totals").innerHTML = `
    <div class="metric ${usedPct >= 100 ? "warn" : ""}"><div class="v">${fmt(t.total_lux_hours)}</div><div class="k">累计剂量 lux·h（含断点）</div></div>
    <div class="metric"><div class="v">${fmt(t.audited_lux_hours)}</div><div class="k">审计剂量 lux·h（扣除断点段）</div></div>
    <div class="metric ${t.breakpoint_lux_hours > 0 ? "warn" : ""}"><div class="v">${fmt(t.breakpoint_lux_hours)}</div><div class="k">跨断点剂量 lux·h（${a.breakpoint_evidence.length} 段）</div></div>
    <div class="metric"><div class="v">${fmt(t.occupied_hours)}</div><div class="k">在柜小时数</div></div>
    <div class="metric"><div class="v">${fmt(t.covered_hours)}</div><div class="k">有读数覆盖小时数</div></div>
    <div class="metric ${usedPct >= 100 ? "warn" : ""}"><div class="v">${fmt(usedPct)}%</div><div class="k">占年度上限 ${fmt(a.exhibit.annual_limit_lux_hours, 0)} lux·h</div></div>`;
}

function renderDoseChart(a) {
  const data = toTimeSeries(a.cumulative_curve);
  const limit = a.exhibit.annual_limit_lux_hours;
  drawChart($("#chart-dose"), [
    { data, color: "#8a5a2b", width: 2 },
  ], {
    yLabel: "lux·h",
    yMax: Math.max(limit * 1.1, Math.max(...data.map((p) => p.y)) * 1.1),
    refLines: [{ value: limit, color: "#c0392b", label: "年度上限 " + fmt(limit, 0) }],
    legend: [{ label: "累计剂量（含断点）", color: "#8a5a2b" }],
  });

  $("#bycase").innerHTML = a.by_case.map((b) =>
    `<span class="chip">${b.case_name}：${fmt(b.total_lux_hours)} lux·h` +
    (b.breakpoint_lux_hours > 0 ? ` <span class="tag-bp">（断点 ${fmt(b.breakpoint_lux_hours)}）</span>` : "") +
    `</span>`).join("");
}

function renderEvidence(a) {
  const t0 = Date.parse(a.window.start_ts);
  const t1 = Date.parse(a.window.end_ts);
  const bpBands = a.breakpoint_evidence.map((e) => ({
    x0: Date.parse(e.reading_start), x1: Date.parse(e.reading_end),
    color: "#c0392b", alpha: 0.28,
  }));
  const gapBands = a.coverage_gaps.map((e) => ({
    x0: Date.parse(e.start), x1: Date.parse(e.end),
    color: "#d9930f", alpha: 0.30,
  }));

  // points: breakpoint trapezoids at their mid-time, y = their dose; gaps on baseline
  const bpPoints = a.breakpoint_evidence.map((e) => ({
    x: (Date.parse(e.reading_start) + Date.parse(e.reading_end)) / 2,
    y: e.lux_hours_in_occupancy,
    color: "#c0392b", r: 4,
  }));
  const gapPoints = a.coverage_gaps.map((e) => ({
    x: (Date.parse(e.start) + Date.parse(e.end)) / 2,
    y: 0,
    color: "#d9930f", r: 4,
  }));

  drawChart($("#chart-evidence"), [
    { data: [], bands: [...bpBands, ...gapBands], points: [...bpPoints, ...gapPoints], color: "#c0392b" },
  ], {
    yLabel: "lux·h/段",
    legend: [
      { label: `跨断点区间 × ${a.breakpoint_evidence.length}`, color: "#c0392b" },
      { label: `无读数覆盖 × ${a.coverage_gaps.length}`, color: "#d9930f" },
    ],
  });

  const bpRows = a.breakpoint_evidence.map((e) => `
    <tr><td class="tag-bp">断点</td><td>${e.case_name}</td>
    <td>${fmtDate(e.reading_start)}<br>${fmtDate(e.reading_end)}</td>
    <td>${fmt(e.gap_hours, 2)} h<br><span style="color:#999">阈值 ${fmt(e.threshold_hours, 2)} h</span></td>
    <td>${fmt(e.lux_start)} → ${fmt(e.lux_end)} lux</td>
    <td>${fmt(e.lux_hours_in_occupancy)}</td></tr>`).join("");
  const gapRows = a.coverage_gaps.map((e) => `
    <tr><td class="tag-gap">无覆盖</td><td>${e.case_name}</td>
    <td>${fmtDate(e.start)}<br>${fmtDate(e.end)}</td>
    <td>${fmt(e.hours, 2)} h</td><td colspan="2">${e.reason === "no_readings" ? "该柜完全没有读数" : "超出读数覆盖范围"}</td></tr>`).join("");
  $("#evidence-list").innerHTML =
    `<table><thead><tr><th>类型</th><th>展柜</th><th>时间范围 (UTC)</th><th>间隔</th><th>照度</th><th>剂量 lux·h</th></tr></thead>
     <tbody>${bpRows}${gapRows || '<tr><td colspan="6" style="color:#999">无覆盖缺口</td></tr>'}</tbody></table>` +
    (bpRows ? "" : "");
}

function renderSimulation(s) {
  state.simulation = s;
  const stayData = toTimeSeries(s.stay.projected_curve);
  const moveData = toTimeSeries(s.move.projected_curve);
  const limit = s.exhibit.annual_limit_lux_hours;
  const vMax = Math.max(limit * 1.15, ...stayData.map((p) => p.y), ...moveData.map((p) => p.y), 1);

  const markPoint = (isoStr) => isoStr ? {
    x: Date.parse(isoStr), y: limit, color: "#c0392b", r: 5,
    label: new Date(isoStr).toISOString().slice(0, 10),
  } : null;
  const pts = [markPoint(s.stay.projected_limit_ts), markPoint(s.move.projected_limit_ts)].filter(Boolean);

  drawChart($("#chart-sim"), [
    { data: stayData, color: "#8a5a2b", width: 2 },
    { data: moveData, color: "#2f6f6a", width: 2 },
    { data: [], points: pts, color: "#c0392b" },
  ], {
    yLabel: "lux·h",
    yMax: vMax,
    refLines: [{ value: limit, color: "#c0392b", label: "年度上限" }],
    legend: [
      { label: "留在原柜 " + s.stay.current_case_name, color: "#8a5a2b" },
      { label: `换入 ${s.move.current_case_name}`, color: "#2f6f6a" },
    ],
  });

  const row = (k, v1, v2) => `<tr><th>${k}</th><td class="stay-c">${v1}</td><td class="move-c">${v2}</td></tr>`;
  const dateCell = (sc) => {
    if (sc.projected_limit_crosses_annual_reset && !sc.projected_limit_ts) {
      return `<span class="big-date" style="color:#2f6f6a">年度窗口内不超限</span>` +
        (sc.note ? `<br><small>${sc.note}</small>` : "");
    }
    if (!sc.projected_limit_ts) return (sc.note || "无法预测");
    const d = fmtDate(sc.projected_limit_ts);
    return `<span class="big-date">${d}</span>` +
      (sc.projected_limit_crosses_annual_reset ? '<span class="flag">超出年度重置点</span>' : "") +
      (sc.note ? `<br><small>${sc.note}</small>` : "");
  };
  $("#sim-result").innerHTML = `
    <p style="margin:2px 0 8px">展品 <b>${s.exhibit.name}</b> · 年度窗口 ${fmtDate(s.annual_window.start_ts)} → ${fmtDate(s.annual_window.end_ts)}
      <br>评估时刻 as_of：${fmtDate(s.as_of_ts)} · 换柜时刻：${fmtDate(s.move_ts)}</p>
    <table>
      <thead><tr><th></th><th>留在 ${s.stay.current_case_name}</th><th>换入 ${s.move.current_case_name}</th></tr></thead>
      <tbody>
      ${row("年度已累计剂量 (lux·h)", fmt(s.stay.annual_dose_ytd_lux_hours), fmt(s.move.annual_dose_ytd_lux_hours))}
      ${row("剩余预算 (lux·h)", fmt(s.stay.remaining_budget_lux_hours), fmt(s.move.remaining_budget_lux_hours))}
      ${row("展柜历史平均照度 (lux)", fmt(s.stay.mean_lux_case_historical, 2), fmt(s.move.mean_lux_case_historical, 2))}
      ${row("剂量速率 (lux·h / 在柜小时)", fmt(s.stay.rate_lux_hours_per_occupied_hour, 3), fmt(s.move.rate_lux_hours_per_occupied_hour, 3))}
      <tr><th>预计达到 ${fmt(limit, 0)} lux·h 日期</th>
        <td class="stay-c">${dateCell(s.stay)}</td>
        <td class="move-c">${dateCell(s.move)}</td></tr>
      </tbody>
    </table>`;
}

// ---------------------------------------------------------------------------
// actions
// ---------------------------------------------------------------------------

async function currentExhibitId() {
  const v = $("#exhibit-select").value;
  if (!v) { toast("请先创建并选择展品", true); return null; }
  return Number(v);
}

async function runAudit() {
  const exId = await currentExhibitId();
  if (!exId) return;
  const params = {
    exhibit_id: exId,
    start_ts: $("#audit-start").value.trim(),
    end_ts: $("#audit-end").value.trim(),
    step_hours: $("#audit-step").value || 1,
  };
  const a = await api("/api/audit" + qs(params));
  state.audit = a;
  renderTotals(a);
  renderDoseChart(a);
  renderEvidence(a);
  toast(`审计完成：${fmt(a.totals.total_lux_hours)} lux·h，${a.breakpoint_evidence.length} 个断点区间`);
}

document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-action], button[data-occ-del]");
  if (!btn) return;
  const delId = btn.getAttribute("data-occ-del");
  try {
    if (delId) {
      await api("/api/occupancies/" + delId, { method: "DELETE" });
      await renderOccupancies();
      return;
    }
    const act = btn.dataset.action;

    if (act === "refresh") { await loadAll(); toast("已刷新"); return; }

    if (act === "add-case") {
      const name = $("#case-name").value.trim();
      if (!name) throw new Error("展柜名不能为空");
      await api("/api/cases", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, gap_threshold_minutes: Number($("#case-gap").value) || 60 }),
      });
      $("#case-name").value = "";
      await loadAll(); toast("展柜已创建");
    } else if (act === "add-exhibit") {
      const name = $("#exhibit-name").value.trim();
      if (!name) throw new Error("展品名不能为空");
      await api("/api/exhibits", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name, material: $("#exhibit-material").value.trim(),
          annual_limit_lux_hours: Number($("#exhibit-limit").value) || 50000,
        }),
      });
      $("#exhibit-name").value = ""; $("#exhibit-material").value = "";
      await loadAll(); toast("展品已创建");
    } else if (act === "add-occ") {
      const exId = await currentExhibitId(); if (!exId) return;
      const caseId = $("#occ-case").value;
      if (!caseId) throw new Error("请先创建展柜");
      const start = $("#occ-start").value.trim();
      if (!start) throw new Error("开始时间必填");
      await api("/api/occupancies", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          exhibit_id: exId, case_id: Number(caseId),
          start_ts: start, end_ts: $("#occ-end").value.trim() || null,
        }),
      });
      $("#occ-start").value = ""; $("#occ-end").value = "";
      await renderOccupancies(); toast("在柜时间段已添加");
    } else if (act === "upload-multi" || act === "upload-single") {
      const f = $("#readings-file").files[0];
      if (!f) throw new Error("请选择 JSONL 文件");
      const fd = new FormData();
      fd.append("file", f);
      let path;
      if (act === "upload-multi") {
        const c = $("#upload-case").value;
        if (c) fd.append("default_case", state.cases.find((x) => String(x.id) === String(c))?.name || "");
        path = "/api/readings/import";
      } else {
        const c = $("#upload-case").value;
        if (!c) throw new Error("请先创建并选择目标展柜");
        path = `/api/cases/${c}/readings/upload`;
      }
      const r = await api(path, { method: "POST", body: fd });
      await loadAll();
      toast(`照度数据已导入：${r.inserted_or_updated} 条读数`);
    } else if (act === "upload-occ") {
      const f = $("#occ-file").files[0];
      if (!f) throw new Error("请选择 JSONL 文件");
      const fd = new FormData();
      fd.append("file", f);
      const r = await api("/api/occupancies/import", { method: "POST", body: fd });
      await loadAll();
      toast(`在柜时间段已导入：${r.inserted} 条`);
    } else if (act === "seed") {
      const has = state.cases.length > 0;
      const fd = new FormData();
      fd.append("overwrite", has ? "true" : "false");
      const r = await api("/api/admin/seed", { method: "POST", body: fd });
      await loadAll();
      // auto-select the demo exhibit + prefill audit window
      const ex = state.exhibits.find((e) => e.name === "silk-scroll");
      if (ex) {
        $("#exhibit-select").value = String(ex.id);
        const targetB = state.cases.find((c) => c.name === "case-b");
        if (targetB) $("#target-case").value = String(targetB.id);
      }
      await renderOccupancies();
      toast(`示例数据就绪：${r.readings_upserted} 条读数、${r.occupancies_inserted} 条在柜段。点击“计算累计剂量”`);
    } else if (act === "run-audit") {
      await runAudit();
    } else if (act === "simulate") {
      const exId = await currentExhibitId(); if (!exId) return;
      const target = $("#target-case").value;
      if (!target) throw new Error("请先创建目标展柜");
      const s = await api("/api/simulate" + qs({
        exhibit_id: exId,
        target_case_id: target,
        move_ts: $("#move-ts").value.trim(),
        annual_start: $("#annual-start").value.trim(),
      }));
      renderSimulation(s);
      const st = s.stay.projected_limit_ts ? new Date(s.stay.projected_limit_ts).toISOString().slice(0, 10) : "—";
      const mv = s.move.projected_limit_ts ? new Date(s.move.projected_limit_ts).toISOString().slice(0, 10) : "—";
      toast(`预测：留柜 ${st} / 换柜 ${mv}`);
    } else if (act === "export") {
      const exId = await currentExhibitId(); if (!exId) return;
      const params = new URLSearchParams({
        exhibit_id: String(exId),
        step_hours: $("#audit-step").value || "1",
      });
      const st = $("#audit-start").value.trim();
      const en = $("#audit-end").value.trim();
      if (st) params.set("start_ts", st);
      if (en) params.set("end_ts", en);
      const target = $("#target-case").value;
      if (target) params.set("simulate_target_case_id", target);
      const res = await fetch("/api/audit/export?" + params.toString(), { method: "POST" });
      if (!res.ok) {
        let d = res.statusText;
        try { d = (await res.json()).detail ?? d; } catch (_) {}
        throw new Error(d);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = "light_audit.json"; a.click();
      URL.revokeObjectURL(url);
      toast("审计 JSON 已导出（含 SHA-256 校验和）");
    }
  } catch (err) {
    toast("错误：" + err.message, true);
  }
});

window.addEventListener("resize", () => {
  if (state.audit) { renderDoseChart(state.audit); renderEvidence(state.audit); }
  if (state.simulation) renderSimulation(state.simulation);
});

loadAll().catch((e) => toast("初始化失败：" + e.message, true));
