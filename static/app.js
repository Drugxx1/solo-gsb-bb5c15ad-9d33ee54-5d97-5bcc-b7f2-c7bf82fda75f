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
  rotRenderCapacityInputs();
  rotRefreshRows();
  await rotLoadBatches();
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
    } else if (act === "rot-add-row") {
      rotAddRow();
    } else if (act === "rot-row-del") {
      const row = btn.closest(".rot-row");
      if (row) row.remove();
      if (!$$("#rot-draft-rows .rot-row").length) rotAddRow();
    } else if (act === "rot-submit") {
      await rotSubmit();
    } else if (act === "rot-refresh-batches") {
      await rotLoadBatches();
      toast("批次列表已刷新");
    } else if (act === "rot-load") {
      await rotLoadBatch(Number(btn.getAttribute("data-batch")));
    } else if (act === "rot-accept" || act === "rot-revoke") {
      const draftId = btn.getAttribute("data-draft");
      const action = act === "rot-accept" ? "accept" : "revoke";
      const r = await api(`/api/rotation/drafts/${draftId}/${action}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ note: "" }),
      });
      toast(`草案 #${draftId} 已${action === "accept" ? "接受" : "撤销"}（v${r.version}）`);
      if (rotState.batchId) await rotLoadBatch(rotState.batchId, { scroll: false });
      await rotLoadBatches();
    } else if (act === "rot-versions") {
      await rotShowVersions(Number(btn.getAttribute("data-draft")));
    }
  } catch (err) {
    toast("错误：" + err.message, true);
  }
});

window.addEventListener("resize", () => {
  if (state.audit) { renderDoseChart(state.audit); renderEvidence(state.audit); }
  if (state.simulation) renderSimulation(state.simulation);
});

// ---------------------------------------------------------------------------
// 4 · 轮换排期（rotation scheduling）
// ---------------------------------------------------------------------------

const rotState = { batchId: null, detail: null };

const ROT_CONFLICT_NAMES = {
  exhibit_double_booked: "同一展品多柜冲突",
  case_over_capacity: "展柜超容",
};
const ROT_NOTE_NAMES = {
  measured: "实测积分",
  "measured+extrapolated": "实测＋历史均值外推",
  extrapolated_from_case_history: "历史均值外推",
  uncovered_not_estimable: "无读数且无法外推",
  no_readings: "展柜无读数",
};

function rotStatusTag(s) {
  const map = { draft: ["草案", "st-draft"], accepted: ["已接受", "st-ok"], revoked: ["已撤销", "st-rev"] };
  const [label, cls] = map[s] || [s, "st-draft"];
  return `<span class="stag ${cls}">${label}</span>`;
}

function rotRenderCapacityInputs() {
  const box = $("#rot-capacities");
  if (!state.cases.length) { box.innerHTML = '<span class="hint">（请先创建展柜）</span>'; return; }
  box.innerHTML = state.cases.map((c) =>
    `<label>${c.name} <input type="number" min="1" max="99" value="1" data-rot-cap="${c.id}" class="rot-cap-input"></label>`).join("");
}

function rotRowHtml() {
  const exOpts = state.exhibits.map((e) => `<option value="${e.id}">${e.name}</option>`).join("");
  const caseOpts = state.cases.map((c) => `<option value="${c.id}">${c.name}</option>`).join("");
  return `<div class="row rot-row">
    <select class="rot-ex" title="展品">${exOpts}</select>
    <select class="rot-case" title="展柜">${caseOpts}</select>
    <input class="rot-start" placeholder="开始 ISO8601，如 2026-10-01T00:00:00Z">
    <input class="rot-end" placeholder="结束（留空=开放段）">
    <input class="rot-note" placeholder="备注">
    <button data-action="rot-row-del" class="secondary" title="删除此行">✕</button>
  </div>`;
}

function rotAddRow() {
  $("#rot-draft-rows").insertAdjacentHTML("beforeend", rotRowHtml());
}

function rotRefreshRows() {
  if (!$$("#rot-draft-rows .rot-row").length) rotAddRow();
  $$("#rot-draft-rows .rot-row").forEach((row) => {
    const exSel = row.querySelector(".rot-ex");
    const caseSel = row.querySelector(".rot-case");
    const exV = exSel.value, caseV = caseSel.value;
    exSel.innerHTML = state.exhibits.map((e) => `<option value="${e.id}">${e.name}</option>`).join("");
    caseSel.innerHTML = state.cases.map((c) => `<option value="${c.id}">${c.name}</option>`).join("");
    if (exV) exSel.value = exV;
    if (caseV) caseSel.value = caseV;
  });
}

async function rotSubmit() {
  if (!state.exhibits.length || !state.cases.length) throw new Error("请先创建展品与展柜");
  const name = $("#rot-name").value.trim() || `排期批次 ${new Date().toISOString().slice(0, 16).replace("T", " ")}`;
  const capacities = $$("[data-rot-cap]").map((inp) => ({
    case_id: Number(inp.getAttribute("data-rot-cap")), capacity: Number(inp.value) || 1,
  }));
  const rows = $$("#rot-draft-rows .rot-row");
  if (!rows.length) throw new Error("请至少添加一条草案");
  const drafts = rows.map((row, i) => {
    const start = row.querySelector(".rot-start").value.trim();
    if (!start) throw new Error(`第 ${i + 1} 行草案缺少开始时间`);
    return {
      exhibit_id: Number(row.querySelector(".rot-ex").value),
      case_id: Number(row.querySelector(".rot-case").value),
      start_ts: start,
      end_ts: row.querySelector(".rot-end").value.trim() || null,
      note: row.querySelector(".rot-note").value.trim(),
    };
  });
  const detail = await api("/api/rotation/batches", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name, note: "",
      annual_start: $("#rot-annual-start").value.trim() || null,
      capacities, drafts,
    }),
  });
  rotState.batchId = detail.batch.id;
  rotRender(detail);
  $("#rot-draft-rows").innerHTML = "";
  rotAddRow();
  await rotLoadBatches();
  const a = detail.analysis;
  toast(`批次 #${detail.batch.id} 已创建：${a.conflict_chains.length} 条冲突链，` +
    `${a.over_limit_plans.length} 件展品超限`);
}

async function rotLoadBatches() {
  const list = await api("/api/rotation/batches");
  const box = $("#rot-batch-list");
  if (!list.length) { box.innerHTML = '<p class="hint">暂无排期批次</p>'; return; }
  box.innerHTML = list.map((b) => `<div class="row rot-batch-row">
    <span><b>#${b.id} ${b.name}</b><br>
    <span class="hint">${fmtDate(b.created_at)} · 草案 ${b.draft_count}（接受 ${b.accepted_count} / 撤销 ${b.revoked_count}）</span></span>
    <button data-action="rot-load" data-batch="${b.id}" class="secondary">载入分析</button>
  </div>`).join("");
}

async function rotLoadBatch(id, opts = {}) {
  const detail = await api(`/api/rotation/batches/${id}`);
  rotState.batchId = id;
  rotRender(detail);
  if (opts.scroll !== false) $("#rot-result").scrollIntoView({ behavior: "smooth", block: "start" });
}

function rotRender(detail) {
  rotState.detail = detail;
  $("#rot-result").classList.remove("hidden");
  const a = detail.analysis;
  $("#rot-summary").innerHTML = `
    <div class="metric ${a.conflict_chains.length ? "warn" : ""}"><div class="v">${a.conflict_chains.length}</div><div class="k">冲突链</div></div>
    <div class="metric"><div class="v">${a.involved_exhibits.length}</div><div class="k">涉及展品</div></div>
    <div class="metric ${a.over_limit_plans.length ? "warn" : ""}"><div class="v">${a.over_limit_plans.length}</div><div class="k">超限计划</div></div>
    <div class="metric"><div class="v">${detail.drafts.length}</div><div class="k">草案总数</div></div>
    <div class="metric"><div class="v" style="font-size:14px">${fmtDate(a.annual_window.start_ts).slice(0, 10)}<br>${fmtDate(a.annual_window.end_ts).slice(0, 10)}</div><div class="k">年度窗口（UTC）</div></div>`;
  rotRenderChains(a);
  rotRenderPlans(a);
  rotRenderDrafts(detail);
  $("#rot-versions").innerHTML = "";
}

function rotRenderChains(a) {
  const box = $("#rot-chains");
  let html = "";
  if (a.outside_annual_window.length) {
    html += `<p class="hint">⚠️ ${a.outside_annual_window.length} 条草案在年度窗口之外，未参与扫描：${a.outside_annual_window.map((d) => "#" + d.draft_id).join("、")}</p>`;
  }
  if (!a.conflict_chains.length) {
    box.innerHTML = html + '<p class="hint">✅ 未发现同一展品多柜冲突或展柜超容。</p>';
    return;
  }
  box.innerHTML = html + a.conflict_chains.map((ch) => {
    const periods = ch.periods.map((p) => `<tr>
      <td class="tag-bp">${ROT_CONFLICT_NAMES[p.type] || p.type}</td>
      <td>${fmtDate(p.start_ts)}<br>${fmtDate(p.end_ts)}</td>
      <td>${p.type === "case_over_capacity"
        ? `${p.case_name}：同时在柜 ${p.active} 件 / 容量 ${p.capacity}`
        : `展品 ${p.exhibit_name}（${p.active} 段重叠）`}</td></tr>`).join("");
    const drafts = ch.drafts.map((d) => `<tr>
      <td>#${d.draft_id}</td><td>${d.exhibit_name}</td><td>${d.case_name}</td>
      <td>${fmtDate(d.start_ts)}</td><td>${d.open_ended ? "开放段" : fmtDate(d.end_ts)}</td>
      <td>${rotStatusTag(d.status)}</td></tr>`).join("");
    const exChips = ch.exhibits.map((e) => `<span class="chip">🏺 ${e.name}</span>`).join("");
    const caseChips = ch.cases.map((c) => `<span class="chip">🗄️ ${c.name}</span>`).join("");
    return `<div class="chain">
      <h4>冲突链 #${ch.chain_no} · 始于 ${fmtDate(ch.start_ts)} · ${ch.types.map((t) => ROT_CONFLICT_NAMES[t]).join("＋")}</h4>
      <div class="row">${exChips}${caseChips}</div>
      <table class="tbl"><thead><tr><th>冲突类型</th><th>区间 (UTC)</th><th>详情</th></tr></thead><tbody>${periods}</tbody></table>
      <table class="tbl"><thead><tr><th>草案</th><th>展品</th><th>展柜</th><th>开始</th><th>结束</th><th>状态</th></tr></thead><tbody>${drafts}</tbody></table>
    </div>`;
  }).join("");
}

function rotRenderPlans(a) {
  const box = $("#rot-plans");
  if (!a.exhibit_plans.length) { box.innerHTML = '<p class="hint">批次内没有生效中的草案（已撤销的草案不参与估算）。</p>'; return; }
  box.innerHTML = a.exhibit_plans.map((p) => {
    const segs = p.segments.map((s) => `<tr>
      <td>#${s.draft_id}</td><td>${s.case_name}</td>
      <td>${fmtDate(s.start_ts)}<br>${s.open_ended ? "开放段→" : ""}${fmtDate(s.end_ts)}</td>
      <td>${fmt(s.measured_lux_hours)}</td><td>${fmt(s.estimated_lux_hours)}</td>
      <td><b>${fmt(s.dose_lux_hours)}</b></td>
      <td>${ROT_NOTE_NAMES[s.estimate_note] || s.estimate_note}</td></tr>`).join("");
    return `<div class="block ${p.exceeds_limit ? "over" : ""}">
      <h4>${p.exhibit_name} ${p.exceeds_limit ? '<span class="flag">超出年度上限</span>' : ""}</h4>
      <div class="totals">
        <div class="metric"><div class="v">${fmt(p.baseline_lux_hours)}</div><div class="k">既有在柜剂量 lux·h</div></div>
        <div class="metric"><div class="v">${fmt(p.planned_lux_hours)}</div><div class="k">本批计划剂量 lux·h</div></div>
        <div class="metric ${p.exceeds_limit ? "warn" : ""}"><div class="v">${fmt(p.total_lux_hours)}</div>
          <div class="k">合计 / 上限 ${fmt(p.annual_limit_lux_hours, 0)} lux·h${p.exceeds_limit ? `（超 ${fmt(p.excess_lux_hours)}）` : ""}</div></div>
      </div>
      <table class="tbl"><thead><tr><th>草案</th><th>展柜</th><th>区间 (UTC)</th><th>实测 lux·h</th><th>外推 lux·h</th><th>合计</th><th>估算方式</th></tr></thead>
      <tbody>${segs}</tbody></table>
    </div>`;
  }).join("");
}

function rotRenderDrafts(detail) {
  const rows = detail.drafts.map((d) => `<tr>
    <td>#${d.id}</td><td>${d.exhibit_name}</td><td>${d.case_name}</td>
    <td>${fmtDate(d.start_ts)}</td><td>${d.end_ts ? fmtDate(d.end_ts) : "开放段"}</td>
    <td>${rotStatusTag(d.status)} <span class="hint">v${d.version}</span></td>
    <td class="rot-ops">
      <button data-action="rot-accept" data-draft="${d.id}" ${d.status === "accepted" ? "disabled" : ""}>接受</button>
      <button data-action="rot-revoke" data-draft="${d.id}" class="secondary" ${d.status === "revoked" ? "disabled" : ""}>撤销</button>
      <button data-action="rot-versions" data-draft="${d.id}" class="secondary">版本</button>
    </td></tr>`).join("");
  $("#rot-drafts").innerHTML =
    `<table class="tbl"><thead><tr><th>草案</th><th>展品</th><th>展柜</th><th>开始</th><th>结束</th><th>状态</th><th>操作</th></tr></thead>
     <tbody>${rows}</tbody></table>`;
}

async function rotShowVersions(draftId) {
  const vs = await api(`/api/rotation/drafts/${draftId}/versions`);
  $("#rot-versions").innerHTML = `<h4>草案 #${draftId} 版本历史</h4>` + vs.map((v) =>
    `<div class="row vrow"><span class="chip">v${v.version}</span> ${rotStatusTag(v.status)}` +
    `<span class="hint">${fmtDate(v.created_at)}${v.note ? " · " + v.note : ""}</span></div>`).join("");
}

loadAll().catch((e) => toast("初始化失败：" + e.message, true));
