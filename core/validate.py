"""
validate.py — قواعد کیفیت (۱۰ بررسی الزامی specification)
==========================================================
هر بررسی یک dict برمی‌گرداند: {id, title_fa, status: pass|warn|fail, detail}
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import date

from .plan_model import jalali_seasonality_factor
from .state import TROUGH, d


def run_all(db, A, bio, state, ledger, forecast, fxb=None) -> dict:
    checks = [
        _mass_balance(state),
        _cohort_balance(db, state),
        _pond_capacity(state, A, bio),
        _integer_ponds(state, A),
        _feed_reconciliation(db, state),
        _cash_ledger(db, ledger),
        _monthly_egg_limit(db, A, state.as_of),
        _offer_consistency(db),
        _fx_reconciliation(fxb),
        _regression_core(bio, A),
        _historical_sales(state),
        _effective_dating(A),
        _audit_trail(db),
        _transaction_fields(db),
        _inferred_cohorts(db),
    ]
    n_fail = sum(1 for c in checks if c["status"] == "fail")
    n_warn = sum(1 for c in checks if c["status"] == "warn")
    return {"checks": checks, "failed": n_fail, "warnings": n_warn,
            "passed": len(checks) - n_fail - n_warn,
            "overall": "fail" if n_fail else ("warn" if n_warn else "pass")}


def _mk(cid, title, status, detail):
    return {"id": cid, "title_fa": title, "status": status, "detail": detail}


# 1 ---------------------------------------------------------------------
def _mass_balance(state):
    bad = []
    for c in state.cohorts.values():
        out = c.alive + c.sold_count
        if out > c.egg_count + 0.5:
            bad.append(f"{c.cohort_id}: زنده+فروخته ({out:,.0f}) > تخم اولیه ({c.egg_count:,.0f})")
        if c.alive < -0.5:
            bad.append(f"{c.cohort_id}: تعداد زنده منفی")
    return _mk("mass_balance", "توازن جرمی (تخم ≥ زنده + فروخته)",
               "fail" if bad else "pass",
               "; ".join(bad) if bad else "همه cohortها متوازن هستند")


# 2 ---------------------------------------------------------------------
def _cohort_balance(db, state):
    bad = []
    for c in state.cohorts.values():
        alloc = sum(c.alloc.values())
        if abs(alloc - c.alive) > max(1.0, 0.001 * c.alive):
            bad.append(f"{c.cohort_id}: مجموع تخصیص استخر {alloc:,.0f} ≠ زنده {c.alive:,.0f}")
    return _mk("cohort_balance", "توازن cohort (تخصیص استخرها = تعداد زنده)",
               "fail" if bad else "pass",
               "; ".join(bad) if bad else "تخصیص همه cohortها با تعداد زنده می‌خواند")


# 3 ---------------------------------------------------------------------
def _pond_capacity(state, A, bio):
    over = []
    for p in state.pond_view():
        if p["capacity_applies"] and p["capacity"] > 0 and p["count"] > p["capacity"] * 1.0001:
            over.append(f"{p['pond_id']}: {p['count']:,.0f} > ظرفیت {p['capacity']:,.0f}")
    return _mk("pond_capacity", "رعایت ظرفیت تجربی استخر",
               "warn" if over else "pass",
               "; ".join(over) if over else "هیچ استخری از ظرفیت تجربی عبور نکرده است")


# 4 ---------------------------------------------------------------------
def _integer_ponds(state, A):
    req = sum(v for v in [state.bio.ponds_required(c.alive, c.mean_weight)
                          for c in state.cohorts.values()])
    int_req = sum(math.ceil(state.bio.ponds_required(c.alive, c.mean_weight))
                  for c in state.cohorts.values() if c.alive >= 1)
    op = int(A.get("farm.operational_ponds"))
    status = "pass"
    if int_req > op:
        status = "warn"
    return _mk("integer_ponds", "ظرفیت با استخر صحیح (integer)", status,
               f"نیاز کسری {req:.2f} استخر، با گرد کردن به بالا در هر cohort "
               f"{int_req} استخر در برابر {op} استخر عملیاتی"
               + ("  ← فشار ظرفیت" if int_req > op else ""))


# 5 ---------------------------------------------------------------------
def _feed_reconciliation(db, state):
    bad = []
    for name, f in state.feed.items():
        if f["qty_kg"] < -0.001:
            bad.append(f"{name}: موجودی منفی {f['qty_kg']:.1f} kg")
        expect = f["purchased_kg"] - f["consumed_kg"]
        if abs(expect - f["qty_kg"]) > 0.01:
            bad.append(f"{name}: خرید−مصرف ({expect:.1f}) ≠ موجودی ({f['qty_kg']:.1f})")
    if not state.feed:
        return _mk("feed_reconciliation", "تطبیق موجودی خوراک", "warn",
                   "هیچ خرید خوراکی ثبت نشده است — موجودی صفر فرض شده")
    return _mk("feed_reconciliation", "تطبیق موجودی خوراک",
               "fail" if bad else "pass",
               "; ".join(bad) if bad else "خرید − مصرف = موجودی برای همه انواع خوراک")


# 6 ---------------------------------------------------------------------
def _cash_ledger(db, ledger):
    m = ledger.metrics()
    ser = ledger.balance_series()
    total = sum(r["amount"] for r in ledger.rows)
    closing = ser[-1]["balance"] if ser else m["opening_cash"]
    ok = abs((m["opening_cash"] + total) - closing) < 1.0
    detail = (f"مانده پایانی {closing:,.0f} تومان؛ کمترین مانده {m['minimum_cash_balance']:,.0f}؛ "
              f"سرمایه در گردش: موجود {m['wc_available']:,.0f} / "
              f"قفل‌شده {m['wc_tied_up_now']:,.0f} / "
              f"اوج پیش‌بینی {m['wc_forecast_peak']:,.0f}")
    if not ok:
        return _mk("cash_ledger", "تطبیق دفتر نقدی", "fail",
                   "مجموع جریان‌ها با مانده پایانی نمی‌خواند")
    if m["wc_breach"]:
        return _mk("cash_ledger", "تطبیق دفتر نقدی", "warn",
                   detail + " — نیاز از سرمایه در گردش موجود بیشتر است")
    return _mk("cash_ledger", "تطبیق دفتر نقدی", "pass", detail)


# 7 ---------------------------------------------------------------------
def _monthly_egg_limit(db, A, as_of: date | None = None):
    """
    ۳۰۰٬۰۰۰ عدد در ماه یک «راهنمای برنامه‌ریزی» است، نه محدودیت فیزیکی مطلق
    (اصلاح ۷). داده واقعی تیر/جولای ۲۰۲۶ خودش ۳۲۰٬۰۰۰ بوده است.
    عبور از راهنما → اطلاع‌رسانی/هشدار. عبور از «سقف سخت» فقط وقتی خطاست که
    کاربر آن را در فرضیات فعال کرده باشد.
    """
    as_of = as_of or date.today()
    guide = float(A.get("egg.monthly_guideline"))
    hard_on = bool(A.get("egg.enforce_hard_monthly_max"))
    hard = float(A.get("egg.hard_monthly_max"))
    per = defaultdict(float)
    for t in db.active_txns():
        if t["txn_type"] == "egg_purchase":
            per[t["txn_date"][:7]] += float(t["quantity"] or 0)
    cur = as_of.strftime("%Y-%m")
    over_hard, over_guide_future, over_guide_past = [], [], []
    for k, v in sorted(per.items()):
        if hard_on and v > hard + 1e-6:
            over_hard.append(f"{k}: {v:,.0f} > سقف سخت {hard:,.0f}")
        elif v > guide + 1e-6:
            (over_guide_past if k <= cur else over_guide_future).append(
                f"{k}: {v:,.0f} > راهنما {guide:,.0f}")
    title = "راهنمای خرید ماهانه تخم"
    if over_hard:
        return _mk("monthly_egg_limit", title, "fail",
                   "عبور از سقف سخت فعال‌شده — " + "; ".join(over_hard))
    if over_guide_future:
        return _mk("monthly_egg_limit", title, "warn",
                   "خرید برنامه‌ریزی‌شده از راهنما عبور می‌کند (مجاز است اگر عرضه، "
                   "ظرفیت و سرمایه در گردش اجازه دهد): " + "; ".join(over_guide_future))
    if over_guide_past:
        return _mk("monthly_egg_limit", title, "pass",
                   "خرید واقعی از راهنمای ۳۰۰٬۰۰۰ عبور کرده است — این مغایرت نیست، "
                   "راهنما محدودیت فیزیکی نیست: " + "; ".join(over_guide_past))
    return _mk("monthly_egg_limit", title, "pass",
               f"بیشترین خرید ماهانه ثبت‌شده {max(per.values()) if per else 0:,.0f} عدد "
               f"در برابر راهنمای {guide:,.0f}")


# 8 ---------------------------------------------------------------------
def _offer_consistency(db):
    bad = []
    for o in db.egg_offers():
        if o["status"] in ("accepted", "partial"):
            if (o["accepted_quantity"] or 0) <= 0:
                bad.append(f"{o['offer_id']}: پذیرفته ولی مقدار صفر")
            if (o["accepted_quantity"] or 0) > (o["quantity"] or 0) + 1e-6:
                bad.append(f"{o['offer_id']}: مقدار پذیرفته > مقدار آفر")
        if o["status"] == "rejected" and (o["accepted_quantity"] or 0) > 0:
            bad.append(f"{o['offer_id']}: رد شده ولی مقدار پذیرفته دارد")
    n = len(db.egg_offers())
    return _mk("offer_consistency", "سازگاری Offerهای تخم",
               "fail" if bad else "pass",
               "; ".join(bad) if bad else f"{n} آفر ثبت‌شده، همه سازگار")


# 9 ---------------------------------------------------------------------
def _fx_reconciliation(fxb):
    """داده واقعی اکسل TGJU؛ هیچ سری ساختگی پذیرفته نمی‌شود (اصلاح ۸)."""
    if fxb is None or not fxb.series:
        return _mk("fx_reconciliation", "تطبیق بنچمارک ارزی", "warn",
                   "فایل واقعی نرخ دلار بارگذاری نشده است — بنچمارک ارزی "
                   "«در دسترس نیست» نمایش داده می‌شود و داده جایگزین ساخته نمی‌شود")
    s = fxb.series
    bad = sum(1 for r in s if r["close_toman"] <= 0)
    if fxb.source != "file":
        return _mk("fx_reconciliation", "تطبیق بنچمارک ارزی", "fail",
                   f"منبع سری نرخ دلار «{fxb.source}» است، نه فایل واقعی")
    if bad:
        return _mk("fx_reconciliation", "تطبیق بنچمارک ارزی", "fail",
                   f"{bad} رکورد نامعتبر در سری نرخ دلار")
    return _mk("fx_reconciliation", "تطبیق بنچمارک ارزی", "pass",
               f"{len(s):,} رکورد روزانه واقعی از {s[0]['date_g']} تا {s[-1]['date_g']} "
               f"(شیت «روزانه»، ستون «قیمت پایانی (تومان)»)")


# 10 --------------------------------------------------------------------
def _regression_core(bio, A):
    errs = []

    def close(a, b, tol, label):
        if abs(a - b) > tol:
            errs.append(f"{label}: {a:.4f} ≠ {b:.4f}")

    close(bio.weight_at_age(bio.day_1g), 1.0, 1e-6, f"وزن در روز {bio.day_1g}")
    close(bio.weight_at_age(bio.day_2g), 2.0, 1e-6, f"وزن در روز {bio.day_2g}")
    close(bio.weight_at_age(bio.day_10g), 10.0, 1e-6, f"وزن در روز {bio.day_10g}")
    close(bio.weight_at_age(bio.day_15g), 15.0, 1e-6, f"وزن در روز {bio.day_15g}")
    close(bio.cum_mortality(bio.day_1g), float(A.get("mortality.cum_at_1g")), 1e-9, "تلفات ۱g")
    close(bio.cum_mortality(bio.day_15g), float(A.get("mortality.cum_at_15g")), 1e-9, "تلفات ۱۵g")
    close(bio.fish_per_pond(1.0), float(A.get("capacity.fish_per_pond_1g")), 1.0, "ظرفیت ۱g")
    close(bio.fish_per_pond(15.0), float(A.get("capacity.fish_per_pond_15g")), 1.0, "ظرفیت ۱۵g")
    close(bio.sale_price(15.0), 22200, 1.0, "قیمت ۱۵g")
    close(bio.sale_price(5.0), 14200, 1.0, "قیمت ۵g")
    # مونوتونی
    ws = [0.1 * i for i in range(1, 200)]
    if any(bio.weight_at_age(i) > bio.weight_at_age(i + 1) for i in range(0, 200)):
        errs.append("منحنی رشد صعودی نیست")
    if any(bio.fish_per_pond(ws[i]) < bio.fish_per_pond(ws[i + 1]) - 1e-6
           for i in range(len(ws) - 1)):
        errs.append("منحنی ظرفیت نزولی نیست")
    if any(bio.cum_mortality(i) > bio.cum_mortality(i + 1) + 1e-12 for i in range(0, 200)):
        errs.append("تلفات تجمعی نزولی شده است")
    # heterogeneity
    q10, q50, q90 = (bio.weight_quantile(5.0, q) for q in (0.1, 0.5, 0.9))
    if not (q10 < q50 < q90):
        errs.append("چندک‌های وزن مرتب نیستند")
    if not (0.0 <= bio.fraction_above(5.0, 5.0) <= 1.0):
        errs.append("fraction_above خارج از بازه")
    return _mk("regression_core", "تست‌های رگرسیون محاسبات اصلی",
               "fail" if errs else "pass",
               "; ".join(errs) if errs else
               "منحنی رشد، تلفات، ظرفیت، قیمت و چندک‌های وزن همگی درست")


# 11 --------------------------------------------------------------------
def _historical_sales(state):
    """فروش‌های تاریخی بدون cohort: هرگز خودکار اعمال نمی‌شوند (اصلاح ۱۰)."""
    us = getattr(state, "unassigned_sales", [])
    if not us:
        return _mk("historical_sales", "تطبیق فروش‌های تاریخی", "pass",
                   "همه فروش‌های ثبت‌شده به cohort مشخصی نسبت داده شده‌اند")
    n = sum(x["quantity"] for x in us)
    val = sum(x["amount"] for x in us)
    no_w = [x for x in us if x.get("weight_g") in (None, 0)]
    detail = (f"{len(us)} فروش با مجموع {n:,.0f} قطعه و {val:,.0f} تومان درآمد واقعی، "
              f"بدون cohort مشخص (Cohort Unassigned). درآمد در دفتر نقدی لحاظ شده "
              f"ولی موجودی cohortها کاهش نیافته است — نیاز به اطلاعات بیشتر برای "
              f"reconciliation.")
    if no_w:
        detail += (f" همچنین {len(no_w)} فروش وزن نامشخص دارد (حدس زده نشده) — "
                   f"بدون وزن، تشخیص cohort ممکن نیست.")
    else:
        detail += (" وزن همه آن‌ها ثبت شده است، پس از پنل «تشخیص cohort» "
                   "می‌توان مبدأ را تعیین کرد.")
    return _mk("historical_sales", "تطبیق فروش‌های تاریخی", "warn", detail)


# 12 --------------------------------------------------------------------
def _effective_dating(A):
    """تغییر قیمت نباید retroactive باشد (اصلاح ۶)."""
    keys = A.effective_dated_keys()
    if not keys:
        return _mk("effective_dating", "تاریخ اعتبار پارامترهای مالی", "fail",
                   "هیچ پارامتر مالی effective-dated تعریف نشده است")
    errs, notes = [], []
    for k in keys:
        h = A.hist.get(k) or []
        if not h:
            continue
        dates = [x["effective_from"] for x in h]
        if dates != sorted(dates):
            errs.append(f"{k}: تاریخچه مرتب نیست")
        # مقدار قبل از اولین تاریخ اعتبار نباید مقدار جدید باشد
        first = h[0]
        before = A.get_at(k, "1900-01-01")
        if before == first["value"] and len(h) >= 1 and A.get(k) != first["value"]:
            errs.append(f"{k}: مقدار جدید به گذشته سرایت کرده است")
        notes.append(f"{k}: {len(h)} رکورد تاریخ‌دار از {dates[0]}")
    return _mk("effective_dating", "تاریخ اعتبار پارامترهای مالی",
               "fail" if errs else "pass",
               "; ".join(errs) if errs else
               (("; ".join(notes)) if notes else
                f"{len(keys)} پارامتر مالی effective-dated هستند؛ هنوز تغییر تاریخ‌داری ثبت نشده"))


# 13 --------------------------------------------------------------------
def _audit_trail(db):
    """اصلاح تراکنش‌ها باید زنجیره کامل داشته باشد و رکوردی حذف نشده باشد."""
    errs = []
    rows = db.q("SELECT id,status,corrects_id FROM transactions")
    ids = {r["id"] for r in rows}
    for r in rows:
        if r["corrects_id"] and r["corrects_id"] not in ids:
            errs.append(f"#{r['id']}: رکورد اصلاح‌شده مرجع ({r['corrects_id']}) وجود ندارد")
    orphan = db.q("SELECT id FROM transactions WHERE status='corrected' AND id NOT IN "
                  "(SELECT corrects_id FROM transactions WHERE corrects_id IS NOT NULL)")
    for o in orphan:
        errs.append(f"#{o['id']}: علامت «اصلاح‌شده» دارد ولی نسخه جدیدی برایش ثبت نشده")
    n_corr = len([r for r in rows if r["corrects_id"]])
    return _mk("audit_trail", "زنجیره اصلاح و audit trail",
               "fail" if errs else "pass",
               "; ".join(errs) if errs else
               f"{len(rows)} تراکنش، {n_corr} اصلاح — هیچ رکوردی حذف نشده است")


# 14 --------------------------------------------------------------------
def _payload(t) -> dict:
    """payload ممکن است رشته JSON یا dict از قبل تبدیل‌شده باشد."""
    p = t.get("payload")
    if isinstance(p, dict):
        return p
    try:
        return json.loads(p or "{}")
    except Exception:
        return {}


def _transaction_fields(db):
    """کامل بودن فیلدهای الزامی هر تراکنش (اصلاح ۳)."""
    need_amount = {"egg_purchase", "feed_purchase", "sale", "operating_cost",
                   "payment", "receipt"}
    need_qty = {"egg_purchase", "feed_purchase", "feed_consumption", "mortality",
                "count_observation", "sale", "transfer"}
    miss = []
    for t in db.active_txns():
        typ = t["txn_type"]
        pl = _payload(t)
        if typ in need_amount and not t.get("amount") and not pl.get("cost_unknown"):
            miss.append(f"#{t['id']} {typ}: مبلغ ندارد")
        if typ in need_qty and not t.get("quantity"):
            miss.append(f"#{t['id']} {typ}: مقدار ندارد")
        if not t.get("unit"):
            miss.append(f"#{t['id']} {typ}: واحد ندارد")
        if typ == "operating_cost" and not t.get("category"):
            miss.append(f"#{t['id']}: هزینه بدون دسته‌بندی")
    n = len(db.active_txns())
    return _mk("transaction_fields", "کامل بودن فیلدهای تراکنش",
               "warn" if miss else "pass",
               "; ".join(miss[:6]) + (f" … و {len(miss)-6} مورد دیگر" if len(miss) > 6 else "")
               if miss else f"همه {n} تراکنش فعال فیلدهای الزامی را دارند")


# ===================================================================== #
#  بررسی‌های کیفیت برنامه (مرحله ۲)                                    #
# ===================================================================== #
def run_plan_checks(A, plan) -> dict:
    checks = [
        _plan_mass_balance(plan),
        _plan_pond_capacity(A, plan),
        _plan_integer_ponds(A, plan),
        _plan_monthly_eggs(A, plan),
        _plan_working_capital(plan),
        _plan_feed_consistency(plan),
        _plan_timeline_non_cyclic(A, plan),
        _plan_cohort_allocation(plan),
        _plan_fx_benchmark(plan),
        _plan_contribution_identity(plan),
    ]
    nf = sum(1 for c in checks if c["status"] == "fail")
    nw = sum(1 for c in checks if c["status"] == "warn")
    return {"checks": checks, "failed": nf, "warnings": nw,
            "passed": len(checks) - nf - nw,
            "overall": "fail" if nf else ("warn" if nw else "pass")}


def _plan_mass_balance(plan):
    """ماهی فروخته‌شده در برنامه نباید از موجودی + خرید بیشتر باشد."""
    bought = sum(l["quantity"] for l in plan.solution.chosen_lots)
    alive = sum(c.alive for c in plan.state.cohorts.values())
    sold = sum(plan.harvest_fish)
    if sold > bought + alive + 1:
        return _mk("plan_mass_balance", "توازن جرمی برنامه", "fail",
                   f"فروش برنامه {sold:,.0f} > موجودی {alive:,.0f} + خرید {bought:,.0f}")
    surv = sold / (bought + alive) if (bought + alive) else 0
    return _mk("plan_mass_balance", "توازن جرمی برنامه", "pass",
               f"فروش {sold:,.0f} از {bought + alive:,.0f} قطعه ورودی "
               f"(بقای کلی {surv:.1%})")


def _plan_pond_capacity(A, plan):
    op = int(A.get("farm.operational_ponds"))
    peak = max(plan.ponds) if plan.ponds else 0
    if peak <= op:
        return _mk("plan_pond_capacity", "ظرفیت استخر در برنامه", "pass",
                   f"اوج نیاز {peak:.0f} استخر در برابر {op} استخر عملیاتی")
    wk = plan.ponds.index(peak)
    when = plan.grid.dates[wk].isoformat()
    # منشأ از همان تفکیک `Plan._classify_pond_breach` می‌آید (تک‌منبعی)، نه
    # از این فرض قدیمی که «هر عبور بعد از هفته صفر تقصیر برنامه است».
    forced = getattr(plan, "pond_forced_floor", None)
    if wk == 0 or (forced and wk < len(forced) and forced[wk] >= peak):
        cause = "  ← این کمبود از موجودی فعلی مزرعه می‌آید، نه از تصمیم‌های برنامه"
    else:
        cause = "  ← ناشی از تصمیم‌های برنامه"
    return _mk("plan_pond_capacity", "ظرفیت استخر در برنامه", "warn",
               f"اوج نیاز {peak:.0f} استخر در {when} در برابر {op} عملیاتی" + cause)


def _plan_integer_ponds(A, plan):
    """پروفایل‌ها با ceil در سطح cohort ساخته شده‌اند؛ نباید عدد کسری بماند."""
    bad = [t for t, v in enumerate(plan.ponds) if abs(v - round(v)) > 1e-6]
    lp = getattr(plan, "ponds_lp", plan.ponds)
    gap = max(abs(lp[t] - plan.ponds[t]) for t in range(len(plan.ponds))) if lp else 0.0
    if bad:
        return _mk("plan_integer_ponds", "استخر صحیح در برنامه", "fail",
                   f"{len(bad)} هفته با نیاز کسری استخر")
    detail = ("نیاز استخر در همه هفته‌ها عدد صحیح است — شاخه‌های یک cohort پیش از "
              "گرد شدن تجمیع می‌شوند تا ظرفیت دوباره‌شماری نشود.")
    if gap > 0.5:
        detail += (f" تقریب خطی داخل MILP تا {gap:.0f} استخر با عدد نهایی اختلاف "
                   f"دارد؛ عدد گزارش‌شده همین محاسبه پس از حل است.")
    return _mk("plan_integer_ponds", "استخر صحیح در برنامه", "pass", detail)


def _plan_monthly_eggs(A, plan):
    guide = float(A.get("egg.monthly_guideline"))
    hard_on = bool(A.get("egg.enforce_hard_monthly_max"))
    hard = float(A.get("egg.hard_monthly_max"))
    avail = float(A.get("planning.monthly_availability"))
    per = {}
    for l in plan.solution.chosen_lots:
        per[l["month"]] = per.get(l["month"], 0.0) + l["quantity"]
    # سقف guide/avail باید با همان Seasonality شمسی که خودِ optimizer در
    # محدودیت `avail_{m}` اعمال می‌کند بازتوزیع شود (اصلاح ۴) — وگرنه یک
    # خرید مجاز در نیمهٔ دوم سال شمسی (ضریب >۱) اینجا به‌غلط «عبور از سقف»
    # گزارش می‌شود، درحالی‌که خودِ حل واقعاً از سقف واقعی همان ماه عبور
    # نکرده است.
    season = {m: jalali_seasonality_factor(A, date.fromisoformat(m + "-01"))
             for m in per}
    over_guide = [f"{m}: {v:,.0f}" for m, v in sorted(per.items())
                 if v > guide + 1e-6]
    over_hard = [f"{m}: {v:,.0f}" for m, v in sorted(per.items())
                 if hard_on and v > hard + 1e-6]
    over_avail = [f"{m}: {v:,.0f}" for m, v in sorted(per.items())
                 if v > avail * season[m] + 1e-6]
    if over_hard:
        return _mk("plan_monthly_eggs", "خرید ماهانه تخم در برنامه", "fail",
                   "عبور از سقف سخت: " + "; ".join(over_hard))
    if over_avail:
        return _mk("plan_monthly_eggs", "خرید ماهانه تخم در برنامه", "fail",
                   "عبور از عرضه مورد انتظار (با احتساب Seasonality شمسی): "
                   + "; ".join(over_avail))
    if over_guide:
        return _mk("plan_monthly_eggs", "خرید ماهانه تخم در برنامه", "warn",
                   "عبور از راهنمای ماهانه (مجاز ولی جریمه‌دار): " + "; ".join(over_guide))
    return _mk("plan_monthly_eggs", "خرید ماهانه تخم در برنامه", "pass",
               f"بیشترین خرید ماهانه برنامه {max(per.values()) if per else 0:,.0f} "
               f"در برابر راهنمای {guide:,.0f} (با احتساب Seasonality شمسی)")


def _plan_working_capital(plan):
    peak = max(plan.capital) if plan.capital else 0
    avail = plan.wc_available
    if peak <= avail:
        return _mk("plan_working_capital", "سرمایه در گردش برنامه", "pass",
                   f"اوج {peak:,.0f} در برابر {avail:,.0f} تومان موجود "
                   f"(فاصله {avail - peak:,.0f})")
    return _mk("plan_working_capital", "سرمایه در گردش برنامه", "warn",
               f"اوج {peak:,.0f} از سرمایه موجود {avail:,.0f} بیشتر است "
               f"(کسری {peak - avail:,.0f})")


def _plan_feed_consistency(plan):
    kg = sum(plan.feed_kg)
    cost = sum(plan.feed_cost)
    if kg <= 0:
        return _mk("plan_feed", "سازگاری خوراک برنامه", "warn",
                   "برنامه هیچ نیاز خوراکی محاسبه نکرده است")
    avg = cost / kg
    lo = min(float(b["price"]) for b in plan.bio.feed_bands)
    hi = max(float(b["price"]) for b in plan.bio.feed_bands)
    if not (lo * 0.95 <= avg <= hi * 1.05):
        return _mk("plan_feed", "سازگاری خوراک برنامه", "fail",
                   f"میانگین قیمت خوراک برنامه {avg:,.0f} خارج از بازه جدول "
                   f"({lo:,.0f} تا {hi:,.0f})")
    return _mk("plan_feed", "سازگاری خوراک برنامه", "pass",
               f"{kg:,.0f} kg خوراک، میانگین {avg:,.0f} تومان/kg "
               f"(داخل بازه جدول piecewise)")


def _plan_timeline_non_cyclic(A, plan):
    """Fix 2 — تقویم واقعی، حداقل ۷۲ هفته، بدون modulo-52."""
    weeks = plan.model.weeks
    dates = plan.grid.dates
    ok_len = weeks >= 72
    ok_monotone = all(dates[i] < dates[i + 1] for i in range(len(dates) - 1))
    span_days = (dates[-1] - dates[0]).days
    if not ok_len:
        return _mk("plan_timeline", "تقویم مالی غیرچرخه‌ای", "fail",
                   f"افق {weeks} هفته کمتر از حداقل ۷۲ هفته است")
    if not ok_monotone:
        return _mk("plan_timeline", "تقویم مالی غیرچرخه‌ای", "fail",
                   "شبکه زمانی صعودی نیست")
    return _mk("plan_timeline", "تقویم مالی غیرچرخه‌ای", "pass",
               f"{weeks} هفته پیوسته تقویمی ({span_days} روز) از {dates[0]} تا "
               f"{dates[-1]} — بدون modulo-52")


def _plan_cohort_allocation(plan):
    """هر cohort موجود باید دقیقاً یک بار (شاید تقسیم‌شده) تخصیص یابد."""
    bad = []
    live = {c.cohort_id for c in plan.state.cohorts.values() if c.alive >= 1}
    for cid in live:
        tot = sum(plan.solution.cohort_split.get(cid, {}).values())
        if abs(tot - 1.0) > 1e-4:
            bad.append(f"{cid}: مجموع سهم {tot:.3f}")
    partial = [c for c in plan.cohort_decisions() if c["partial_harvest"]]
    if bad:
        return _mk("plan_cohort_allocation", "تخصیص cohortها در برنامه", "fail",
                   "; ".join(bad))
    return _mk("plan_cohort_allocation", "تخصیص cohortها در برنامه", "pass",
               f"هر {len(live)} cohort فعال دقیقاً یک بار تخصیص یافته؛ "
               f"{len(partial)} مورد برداشت جزئی/grading")


def _plan_fx_benchmark(plan):
    qs = getattr(plan, "_last_fx", None)
    if qs is None:
        return _mk("plan_fx", "بنچمارک ارزی برنامه", "pass",
                   "بنچمارک ارزی جداگانه محاسبه می‌شود و جای سود عملیاتی را نمی‌گیرد")
    return _mk("plan_fx", "بنچمارک ارزی برنامه", "pass", "محاسبه شد")


def _plan_contribution_identity(plan):
    """اتحاد حسابداری: درآمد − خوراک − تخم − ثابت = حاشیه اسمی."""
    rev = sum(plan.revenue)
    feed = sum(plan.feed_cost)
    egg = sum(plan.egg_cost)
    fixed = sum(plan.fixed_cost)
    lhs = rev - feed - egg - fixed
    rhs = sum(b["contribution_nominal"] for b in plan.monthly)
    if abs(lhs - rhs) > max(1.0, abs(lhs) * 1e-9):
        return _mk("plan_contribution", "اتحاد حسابداری برنامه", "fail",
                   f"{lhs:,.0f} ≠ {rhs:,.0f}")
    q = sum(b["contribution_nominal"] for b in plan.quarterly)
    if abs(q - rhs) > max(1.0, abs(rhs) * 1e-9):
        return _mk("plan_contribution", "اتحاد حسابداری برنامه", "fail",
                   f"جمع سه‌ماهه {q:,.0f} ≠ جمع ماهانه {rhs:,.0f}")
    return _mk("plan_contribution", "اتحاد حسابداری برنامه", "pass",
               f"درآمد {rev:,.0f} − خوراک {feed:,.0f} − تخم {egg:,.0f} − "
               f"ثابت {fixed:,.0f} = {rhs:,.0f}؛ ماهانه و سه‌ماهه هم می‌خوانند")


# 15 --------------------------------------------------------------------
def _inferred_cohorts(db):
    """cohortهای استنتاجی باید صریحاً Estimated بمانند، نه Observed."""
    rows = [t for t in db.active_txns()
            if t["txn_type"] == "egg_purchase" and _payload(t).get("inferred")]
    if not rows:
        return _mk("inferred_cohorts", "cohortهای استنتاجی", "pass",
                   "همه cohortها از خرید واقعی ثبت‌شده ساخته شده‌اند")
    bad = [f"#{t['id']}" for t in rows if t.get("data_source") != "estimated"]
    if bad:
        return _mk("inferred_cohorts", "cohortهای استنتاجی", "fail",
                   "این cohortها استنتاجی‌اند ولی برچسب Estimated ندارند: "
                   + "، ".join(bad))
    n = sum(float(t["quantity"] or 0) for t in rows)
    unknown = [t for t in rows if _payload(t).get("cost_unknown")]
    detail = (f"{len(rows)} cohort استنتاجی با مجموع {n:,.0f} تخم برآوردی، "
              f"همگی با برچسب Estimated")
    if unknown:
        detail += (f"؛ بهای تخم {len(unknown)} مورد نامشخص است و صفر ثبت شده تا "
                   f"دفتر نقدی با عدد ساختگی آلوده نشود")
    return _mk("inferred_cohorts", "cohortهای استنتاجی", "warn", detail)


# ============ بررسی‌های اصلاحات نهایی مرحله ۲ ============
def _plan_state_transition(plan):
    """موجودی ابتدا − تلفات − فروش = موجودی پایان، برای هر پروفایل انتخاب‌شده."""
    bad = []
    for k, w in plan.solution.selected.items():
        pool = plan.cand_base["new_lots"] if k.startswith("L|") \
            else plan.cand_base["existing"]
        p = pool.get(k)
        if not p:
            continue
        start = p.quantity
        died = sum(p.mortality)
        sold = sum(p.harvest_fish)
        left = p.fish[min(p.last_week, len(p.fish) - 1)]
        if abs(start - (died + sold + left)) > max(1.0, start * 1e-6):
            bad.append(f"{p.key}: {start:,.0f} ≠ {died:,.0f}+{sold:,.0f}+{left:,.0f}")
        # پس از آخرین برداشت نباید خوراکی مصرف شود
        if sum(p.feed_kg[p.last_week + 1:]) > 1e-6:
            bad.append(f"{p.key}: پس از آخرین برداشت خوراک محاسبه شده است")
    return _mk("plan_state_transition", "گذار حالت موجودی برنامه",
               "fail" if bad else "pass",
               "; ".join(bad[:4]) if bad else
               "برای همه پروفایل‌ها: ابتدا − تلفات − فروش = پایان؛ ماهی فروخته‌شده "
               "دوباره تلفات یا مصرف‌کننده خوراک شمرده نمی‌شود")


def _plan_capital_release(plan):
    """پس از فروش کامل، سرمایه درگیر آن cohort باید آزاد شده باشد."""
    bad = []
    for k in plan.solution.selected:
        pool = plan.cand_base["new_lots"] if k.startswith("L|") \
            else plan.cand_base["existing"]
        p = pool.get(k)
        if not p or p.last_week >= len(p.capital) - 1:
            continue
        if p.fish[p.last_week] < 1 and p.capital[p.last_week] > 1:
            bad.append(f"{p.key}: {p.capital[p.last_week]:,.0f} تومان سرمایه بعد از "
                       f"فروش کامل آزاد نشده")
    return _mk("plan_capital_release", "آزادسازی سرمایه پس از فروش",
               "fail" if bad else "pass",
               "; ".join(bad[:3]) if bad else
               "سرمایه هر cohort به نسبت ماهی فروخته‌شده آزاد می‌شود؛ سرمایه در "
               "موجودی فقط متعلق به ماهی باقیمانده است")


def _plan_cash_reconciliation(plan):
    """دفتر نقدی برنامه باید با جریان‌های آن بخواند و ۵۰/۵۰ رعایت شود."""
    c = plan.cash
    m = c.metrics()
    lhs = m["opening_cash"] + m["total_inflow"] - m["total_outflow"]
    if abs(lhs - m["closing_balance"]) > 1.0:
        return _mk("plan_cash", "تطبیق دفتر نقدی برنامه", "fail",
                   f"{lhs:,.0f} ≠ {m['closing_balance']:,.0f}")
    rev = sum(plan.revenue)
    got = sum(c.inflow)
    if abs(rev - got) > max(1.0, rev * 1e-6):
        return _mk("plan_cash", "تطبیق دفتر نقدی برنامه", "fail",
                   f"درآمد {rev:,.0f} با مجموع دریافتی‌ها {got:,.0f} نمی‌خواند")
    return _mk("plan_cash", "تطبیق دفتر نقدی برنامه", "pass",
               f"شرایط پرداخت مشتری: {m['upfront_share']:.0%} نقد و باقیمانده پس از "
               f"{m['balance_delay_days']} روز · اوج نیاز تأمین مالی "
               f"{m['peak_funding_requirement']:,.0f} در {m['peak_funding_date']} · "
               f"چرخه تبدیل وجه نقد {m['cash_conversion_cycle_days']} روز")


def _plan_working_capital_v2(plan):
    m = plan.cash.metrics()
    if not m["wc_breach"]:
        return _mk("plan_working_capital", "سرمایه در گردش برنامه", "pass",
                   f"اوج نیاز تأمین مالی {m['peak_funding_requirement']:,.0f} در برابر "
                   f"{m['wc_available']:,.0f} تومان موجود "
                   f"(فاصله {m['wc_headroom']:,.0f})؛ اوج سرمایه در موجودی "
                   f"{m['peak_inventory_capital']:,.0f}")
    return _mk("plan_working_capital", "سرمایه در گردش برنامه", "fail",
               f"اوج نیاز {m['peak_funding_requirement']:,.0f} از سرمایه موجود "
               f"{m['wc_available']:,.0f} بیشتر است — برنامه feasible نیست "
               f"(کسری {-m['wc_headroom']:,.0f})")


def _plan_pond_feasibility(A, plan):
    """
    عبور از ظرفیت همیشه یک معنا ندارد و نباید یک‌کاسه `fail` شود.

    * اگر تصمیم‌های برنامه (lot جدید یا cohortهایی که هنوز گزینه دارند)
      باعث عبور شوند → `fail`. برنامه واقعاً باید اصلاح شود.
    * اگر عبور فقط از کف اجباری موجودی فعلی بیاید — cohortهایی که از
      بالاترین وزن برداشتِ کالیبره‌شده عبور کرده‌اند و در مدل فعلی هیچ
      تصمیمی برایشان باقی نمانده — این یک واقعیت وضعیت موجود است، نه خطای
      برنامه. تا وقتی با استخرهای رزرو (کل {total}) قابل جذب باشد،
      `warn` است با ذکر صریح منشأ؛ اگر از کل استخرها هم عبور کند، `fail`.
    """
    op = int(A.get("farm.operational_ponds"))
    total = int(A.get("farm.total_ponds"))
    peak_future = max(plan.ponds[1:], default=0)
    if peak_future <= op:
        return _mk("plan_pond_feasible", "اجراشدنی بودن ظرفیت استخر", "pass",
                   f"اوج نیاز آینده {peak_future:.0f} استخر ≤ {op} عملیاتی "
                   f"(پس از {plan.repair_rounds} دور اصلاح صحیح)؛ "
                   f"استخرهای رزرو دست‌نخورده باقی می‌مانند")

    if getattr(plan, "pond_breach_source", "plan") == "state" and peak_future <= total:
        forced = getattr(plan, "pond_forced_floor", [])
        wk = max(plan.pond_over_unavoidable, key=plan.pond_over_unavoidable.get)
        cids = ", ".join(sorted(
            c.cohort_id for c in plan.state.cohorts.values()
            if c.alive >= 1 and c.mean_weight >
            max(float(w) for w in A.get("planning.harvest_weights"))))
        return _mk("plan_pond_feasible", "اجراشدنی بودن ظرفیت استخر", "warn",
                   f"اوج نیاز {peak_future:.0f} استخر از {op} عملیاتی بیشتر است، "
                   f"ولی منشأ آن تصمیم‌های برنامه نیست: کف اجباری موجودی فعلی در "
                   f"{plan.grid.dates[wk].isoformat()} به‌تنهایی "
                   f"{forced[wk] if wk < len(forced) else 0:.0f} استخر می‌خواهد"
                   + (f" (cohortهای خارج از بازهٔ وزن برداشت: {cids})" if cids else "")
                   + f". با {peak_future - op:.0f} استخر از {total - op} رزرو قابل "
                     f"اجراست. برای آزادسازی ظرفیت، grading یا برداشت جزئی زودهنگام "
                     f"همین cohortها تنها اهرم واقعی است.")

    return _mk("plan_pond_feasible", "اجراشدنی بودن ظرفیت استخر", "fail",
               f"پس از گرد کردن صحیح، اوج نیاز {peak_future:.0f} استخر از {op} "
               f"عملیاتی بیشتر است — برنامه feasible اعلام نمی‌شود")


def _plan_reconciliation(plan):
    r = plan.reconciliation
    if r["reconciled"]:
        return _mk("plan_reconciliation", "وضعیت reconciliation موجودی", "pass",
                   f"همه {r['total_sold_fish']:,.0f} قطعه فروش به cohort تخصیص یافته‌اند؛ "
                   f"وضعیت برنامه FINAL")
    return _mk("plan_reconciliation", "وضعیت reconciliation موجودی", "warn",
               f"{r['unallocated_fish']:,.0f} از {r['total_sold_fish']:,.0f} قطعه فروش "
               f"هنوز تخصیص نیافته ({r['coverage_ratio']:.0%} پوشش) — "
               f"وضعیت برنامه PROVISIONAL")


def _plan_horizon_labels(plan):
    hs = plan.horizon_split()
    a, b = hs["rolling_12m"], hs["full_lifecycle"]
    if b["contribution_nominal"] < a["contribution_nominal"] - 1:
        return _mk("plan_horizon", "تفکیک نتیجه ۱۲ ماهه از چرخه عمر", "fail",
                   "ارزش چرخه عمر نباید کمتر از نتیجه ۱۲ ماهه باشد")
    return _mk("plan_horizon", "تفکیک نتیجه ۱۲ ماهه از چرخه عمر", "pass",
               f"نتیجه غلتان ۱۲ ماهه {a['contribution_nominal']:,.0f} · "
               f"ارزش کل چرخه عمر {b['contribution_nominal']:,.0f} "
               f"({b['spillover_fish']:,.0f} قطعه به سال بعد می‌رود) — "
               f"عدد چرخه عمر «سود سالانه» نامیده نمی‌شود")


def _plan_saleability(A, plan):
    bands = A.get("planning.saleability")
    probs = {float(b["w_min"]): float(b["prob"]) for b in bands}
    if not probs:
        return _mk("plan_saleability", "مدل احتمال یافتن مشتری", "fail",
                   "هیچ بازه‌ای تعریف نشده است")
    delayed = 0
    for k in plan.solution.selected:
        pool = plan.cand_base["new_lots"] if k.startswith("L|") \
            else plan.cand_base["existing"]
        p = pool.get(k)
        if p and len(p.harvest_weeks) > 3:
            delayed += 1
    warnings = getattr(plan, "saleability_warnings", [])
    detail = ("; ".join(f"{b['label']}: {float(b['prob']):.0%}" for b in bands) +
               f" — احتمال تلاش اول در قیمت آن ضرب نمی‌شود؛ فقط تلاش‌های "
               f"تأخیریافته (نه کل حجم) مشمول تخفیف بازهٔ همان وزن می‌شوند "
               f"({delayed} پروفایل با موج فروش کشیده‌شده)")
    if warnings:
        return _mk("plan_saleability", "مدل احتمال یافتن مشتری", "warn",
                   detail + " — " + " ".join(warnings))
    return _mk("plan_saleability", "مدل احتمال یافتن مشتری", "pass", detail)


def run_plan_checks_v2(A, plan) -> dict:
    """بررسی‌های کیفیت برنامه، شامل اصلاحات نهایی مرحله ۲."""
    base = run_plan_checks(A, plan)
    extra = [
        _plan_state_transition(plan),
        _plan_capital_release(plan),
        _plan_cash_reconciliation(plan),
        _plan_working_capital_v2(plan),
        _plan_pond_feasibility(A, plan),
        _plan_reconciliation(plan),
        _plan_horizon_labels(plan),
        _plan_saleability(A, plan),
    ]
    keep = [c for c in base["checks"]
            if c["id"] not in {"plan_working_capital"}]
    checks = keep + extra
    nf = sum(1 for c in checks if c["status"] == "fail")
    nw = sum(1 for c in checks if c["status"] == "warn")
    return {"checks": checks, "failed": nf, "warnings": nw,
            "passed": len(checks) - nf - nw,
            "overall": "fail" if nf else ("warn" if nw else "pass")}
