"""
offers.py — موتور تصمیم خرید و فروش (مرحله ۳، نسخه اصلاح‌شده)
================================================================
ابزار عملیاتی برای تصمیم‌های واقعی، نه تحلیل نظری.

**اصل مرکزی:** ارزش یک تصمیم، سود همان ماهی یا همان تخم به‌تنهایی نیست؛
ارزش کل مزرعه **با** آن تصمیم در برابر ارزش کل مزرعه **بدون** آن است:

    وضعیت واقعی → Clone → اعمال تراکنش فرضی → بازمحاسبه وضعیت
        → اجرای دوباره Optimizer → مقایسه با Baseline

بنابراین ظرفیت استخر آزادشده، نقدینگی زودتر، خوراک نخورده، تلفات
رخ‌نداده و تأخیر احتمالی فروش، همه به‌طور خودکار در برنامه جدید ظاهر
می‌شوند — نه به‌صورت یک اصلاح دستی روی baseline.

معیار مقایسه **ارزش امروزِ خالص (NPV)** دفتر نقدی برنامه است، چون
«فروش امروز» و «فروش چهار ماه بعد» دو جریان هم‌ارز نیستند و شرایط
پرداخت هم باید معنا داشته باشد. تنزیل ساده و قابل تنظیم است.

نکته صادقانه: تابع هدف MILP همچنان حاشیه اسمی است. یعنی optimizer
بهترین برنامه را بر مبنای اسمی پیدا می‌کند و ما آن برنامه را با NPV
ارزش‌گذاری می‌کنیم. برای مقایسه دو سناریو کافی است، ولی «بهینه دقیق
NPV» نیست.

## سه قیمت آفر فروش
1. **کف حسابداری** — بهای تمام‌شده تاریخی هر ماهی. زیر آن، زیان دفتری.
2. **کف تصمیم اقتصادی** — قیمتی که در آن «فروش حالا» و «بهترین برنامه
   بدون فروش» از نظر ارزش اقتصادی برابر می‌شوند. با جست‌وجوی عددی روی
   قیمت و اجرای دوباره optimizer پیدا می‌شود، نه با فرمول cost + margin.
3. **قیمت پیشنهادی متقابل** — کف اقتصادی به‌علاوه حاشیه چانه‌زنی.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from .hypothetical import (apply_sale, cohort_availability, discount_factor,
                           egg_payment_rows, integer_ponds_now,
                           payment_pv_factor, sale_cash_rows, schedule_sale, solve_plan,
                           suggest_allocation, timed_sale_cash_adjustment_rows,
                           validate_allocation)
from .planner import Plan
from .plan_model import saleability_band
from .state import _age, d

# کلیدهای منحنی تلفات تجمعی — برای سناریوهای What-If
MORTALITY_KEYS = ["mortality.cum_at_1g", "mortality.cum_at_2g",
                  "mortality.cum_at_5g", "mortality.cum_at_10g",
                  "mortality.cum_at_15g"]


# ═══════════════════════════════════════════════ ارزیابی آفر تخم
def evaluate_egg_offer(A, bio, state, offer: dict, variant: str = "balanced",
                       partial_options: bool = True) -> dict:
    """
    ورودی: {date, quantity, price, supplier?, payment_terms?, expiry?, quality?}
    خروجی: BUY / PARTIAL_BUY / REJECT به‌همراه دلیل عددی.

    **BUY فقط وقتی مجاز است که برنامه حاصل از خرید عملاً اجراشدنی باشد**
    (اصلاح ۵): سود مثبت به‌تنهایی کافی نیست. اگر ظرفیت استخر یا نقدینگی
    اجازه ندهد، یا PARTIAL_BUY پیشنهاد می‌شود یا REJECT.
    """
    qty = float(offer["quantity"])
    price = float(offer["price"])
    odate = d(offer.get("date") or state.as_of.isoformat())
    terms = offer.get("payment_terms") or {}
    if qty <= 0:
        raise ValueError("تعداد آفر باید بزرگ‌تر از صفر باشد")
    if odate < state.as_of:
        raise ValueError("تاریخ آفر در گذشته است")

    base = solve_plan(A, bio, state, variant)
    bd = base.value_digest()
    pay = _egg_terms(A, terms, odate, state.as_of)

    full = _lot_value(A, bio, state, variant, odate, qty, price, terms, bd, pay)

    result = {
        "offer": {"date": odate.isoformat(), "quantity": qty, "price": price,
                  "supplier": offer.get("supplier"),
                  "expiry": offer.get("expiry"),
                  "quality": offer.get("quality"),
                  "payment_terms": terms or None},
        "as_of": state.as_of.isoformat(),
        "variant": variant,
        "payment": pay,
        "baseline": bd,
        "full_accept": full,
        "options": [],
    }

    # گزینه‌های پذیرش جزئی: اندازه‌های واقعی lot + کسرهای ساده از خود آفر
    if partial_options:
        cands = {float(x) for x in A.get("planning.lot_candidates")
                 if 0 < float(x) < qty}
        cands |= {round(qty * f / 1000.0) * 1000.0 for f in (0.75, 0.5, 0.25)}
        cands = sorted({c for c in cands if 1000 <= c < qty}, reverse=True)[:4]
        for q in cands:
            result["options"].append(
                _lot_value(A, bio, state, variant, odate, q, price, terms, bd,
                           _egg_terms(A, terms, odate, state.as_of)))

    all_opts = [full] + result["options"]
    viable = [o for o in all_opts if o["incremental_profit"] > 0 and o["feasible"]]
    profitable = [o for o in all_opts if o["incremental_profit"] > 0]
    best = max(viable, key=lambda o: o["incremental_profit"]) if viable else \
        max(all_opts, key=lambda o: o["incremental_profit"])

    if not viable:
        decision = "REJECT"
        blocker = ("feasibility" if profitable else "price")
    elif best["quantity"] >= qty - 1:
        decision = "BUY"
        blocker = None
    else:
        decision = "PARTIAL_BUY"
        blocker = ("feasibility" if not full["feasible"] else "price")

    result.update({
        "decision": decision,
        "decision_fa": {"BUY": "بخر", "PARTIAL_BUY": "خرید جزئی",
                        "REJECT": "رد کن"}[decision],
        "blocker": blocker,
        "preferred_quantity": best["quantity"] if viable else 0.0,
        "max_justified_price": full["max_justified_price"],
        "expected_profit_impact": best["incremental_profit"] if viable
        else max(o["incremental_profit"] for o in all_opts),
        "feasible": full["feasible"],
        "confidence": _confidence(best, full),
        "explanation_fa": _egg_explanation(decision, result, best, price, blocker),
    })
    return result


def _egg_terms(A, terms: dict, odate: date, as_of: date) -> dict:
    """
    ارزش‌گذاری شرایط پرداخت تأمین‌کننده (اصلاح ۵).

    «۶٬۵۰۰ نقد امروز» و «۶٬۸۰۰ با ۶۰ روز اعتبار» ارزش اقتصادی یکسانی
    ندارند. ضریب ارزش امروز از همان نرخ فرصت و **همان دانه‌بندی هفتگی دفتر
    نقدی** می‌آید تا «حداکثر قیمت توجیه‌پذیر» دقیقاً نقطه بی‌تفاوتی همان
    NPVی باشد که مدل گزارش می‌کند — مدل مالی جدیدی ساخته نشده.
    """
    rate = float(A.get("offers.opportunity_rate_annual"))
    default_credit = int(A.get("finance.supplier_credit_days"))
    up = float((terms or {}).get("upfront_share", 1.0))
    delay = int((terms or {}).get("delay_days",
                (terms or {}).get("credit_days", default_credit)))

    def grid_days(day: date) -> int:
        # دفتر نقدی تاریخ‌ها را به شبکه هفتگی می‌برد (index_of ÷۷)
        return 7 * max(0, (day - as_of).days // 7)

    def df_of(day: date) -> float:
        return 1.0 / (1.0 + rate * grid_days(day) / 365.0)

    cost_factor = up * df_of(odate + timedelta(days=default_credit if not terms
                                               else 0)) \
        + (1 - up) * df_of(odate + timedelta(days=delay))
    # ضریب گزارشی «ارزش امروزِ قیمت اسمی» نسبت به پرداخت نقد در تاریخ آفر:
    base_df = df_of(odate + timedelta(days=default_credit))
    pv_factor = cost_factor / base_df if base_df else 1.0
    return {"upfront_share": up, "delay_days": delay,
            "opportunity_rate_annual": rate,
            "present_value_factor": min(1.0, pv_factor),
            "purchase_discount_factor": base_df,
            "cost_factor": cost_factor}


def _lot_value(A, bio, state, variant, odate, qty, price, terms, bd, pay) -> dict:
    """
    ارزش یک lot: برنامه **با** آن در برابر برنامه بدون آن.

    نکته معماری: قیمت تخم، انتخاب بهینه را عوض نمی‌کند (برای گروه اجباری یک
    جابه‌جایی ثابت است و سرمایه پروفایل هم به قیمت آفر وابسته نیست). پس مدل
    فقط **یک بار با قیمت صفر** حل می‌شود و پرداخت واقعی آفر — با شرایط
    اعلام‌شده — «پس از حل» وارد دفتر نقدی می‌گردد (همان فلسفه Post-Solve
    Ledger). نتیجه:
      * «حداکثر قیمت توجیه‌پذیر» دقیقاً نقطه بی‌تفاوتی NPV است،
      * دو قیمت مختلف دو جواب CBC متفاوت (alternate optima) نمی‌گیرند،
      * feasibility نقدی با پرداخت واقعی سنجیده می‌شود.
    """
    from .plan_cash import PlannedCashLedger

    with_lot = solve_plan(A, bio, state, variant,
                          extra_lots=[{"date": odate, "quantity": qty,
                                       "price": 0.0}])
    base = solve_plan(A, bio, state, variant)

    # پرداخت واقعی آفر، پس از حل، در دفتر نقدی (lot داخل مدل قیمت صفر دارد)
    pay_rows = _egg_offer_pay_rows(A, qty, price, odate, terms)
    ledger = PlannedCashLedger(with_lot, with_lot.extra_cash_rows + pay_rows)
    cm = ledger.metrics()

    npv_with = ledger.npv()
    inc = npv_with - bd["npv"]                      # سود افزوده به ارزش امروز
    egg_pv = qty * price * pay["cost_factor"]
    gross_pv = inc + egg_pv                         # ارزش ناخالص lot
    max_price = gross_pv / (qty * pay["cost_factor"]) if qty else 0.0

    wd = with_lot.value_digest()
    inc_nominal = (wd["contribution_nominal"] - bd["contribution_nominal"]) \
        - qty * price
    inc_adverse = (wd["contribution_risk_adjusted"]
                   - bd["contribution_risk_adjusted"]) - qty * price

    # digest سناریو با پرداخت واقعی (برای feasibility و گزارش)
    wd = {**wd,
          "npv": npv_with,
          "contribution_nominal": wd["contribution_nominal"] - qty * price,
          "contribution_risk_adjusted":
              wd["contribution_risk_adjusted"] - qty * price,
          "egg_cost": wd["egg_cost"] + qty * price,
          "peak_funding": cm["peak_funding_requirement"],
          "minimum_cash_balance": cm["minimum_cash_balance"],
          "wc_breach": cm["wc_breach"]}

    feasible = _is_feasible(wd, bd)
    lot = _forced_lot_facts(with_lot)
    return {
        "quantity": qty, "price": price,
        "egg_cost": qty * price,
        "egg_cost_present_value": egg_pv,
        "gross_value": gross_pv,
        "incremental_profit": inc,
        "incremental_profit_nominal": inc_nominal,
        "incremental_profit_adverse": inc_adverse,
        "max_justified_price": max_price,
        "margin_per_egg": max_price - price,
        "expected_survival": lot.get("lot_survival", 0.0),
        "revenue_per_egg": (lot.get("lot_revenue", 0.0) / qty) if qty else 0.0,
        "rolling_12m_delta": wd["rolling_12m"] - bd["rolling_12m"],
        "feed_kg_delta": sum(with_lot.feed_kg) - sum(base.feed_kg),
        "feed_cost_delta": wd["feed_cost"] - bd["feed_cost"],
        "fish_sold_delta": wd["fish_sold"] - bd["fish_sold"],
        "revenue_delta": wd["revenue"] - bd["revenue"],
        "peak_ponds_before": bd["peak_ponds"], "peak_ponds_after": wd["peak_ponds"],
        "extra_ponds": wd["peak_ponds"] - bd["peak_ponds"],
        "peak_funding_before": bd["peak_funding"],
        "peak_funding_after": wd["peak_funding"],
        "extra_funding": wd["peak_funding"] - bd["peak_funding"],
        "wc_available": bd["wc_available"],
        "wc_headroom_after": bd["wc_available"] - wd["peak_funding"],
        "pond_feasible": wd["pond_feasible"],
        "pond_shortfall": wd["pond_shortfall"],
        "wc_breach": wd["wc_breach"],
        "feasible": feasible,
        "infeasible_reason_fa": _infeasible_reason(wd, bd) if not feasible else None,
        "capacity_curve_delta": _capacity_delta(base, with_lot, state),
        "capacity_risk": ("high" if not feasible else
                          "medium" if wd["peak_ponds"] > bd["peak_ponds"] else "low"),
        **lot,
    }


def _egg_offer_pay_rows(A, qty: float, price: float, odate: date,
                        terms: dict | None) -> list:
    """
    ردیف‌های پرداخت واقعی آفر تخم برای دفتر نقدی پس از حل.

    lot داخل مدل با قیمت صفر است، پس اینجا کل پرداخت — با شرایط اعلام‌شده یا
    اعتبار پیش‌فرض تأمین‌کننده — ثبت می‌شود.
    """
    if qty <= 0 or price <= 0:
        return []
    t = terms or {}
    default_credit = int(A.get("finance.supplier_credit_days"))
    up = float(t.get("upfront_share", 1.0))
    delay = int(t.get("delay_days", t.get("credit_days", default_credit)))
    if not t:
        up, delay = 1.0, default_credit
    total = qty * price
    rows = []
    if up > 0:
        rows.append({"date": (odate + timedelta(days=default_credit if not t
                                                else 0)).isoformat(),
                     "amount": -total * up, "type": "egg_payment",
                     "label": f"پرداخت تخم آفر ({up:.0%} نقد)"})
    if up < 1:
        rows.append({"date": (odate + timedelta(days=delay)).isoformat(),
                     "amount": -total * (1 - up), "type": "egg_payment",
                     "label": f"پرداخت تخم آفر (+{delay} روز)"})
    return rows


def _is_feasible(wd: dict, bd: dict) -> bool:
    """
    Hard Feasibility (اصلاح ۵).

    خرید فقط وقتی مجاز است که برنامه حاصل، از نظر ظرفیت استخر و نقدینگی
    اجراشدنی باشد. اگر خودِ وضعیت فعلی مزرعه از قبل نقض دارد، معیار به
    «بدتر نکردن» تبدیل می‌شود — وگرنه تا اصلاح وضعیت موجود هیچ خریدی
    ممکن نمی‌شد.
    """
    ok_pond = wd["pond_feasible"] or \
        wd["pond_shortfall"] <= bd["pond_shortfall"] + 1e-6
    ok_cash = (not wd["wc_breach"]) or \
        wd["peak_funding"] <= bd["peak_funding"] + 1e-6
    return bool(ok_pond and ok_cash)


def _infeasible_reason(wd: dict, bd: dict) -> str:
    bits = []
    if not (wd["pond_feasible"] or wd["pond_shortfall"] <= bd["pond_shortfall"] + 1e-6):
        bits.append(f"ظرفیت استخر: اوج نیاز {wd['peak_ponds']:.0f} استخر")
    if wd["wc_breach"] and wd["peak_funding"] > bd["peak_funding"] + 1e-6:
        bits.append(f"نقدینگی: اوج نیاز {wd['peak_funding']:,.0f} تومان در برابر "
                    f"سرمایه موجود {wd['wc_available']:,.0f}")
    return " · ".join(bits) or "محدودیت عملیاتی"


def _capacity_delta(base: Plan, withl: Plan, state) -> list:
    """اثر آفر بر ظرفیت در بازه‌های ۳۰/۶۰/۹۰/۱۴۰ روز."""
    out = []
    for days in (30, 60, 90, 140):
        t = base.grid.index_of(state.as_of + timedelta(days=days))
        b = max(base.ponds[:t + 1]) if base.ponds else 0
        w = max(withl.ponds[:t + 1]) if withl.ponds else 0
        out.append({"days": days, "date": base.grid.dates[t].isoformat(),
                    "ponds_before": b, "ponds_after": w, "delta": w - b})
    return out


def _forced_lot_facts(p: Plan) -> dict:
    """
    مشخصات خودِ lot اجباری.

    بقا و فروش از پروفایل همین lot خوانده می‌شود، نه از تفاضل کل برنامه.
    تفاضل کل شامل واکنش optimizer هم هست (مثلاً فروش زودتر یک cohort دیگر
    برای آزادکردن استخر) و می‌تواند حتی منفی شود.
    """
    for k, weight in p.solution.selected.items():
        if "OFFER" not in k:
            continue
        prof = p.cand_base["new_lots"].get(k)
        if not prof:
            continue
        return {
            "harvest_weight": prof.harvest_w,
            "lot_fish_sold": prof.sold_fish * weight,
            "lot_survival": prof.survival_at_harvest,
            "lot_revenue": sum(prof.revenue) * weight,
            "lot_feed_kg": sum(prof.feed_kg) * weight,
            "lot_feed_cost": sum(prof.feed_cost) * weight,
            "lot_peak_ponds": max(prof.ponds) if prof.ponds else 0,
        }
    return {"harvest_weight": None, "lot_fish_sold": 0.0, "lot_survival": 0.0,
            "lot_revenue": 0.0, "lot_feed_kg": 0.0, "lot_feed_cost": 0.0,
            "lot_peak_ponds": 0}


def _confidence(best: dict, full: dict) -> str:
    """اطمینان بر مبنای فاصله از نقطه بی‌تفاوتی و پایداری در سناریوی نامساعد."""
    if best["incremental_profit"] <= 0:
        return "high" if best["margin_per_egg"] < -300 else "medium"
    if not best.get("feasible", True):
        return "low"
    if best["incremental_profit_adverse"] > 0 and best["margin_per_egg"] > 500:
        return "high"
    if best["incremental_profit_adverse"] > 0:
        return "medium"
    return "low"


def _egg_explanation(decision, res, best, price, blocker) -> str:
    f = res["full_accept"]
    mx = f["max_justified_price"]
    pay = res["payment"]
    tail = ""
    if pay["present_value_factor"] < 0.999:
        tail = (f" با شرایط پرداخت اعلام‌شده ({pay['upfront_share']:.0%} نقد و بقیه "
                f"پس از {pay['delay_days']} روز)، قیمت اسمی بالاتری توجیه می‌شود؛ "
                f"ارزش امروزِ هر تخم {price * pay['present_value_factor']:,.0f} تومان است.")
    if decision == "REJECT":
        if blocker == "feasibility":
            return (f"با وجود سود اسمی مثبت، هیچ مقداری از این آفر اجراشدنی نیست — "
                    f"{f['infeasible_reason_fa']}. تا آزاد شدن ظرفیت یا نقدینگی، "
                    f"خرید توصیه نمی‌شود.")
        return (f"با قیمت {price:,.0f} تومان، خرید ارزش امروزِ مزرعه را "
                f"{-best['incremental_profit']:,.0f} تومان کم می‌کند. حداکثر قیمتی "
                f"که این تخم‌ها توجیه می‌کنند {mx:,.0f} تومان است — یعنی "
                f"{price - mx:,.0f} تومان بالاتر از ارزش واقعی آن‌ها." + tail)
    if decision == "PARTIAL_BUY":
        why = (f"محدودیت اجرایی ({f['infeasible_reason_fa']})"
               if blocker == "feasibility" else "صرفه اقتصادی")
        return (f"خرید کامل {f['quantity']:,.0f} عدد توصیه نمی‌شود؛ {why}. "
                f"خرید {best['quantity']:,.0f} عدد ارزش امروزِ مزرعه را "
                f"{best['incremental_profit']:,.0f} تومان بالا می‌برد و برنامه حاصل "
                f"اجراشدنی است." + tail)
    return (f"خرید توصیه می‌شود: ارزش افزوده {best['incremental_profit']:,.0f} تومان "
            f"به ارزش امروز. حداکثر قیمت توجیه‌پذیر {mx:,.0f} تومان است، یعنی تا "
            f"{best['margin_per_egg']:,.0f} تومان بر هر تخم فضای چانه‌زنی دارید. "
            f"اوج نیاز استخر {f['peak_ponds_after']:.0f} و اوج نیاز نقدی "
            f"{f['peak_funding_after']:,.0f} تومان می‌شود." + tail)


# ═══════════════════════════════════════════════ ارزیابی آفر فروش
def evaluate_sale_offer(A, bio, state, offer: dict, variant: str = "balanced",
                        plan: Plan | None = None) -> dict:
    """
    ورودی:
        {quantity, price, cohort_id? | allocations?, weight_g?,
         delivery_date?, payment_terms?}

    اگر `cohort_id` داده نشود، سیستم خودش cohortهای واجد شرایط را از روی
    توزیع وزن پیدا می‌کند و تخصیص پیشنهاد می‌دهد (اصلاح ۴).

    خروجی: سه قیمت مرجع + ACCEPT / NEGOTIATE / REJECT.
    """
    qty = float(offer["quantity"])
    if qty <= 0:
        raise ValueError("تعداد باید بزرگ‌تر از صفر باشد")
    price = float(offer["price"])
    terms = offer.get("payment_terms") or {}
    deliver = d(offer["delivery_date"]) if offer.get("delivery_date") else state.as_of
    if deliver < state.as_of:
        raise ValueError("تاریخ تحویل در گذشته است")

    alloc_info = _resolve_allocation(A, bio, state, offer, qty, deliver)
    allocs = alloc_info["allocations"]
    w_now = alloc_info["weight_g"]

    base = plan or solve_plan(A, bio, state, variant)
    bd = base.value_digest()

    # ---- سناریوی B: پذیرش فروش، روی وضعیت clone شده
    scen = _sale_scenario(A, bio, state, variant, allocs, qty, price, deliver,
                          terms, w_now)
    sd = scen["digest"]

    # ---- کف تصمیم اقتصادی: قیمتی که دو سناریو برابر شوند
    floor = _economic_floor(A, bio, state, variant, allocs, qty, deliver, terms,
                            w_now, bd["npv"], price, sd["npv"])
    econ = floor["price"]

    acc = _accounting_floor(A, bio, state, allocs)
    alt = _best_alternative(A, bio, state, allocs, w_now)

    pay = payment_pv_factor(A, terms)
    margin = float(A.get("offers.negotiation_margin"))
    counter = max(econ, bio.sale_price(w_now)) * (1.0 + margin)

    band = float(A.get("offers.negotiate_band"))
    if price >= econ:
        decision = "ACCEPT"
    elif price >= econ * (1 - band):
        decision = "NEGOTIATE"
    else:
        decision = "REJECT"

    # ---- ظرفیت استخر واقعاً آزادشده (اصلاح ۳)
    ponds_before = integer_ponds_now(state)
    ponds_after = integer_ponds_now(scen["state"])
    wc_release_at_delivery = sum(a["quantity"] for a in allocs) * acc["cost_per_fish"]
    wc_released = wc_release_at_delivery if deliver == state.as_of else 0.0

    diff = sd["npv"] - bd["npv"]
    # قبلاً `qty * price` بی‌قید‌و‌شرط اضافه می‌شد؛ برای تحویل آینده که
    # فروش داخل خود برنامه ثبت می‌شود، این دو‌بار‌شماری بود.
    dm = decision_margin(bd, sd, state,
                         [{"quantity": qty, "price": price, "date": deliver}])
    nominal_effect = dm["expected"]

    return {
        "as_of": state.as_of.isoformat(),
        "method": "re-optimisation",
        "offer": {"cohort_id": offer.get("cohort_id"), "quantity": qty,
                  "price": price, "weight_g": w_now,
                  "delivery_date": deliver.isoformat(),
                  "payment_terms": terms or None},
        "allocation": alloc_info,
        "cohort": alloc_info["primary_cohort"],
        "prices": {
            "offered": price,
            "effective_after_terms": price * pay["present_value_factor"],
            "accounting_floor": acc["cost_per_fish"],
            "economic_floor": econ,
            "economic_floor_cash": econ * pay["present_value_factor"],
            "counter_price": counter,
            "baseline_curve": bio.sale_price(w_now),
        },
        "accounting": acc,
        "alternative": alt,
        "payment": {**pay, "effective_price": price * pay["present_value_factor"],
                    "cost_of_terms": price * (1 - pay["present_value_factor"])},
        "floor_search": floor,
        "scenarios": {
            "keep": {**bd, "label_fa": "ادامه وضعیت فعلی (بدون فروش)"},
            "accept": {**sd, "label_fa": "پذیرش فروش و بهینه‌سازی دوباره"},
        },
        "decision": decision,
        "decision_fa": {"ACCEPT": "بپذیر", "NEGOTIATE": "مذاکره کن",
                        "REJECT": "رد کن"}[decision],
        "difference_vs_keeping": diff,
        "ponds_freed": ponds_before - ponds_after,
        "ponds_before": ponds_before, "ponds_after": ponds_after,
        "peak_ponds_before": bd["peak_ponds"], "peak_ponds_after": sd["peak_ponds"],
        "working_capital_released": wc_released,
        "working_capital": {
            "inventory_capital_released": wc_released,
            "inventory_capital_release_at_delivery": wc_release_at_delivery,
            "release_date": deliver.isoformat(),
            "peak_funding_before": bd["peak_funding"],
            "peak_funding_after": sd["peak_funding"],
            "peak_funding_delta": sd["peak_funding"] - bd["peak_funding"],
            "minimum_cash_before": bd["minimum_cash_balance"],
            "minimum_cash_after": sd["minimum_cash_balance"],
            "wc_available": bd["wc_available"],
            "wc_breach_before": bd["wc_breach"], "wc_breach_after": sd["wc_breach"],
        },
        "feed_cost_avoided": bd["feed_cost"] - sd["feed_cost"],
        "annual_profit_effect": nominal_effect,
        "margin": {
            "expected": dm["expected"],
            "plan_only": dm["plan_only"],
            "transaction_proceeds": dm["transaction_proceeds"],
            "label_fa": "حاشیه مورد انتظار تصمیم",
            "note_fa": ("«حاشیه مورد انتظار» = تغییر حاشیهٔ برنامه + عایدی خود "
                        "این فروش. حاشیهٔ برنامه از همان منطق مشترک بازار "
                        "(احتمال فروش، تخفیف تلاش‌های بعدی، سقف تقاضا و "
                        "carry-over) می‌آید. عدد «فقط برنامه» تشخیصی است و "
                        "پول همین فروش را در خود ندارد."),
        },
        "explanation_fa": _sale_explanation(decision, price, econ, acc, alt, counter,
                                            qty, ponds_before - ponds_after, diff,
                                            pay, sd, bd, alloc_info),
    }


# ------------------------------------------------------ تخصیص چند-cohort
def _resolve_allocation(A, bio, state, offer: dict, qty: float,
                        deliver: date) -> dict:
    """
    تعیین اینکه این تعداد ماهی از کدام cohortها تأمین می‌شود (اصلاح ۴).

    سه حالت:
      * `allocations` صریح از کاربر (پس از ویرایش پیشنهاد) → فقط اعتبارسنجی
      * `cohort_id` مشخص → همان یک cohort
      * هیچ‌کدام → پیشنهاد خودکار بر اساس توزیع وزن هر cohort
    """
    weight_req = float(offer["weight_g"]) if offer.get("weight_g") else None

    if offer.get("allocations"):
        allocs = validate_allocation(state, offer["allocations"], qty)
        source = "user"
        cands = cohort_availability(A, bio, state, weight_req, deliver)
    elif offer.get("cohort_id"):
        cid = offer["cohort_id"]
        c = state.cohorts.get(cid)
        if not c:
            raise ValueError(f"cohort ناشناخته: {cid}")
        if qty > c.alive + 1:
            raise ValueError(f"تعداد درخواستی از موجودی {cid} "
                             f"({c.alive:,.0f} قطعه) بیشتر است")
        allocs = [{"cohort_id": cid, "quantity": qty}]
        source = "single_cohort"
        cands = cohort_availability(A, bio, state, weight_req, deliver)
    else:
        if weight_req is None:
            raise ValueError("برای آفر بدون cohort مشخص، وزن درخواستی لازم است")
        sug = suggest_allocation(A, bio, state, qty, weight_req, deliver)
        if not sug["feasible"]:
            raise ValueError(
                f"موجودی کافی در وزن حدود {weight_req:g} گرم وجود ندارد؛ "
                f"حداکثر {sug['allocated']:,.0f} قطعه از {qty:,.0f} قابل تأمین است")
        allocs = [{"cohort_id": a["cohort_id"], "quantity": a["quantity"]}
                  for a in sug["allocations"]]
        source = "auto"
        cands = sug["candidates"]

    rows = []
    wsum, n = 0.0, 0.0
    for a in allocs:
        c = state.cohorts[a["cohort_id"]]
        w = state.weight_of(c, deliver) if deliver != state.as_of else c.mean_weight
        rows.append({"cohort_id": a["cohort_id"], "quantity": a["quantity"],
                     "mean_weight_g": w, "alive": c.alive,
                     "share_of_cohort": a["quantity"] / c.alive if c.alive else 0.0,
                     "age_days": _age(c.purchase_date, deliver)})
        wsum += a["quantity"] * w
        n += a["quantity"]
    w_eff = weight_req if weight_req is not None else (wsum / n if n else 0.0)

    first = rows[0] if rows else None
    primary = {"cohort_id": first["cohort_id"] if first else None,
               "alive": first["alive"] if first else 0.0,
               "mean_weight_g": first["mean_weight_g"] if first else 0.0,
               "age_days": first["age_days"] if first else 0,
               "share_of_cohort": first["share_of_cohort"] if first else 0.0}

    return {"source": source, "allocations": allocs, "detail": rows,
            "candidates": cands, "weight_g": w_eff,
            "weight_requested": weight_req,
            "quantity_weighted_mean_weight_g": (wsum / n) if n else 0.0,
            "multi_cohort": len(allocs) > 1,
            "primary_cohort": primary,
            "editable": True,
            "note_fa": ("تخصیص پیشنهادی سیستم است و پیش از ثبت فروش واقعی "
                        "قابل ویرایش است." if source == "auto" else
                        "تخصیص اعلام‌شده توسط کاربر." if source == "user" else
                        "آفر روی یک cohort مشخص.")}


# ---------------------------------------------- سناریوی پذیرش فروش
def outside_plan_sale_proceeds(state, sales: list) -> float:
    """
    درآمد اسمی فروش‌های فرضی که **بیرون از سری درآمد برنامه** ثبت می‌شوند.

    این تنها منبع حقیقت برای این سؤال است و هم Offer Evaluator و هم
    What-If از آن استفاده می‌کنند، چون دو مسیر جدا دو جواب متفاوت
    می‌دادند.

    قاعده از خود `_apply_sale_timing` می‌آید و باید دقیقاً با آن هم‌خوان
    بماند:

    * **تحویل امروز** → `apply_sale` موجودی را کم می‌کند و وجه فقط به دفتر
      نقدی می‌رود. برنامه از `as_of` به بعد شبیه‌سازی می‌شود، پس این فروش
      هرگز در `contribution_nominal` برنامه دیده نمی‌شود → باید جداگانه
      شمرده شود.
    * **تحویل در آینده** → `schedule_sale` آن را به‌صورت یک فروش
      اجباری/متعهدشده داخل همان profile می‌گذارد و `_simulate` درآمدش را
      در `p.revenue` ثبت می‌کند → از قبل داخل برنامه هست و شمردن دوباره
      آن، دو‌بار‌شماری است.

    خرید تخم شمرده نمی‌شود: به‌صورت `extra_lots` داخل خود برنامه می‌رود.
    """
    total = 0.0
    for s in sales:
        if s["date"] == state.as_of:
            total += float(s["quantity"]) * float(s["price"])
    return total


def decision_margin(base_digest: dict, scen_digest: dict, state,
                    sales: list) -> dict:
    """
    دو معیار جدا برای «تغییر حاشیه» یک تصمیم — تابع مشترک هر دو مسیر.

    ۱) `expected` — **معیار اصلی تصمیم**. تغییر حاشیهٔ برنامه به‌علاوهٔ
       عایدی خودِ تراکنش. حاشیهٔ برنامه از `Plan.contribution_nominal`
       می‌آید که خودش روی همان منطق مشترک بازار ساخته شده است:
       `saleability_band` (احتمال فروش تلاش اول)، `retry_discount`
       (تخفیف تلاش‌های بعدی)، `max_demand_per_period` (سقف تقاضا) و
       carry-over موجودی فروش‌نرفته — یعنی فروش‌های آیندهٔ برنامه با
       قیمت تعرفهٔ کامل و فروش قطعی ارزش‌گذاری نمی‌شوند.

    ۲) `plan_only` — **فقط تشخیصی**. تغییر خالص حاشیهٔ برنامه، بدون
       عایدی تراکنش. این عدد به‌تنهایی گمراه‌کننده است (فروش امروز
       ماهی‌ها را از برنامه برمی‌دارد ولی پولش را نشان نمی‌دهد)، ولی برای
       دیدن «چقدر از ارزش آیندهٔ برنامه کنار گذاشته شد» مفید است.
    """
    plan_only = scen_digest["contribution_nominal"] - base_digest["contribution_nominal"]
    txn = outside_plan_sale_proceeds(state, sales)
    return {"expected": plan_only + txn,
            "plan_only": plan_only,
            "transaction_proceeds": txn}


def _apply_sale_timing(A, state, allocs, qty, price, deliver, terms, weight_g,
                       label="وجه آفر فروش") -> list:
    """مسیر واحد فروش امروز/آینده برای evaluate_sale_offer و what_if."""
    if deliver == state.as_of:
        # رفتار Stage 3 برای فروش امروز عمداً بدون تغییر می‌ماند.
        apply_sale(state, allocs, price, deliver, weight_g)
        return sale_cash_rows(A, qty, price, deliver, terms, label=label)
    schedule_sale(state, allocs, price, deliver, weight_g)
    return timed_sale_cash_adjustment_rows(
        A, qty, price, deliver, state.as_of, terms)


def _sale_scenario(A, bio, state, variant, allocs, qty, price, deliver, terms,
                   weight_g) -> dict:
    """
    وضعیت مزرعه اگر این فروش پذیرفته شود: clone → کاهش موجودی → وجه فروش
    در دفتر نقدی → بهینه‌سازی دوباره.
    """
    st2 = state.clone()
    rows = _apply_sale_timing(A, st2, allocs, qty, price, deliver, terms, weight_g)
    p2 = solve_plan(A, bio, st2, variant, extra_cash_rows=rows)
    return {"state": st2, "plan": p2, "digest": p2.value_digest(),
            "cash_rows": rows}


def _economic_floor(A, bio, state, variant, allocs, qty, deliver, terms,
                    weight_g, base_npv: float, p0: float, npv0: float) -> dict:
    """
    کف تصمیم اقتصادی = قیمتی که در آن

        ارزش(پذیرش فروش)  ≈  ارزش(بهترین برنامه بدون فروش)

    این عدد از یک فرمول `cost + margin` نمی‌آید؛ با اجرای دوباره optimizer
    روی وضعیت فرضی پیدا می‌شود و بنابراین **هزینه فرصت کل مزرعه** را نشان
    می‌دهد: ظرفیت استخر آزادشده، نقدینگی زودتر، خوراک نخورده، تلفات
    رخ‌نداده و ریسک تأخیر فروش در آینده.

    روش: چون وجه فروش تقریباً خطی وارد ارزش می‌شود، از شیب تحلیلی
    (تعداد × ضریب ارزش امروز) شروع می‌کنیم و اگر باقیمانده از حد دقت
    بیشتر بود، با جست‌وجوی دودویی روی قیمت ادامه می‌دهیم — هر گام یک بار
    اجرای واقعی optimizer.
    """
    pay = payment_pv_factor(A, terms)
    df = discount_factor(A, (deliver - state.as_of).days)
    slope = max(1e-9, qty * pay["present_value_factor"] * df)
    tol = float(A.get("offers.floor_tolerance_per_fish")) * qty
    budget = int(A.get("offers.floor_search_max_solves"))

    def g_at(P: float) -> float:
        s = _sale_scenario(A, bio, state, variant, allocs, qty, P, deliver,
                           terms, weight_g)
        return s["digest"]["npv"] - base_npv

    P, g = float(p0), npv0 - base_npv
    pts = [(P, g)]
    used = 0
    method = "analytic"
    while used < budget and abs(g) > tol:
        neg = [t for t in pts if t[1] < 0]
        pos = [t for t in pts if t[1] > 0]
        if neg and pos:                       # قیمت بی‌تفاوتی محصور شده است
            lo = max(neg, key=lambda t: t[0])
            hi = min(pos, key=lambda t: t[0])
            if hi[0] - lo[0] <= 1.0:
                break
            P2 = 0.5 * (lo[0] + hi[0])
            method = "binary_search"
        else:                                  # گام تحلیلی (سکانت)
            P2 = max(0.0, P - g / slope)
            if abs(P2 - P) < 1.0:
                break
        g2 = g_at(P2)
        used += 1
        pts.append((P2, g2))
        if abs(P2 - P) > 1e-9 and (g2 - g) / (P2 - P) > 0:
            slope = (g2 - g) / (P2 - P)
        P, g = P2, g2

    return {"price": max(0.0, P), "residual_value": g,
            "residual_per_fish": g / qty if qty else 0.0,
            "tolerance_per_fish": float(A.get("offers.floor_tolerance_per_fish")),
            "solver_runs": used + 1, "method": method,
            "probe_points": [{"price": a, "value_gap": b} for a, b in pts],
            "note_fa": ("قیمتی که در آن «فروش حالا» و «بهترین برنامه بدون فروش» "
                        "از نظر ارزش امروز برابر می‌شوند — با اجرای دوباره "
                        "optimizer، نه با فرمول بهای تمام‌شده.")}


def _accounting_floor(A, bio, state, allocs) -> dict:
    """کف حسابداری: بهای تمام‌شده تاریخی هر ماهی زنده (میانگین وزنی چند cohort)."""
    tot_cost, tot_fish, per_cohort = 0.0, 0.0, []
    basis = "actual"
    for a in allocs:
        c = state.cohorts[a["cohort_id"]]
        egg_cost = c.egg_count * c.egg_price
        feed_cost = c.feed_cost_actual or state._est_feed_cost(c)
        total = egg_cost + feed_cost
        per = (total / c.alive) if c.alive else 0.0
        if not c.feed_cost_actual:
            basis = "estimated"
        per_cohort.append({"cohort_id": c.cohort_id, "cost_per_fish": per,
                           "egg_cost_total": egg_cost, "feed_cost_total": feed_cost,
                           "alive": c.alive, "quantity": a["quantity"]})
        tot_cost += per * a["quantity"]
        tot_fish += a["quantity"]
    egg_total = sum(x["egg_cost_total"] for x in per_cohort)
    feed_total = sum(x["feed_cost_total"] for x in per_cohort)
    return {"egg_cost_total": egg_total, "feed_cost_total": feed_total,
            "total_cost": egg_total + feed_total,
            "alive": sum(x["alive"] for x in per_cohort),
            "cost_per_fish": (tot_cost / tot_fish) if tot_fish else 0.0,
            "by_cohort": per_cohort, "basis": basis}


def _best_alternative(A, bio, state, allocs, w_now: float) -> dict:
    """
    نمای تشخیصی «نگه‌داشتن تا وزن‌های بالاتر»، به ازای هر ماهی.

    این عدد **کف تصمیم را تعیین نمی‌کند** — کف واقعی از بهینه‌سازی دوباره
    می‌آید (که خودش از همین `saleability_band` مشترک تغذیه می‌شود). اینجا
    فقط برای این است که مدیر ببیند رشد تا هر وزن چه چیزی اضافه یا کم
    می‌کند — و باید همان «Market Saleability» را ببیند، نه یک عدد جدا:

        ارزش مورد انتظار = بقا × قیمت × (تلاش اول با قیمت کامل +
                            تلاش‌های بعدی با تخفیف بازه) − هزینه خوراک

    فروش امروز (`sell_now`) قطعی است (آفر واقعی، احتمال ۱، تأخیر صفر)؛
    فروش در وزن‌های آینده فقط پیش‌بینی است و با همین مکانیزم، عدم‌قطعیت
    بازار به‌جای مقایسه با اطمینان برابر، صریحاً در ارزش امروز آن اثر
    می‌گذارد.
    """
    c = state.cohorts[allocs[0]["cohort_id"]]
    age_now = _age(c.purchase_date, state.as_of) + c.growth_offset_days
    qty_total = sum(float(a["quantity"]) for a in allocs)
    opts = []
    for w in sorted(float(x) for x in A.get("planning.harvest_weights")):
        if w <= w_now + 1e-9:
            continue
        age_t = bio.age_at_weight(w)
        grow_days = max(0.0, age_t - age_now)
        band = saleability_band(A, w)
        # تلاش اول با قیمت کامل (احتمال هرگز مستقیم در قیمت ضرب نمی‌شود)؛
        # اگر آن تلاش موفق نشود، فروش بعدی به‌اندازه retry_discount همان
        # بازه تخفیف می‌خورد — «possible discount required for
        # slow-moving weights».
        expected_price_factor = 1.0 - (1.0 - band["prob"]) * band["retry_discount"]
        # اگر سقف تقاضای این بازه محدود است، جابه‌جاکردن یک دستهٔ بزرگ
        # واقعاً زمان بیشتری از میانگین می‌برد — همان عدد مشترکی که
        # simulate واقعی هم استفاده می‌کند، نه یک فرض جدا.
        cap_week = band["demand_cap_per_week"]
        demand_days = (qty_total / cap_week * 7.0) if cap_week else 0.0
        total_days = grow_days + max(band["expected_days_to_sale"], demand_days)
        surv = bio.survival_ratio(age_now, age_t)
        feed = bio.feed_cost_per_gram_gain(w_now, w) * (w - w_now)
        gross = surv * bio.sale_price(w) * expected_price_factor
        value = (gross - feed) * discount_factor(A, total_days)
        opts.append({"harvest_w": w, "days_to_reach": round(grow_days, 1),
                     "expected_days_to_sale": round(total_days - grow_days, 1),
                     "survival": surv, "feed_cost_per_fish": feed,
                     "gross_per_fish": gross,
                     "value_per_fish": value,
                     "saleability": band["prob"],
                     "saleability_label": band["label"],
                     "demand_cap_per_period": band["max_demand_per_period"],
                     "ready_date": (state.as_of + timedelta(days=int(grow_days))).isoformat()})
    sell_now = {"harvest_w": w_now, "days_to_reach": 0.0,
                "expected_days_to_sale": 0.0,
                "survival": 1.0, "feed_cost_per_fish": 0.0,
                "gross_per_fish": bio.sale_price(w_now),
                "value_per_fish": bio.sale_price(w_now),
                "saleability": 1.0, "saleability_label": "آفر تأییدشده — قطعی",
                "ready_date": state.as_of.isoformat(), "is_sell_now": True}
    best = max(opts + [sell_now], key=lambda o: o["value_per_fish"])
    return {**best, "options": opts + [sell_now],
            "note_fa": ("نمای تشخیصی: ارزش امروزِ مورد انتظار نگه‌داشتن تا وزن هدف، "
                        "با همان مدل سهولت فروش (احتمال تلاش اول + تخفیف تلاش‌های "
                        "بعدی + تأخیر مورد انتظار) که در بهینه‌سازی اصلی مزرعه هم "
                        "استفاده می‌شود. هزینه گذشته وارد نمی‌شود چون sunk است. "
                        "کف تصمیم نهایی از بهینه‌سازی دوباره کل مزرعه می‌آید، نه "
                        "از این جدول.")}


def _sale_explanation(decision, price, econ, acc, alt, counter, qty, ponds,
                      diff, pay, sd, bd, alloc_info) -> str:
    bits = []
    if alloc_info["multi_cohort"]:
        bits.append("تأمین از " + " و ".join(
            f"{r['cohort_id']} ({r['quantity']:,.0f} قطعه)"
            for r in alloc_info["detail"]))
    if pay["present_value_factor"] < 0.999:
        bits.append(f"شرایط پرداخت ({pay['upfront_share']:.0%} نقد و بقیه پس از "
                    f"{pay['delay_days']} روز) در همین کف لحاظ شده است")
    if ponds > 0:
        bits.append(f"{ponds:.0f} استخر واقعاً آزاد می‌شود")
    fund_delta = sd["peak_funding"] - bd["peak_funding"]
    if abs(fund_delta) > 1:
        bits.append(f"اوج نیاز نقدی {abs(fund_delta):,.0f} تومان "
                    + ("کم" if fund_delta < 0 else "زیاد") + " می‌شود")
    tail = ("‌ " + " · ".join(bits) + ".") if bits else ""

    if decision == "REJECT":
        return (f"قیمت {price:,.0f} تومان زیر کف تصمیم اقتصادی {econ:,.0f} تومان است. "
                f"پذیرش این آفر ارزش امروزِ مزرعه را {-diff:,.0f} تومان کم می‌کند؛ "
                f"بهترین برنامه بدون فروش بهتر است." + tail)
    if decision == "NEGOTIATE":
        return (f"قیمت نزدیک کف اقتصادی {econ:,.0f} تومان است ولی هنوز زیر آن. "
                f"پیشنهاد متقابل {counter:,.0f} تومان بدهید؛ در {econ:,.0f} تومان "
                f"فروش و نگه‌داشتن برابر می‌شوند." + tail)
    return (f"پذیرش توصیه می‌شود: قیمت از کف اقتصادی {econ:,.0f} تومان بالاتر است و "
            f"فروش {qty:,.0f} قطعه ارزش امروزِ مزرعه را {diff:,.0f} تومان بالا "
            f"می‌برد." + tail)


# ═══════════════════════════════════════════════ What-If
def what_if(A, bio, state, changes: list, variant: str = "balanced") -> dict:
    """
    شبیه‌سازی سناریو روی یک **وضعیت واقعیِ clone شده** (اصلاح ۶).

        وضعیت فعلی → Clone → اعمال تراکنش‌های فرضی → بازمحاسبه وضعیت
            → اجرای دوباره Optimizer → مقایسه با Baseline

    یعنی «اگر امروز ۸۰ هزار ماهی بفروشم» واقعاً موجودی را کم می‌کند، نیاز
    استخر و خوراک آینده را عوض می‌کند، وجه را به دفتر نقدی می‌آورد و برنامه
    را دوباره بهینه می‌کند — نه اینکه فقط یک عدد سود به baseline اضافه شود.

    هر تغییر یکی از این‌هاست:
        {"type": "buy_eggs",  "quantity":…, "price":…, "date":…, "payment_terms":…}
        {"type": "sell_fish", "cohort_id"|"allocations"|"weight_g":…,
                              "quantity":…, "price":…, "date":…}
        {"type": "feed_price_pct",  "value": 20}
        {"type": "mortality_pct",   "value": 5}
        {"type": "sale_price_pct",  "value": -10}
        {"type": "assumption", "key":…, "value":…}

    فرضیات با overlay فقط-در-حافظه تغییر می‌کنند؛ هرگز در پایگاه داده
    نوشته نمی‌شوند و در پایان حتماً برداشته می‌شوند.
    """
    base = solve_plan(A, bio, state, variant)
    b = base.value_digest()

    st2 = state.clone()
    lots, cash_rows, notes, applied = [], [], [], []
    sales_applied: list = []
    overlay: dict = {}
    prev = None
    try:
        for ch in changes:
            t = ch.get("type")
            if t == "buy_eggs":
                odate = d(ch.get("date") or state.as_of.isoformat())
                q, p = float(ch["quantity"]), float(ch.get("price") or 0)
                lots.append({"date": odate, "quantity": q, "price": p})
                cash_rows += egg_payment_rows(A, q, p, odate,
                                              ch.get("payment_terms"))
                notes.append(f"خرید {q:,.0f} تخم با قیمت {p:,.0f} در {odate.isoformat()}")
                applied.append({"type": "buy_eggs", "quantity": q, "price": p,
                                "date": odate.isoformat()})
            elif t == "sell_fish":
                sdate = d(ch.get("date") or state.as_of.isoformat())
                q, p = float(ch["quantity"]), float(ch.get("price") or 0)
                info = _resolve_allocation(A, bio, st2, ch, q, sdate)
                cash_rows += _apply_sale_timing(
                    A, st2, info["allocations"], q, p, sdate, ch.get("payment_terms"),
                    info["weight_g"], label="وجه فروش فرضی")
                notes.append(f"فروش {q:,.0f} قطعه با قیمت {p:,.0f} از "
                             + "، ".join(a["cohort_id"] for a in info["allocations"]))
                applied.append({"type": "sell_fish", "quantity": q, "price": p,
                                "date": sdate.isoformat(),
                                "allocations": info["allocations"]})
                sales_applied.append({"quantity": q, "price": p, "date": sdate})
            elif t in ("feed_price_pct", "mortality_pct", "sale_price_pct"):
                keys, label = {
                    "feed_price_pct": (["feed.price_table"], "قیمت خوراک"),
                    "mortality_pct": (MORTALITY_KEYS, "تلفات"),
                    "sale_price_pct": (["price.base_1g", "price.slope_per_gram"],
                                       "قیمت فروش"),
                }[t]
                pct = float(ch["value"])
                for key in keys:
                    overlay[key] = _scale(A.get(key), pct)
                notes.append(f"{label} {pct:+.0f}٪")
                applied.append({"type": t, "value": pct})
            elif t == "assumption":
                overlay[ch["key"]] = ch["value"]
                notes.append(f"{ch['key']} = {ch['value']}")
                applied.append({"type": "assumption", "key": ch["key"],
                                "value": ch["value"]})
            else:
                raise ValueError(f"نوع سناریو ناشناخته: {t}")

        prev = A.push_overlay(overlay) if overlay else None
        from .biology import Biology
        bio2 = Biology(A, on_date=state.as_of.isoformat()) if overlay else bio
        st2.bio = bio2
        scen = solve_plan(A, bio2, st2, variant, extra_lots=lots,
                          extra_cash_rows=cash_rows, use_cache=not overlay)
        w = scen.value_digest()
    finally:
        if prev is not None or overlay:
            A.pop_overlay(prev or {})

    delta = {k: (w[k] - b[k]) for k in
             ("npv", "contribution_nominal", "contribution_risk_adjusted",
              "rolling_12m", "peak_ponds", "peak_funding", "feed_cost",
              "revenue", "fish_sold", "eggs_planned")}
    # همان تابع مشترکی که Offer Evaluator استفاده می‌کند. بدون این، فروش
    # فرضیِ امروز ماهی‌ها را از برنامه برمی‌داشت ولی پولش هیچ‌جا شمرده
    # نمی‌شد و «تغییر حاشیه» به‌شدت منفی و گمراه‌کننده می‌شد.
    dm = decision_margin(b, w, state, sales_applied)
    delta["margin_expected"] = dm["expected"]
    delta["margin_plan_only"] = dm["plan_only"]
    delta["transaction_proceeds"] = dm["transaction_proceeds"]
    # سازگاری با نسخه قبلی خروجی — `contribution` اکنون معیار تصمیم است
    delta["contribution"] = dm["expected"]
    delta["contribution_adverse"] = delta["contribution_risk_adjusted"] \
        + dm["transaction_proceeds"]
    delta["eggs"] = delta["eggs_planned"]

    live_before = sum(c.alive for c in state.cohorts.values())
    live_after = sum(c.alive for c in st2.cohorts.values())
    forced = [k for k in scen.solution.selected if "OFFER" in k]
    state_delta = {
        "live_fish_before": live_before, "live_fish_after": live_after,
        "live_fish_delta": live_after - live_before,
        "ponds_now_before": integer_ponds_now(state),
        "ponds_now_after": integer_ponds_now(st2),
        "hypothetical_lots": len(lots),
        "hypothetical_lots_in_plan": forced,
        "cash_rows": cash_rows,
    }
    return {"as_of": state.as_of.isoformat(), "changes": changes,
            "changes_fa": notes, "applied": applied,
            "baseline": b, "scenario": w, "delta": delta,
            "state_delta": state_delta,
            "manual_delta": 0.0,      # دیگر لازم نیست؛ همه‌چیز داخل مدل است
            "method": "cloned_state_reoptimisation",
            "verdict_fa": ("این سناریو ارزش امروزِ مزرعه را "
                           f"{abs(delta['npv']):,.0f} تومان "
                           + ("بالا می‌برد." if delta["npv"] >= 0
                              else "پایین می‌آورد.")),
            "note_fa": "هیچ داده واقعی تغییر نکرد؛ سناریو روی نسخه کپی اجرا شد."}


def _scale(value, pct: float):
    f = 1.0 + pct / 100.0
    if isinstance(value, (int, float)):
        return min(0.95, value * f) if 0 < value < 1 else value * f
    if isinstance(value, list):
        out = []
        for row in value:
            if isinstance(row, dict):
                r = dict(row)
                for k in ("price", "cum"):
                    if k in r and isinstance(r[k], (int, float)):
                        r[k] = min(0.99, r[k] * f) if k == "cum" else r[k] * f
                out.append(r)
            elif isinstance(row, (int, float)):
                out.append(row * f)
            else:
                out.append(row)
        return out
    return value
