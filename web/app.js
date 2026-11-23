/* Sparse matrix repository — UI */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const ui = {
  summary: null,
  queryLayout: "csr",
  arrayLayout: "csr",
  compareLayout: "csr",
  storageVersion: null,
  pollTimer: null,
  busy: false,
};

/* ------------------------------------------------------------------ api */
async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch (_) { data = { error: text }; }
  if (!res.ok) {
    const err = new Error((data && data.error) || `HTTP ${res.status}`);
    err.status = res.status;
    err.payload = data;
    throw err;
  }
  return data;
}

function toast(message, kind = "") {
  const el = $("#toast");
  el.textContent = message;
  el.className = "toast" + (kind ? " " + kind : "");
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 3200);
}

function showMsg(sel, text, kind = "info") {
  const el = $(sel);
  if (!text) { el.hidden = true; el.textContent = ""; return; }
  el.hidden = false;
  el.className = "msg " + kind;
  el.textContent = text;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmtValue(kind, value) {
  if (kind === "zero") return "0";
  if (kind === "missing") return "缺测";
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value);
  return Number.isInteger(n) ? String(n) : String(+n.toFixed(10));
}

function badge(kind, label) {
  if (kind === "absent") return `<span class="badge absent">${esc(label || "无元素")}</span>`;
  const names = { value: "数值", zero: "显式零", missing: "显式缺测" };
  return `<span class="badge ${kind}">${esc(label || names[kind] || kind)}</span>`;
}

/* ---------------------------------------------------------------- tabs */
$$("#tabs button").forEach(btn => {
  btn.addEventListener("click", () => {
    $$("#tabs button").forEach(b => b.classList.toggle("active", b === btn));
    $$(".panel").forEach(p => p.classList.toggle("active", p.id === `panel-${btn.dataset.panel}`));
    if (btn.dataset.panel === "storage") loadArrays(ui.arrayLayout);
    if (btn.dataset.panel === "explore") loadArrays(ui.queryLayout, "#arrayView", "#arrayMeta");
  });
});

/* -------------------------------------------------------------- summary */
async function refresh({ quiet = true } = {}) {
  try {
    ui.summary = await api("GET", "/api/state");
  } catch (err) {
    if (!quiet) toast(err.message, "bad");
    return;
  }
  renderMeta();
  renderVersions();
  renderLabels();
  renderSources();
  renderExports();
  renderAudit();
  renderStorage();
  syncSelects();
  schedulePoll();
}

function renderMeta() {
  const s = ui.summary;
  if (!s) return;
  $("#matrixMeta").textContent = s.matrix_id ? `matrix ${s.matrix_id.slice(0, 8)} · ${s.shape[0]}×${s.shape[1]}` : "空仓库";
  $("#revMeta").textContent = `rev ${s.revision}`;
}

function versionById(id) {
  return (ui.summary?.versions || []).find(v => v.version_id === Number(id)) || null;
}

function renderVersions() {
  const root = $("#versionList");
  const versions = ui.summary?.versions || [];
  if (!versions.length) {
    root.innerHTML = `<div class="empty">还没有数据版。粘贴三元组或点一个样例开始。</div>`;
    return;
  }
  root.innerHTML = versions.map(v => {
    const lay = Object.entries(v.layouts).map(([name, l]) =>
      `<span class="lay-pill ${l.state}">${name.toUpperCase()} · ${l.state}${l.state === "building" && l.progress ? " " + l.progress.percent + "%" : ""}</span>`
    ).join("");
    const notices = (v.notices || []).map(n => `<div class="muted small">· ${esc(n)}</div>`).join("");
    return `<div class="version">
      <div class="vh"><strong>#${v.version_id} ${esc(v.label)}</strong>
        <span class="pill">${v.policy === "sum" ? "求和" : "最新"}</span>
        <span class="meta muted small" style="margin-left:auto">${v.shape[0]}×${v.shape[1]} · ${v.element_count} 元素</span>
      </div>
      <div class="vmeta">
        <span>数值 ${v.counts.value}</span><span>显式零 ${v.counts.zero}</span><span>显式缺测 ${v.counts.missing}</span>
        ${v.empty_rows.length ? `<span>空行 ${v.empty_rows.length}</span>` : ""}
        ${v.empty_cols.length ? `<span>空列 ${v.empty_cols.length}</span>` : ""}
      </div>
      ${notices}
      <div class="vlayout">${lay || `<span class="lay-pill">尚未转换</span>`}</div>
    </div>`;
  }).join("");
}

function syncSelects() {
  const versions = (ui.summary?.versions || []).slice();
  const specs = [
    ["#qVersion", null], ["#sVersion", "storageVersion"],
    ["#cVersion", null], ["#dVersion", null],
  ];
  for (const [sel, key] of specs) {
    const el = $(sel);
    if (!el) continue;
    const prev = el.value;
    el.innerHTML = versions.map(v =>
      `<option value="${v.version_id}">#${v.version_id} ${esc(v.label)}</option>`).join("");
    const wanted = (key && ui[key]) || prev;
    if (wanted && versions.some(v => String(v.version_id) === String(wanted))) {
      el.value = String(wanted);
    } else if (versions.length) {
      el.value = String(versions[0].version_id);
    }
    if (key) ui[key] = el.value ? Number(el.value) : null;
  }
}

/* -------------------------------------------------------------- labels */
function renderLabels() {
  const s = ui.summary;
  if (!s) return;
  const latest = s.versions[0];
  const emptyRow = new Set(latest?.empty_rows || []);
  const emptyCol = new Set(latest?.empty_cols || []);
  for (const axis of ["row", "col"]) {
    const root = $(axis === "row" ? "#rowLabels" : "#colLabels");
    const labels = s.labels[axis] || [];
    if (!labels.length) {
      root.innerHTML = `<div class="empty">暂无${axis === "row" ? "行" : "列"}标签</div>`;
      continue;
    }
    const isEmpty = axis === "row" ? emptyRow : emptyCol;
    root.innerHTML = labels.map((lab, i) => `
      <div class="label-item ${isEmpty.has(lab.name) ? "empty" : ""}" data-axis="${axis}" data-id="${lab.id}">
        <span class="pos">${i}</span>
        <span class="lid">#${lab.id}</span>
        <input class="name" value="${esc(lab.name)}" aria-label="标签名称">
        <span class="btns">
          <button class="ghost" data-move="-1" title="上移" ${i === 0 ? "disabled" : ""}>↑</button>
          <button class="ghost" data-move="1" title="下移" ${i === labels.length - 1 ? "disabled" : ""}>↓</button>
          <button class="ghost" data-rename>重命名</button>
        </span>
      </div>`).join("");
  }
}

async function reorder(axis, index, delta) {
  const labels = ui.summary.labels[axis].slice();
  const target = index + delta;
  if (target < 0 || target >= labels.length) return;
  [labels[index], labels[target]] = [labels[target], labels[index]];
  try {
    const res = await api("POST", `/api/labels/${axis}/order`, { order: labels.map(l => l.id) });
    toast(`已重排${axis === "row" ? "行" : "列"}标签：位置改变，元素与布局含义不变`);
    await refresh();
  } catch (err) { toast(err.message, "bad"); }
}

document.addEventListener("click", async ev => {
  const item = ev.target.closest(".label-item");
  if (!item) return;
  const axis = item.dataset.axis;
  const id = Number(item.dataset.id);
  const labels = ui.summary.labels[axis];
  const index = labels.findIndex(l => l.id === id);
  if (ev.target.dataset.move !== undefined) {
    await reorder(axis, index, Number(ev.target.dataset.move));
  } else if (ev.target.dataset.rename !== undefined) {
    const name = item.querySelector("input.name").value.trim();
    try {
      await api("POST", `/api/labels/${axis}/${id}`, { name });
      toast("标签已重命名（编号不变）");
      await refresh();
    } catch (err) { toast(err.message, "bad"); }
  }
});

$("#btnAddLabel").addEventListener("click", async () => {
  const axis = $("#newLabelAxis").value;
  const name = $("#newLabelName").value.trim();
  if (!name) return showMsg("#labelMsg", "请输入标签名称", "warn");
  try {
    const res = await api("POST", "/api/labels", { axis, name });
    $("#newLabelName").value = "";
    showMsg("#labelMsg",
      `已新增${axis === "row" ? "行" : "列"}标签 “${res.label.name}”（编号 #${res.label.id}）。` +
      (res.invalidated_layouts.length
        ? `\n${res.invalidated_layouts.length} 个已发布布局因矩阵维度变化而失效，需重新转换：${res.invalidated_layouts.join(", ")}`
        : "\n既有元素含义保持不变。"), "ok");
    await refresh();
  } catch (err) { showMsg("#labelMsg", err.message, "bad"); }
});

/* ------------------------------------------------------------- sources */
function renderSources() {
  const root = $("#sourceList");
  const sources = ui.summary?.sources || [];
  if (!sources.length) { root.innerHTML = `<div class="empty">暂无来源记录</div>`; return; }
  root.innerHTML = sources.map(s => `
    <div class="source-item">
      <span class="sid">#${s.id}</span>
      <span>${esc(s.name)}</span>
      ${s.deleted ? `<span class="gone">已删除（数值保留）</span>`
        : `<button class="danger" data-del-source="${s.id}">删除来源</button>`}
    </div>`).join("");
}

document.addEventListener("click", async ev => {
  const btn = ev.target.closest("[data-del-source]");
  if (!btn) return;
  const id = Number(btn.dataset.delSource);
  if (!confirm("删除该来源记录？元素数值会保留，只移除来源归属。")) return;
  try {
    const res = await api("DELETE", `/api/sources/${id}`);
    showMsg("#sourceMsg", `来源 #${id} 已删除；${res.orphaned_elements} 个元素的来源归属被移除，数值未变。`, "ok");
    await refresh();
  } catch (err) { showMsg("#sourceMsg", err.message, "bad"); }
});

$("#btnAddSource").addEventListener("click", async () => {
  const name = $("#sourceName").value.trim();
  if (!name) return showMsg("#sourceMsg", "请输入来源名称", "warn");
  try {
    await api("POST", "/api/sources", { name });
    $("#sourceName").value = "";
    showMsg("#sourceMsg", `已添加来源 “${name}”`, "ok");
    await refresh();
  } catch (err) { showMsg("#sourceMsg", err.message, "bad"); }
});

/* -------------------------------------------------------------- import */
const SAMPLES = {
  sum: `row,col,value,source_version,source
A,2024,1.50,1,budget
A,2024,-1.50,2,budget
A,2025,0,1,budget
A,2026,,1,budget
B,2024,,2,actuals
B,2025,3.25,1,actuals
C,2026,7,1,budget`,
  latest: `row,col,value,source_version,source
A,2024,1.50,1,plan
A,2024,2.75,3,actuals
A,2024,2.00,2,revised
B,2025,,1,plan
B,2025,4.50,2,actuals
C,2024,6.00,1,plan
C,2024,,4,revised`,
  missing: `row,col,value,source_version,source
A,2026,,1,plan
B,2026,NA,2,actuals
C,2026,,3,plan
A,2024,1,1,plan
B,2024,2,1,actuals`,
  labels: `row,col,value,source_version,source
A,2024,1,1,plan
B,2024,2,1,actuals
A,2025,3,1,plan`,
};

document.addEventListener("click", async ev => {
  const chip = ev.target.closest("[data-sample]");
  if (!chip) return;
  const key = chip.dataset.sample;
  $("#tripleText").value = SAMPLES[key];
  const labels = { sum: "样例：重复坐标相消为零", latest: "样例：按来源版本取最新",
    missing: "样例：只含缺测的列", labels: "样例：新增标签（空行空列）" };
  if (key === "latest") $("#policy").value = "latest";
  else $("#policy").value = "sum";
  if (key === "missing") $("#versionLabel").value = "缺测列样例";
  else if (key === "labels") $("#versionLabel").value = "标签样例";
  else $("#versionLabel").value = labels[key];
  $("#importLayout").value = "csr";
  showMsg("#importMsg",
    key === "labels"
      ? "导入基础矩阵后会再新增一个空行标签与一个空列标签；标签只增不改既有元素。"
      : "样例已填入。点击“导入为新的数据版”。", "info");
  if (key === "labels") {
    try {
      await runImport({ label: "标签样例", quiet: true });
      const rowRes = await api("POST", "/api/labels", { axis: "row", name: "D-空行" });
      const colRes = await api("POST", "/api/labels", { axis: "col", name: "2027-空列" });
      showMsg("#importMsg",
        `已导入并新增空行 “${rowRes.label.name}” 与空列 “${colRes.label.name}”。` +
        `\n它们没有元素但保留在维度中；可在此版本上重新转换布局。`, "ok");
      await refresh();
    } catch (err) { showMsg("#importMsg", err.message, "bad"); }
  }
});

async function runImport({ label, quiet = false } = {}) {
  const text = $("#tripleText").value;
  if (!text.trim()) throw new Error("三元组文本为空");
  const body = {
    text,
    policy: $("#policy").value,
    label: label || $("#versionLabel").value,
    layout: $("#importLayout").value,
    source: $("#defaultSource").value.trim() || null,
    source_version: Number($("#sourceVersion").value || 1),
  };
  const view = await api("POST", "/api/import", body);
  if (!quiet) {
    const bits = [`数据版 #${view.version_id} 已创建：${view.shape[0]}×${view.shape[1]}，${view.element_count} 个逻辑元素`];
    if (view.import.duplicates) bits.push(`合并重复坐标 ${view.import.duplicates} 次`);
    if (view.import.cancelled_to_zero) bits.push(`${view.import.cancelled_to_zero} 个坐标求和为零（保留为显式零）`);
    bits.push(`显式零 ${view.counts.zero}，显式缺测 ${view.counts.missing}`);
    if (view.notices?.length) bits.push(...view.notices);
    showMsg("#importMsg", bits.join("\n"), "ok");
  }
  return view;
}

$("#btnImport").addEventListener("click", async () => {
  try {
    await runImport();
    await refresh();
  } catch (err) { showMsg("#importMsg", err.message, "bad"); }
});

/* --------------------------------------------------------------- query */
$$("#queryLayout button").forEach(b => b.addEventListener("click", () => {
  ui.queryLayout = b.dataset.layout;
  $$("#queryLayout button").forEach(x => x.classList.toggle("active", x === b));
  loadArrays(ui.queryLayout, "#arrayView", "#arrayMeta");
}));

function splitNames(raw) {
  return raw.split(",").map(s => s.trim()).filter(Boolean);
}

async function runQuery() {
  const vid = $("#qVersion").value;
  if (!vid) return showMsg("#queryMsg", "请先导入数据版", "warn");
  const params = new URLSearchParams({ layout: ui.queryLayout, limit: "2000" });
  const rows = splitNames($("#qRows").value);
  const cols = splitNames($("#qCols").value);
  if (rows.length) params.set("rows", rows.join(","));
  if (cols.length) params.set("cols", cols.join(","));
  if ($("#qRowPos").value.trim()) params.set("rowpos", $("#qRowPos").value.trim());
  if ($("#qColPos").value.trim()) params.set("colpos", $("#qColPos").value.trim());
  if ($("#qKinds").value) params.set("kinds", $("#qKinds").value);
  try {
    const res = await api("GET", `/api/versions/${vid}/query?${params}`);
    renderQuery(res);
    showMsg("#queryMsg", "", "info");
  } catch (err) {
    $("#queryTable").innerHTML = "";
    $("#queryCount").textContent = "";
    showMsg("#queryMsg", err.message, "bad");
  }
}

function renderQuery(res) {
  const table = $("#queryTable");
  $("#queryCount").textContent = `${res.total_matches} 命中 / ${res.nnz_total} 个存储槽位`;
  if (!res.matches.length) {
    table.innerHTML = `<tbody><tr><td class="empty">没有匹配元素（可能是空切片，或该坐标本就没有元素）</td></tr></tbody>`;
    return;
  }
  table.innerHTML = `<thead><tr>
      <th>行</th><th>列</th><th>状态</th><th style="text-align:right">数值</th>
      <th style="text-align:right">槽位</th><th>行/列编号</th>
    </tr></thead><tbody>${res.matches.map(m => `<tr>
      <td>${esc(m.row)}</td><td>${esc(m.col)}</td><td>${badge(m.kind)}</td>
      <td class="num">${esc(fmtValue(m.kind, m.value))}</td>
      <td class="num mono">${m.slot}</td>
      <td class="mono">#${m.row_id} / #${m.col_id}</td>
    </tr>`).join("")}</tbody>`;
}

$("#btnQuery").addEventListener("click", runQuery);

$("#btnPoint").addEventListener("click", async () => {
  const vid = $("#qVersion").value;
  const row = $("#ptRow").value.trim();
  const col = $("#ptCol").value.trim();
  if (!vid || !row || !col) return showMsg("#queryMsg", "点查需要数据版、行标签与列标签", "warn");
  try {
    const res = await api("GET", `/api/versions/${vid}/point?layout=${ui.queryLayout}&row=${encodeURIComponent(row)}&col=${encodeURIComponent(col)}`);
    const table = $("#queryTable");
    if (res.present) {
      table.innerHTML = `<thead><tr><th>行</th><th>列</th><th>状态</th><th style="text-align:right">数值</th><th style="text-align:right">槽位</th></tr></thead>
        <tbody><tr><td>${esc(res.row)}</td><td>${esc(res.col)}</td><td>${badge(res.kind)}</td>
        <td class="num">${esc(fmtValue(res.kind, res.value))}</td><td class="num mono">${res.slot}</td></tr></tbody>`;
      $("#queryCount").textContent = "点查命中";
    } else {
      table.innerHTML = `<thead><tr><th>行</th><th>列</th><th>状态</th></tr></thead>
        <tbody><tr><td>${esc(res.row)}</td><td>${esc(res.col)}</td><td>${badge("absent")}</td></tr></tbody>`;
      $("#queryCount").textContent = "点查：无元素";
    }
    showMsg("#queryMsg", res.present ? "" : "该坐标没有元素——这与显式零、显式缺测不同。", "info");
  } catch (err) { showMsg("#queryMsg", err.message, "bad"); }
});

/* -------------------------------------------------------------- arrays */
async function loadArrays(layout, target = "#storageArrays", metaSel = null) {
  const vid = target === "#arrayView" ? $("#qVersion").value : $("#sVersion").value;
  const root = $(target);
  if (!vid) { root.innerHTML = `<div class="empty">请选择数据版</div>`; return; }
  try {
    const res = await api("GET", `/api/versions/${vid}/layouts/${layout}/arrays?limit=200`);
    renderArrays(res, root);
    if (metaSel) {
      $(metaSel).textContent = `${layout.toUpperCase()} · ${res.shape[0]}×${res.shape[1]} · nnz ${res.nnz} · ${res.arrays_bytes} B`;
    }
  } catch (err) {
    root.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
    if (metaSel) $(metaSel).textContent = "";
  }
}

function renderArrays(res, root) {
  const kinds = res.kinds;
  const kindMark = k => k === "zero" ? "zero" : (k === "missing" ? "missing" : "value");
  const data = res.data.map((v, i) => {
    const k = kinds[i];
    const cls = kindMark(k);
    const text = k === "missing" ? "NaN" : (k === "zero" ? "0.0" : Number(v).toString());
    return `<span class="${cls}">${text}</span>`;
  }).join(", ");
  const kindText = kinds.map(k => `<span class="${kindMark(k)}">${k}</span>`).join(", ");
  root.innerHTML = `
    <div class="arr">
      <div class="arr-h"><strong>indptr</strong><span class="muted">int64[${res.indptr_len}] · 偏移${res.truncated ? "（截断）" : ""}</span></div>
      <div class="arr-b">[${res.indptr.join(", ")}${res.indptr.length < res.indptr_len ? ", …" : ""}]</div>
    </div>
    <div class="arr">
      <div class="arr-h"><strong>indices</strong><span class="muted">int64[${res.nnz}] · 次轴索引</span></div>
      <div class="arr-b">[${res.indices.join(", ")}${res.truncated ? ", …" : ""}]</div>
    </div>
    <div class="arr">
      <div class="arr-h"><strong>kinds</strong><span class="muted">uint8[${res.nnz}] · 0数值 1显式零 2缺测</span></div>
      <div class="arr-b">[${kindText}${res.truncated ? ", …" : ""}]</div>
    </div>
    <div class="arr">
      <div class="arr-h"><strong>data</strong><span class="muted">float64[${res.nnz}] · 缺测为 NaN，零为 0.0</span></div>
      <div class="arr-b">[${data}${res.truncated ? ", …" : ""}]</div>
    </div>
    <div class="kv">
      <span>shape ${res.shape[0]}×${res.shape[1]}</span><span>nnz ${res.nnz}</span>
      <span>digest ${esc((res.digest || "").slice(0, 16))}</span>
    </div>`;
}

/* ------------------------------------------------------------- storage */
function renderStorage() {
  const vid = Number($("#sVersion").value || 0);
  const v = versionById(vid);
  for (const name of ["csr", "csc"]) {
    const card = $(`.layout-card[data-layout="${name}"]`);
    const lay = v?.layouts?.[name];
    card.className = `layout-card ${lay ? lay.state : ""}`;
    const stateLabel = card.querySelector(".state-label");
    const bar = card.querySelector(".progress span");
    const meta = card.querySelector(".lc-meta");
    if (!lay) {
      stateLabel.textContent = "未构建";
      bar.style.width = "0%";
      meta.textContent = "尚未转换";
      continue;
    }
    stateLabel.textContent = lay.state;
    bar.style.width = `${lay.state === "ready" ? 100 : (lay.progress?.percent ?? 0)}%`;
    const bits = [
      `shape ${lay.shape[0]}×${lay.shape[1]} · nnz ${lay.nnz}`,
      `indptr ${lay.indptr_len} · ${lay.arrays_bytes} B`,
    ];
    if (lay.progress && lay.state !== "ready") {
      bits.push(`进度 ${lay.progress.next_index}/${lay.progress.total}（${lay.progress.percent}%）· 批 ${lay.progress.chunk}`);
    }
    if (lay.stale_reason) bits.push(`失效：${lay.stale_reason}`);
    if (lay.pause_reason) bits.push(`暂停：${lay.pause_reason || "未知"}`);
    if (lay.error) bits.push(`错误：${lay.error}`);
    if (lay.verified_at) bits.push(`核对通过 ${new Date(lay.verified_at * 1000).toLocaleTimeString()}`);
    meta.textContent = bits.join("\n");
  }
  const latest = v?.layouts?.[ui.arrayLayout] || v?.layouts?.csr || null;
  renderChecks(latest?.verification);
}

function renderChecks(verification) {
  const root = $("#checksPanel");
  if (!verification) {
    root.innerHTML = `<div class="check fail"><span class="ic">!</span><span class="nm">尚未发布，无核对记录</span></div>`;
    return;
  }
  const names = {
    indptr_length: "indptr 长度 = 主维 + 1",
    indptr_zero_base: "indptr[0] = 0",
    indptr_final_nnz: "indptr[-1] = nnz",
    indptr_monotonic: "偏移单调不减",
    arrays_aligned: "数组长度一致",
    indices_in_range: "索引在范围内",
    kind_codes_valid: "类型码合法",
    kinds_values_consistent: "类型与数值一致",
    coordinates_unique: "坐标唯一",
    coordinates_match_logical: "坐标存在于逻辑三元组",
    values_match_logical: "逐坐标数值一致",
    coordinates_complete: "逻辑坐标无遗漏",
    counts_match: "条目数一致",
    canonical_order: "物理顺序规范",
  };
  root.innerHTML = Object.entries(verification.checks).map(([k, ok]) =>
    `<div class="check ${ok ? "pass" : "fail"}"><span class="ic">${ok ? "✓" : "✕"}</span>
      <span class="nm">${esc(names[k] || k)}</span></div>`).join("");
}

$$("#arrayLayout button").forEach(b => b.addEventListener("click", () => {
  ui.arrayLayout = b.dataset.layout;
  $$("#arrayLayout button").forEach(x => x.classList.toggle("active", x === b));
  loadArrays(ui.arrayLayout);
  renderStorage();
}));

$("#sVersion").addEventListener("change", () => {
  ui.storageVersion = Number($("#sVersion").value);
  renderStorage();
  loadArrays(ui.arrayLayout);
});

document.querySelectorAll(".layout-card").forEach(card => {
  card.addEventListener("click", async ev => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const layout = card.dataset.layout;
    const vid = $("#sVersion").value;
    if (!vid) return toast("请先导入数据版", "bad");
    const act = btn.dataset.act;
    const budget = Math.max(256, Number($("#budget").value || 4096));
    const pace = Math.max(0, Number($("#pace").value || 0));
    btn.disabled = true;
    try {
      let res;
      if (act === "start") {
        res = await api("POST", `/api/versions/${vid}/layouts/${layout}/start`,
          { budget_bytes: budget, pace_ms: pace });
        showMsg("#storageMsg",
          res.started
            ? `已启动 ${layout.toUpperCase()} 转换：每批 ${Math.max(1, Math.floor(budget / 32))} 个元素，内存预算 ${budget} B。` +
              (res.layout.progress?.next_index ? `从 ${res.layout.progress.next_index}/${res.layout.progress.total} 处继续。` : "")
            : `未启动：${res.reason}`, res.started ? "ok" : "warn");
      } else if (act === "pause") {
        res = await api("POST", `/api/versions/${vid}/layouts/${layout}/pause`, {});
        showMsg("#storageMsg", res.paused ? `已暂停，进度已保存，可稍后恢复。` : "该布局当前不在转换中。", res.paused ? "ok" : "warn");
      } else if (act === "resume") {
        res = await api("POST", `/api/versions/${vid}/layouts/${layout}/resume`, {});
        showMsg("#storageMsg", res.resumed ? "已从检查点恢复转换。" : "无需恢复。", "ok");
      } else if (act === "verify") {
        res = await api("POST", `/api/versions/${vid}/layouts/${layout}/verify`, {});
        const rep = res.verification;
        showMsg("#storageMsg",
          rep.ok ? `核对通过：${rep.coordinate_checks} 个坐标逐一比对，偏移单调、索引在范围内。`
                 : `核对失败：\n${rep.errors.join("\n")}`, rep.ok ? "ok" : "bad");
      } else if (act === "export") {
        res = await api("POST", `/api/versions/${vid}/layouts/${layout}/export`, {});
        const e = res.export;
        showMsg("#storageMsg", `已生成 ${e.name}（${e.bytes} 字节，sha256 ${e.sha256.slice(0, 16)}…），可在“交付下载”页取得。`, "ok");
      }
      await refresh();
    } catch (err) {
      showMsg("#storageMsg", err.message, "bad");
    } finally {
      btn.disabled = false;
      renderStorage();
      loadArrays(ui.arrayLayout);
    }
  });
});

/* ---------------------------------------------------------- correction */
$("#btnCorrect").addEventListener("click", async () => {
  const vid = $("#cVersion").value;
  const row = $("#cRow").value.trim();
  const col = $("#cCol").value.trim();
  const kind = $("#cKind").value;
  const raw = $("#cValue").value.trim();
  if (!vid || !row || !col) return showMsg("#correctMsg", "需要数据版、行标签与列标签", "warn");
  let value = null;
  if (kind === "value") {
    if (raw === "") return showMsg("#correctMsg", "数值类型需要填入数字", "warn");
    value = Number(raw);
    if (!Number.isFinite(value)) return showMsg("#correctMsg", "数值必须为有限数字", "warn");
  }
  try {
    const res = await api("POST", `/api/versions/${vid}/element`, { row, col, kind, value });
    showMsg("#correctMsg",
      `已更正 (${row}, ${col}) 为 ${kind === "value" ? value : kind}。` +
      (res.invalidated_layouts.length
        ? `\n布局 ${res.invalidated_layouts.join(", ")} 已失效，重新转换并核对后才能读取。`
        : "\n没有已发布布局需要失效。"), "ok");
    await refresh();
  } catch (err) { showMsg("#correctMsg", err.message, "bad"); }
});

$$("#compareLayout button").forEach(b => b.addEventListener("click", () => {
  ui.compareLayout = b.dataset.layout;
  $$("#compareLayout button").forEach(x => x.classList.toggle("active", x === b));
}));

$("#btnCompare").addEventListener("click", async () => {
  const vid = $("#dVersion").value;
  if (!vid) return showMsg("#compareMsg", "请选择数据版", "warn");
  try {
    const res = await api("GET", `/api/versions/${vid}/compare?layout=${ui.compareLayout}`);
    renderCompare(res);
    showMsg("#compareMsg", res.all_match
      ? "逻辑三元组与存储数组逐坐标一致。"
      : "存在差异，见下方计数与明细。", res.all_match ? "ok" : "bad");
  } catch (err) {
    showMsg("#compareMsg", err.message, "bad");
  }
});

function renderCompare(res) {
  const c = res.counter;
  const stat = (k, v, cls = "") => `<div class="stat ${cls}"><div class="k">${k}</div><div class="v">${v}</div></div>`;
  $("#compareSummary").innerHTML = [
    stat("逻辑坐标", res.logical_count),
    stat("存储坐标", res.stored_count),
    stat("重复坐标", c.duplicates, c.duplicates ? "bad" : "zero"),
    stat("缺失于布局", c.missing_in_layout, c.missing_in_layout ? "bad" : "zero"),
    stat("布局多余", c.extra_in_layout, c.extra_in_layout ? "bad" : "zero"),
    stat("类型不一致", c.kind_mismatches, c.kind_mismatches ? "bad" : "zero"),
    stat("数值不一致", c.value_mismatches, c.value_mismatches ? "bad" : "zero"),
    stat("结论", res.all_match ? "一致" : "不一致", res.all_match ? "good" : "bad"),
  ].join("");
  $("#logicalTable").innerHTML = `<thead><tr><th>行</th><th>列</th><th>状态</th><th style="text-align:right">数值</th><th>来源</th></tr></thead>
    <tbody>${res.logical.map(t => `<tr>
      <td>${esc(t.row)}</td><td>${esc(t.col)}</td><td>${badge(t.kind)}</td>
      <td class="num">${esc(fmtValue(t.kind, t.value))}</td>
      <td class="mono">${esc(t.source || "—")}${t.source_version != null ? ` v${t.source_version}` : ""}</td>
    </tr>`).join("") || `<tr><td class="empty" colspan="5">无逻辑元素</td></tr>`}</tbody>`;
  const rowIds = Object.fromEntries((ui.summary?.labels.row || []).map(l => [l.id, l.name]));
  const colIds = Object.fromEntries((ui.summary?.labels.col || []).map(l => [l.id, l.name]));
  $("#storedTable").innerHTML = `<thead><tr><th>行</th><th>列</th><th>状态</th><th style="text-align:right">数值</th></tr></thead>
    <tbody>${res.stored.map(s => `<tr>
      <td>${esc(rowIds[s.row_id] ?? "#" + s.row_id)}</td>
      <td>${esc(colIds[s.col_id] ?? "#" + s.col_id)}</td>
      <td>${badge(s.kind)}</td>
      <td class="num">${esc(fmtValue(s.kind, s.value))}</td>
    </tr>`).join("") || `<tr><td class="empty" colspan="4">无存储元素</td></tr>`}</tbody>`;
}

/* --------------------------------------------------------------- export */
function renderExports() {
  const root = $("#exportList");
  const exports = ui.summary?.exports || [];
  if (!exports.length) {
    root.innerHTML = `<div class="empty">还没有导出文件。先在“存储布局”页转换并发布一个布局，再导出。</div>`;
    return;
  }
  root.innerHTML = exports.map(e => `
    <div class="export-item">
      <div>
        <div class="nm">${esc(e.name)}</div>
        <div class="meta">${e.layout.toUpperCase()} · ${e.shape[0]}×${e.shape[1]} · nnz ${e.nnz} · ${e.bytes} B</div>
        <div class="meta">sha256 ${esc(e.sha256.slice(0, 32))}…</div>
      </div>
      <a class="primary" style="text-decoration:none" href="/files/${encodeURIComponent(e.name)}" download>下载</a>
    </div>`).join("");
}

function renderAudit() {
  const root = $("#auditList");
  const items = ui.summary?.audit || [];
  if (!items.length) { root.innerHTML = `<div class="empty">暂无操作</div>`; return; }
  root.innerHTML = items.map(a => {
    const detail = Object.entries(a.detail || {}).map(([k, v]) =>
      `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join(" ");
    const time = new Date(a.at * 1000).toLocaleTimeString();
    return `<div class="audit-item"><span class="t">${time}</span>
      <span class="a">${esc(a.action)}</span><span class="d">${esc(detail)}</span></div>`;
  }).join("");
}

/* ---------------------------------------------------------------- misc */
$("#btnReset").addEventListener("click", async () => {
  if (!confirm("清空整个仓库？所有数据版、布局与导出记录都会被删除。")) return;
  try {
    await api("POST", "/api/reset", {});
    $("#importMsg").hidden = true;
    toast("仓库已清空");
    await refresh();
  } catch (err) { toast(err.message, "bad"); }
});

$("#btnHelp").addEventListener("click", () => { $("#helpModal").hidden = false; });
$("#btnHelpClose").addEventListener("click", () => { $("#helpModal").hidden = true; });
$("#helpModal").addEventListener("click", ev => {
  if (ev.target.id === "helpModal") $("#helpModal").hidden = true;
});

function anyBuilding() {
  for (const v of ui.summary?.versions || []) {
    for (const l of Object.values(v.layouts || {})) {
      if (l.state === "building") return true;
    }
  }
  return false;
}

function schedulePoll() {
  const building = anyBuilding();
  if (building && !ui.pollTimer) {
    ui.pollTimer = setInterval(async () => {
      await refresh();
      if (!anyBuilding()) {
        clearInterval(ui.pollTimer);
        ui.pollTimer = null;
        loadArrays(ui.arrayLayout);
      }
    }, 450);
  } else if (!building && ui.pollTimer) {
    clearInterval(ui.pollTimer);
    ui.pollTimer = null;
  }
}

/* ---------------------------------------------------------------- boot */
refresh({ quiet: false }).then(() => {
  if ((ui.summary?.versions || []).length) {
    runQuery();
  }
});
$("#qVersion").addEventListener("change", () => { loadArrays(ui.queryLayout, "#arrayView", "#arrayMeta"); });
