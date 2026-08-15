"""
variance.py — Target vs Actual (مرحله ۲)
==========================================
سه ستون در برابر هم:

  Original Plan          برنامه‌ای که یک بار ذخیره شده و دیگر تغییر نمی‌کند
  Current Re-Optimised   برنامه‌ای که همین حالا از وضعیت واقعی ساخته شده
  Actual to Date         آنچه واقعاً اتفاق افتاده، از روی transactionها

و ستون چهارم: Variance = Actual − Original برای ماه‌های سپری‌شده.

نکته مهم: «Actual» فقط از تراکنش‌های واقعی می‌آید. هیچ عدد تخمینی یا
برنامه‌ای وارد ستون Actual نمی‌شود.
"""
from __future__ import annotations

from datetime import date

from .state import d


def actual_by_month(db, A, as_of: date) -> dict:
    """تجمیع ماهانه داده واقعی ثبت‌شده تا امروز."""
    out: dict[str, dict] = {}

    def row(m):
        return out.setdefault(m, {
            "key": m, "eggs_purchased": 0.0, "egg_cost": 0.0,
            "fish_sold": 0.0, "revenue": 0.0,
            "feed_purchase_kg": 0.0, "feed_purchase_cost": 0.0,
            "feed_consumed_kg": 0.0, "operating_cost": 0.0,
            "mortality_recorded": 0.0, "sales_by_weight": {},
            "unassigned_sales_fish": 0.0,
        })

    for t in db.active_txns():
        td = d(t["txn_date"])
        if td > as_of:
            continue
        m = t["txn_date"][:7]
        r = row(m)
        q = float(t.get("quantity") or 0)
        amt = float(t.get("amount") or 0)
        typ = t["txn_type"]
        if typ == "egg_purchase":
            r["eggs_purchased"] += q
            r["egg_cost"] += amt
        elif typ == "sale":
            r["fish_sold"] += q
            r["revenue"] += amt
            w = t.get("weight_g")
            key = f"{float(w):g}" if w else "نامشخص"
            r["sales_by_weight"][key] = r["sales_by_weight"].get(key, 0.0) + q
            if not t.get("cohort_id"):
                r["unassigned_sales_fish"] += q
        elif typ == "feed_purchase":
            r["feed_purchase_kg"] += q
            r["feed_purchase_cost"] += amt
        elif typ == "feed_consumption":
            r["feed_consumed_kg"] += q
        elif typ == "operating_cost":
            r["operating_cost"] += amt
        elif typ == "mortality":
            r["mortality_recorded"] += q

    # هزینه ثابت پایه طبق حالت انتخابی
    mode = str(A.get("cost.fixed_cost_mode", "top_up"))
    if mode != "actual_only":
        start = d(A.get("finance.fixed_cost_start_date"))
        y, m = start.year, start.month
        while date(y, m, 1) <= as_of:
            mk = f"{y}-{m:02d}"
            r = row(mk)
            base = float(A.get_at("cost.fixed_monthly", date(y, m, 1)))
            r["fixed_cost"] = (max(0.0, base - r["operating_cost"])
                               if mode == "top_up" else base)
            m += 1
            if m > 12:
                m, y = 1, y + 1
    for r in out.values():
        r.setdefault("fixed_cost", 0.0)
        r["contribution_nominal"] = (r["revenue"] - r["feed_purchase_cost"]
                                     - r["egg_cost"] - r["fixed_cost"]
                                     - r["operating_cost"]
                                     if mode == "actual_only" else
                                     r["revenue"] - r["feed_purchase_cost"]
                                     - r["egg_cost"] - r["fixed_cost"])
    return out


FIELDS = [
    ("eggs_purchased", "خرید تخم", "عدد"),
    ("egg_cost", "هزینه تخم", "تومان"),
    ("fish_sold", "فروش ماهی", "قطعه"),
    ("revenue", "درآمد", "تومان"),
    ("feed_purchase_kg", "خرید خوراک", "kg"),
    ("feed_purchase_cost", "هزینه خوراک", "تومان"),
    ("fixed_cost", "هزینه ثابت", "تومان"),
    ("contribution_nominal", "حاشیه اسمی", "تومان"),
]


def build(db, A, as_of: date, current_plan, original: dict | None) -> dict:
    """
    مقایسه سه‌ستونی. `current_plan` یک شیء Plan است، `original` رکورد
    ذخیره‌شده از جدول plans (یا None اگر هنوز baseline ثبت نشده باشد).
    """
    act = actual_by_month(db, A, as_of)
    cur = {b["key"]: b for b in current_plan.monthly}
    orig = {b["key"]: b for b in (original or {}).get("monthly", [])}

    months = sorted(set(act) | set(cur) | set(orig))
    cur_m = as_of.strftime("%Y-%m")
    rows = []
    for m in months:
        a, c, o = act.get(m), cur.get(m), orig.get(m)
        elapsed = m <= cur_m
        row = {"key": m, "elapsed": elapsed, "is_current": m == cur_m, "fields": {}}
        for f, label, unit in FIELDS:
            av = (a or {}).get(f)
            cv = (c or {}).get(f)
            ov = (o or {}).get(f)
            var = None
            if elapsed and av is not None and ov is not None:
                var = av - ov
            row["fields"][f] = {"label_fa": label, "unit": unit,
                                "actual": av, "current_plan": cv,
                                "original_plan": ov, "variance": var,
                                "variance_pct": (var / ov) if (var is not None and ov)
                                else None}
        rows.append(row)

    # جمع دوره سپری‌شده
    totals = {}
    for f, label, unit in FIELDS:
        av = sum((act.get(m) or {}).get(f, 0.0) or 0.0 for m in months if m <= cur_m)
        ov = sum((orig.get(m) or {}).get(f, 0.0) or 0.0 for m in months if m <= cur_m)
        cv = sum((cur.get(m) or {}).get(f, 0.0) or 0.0 for m in months if m <= cur_m)
        totals[f] = {"label_fa": label, "unit": unit, "actual": av,
                     "original_plan": ov, "current_plan": cv,
                     "variance": (av - ov) if orig else None,
                     "variance_pct": ((av - ov) / ov) if (orig and ov) else None}

    return {
        "has_original": bool(original),
        "original_created_at": (original or {}).get("created_at"),
        "original_as_of": (original or {}).get("as_of"),
        "original_variant": (original or {}).get("variant"),
        "current_variant": current_plan.variant.name,
        "as_of": as_of.isoformat(),
        "months": rows,
        "totals_to_date": totals,
        "fields": [{"key": f, "label_fa": l, "unit": u} for f, l, u in FIELDS],
    }
