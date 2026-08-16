/* =====================================================================
   داشبورد زنده مزرعه قزل‌آلا — مرحله ۱
   همه اعداد از API می‌آیند؛ هیچ عدد hard-code شده‌ای در این فایل نیست.
   ===================================================================== */
"use strict";

let DATA = null, TXNS = [], OFFERS = null, ASSUME = null, TAB = "overview";

/* ------------------------------------------------------------- helpers */
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const el = (t, c, h) => { const e = document.createElement(t);
  if (c) e.className = c; if (h !== undefined) e.innerHTML = h; return e; };

const nfFa = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 0 });
const nfFa1 = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 1 });
const nfFa2 = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 2 });

function n0(v) { return (v === null || v === undefined || isNaN(v)) ? "—" : nfFa.format(v); }
function n1(v) { return (v === null || v === undefined || isNaN(v)) ? "—" : nfFa1.format(v); }
function n2(v) { return (v === null || v === undefined || isNaN(v)) ? "—" : nfFa2.format(v); }
function pct(v, dg = 1) { return (v === null || v === undefined || isNaN(v)) ? "—"
  : nfFa1.format(v * 100) + "٪"; }
function money(v) {                       // تومان با مقیاس خوانا
  if (v === null || v === undefined || isNaN(v)) return "—";
  const a = Math.abs(v), s = v < 0 ? "−" : "";
  if (a >= 1e9) return s + nfFa2.format(a / 1e9) + " میلیارد";
  if (a >= 1e6) return s + nfFa1.format(a / 1e6) + " میلیون";
  return s + nfFa.format(a);
}
function jdate(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso + (iso.length <= 10 ? "T00:00:00" : ""))
      .toLocaleDateString("fa-IR-u-ca-persian", { year: "numeric", month: "short", day: "numeric" });
  } catch (e) { return iso; }
}
function gdate(iso) { return iso ? iso.slice(0, 10) : "—"; }
function today() { return new Date().toISOString().slice(0, 10); }

function toast(msg, err) {
  const t = $("#toast"); t.textContent = msg;
  t.className = "toast show" + (err ? " err" : "");
  clearTimeout(t._t); t._t = setTimeout(() => t.className = "toast", 3600);
}

async function api(path, method = "GET", body) {
  const r = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined
  });
  const j = await r.json();
  if (!r.ok) {
    const err = new Error(j.error || "خطای ناشناخته");
    err.code = j.code;            // مثلاً optimizer_unavailable
    err.solver = j.solver;        // جزئیات عیب‌یابی حل‌کننده
    throw err;
  }
  return j;
}

const basisBadge = b => {
  if (b === "actual") return '<span class="badge actual">واقعی</span>';
  if (b === "estimated_from_actual") return '<span class="badge estimated">تخمین از داده واقعی</span>';
  if (b === "estimated") return '<span class="badge estimated">تخمینی</span>';
  if (b === "forecast") return '<span class="badge forecast">پیش‌بینی</span>';
  return "";
};
const srcBadge = s => `<span class="badge ${s === "OBS" ? "obs" : s === "DER" ? "der" : "ma"}">${s}</span>`;

/* ============================================================ bootstrap */
async function load() {
  try {
    DATA = await api("/api/bootstrap");
    render();
  } catch (e) { toast("خطا در بارگذاری: " + e.message, true); }
}

function render() {
  $("#asof").textContent = "وضعیت در تاریخ " + jdate(DATA.meta.as_of) +
    "  ·  " + gdate(DATA.meta.as_of);
  renderKpis(); renderWarnings(); renderUnassignedSales();
  renderReconciliation(); renderPondAllocation();
  renderPonds("#pondGridMini"); renderPonds("#pondGrid");
  renderLegend("#legend"); renderLegend("#legend2");
  renderCapacity(); renderFx(); renderTimeline();
  renderMilestones(); renderCash();
  renderCohorts(); renderFeed(); renderChecks(); renderRef();
  $("#pondSub").innerHTML =
    `${n0(DATA.summary.operational_ponds_used)} از ${n0(DATA.summary.operational_ponds_total)} استخر عملیاتی اشغال است` +
    ` · نیاز محاسبه‌شده هم‌اکنون: <b>${n2(DATA.summary.ponds_required_now)}</b> استخر`;
}

/* ------------------------- فروش‌های تاریخی بدون cohort (اصلاح ۱۰) */
function renderUnassignedSales() {
  const list = (DATA.unassigned_sales || []);
  const box = $("#unassignedPanel");
  if (!list.length) { box.style.display = "none"; return; }
  box.style.display = "";
  $("#unassignedTable").innerHTML = `<thead><tr><th>تاریخ</th><th class="num">تعداد</th>
    <th class="num">وزن</th><th class="num">قیمت محقق‌شده</th><th class="num">مبلغ کل</th>
    <th>پیشنهاد سیستم</th><th></th></tr></thead><tbody>` +
    list.map(x => `<tr>
      <td class="small">${jdate(x.date)}</td>
      <td class="num">${n0(x.quantity)}</td>
      <td class="num">${x.weight_g ? n1(x.weight_g) + " گرم"
        : '<span class="badge estimated">نامشخص</span>'}</td>
      <td class="num">${n0(x.unit_price)} تومان</td>
      <td class="num">${money(x.amount)}</td>
      <td class="tiny" id="sug_${x.txn_id}"><span class="muted">در حال بررسی…</span></td>
      <td><button class="btn small primary" data-assign="${x.txn_id}">تخصیص cohort</button></td>
      </tr>`).join("") +
    `<tr><td><b>جمع</b></td><td class="num"><b>${n0(list.reduce((a, b) => a + b.quantity, 0))}</b></td>
      <td></td><td></td><td class="num"><b>${money(list.reduce((a, b) => a + b.amount, 0))}</b></td>
      <td colspan="2"></td></tr></tbody>`;
  $$("[data-assign]").forEach(b =>
    b.onclick = () => openAssignDrawer(parseInt(b.dataset.assign)));
  loadInlineSuggestions(list);
}

function splitLine(S, cohortId, qty) {
  return `<div class="row" data-spline style="margin-bottom:6px">
    <select class="sp_cohort">${(S.cohort_ids || []).map(c =>
      `<option ${c === cohortId ? "selected" : ""}>${c}</option>`).join("")}</select>
    <input class="sp_qty" type="number" step="1" value="${Math.round(qty || 0)}"
      style="max-width:150px">
    <button class="btn small ghost sp_del">حذف</button></div>`;
}

const CONF_BADGE = {
  high: '<span class="badge ok">تطابق قوی</span>',
  medium: '<span class="badge estimated">تطابق مشروط</span>',
  low: '<span class="badge estimated">تطابق ضعیف</span>',
  none: '<span class="badge bad">بدون تطابق</span>',
};

async function loadInlineSuggestions(list) {
  for (const x of list) {
    const cell = $("#sug_" + x.txn_id);
    if (!cell) continue;
    try {
      const s = await api(`/api/sales/${x.txn_id}/suggest`);
      cell.innerHTML = (CONF_BADGE[s.confidence] || "") +
        (s.best ? ` <b>${s.best}</b>` : "") +
        `<div class="tiny muted">${s.message_fa.slice(0, 110)}</div>`;
    } catch (e) { cell.innerHTML = '<span class="muted tiny">—</span>'; }
  }
}

/* ---------------- تخصیص cohort بر مبنای وزن فروخته‌شده ---------------- */
async function openAssignDrawer(txnId) {
  let S;
  try { S = await api(`/api/sales/${txnId}/suggest`); }
  catch (e) { toast("خطا: " + e.message, true); return; }

  const cands = S.candidates || [];
  const hint = S.missing_cohort_hint;
  $("#drawer").innerHTML = `
    <div class="row"><h3 style="flex:1">تخصیص cohort — فروش #${txnId}</h3>
      <button class="btn small" onclick="closeDrawer()">بستن</button></div>

    <table><tbody>
      <tr><td>تاریخ فروش</td><td class="num">${jdate(S.sale_date)}</td></tr>
      <tr><td>تعداد</td><td class="num">${n0(S.quantity)} قطعه</td></tr>
      <tr><td>وزن فروخته‌شده</td><td class="num">${S.weight_g
        ? n1(S.weight_g) + " گرم" : '<span class="badge estimated">ثبت نشده</span>'}</td></tr>
      <tr><td>cohort فعلی</td><td class="num">${S.current_cohort_id
        || '<span class="badge estimated">تخصیص نیافته</span>'}</td></tr>
      ${S.implied_purchase_date ? `<tr><td>تاریخ خرید ضمنی این وزن</td>
        <td class="num">${jdate(S.implied_purchase_date)}
        <div class="tiny muted">سن لازم ${n0(S.implied_age_days)} روز</div></td></tr>` : ""}
    </tbody></table>

    <div class="note ${S.confidence === "none" ? "bad"
      : S.confidence === "high" ? "ok" : ""}" style="margin:10px 0">
      ${CONF_BADGE[S.confidence] || ""} ${S.message_fa}</div>

    ${S.needs_weight ? `<div class="form-grid">
      <div class="field"><label>وزن واقعی فروش (گرم)</label>
        <input id="as_weight_only" type="number" step="any"></div>
      <div class="field" style="align-self:end">
        <button class="btn primary" id="as_saveweight">ثبت وزن و بررسی دوباره</button></div>
    </div>` : ""}

    ${cands.length ? `<h3 style="margin-top:16px">گزینه‌ها به ترتیب احتمال</h3>
      <table><thead><tr><th></th><th>Cohort</th><th class="num">امتیاز</th>
        <th class="num">وزن مورد انتظار</th><th class="num">موجودی آن تاریخ</th>
        <th>دلیل</th></tr></thead><tbody>` +
      cands.map(c => `<tr class="${c.cohort_id === S.best ? "rowcur" : ""}">
        <td><input type="radio" name="cand" value="${c.cohort_id}"
          ${c.cohort_id === (S.current_cohort_id || S.best) ? "checked" : ""}></td>
        <td class="small">${c.cohort_id}
          <div class="tiny muted">خرید ${jdate(c.purchase_date)}</div></td>
        <td class="num">${(c.score * 100).toFixed(0)}%</td>
        <td class="num">${c.expected_weight_g === null || c.expected_weight_g === undefined
          ? "—" : n2(c.expected_weight_g) + " g"}</td>
        <td class="num">${n0(c.available_fish || 0)}</td>
        <td class="tiny">
          ${(c.reasons || []).map(r => `<div>✓ ${r}</div>`).join("")}
          ${(c.blockers || []).map(r =>
            `<div style="color:var(--bad)">✕ ${r}</div>`).join("")}</td>
      </tr>`).join("") + "</tbody></table>" : ""}

    <div class="form-grid" style="margin-top:14px">
      <div class="field"><label>یا انتخاب دستی از همه cohortها</label>
        <select id="as_manual"><option value="">— استفاده از گزینه بالا —</option>
          ${(S.cohort_ids || []).map(c =>
            `<option ${c === S.current_cohort_id ? "selected" : ""}>${c}</option>`).join("")}
        </select></div>
      <div class="field"><label>اصلاح وزن (اختیاری)</label>
        <input id="as_weight" type="number" step="any"
          value="${S.weight_g || ""}" placeholder="گرم"></div>
      <div class="field" style="grid-column:1/-1"><label>دلیل / یادداشت</label>
        <input id="as_reason" placeholder="مثلاً: طبق دفتر فروش، از cohort بهمن بوده"></div>
    </div>

    <h3 style="margin-top:16px">تقسیم بین چند cohort</h3>
    <div class="sub">یک فروش می‌تواند از ترکیب چند cohort باشد. مجموع تخصیص‌ها
      باید دقیقاً برابر ${n0(S.quantity)} قطعه باشد.</div>
    <div id="sp_lines" style="margin-top:8px">
      ${(S.suggested_split || []).map(r => splitLine(S, r.cohort_id, r.quantity)).join("")
        || splitLine(S, (S.cohort_ids || [])[0], S.quantity)}</div>
    <div class="row" style="margin-top:6px">
      <button class="btn small" id="sp_add">افزودن cohort</button>
      <span class="muted small" id="sp_total"></span>
      ${S.split_confidence ? `<span class="badge ${
        S.split_confidence === "high" ? "ok" : S.split_confidence === "none"
        ? "bad" : "estimated"}">اطمینان تقسیم: ${
        { high: "بالا", medium: "متوسط", low: "پایین", none: "نامشخص"
        }[S.split_confidence]}</span>` : ""}
    </div>
    <div class="row" style="margin-top:8px">
      <button class="btn primary" id="sp_save">تأیید تقسیم چند-cohort</button>
    </div>

    <div class="row" style="margin-top:14px;flex-wrap:wrap">
      <button class="btn primary" id="as_apply">ثبت تخصیص تک-cohort</button>
      ${S.current_cohort_id
        ? '<button class="btn" id="as_clear">حذف تخصیص</button>' : ""}
      ${hint ? `<button class="btn" id="as_implied">ساخت cohort تاریخی
        ${jdate(hint.purchase_date)}</button>` : ""}
      <button class="btn ghost" data-hist="${txnId}" id="as_hist">تاریخچه اصلاحات</button>
    </div>

    ${hint ? `<div class="note" style="margin-top:10px">
      اگر این فروش از یک خرید قدیمی‌تر بوده که هنوز ثبت نشده، می‌توانید آن را
      به‌صورت <b>Estimated / Inferred</b> بسازید: خرید حدود
      <b>${jdate(hint.purchase_date)}</b> با تقریباً
      <b>${n0(hint.suggested_egg_count)}</b> تخم.
      این cohort با برچسب «استنتاجی» ثبت می‌شود و بهای تخم آن صفر می‌ماند تا
      دفتر نقدی با عدد ساختگی آلوده نشود.</div>` : ""}
    <div id="as_result"></div>`;

  const recountSplit = () => {
    const t = [...document.querySelectorAll(".sp_qty")]
      .reduce((a, e) => a + (parseFloat(e.value) || 0), 0);
    const el = $("#sp_total");
    const ok = Math.abs(t - S.quantity) < 1;
    el.innerHTML = `مجموع: <b style="color:${ok ? "var(--ok)" : "var(--bad)"}">
      ${n0(t)}</b> از ${n0(S.quantity)}`;
  };
  const wireSplit = () => {
    $$(".sp_del").forEach(b => b.onclick = () => {
      b.closest("[data-spline]").remove(); recountSplit();
    });
    $$(".sp_qty").forEach(e => e.oninput = recountSplit);
    recountSplit();
  };
  $("#sp_add").onclick = () => {
    $("#sp_lines").insertAdjacentHTML("beforeend",
      splitLine(S, (S.cohort_ids || [])[0], 0));
    wireSplit();
  };
  $("#sp_save").onclick = async () => {
    const allocations = [...document.querySelectorAll("[data-spline]")].map(el => ({
      cohort_id: el.querySelector(".sp_cohort").value,
      quantity: parseFloat(el.querySelector(".sp_qty").value) || 0,
    })).filter(a => a.quantity > 0);
    try {
      const r = await api(`/api/sales/${txnId}/split`, "POST",
        { allocations, reason: $("#as_reason").value });
      toast(`فروش بین ${r.allocations.length} cohort تقسیم شد`);
      closeDrawer(); await loadTxns(); await load();
      if (PLAN) loadPlan();
    } catch (e) { toast("خطا: " + e.message, true); }
  };
  wireSplit();

  const pick = () => $("#as_manual").value ||
    (document.querySelector('input[name="cand"]:checked') || {}).value || "";

  const wv = () => {
    const v = $("#as_weight") ? $("#as_weight").value : "";
    return v === "" ? null : parseFloat(v);
  };

  if ($("#as_saveweight")) $("#as_saveweight").onclick = async () => {
    const w = parseFloat($("#as_weight_only").value);
    if (!w) { toast("وزن را وارد کنید", true); return; }
    await api(`/api/sales/${txnId}/assign`, "POST",
      { weight_g: w, reason: "ثبت وزن واقعی فروش" });
    toast("وزن ثبت شد");
    await loadTxns(); await load();
    const fresh = DATA.unassigned_sales.find(x => x.date);
    openAssignDrawer(fresh ? txnId : txnId);
  };

  $("#as_apply").onclick = async () => {
    const cid = pick();
    if (!cid) { toast("یک cohort انتخاب کنید", true); return; }
    try {
      await api(`/api/sales/${txnId}/assign`, "POST", {
        cohort_id: cid, weight_g: wv(),
        reason: $("#as_reason").value,
        method: cid === S.best ? "auto_accepted" : "manual" });
      toast(`فروش به ${cid} تخصیص یافت؛ موجودی و برنامه بازمحاسبه شدند`);
      closeDrawer(); await loadTxns(); await load();
      if (PLAN) loadPlan();
    } catch (e) { toast("خطا: " + e.message, true); }
  };

  if ($("#as_clear")) $("#as_clear").onclick = async () => {
    await api(`/api/sales/${txnId}/assign`, "POST",
      { cohort_id: null, reason: $("#as_reason").value || "حذف تخصیص توسط کاربر",
        method: "cleared" });
    toast("تخصیص حذف شد");
    closeDrawer(); await loadTxns(); await load();
  };

  if ($("#as_implied")) $("#as_implied").onclick = async () => {
    if (!confirm(`یک cohort استنتاجی با تاریخ خرید ${hint.purchase_date} و حدود ` +
                 `${hint.suggested_egg_count.toLocaleString()} تخم ساخته شود؟`)) return;
    try {
      const r = await api(`/api/sales/${txnId}/implied-cohort`, "POST",
        { reason: $("#as_reason").value });
      toast(`cohort ${r.cohort_id} ساخته و فروش به آن تخصیص یافت`);
      closeDrawer(); await loadTxns(); await load();
      if (PLAN) loadPlan();
    } catch (e) { toast("خطا: " + e.message, true); }
  };

  $("#as_hist").onclick = () => openTxnHistory(txnId);
  $("#drawer").classList.add("open"); $("#overlay").classList.add("open");
}

/* ------------------------- وضعیت reconciliation موجودی (۷) */
async function renderReconciliation() {
  let r;
  try { r = await api("/api/sales/reconciliation"); } catch (e) { return; }
  const box = $("#reconPanel");
  if (r.reconciled) { box.style.display = "none"; return; }
  box.style.display = "";
  $("#reconBody").innerHTML = `
    <div class="note bad"><b>Inventory Reconciliation Required</b> —
      ${n0(r.unallocated_fish)} از ${n0(r.total_sold_fish)} قطعه فروش هنوز به
      cohort تخصیص نیافته است (${pct(r.coverage_ratio, 0)} پوشش).
      تا رفع این مسئله وضعیت برنامه <b>PROVISIONAL</b> است و موجودی زنده
      بیش از واقعیت تخمین زده می‌شود.</div>
    <table style="margin-top:8px"><thead><tr><th>تاریخ</th><th class="num">تعداد</th>
      <th class="num">وزن</th><th class="num">تخصیص‌یافته</th>
      <th class="num">باقیمانده</th></tr></thead><tbody>` +
    r.pending.map(x => `<tr>
      <td class="small">${jdate(x.date)}</td>
      <td class="num">${n0(x.quantity)}</td>
      <td class="num">${x.weight_g ? n1(x.weight_g) + " گرم"
        : '<span class="badge estimated">نامشخص</span>'}</td>
      <td class="num">${n0(x.allocated)}</td>
      <td class="num"><b>${n0(x.unallocated)}</b></td></tr>`).join("") + "</tbody></table>";
}

/* ------------------------- تخصیص خودکار استخر (۱) */
async function renderPondAllocation() {
  let r;
  try { r = await api("/api/ponds/allocation"); } catch (e) { return; }
  const box = $("#pondAllocPanel");
  if (!r.suggestions.length) { box.style.display = "none"; return; }
  box.style.display = "";
  $("#pondAllocBody").innerHTML =
    (r.warnings.length ? r.warnings.map(w =>
      `<div class="note bad">${w}</div>`).join("") : "") +
    `<table style="margin-top:8px"><thead><tr><th>Cohort</th><th class="num">ماهی</th>
      <th class="num">وزن</th><th class="num">استخر لازم</th>
      <th>تخصیص پیشنهادی</th><th></th></tr></thead><tbody>` +
    r.suggestions.map(x => `<tr>
      <td class="small">${x.cohort_id}
        <span class="badge estimated">Suggested</span></td>
      <td class="num">${n0(x.fish)}</td>
      <td class="num">${n2(x.mean_weight_g)} g</td>
      <td class="num">${n0(x.ponds_needed)}${x.shortfall_ponds > 0
        ? ` <span class="badge bad">کمبود ${n0(x.shortfall_ponds)}</span>` : ""}</td>
      <td class="tiny">${x.allocations.map(a =>
        `${a.pond_id} (${n0(a.quantity)})`).join(" · ") || "—"}</td>
      <td class="nowrap">
        <button class="btn small primary" data-pacc="${x.cohort_id}">تأیید</button>
        <button class="btn small ghost" data-pedit="${x.cohort_id}">ویرایش</button>
      </td></tr>`).join("") + "</tbody></table>" +
    `<div class="tiny muted" style="margin-top:8px">استخرهای رزرو
      (${r.reserve_ponds.join("، ")}) عمداً آزاد نگه داشته شده‌اند.
      استخر آزاد باقیمانده: ${r.free_ponds_remaining.length}</div>`;
  $$("[data-pacc]").forEach(b => b.onclick = async () => {
    try {
      const res = await api("/api/ponds/allocation/accept", "POST",
        { cohort_id: b.dataset.pacc, reason: "تأیید تخصیص پیشنهادی از داشبورد" });
      toast(`${res.created.length} استخر برای ${res.cohort_id} به Actual تبدیل شد`);
      await loadTxns(); await load();
    } catch (e) { toast("خطا: " + e.message, true); }
  });
  $$("[data-pedit]").forEach(b =>
    b.onclick = () => openPondAllocEditor(b.dataset.pedit, r));
}

function openPondAllocEditor(cohortId, R) {
  const row = R.suggestions.find(x => x.cohort_id === cohortId);
  if (!row) return;
  const allPonds = [...new Set([...row.allocations.map(a => a.pond_id),
                                ...R.free_ponds_remaining, ...R.reserve_ponds])];
  const line = (pid, qty) => `<div class="row" data-line style="margin-bottom:6px">
      <select class="pa_pond">${allPonds.map(p =>
        `<option ${p === pid ? "selected" : ""}>${p}</option>`).join("")}</select>
      <input class="pa_qty" type="number" step="1" value="${Math.round(qty)}"
        style="max-width:140px">
      <button class="btn small ghost pa_del">حذف</button></div>`;
  $("#drawer").innerHTML = `
    <div class="row"><h3 style="flex:1">ویرایش تخصیص استخر — ${cohortId}</h3>
      <button class="btn small" onclick="closeDrawer()">بستن</button></div>
    <div class="note">مجموع تخصیص باید با ${n0(row.fish)} قطعه ماهی تخصیص‌نیافته
      برابر یا کمتر باشد. ظرفیت هر استخر در وزن ${n2(row.mean_weight_g)} گرم حدود
      ${n0(row.capacity_per_pond)} قطعه است.</div>
    <div id="pa_lines" style="margin-top:10px">
      ${row.allocations.map(a => line(a.pond_id, a.quantity)).join("")}</div>
    <div class="row" style="margin-top:6px">
      <button class="btn small" id="pa_add">افزودن استخر</button>
      <span class="muted small" id="pa_total"></span></div>
    <div class="field" style="margin-top:10px"><label>یادداشت</label>
      <input id="pa_note" placeholder="دلیل تقسیم یا تغییر"></div>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" id="pa_save">ثبت به‌عنوان Actual</button></div>`;
  const recount = () => {
    const t = [...document.querySelectorAll(".pa_qty")]
      .reduce((a, e) => a + (parseFloat(e.value) || 0), 0);
    $("#pa_total").textContent = `مجموع: ${n0(t)} از ${n0(row.fish)}`;
  };
  const wire = () => {
    $$(".pa_del").forEach(b => b.onclick = () => {
      b.closest("[data-line]").remove(); recount();
    });
    $$(".pa_qty").forEach(e => e.oninput = recount);
    recount();
  };
  $("#pa_add").onclick = () => {
    $("#pa_lines").insertAdjacentHTML("beforeend",
      line(R.free_ponds_remaining[0] || allPonds[0], 0));
    wire();
  };
  $("#pa_save").onclick = async () => {
    const allocations = [...document.querySelectorAll("[data-line]")].map(el => ({
      pond_id: el.querySelector(".pa_pond").value,
      quantity: parseFloat(el.querySelector(".pa_qty").value) || 0,
    })).filter(a => a.quantity > 0);
    try {
      await api("/api/ponds/allocation/accept", "POST",
        { cohort_id: cohortId, allocations, reason: $("#pa_note").value });
      toast("تخصیص ثبت شد و به Actual تبدیل شد");
      closeDrawer(); await loadTxns(); await load();
    } catch (e) { toast("خطا: " + e.message, true); }
  };
  wire();
  $("#drawer").classList.add("open"); $("#overlay").classList.add("open");
}

/* ================================================================= KPI */
function renderKpis() {
  const s = DATA.summary, L = DATA.ledger, p = DATA.peak_pressure;
  const feedDays = s.feed_days_remaining;
  const capTight = p.peak_ponds_required > p.operational_ponds;
  const cards = [
    { l: "ماهی زنده", v: n0(s.live_fish), u: "قطعه",
      f: `از ${n0(s.eggs_purchased_total)} تخم خریداری‌شده`, k: "" },
    { l: "کوهورت فعال", v: n0(s.active_cohorts), u: "",
      f: `${n0(s.fish_sold_total)} قطعه فروخته‌شده` },
    { l: "استخر عملیاتی", v: `${n0(s.operational_ponds_used)}/${n0(s.operational_ponds_total)}`,
      u: "", f: `رزرو: ${n0(s.reserve_ponds_used)}/${n0(s.reserve_ponds_total)}` },
    { l: "نیاز ظرفیت اکنون", v: n2(s.ponds_required_now), u: "استخر",
      f: "بر پایه ظرفیت تجربی وزن فعلی",
      k: s.ponds_required_now > s.operational_ponds_total ? "alert" : "" },
    { l: "زیست‌توده کل", v: n0(s.total_biomass_kg), u: "kg", f: "تخمینی" },
    { l: "موجودی خوراک", v: n0(s.feed_inventory_kg), u: "kg",
      f: feedDays === null ? "مصرف روزانه صفر"
        : `${s.feed_critical_type || ""} — ${n0(feedDays)} روز باقی مانده`,
      k: (feedDays !== null && feedDays < 14) ? "warn" : "" },
    { l: "ارزش موجودی زنده", v: money(s.stock_value), u: "تومان",
      f: "به قیمت روز همان وزن" },
    { l: "اوج نیاز نقدی", v: money(L.peak_funding_requirement), u: "تومان",
      f: `سرمایه در گردش: ${money(L.peak_working_capital)}`,
      k: L.wc_breach ? "warn" : "" },
    { l: "اوج فشار ظرفیت", v: n1(p.peak_ponds_required), u: "استخر",
      f: p.peak_date ? `در ${jdate(p.peak_date)}` : "—", k: capTight ? "alert" : "good" },
    { l: "وضعیت ریسک", v: DATA.validation.overall === "fail" ? "بحرانی"
        : DATA.validation.overall === "warn" ? "نیازمند توجه" : "سالم",
      u: "", f: `${n0(DATA.validation.failed)} خطا · ${n0(DATA.validation.warnings)} هشدار`,
      k: DATA.validation.overall === "fail" ? "alert"
        : DATA.validation.overall === "warn" ? "warn" : "good" },
  ];
  $("#kpis").innerHTML = cards.map(c => `
    <div class="kpi ${c.k || ""}">
      <div class="label">${c.l}</div>
      <div class="value">${c.v}<span class="unit">${c.u || ""}</span></div>
      <div class="foot">${c.f || ""}</div>
    </div>`).join("");
}

function renderWarnings() {
  const w = DATA.summary.warnings || [];
  $("#warnPanel").style.display = w.length ? "" : "none";
  $("#warnList").innerHTML = w.map(x =>
    `<div class="check warn"><span class="ic">!</span><span>${x}</span></div>`).join("");
}

/* ======================================================= pond schematic */
const STATUS_TXT = { empty: "خالی", occupied: "پر", over: "بیش از ظرفیت", reserve: "رزرو",
                     planned: "رزرو آینده (پیش‌بینی)" };
const STATUS_GLYPH = { empty: "○", occupied: "●", over: "▲", reserve: "◇", planned: "◐" };

function renderPonds(sel) {
  const box = $(sel); if (!box) return;
  box.innerHTML = DATA.ponds.map(p => {
    const est = p.basis === "estimated";
    const cls = ["pond", p.status, est ? "est" : ""].join(" ");
    const util = p.capacity ? Math.min(1.4, p.utilisation) : 0;
    const cohorts = p.occupants.map(o => o.cohort_id).join("، ") || "—";
    const nm = p.next_milestone;
    return `<div class="${cls}" data-pond="${p.pond_id}">
      ${p.count > 0 ? `<div class="fill" style="--lvl:${Math.min(112, util * 100)}%"></div>` : ""}
      <div class="pid">
        <span class="glyph">${STATUS_GLYPH[p.status]}</span> ${p.label}
        ${p.role === "reserve" ? '<span class="badge neutral">رزرو</span>' : ""}
        ${est ? '<span class="badge estimated">تخمینی</span>' : ""}
      </div>
      <div class="cohort">${cohorts}</div>
      ${p.count > 0 ? `
        <div class="nums">
          <span><b>${n0(p.count)}</b> قطعه</span>
          <span><b>${n1(p.avg_weight_g)}</b> گرم</span>
          <span><b>${n0(p.biomass_kg)}</b> kg</span>
        </div>
        <div class="bar"><i style="width:${Math.min(100, util * 100)}%"></i></div>
        <div class="meta">${p.capacity_applies
          ? `${pct(p.utilisation)} از ظرفیت ${n0(p.capacity)}`
          : "بدون محدودیت ظرفیت (زیر ۱ گرم)"}${
          nm ? ` · تا ${n1(nm.weight_g)}g: ${n0(nm.days)} روز` : ""}</div>`
      : `<div class="meta">${STATUS_TXT[p.status]} · ${n0(p.volume_m3)} m³</div>`}
    </div>`;
  }).join("");
  $$(".pond", box).forEach(d => d.onclick = () => openPond(d.dataset.pond));
}

function renderLegend(sel) {
  const b = $(sel); if (!b) return;
  b.innerHTML = `
    <span><i style="border-color:rgba(20,148,99,.35);background:#fff"></i> ● پر (واقعی)</span>
    <span><i style="border-color:rgba(111,92,174,.35);background:#fcfbff"></i> تخصیص تخمینی</span>
    <span><i style="border-color:rgba(26,40,48,.15);background:#fbfcfd"></i> ○ خالی</span>
    <span><i style="border-color:rgba(26,40,48,.15);background:#fafbfd"></i> ◇ رزرو</span>
    <span><i style="border-color:rgba(185,80,79,.35);background:#fdeceb"></i> ▲ بیش از ظرفیت</span>
    <span><i style="border-color:rgba(60,125,144,.35);background:#eef6fb"></i> ◐ رزرو آینده (پیش‌بینی)</span>`;
}

/* ═══════════════════════════════════════ بهینه‌سازی چیدمان استخر ═══ */
function addDays(iso, n) {
  const dt = new Date((iso || today()) + "T00:00:00");
  dt.setDate(dt.getDate() + Number(n));
  return dt.toISOString().slice(0, 10);
}

function renderPondsForecast(view) {
  const box = $("#pondGrid"); if (!box) return;
  box.innerHTML = view.ponds.map(p => {
    const cls = ["pond", p.status].join(" ");
    const util = p.capacity ? Math.min(1.4, p.utilisation) : 0;
    const cohorts = p.occupants.map(o => o.cohort_id).join("، ") || "—";
    return `<div class="${cls}">
      ${p.count > 0 ? `<div class="fill" style="--lvl:${Math.min(112, util * 100)}%"></div>` : ""}
      <div class="pid"><span class="glyph">${STATUS_GLYPH[p.status] || "○"}</span> ${p.label}
        ${p.mixed ? '<span class="badge estimated">Mixed Cohort/Weight</span>' : ""}
        <span class="badge forecast">Forecast</span></div>
      <div class="cohort">${cohorts}</div>
      ${p.count > 0 ? `
        <div class="nums">
          <span><b>${n0(p.count)}</b> قطعه</span>
          <span><b>${n1(p.avg_weight_g)}</b> گرم</span>
          <span><b>${n0(p.biomass_kg)}</b> kg</span>
        </div>
        <div class="bar"><i style="width:${Math.min(100, util * 100)}%"></i></div>
        <div class="meta">${pct(p.utilisation)} از ظرفیت (پیش‌بینی)</div>`
      : `<div class="meta">${STATUS_TXT[p.status] || p.status}</div>`}
    </div>`;
  }).join("");
}

async function onPondDateChange() {
  const sel = $("#pondDateSel").value;
  const custom = $("#pondDateCustom");
  custom.style.display = sel === "custom" ? "" : "none";
  const banner = $("#pondForecastBanner"), note = $("#pondPlannedNote");

  if (sel === "0") {
    banner.classList.add("hidden"); note.classList.add("hidden");
    renderPonds("#pondGrid");
    return;
  }
  const targetDate = sel === "custom" ? custom.value : addDays(DATA.meta.as_of, sel);
  if (!targetDate) return;
  try {
    const view = await api(`/api/ponds/forecast?date=${targetDate}`);
    banner.classList.remove("hidden"); note.classList.remove("hidden");
    banner.innerHTML = `<b>Forecast</b> — پیش‌بینی وضعیت استخرها در ${jdate(targetDate)}
      (${gdate(targetDate)})، نه وضعیت واقعی امروز.
      ${view.planned_transfers.length ? `<div class="tiny" style="margin-top:4px">` +
        view.planned_transfers.map(pt => `${pt.cohort_id}: ${pt.detail}`).join(" · ") +
        `</div>` : ""}`;
    renderPondsForecast(view);
  } catch (e) { toast("خطا در forecast: " + e.message, true); }
}

let PONDOPT_LAST_PREVIEW = null;

async function runPondOptimize() {
  const btn = $("#btnPondOptimize"); btn.disabled = true;
  btn.textContent = "در حال محاسبه…";
  try {
    const r = await api("/api/ponds/optimize/preview");
    PONDOPT_LAST_PREVIEW = r;
    showPondOptPreview(r);
  } catch (e) { toast("خطا: " + e.message, true); }
  finally { btn.disabled = false; btn.textContent = "بهینه‌سازی چیدمان استخر"; }
}

function showPondOptPreview(r) {
  const util = v => pct(v || 0);
  $("#drawer").innerHTML = `
    <button class="btn small ghost" onclick="closeDrawer()" style="float:left">بستن</button>
    <h3>پیش‌نمایش بهینه‌سازی چیدمان استخر</h3>
    <div class="tiny muted" style="margin-bottom:10px">این فقط پیش‌نمایش است — تا «اعمال» را
      نزنید، هیچ داده واقعی تغییر نمی‌کند. افق آینده‌نگری: ${n0(r.horizon_days)} روز.</div>
    ${!r.feasible ? `<div class="note bad">${r.reason_if_infeasible}</div>` : ""}
    <table style="margin-bottom:10px"><tbody>
      <tr><td>تعداد جابه‌جایی لازم</td><td class="num"><b>${n0(r.transfers_count)}</b></td></tr>
      <tr><td>استخر آزادشده</td><td class="num">${n0(r.ponds_freed)}</td></tr>
      <tr><td>حداکثر اشغال — قبل / بعد</td>
        <td class="num">${util(r.max_utilisation_before)} → ${util(r.max_utilisation_after)}</td></tr>
      <tr><td>تعارض ظرفیت آینده رفع‌شده</td>
        <td class="num">${n0(r.future_capacity_conflicts_avoided)}</td></tr>
      <tr><td>cohortهای تحت‌تأثیر</td><td class="num">${r.cohorts_affected.length}</td></tr>
    </tbody></table>
    ${r.moves.length ? `
      <h4 style="margin:10px 0 4px">جابه‌جایی‌های پیشنهادی</h4>
      <div class="scroll" style="max-height:260px"><table>
        <thead><tr><th>Cohort</th><th>از</th><th>به</th><th class="num">تعداد</th><th>دلیل</th></tr></thead>
        <tbody>${r.moves.map(m => `<tr>
          <td>${m.cohort_id}</td><td>${m.from_pond || "—"}</td><td>${m.to_pond}</td>
          <td class="num">${n0(m.quantity)}</td><td class="tiny muted">${m.reason}</td></tr>`).join("")}
        </tbody></table></div>`
      : '<div class="note info">تغییری لازم نیست — چیدمان فعلی همین‌طور بهینه است.</div>'}
    <div class="row" style="margin-top:14px">
      <button class="btn primary" id="btnPondOptApply" ${r.moves.length ? "" : "disabled"}>
        اعمال (Apply)</button>
      <button class="btn ghost" onclick="closeDrawer()">انصراف</button>
    </div>`;
  $("#drawer").classList.add("open"); $("#overlay").classList.add("open");
  $("#btnPondOptApply").onclick = applyPondOptimize;
}

async function applyPondOptimize() {
  if (!PONDOPT_LAST_PREVIEW || !PONDOPT_LAST_PREVIEW.moves.length) return;
  const btn = $("#btnPondOptApply"); btn.disabled = true; btn.textContent = "در حال ثبت…";
  try {
    const r = await api("/api/ponds/optimize/apply", "POST", {
      moves: PONDOPT_LAST_PREVIEW.moves, reason: "تأیید بهینه‌سازی چیدمان توسط کاربر" });
    toast(`${n0(r.applied)} جابه‌جایی ثبت شد`);
    closeDrawer();
    PONDOPT_LAST_PREVIEW = null;
    await load();
  } catch (e) { toast("خطا در اعمال: " + e.message, true); btn.disabled = false; }
}

async function restorePondAllocation() {
  if (!confirm("چیدمان استخر به حالت قبل از آخرین بهینه‌سازی برگردد؟ این خودش هم ثبت می‌شود.")) return;
  const btn = $("#btnPondRestore"); btn.disabled = true; btn.textContent = "در حال بازگردانی…";
  try {
    const r = await api("/api/ponds/optimize/restore", "POST", {});
    if (r.ok) { toast("چیدمان قبلی بازگردانده شد"); await load(); }
    else toast(r.reason || "چیزی برای بازگردانی نبود", true);
  } catch (e) { toast("خطا: " + e.message, true); }
  finally { btn.disabled = false; btn.textContent = "بازگردانی چیدمان قبلی"; }
}

async function openPond(pid) {
  let D;
  try { D = await api("/api/pond/" + pid); }
  catch (e) { toast("خطا در بازکردن استخر: " + e.message, true); return; }
  const p = D.pond, o2 = p.oxygen_load;
  const cohortOpts = [...new Set([...D.cohorts.map(c => c.cohort_id),
                                  ...DATA.meta.cohort_ids])];
  const fld = (id, label, extra = "") =>
    `<div class="field"><label>${label}</label>
       <input id="pf_${id}" type="number" step="any" ${extra}></div>`;
  const defaultCohort = D.cohorts[0] ? D.cohorts[0].cohort_id : "";
  const otherPonds = (DATA.meta.pond_ids || []).filter(x => x !== pid);

  $("#drawer").innerHTML = `
    <div class="row"><h3 style="flex:1">${p.label} (${p.pond_id})</h3>
      <button class="btn small" onclick="closeDrawer()">بستن</button></div>
    <div class="row" style="margin-bottom:10px">
      <span class="badge ${p.status === "over" ? "bad" : "neutral"}">${STATUS_TXT[p.status]}</span>
      <span class="badge neutral">${p.role === "reserve" ? "رزرو" : "عملیاتی"}</span>
      ${p.basis === "estimated" ? '<span class="badge estimated">تخصیص تخمینی</span>' : ""}
    </div>
    <table>
      <tr><td>حجم</td><td class="num">${n0(p.volume_m3)} m³</td></tr>
      <tr><td>تعداد ماهی</td><td class="num">${n0(p.count)}</td></tr>
      <tr><td>وزن متوسط</td><td class="num">${n2(p.avg_weight_g)} گرم</td></tr>
      <tr><td>زیست‌توده</td><td class="num">${n1(p.biomass_kg)} kg</td></tr>
      <tr><td>ظرفیت تجربی این وزن</td><td class="num">${n0(p.capacity)}</td></tr>
      <tr><td>درصد اشغال</td><td class="num">${p.capacity_applies ? pct(p.utilisation) : "—"}</td></tr>
      <tr><td>بار اکسیژن (تشخیصی)</td><td class="num">${pct(o2)}</td></tr>
      <tr><td>milestone بعدی</td><td class="num">${p.next_milestone
        ? n1(p.next_milestone.weight_g) + " گرم در " + jdate(p.next_milestone.date) : "—"}</td></tr>
      <tr><td>آخرین قرائت آب</td><td class="num">${D.water
        ? `${jdate(D.water.date)} · دما ${n1(D.water.temperature_c || 0)}°C ·
           DO ${n1(D.water.do_in || 0)}→${n1(D.water.do_out || 0)} · دبی ${n1(D.water.flow_l_s || 0)}`
        : "ثبت نشده"}</td></tr>
    </table>

    <h3 style="margin-top:16px">Estimated در برابر Actual</h3>
    ${D.cohorts.length ? `<table><thead><tr><th>Cohort</th><th class="num">تعداد</th>
      <th class="num">وزن تخمینی</th><th>مبنا</th><th class="num">آخرین داده واقعی</th>
      </tr></thead><tbody>` +
      D.cohorts.map(c => `<tr>
        <td class="small">${c.cohort_id}</td>
        <td class="num">${n0(c.count_in_pond)} ${basisBadge(c.alloc_basis)}</td>
        <td class="num">${n2(c.estimated_mean_weight_g)} g</td>
        <td>${basisBadge(c.weight_basis)}</td>
        <td class="num tiny">${c.last_actual_weight
          ? `وزن ${n2(c.last_actual_weight.weight_g)}g در ${jdate(c.last_actual_weight.txn_date)}`
          : "وزن‌کشی نشده"}${c.last_actual_count
          ? `<br>شمارش ${n0(c.last_actual_count.quantity)} در ${jdate(c.last_actual_count.txn_date)}` : ""}
          ${c.growth_offset_days ? `<br>offset رشد: ${n1(c.growth_offset_days)} روز` : ""}</td>
      </tr>`).join("") + "</tbody></table>"
      : '<div class="muted small">این استخر خالی است</div>'}

    <h3 style="margin-top:18px">ثبت داده واقعی این استخر</h3>
    <div class="sub">تعداد و وزن در فرم اصلی هستند؛ اطلاعات کم‌استفاده داخل
      <b>جزئیات بیشتر / More Details</b> قرار گرفته‌اند.</div>
    <div class="form-grid" style="margin-top:8px">
      <div class="field"><label>تاریخ اندازه‌گیری</label>
        <input id="pf_date" type="date" value="${today()}"></div>
      <div class="field"><label>Cohort</label>
        <select id="pf_cohort"><option value="">—</option>
          ${cohortOpts.map(c => `<option ${defaultCohort === c ? "selected" : ""}>${c}</option>`).join("")}</select></div>
      ${fld("actual_count", "تعداد واقعی ماهی (قطعه)")}
      ${fld("actual_mean_weight_g", "وزن متوسط نمونه (گرم)")}
      ${fld("sd_g", "انحراف معیار وزن (گرم)")}
      ${fld("n_sampled", "تعداد نمونه")}
      ${fld("biomass_kg", "زیست‌توده اندازه‌گیری‌شده (kg)")}
      <div class="field" style="grid-column:1/-1"><label>یادداشت اختیاری</label>
        <input id="pf_note" placeholder="شرایط، مشاهده، مسئول اندازه‌گیری…"></div>
    </div>
    <details style="margin-top:10px;border:1px solid var(--line);border-radius:10px;padding:10px 12px">
      <summary style="cursor:pointer;font-weight:600">جزئیات بیشتر / More Details</summary>
      <div class="form-grid" style="margin-top:10px">
        ${fld("temperature_c", "دما (°C)")}
        ${fld("do_in", "DO ورودی (mg/L)")}
        ${fld("do_out", "DO خروجی (mg/L)")}
        ${fld("flow_l_s", "دبی (L/s)")}
        <div class="field"><label>نوع خوراک</label>
          <select id="pf_feed_name"><option value="">—</option>
            ${DATA.meta.feed_names.map(f => `<option>${f}</option>`).join("")}</select></div>
        ${fld("feed_kg", "خوراک مصرف‌شده (kg)")}
      </div>
    </details>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" id="pf_save">ثبت داده واقعی</button>
      <span class="muted small">فیلدهای خالی نادیده گرفته می‌شوند.</span>
    </div>
    <div id="pf_result"></div>

    <h3 style="margin-top:20px">ثبت سریع رویداد استخر</h3>
    <div class="sub">این رویدادها مستقیماً در همان Transaction/Event system ثبت می‌شوند.</div>
    <div class="form-grid" style="margin-top:8px">
      <div class="field"><label>نوع رویداد</label>
        <select id="pe_type">
          <option value="transfer">انتقال به استخر دیگر</option>
          <option value="mortality">ثبت تلفات</option>
          <option value="sale">ثبت فروش از همین استخر</option>
          <option value="adjustment">سایر موارد</option>
        </select></div>
      <div class="field"><label>Cohort</label>
        <select id="pe_cohort"><option value="">—</option>
          ${cohortOpts.map(c => `<option ${defaultCohort === c ? "selected" : ""}>${c}</option>`).join("")}</select></div>
      <div class="field"><label>تاریخ رویداد</label><input id="pe_date" type="date" value="${today()}"></div>

      <div class="field pe-transfer"><label>استخر مقصد</label>
        <select id="pe_to_pond"><option value="">— انتخاب —</option>
          ${otherPonds.map(x => `<option>${x}</option>`).join("")}</select></div>
      <div class="field pe-transfer"><label>تعداد انتقال</label><input id="pe_transfer_qty" type="number" min="0" step="1"></div>

      <div class="field pe-mortality" style="display:none"><label>تعداد تلفات</label>
        <input id="pe_mortality_qty" type="number" min="0" step="1"></div>

      <div class="field pe-sale" style="display:none"><label>تعداد فروش</label>
        <input id="pe_sale_qty" type="number" min="0" step="1"></div>
      <div class="field pe-sale" style="display:none"><label>وزن متوسط فروش (گرم)</label>
        <input id="pe_sale_weight" type="number" min="0" step="any"></div>
      <div class="field pe-sale" style="display:none"><label>قیمت واحد (تومان، اختیاری)</label>
        <input id="pe_sale_price" type="number" min="0" step="any"></div>

      <div class="field pe-adjustment" style="display:none"><label>نوع تغییر</label>
        <select id="pe_adjust_dir"><option value="increase">افزایش تعداد</option><option value="decrease">کاهش تعداد</option></select></div>
      <div class="field pe-adjustment" style="display:none"><label>مقدار</label>
        <input id="pe_adjust_qty" type="number" min="0" step="1"></div>
      <div class="field pe-adjustment" style="display:none"><label>دلیل / Note</label>
        <input id="pe_adjust_reason" placeholder="دلیل افزایش یا کاهش"></div>

      <div class="field" style="grid-column:1/-1"><label>یادداشت اختیاری</label>
        <input id="pe_note" placeholder="توضیح تکمیلی رویداد…"></div>
    </div>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" id="pe_save">ثبت رویداد</button>
      <span class="muted small">پس از ثبت، وضعیت cohort، استخر و تاریخچه refresh می‌شود.</span>
    </div>
    <div id="pe_result"></div>

    <h3 style="margin-top:18px">تاریخچه استخر</h3>
    ${D.history.length ? `<table><tbody>` + D.history.slice(0, 30).map(t => {
      let label = TXN_LABELS[t.txn_type] || t.txn_type;
      if (t.txn_type === "count_observation" && (t.note || "").includes("سایر موارد")) label = "سایر موارد";
      return `<tr style="${t.status !== "active" ? "opacity:.5" : ""}">
       <td class="tiny">${jdate(t.txn_date)}</td>
       <td class="tiny">${label}</td>
       <td class="num tiny">${t.quantity === null ? "" : n0(t.quantity)} ${t.unit || ""}</td>
       <td class="tiny">${(t.note || "").slice(0, 42)}</td></tr>`;
    }).join("") + "</tbody></table>"
      : '<div class="muted small">رویدادی ثبت نشده است</div>'}`;

  $("#pf_save").onclick = () => savePondObservation(pid);
  $("#pe_type").onchange = renderPondEventFields;
  $("#pe_save").onclick = () => savePondEvent(pid, D);
  renderPondEventFields();
  $("#drawer").classList.add("open"); $("#overlay").classList.add("open");
}

function renderPondEventFields() {
  const typ = $("#pe_type") ? $("#pe_type").value : "transfer";
  ["transfer", "mortality", "sale", "adjustment"].forEach(k => {
    $$(".pe-" + k).forEach(el => el.style.display = k === typ ? "" : "none");
  });
}

async function savePondEvent(pid, D) {
  const v = id => { const e = $("#pe_" + id); return e ? e.value : ""; };
  const typ = v("type"), cohort = v("cohort"), when = v("date") || today();
  if (!cohort) { toast("برای ثبت رویداد، Cohort را انتخاب کنید", true); return; }
  const note = v("note");
  const body = { txn_date: when, cohort_id: cohort, pond_id: pid,
                 note: note, payload: { entry: "pond_panel" } };

  if (typ === "transfer") {
    if (!v("to_pond") || !v("transfer_qty")) { toast("استخر مقصد و تعداد انتقال لازم است", true); return; }
    body.txn_type = "transfer"; body.to_pond_id = v("to_pond"); body.quantity = parseFloat(v("transfer_qty"));
  } else if (typ === "mortality") {
    if (!v("mortality_qty")) { toast("تعداد تلفات را وارد کنید", true); return; }
    body.txn_type = "mortality"; body.quantity = parseFloat(v("mortality_qty"));
  } else if (typ === "sale") {
    if (!v("sale_qty") || !v("sale_weight")) { toast("تعداد و وزن فروش لازم است", true); return; }
    body.txn_type = "sale"; body.quantity = parseFloat(v("sale_qty")); body.weight_g = parseFloat(v("sale_weight"));
    if (v("sale_price") !== "") body.unit_price = parseFloat(v("sale_price"));
    else body.payload.price_pending = true;
  } else {
    if (!v("adjust_qty")) { toast("مقدار افزایش/کاهش را وارد کنید", true); return; }
    const delta = parseFloat(v("adjust_qty")) * (v("adjust_dir") === "decrease" ? -1 : 1);
    const occ = (D.cohorts || []).find(c => c.cohort_id === cohort);
    const current = occ ? Number(occ.count_in_pond || 0) : 0;
    if (delta < 0 && -delta > current + 1e-6) { toast("مقدار کاهش از تعداد فعلی این cohort در استخر بیشتر است", true); return; }
    body.txn_type = "count_observation";
    body.quantity = Math.max(0, current + delta);
    body.payload.pond_adjustment_delta = delta;
    body.payload.adjustment_direction = delta >= 0 ? "increase" : "decrease";
    body.payload.adjustment_amount = Math.abs(delta);
    body.payload.adjustment_reason = v("adjust_reason") || "";
    body.note = `سایر موارد — ${delta >= 0 ? "افزایش" : "کاهش"} ${n0(Math.abs(delta))} قطعه` +
                (v("adjust_reason") ? ` · ${v("adjust_reason")}` : "") + (note ? ` · ${note}` : "");
  }

  const btn = $("#pe_save"); btn.disabled = true; btn.textContent = "در حال ثبت…";
  try {
    const r = await api("/api/transactions", "POST", body);
    toast(`رویداد با موفقیت ثبت شد (#${r.id})`);
    await loadTxns(); await load();
    await openPond(pid); // فرم reset و تاریخچه/pond state تازه می‌شود
  } catch (e) {
    toast("خطا در ثبت رویداد: " + e.message, true);
    if (btn) { btn.disabled = false; btn.textContent = "ثبت رویداد"; }
  }
}

async function savePondObservation(pid) {
  const v = id => { const e = $("#pf_" + id); return e ? e.value : ""; };
  const body = { pond_id: pid, measurement_date: v("date"),
                 cohort_id: v("cohort"), note: v("note"),
                 feed_name: v("feed_name") };
  ["actual_count", "actual_mean_weight_g", "sd_g", "n_sampled", "mortality",
   "biomass_kg", "temperature_c", "do_in", "do_out", "flow_l_s", "feed_kg"]
    .forEach(k => { if (v(k) !== "") body[k] = parseFloat(v(k)); });
  try {
    const r = await api("/api/pond/observe", "POST", body);
    const b = r.before_estimated, a = r.after;
    $("#pf_result").innerHTML = `<div class="note ok" style="margin-top:10px">
      ${r.created.length} رکورد واقعی ثبت شد.
      ${b && a ? `<br>تعداد: تخمینی ${n0(b.estimated_alive)} ← ${n0(a.estimated_alive)}
        · وزن: تخمینی ${n2(b.estimated_mean_weight_g)}g ← ${n2(a.mean_weight_g)}g
        ${a.growth_offset_days ? `· offset رشد ${n1(a.growth_offset_days)} روز` : ""}` : ""}
      </div>`;
    toast("داده واقعی ثبت شد؛ forecast از این نقطه بازمحاسبه شد");
    await loadTxns(); await load(); await openPond(pid);
  } catch (e) { toast("خطا: " + e.message, true); }
}

function closeDrawer() {
  $("#drawer").classList.remove("open"); $("#overlay").classList.remove("open");
}
window.closeDrawer = closeDrawer;

/* ==================================================== capacity forecast */
function renderCapacity() {
  const c = DATA.capacity_curve, op = DATA.peak_pressure.operational_ponds;
  const svg = $("#capChart"), W = 760, H = 240, m = { t: 14, r: 14, b: 26, l: 40 };
  const maxY = Math.max(op * 1.25, ...c.map(x => x.ponds_required)) || 1;
  const X = i => m.l + (W - m.l - m.r) * (c[i].day_offset / (c[c.length - 1].day_offset || 1));
  const Y = v => H - m.b - (H - m.t - m.b) * (v / maxY);
  const pts = c.map((x, i) => `${X(i)},${Y(x.ponds_required)}`).join(" ");
  const area = `${m.l},${Y(0)} ${pts} ${X(c.length - 1)},${Y(0)}`;
  let ticks = "";
  for (let k = 0; k <= 4; k++) {
    const v = maxY * k / 4, y = Y(v);
    ticks += `<line x1="${m.l}" y1="${y}" x2="${W - m.r}" y2="${y}" stroke="rgba(26,40,48,.08)"/>
      <text x="${m.l - 6}" y="${y + 4}" font-size="10" fill="#64748b" text-anchor="end">${n0(v)}</text>`;
  }
  let xlab = "";
  c.forEach((x, i) => {
    if (x.day_offset % 30 === 0) xlab += `<text x="${X(i)}" y="${H - 8}" font-size="10"
      fill="#64748b" text-anchor="middle">${n0(x.day_offset)} روز</text>`;
  });
  svg.innerHTML = `${ticks}${xlab}
    <polygon points="${area}" fill="#3c7d90" opacity=".10"/>
    <polyline points="${pts}" fill="none" stroke="#3c7d90" stroke-width="2.2"/>
    <line x1="${m.l}" y1="${Y(op)}" x2="${W - m.r}" y2="${Y(op)}"
      stroke="#b9504f" stroke-width="1.6" stroke-dasharray="6 4"/>
    <text x="${W - m.r}" y="${Y(op) - 6}" font-size="10.5" fill="#b9504f"
      text-anchor="end">سقف ${n0(op)} استخر عملیاتی</text>`;
  const pp = DATA.peak_pressure;
  $("#capSub").innerHTML = `اوج نیاز <b>${n1(pp.peak_ponds_required)}</b> استخر در ${jdate(pp.peak_date)}` +
    (pp.first_breach_date ? ` · <span style="color:var(--bad)">اولین عبور از سقف: ${jdate(pp.first_breach_date)}</span>`
      : " · بدون عبور از سقف") +
    `<br><span class="tiny">فرض: ماهی در ${n0(pp.harvest_weight_g)} گرم فروخته می‌شود` +
    (pp.excluded_cohorts.length ? ` · cohortهای از این وزن گذشته از forecast خارج شده‌اند: ${pp.excluded_cohorts.join("، ")}` : "") +
    ` (قابل تغییر در تب فرضیات)</span>`;
  $("#capPoints").innerHTML = DATA.checkpoints.map(k =>
    `<span class="pill">${n0(k.days)} روز: <b>${n1(k.ponds_required)}</b> استخر
      ${k.breach ? '<span class="badge bad">عبور از سقف</span>' : ""}</span>`).join("");
}

/* ============================================================ timeline */
function renderTimeline() {
  const rows = DATA.timeline, svg = $("#timeline");
  if (!rows.length) { svg.innerHTML = ""; TL_STATE = null; return; }
  const H = Math.max(120, 46 + rows.length * 34), W = 900,
        m = { t: 26, r: 20, b: 24, l: 118 };
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const all = rows.flatMap(r => [r.purchase_date, r.end_date]);
  const t0 = new Date(Math.min(...all.map(d => +new Date(d))));
  const t1 = new Date(Math.max(...all.map(d => +new Date(d))));
  const span = Math.max(1, (t1 - t0));
  const X = d => m.l + (W - m.l - m.r) * ((new Date(d) - t0) / span);
  const now = DATA.meta.as_of;
  const MW = { 1: "۱", 2: "۲", 5: "۵", 10: "۱۰", 15: "۱۵" };
  let s = `<line x1="${X(now)}" y1="${m.t - 12}" x2="${X(now)}" y2="${H - m.b}"
     stroke="#3c7d90" stroke-width="1.4" stroke-dasharray="4 3"/>
    <text x="${X(now)}" y="${m.t - 16}" font-size="10" fill="#3c7d90" text-anchor="middle">امروز</text>`;
  rows.forEach((r, i) => {
    const y = m.t + i * 34;
    s += `<text x="${m.l - 8}" y="${y + 13}" font-size="10.5" fill="#1a2830"
        text-anchor="end">${r.cohort_id}</text>
      <text x="${m.l - 8}" y="${y + 25}" font-size="9" fill="#64748b"
        text-anchor="end">${n0(r.alive)} قطعه · ${n2(r.mean_weight_g)}g</text>
      <rect x="${X(r.purchase_date)}" y="${y + 5}" width="${Math.max(2, X(r.end_date) - X(r.purchase_date))}"
        height="14" rx="7" fill="#3c7d90" opacity=".13"/>`;
    r.marks.forEach(k => {
      const xa = X(k.date_fast), xb = X(k.date_slow), xm = X(k.date);
      s += `<line x1="${xa}" y1="${y + 12}" x2="${xb}" y2="${y + 12}"
          stroke="#6f5cae" stroke-width="1.1" opacity=".55"/>
        <circle cx="${xm}" cy="${y + 12}" r="4.6"
          fill="${k.passed ? "#149463" : "#fff"}" stroke="#3c7d90" stroke-width="1.6"/>
        <text x="${xm}" y="${y + 1}" font-size="8.5" fill="#64748b"
          text-anchor="middle">${MW[k.weight_g] || k.weight_g}</text>`;
    });
  });
  const months = 6;
  for (let k = 0; k <= months; k++) {
    const d = new Date(+t0 + span * k / months);
    s += `<text x="${X(d)}" y="${H - 6}" font-size="9" fill="#64748b"
      text-anchor="middle">${jdate(d.toISOString().slice(0, 10))}</text>`;
  }
  svg.innerHTML = s;

  // ---- لایهٔ تعاملی «Selected Date» — طراحی/منطق بالا دست‌نخورده ماند،
  // فقط این overlay روی همان چارت اضافه شده (بدون هیچ کتابخانهٔ جدید).
  TL_STATE = { t0, span, m, W, H, rows, X };
  svg.insertAdjacentHTML("beforeend", `<g id="tlSel" style="display:none;pointer-events:none">
      <line id="tlSelLine" x1="0" y1="${m.t - 12}" x2="0" y2="${H - m.b}"
        stroke="var(--gold)" stroke-width="1.6"/>
      <text id="tlSelLabel" x="0" y="${m.t - 16}" font-size="10.5" font-weight="700"
        fill="var(--gold)" text-anchor="middle"></text>
      ${rows.map((r, i) => `<text class="tlSelW" data-row="${i}" x="0"
        y="${m.t + i * 34 + 12 + 3}" font-size="9.5" font-weight="700"
        fill="var(--gold)" text-anchor="middle"></text>`).join("")}
    </g>`);
  bindTimelineInteraction(svg);
}
let TL_STATE = null;
let TL_PINNED = false;

function tlSvgX(svg, evt) {
  const pt = svg.createSVGPoint();
  pt.x = evt.clientX; pt.y = evt.clientY;
  const inv = svg.getScreenCTM().inverse();
  return pt.matrixTransform(inv).x;
}

function tlDateFromX(svgX) {
  const { t0, span, m, W } = TL_STATE;
  const frac = (svgX - m.l) / (W - m.l - m.r);
  const clamped = Math.max(0, Math.min(1, frac));
  return new Date(+t0 + span * clamped);
}

// درون‌یابی خطی سبک بین milestoneهای شناخته‌شدهٔ همان cohort (فقط برای
// نمایش در تعامل؛ هیچ منطق تصمیم‌گیری جدیدی نیست) — همان marks که خودِ
// چارت از قبل رسم می‌کند، دوباره استفاده می‌شوند.
function tlEstimateWeight(row, date) {
  const t = +date;
  const purchaseT = +new Date(row.purchase_date);
  if (t < purchaseT) return null;    // هنوز این cohort خریداری نشده بود
  const knots = [{ t: purchaseT, w: 0 },
                 ...row.marks.map(k => ({ t: +new Date(k.date), w: k.weight_g }))]
    .sort((a, b) => a.t - b.t);
  const last = knots[knots.length - 1];
  if (t >= last.t) return `${last.w}+`;
  for (let i = 0; i < knots.length - 1; i++) {
    const a = knots[i], b = knots[i + 1];
    if (t >= a.t && t <= b.t) {
      const f = (b.t - a.t) > 0 ? (t - a.t) / (b.t - a.t) : 0;
      return (a.w + (b.w - a.w) * f).toFixed(1);
    }
  }
  return last.w;
}

function tlUpdateSelection(svgX) {
  const { rows } = TL_STATE;
  const d = tlDateFromX(svgX);
  const iso = d.toISOString().slice(0, 10);
  const g = $("#tlSel");
  g.style.display = "";
  $("#tlSelLine").setAttribute("x1", svgX);
  $("#tlSelLine").setAttribute("x2", svgX);
  $("#tlSelLabel").setAttribute("x", svgX);
  $("#tlSelLabel").textContent = jdate(iso);
  $$(".tlSelW").forEach(el => {
    const r = rows[+el.dataset.row];
    const w = tlEstimateWeight(r, d);
    el.setAttribute("x", svgX);
    el.textContent = w === null ? "" : `${w}g`;
  });
}

function bindTimelineInteraction(svg) {
  if (svg.dataset.tlBound) return;    // فقط یک‌بار listener وصل شود
  svg.dataset.tlBound = "1";
  TL_PINNED = false;

  const onMove = evt => {
    if (!TL_STATE) return;
    const x = tlSvgX(svg, evt);
    if (x < TL_STATE.m.l || x > TL_STATE.W - TL_STATE.m.r) return;
    tlUpdateSelection(x);
  };
  // حرکت موس روی Timeline و درگ افقی هر دو همین یک رویداد را دارند —
  // خط همیشه دنبال نشانگر می‌آید، چه دکمه پایین باشد چه نباشد.
  svg.addEventListener("mousemove", onMove);
  svg.addEventListener("mouseleave", () => {
    if (!TL_PINNED && $("#tlSel")) $("#tlSel").style.display = "none";
  });
  // Click: تاریخ انتخابی را «سنجاق» می‌کند تا با دورشدن موس هم باقی
  // بماند (برای خواندن راحت‌تر برچسب‌ها)؛ کلیک دوباره باز می‌کند.
  svg.addEventListener("click", evt => {
    TL_PINNED = !TL_PINNED;
    onMove(evt);
    if (!TL_PINNED) $("#tlSel").style.display = "none";
  });
}

/* =========================================================== milestones */
function renderMilestones() {
  const ms = DATA.milestones;
  $("#msTable").innerHTML = `<thead><tr><th>Cohort</th><th class="num">وزن</th>
    <th>تاریخ</th><th class="num">روز</th><th class="num">تعداد</th>
    <th class="num">استخر لازم</th><th class="num">ارزش بالقوه</th></tr></thead><tbody>` +
    (ms.length ? ms.map(m => `<tr>
      <td class="small">${m.cohort_id}</td>
      <td class="num">${n0(m.weight_g)} g</td>
      <td class="small">${jdate(m.date)}</td>
      <td class="num">${n0(m.days_from_now)}</td>
      <td class="num">${n0(m.fish)}</td>
      <td class="num">${n2(m.ponds_required)}</td>
      <td class="num">${money(m.potential_revenue)}</td></tr>`).join("")
      : `<tr><td colspan="7" class="muted">milestone پیش‌رویی وجود ندارد</td></tr>`) +
    "</tbody>";
}

function renderCash() {
  const L = DATA.ledger;
  const wcRows = [
    ["سرمایه در گردش موجود / فرض پایه", money(L.wc_available),
     "ورودی قابل تغییر در تب فرضیات — خروجی ثابت optimizer نیست"],
    ["سرمایه قفل‌شده هم‌اکنون", money(L.wc_tied_up_now),
     "موجودی زنده + خوراک + کسری نقدی جاری، از تراکنش‌های واقعی"],
    ["اوج پیش‌بینی‌شده موردنیاز", money(L.wc_forecast_peak),
     `افق ${n0(L.wc_forecast_days)} روز · بدترین نقطه: ${jdate(L.wc_forecast_peak_date)}`],
  ];
  const head = `<table class="wc"><tbody>` + wcRows.map((r, i) =>
    `<tr class="${i === 0 ? "wc-a" : i === 1 ? "wc-b" : "wc-c"}">
       <td><b>${r[0]}</b><div class="tiny muted">${r[2]}</div></td>
       <td class="num"><b>${r[1]}</b></td></tr>`).join("") + `</tbody></table>
    <div class="note ${L.wc_breach ? "bad" : ""}" style="margin:8px 0">
      ${L.wc_breach
        ? `نیاز پیش‌بینی‌شده <b>${money(Math.max(L.wc_tied_up_now, L.wc_forecast_peak))}</b>
           از سرمایه در گردش موجود بیشتر است؛ کسری تقریبی
           <b>${money(-L.wc_headroom)}</b>.`
        : `فاصله تا سقف سرمایه در گردش موجود: <b>${money(L.wc_headroom)}</b>.`}
    </div>`;
  const rows = [
    ["موجودی نقد ابتدای دوره", money(L.opening_cash)],
    ["مانده نقدی فعلی", money(L.closing_balance)],
    ["کمترین مانده نقدی", money(L.minimum_cash_balance)],
    ["اوج کسری نقدی", money(L.peak_cash_deficit)],
    ["سرمایه قفل‌شده در موجودی زنده", money(L.inventory_capital)],
    ["ارزش موجودی خوراک", money(L.feed_inventory_value)],
    ["مجموع ورودی نقدی", money(L.total_inflow)],
    ["مجموع خروجی نقدی", money(L.total_outflow)],
    ["هزینه‌های جانبی واقعی ثبت‌شده", money(L.actual_operating_cost_total) +
      ` <span class="badge neutral" title="حالت ترکیب با هزینه ثابت پایه">${
        { top_up: "مابه‌التفاوت", baseline_only: "فقط پایه", actual_only: "فقط واقعی" }[L.fixed_cost_mode] || L.fixed_cost_mode}</span>`],
    ["چرخه تبدیل وجه نقد", L.cash_conversion_cycle_days === null ? "—"
      : n0(L.cash_conversion_cycle_days) + " روز"],
    ["بازده روی اوج سرمایه در گردش", L.return_on_peak_wc === null ? "—" : pct(L.return_on_peak_wc)],
  ];
  const cats = Object.entries(L.cost_by_category || {});
  $("#cashTable").innerHTML = head + "<tbody>" + rows.map(r =>
    `<tr><td>${r[0]}</td><td class="num">${r[1]}</td></tr>`).join("") + "</tbody>";
  if (cats.length) {
    $("#cashTable").innerHTML += `<tbody><tr><td colspan="2" class="tiny muted"
      style="padding-top:10px">هزینه‌های جانبی به تفکیک دسته</td></tr>` +
      cats.map(([k, v]) => `<tr><td class="small">${k}</td>
        <td class="num small">${money(v)}</td></tr>`).join("") + "</tbody>";
  }
}

/* ==================================================================== FX */
function renderFx() {
  const f = DATA.fx, k = f.kpi, live = f.live || {};
  const liveBox = live.available ? `
    <div class="card" style="padding:10px;margin-bottom:10px">
      <div class="row" style="justify-content:space-between;gap:8px;flex-wrap:wrap">
        <div><b>Latest USD/Toman</b> <span class="num" style="font-size:18px">${n0(live.close_toman)} تومان</span></div>
        <span class="badge ${live.fresh ? "ok" : "bad"}">${live.status || (live.fresh ? "Fresh" : "Stale")}</span>
      </div>
      <div class="tiny muted" style="margin-top:5px">
        Last verified close: <span dir="ltr">${live.date_g}</span>
        · ${live.age_days || 0} day${live.age_days === 1 ? "" : "s"} old
        · source: <span class="mono">${live.source || "—"}</span>
      </div>
      <table style="margin-top:7px"><tbody>
        <tr><td>تغییر روزانه</td><td class="num">${live.daily_change == null ? "—" : pct(live.daily_change)}</td></tr>
        <tr><td>از ابتدای سه‌ماهه</td><td class="num">${live.change_since_quarter_start == null ? "—" : pct(live.change_since_quarter_start)}</td></tr>
        <tr><td>از ابتدای سال</td><td class="num">${live.change_since_year_start == null ? "—" : pct(live.change_since_year_start)}</td></tr>
      </tbody></table>
      ${live.stale ? `<div class="note bad" style="margin-top:7px">این نرخ، آخرین close تأییدشده است و نرخ امروز محسوب نمی‌شود.</div>` : ""}
      <div class="row" style="margin-top:7px">
        <button class="btn small" id="fxRefreshBtn">Refresh live FX</button>
        <button class="btn small" id="fxManualBtn">Manual fallback</button>
      </div>
    </div>` : `<div class="note bad" style="margin-bottom:8px">Live FX در دسترس نیست. نرخ جاری ساخته نمی‌شود؛ Excel تاریخی برای benchmark حفظ شده است.
      <div class="row" style="margin-top:7px"><button class="btn small" id="fxRefreshBtn">Refresh live FX</button>
      <button class="btn small" id="fxManualBtn">Manual fallback</button></div></div>`;
  if (!k.available) {
    $("#fxBox").innerHTML = liveBox + `<div class="note bad">بنچمارک ارزی در دسترس نیست —
      ${k.note || "داده واقعی نرخ دلار برای این دوره موجود نیست"}.<br>
      فایل <span class="mono" dir="ltr">TGJU_USD_3Y_Daily_Weekly_Close(1).xlsx</span>
      را در پوشه <span class="mono" dir="ltr">data/</span> بگذارید.
      هیچ داده جایگزینی ساخته نمی‌شود.</div>`;
    bindFxActions();
    return;
  }
  const shares = [0.25, 0.5, 0.75, 1];
  $("#fxBox").innerHTML = liveBox + `
    <div class="tiny muted" style="margin-bottom:6px">Quarterly benchmark — historical verified series
      · <span class="mono" dir="ltr">${k.file || ""}</span></div>
    <table style="margin-top:8px"><tbody>
      <tr><td>سه‌ماهه جاری</td><td class="num">${k.label}</td></tr>
      <tr><td>نرخ ابتدای سه‌ماهه</td><td class="num">${n0(k.fx_start)} تومان</td></tr>
      <tr><td>آخرین نرخ verified</td><td class="num">${n0(k.fx_latest)} تومان</td></tr>
      <tr><td>تغییر نرخ</td><td class="num">${pct(k.fx_return)}</td></tr>
      <tr><td>سرمایه درگیر ابتدای سه‌ماهه</td><td class="num">${money(k.capital)}</td></tr>
      <tr><td>USD Alternative End Value</td><td class="num">${money(k.usd_alternative_end_value)}</td></tr>
      <tr><td>USD Alternative Gain</td><td class="num">${money(k.usd_alternative_gain)}</td></tr>
      <tr><td>تغییر ارزش مزرعه در سه‌ماهه</td><td class="num">${money(k.farm_value_change)}</td></tr>
      <tr><td><b>مازاد نسبت به بنچمارک</b></td>
        <td class="num"><b style="color:${(k.excess_over_fx || 0) >= 0 ? "var(--ok)" : "var(--bad)"}">
          ${money(k.excess_over_fx)}</b></td></tr>
    </tbody></table>
    <div class="tiny muted" style="margin-top:6px">این بخش فقط Opportunity Cost / Capital Value Benchmark است و جایگزین سود عملیاتی، NPV یا objective optimiser نیست.</div>
    <div class="row" style="margin-top:9px">
      <span class="small muted">سهم سرمایه:</span>
      ${shares.map(s => `<button class="btn small ${s === k.benchmark_share ? "primary" : ""}"
        data-share="${s}">${pct(s, 0)}</button>`).join("")}
    </div>
    <div class="scroll" style="margin-top:10px;max-height:190px">
      <table><thead><tr><th>سه‌ماهه</th><th class="num">تغییر دلار</th>
      <th class="num">سرمایه</th><th class="num">سود بنچمارک</th></tr></thead><tbody>
      ${f.quarters.map(q => q.available === false
        ? `<tr class="muted"><td>${q.label}</td><td class="num" colspan="3">داده موجود نیست</td></tr>`
        : `<tr><td>${q.label}${q.is_current ? " *" : ""}</td>
        <td class="num">${pct(q.fx_return)}</td><td class="num">${money(q.capital)}</td>
        <td class="num">${money(q.usd_alternative_gain)}</td></tr>`).join("")}
      </tbody></table></div>`;
  $$("#fxBox [data-share]").forEach(b => b.onclick = async () => {
    await api("/api/assumptions", "POST",
      { key: "fx.benchmark_share", value: parseFloat(b.dataset.share) });
    await load(); toast("سهم سرمایه بنچمارک به‌روزرسانی شد");
  });
  bindFxActions();
}

function bindFxActions() {
  const rb = $("#fxRefreshBtn");
  if (rb) rb.onclick = async () => {
    await api("/api/fx/refresh", "POST", {});
    await load(); toast("FX refresh completed");
  };
  const mb = $("#fxManualBtn");
  if (mb) mb.onclick = async () => {
    const ds = prompt("تاریخ close (YYYY-MM-DD)", new Date().toISOString().slice(0,10));
    if (!ds) return;
    const vs = prompt("USD/Toman closing price");
    if (!vs) return;
    const v = Number(String(vs).replace(/,/g, ""));
    if (!Number.isFinite(v) || v <= 0) { toast("نرخ نامعتبر است"); return; }
    await api("/api/fx/manual", "POST", {date: ds, close_toman: v, verified: true});
    await load(); toast("Manual FX saved");
  };
}

/* =============================================================== cohorts */
function renderCohorts() {
  const cs = DATA.cohorts;
  $("#cohortTable").innerHTML = `<thead><tr>
    <th>Cohort</th><th>تاریخ خرید</th><th class="num">سن</th><th class="num">تخم</th>
    <th class="num">زنده</th><th>مبنای تعداد</th><th class="num">تلفات تجمعی</th>
    <th class="num">وزن متوسط</th><th>مبنای وزن</th><th class="num">زیست‌توده</th>
    <th class="num">استخر لازم</th><th>استخرها</th><th>milestone بعدی</th>
    <th class="num">ارزش موجودی</th></tr></thead><tbody>` +
    cs.map(c => `<tr>
      <td class="small">${c.cohort_id}${c.beyond_model_range
        ? ' <span class="badge bad" title="خارج از بازه معتبر مدل">!</span>' : ""}</td>
      <td class="small">${jdate(c.purchase_date)}</td>
      <td class="num">${n0(c.age_days)}</td>
      <td class="num">${n0(c.egg_count)}</td>
      <td class="num">${n0(c.alive)}</td>
      <td>${basisBadge(c.count_basis)}</td>
      <td class="num">${pct(c.cum_mortality)}</td>
      <td class="num">${n2(c.mean_weight_g)} g</td>
      <td>${basisBadge(c.weight_basis)}</td>
      <td class="num">${n0(c.biomass_kg)} kg</td>
      <td class="num">${n2(c.ponds_required)}</td>
      <td class="small">${Object.keys(c.ponds).join("، ") || "—"}</td>
      <td class="small">${c.next_milestone
        ? `${n0(c.next_milestone.weight_g)}g · ${n0(c.next_milestone.days)} روز` : "—"}</td>
      <td class="num">${money(c.stock_value_at_current_price)}</td></tr>`).join("") + "</tbody>";

  const maxW = Math.max(...cs.map(c => c.w_fast), 1);
  $("#distList").innerHTML = cs.map(c => {
    const a = 100 * c.w_slow / maxW, b = 100 * c.w_fast / maxW, mid = 100 * c.mean_weight_g / maxW;
    return `<div>
      <div class="row small" style="justify-content:space-between">
        <span><b>${c.cohort_id}</b> · CV = ${pct(c.cv)}</span>
        <span class="muted">کند ${n2(c.w_slow)}g · میانه ${n2(c.w_typical)}g · تند ${n2(c.w_fast)}g</span>
      </div>
      <div class="dist">
        <div class="rng" style="right:${a}%;width:${Math.max(1, b - a)}%"></div>
        <div class="mid" style="right:${mid}%"></div>
        <div class="lbl">${n2(c.mean_weight_g)} گرم (میانگین)</div>
      </div></div>`;
  }).join("");
}

/* ================================================================== feed */
function renderFeed() {
  const f = DATA.feed;
  $("#feedTable").innerHTML = `<thead><tr><th>نوع خوراک</th><th class="num">موجودی kg</th>
    <th class="num">میانگین موزون هزینه</th><th class="num">ارزش موجودی</th>
    <th class="num">خرید کل</th><th class="num">مصرف ثبت‌شده</th>
    <th>آخرین خرید</th></tr></thead><tbody>` +
    (f.length ? f.map(x => `<tr><td>${x.name}</td>
      <td class="num">${n0(x.qty_kg)}</td>
      <td class="num">${n0(x.avg_cost)}</td>
      <td class="num">${money(x.value)}</td>
      <td class="num">${n0(x.purchased_kg)}</td>
      <td class="num">${n0(x.consumed_kg)}</td>
      <td class="small">${jdate(x.last_purchase)}</td></tr>`).join("")
      : `<tr><td colspan="7" class="muted">هیچ خرید خوراکی ثبت نشده است</td></tr>`) + "</tbody>";

  const dem = DATA.feed_demand, keys = Object.keys(dem).filter(k => k !== "__total__");
  $("#feedOutlook").innerHTML = `<thead><tr><th>خوراک</th>
    <th class="num">مصرف روزانه فعلی (kg)</th><th class="num">موجودی (kg)</th>
    <th class="num">روز باقی‌مانده</th></tr></thead><tbody>` +
    (keys.length ? keys.map(k => {
      const stock = (f.find(x => x.name === k) || {}).qty_kg || 0;
      const days = dem[k] > 0 ? stock / dem[k] : null;
      return `<tr><td>${k}</td><td class="num">${n1(dem[k])}</td>
        <td class="num">${n0(stock)}</td>
        <td class="num ${days !== null && days < 14 ? "" : ""}">${days === null ? "—" : n0(days)}</td></tr>`;
    }).join("") : `<tr><td colspan="4" class="muted">cohort فعالی وجود ندارد</td></tr>`) +
    `<tr><td><b>جمع</b></td><td class="num"><b>${n1(dem.__total__)}</b></td>
     <td class="num"><b>${n0(DATA.summary.feed_inventory_kg)}</b></td>
     <td class="num"><b>${DATA.summary.feed_days_remaining === null ? "—"
       : n0(DATA.summary.feed_days_remaining)}</b></td></tr></tbody>`;
}

/* ============================================================== quality */
function renderChecks() {
  const ic = { pass: "✓", warn: "!", fail: "×" };
  $("#checkList").innerHTML = DATA.validation.checks.map(c =>
    `<div class="check ${c.status}"><span class="ic">${ic[c.status]}</span>
      <span><b>${c.title_fa}</b><br><span class="muted">${c.detail}</span></span></div>`).join("");
}

function renderRef() {
  $("#refTable").innerHTML = `<thead><tr><th class="num">وزن</th><th class="num">روز از خرید</th>
    <th class="num">تلفات تجمعی</th><th class="num">بقا</th><th class="num">ظرفیت استخر</th>
    <th class="num">قیمت فروش</th><th>خوراک</th><th class="num">قیمت خوراک</th>
    <th>منبع</th></tr></thead><tbody>` +
    DATA.reference.map(r => `<tr>
      <td class="num">${n0(r.weight_g)} g</td><td class="num">${n0(r.day)}</td>
      <td class="num">${pct(r.cum_mortality)}</td><td class="num">${pct(r.survival)}</td>
      <td class="num">${n0(r.fish_per_pond)}</td><td class="num">${n0(r.sale_price)}</td>
      <td>${r.feed_name}</td><td class="num">${n0(r.feed_price)}</td>
      <td>${srcBadge(r.source)}</td></tr>`).join("") + "</tbody>";
}

/* ========================================================= transactions */
const TXN_LABELS = {
  egg_purchase: "خرید تخم", feed_purchase: "خرید خوراک", feed_consumption: "مصرف خوراک",
  mortality: "تلفات", count_observation: "شمارش واقعی", weight_sample: "نمونه‌برداری وزن",
  transfer: "انتقال بین استخر", sale: "فروش", payment: "پرداخت", receipt: "دریافت",
  operating_cost: "هزینه عملیاتی", water_reading: "قرائت آب",
  maintenance: "نگهداری و تعمیرات"
};

// «نگهداری و تعمیرات» یک نوع جدا در پایگاه داده نیست؛ یک هزینه عملیاتی با
// دسته از پیش انتخاب‌شده است. این‌طور هم برای کاربر ساده است و هم ساختار
// تراکنش‌ها تمیز می‌ماند.
const TXN_ALIAS = { maintenance: { type: "operating_cost",
                                   category: "نگهداری و تعمیرات" } };
const TXN_FIELDS = {
  egg_purchase:      ["cohort_id_new", "quantity", "unit_price", "counterparty", "pond_id"],
  feed_purchase:     ["feed_name", "quantity", "unit_price", "counterparty"],
  feed_consumption:  ["feed_name", "quantity", "cohort_id"],
  mortality:         ["cohort_id", "quantity", "pond_id"],
  count_observation: ["cohort_id", "quantity", "pond_id"],
  weight_sample:     ["cohort_id", "weight_g", "sd_g", "quantity"],
  transfer:          ["cohort_id", "pond_id", "to_pond_id", "quantity"],
  sale:              ["cohort_id", "quantity", "weight_g", "unit_price", "counterparty", "pond_id"],
  payment:           ["amount", "counterparty"],
  receipt:           ["amount", "counterparty"],
  operating_cost:    ["category", "amount", "counterparty", "cohort_id", "pond_id"],
  maintenance:       ["amount", "counterparty", "pond_id"],
  water_reading:     ["pond_id", "temperature_c", "do_in", "do_out", "flow_l_s"],
};
const FIELD_LABELS = {
  cohort_id: "Cohort", cohort_id_new: "شناسه Cohort (اختیاری)", quantity: "تعداد / مقدار",
  unit_price: "قیمت واحد (تومان)", amount: "مبلغ (تومان)", weight_g: "وزن متوسط (گرم)",
  sd_g: "انحراف معیار وزن (گرم)", pond_id: "استخر", to_pond_id: "استخر مقصد",
  counterparty: "طرف حساب", feed_name: "نوع خوراک", temperature_c: "دما (°C)",
  do_in: "DO ورودی", do_out: "DO خروجی", flow_l_s: "دبی (L/s)",
  category: "دسته هزینه"
};

function buildTxnForm() {
  const box = $("#txnForm");
  const type = box._type || "egg_purchase";
  const meta = DATA.meta;
  const sel = (name, opts, val) => `<select id="f_${name}">
    <option value="">—</option>${opts.map(o =>
      `<option ${o === val ? "selected" : ""}>${o}</option>`).join("")}</select>`;
  let h = `<div class="field"><label>نوع رویداد</label>
    <select id="f_type">${Object.keys(TXN_LABELS).map(k =>
      `<option value="${k}" ${k === type ? "selected" : ""}>${TXN_LABELS[k]}</option>`).join("")}</select></div>
    <div class="field"><label>تاریخ (میلادی)</label>
      <input id="f_date" type="date" value="${today()}"></div>`;
  const required = REQUIRED_BY_TYPE[type] || [];
  (TXN_FIELDS[type] || []).forEach(f => {
    let inp;
    if (f === "cohort_id") inp = sel("cohort_id", meta.cohort_ids);
    else if (f === "pond_id" || f === "to_pond_id") inp = sel(f, meta.pond_ids);
    else if (f === "feed_name") inp = sel("feed_name", meta.feed_names);
    else if (f === "category") inp = sel("category", meta.cost_categories);
    else if (f === "cohort_id_new") inp = `<input id="f_cohort_id" placeholder="خودکار">`;
    else if (f === "counterparty") inp = `<input id="f_${f}">`;
    else inp = `<input id="f_${f}" type="number" step="any">`;
    const key = f === "cohort_id_new" ? "cohort_id" : f;
    const star = required.includes(key)
      ? ' <span style="color:var(--bad)">*</span>' : "";
    h += `<div class="field"><label>${FIELD_LABELS[f] || f}${star}</label>${inp}</div>`;
  });
  h += `<div class="field" style="grid-column:1/-1"><label>یادداشت</label>
    <input id="f_note" placeholder="توضیح یا مرجع"></div>`;
  box.innerHTML = h;
  box._type = type;
  $("#f_type").onchange = e => { box._type = e.target.value; buildTxnForm(); };
  // Enter در هر فیلد = ثبت رویداد
  box.querySelectorAll("input").forEach(el => el.addEventListener("keydown", ev => {
    if (ev.key === "Enter") { ev.preventDefault(); submitTxn(); }
  }));
  $("#txnHint").textContent = {
    weight_sample: "این رکورد مبنای رشد را به داده واقعی منتقل می‌کند.",
    count_observation: "این رکورد تعداد را به‌صورت مطلق تثبیت می‌کند.",
    mortality: "تا تاریخ آخرین رکورد تلفات، منحنی مدل غیرفعال می‌شود.",
    operating_cost: "هزینه واقعی ثبت‌شده؛ برای جلوگیری از double counting طبق حالت انتخابی در فرضیات با هزینه ثابت پایه ترکیب می‌شود.",
    egg_purchase: "اگر قیمت واحد را خالی بگذارید، قیمت با تاریخ اعتبار همان روز اعمال می‌شود.",
  }[type] || "";
}

const REQUIRED_BY_TYPE = {
  egg_purchase: ["quantity"],
  maintenance: ["amount"],
  feed_purchase: ["feed_name", "quantity"],
  feed_consumption: ["feed_name", "quantity"],
  mortality: ["cohort_id", "quantity"],
  count_observation: ["cohort_id", "quantity"],
  weight_sample: ["cohort_id", "weight_g"],
  transfer: ["cohort_id", "to_pond_id", "quantity"],
  sale: ["quantity"],
  payment: ["amount"], receipt: ["amount"],
  operating_cost: ["amount"],
  water_reading: ["pond_id"],
};

async function submitTxn() {
  const typeEl = $("#f_type");
  if (!typeEl) { buildTxnForm(); toast("فرم دوباره ساخته شد؛ لطفاً مقادیر را وارد کنید", true); return; }
  const type = typeEl.value, v = id => { const e = $("#f_" + id); return e ? e.value : ""; };

  // اعتبارسنجی سمت کاربر پیش از ارسال
  const missing = (REQUIRED_BY_TYPE[type] || []).filter(k => v(k) === "");
  if (missing.length) {
    toast("این فیلدها الزامی‌اند: " +
      missing.map(k => FIELD_LABELS[k] || k).join("، "), true);
    missing.forEach(k => { const el = $("#f_" + k); if (el) el.focus(); });
    return;
  }
  if (!v("date")) { toast("تاریخ رویداد الزامی است", true); return; }
  const alias = TXN_ALIAS[type];
  const body = { txn_type: alias ? alias.type : type,
                 txn_date: v("date"), note: v("note"), payload: {} };
  if (alias && alias.category) body.category = alias.category;
  ["cohort_id", "pond_id", "to_pond_id", "counterparty", "category"].forEach(k => {
    if (v(k)) body[k] = v(k);
  });
  ["quantity", "weight_g", "unit_price", "amount"].forEach(k => {
    if (v(k) !== "") body[k] = parseFloat(v(k));
  });
  ["feed_name"].forEach(k => { if (v(k)) body.payload[k] = v(k); });
  ["sd_g", "temperature_c", "do_in", "do_out", "flow_l_s"].forEach(k => {
    if (v(k) !== "") body.payload[k] = parseFloat(v(k));
  });
  if (type === "feed_purchase" && body.payload.feed_name) {
    body.note = body.note || body.payload.feed_name;
  }
  const btn = $("#btnAddTxn");
  const label = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "در حال ثبت…"; }
  try {
    const r = await api("/api/transactions", "POST", body);
    // بازمحاسبه کامل: فهرست رویدادها، Live Farm State، cohortها، استخرها،
    // موجودی خوراک، دفتر نقدی، KPIها و forecast
    await loadTxns();
    await load();
    if (PLAN) await loadPlan();
    if (ASSUME) await loadAssumptions();
    $("#txnResult").innerHTML = `<div class="note ok">
      <b>Transaction saved successfully</b> — رویداد #${r.id} ثبت شد.
      <div class="tiny">${TXN_LABELS[r.txn_type] || r.txn_type} ·
        تاریخ ${jdate(r.txn_date)} ·
        ${r.quantity !== null && r.quantity !== undefined
          ? n0(r.quantity) + " " + (r.unit || "") : ""}
        ${r.amount ? " · " + money(r.amount) : ""} ·
        ثبت در ${(r.created_at || "").slice(0, 16).replace("T", " ")}
        ${r.cohort_id ? " · cohort " + r.cohort_id : ""}</div></div>`;
    toast("Transaction saved successfully — رویداد ثبت و وضعیت مزرعه بازمحاسبه شد");
    resetTxnForm(type);
  } catch (e) {
    $("#txnResult").innerHTML = `<div class="note bad">
      <b>رویداد ثبت نشد</b> — هیچ داده ناقصی ذخیره نشده است.
      <div class="tiny">${e.message}</div></div>`;
    toast("خطا: " + e.message, true);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

function resetTxnForm(keepType) {
  const box = $("#txnForm");
  box._type = keepType;
  buildTxnForm();          // نوع رویداد و تاریخ می‌مانند، مقادیر پاک می‌شوند
}

async function loadTxns() {
  const r = await api("/api/transactions");
  TXNS = r.transactions;
  $("#txnTable").innerHTML = `<thead><tr><th>#</th><th>تاریخ</th><th>نوع</th><th>Cohort</th>
    <th>استخر</th><th class="num">مقدار</th><th class="num">قیمت واحد</th>
    <th class="num">مبلغ</th><th>طرف حساب</th><th>وضعیت</th>
    <th>یادداشت</th><th></th></tr></thead><tbody>` +
    TXNS.map(t => `<tr style="${t.status !== "active" ? "opacity:.5" : ""}">
      <td class="tiny">${t.id}${t.corrects_id
        ? `<div class="tiny muted">اصلاح #${t.corrects_id}</div>` : ""}</td>
      <td class="small">${jdate(t.txn_date)}</td>
      <td class="small">${TXN_LABELS[t.txn_type] || t.txn_type}
        ${t.category ? `<div class="tiny muted">${t.category}</div>` : ""}</td>
      <td class="tiny">${t.cohort_id || '<span class="badge estimated">بدون cohort</span>'}</td>
      <td class="tiny">${t.pond_id || ""}${t.to_pond_id ? " → " + t.to_pond_id : ""}</td>
      <td class="num">${t.quantity === null ? "—" : n0(t.quantity)}
        <span class="tiny muted">${t.unit || ""}</span></td>
      <td class="num small">${t.unit_price ? n0(t.unit_price) : "—"}</td>
      <td class="num">${t.amount ? money(t.amount) : "—"}</td>
      <td class="tiny">${t.counterparty || "—"}</td>
      <td>${t.status === "active" ? '<span class="badge actual">فعال</span>'
        : t.status === "corrected" ? '<span class="badge estimated">اصلاح‌شده</span>'
        : '<span class="badge bad">باطل</span>'}</td>
      <td class="tiny">${(t.note || "").slice(0, 40)}</td>
      <td class="nowrap">
        ${t.status === "active"
          ? `<button class="btn small ghost" data-edit="${t.id}">ویرایش</button>
             <button class="btn small ghost" data-void="${t.id}">ابطال</button>` : ""}
        <button class="btn small ghost" data-hist="${t.id}">تاریخچه</button>
      </td>
    </tr>`).join("") + "</tbody>";
  $$("[data-void]").forEach(b => b.onclick = async () => {
    const why = prompt("دلیل ابطال این تراکنش؟ (رکورد حذف نمی‌شود و در تاریخچه می‌ماند)");
    if (why === null) return;
    await api(`/api/transactions/${b.dataset.void}/void`, "POST", { note: why || "از داشبورد" });
    await loadTxns(); await load(); toast("تراکنش باطل شد؛ رکورد در تاریخچه باقی ماند");
  });
  $$("[data-edit]").forEach(b => b.onclick = () => openTxnEdit(parseInt(b.dataset.edit)));
  $$("[data-hist]").forEach(b => b.onclick = () => openTxnHistory(parseInt(b.dataset.hist)));
}

/* ------------------------------------------- ویرایش با حفظ audit trail (۵) */
const EDITABLE = [
  ["txn_date", "تاریخ", "date"], ["quantity", "مقدار", "number"],
  ["weight_g", "وزن (گرم)", "number"], ["unit_price", "قیمت واحد", "number"],
  ["amount", "مبلغ کل", "number"], ["cohort_id", "Cohort", "text"],
  ["pond_id", "استخر", "text"], ["to_pond_id", "استخر مقصد", "text"],
  ["category", "دسته هزینه", "text"], ["counterparty", "طرف حساب", "text"],
  ["note", "یادداشت", "text"],
];

function openTxnEdit(id) {
  const t = TXNS.find(x => x.id === id); if (!t) return;
  $("#drawer").innerHTML = `
    <div class="row"><h3 style="flex:1">ویرایش رویداد #${t.id}</h3>
      <button class="btn small" onclick="closeDrawer()">بستن</button></div>
    <div class="note">رکورد قبلی حذف یا overwrite نمی‌شود. یک نسخه جدید ثبت می‌شود و
      نسخه فعلی با برچسب «اصلاح‌شده» و مقدار اولیه خود در تاریخچه باقی می‌ماند.</div>
    <div class="form-grid" style="margin-top:10px">
      ${EDITABLE.map(([k, label, type]) => `<div class="field"><label>${label}</label>
        <input id="ed_${k}" type="${type === "number" ? "number" : type}"
          ${type === "number" ? 'step="any"' : ""}
          value="${t[k] === null || t[k] === undefined ? "" : t[k]}"></div>`).join("")}
      <div class="field" style="grid-column:1/-1"><label>دلیل اصلاح (ثبت می‌شود)</label>
        <input id="ed_reason" placeholder="مثلاً: قیمت واقعی فاکتور متفاوت بود"></div>
    </div>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" id="ed_save">ثبت اصلاح</button>
      <span class="muted small">نوع رویداد تغییر نمی‌کند: ${TXN_LABELS[t.txn_type] || t.txn_type}</span>
    </div>`;
  $("#ed_save").onclick = async () => {
    const body = { reason: $("#ed_reason").value || "بدون توضیح" };
    EDITABLE.forEach(([k, , type]) => {
      const val = $("#ed_" + k).value;
      const orig = t[k] === null || t[k] === undefined ? "" : String(t[k]);
      if (val !== orig) body[k] = (type === "number" && val !== "") ? parseFloat(val)
        : (val === "" ? null : val);
    });
    if (Object.keys(body).length === 1) { toast("تغییری وارد نشده است", true); return; }
    try {
      await api(`/api/transactions/${id}/correct`, "POST", body);
      toast("اصلاح ثبت شد؛ همه محاسبات وابسته بازمحاسبه شدند");
      closeDrawer(); await loadTxns(); await load();
    } catch (e) { toast("خطا: " + e.message, true); }
  };
  $("#drawer").classList.add("open"); $("#overlay").classList.add("open");
}

async function openTxnHistory(id) {
  const r = await api(`/api/transactions/${id}/history`);
  const c = r.chain;
  const cell = (v, u) => v === null || v === undefined ? "—" : n0(v) + (u ? " " + u : "");
  $("#drawer").innerHTML = `
    <div class="row"><h3 style="flex:1">تاریخچه اصلاحات رویداد #${id}</h3>
      <button class="btn small" onclick="closeDrawer()">بستن</button></div>
    <div class="sub">${c.length === 1 ? "این رکورد اصلاح نشده است."
      : `${c.length} نسخه — نسخه اول مقدار اولیه است.`}</div>
    <table style="margin-top:10px"><thead><tr><th>#</th><th>تاریخ رویداد</th>
      <th class="num">مقدار</th><th class="num">مبلغ</th><th>وضعیت</th>
      <th>ثبت در</th><th>دلیل</th></tr></thead><tbody>
      ${c.map((t, i) => `<tr>
        <td class="tiny">${t.id}${i === 0 ? ' <span class="badge neutral">اولیه</span>' : ""}</td>
        <td class="small">${jdate(t.txn_date)}</td>
        <td class="num">${cell(t.quantity, t.unit)}</td>
        <td class="num">${t.amount ? money(t.amount) : "—"}</td>
        <td>${t.status === "active" ? '<span class="badge actual">فعال</span>'
          : t.status === "corrected" ? '<span class="badge estimated">اصلاح‌شده</span>'
          : '<span class="badge bad">باطل</span>'}</td>
        <td class="tiny">${(t.created_at || "").slice(0, 16).replace("T", " ")}</td>
        <td class="tiny">${t.correction_reason || "—"}</td></tr>`).join("")}
    </tbody></table>`;
  $("#drawer").classList.add("open"); $("#overlay").classList.add("open");
}

/* ================================================================ offers */
function buildOfferForm() {
  $("#offerForm").innerHTML = `
    <div class="field"><label>تاریخ آفر</label><input id="o_date" type="date" value="${today()}"></div>
    <div class="field"><label>تأمین‌کننده</label><input id="o_sup"></div>
    <div class="field"><label>تعداد تخم</label><input id="o_qty" type="number" step="any"></div>
    <div class="field"><label>قیمت هر تخم (تومان)</label><input id="o_price" type="number" step="any"></div>
    <div class="field"><label>اعتبار تا</label><input id="o_exp" type="date"></div>
    <div class="field"><label>مهلت پرداخت (روز)</label><input id="o_terms" type="number" value="0"></div>
    <div class="field"><label>امتیاز کیفیت (۰-۱۰)</label><input id="o_q" type="number" step="any"></div>`;
}

async function loadOffers() {
  OFFERS = await api("/api/offers");
  const st = { pending: "در انتظار", accepted: "پذیرفته", partial: "پذیرش جزئی",
               rejected: "رد شده", expired: "منقضی" };
  $("#offerTable").innerHTML = `<thead><tr><th>شناسه</th><th>تاریخ</th><th>تأمین‌کننده</th>
    <th class="num">تعداد</th><th class="num">قیمت</th><th>وضعیت</th>
    <th class="num">پذیرفته</th><th></th></tr></thead><tbody>` +
    OFFERS.egg_offers.map(o => `<tr>
      <td class="tiny">${o.offer_id}</td><td class="small">${jdate(o.offer_date)}</td>
      <td class="small">${o.supplier || "—"}</td><td class="num">${n0(o.quantity)}</td>
      <td class="num">${n0(o.price_per_egg)}</td>
      <td><span class="badge ${o.status === "rejected" ? "bad"
        : o.status === "pending" ? "neutral" : "actual"}">${st[o.status] || o.status}</span></td>
      <td class="num">${n0(o.accepted_quantity)}</td>
      <td>${o.status === "pending" ? `
        <button class="btn small" data-acc="${o.offer_id}">پذیرش</button>
        <button class="btn small" data-rej="${o.offer_id}">رد</button>` : ""}</td>
    </tr>`).join("") + "</tbody>";
  $$("[data-acc]").forEach(b => b.onclick = async () => {
    const o = OFFERS.egg_offers.find(x => x.offer_id === b.dataset.acc);
    const q = prompt("تعداد پذیرفته‌شده:", o.quantity);
    if (q === null) return;
    await api("/api/offers/egg/decide", "POST",
      { offer_id: o.offer_id, decision: "accept", quantity: parseFloat(q) });
    await loadOffers(); await loadTxns(); await load(); toast("آفر پذیرفته و cohort ساخته شد");
  });
  $$("[data-rej]").forEach(b => b.onclick = async () => {
    const note = prompt("دلیل رد (برای یادگیری آینده ذخیره می‌شود):", "") || "";
    await api("/api/offers/egg/decide", "POST",
      { offer_id: b.dataset.rej, decision: "reject", note });
    await loadOffers(); toast("آفر رد شد و در تاریخچه ماند");
  });
}

/* =========================================================== assumptions */
async function loadAssumptions() {
  ASSUME = await api("/api/assumptions");
  $("#assumeList").innerHTML = ASSUME.groups.filter(g => g.params.length).map(g => `
    <h3 style="margin:16px 0 8px;font-size:14px">${g.title_fa}</h3>
    <div class="alist">${g.params.map(p => renderParam(p)).join("")}</div>`).join("");
  $$("[data-save]").forEach(b => b.onclick = () => saveParam(b.dataset.save));
  $$("[data-reset]").forEach(b => b.onclick = async () => {
    await api("/api/assumptions/reset", "POST", { key: b.dataset.reset });
    await loadAssumptions(); await load(); toast("به مقدار پیش‌فرض بازگشت");
  });
  $$("[data-eff]").forEach(b => b.onclick = () => openEffectiveDialog(b.dataset.eff));
  $$("[data-effdel]").forEach(b => b.onclick = async () => {
    if (!confirm("این تغییر تاریخ‌دار حذف شود؟")) return;
    await api("/api/assumptions/effective/delete", "POST", { id: parseInt(b.dataset.effdel) });
    await loadAssumptions(); await load(); toast("رکورد تاریخ‌دار حذف شد");
  });
}

/* -------------------------------- تغییر قیمت با تاریخ اعتبار (اصلاح ۶) */
function openEffectiveDialog(key) {
  const p = ASSUME.groups.flatMap(g => g.params).find(x => x.key === key);
  if (!p) return;
  const isFeedTable = key === "feed.price_table" && Array.isArray(p.value);
  $("#drawer").innerHTML = `
    <div class="row"><h3 style="flex:1">${p.label_fa}</h3>
      <button class="btn small" onclick="closeDrawer()">بستن</button></div>
    <div class="note">مقدار جدید فقط <b>از تاریخ اعتبار به بعد</b> اعمال می‌شود.
      تراکنش‌های قبل از آن تاریخ با قیمت تاریخی خودشان باقی می‌مانند و هیچ
      محاسبه‌ای retroactive تغییر نمی‌کند.</div>
    ${isFeedTable ? `
    <div id="feedTableEditor" style="margin-top:10px"></div>
    <div class="row" style="margin-top:6px">
      <button class="btn small ghost" id="feedRowAdd">+ ردیف</button>
    </div>` : `
    <div class="form-grid" style="margin-top:10px">
      <div class="field"><label>مقدار جدید ${p.unit ? "(" + p.unit + ")" : ""}</label>
        <input id="eff_value" dir="ltr" value="${typeof p.value === "object" ? "" : p.value}"
          ${typeof p.value === "object" ? 'placeholder="جدول — از config.yaml ویرایش شود" disabled' : ""}></div>
    </div>`}
    <div class="form-grid" style="margin-top:10px">
      <div class="field"><label>معتبر از تاریخ</label>
        <input id="eff_from" type="date" value="${today()}"></div>
      <div class="field" style="grid-column:1/-1"><label>توضیح</label>
        <input id="eff_note" placeholder="مثلاً: افزایش قیمت خوراک از مهر"></div>
    </div>
    <div class="row" style="margin-top:10px">
      <button class="btn primary" id="eff_save">ثبت</button>
      <span class="muted small">مقدار فعلی: ${typeof p.value === "object" ? "جدول" : p.value}</span>
    </div>
    ${(p.history || []).length ? `<h3 style="margin-top:16px">تاریخچه قیمت</h3>
      <table><thead><tr><th>از تاریخ</th><th class="num">مقدار</th><th>ثبت شده در</th>
      </tr></thead><tbody>${p.history.map(h => `<tr>
        <td class="small">${jdate(h.effective_from)}</td>
        <td class="num">${typeof h.value === "object" ? "جدول" : n0(h.value)}</td>
        <td class="tiny">${(h.changed_at || "").slice(0, 10)}</td></tr>`).join("")}
      </tbody></table>` : ""}`;
  if (isFeedTable) {
    renderFeedTableEditor(p.value);
    $("#feedRowAdd").onclick = () => {
      renderFeedTableEditor([...readFeedTableEditor(),
        { w_min: 0, w_max: 0, name: "", price: 0 }]);
    };
  }
  $("#eff_save").onclick = async () => {
    try {
      const value = isFeedTable ? readFeedTableEditor() : $("#eff_value").value;
      await api("/api/assumptions/effective", "POST", {
        key, value,
        effective_from: $("#eff_from").value, note: $("#eff_note").value });
      toast("مقدار جدید با تاریخ اعتبار ثبت شد؛ گذشته تغییر نکرد");
      closeDrawer(); await loadAssumptions(); await load();
    } catch (e) { toast("خطا: " + e.message, true); }
  };
  $("#drawer").classList.add("open"); $("#overlay").classList.add("open");
}

/* ------------------------------- ویرایشگر جدول قیمت خوراک (اصلاح ۳) */
function renderFeedTableEditor(rows) {
  $("#feedTableEditor").innerHTML = `<table><thead><tr>
      <th>از وزن (g)</th><th>تا وزن (g)</th><th>نام/سایز</th>
      <th>قیمت (تومان/kg)</th><th></th></tr></thead><tbody>` +
    rows.map((r, i) => `<tr data-row="${i}">
      <td><input class="ft-w_min" dir="ltr" value="${r.w_min}" style="width:70px"></td>
      <td><input class="ft-w_max" dir="ltr" value="${r.w_max}" style="width:70px"></td>
      <td><input class="ft-name" value="${r.name}" style="width:90px"></td>
      <td><input class="ft-price" dir="ltr" value="${r.price}" style="width:100px"></td>
      <td><button class="btn small ghost" data-ftdel="${i}">حذف</button></td>
    </tr>`).join("") + "</tbody></table>";
  $$("[data-ftdel]").forEach(b => b.onclick = () => {
    const rows2 = readFeedTableEditor();
    rows2.splice(parseInt(b.dataset.ftdel), 1);
    renderFeedTableEditor(rows2);
  });
}

function readFeedTableEditor() {
  return $$("#feedTableEditor tbody tr").map(tr => ({
    w_min: parseFloat(tr.querySelector(".ft-w_min").value) || 0,
    w_max: parseFloat(tr.querySelector(".ft-w_max").value) || 0,
    name: tr.querySelector(".ft-name").value.trim(),
    price: parseFloat(tr.querySelector(".ft-price").value) || 0,
  }));
}

function renderParam(p) {
  const id = "p_" + p.key.replace(/\./g, "_");
  let input;
  if (p.type === "table") {
    input = `<span class="pill">${p.value.length} ردیف — ویرایش از config.yaml</span>`;
  } else if (p.type === "bool") {
    input = `<select id="${id}"><option value="true" ${p.value ? "selected" : ""}>بله</option>
      <option value="false" ${!p.value ? "selected" : ""}>خیر</option></select>`;
  } else if (p.type === "choice") {
    input = `<select id="${id}">${(p.choices || []).map(c =>
      `<option value="${c}" ${String(c) === String(p.value) ? "selected" : ""}>${c}</option>`).join("")}</select>`;
  } else if (p.type === "list_float" || p.type === "list_str") {
    input = `<input id="${id}" value="${(p.value || []).join("، ")}">`;
  } else {
    input = `<input id="${id}" value="${p.value}" dir="ltr" ${p.type === "date" ? 'type="date"' : ""}>`;
  }
  return `<div class="arow ${p.overridden ? "changed" : ""}">
    <div>
      <div class="nm">${p.label_fa} ${srcBadge(p.source)}
        ${p.overridden ? '<span class="badge estimated">تغییر یافته</span>' : ""}</div>
      <div class="k">${p.key}${p.unit ? " · " + p.unit : ""}</div>
      ${p.note_fa ? `<div class="tiny muted">${p.note_fa}</div>` : ""}
      ${p.overridden ? `<div class="tiny muted">پیش‌فرض: ${p.default} · آخرین تغییر: ${jdate(p.changed_at)}</div>` : ""}
    </div>
    <div class="field">${input}</div>
    <div class="row">
      ${p.type === "table" ? "" : `<button class="btn small primary" data-save="${p.key}">ذخیره</button>`}
      ${p.overridden ? `<button class="btn small" data-reset="${p.key}">بازنشانی</button>` : ""}
      ${p.effective_dated
        ? `<button class="btn small ghost" data-eff="${p.key}">تغییر با تاریخ اعتبار</button>` : ""}
    </div>
    ${p.effective_dated ? `<div class="eff" style="grid-column:1/-1">
      <div class="tiny muted">این پارامتر مالی <b>effective-dated</b> است: مقدار جدید
        فقط از تاریخ اعتبار به بعد اعمال می‌شود و خریدها/فروش‌های قبلی با قیمت
        تاریخی خودشان می‌مانند. قیمت واقعی ثبت‌شده در تراکنش همیشه اولویت دارد.</div>
      ${(p.history || []).length ? `<table class="hist"><thead><tr><th>از تاریخ</th>
        <th class="num">مقدار</th><th>توضیح</th><th></th></tr></thead><tbody>` +
        p.history.map(h => `<tr><td class="small">${jdate(h.effective_from)}</td>
          <td class="num small">${typeof h.value === "object" ? "جدول" : n0(h.value)}</td>
          <td class="tiny">${h.note || "—"}</td>
          <td><button class="btn small ghost" data-effdel="${h.id}">حذف</button></td>
          </tr>`).join("") + "</tbody></table>"
        : '<div class="tiny muted">هنوز تغییر تاریخ‌داری ثبت نشده است.</div>'}
      </div>` : ""}
    </div>`;
}

async function saveParam(key) {
  const e = $("#p_" + key.replace(/\./g, "_"));
  if (!e) return;
  try {
    await api("/api/assumptions", "POST", { key, value: e.value });
    await loadAssumptions(); await load();
    toast("فرضیه ذخیره شد؛ تخمین‌ها بازمحاسبه شدند (داده تاریخی تغییر نکرد)");
  } catch (err) { toast("خطا: " + err.message, true); }
}

/* ================================================================= tabs */
function showTab(t) {
  TAB = t;
  $$("#tabs button[data-tab]").forEach(b => b.classList.toggle("active", b.dataset.tab === t));
  $$("main > section").forEach(s => s.classList.toggle("hidden", s.id !== "tab-" + t));
  // فرم باید همیشه ساخته شود. قبلاً به خالی بودن فهرست تراکنش‌ها گره خورده
  // بود و چون فهرست هنگام راه‌اندازی پر می‌شد، فرم هرگز ساخته نمی‌شد.
  if (t === "txns") {
    if (!$("#txnForm").children.length) buildTxnForm();
    if (!TXNS.length) loadTxns();
  }
  if (t === "offers" && !OFFERS) { buildOfferForm(); loadOffers(); }
  if (t === "assumptions" && !ASSUME) loadAssumptions();
  if (t === "plan" && !PLAN) loadPlan();
  if (t === "decide" && !DECIDE_CTX) loadDecide();
}

/* ====================================================== برنامه (مرحله ۲) */
let PLAN = null, PLAN_VARIANT = "balanced", PLAN_BUSY = false;
const VARIANT_LABELS = { max_profit: "بیشینه سود", balanced: "متعادل",
                         conservative: "محافظه‌کارانه" };

async function loadPlan(variant) {
  if (PLAN_BUSY) return;
  PLAN_BUSY = true;
  PLAN_VARIANT = variant || PLAN_VARIANT;
  $("#planMeta").innerHTML = '<span class="badge neutral">در حال حل مدل…</span>';
  try {
    PLAN = await api("/api/plan?variant=" + PLAN_VARIANT);
    renderPlan();
  } catch (e) {
    const off = /PuLP|Optimisation engine|CBC/i.test(e.message);
    const sv = e.solver || {};
    $("#planMeta").innerHTML = off
      ? `<div class="note bad"><b>موتور بهینه‌سازی در دسترس نیست</b>
         <div style="white-space:pre-wrap;margin-top:6px">${e.message}</div>
         ${sv.install_command ? `<div style="margin-top:8px">دستور نصب:
           <code class="mono" dir="ltr">${sv.install_command}</code></div>` : ""}
         <div class="tiny" style="margin-top:6px">پس از نصب، سرور را دوباره اجرا کنید.
         تا آن زمان ثبت داده، Live Farm State و بقیه تب‌ها کار می‌کنند؛ نتیجه
         heuristic به‌جای خروجی optimizer نمایش داده نمی‌شود.</div></div>`
      : `<span class="badge bad">خطا: ${e.message}</span>`;
  } finally { PLAN_BUSY = false; }
}

function renderPlan() {
  const P = PLAN, s = P.summary;
  $("#variantPicker").innerHTML = Object.keys(VARIANT_LABELS).map(v =>
    `<button class="btn small ${v === PLAN_VARIANT ? "primary" : ""}"
      data-variant="${v}">${VARIANT_LABELS[v]}</button>`).join("");
  $$("#variantPicker [data-variant]").forEach(b =>
    b.onclick = () => loadPlan(b.dataset.variant));

  $("#planMeta").innerHTML =
    `حل‌کننده: ${s.solver} · وضعیت: ${s.status} · ${s.decision_months} ماه تصمیم ·
     افق ارزیابی ${s.eval_weeks} هفته تقویمی · مبنا ${jdate(s.as_of)}` +
    (s.notes || []).map(n => `<div class="tiny" style="color:var(--warn)">${n}</div>`).join("");

  const cards = [
    { l: "خرید تخم برنامه", v: n0(s.eggs_planned), u: "عدد",
      f: `${n0(s.lots_planned)} lot در ${s.decision_months} ماه` },
    { l: "فروش برنامه", v: n0(s.fish_to_sell), u: "قطعه",
      f: Object.entries(s.sales_by_weight).sort((a, b) => a[0] - b[0])
          .map(([w, n]) => `${n1(parseFloat(w))}g: ${n0(n)}`).join(" · ") },
    { l: "درآمد برنامه", v: money(s.revenue_total), u: "",
      f: `خوراک ${money(s.feed_cost_total)} · تخم ${money(s.egg_cost_total)}` },
    { l: "نتیجه غلتان ۱۲ ماهه", v: money(s.rolling_12m.contribution_nominal), u: "",
      f: "سود عملیاتی ۱۲ ماه آینده" },
    { l: "ارزش کل چرخه عمر", v: money(s.full_lifecycle.contribution_nominal), u: "",
      f: `شامل ${n0(s.full_lifecycle.spillover_fish)} قطعه سرریز به سال بعد — سود سالانه نیست` },
    { l: "حاشیه ریسک‌تعدیل‌شده", v: money(s.contribution_risk_adjusted), u: "",
      f: "سناریوی نامساعد: تلفات، FCR و قیمت", k: s.contribution_risk_adjusted < 0 ? "bad" : "" },
    { l: "اوج نیاز استخر", v: n0(s.peak_ponds), u: `/ ${n0(s.operational_ponds)}`,
      f: `نامساعد: ${n0(s.peak_ponds_adverse)}`, k: s.pond_breach ? "bad" : "" },
    { l: "اوج نیاز تأمین مالی", v: money(s.peak_funding_requirement), u: "",
      f: `موجود ${money(s.wc_available)} · فاصله ${money(s.wc_headroom)}`,
      k: s.wc_breach ? "bad" : "" },
  ];
  $("#planKpis").innerHTML = cards.map(c => `<div class="kpi ${c.k || ""}">
    <div class="l">${c.l}</div><div class="v">${c.v}<span class="u">${c.u || ""}</span></div>
    <div class="f">${c.f || ""}</div></div>`).join("");

  $("#planFlags").innerHTML = (P.risk_flags || []).map(f =>
    `<div class="check ${f.level === "high" ? "fail" : "warn"}">
      <b>${f.title_fa}</b><div class="tiny">${f.detail_fa}</div></div>`).join("")
    || '<div class="muted small">هشدار ریسکی وجود ندارد.</div>';

  renderPlanStatus(); renderHorizons(); renderPlanCash();
  renderPlan90(); renderPlanCap(); renderPlanFx();
  renderPlanTable("#planMonthlyTable", P.monthly, "ماه");
  renderPlanTable("#planQuarterlyTable", P.quarterly, "سه‌ماهه");
  renderVariance(); renderCohortDecisions(); renderGrading();
  $("#planChecks").innerHTML = (P.validation.checks || []).map(c =>
    `<div class="check ${c.status}"><b>${c.title_fa}</b>
      <div class="tiny">${c.detail}</div></div>`).join("");
  $("#baselineInfo").textContent = P.has_original
    ? `برنامه پایه ثبت‌شده در ${jdate(P.variance.original_as_of)} (حالت ${
        VARIANT_LABELS[P.variance.original_variant] || P.variance.original_variant})`
    : "هنوز برنامه پایه‌ای ثبت نشده است.";

  renderManualMixInputs();
}

// ══════════════════════════ Manual Sales Mix Comparison (افزونه مستقل)
const MANUAL_MIX_WEIGHTS = [1, 2, 5, 10, 15];
let MANUAL_MIX_RESULT = null;

function renderManualMixInputs() {
  const byW = (PLAN && PLAN.manual_mix_target) || {};
  $("#manualMixInputs").innerHTML = MANUAL_MIX_WEIGHTS.map(w => `
    <div class="field">
      <label>هدف برداشت ${w}g <span class="tiny muted" id="mmPct${w}"></span></label>
      <input type="number" step="1000" min="0" class="mm-input" data-w="${w}"
             value="${Math.round(byW[w.toFixed(1)] || 0)}">
    </div>`).join("");
  $$(".mm-input").forEach(inp => inp.oninput = updateManualMixTotal);
  updateManualMixTotal();
}

function readManualMixInputs() {
  const out = {};
  $$(".mm-input").forEach(inp => { out[inp.dataset.w] = parseFloat(inp.value) || 0; });
  return out;
}

function updateManualMixTotal() {
  const vals = readManualMixInputs();
  const total = Object.values(vals).reduce((a, b) => a + b, 0);
  MANUAL_MIX_WEIGHTS.forEach(w => {
    const v = vals[w] || 0;
    const el = $(`#mmPct${w}`);
    if (el) el.textContent = total > 0 ? `(${(v / total * 100).toFixed(0)}٪)` : "";
  });
  $("#manualMixTotal").textContent = `مجموع ورودی: ${n0(total)} قطعه`;
}

async function runManualMix() {
  const targets = readManualMixInputs();
  const total = Object.values(targets).reduce((a, b) => a + b, 0);
  if (total <= 0) {
    $("#manualMixWarnings").innerHTML =
      '<div class="check warn"><b>ورودی خالی است</b>' +
      '<div class="tiny">حداقل یک وزن باید مقدار داشته باشد.</div></div>';
    $("#manualMixKpis").innerHTML = "";
    $("#manualMixActual").innerHTML = "";
    return;
  }
  $("#btnManualMixRun").disabled = true;
  $("#manualMixWarnings").innerHTML =
    '<div class="check warn"><b>در حال محاسبه سناریوی دستی…</b></div>';
  try {
    const r = await api("/api/plan/manual_mix", "POST",
      { variant: PLAN_VARIANT, targets });
    MANUAL_MIX_RESULT = r;
    renderManualMixResult(r);
  } catch (e) {
    $("#manualMixWarnings").innerHTML =
      `<div class="check fail"><b>خطا در محاسبه</b><div class="tiny">${e.message}</div></div>`;
    $("#manualMixKpis").innerHTML = "";
    $("#manualMixActual").innerHTML = "";
  } finally {
    $("#btnManualMixRun").disabled = false;
  }
}

function renderManualMixResult(r) {
  const warnHtml = (r.warnings || []).map(w =>
    `<div class="check fail"><b>Warning</b><div class="tiny">${w}</div></div>`).join("");
  $("#manualMixWarnings").innerHTML = warnHtml ||
    '<div class="check pass"><b>سناریوی دستی اجراشدنی است</b>' +
    '<div class="tiny">با تمام محدودیت‌های ظرفیت استخر، سقف تقاضا و سرمایه در گردش سازگار است.</div></div>';

  const rows = [
    { l: "درآمد برنامه", k: "revenue_total", fmt: money },
    { l: "نتیجه غلتان / Rolling Result", k: "rolling_12m", fmt: money },
    { l: "ارزش کل چرخه / Cycle Value", k: "full_lifecycle", fmt: money },
    { l: "حاشیه ریسک", k: "contribution_risk_adjusted", fmt: money },
    { l: "اوج نیاز استخر", k: "peak_ponds", fmt: v => `${n0(v)} / ${n0(r.operational_ponds)}` },
    { l: "اوج نیاز تأمین مالی", k: "peak_funding_requirement", fmt: money },
  ];
  $("#manualMixKpis").innerHTML = rows.map(row => {
    const m = r.manual[row.k], o = r.optimised[row.k], d = r.difference[row.k];
    const bad = row.k === "peak_ponds" ? d > 0 : (row.k === "peak_funding_requirement" ? d > 0 : d < 0);
    return `<div class="kpi ${bad ? "warn" : ""}">
      <div class="l">${row.l}</div>
      <div class="v">${row.fmt(m)}</div>
      <div class="f">Optimised: ${row.fmt(o)}<br>Difference: ${d >= 0 ? "+" : ""}${row.fmt(d)}</div>
    </div>`;
  }).join("");

  // ترکیب *واقعیِ* حاصل — مهم‌ترین بخش برای رفع سردرگمی «چرا ورودی ۱۰g و
  // ۱۵g صفر است ولی فروش در آن وزن‌ها اتفاق می‌افتد؟»
  const mw = r.manual_sales_by_weight || {}, ow = r.optimised_sales_by_weight || {};
  const allW = [...new Set([...Object.keys(mw), ...Object.keys(ow)])]
    .map(parseFloat).sort((a, b) => a - b);
  const sumV = o => Object.values(o).reduce((a, b) => a + b, 0);
  const diffCell = (a, b) => `<td class="num" style="color:${
    a - b >= 0 ? "var(--ok)" : "var(--bad)"}">${a - b >= 0 ? "+" : "−"}${
    n0(Math.abs(a - b))}</td>`;
  $("#manualMixActual").innerHTML = `
    <div class="sub" style="margin-bottom:6px"><b>ترکیب واقعی فروش حاصل</b> —
      خروجی شبیه‌سازی به تفکیک وزنی که ماهی <b>واقعاً</b> در آن فروخته می‌شود.
      این با ورودی‌های «هدف برداشت» بالا یکی نیست: ماهیِ فروش‌نرفته در
      carry-over به رشدش ادامه می‌دهد و می‌تواند در بازهٔ سنگین‌تری فروخته
      شود — به همین دلیل حتی با ورودی صفر برای ۱۰g و ۱۵g، فروش واقعی در آن
      وزن‌ها اتفاق می‌افتد.</div>
    <table><thead><tr>
      <th>وزن واقعی فروش</th><th class="num">سناریوی دستی</th>
      <th class="num">برنامه بهینه</th><th class="num">اختلاف</th>
    </tr></thead><tbody>
      ${allW.map(w => {
        const a = mw[w.toFixed(1)] || 0, b = ow[w.toFixed(1)] || 0;
        return `<tr><td>${n1(w)}g</td><td class="num">${n0(a)}</td>
          <td class="num">${n0(b)}</td>${diffCell(a, b)}</tr>`;
      }).join("")}
      <tr><td><b>مجموع</b></td><td class="num"><b>${n0(sumV(mw))}</b></td>
        <td class="num"><b>${n0(sumV(ow))}</b></td>
        ${diffCell(sumV(mw), sumV(ow))}</tr>
    </tbody></table>`;
}

function resetManualMixToOptimised() {
  renderManualMixInputs();
  MANUAL_MIX_RESULT = null;
  $("#manualMixWarnings").innerHTML = "";
  $("#manualMixKpis").innerHTML = "";
  $("#manualMixActual").innerHTML = "";
}

/* ============================================ Manual Sales Mix Comparison
   افزونه مستقل — Optimizer اصلی و معماری Plan را تغییر نمی‌دهد؛ فقط از
   /api/plan/manual_mix می‌خواند که خودش از Plan._aggregate و
   PlannedCashLedger موجود (بدون تغییر) استفاده می‌کند. */
function renderPlanStatus() {
  const ps = PLAN.plan_status;
  const prov = ps.status === "PROVISIONAL";
  $("#planMeta").insertAdjacentHTML("afterbegin",
    `<div class="note ${prov ? "bad" : "ok"}" style="margin-bottom:8px">
       <b>Plan Status: ${ps.status}</b>
       ${prov ? "— برنامه قطعی نیست." : "— برنامه قطعی است."}
       ${ps.reasons.map(r => `<div class="tiny">• ${r}</div>`).join("")}
       ${(PLAN.repair_log || []).map(r =>
         `<div class="tiny muted">↻ ${r}</div>`).join("")}
     </div>`);
}

const HZ_ROWS = [
  ["eggs_purchased", "خرید تخم", n0],
  ["fish_sold", "فروش (قطعه)", n0],
  ["revenue", "درآمد", money],
  ["egg_cost", "هزینه تخم", money],
  ["feed_cost", "هزینه خوراک", money],
  ["fixed_cost", "هزینه ثابت", money],
  ["contribution_nominal", "حاشیه اسمی", money],
  ["contribution_risk_adjusted", "حاشیه ریسک‌تعدیل", money],
];

function renderHorizons() {
  const H = PLAN.horizon;
  const draw = (sel, o, extra) => {
    $(sel).innerHTML = `<tbody>` + HZ_ROWS.map(r =>
      `<tr><td>${r[1]}</td><td class="num">${r[2](o[r[0]])}</td></tr>`).join("") +
      (extra || "") +
      `<tr><td colspan="2" class="tiny muted" style="padding-top:8px">
         ${jdate(o.from)} تا ${jdate(o.to)} · ${o.note_fa}</td></tr></tbody>`;
  };
  draw("#horizon12Table", H.rolling_12m);
  draw("#horizonLifeTable", H.full_lifecycle,
    `<tr><td>سرریز به سال بعد</td><td class="num">
       ${n0(H.full_lifecycle.spillover_fish)} قطعه ·
       ${money(H.full_lifecycle.spillover_revenue)}</td></tr>`);
}

function renderPlanCash() {
  const m = PLAN.cash.metrics, rows = PLAN.cash.by_month;
  $("#cashSub").innerHTML =
    `شرایط پرداخت مشتری: <b>${pct(m.upfront_share, 0)}</b> نقد در تاریخ فروش و
     باقیمانده پس از <b>${n0(m.balance_delay_days)} روز</b>.
     تاریخ شناسایی درآمد از تاریخ دریافت وجه جداست.<br>
     اوج نیاز تأمین مالی <b>${money(m.peak_funding_requirement)}</b>
     در ${jdate(m.peak_funding_date)} ·
     سرمایه در گردش موجود <b>${money(m.wc_available)}</b> ·
     <b style="color:${m.wc_breach ? "var(--bad)" : "var(--ok)"}">
       ${m.wc_breach ? "کسری " + money(-m.wc_headroom) : "فاصله " + money(m.wc_headroom)}</b>
     · اوج سرمایه در موجودی ${money(m.peak_inventory_capital)}
     · اوج مطالبات ${money(m.peak_receivables)}
     · چرخه تبدیل وجه نقد ${m.cash_conversion_cycle_days === null ? "—"
        : n0(m.cash_conversion_cycle_days) + " روز"}`;
  $("#planCashTable").innerHTML = `<thead><tr><th>ماه</th>
    <th class="num">درآمد شناسایی‌شده</th><th class="num">دریافت نقدی</th>
    <th class="num">شکاف درآمد و نقد</th><th class="num">پرداخت‌ها</th>
    <th class="num">خالص نقدی</th><th class="num">مانده پایان</th>
    <th class="num">اوج نیاز تأمین مالی</th></tr></thead><tbody>` +
    rows.map(b => `<tr class="${b.min_balance < 0 ? "rowbad" : ""}">
      <td class="small">${b.key}</td>
      <td class="num">${money(b.revenue_recognised)}</td>
      <td class="num">${money(b.inflow)}</td>
      <td class="num">${money(b.cash_vs_revenue_gap)}</td>
      <td class="num">${money(b.outflow)}</td>
      <td class="num">${money(b.net_cash)}</td>
      <td class="num">${money(b.closing_balance)}</td>
      <td class="num">${money(b.peak_funding)}</td></tr>`).join("") + "</tbody>";
}

const ACT_ICON = { egg_purchase: "🥚", sale: "💰", feed_purchase: "🌾" };
function renderPlan90() {
  const a = PLAN.action_plan_90d;
  $("#plan90Sub").innerHTML =
    `تا ${jdate(a.until)} · خرید ${n0(a.eggs_to_buy)} تخم · فروش ${n0(a.fish_to_sell)} قطعه ·
     خوراک ${n0(a.feed_kg)} kg · درآمد ${money(a.revenue)} ·
     اوج ${n0(a.peak_ponds)} استخر`;
  $("#plan90Table").innerHTML = `<thead><tr><th>تاریخ</th><th class="num">روز</th>
    <th>اقدام</th><th>توضیح</th></tr></thead><tbody>` +
    a.actions.map(x => `<tr>
      <td class="small">${jdate(x.date)}<div class="tiny muted">${gdate(x.date)}</div></td>
      <td class="num tiny">${x.days === 0 ? "امروز" : "+" + n0(x.days)}</td>
      <td>${ACT_ICON[x.type] || ""} ${x.title}</td>
      <td class="tiny muted">${x.detail}</td></tr>`).join("") + "</tbody>";
}

function renderPlanCap() {
  const c = PLAN.capacity_curve, op = PLAN.summary.operational_ponds;
  const svg = $("#planCapChart"), W = 760, H = 240, m = { t: 14, r: 14, b: 26, l: 40 };
  const maxY = Math.max(op * 1.25, ...c.map(x => Math.max(x.ponds, x.ponds_adverse))) || 1;
  const X = i => m.l + (W - m.l - m.r) * (i / (c.length - 1 || 1));
  const Y = v => H - m.b - (H - m.t - m.b) * (v / maxY);
  const path = key => c.map((p, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(p[key]).toFixed(1)}`).join("");
  const ticks = [0, 1, 2, 3, 4].map(k => {
    const v = maxY * k / 4;
    return `<line x1="${m.l}" y1="${Y(v)}" x2="${W - m.r}" y2="${Y(v)}" stroke="rgba(26,40,48,.08)"/>
      <text x="${m.l - 6}" y="${Y(v) + 4}" font-size="10" fill="#64748b"
        text-anchor="end">${n0(v)}</text>`;
  }).join("");
  const lab = [0, Math.floor(c.length / 3), Math.floor(2 * c.length / 3), c.length - 1]
    .map(i => `<text x="${X(i)}" y="${H - 8}" font-size="10" fill="#64748b"
      text-anchor="middle">${c[i].date.slice(0, 7)}</text>`).join("");
  svg.innerHTML = `${ticks}
    <line x1="${m.l}" y1="${Y(op)}" x2="${W - m.r}" y2="${Y(op)}"
      stroke="#b9504f" stroke-width="1.4" stroke-dasharray="6 4"/>
    <text x="${W - m.r}" y="${Y(op) - 5}" font-size="10" fill="#b9504f"
      text-anchor="end">${op} استخر عملیاتی</text>
    <path d="${path("ponds_adverse")}" fill="none" stroke="#a97a1c"
      stroke-width="1.6" stroke-dasharray="5 4"/>
    <path d="${path("ponds")}" fill="none" stroke="#3c7d90" stroke-width="2.2"/>
    ${lab}`;
}

function renderPlanFx() {
  const qs = PLAN.quarterly_fx;
  $("#planFxTable").innerHTML = `<thead><tr><th>سه‌ماهه</th>
    <th class="num">سرمایه ابتدا</th><th class="num">سود مزرعه</th>
    <th class="num">بازده مزرعه</th><th class="num">تغییر دلار</th>
    <th class="num">مازاد بر بنچمارک</th></tr></thead><tbody>` +
    qs.map(q => `<tr>
      <td class="small">${q.label}
        <div class="tiny muted">${q.return_label_en || ""}</div></td>
      <td class="num">${money(q.beginning_capital)}</td>
      <td class="num">${money(q.farm_gain)}</td>
      <td class="num">${q.farm_return === null || q.farm_return === undefined
        ? "—" : pct(q.farm_return)}</td>
      <td class="num">${q.fx_available ? pct(q.fx_return)
        : '<span class="badge estimated">موجود نیست</span>'}</td>
      <td class="num">${q.fx_available
        ? `<b style="color:${q.excess_over_fx >= 0 ? "var(--ok)" : "var(--bad)"}">
             ${money(q.excess_over_fx)}</b>` : "—"}</td></tr>`).join("") + "</tbody>";
}

const PLAN_COLS = [
  ["eggs_purchased", "خرید تخم", n0],
  ["expected_survival", "بقای مورد انتظار", v => v === null ? "—" : pct(v)],
  ["harvest_fish", "فروش (قطعه)", n0],
  ["revenue", "درآمد", money],
  ["feed_purchase_kg", "خوراک (kg)", n0],
  ["feed_purchase_cost", "هزینه خوراک", money],
  ["peak_ponds", "اوج استخر", n0],
  ["pond_utilisation", "بهره‌برداری", v => pct(v)],
  ["peak_capital", "سرمایه در گردش", money],
  ["contribution_nominal", "حاشیه اسمی", money],
  ["contribution_risk_adjusted", "حاشیه ریسک‌تعدیل", money],
];

function renderPlanTable(sel, rows, head) {
  $(sel).innerHTML = `<thead><tr><th>${head}</th>` +
    PLAN_COLS.map(c => `<th class="num">${c[1]}</th>`).join("") +
    `<th>فروش به تفکیک وزن</th></tr></thead><tbody>` +
    rows.map(b => `<tr class="${b.pond_headroom < 0 ? "rowbad" : ""}">
      <td class="small">${b.key}</td>` +
      PLAN_COLS.map(c => `<td class="num">${c[2](b[c[0]])}</td>`).join("") +
      `<td class="tiny">${Object.entries(b.sales_by_weight || {})
        .sort((x, y) => x[0] - y[0])
        .map(([w, n]) => `${n1(parseFloat(w))}g: ${n0(n)}`).join(" · ") || "—"}</td>
      </tr>`).join("") + "</tbody>";
}

function renderVariance() {
  const V = PLAN.variance;
  const fields = V.fields;
  $("#varianceTable").innerHTML = `<thead><tr><th>ماه</th><th>شاخص</th>
    <th class="num">برنامه پایه</th><th class="num">برنامه فعلی</th>
    <th class="num">واقعی</th><th class="num">انحراف</th></tr></thead><tbody>` +
    V.months.filter(m => m.elapsed).map(m => fields.map((f, i) => {
      const d = m.fields[f.key];
      const showVar = d.variance !== null && d.variance !== undefined;
      return `<tr class="${m.is_current ? "rowcur" : ""}">
        ${i === 0 ? `<td class="small" rowspan="${fields.length}">${m.key}
          ${m.is_current ? '<div class="badge neutral">ماه جاری</div>' : ""}</td>` : ""}
        <td class="tiny">${f.label_fa}</td>
        <td class="num tiny">${d.original_plan === null || d.original_plan === undefined
          ? "—" : (f.unit === "تومان" ? money(d.original_plan) : n0(d.original_plan))}</td>
        <td class="num tiny">${d.current_plan === null || d.current_plan === undefined
          ? "—" : (f.unit === "تومان" ? money(d.current_plan) : n0(d.current_plan))}</td>
        <td class="num tiny"><b>${d.actual === null || d.actual === undefined
          ? "—" : (f.unit === "تومان" ? money(d.actual) : n0(d.actual))}</b></td>
        <td class="num tiny">${showVar
          ? `<span style="color:${d.variance >= 0 ? "var(--ok)" : "var(--bad)"}">
             ${f.unit === "تومان" ? money(d.variance) : n0(d.variance)}
             ${d.variance_pct !== null && d.variance_pct !== undefined
               ? ` (${pct(d.variance_pct)})` : ""}</span>` : "—"}</td>
      </tr>`;
    }).join("")).join("") +
    `<tr class="rowtotal"><td><b>جمع تا امروز</b></td><td colspan="5"></td></tr>` +
    fields.map(f => {
      const d = V.totals_to_date[f.key];
      const fm = v => v === null || v === undefined ? "—"
        : (f.unit === "تومان" ? money(v) : n0(v));
      return `<tr class="rowtotal"><td></td><td class="tiny">${f.label_fa}</td>
        <td class="num tiny">${fm(d.original_plan)}</td>
        <td class="num tiny">${fm(d.current_plan)}</td>
        <td class="num tiny"><b>${fm(d.actual)}</b></td>
        <td class="num tiny">${fm(d.variance)}</td></tr>`;
    }).join("") + "</tbody>";
}

function renderCohortDecisions() {
  $("#cohortDecisionTable").innerHTML = `<thead><tr><th>Cohort</th>
    <th class="num">زنده</th><th class="num">وزن فعلی</th><th>تصمیم برداشت</th>
    </tr></thead><tbody>` +
    PLAN.cohort_decisions.map(c => `<tr>
      <td class="small">${c.cohort_id}${c.partial_harvest
        ? ' <span class="badge estimated">برداشت جزئی</span>' : ""}</td>
      <td class="num">${n0(c.alive)}</td>
      <td class="num">${n1(c.mean_weight_g)} g</td>
      <td class="tiny">${c.split.map(r =>
        `${pct(r.fraction, 0)} در ${n1(r.harvest_w)} گرم` +
        (r.first_harvest ? ` — از ${jdate(r.first_harvest)}` : "")).join("<br>")}</td>
      </tr>`).join("") + "</tbody>";
}

function renderGrading() {
  const g = PLAN.grading;
  $("#gradingTable").innerHTML = g.length ? `<thead><tr><th>Cohort</th>
    <th class="num">وزن هدف</th><th>موج‌های برداشت</th><th class="num">پراکندگی</th>
    </tr></thead><tbody>` +
    g.map(r => `<tr>
      <td class="small">${r.cohort_id}</td>
      <td class="num">${n1(r.harvest_w)} g</td>
      <td class="tiny">${r.waves.map(w =>
        `${jdate(w.date)}: ${n0(w.fish)} قطعه`).join("<br>")}</td>
      <td class="num">${n0(r.spread_days)} روز
        ${r.grading_needed ? '<div class="badge estimated">grading</div>' : ""}</td>
      </tr>`).join("") + "</tbody>"
    : '<tbody><tr><td class="muted small">موردی برای نمایش نیست.</td></tr></tbody>';
}

async function loadVariantComparison() {
  $("#variantTable").innerHTML = '<tbody><tr><td class="muted small">در حال حل سه مدل…</td></tr></tbody>';
  const r = await api("/api/plan/variants");
  $("#variantTable").innerHTML = `<thead><tr><th>حالت</th><th class="num">تخم</th>
    <th class="num">فروش</th><th class="num">حاشیه اسمی</th>
    <th class="num">حاشیه نامساعد</th><th class="num">اوج استخر</th>
    <th class="num">سرمایه</th></tr></thead><tbody>` +
    r.variants.map(v => `<tr class="${v.variant === PLAN_VARIANT ? "rowcur" : ""}">
      <td class="small">${v.label_fa}</td>
      <td class="num">${n0(v.eggs_planned)}</td>
      <td class="num">${n0(v.fish_to_sell)}</td>
      <td class="num">${money(v.contribution_nominal)}</td>
      <td class="num">${money(v.contribution_risk_adjusted)}</td>
      <td class="num">${n0(v.peak_ponds)} / ${n0(v.peak_ponds_adverse)}</td>
      <td class="num">${money(v.peak_capital)}</td></tr>`).join("") + "</tbody>";
}

async function loadScenarioComparison() {
  $("#scenarioTable").innerHTML = '<tbody><tr><td class="muted small">در حال حل سناریوها…</td></tr></tbody>';
  const r = await api("/api/plan/scenarios?variant=" + PLAN_VARIANT);
  $("#scenarioTable").innerHTML = `<thead><tr><th>سناریو</th>
    <th class="num">خرید عملی</th><th class="num">فروش</th>
    <th class="num">حاشیه اسمی</th><th class="num">اوج استخر</th>
    <th>محدودکننده</th></tr></thead><tbody>` +
    r.scenarios.map(s => `<tr>
      <td class="small">${n0(s.scenario)}</td>
      <td class="num">${n0(s.eggs_planned)}</td>
      <td class="num">${n0(s.fish_to_sell)}</td>
      <td class="num">${money(s.contribution_nominal)}</td>
      <td class="num">${n0(s.peak_ponds)}</td>
      <td class="tiny">${s.binding
        ? '<span class="badge estimated">عرضه/ظرفیت محدود می‌کند</span>'
        : '<span class="badge neutral">سقف سناریو</span>'}</td></tr>`).join("") +
    "</tbody>";
}

/* ============================= موتور تصمیم (مرحله ۳) ============= */
let DECIDE_CTX = null;

async function loadDecide() {
  try { DECIDE_CTX = await api("/api/decide/context"); }
  catch (e) { $("#decideContext").innerHTML =
    `<div class="note bad">${e.message}</div>`; return; }
  const c = DECIDE_CTX;
  $("#decideContext").innerHTML = [
    { l: "cohortهای فعال", v: n0(c.cohorts.length), f:
      c.cohorts.map(x => `${x.cohort_id}: ${n0(x.alive)} @ ${n2(x.mean_weight_g)}g`)
        .join(" · ") },
    { l: "استخر در استفاده", v: `${n0(c.ponds_used)}`, u: `/ ${n0(c.operational_ponds)}` },
    { l: "موجودی خوراک", v: n0(c.feed_inventory_kg), u: "kg",
      f: money(c.feed_inventory_value) },
    { l: "مانده نقدی", v: money(c.cash_balance),
      f: `سرمایه در گردش موجود ${money(c.wc_available)}`,
      k: c.cash_balance < 0 ? "bad" : "" },
    { l: "وضعیت برنامه", v: c.reconciliation.plan_status,
      f: c.reconciliation.reconciled ? "موجودی reconcile شده"
        : `${n0(c.reconciliation.unallocated_fish)} قطعه تخصیص‌نیافته`,
      k: c.reconciliation.reconciled ? "" : "bad" },
  ].map(x => `<div class="kpi ${x.k || ""}"><div class="l">${x.l}</div>
    <div class="v">${x.v}<span class="u">${x.u || ""}</span></div>
    <div class="f">${x.f || ""}</div></div>`).join("");

  $("#so_cohort").innerHTML =
    `<option value="">خودکار (چند cohort بر اساس وزن)</option>` +
    c.cohorts.map(x =>
    `<option value="${x.cohort_id}">${x.cohort_id} — ${n0(x.alive)} قطعه @ ${
      n2(x.mean_weight_g)}g</option>`).join("");
  if (!$("#eo_date").value) $("#eo_date").value = today();
  if (!$("#so_deliver").value) $("#so_deliver").value = today();
  if (!$("#wifList").children.length) addWifRow();
}

const DEC_BADGE = {
  BUY: '<span class="badge ok">بخر</span>',
  PARTIAL_BUY: '<span class="badge estimated">خرید جزئی</span>',
  REJECT: '<span class="badge bad">رد کن</span>',
  ACCEPT: '<span class="badge ok">بپذیر</span>',
  NEGOTIATE: '<span class="badge estimated">مذاکره کن</span>',
};
const CONF_FA = { high: "بالا", medium: "متوسط", low: "پایین" };

async function decideEgg() {
  const q = parseFloat($("#eo_qty").value), p = parseFloat($("#eo_price").value);
  if (!q || !p) { toast("تعداد و قیمت الزامی‌اند", true); return; }
  const terms = {};
  if ($("#eo_upfront").value !== "") terms.upfront_share = parseFloat($("#eo_upfront").value);
  if ($("#eo_credit").value !== "") terms.delay_days = parseFloat($("#eo_credit").value);
  const btn = $("#btnEggDecide"); btn.disabled = true; btn.textContent = "در حال حل مدل…";
  $("#eggResult").innerHTML = '<div class="note">مدل با و بدون این آفر حل می‌شود…</div>';
  try {
    const r = await api("/api/decide/egg", "POST", {
      date: $("#eo_date").value, quantity: q, price: p,
      supplier: $("#eo_supplier").value, expiry: $("#eo_expiry").value,
      quality: $("#eo_quality").value,
      payment_terms: Object.keys(terms).length ? terms : undefined,
      partial_options: $("#eo_partial").checked });
    const f = r.full_accept;
    const opts = [f, ...r.options].sort((a, b) => b.incremental_profit - a.incremental_profit);
    const feasLine = f.feasible
      ? '<span class="badge ok">برنامه حاصل اجراشدنی است</span>'
      : `<span class="badge bad">اجراشدنی نیست</span> <span class="tiny">${
          f.infeasible_reason_fa || ""}</span>`;
    const payLine = r.payment && r.payment.present_value_factor < 0.999
      ? `<tr><td>شرایط پرداخت تأمین‌کننده</td><td class="num">${
          pct(r.payment.upfront_share, 0)} نقد، بقیه +${n0(r.payment.delay_days)} روز
          — ارزش امروز هر تخم ${n0(p * r.payment.present_value_factor)} تومان</td></tr>`
      : "";
    $("#eggResult").innerHTML = `
      <div class="note ${r.decision === "REJECT" ? "bad" : "ok"}" style="margin-top:12px">
        <div style="font-size:16px">${DEC_BADGE[r.decision]}
          <b>${r.decision_fa}</b> · اطمینان ${CONF_FA[r.confidence] || r.confidence}
          · ${feasLine}</div>
        <div style="margin-top:6px">${r.explanation_fa}</div></div>
      <table style="margin-top:10px"><tbody>
        ${payLine}
        <tr><td>حداکثر قیمت توجیه‌پذیر</td>
          <td class="num"><b>${n0(r.max_justified_price)} تومان/تخم</b></td></tr>
        <tr><td>قیمت پیشنهادی</td><td class="num">${n0(p)} تومان</td></tr>
        <tr><td>حاشیه هر تخم</td><td class="num" style="color:${
          f.margin_per_egg >= 0 ? "var(--ok)" : "var(--bad)"}">
          ${n0(f.margin_per_egg)} تومان</td></tr>
        <tr><td>تعداد پیشنهادی خرید</td><td class="num">${n0(r.preferred_quantity)}</td></tr>
        <tr><td>اثر بر سود</td><td class="num">${money(r.expected_profit_impact)}</td></tr>
        <tr><td>اثر بر سود در سناریوی نامساعد</td>
          <td class="num">${money(f.incremental_profit_adverse)}</td></tr>
        <tr><td>بقای مورد انتظار</td><td class="num">${pct(f.expected_survival)}</td></tr>
        <tr><td>وزن برداشت بهینه این lot</td>
          <td class="num">${f.harvest_weight ? n1(f.harvest_weight) + " گرم" : "—"}</td></tr>
        <tr><td>خوراک اضافه</td><td class="num">${n0(f.feed_kg_delta)} kg ·
          ${money(f.feed_cost_delta)}</td></tr>
        <tr><td>اثر بر اوج استخر</td><td class="num">${n0(f.peak_ponds_before)} →
          ${n0(f.peak_ponds_after)} <span class="badge ${
            f.capacity_risk === "high" ? "bad" : f.capacity_risk === "medium"
            ? "estimated" : "ok"}">ریسک ظرفیت ${
            { high: "بالا", medium: "متوسط", low: "پایین" }[f.capacity_risk]}</span></td></tr>
        <tr><td>اثر بر اوج نیاز نقدی</td><td class="num">${money(f.peak_funding_before)} →
          ${money(f.peak_funding_after)} (فاصله تا سقف ${money(f.wc_headroom_after)})</td></tr>
      </tbody></table>
      <h3 style="margin-top:14px">اثر بر ظرفیت در بازه‌های آینده</h3>
      <table><thead><tr><th>افق</th><th class="num">بدون آفر</th>
        <th class="num">با آفر</th><th class="num">تغییر</th></tr></thead><tbody>
        ${f.capacity_curve_delta.map(x => `<tr><td>${x.days} روز (${jdate(x.date)})</td>
          <td class="num">${n0(x.ponds_before)}</td><td class="num">${n0(x.ponds_after)}</td>
          <td class="num">${x.delta > 0 ? "+" : ""}${n0(x.delta)}</td></tr>`).join("")}
      </tbody></table>
      ${opts.length > 1 ? `<h3 style="margin-top:14px">مقایسه تعدادها</h3>
        <table><thead><tr><th class="num">تعداد</th><th class="num">ارزش افزوده</th>
        <th class="num">حاشیه هر تخم</th><th class="num">استخر اضافه</th>
        <th>اجراشدنی</th>
        </tr></thead><tbody>${opts.map(o => `<tr class="${
          o.quantity === r.preferred_quantity ? "rowcur" : ""}">
          <td class="num">${n0(o.quantity)}</td>
          <td class="num">${money(o.incremental_profit)}</td>
          <td class="num">${n0(o.margin_per_egg)}</td>
          <td class="num">${n0(o.extra_ponds)}</td>
          <td>${o.feasible ? '<span class="badge ok">بله</span>'
            : '<span class="badge bad">خیر</span>'}</td></tr>`).join("")}</tbody></table>` : ""}`;
  } catch (e) {
    $("#eggResult").innerHTML = `<div class="note bad">${e.message}</div>`;
  } finally { btn.disabled = false; btn.textContent = "ارزیابی آفر"; }
}

/* ---- تخصیص چند-cohort (اصلاح ۴) ---- */
let SALE_ALLOC = null;   // آخرین تخصیص (قابل ویرایش توسط کاربر)

function readAllocEdits() {
  const rows = [...document.querySelectorAll("#soAlloc [data-alloc]")];
  if (!rows.length) return null;
  return rows.map(r => ({
    cohort_id: r.dataset.alloc,
    quantity: parseFloat(r.querySelector("input").value) || 0,
  })).filter(a => a.quantity > 0);
}

function renderAlloc(info) {
  SALE_ALLOC = info;
  const total = info.allocations.reduce((s, a) => s + a.quantity, 0);
  $("#soAlloc").innerHTML = `
    <div class="note" style="margin-top:6px">
      <b>تخصیص پیشنهادی بین cohortها</b>
      <span class="tiny muted">— قابل ویرایش؛ مجموع باید با تعداد آفر برابر بماند.</span>
      <table style="margin-top:6px"><thead><tr><th>Cohort</th>
        <th class="num">وزن متوسط</th><th class="num">در بازه وزن</th>
        <th class="num">قابل تأمین</th><th class="num">تخصیص</th></tr></thead><tbody>
      ${info.allocations.map(a => {
        const c = (info.candidates || []).find(x => x.cohort_id === a.cohort_id) || {};
        return `<tr data-alloc="${a.cohort_id}">
          <td>${a.cohort_id}</td>
          <td class="num">${n2(c.mean_weight_g ?? a.mean_weight_g ?? 0)} g</td>
          <td class="num">${pct(c.fraction_in_window ?? 1)}</td>
          <td class="num">${n0(c.available ?? 0)}</td>
          <td class="num"><input type="number" step="1000" value="${Math.round(a.quantity)}"
            style="max-width:120px"></td></tr>`; }).join("")}
      </tbody></table>
      <div class="tiny muted" style="margin-top:4px">مجموع فعلی: ${n0(total)}
        از ${n0(info.requested)}${info.shortfall > 0.5
          ? ` · <span style="color:var(--bad)">کسری ${n0(info.shortfall)}</span>` : ""}</div>
    </div>`;
}

async function suggestSaleAlloc() {
  const q = parseFloat($("#so_qty").value), w = parseFloat($("#so_weight").value);
  if (!q) { toast("تعداد را وارد کنید", true); return; }
  if ($("#so_cohort").value) { toast("برای تخصیص خودکار، Cohort را روی «خودکار» بگذارید", true); return; }
  if (!w) { toast("برای تخصیص خودکار، وزن درخواستی لازم است", true); return; }
  try {
    const r = await api("/api/decide/sale/allocation", "POST", {
      quantity: q, weight_g: w, delivery_date: $("#so_deliver").value || undefined });
    if (!r.allocations.length) {
      $("#soAlloc").innerHTML = `<div class="note bad">هیچ cohort واجد شرایطی در
        وزن حدود ${n1(w)} گرم یافت نشد.</div>`;
      SALE_ALLOC = null; return;
    }
    renderAlloc(r);
  } catch (e) { toast("خطا: " + e.message, true); }
}

async function decideSale() {
  const q = parseFloat($("#so_qty").value), p = parseFloat($("#so_price").value);
  if (!q || !p) { toast("تعداد و قیمت الزامی‌اند", true); return; }
  const terms = {};
  if ($("#so_upfront").value !== "") terms.upfront_share = parseFloat($("#so_upfront").value);
  if ($("#so_delay").value !== "") terms.delay_days = parseFloat($("#so_delay").value);
  const body = {
    quantity: q, price: p,
    weight_g: $("#so_weight").value ? parseFloat($("#so_weight").value) : undefined,
    delivery_date: $("#so_deliver").value || undefined,
    payment_terms: Object.keys(terms).length ? terms : undefined };
  const cid = $("#so_cohort").value;
  const edited = readAllocEdits();
  if (cid) body.cohort_id = cid;
  else if (edited && edited.length) body.allocations = edited;
  else if (!body.weight_g) { toast("در حالت خودکار، وزن درخواستی لازم است", true); return; }

  const btn = $("#btnSaleDecide"); btn.disabled = true;
  btn.textContent = "در حال بهینه‌سازی دو سناریو…";
  $("#saleResult").innerHTML = `<div class="note">مدل دو بار حل می‌شود
    («بدون فروش» و «با فروش روی نسخه کپی») و سپس قیمت بی‌تفاوتی با چند اجرای
    دیگر پیدا می‌شود…</div>`;
  try {
    const r = await api("/api/decide/sale", "POST", body);
    const P = r.prices, alt = r.alternative, sc = r.scenarios,
          wc = r.working_capital, fs = r.floor_search, al = r.allocation;
    if (al && al.source === "auto") renderAlloc({
      allocations: al.detail, candidates: al.candidates,
      requested: q, shortfall: 0 });
    $("#saleResult").innerHTML = `
      <div class="note ${r.decision === "REJECT" ? "bad" : "ok"}" style="margin-top:12px">
        <div style="font-size:16px">${DEC_BADGE[r.decision]} <b>${r.decision_fa}</b></div>
        <div style="margin-top:6px">${r.explanation_fa}</div></div>
      ${al && al.multi_cohort ? `<div class="note" style="margin-top:8px">
        <b>تأمین از چند cohort:</b> ${al.detail.map(a =>
          `${a.cohort_id} → ${n0(a.quantity)} قطعه @ ${n2(a.mean_weight_g)}g`)
          .join(" · ")}
        <div class="tiny muted">${al.note_fa || ""}</div></div>` : ""}
      <h3 style="margin-top:12px">سه قیمت مرجع</h3>
      <table class="wc"><tbody>
        <tr class="wc-a"><td><b>کف حسابداری</b>
          <div class="tiny muted">بهای تمام‌شده تاریخی هر ماهی — زیر آن، زیان دفتری</div></td>
          <td class="num"><b>${n0(P.accounting_floor)}</b></td></tr>
        <tr class="wc-b"><td><b>کف تصمیم اقتصادی</b>
          <div class="tiny muted">قیمتی که «فروش حالا» و «بهترین برنامه بدون فروش»
            برابر می‌شوند — از بهینه‌سازی دوباره کل مزرعه (${n0(fs.solver_runs)} اجرای
            optimizer)، نه فرمول بهای تمام‌شده</div></td>
          <td class="num"><b>${n0(P.economic_floor)}</b></td></tr>
        <tr class="wc-c"><td><b>قیمت پیشنهادی متقابل</b>
          <div class="tiny muted">کف اقتصادی به‌علاوه حاشیه چانه‌زنی</div></td>
          <td class="num"><b>${n0(P.counter_price)}</b></td></tr>
      </tbody></table>
      <h3 style="margin-top:12px">مقایسه دو سناریو (ارزش امروز)</h3>
      <table><thead><tr><th></th><th class="num">بدون فروش</th>
        <th class="num">با فروش</th><th class="num">تفاوت</th></tr></thead><tbody>
        <tr><td>ارزش امروز خالص (NPV)</td>
          <td class="num">${money(sc.keep.npv)}</td>
          <td class="num">${money(sc.accept.npv)}</td>
          <td class="num" style="color:${r.difference_vs_keeping >= 0
            ? "var(--ok)" : "var(--bad)"}"><b>${money(r.difference_vs_keeping)}</b></td></tr>
        <tr><td>اوج نیاز استخر برنامه</td>
          <td class="num">${n0(sc.keep.peak_ponds)}</td>
          <td class="num">${n0(sc.accept.peak_ponds)}</td>
          <td class="num">${n0(sc.accept.peak_ponds - sc.keep.peak_ponds)}</td></tr>
        <tr><td>اوج نیاز نقدی</td>
          <td class="num">${money(wc.peak_funding_before)}</td>
          <td class="num">${money(wc.peak_funding_after)}</td>
          <td class="num">${money(wc.peak_funding_delta)}</td></tr>
        <tr><td>هزینه خوراک برنامه</td>
          <td class="num">${money(sc.keep.feed_cost)}</td>
          <td class="num">${money(sc.accept.feed_cost)}</td>
          <td class="num">${money(-r.feed_cost_avoided)}</td></tr>
        <tr><td>حاشیه مورد انتظار تصمیم
          <div class="tiny muted">تغییر حاشیه برنامه + عایدی خود این فروش
            (${money(r.margin.transaction_proceeds)}). فروش‌های آینده برنامه با
            همان منطق بازار ارزش‌گذاری شده‌اند (احتمال فروش، تخفیف تلاش‌های
            بعدی، سقف تقاضا، carry-over) — نه با قیمت تعرفه کامل.
            <br>فقط برنامه (تشخیصی): ${money(r.margin.plan_only)}</div></td>
          <td class="num">—</td><td class="num">—</td>
          <td class="num" style="color:${r.margin.expected >= 0
            ? "var(--ok)" : "var(--bad)"}"><b>${money(r.margin.expected)}</b></td></tr>
      </tbody></table>
      <table style="margin-top:10px"><tbody>
        <tr><td>قیمت پیشنهادی خریدار</td><td class="num">${n0(P.offered)}</td></tr>
        <tr><td>ارزش امروز پس از شرایط پرداخت</td>
          <td class="num">${n0(P.effective_after_terms)}
          <span class="tiny muted">(${pct(r.payment.upfront_share, 0)} نقد،
            بقیه +${n0(r.payment.delay_days)} روز — در خودِ کف اقتصادی لحاظ شده)</span></td></tr>
        <tr><td>قیمت منحنی پایه در این وزن</td><td class="num">${n0(P.baseline_curve)}</td></tr>
        <tr><td>استخر واقعاً آزادشده (نیاز صحیح امروز)</td>
          <td class="num">${n0(r.ponds_freed)}
          <span class="tiny muted">(${n0(r.ponds_before)} ← ${n0(r.ponds_after)})</span></td></tr>
        <tr><td>سرمایه آزادشده از موجودی</td>
          <td class="num">${money(r.working_capital_released)}</td></tr>
      </tbody></table>
      <h3 style="margin-top:14px">نمای تشخیصی: نگه‌داشتن تا وزن‌های بالاتر</h3>
      <div class="tiny muted">${alt.note_fa || ""}</div>
      <table><thead><tr><th class="num">وزن هدف</th><th class="num">روز تا رسیدن</th>
        <th class="num">بقا</th><th class="num">خوراک هر ماهی</th>
        <th class="num">احتمال مشتری</th>
        <th class="num">ارزش امروز هر ماهی</th></tr></thead><tbody>
        ${alt.options.sort((a, b) => b.value_per_fish - a.value_per_fish)
          .map(o => `<tr class="${o.harvest_w === alt.harvest_w ? "rowcur" : ""}">
          <td class="num">${n1(o.harvest_w)} g${o.is_sell_now
            ? ' <span class="badge neutral">فروش امروز</span>' : ""}</td>
          <td class="num">${n0(o.days_to_reach)}</td>
          <td class="num">${pct(o.survival)}</td>
          <td class="num">${n0(o.feed_cost_per_fish)}</td>
          <td class="num">${pct(o.saleability ?? 1)}</td>
          <td class="num"><b>${n0(o.value_per_fish)}</b></td></tr>`).join("")}
      </tbody></table>`;
  } catch (e) {
    $("#saleResult").innerHTML = `<div class="note bad">${e.message}</div>`;
  } finally { btn.disabled = false; btn.textContent = "ارزیابی آفر فروش"; }
}

const WIF_TYPES = {
  buy_eggs: "خرید تخم", sell_fish: "فروش ماهی",
  feed_price_pct: "تغییر درصدی قیمت خوراک",
  mortality_pct: "تغییر درصدی تلفات",
  sale_price_pct: "تغییر درصدی قیمت فروش",
};

function addWifRow() {
  $("#wifList").insertAdjacentHTML("beforeend",
    `<div class="row" data-wif style="margin-bottom:6px;flex-wrap:wrap">
      <select class="wif_type">${Object.entries(WIF_TYPES).map(([k, v]) =>
        `<option value="${k}">${v}</option>`).join("")}</select>
      <select class="wif_cohort" style="display:none">
        <option value="">خودکار (چند cohort)</option>${
        (DECIDE_CTX ? DECIDE_CTX.cohorts : []).map(c =>
          `<option>${c.cohort_id}</option>`).join("")}</select>
      <input class="wif_w" type="number" step="any" placeholder="وزن (g)"
        style="max-width:110px;display:none">
      <input class="wif_a" type="number" step="any" placeholder="تعداد" style="max-width:150px">
      <input class="wif_b" type="number" step="any" placeholder="قیمت" style="max-width:150px">
      <input class="wif_up" type="number" step="0.05" placeholder="سهم نقدی (۰٫۵)"
        style="max-width:130px;display:none">
      <input class="wif_dl" type="number" step="1" placeholder="تأخیر (روز)"
        style="max-width:110px;display:none">
      <button class="btn small ghost wif_del">حذف</button></div>`);
  wireWif();
}

function wireWif() {
  $$("[data-wif]").forEach(row => {
    const t = row.querySelector(".wif_type");
    const sync = () => {
      const v = t.value;
      const isPct = v.endsWith("_pct");
      const sell = v === "sell_fish";
      const coh = row.querySelector(".wif_cohort");
      coh.style.display = sell ? "" : "none";
      row.querySelector(".wif_w").style.display =
        (sell && !coh.value) ? "" : "none";
      row.querySelector(".wif_a").placeholder = isPct ? "درصد ±" : "تعداد";
      row.querySelector(".wif_b").style.display = isPct ? "none" : "";
      // شرایط پرداخت فقط برای فروش معنا دارد؛ برای برابری با موتور تصمیم
      // (Sale Offer Evaluator) باید اینجا هم قابل تنظیم باشد، وگرنه
      // به‌صورت خاموش روی پیش‌فرض ۵۰٪/۴۵روز می‌افتد و دو نتیجه با هم
      // یکی نمی‌شوند حتی اگر بقیه ورودی‌ها یکسان باشند.
      row.querySelector(".wif_up").style.display = sell ? "" : "none";
      row.querySelector(".wif_dl").style.display = sell ? "" : "none";
    };
    t.onchange = sync;
    row.querySelector(".wif_cohort").onchange = sync;
    sync();
    row.querySelector(".wif_del").onclick = () => row.remove();
  });
}

async function runWhatIf() {
  const changes = [...document.querySelectorAll("[data-wif]")].map(row => {
    const type = row.querySelector(".wif_type").value;
    const a = parseFloat(row.querySelector(".wif_a").value);
    const b = parseFloat(row.querySelector(".wif_b").value);
    if (type.endsWith("_pct")) return isNaN(a) ? null : { type, value: a };
    if (type === "buy_eggs") return isNaN(a) ? null
      : { type, quantity: a, price: isNaN(b) ? 0 : b };
    if (isNaN(a)) return null;
    const cid = row.querySelector(".wif_cohort").value;
    const w = parseFloat(row.querySelector(".wif_w").value);
    const ch = { type, quantity: a, price: isNaN(b) ? 0 : b };
    if (cid) ch.cohort_id = cid;
    else if (!isNaN(w)) ch.weight_g = w;
    else { toast("برای فروش خودکار، وزن لازم است", true); return null; }
    const up = parseFloat(row.querySelector(".wif_up").value);
    const dl = parseFloat(row.querySelector(".wif_dl").value);
    const terms = {};
    if (!isNaN(up)) terms.upfront_share = up;
    if (!isNaN(dl)) terms.delay_days = dl;
    if (Object.keys(terms).length) ch.payment_terms = terms;
    return ch;
  }).filter(Boolean);
  if (!changes.length) { toast("حداقل یک تغییر وارد کنید", true); return; }
  const btn = $("#btnWifRun"); btn.disabled = true; btn.textContent = "در حال اجرا…";
  try {
    const r = await api("/api/decide/what-if", "POST", { changes });
    const d = r.delta, sd = r.state_delta || {};
    const row = (l, v, money_ = true) => `<tr><td>${l}</td>
      <td class="num" style="color:${v >= 0 ? "var(--ok)" : "var(--bad)"}">
        ${money_ ? money(v) : n0(v)}</td></tr>`;
    $("#wifResult").innerHTML = `
      <div class="note" style="margin-top:12px"><b>${r.verdict_fa}</b>
        <div class="tiny">${r.changes_fa.join(" · ")}</div></div>
      <table style="margin-top:8px"><tbody>
        ${row("تغییر ارزش امروز خالص (NPV)", d.npv)}
        ${row("تغییر حاشیه مورد انتظار تصمیم", d.margin_expected)}
        <tr><td class="tiny muted" colspan="2" style="padding:2px 0 6px">
          شامل عایدی خود تراکنش (${money(d.transaction_proceeds || 0)}) ‌+ تغییر
          حاشیه برنامه. حاشیه برنامه از همان منطق مشترک بازار می‌آید:
          احتمال فروش، تخفیف تلاش‌های بعدی، سقف تقاضا و carry-over.</td></tr>
        ${row("— فقط تغییر حاشیه برنامه (تشخیصی)", d.margin_plan_only)}
        ${row("تغییر حاشیه در سناریوی نامساعد", d.contribution_adverse)}
        ${row("تغییر نتیجه ۱۲ ماهه", d.rolling_12m)}
        ${row("تغییر درآمد برنامه", d.revenue)}
        ${row("تغییر هزینه خوراک", -d.feed_cost)}
        ${row("تغییر اوج استخر", -d.peak_ponds, false)}
        ${row("تغییر اوج نیاز نقدی", -d.peak_funding)}
        ${row("تغییر تعداد فروش برنامه", d.fish_sold, false)}
      </tbody></table>
      ${sd.live_fish_delta !== undefined && Math.abs(sd.live_fish_delta) > 0.5 ? `
      <table style="margin-top:8px"><tbody>
        <tr><td>موجودی زنده در سناریو</td>
          <td class="num">${n0(sd.live_fish_before)} ← ${n0(sd.live_fish_after)}
          (${n0(sd.live_fish_delta)})</td></tr>
        <tr><td>نیاز استخر امروز (صحیح)</td>
          <td class="num">${n0(sd.ponds_now_before)} ← ${n0(sd.ponds_now_after)}</td></tr>
      </tbody></table>` : ""}
      <div class="tiny muted" style="margin-top:6px">${r.note_fa ||
        "هیچ داده واقعی تغییر نکرد؛ سناریو روی نسخه کپی وضعیت اجرا و برنامه دوباره بهینه شد."}</div>`;
  } catch (e) {
    $("#wifResult").innerHTML = `<div class="note bad">${e.message}</div>`;
  } finally { btn.disabled = false; btn.textContent = "اجرای سناریو"; }
}

/* ================================================================= init */
document.addEventListener("DOMContentLoaded", async () => {
  $$("#tabs button[data-tab]").forEach(b => b.onclick = () => showTab(b.dataset.tab));
  $("#overlay").onclick = closeDrawer;
  $("#btnRefresh").onclick = async () => { await load(); await loadTxns(); toast("به‌روزرسانی شد"); };
  $("#btnPondOptimize").onclick = runPondOptimize;
  $("#btnPondRestore").onclick = restorePondAllocation;
  $("#pondDateSel").onchange = onPondDateChange;
  $("#pondDateCustom").onchange = onPondDateChange;
  $("#btnAddTxn").onclick = submitTxn;
  $("#btnAddOffer").onclick = async () => {
    try {
      await api("/api/offers/egg", "POST", {
        offer_date: $("#o_date").value, supplier: $("#o_sup").value,
        quantity: parseFloat($("#o_qty").value), price_per_egg: parseFloat($("#o_price").value),
        expiry_date: $("#o_exp").value || null,
        payment_terms_days: parseInt($("#o_terms").value || 0),
        quality_score: $("#o_q").value ? parseFloat($("#o_q").value) : null
      });
      await loadOffers(); toast("آفر ثبت شد");
    } catch (e) { toast("خطا: " + e.message, true); }
  };
  $("#btnResetAll").onclick = async () => {
    if (!confirm("همه فرضیات به مقدار پیش‌فرض config.yaml برگردند؟")) return;
    await api("/api/assumptions/reset", "POST", {});
    await loadAssumptions(); await load(); toast("همه فرضیات بازنشانی شدند");
  };
  $("#btnBackup").onclick = () => {
    showTab("quality");
    const inf = $("#exportInfo");
    api("/api/export/json").then(x => {
      inf.innerHTML = "محتوای پشتیبان: " + Object.entries(x.counts)
        .map(([k, v]) => `${k} (${n0(v)})`).join(" · ");
    }).catch(() => {});
    window.location.href = "/api/export/sqlite";
    toast("پشتیبان SQLite در حال دانلود است");
  };
  $("#btnPlanRun").onclick = () => loadPlan();
  $("#btnManualMixRun").onclick = () => runManualMix();
  $("#btnManualMixReset").onclick = () => resetManualMixToOptimised();
  $("#btnEggDecide").onclick = decideEgg;
  $("#btnSaleDecide").onclick = decideSale;
  $("#btnSaleAlloc").onclick = suggestSaleAlloc;
  $("#so_cohort").onchange = () => {           // تغییر حالت → تخصیص قبلی نامعتبر
    $("#soAlloc").innerHTML = ""; SALE_ALLOC = null; };
  $("#btnWifAdd").onclick = addWifRow;
  $("#btnWifRun").onclick = runWhatIf;
  $("#btnVariants").onclick = loadVariantComparison;
  $("#btnScenarios").onclick = loadScenarioComparison;
  $("#btnSaveBaseline").onclick = async () => {
    const replace = PLAN && PLAN.has_original;
    if (replace && !confirm("برنامه پایه قبلی جایگزین شود؟ مقایسه Target vs Actual از این پس با برنامه جدید انجام می‌شود.")) return;
    try {
      await api("/api/plan/save", "POST",
        { variant: PLAN_VARIANT, kind: "original", replace });
      toast("برنامه پایه ثبت شد");
      await loadPlan();
    } catch (e) { toast("خطا: " + e.message, true); }
  };
  $("#btnDemo").onclick = async () => {
    const r = await api("/api/demo/seed", "POST", {});
    await load(); await loadTxns();
    toast(r.seeded ? "داده نمونه اضافه شد (با برچسب [DEMO])" : r.reason);
  };
  $("#btnDemoClear").onclick = async () => {
    await api("/api/demo/clear", "POST", {});
    await load(); await loadTxns(); toast("داده نمونه باطل شد");
  };
  await load();
  await loadTxns();
  buildTxnForm();          // فرم از همان ابتدا آماده است، نه فقط وقتی فهرست خالی است
});
