"""
hypothetical.py — وضعیت فرضی مزرعه و بهینه‌سازی دوباره (اصلاحات مرحله ۳)
==========================================================================
هر تصمیم خرید یا فروش با یک قاعده ارزیابی می‌شود:

    وضعیت واقعی → Clone → اعمال تراکنش فرضی → بازمحاسبه وضعیت
        → اجرای دوباره Optimizer → مقایسه با Baseline

یعنی «ارزش یک فروش» فقط سود همان ماهی نیست؛ ارزش کل مزرعه **با** آن
تصمیم در برابر ارزش کل مزرعه **بدون** آن است. ظرفیت استخر آزادشده،
نقدینگی زودتر، خوراک نخورده و تلفات رخ‌نداده همه به‌طور خودکار در برنامه
جدید ظاهر می‌شوند — نه به‌صورت یک اصلاح دستی روی baseline.

هیچ تابعی در این ماژول در پایگاه داده نمی‌نویسد.
"""
from __future__ import annotations

import math
from collections import OrderedDict
from datetime import date, timedelta

from .planner import Plan
from .state import d

# ═══════════════════════════════════════════════════ cache برنامه‌ها
# حل هر MILP حدود یک تا دو ثانیه طول می‌کشد و جست‌وجوی قیمت بی‌تفاوتی چند بار
# آن را صدا می‌زند. cache فقط روی «همان ورودی دقیقاً همان خروجی» تکیه دارد،
# پس reproducibility را نمی‌شکند.
_CACHE: "OrderedDict[tuple, Plan]" = OrderedDict()
_CACHE_MAX = 16


def cache_clear():
    _CACHE.clear()


def _fingerprint(A, state, variant, wc, lots, cash_rows) -> tuple:
    cohorts = tuple(sorted(
        (c.cohort_id, round(c.alive, 4), round(c.mean_weight, 6),
         round(c.growth_offset_days, 4), round(c.cv_shift, 6))
        for c in state.cohorts.values()))
    lot_fp = tuple(sorted((str(l.get("date")), float(l["quantity"]),
                           float(l.get("price") or 0)) for l in (lots or [])))
    cash_fp = tuple(sorted((str(r.get("date") or r.get("week")),
                            round(float(r.get("amount") or 0), 3),
                            str(r.get("type"))) for r in (cash_rows or [])))
    event_fp = tuple(sorted(
        (str(e.get("type")), str(e.get("date")), round(float(e.get("price") or 0), 3),
         tuple(sorted((str(a.get("cohort_id")), round(float(a.get("quantity") or 0), 3))
                      for a in (e.get("allocations") or []))))
        for e in getattr(state, "hypothetical_events", [])
    ))
    return (A.fingerprint(), state.as_of.isoformat(), variant,
            round(float(wc or 0), 2), cohorts, lot_fp, cash_fp, event_fp)


def solve_plan(A, bio, state, variant: str = "balanced",
               extra_lots: list | None = None,
               extra_cash_rows: list | None = None,
               wc_available: float | None = None,
               use_cache: bool = True) -> Plan:
    """حل برنامه با cache. همان ورودی → همان خروجی."""
    key = _fingerprint(A, state, variant, wc_available, extra_lots, extra_cash_rows)
    if use_cache and key in _CACHE:
        _CACHE.move_to_end(key)
        return _CACHE[key]
    p = Plan(A, bio, state, variant, wc_available=wc_available,
             extra_lots=extra_lots, extra_cash_rows=extra_cash_rows)
    if use_cache:
        _CACHE[key] = p
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return p


# ═══════════════════════════════════════════ ظرفیت استخر به‌صورت صحیح
def integer_ponds_now(state) -> float:
    """
    نیاز استخر امروز به‌صورت **صحیح** (اصلاح ۳).

    هر cohort جداگانه گرد می‌شود، چون ماهی دو cohort با وزن متفاوت در یک
    استخر مخلوط نمی‌شود. ماهی زیر آستانه ظرفیت (تراف) شمرده نمی‌شود.
    """
    bio = state.bio
    total = 0.0
    for c in state.cohorts.values():
        if c.alive < 1 or not bio.counts_toward_pond_capacity(c.mean_weight):
            continue
        total += math.ceil(c.alive / max(1.0, bio.fish_per_pond(c.mean_weight)))
    return total


def ponds_of_cohort(state, c) -> float:
    bio = state.bio
    if c.alive < 1 or not bio.counts_toward_pond_capacity(c.mean_weight):
        return 0.0
    return math.ceil(c.alive / max(1.0, bio.fish_per_pond(c.mean_weight)))


# ═══════════════════════════════════════════════ تراکنش‌های فرضی
def apply_sale(state, allocations: list, price: float,
               sale_date: date | None = None, weight_g: float | None = None) -> list:
    """
    اعمال یک فروش فرضی روی وضعیت (باید از قبل clone شده باشد).

    برمی‌گرداند: ردیف‌های نقدی دریافت وجه، طبق شرایط پرداخت مشتری.
    """
    if getattr(state, "hypothetical", False) is not True:
        raise RuntimeError("فروش فرضی فقط روی وضعیت clone شده مجاز است")
    sale_date = sale_date or state.as_of
    applied = []
    for a in allocations:
        cid, qty = a["cohort_id"], float(a["quantity"])
        c = state.cohorts.get(cid)
        if not c:
            raise ValueError(f"cohort ناشناخته: {cid}")
        n = min(qty, c.alive)
        w = weight_g if weight_g is not None else c.mean_weight
        c.alive -= n
        c.sold_count += n
        c.sold_revenue += n * price
        c.remove(n)
        if c.alive < 1:
            c.closed = True
        applied.append({"cohort_id": cid, "quantity": n, "weight_g": w})
    state.hypothetical_events.append(
        {"type": "sale", "date": sale_date.isoformat(), "price": price,
         "allocations": applied})
    return applied


def schedule_sale(state, allocations: list, price: float, sale_date: date,
                  weight_g: float | None = None) -> list:
    """ثبت فروش آینده به‌صورت event بدون کاهش موجودی امروز."""
    if getattr(state, "hypothetical", False) is not True:
        raise RuntimeError("فروش فرضی فقط روی وضعیت clone شده مجاز است")
    if sale_date <= state.as_of:
        raise ValueError("schedule_sale فقط برای تاریخ آینده است")
    applied = []
    for a in allocations:
        cid, qty = a["cohort_id"], float(a["quantity"])
        c = state.cohorts.get(cid)
        if not c:
            raise ValueError(f"cohort ناشناخته: {cid}")
        if qty > c.alive + 1e-6:
            raise ValueError(f"تعداد فروش آینده از موجودی {cid} بیشتر است")
        applied.append({"cohort_id": cid, "quantity": qty,
                        "weight_g": weight_g})
    state.hypothetical_events.append(
        {"type": "scheduled_sale", "date": sale_date.isoformat(),
         "price": float(price), "weight_g": weight_g, "allocations": applied})
    return applied


def timed_sale_cash_adjustment_rows(A, qty: float, price: float, sale_date: date,
                                    as_of: date, payment_terms: dict | None) -> list:
    """
    اصلاح شرایط وصول برای فروش آینده. درآمد فروش داخل profile ثبت می‌شود و
    PlannedCashLedger به‌طور پیش‌فرض شرایط مشتری را اعمال می‌کند؛ این ردیف‌ها
    فقط در صورت شرایط سفارشی، وصول پیش‌فرض را خنثی و شرایط آفر را جایگزین می‌کنند.
    تاریخ تعهد به ابتدای اولین هفته‌ای گرد می‌شود که قبل از delivery نیست.
    """
    if not payment_terms:
        return []
    days = max(0, (sale_date - as_of).days)
    wk = (days + 6) // 7
    accrual = as_of + timedelta(days=7 * wk)
    default_up = float(A.get("finance.customer_upfront_share"))
    default_delay = int(A.get("finance.customer_balance_delay_days"))
    total = qty * price
    rows = []
    if default_up > 0:
        rows.append({"date": accrual.isoformat(), "amount": -total * default_up,
                     "type": "scheduled_sale_terms_reversal",
                     "label": "برگشت وصول پیش‌فرض فروش زمان‌دار"})
    if default_up < 1:
        rows.append({"date": (accrual + timedelta(days=default_delay)).isoformat(),
                     "amount": -total * (1-default_up),
                     "type": "scheduled_sale_terms_reversal",
                     "label": "برگشت مانده وصول پیش‌فرض فروش زمان‌دار"})
    rows += sale_cash_rows(A, qty, price, accrual, payment_terms,
                           label="وجه فروش زمان‌دار")
    return rows


def sale_cash_rows(A, qty: float, price: float, sale_date: date,
                   payment_terms: dict | None = None, label: str = "وجه آفر فروش") -> list:
    """
    دریافت وجه فروش طبق شرایط پرداخت (پیش‌فرض: ۵۰٪ نقد، ۵۰٪ حدود ۴۵ روز بعد).
    """
    terms = payment_terms or {}
    up = float(terms.get("upfront_share", A.get("finance.customer_upfront_share")))
    delay = int(terms.get("delay_days", A.get("finance.customer_balance_delay_days")))
    total = qty * price
    rows = []
    if up > 0:
        rows.append({"date": sale_date.isoformat(), "amount": total * up,
                     "type": "receipt_upfront", "label": f"{label} ({up:.0%} نقد)"})
    if up < 1:
        rows.append({"date": (sale_date + timedelta(days=delay)).isoformat(),
                     "amount": total * (1 - up), "type": "receipt_balance",
                     "label": f"{label} (+{delay} روز)"})
    return rows


def egg_payment_rows(A, qty: float, price: float, purchase_date: date,
                     payment_terms: dict | None = None) -> list:
    """
    اصلاح زمان پرداخت تخم برای یک آفر مشخص (اصلاح ۵).

    دفتر نقدی برنامه، پرداخت تخم را با اعتبار پیش‌فرض تأمین‌کننده ثبت می‌کند.
    اگر این آفر شرایط دیگری داشته باشد، همان مبلغ در تاریخ پیش‌فرض برگردانده
    و در تاریخ‌های واقعی آفر دوباره پرداخت می‌شود. جمع اسمی تغییر نمی‌کند،
    فقط **زمان** آن — و همین در ارزش امروز و در اوج نیاز نقدی دیده می‌شود.
    """
    terms = payment_terms or {}
    if not terms:
        return []
    default_days = int(A.get("finance.supplier_credit_days"))
    up = float(terms.get("upfront_share", 1.0))
    delay = int(terms.get("delay_days", terms.get("credit_days", 0)))
    total = qty * price
    if up >= 0.999 and delay == default_days:
        return []
    rows = [{"date": (purchase_date + timedelta(days=default_days)).isoformat(),
             "amount": +total, "type": "egg_terms_reversal",
             "label": "برگشت پرداخت تخم با شرایط پیش‌فرض"}]
    if up > 0:
        rows.append({"date": purchase_date.isoformat(), "amount": -total * up,
                     "type": "egg_payment", "label": f"پرداخت تخم ({up:.0%} نقد)"})
    if up < 1:
        rows.append({"date": (purchase_date + timedelta(days=delay)).isoformat(),
                     "amount": -total * (1 - up), "type": "egg_payment",
                     "label": f"پرداخت تخم (+{delay} روز)"})
    return rows


def payment_pv_factor(A, terms: dict | None, default_upfront: float | None = None,
                      default_delay: int | None = None) -> dict:
    """ضریب ارزش امروزِ یک قیمت اسمی با شرایط پرداخت مشخص."""
    terms = terms or {}
    up = float(terms.get("upfront_share",
                         default_upfront if default_upfront is not None
                         else A.get("finance.customer_upfront_share")))
    delay = int(terms.get("delay_days", terms.get("credit_days",
                default_delay if default_delay is not None
                else A.get("finance.customer_balance_delay_days"))))
    rate = float(A.get("offers.opportunity_rate_annual"))
    factor = up + (1 - up) / (1 + rate * delay / 365.0)
    return {"upfront_share": up, "delay_days": delay,
            "opportunity_rate_annual": rate, "present_value_factor": factor}


def discount_factor(A, days: float) -> float:
    r = float(A.get("offers.opportunity_rate_annual"))
    return 1.0 / (1.0 + r * max(0.0, days) / 365.0)


# ═══════════════════════════════════════ آفر چند-cohort (اصلاح ۴)
def cohort_availability(A, bio, state, weight_g: float | None,
                        on: date | None = None, tolerance: float | None = None) -> list:
    """
    cohortهایی که می‌توانند ماهی در وزن درخواستی تأمین کنند.

    مشتری «۸۰٬۰۰۰ قطعه حدود ۳ گرم» می‌خواهد، نه «cohort C07». چون وزن داخل
    هر cohort توزیع دارد، سهم قابل تأمین هر cohort از روی همان توزیع
    لاگ‌نرمال محاسبه می‌شود: چه کسری از ماهی‌های این cohort در بازه وزنی
    درخواستی قرار می‌گیرند.

    اگر وزن مشخص نشده باشد، کل موجودی هر cohort قابل تأمین است.
    """
    on = on or state.as_of
    tol = float(tolerance if tolerance is not None
                else A.get("offers.sale_weight_tolerance"))
    min_share = float(A.get("offers.min_cohort_share"))
    out = []
    for c in state.cohorts.values():
        if c.alive < 1:
            continue
        w = state.weight_of(c, on) if on != state.as_of else c.mean_weight
        cv = state.cv_of(c, w)
        if weight_g is None:
            share, lo, hi = 1.0, None, None
        else:
            lo, hi = weight_g * (1 - tol), weight_g * (1 + tol)
            share = max(0.0, bio.fraction_above(w, lo, cv)
                        - bio.fraction_above(w, hi, cv))
        avail = c.alive * share
        out.append({
            "cohort_id": c.cohort_id, "alive": c.alive,
            "mean_weight_g": w, "cv": cv,
            "weight_window": [lo, hi] if weight_g is not None else None,
            "fraction_in_window": share,
            "available": avail,
            "eligible": share >= min_share and avail >= 1,
            "weight_gap": abs(w - weight_g) if weight_g is not None else 0.0,
            "age_days": (on - c.purchase_date).days,
        })
    out.sort(key=lambda r: (not r["eligible"], r["weight_gap"], -r["available"]))
    return out


def suggest_allocation(A, bio, state, quantity: float, weight_g: float | None,
                       on: date | None = None, tolerance: float | None = None,
                       rank_fn=None) -> dict:
    """
    پیشنهاد تقسیم یک آفر بین چند cohort.

    ترتیب اولویت با `rank_fn` تعیین می‌شود (پیش‌فرض: نزدیک‌ترین وزن، سپس
    بیشترین موجودی). کاربر می‌تواند این پیشنهاد را قبل از ثبت فروش واقعی
    ویرایش کند؛ سیستم فقط اعتبارسنجی می‌کند.
    """
    cands = cohort_availability(A, bio, state, weight_g, on, tolerance)
    elig = [c for c in cands if c["eligible"]]
    if rank_fn:
        elig.sort(key=rank_fn)
    alloc, remaining = [], float(quantity)
    for c in elig:
        if remaining <= 1e-6:
            break
        take = min(remaining, c["available"])
        if take < 1:
            continue
        alloc.append({"cohort_id": c["cohort_id"], "quantity": take,
                      "mean_weight_g": c["mean_weight_g"],
                      "share_of_cohort": take / c["alive"] if c["alive"] else 0.0})
        remaining -= take
    return {"allocations": alloc, "candidates": cands,
            "requested": float(quantity),
            "allocated": float(quantity) - remaining,
            "shortfall": max(0.0, remaining),
            "feasible": remaining <= 1e-6}


def validate_allocation(state, allocations: list, quantity: float) -> list:
    """
    قواعد سخت: مجموع تخصیص = تعداد آفر، و هیچ cohort بیش از موجودی خود.
    """
    if not allocations:
        raise ValueError("تخصیص cohort خالی است")
    clean = []
    for a in allocations:
        cid = a.get("cohort_id")
        q = float(a.get("quantity") or 0)
        c = state.cohorts.get(cid)
        if not c:
            raise ValueError(f"cohort ناشناخته: {cid}")
        if q <= 0:
            continue
        if q > c.alive + 1e-6:
            raise ValueError(
                f"تخصیص {q:,.0f} قطعه از {cid} از موجودی آن ({c.alive:,.0f}) بیشتر است")
        clean.append({"cohort_id": cid, "quantity": q})
    total = sum(a["quantity"] for a in clean)
    if abs(total - float(quantity)) > 1.0:
        raise ValueError(
            f"مجموع تخصیص‌ها ({total:,.0f}) با تعداد آفر ({float(quantity):,.0f}) برابر نیست")
    return clean


# ═══════════════════════════════════════════════════ ابزارهای کمکی
def as_date(x, default: date) -> date:
    return d(x) if x else default
