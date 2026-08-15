"""
attribution.py — تشخیص خودکار cohort یک فروش از روی وزن فروخته‌شده
====================================================================
وقتی یک فروش ثبت شده ولی cohort مبدأ آن مشخص نیست، وزن فروخته‌شده یک
اثر انگشت زمانی است: ماهی ۶ گرمی در ۱۶ مارس یعنی cohortی که حدود ۱۱۷ روز
قبل خریداری شده است.

این ماژول **پیشنهاد** می‌دهد، تصمیم نمی‌گیرد:
  * هر cohort فعال در آن تاریخ امتیازدهی می‌شود
  * دلیل هر امتیاز به زبان ساده گزارش می‌شود
  * کاربر می‌تواند پیشنهاد را بپذیرد، cohort دیگری انتخاب کند، یا رد کند
  * اگر هیچ cohortی قابل قبول نباشد، صریح گفته می‌شود و تاریخ خرید ضمنی
    محاسبه می‌گردد تا کاربر بتواند cohort تاریخی گمشده را ثبت کند

معیارها:
  ۱. تطابق وزن — ماهی با این وزن چقدر در توزیع وزن آن cohort طبیعی است
  ۲. موجودی    — آیا آن cohort در آن تاریخ به اندازه کافی ماهی داشته است
  ۳. سازگاری زمانی — cohort باید قبل از تاریخ فروش خریداری شده باشد

هیچ‌وقت به‌صورت خودکار روی داده اعمال نمی‌شود؛ فقط با تأیید کاربر.
"""
from __future__ import annotations

import math
from datetime import timedelta

from .state import _age, d

# آستانه‌ها (قابل تغییر از config در صورت نیاز)
MIN_SCORE_SUGGEST = 0.15      # زیر این امتیاز، پیشنهاد قابل اتکا نیست
GOOD_SCORE = 0.55             # بالای این امتیاز، پیشنهاد قوی است
GROWTH_TOLERANCE = 1.35       # حداکثر انحراف پذیرفتنی نرخ رشد واقعی از مدل


def _phi(z: float) -> float:
    """تابع توزیع تجمعی نرمال استاندارد."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _two_sided_p(z: float) -> float:
    """چقدر یک مشاهده با این فاصله از میانگین «عادی» است (۱ = دقیقاً میانگین)."""
    return max(0.0, min(1.0, 2.0 * (1.0 - _phi(abs(z)))))


def implied_purchase_date(bio, sale_date, weight_g: float):
    """اگر ماهی در این تاریخ این وزن را داشته، cohort کی خریداری شده است؟"""
    try:
        age = bio.age_at_weight(max(weight_g, 1e-6))
    except Exception:
        return None, None
    return sale_date - timedelta(days=int(round(age))), age


def score_cohort(A, bio, state, c, sale_date, weight_g: float, quantity: float) -> dict:
    """امتیاز یک cohort برای یک فروش مشخص."""
    out = {
        "cohort_id": c.cohort_id,
        "purchase_date": c.purchase_date.isoformat(),
        "egg_count": c.egg_count,
        "eligible": True,
        "reasons": [],
        "blockers": [],
    }
    # ۱) سازگاری زمانی
    if c.purchase_date > sale_date:
        out.update({"eligible": False, "score": 0.0,
                    "expected_weight_g": None, "available_fish": 0.0})
        out["blockers"].append(
            f"در تاریخ فروش هنوز خریداری نشده بود (خرید {c.purchase_date.isoformat()})")
        return out

    age = _age(c.purchase_date, sale_date) + c.growth_offset_days
    exp_w = bio.weight_at_age(age)
    alive = state.alive_on_past(c, sale_date) if sale_date < state.as_of \
        else state.alive_at(c, sale_date)
    out["age_days"] = round(age, 1)
    out["expected_weight_g"] = exp_w
    out["available_fish"] = alive

    # ۲) تطابق وزن در توزیع لاگ‌نرمال همان cohort
    cv = state.cv_of(c, exp_w)
    sigma = math.sqrt(math.log(1.0 + cv * cv))
    mu = math.log(max(exp_w, 1e-9)) - sigma * sigma / 2.0
    z = (math.log(max(weight_g, 1e-9)) - mu) / sigma if sigma > 0 else 0.0
    w_plaus = _two_sided_p(z)
    out["z_score"] = z
    out["weight_plausibility"] = w_plaus

    # سهم cohort که در آن تاریخ در محدوده ±۲۰٪ این وزن است
    lo, hi = 0.8 * weight_g, 1.2 * weight_g
    frac_band = max(0.0, bio.fraction_above(exp_w, lo, cv) -
                    bio.fraction_above(exp_w, hi, cv))
    n_band = alive * frac_band
    out["fraction_in_band"] = frac_band
    out["fish_in_band"] = n_band

    # ۳) ضریب رشد ضمنی — مهم‌ترین تشخیص
    # مدل برای رسیدن به این وزن `needed_age` روز لازم دارد؛ این cohort در آن
    # تاریخ `age` روزه بوده. پس:
    #     gm = needed_age / age  > ۱  ← این cohort سریع‌تر از مدل رشد کرده
    #     gm < ۱                      ← کندتر از مدل رشد کرده
    # نزدیک ۱ یعنی سازگار، دور یعنی عملاً ناممکن.
    needed_age = bio.age_at_weight(max(weight_g, 1e-6))
    gm = (needed_age / age) if age > 0 else None
    out["implied_growth_multiplier"] = gm
    out["actual_age_days"] = round(age, 1)
    out["age_needed_for_weight"] = round(needed_age, 1)

    # ۴) موجودی کافی
    if alive <= 0:
        avail_score = 0.0
        out["blockers"].append("در آن تاریخ ماهی زنده‌ای نداشته است")
    elif alive < quantity:
        avail_score = alive / quantity
        out["blockers"].append(
            f"موجودی آن تاریخ {alive:,.0f} قطعه بوده، کمتر از {quantity:,.0f} قطعه فروش")
    else:
        avail_score = 1.0
        out["reasons"].append(f"موجودی کافی داشته است ({alive:,.0f} قطعه)")

    band_score = min(1.0, n_band / quantity) if quantity > 0 else 0.0

    # سازگاری رشد: چقدر ضریب لازم به ۱ نزدیک است
    if gm is None:
        growth_fit = 0.0
    else:
        growth_fit = max(0.0, 1.0 - abs(math.log(gm)) / math.log(GROWTH_TOLERANCE))
    out["growth_fit"] = growth_fit

    # امتیاز نهایی: تطابق وزن **دروازه** است، نه یک جمع‌شونده.
    # موجودی کافی نباید یک وزن ناممکن را نجات دهد.
    fit = max(w_plaus, growth_fit)
    out["score"] = round(fit * (0.55 + 0.28 * avail_score + 0.17 * band_score), 4)

    # توضیح انسانی
    if abs(z) < 1.5:
        out["reasons"].insert(0, (
            f"وزن {weight_g:g} گرم با وزن مورد انتظار {exp_w:.2f} گرم می‌خواند "
            f"({abs(z):.1f} انحراف معیار)"))
    elif gm and 1 / GROWTH_TOLERANCE <= gm <= GROWTH_TOLERANCE:
        pctd = abs(gm - 1) * 100
        out["reasons"].insert(0, (
            f"وزن مورد انتظار {exp_w:.2f} گرم است، ولی اگر رشد واقعی این cohort "
            f"حدود {pctd:.0f}٪ {'سریع‌تر' if gm > 1 else 'کندتر'} از منحنی مدل "
            f"بوده باشد، وزن {weight_g:g} گرم سازگار است"))
        out["needs_growth_recalibration"] = True
    else:
        out["blockers"].insert(0, (
            f"برای رسیدن به {weight_g:g} گرم حدود {needed_age:.0f} روز سن لازم است، "
            f"ولی این cohort در آن تاریخ فقط {age:.0f} روزه بوده — یعنی رشدی "
            f"{'%.1f برابر' % gm if gm else '—'} سریع‌تر از مدل، که عملاً ناممکن است"))

    if n_band < quantity and alive > 0 and abs(z) < 4:
        out["blockers"].append(
            f"تنها حدود {n_band:,.0f} قطعه از این cohort در آن تاریخ نزدیک "
            f"{weight_g:g} گرم بوده‌اند")
    return out


def suggest(A, bio, state, sale: dict) -> dict:
    """
    پیشنهاد cohort برای یک تراکنش فروش.

    `sale` یک ردیف transactions است. اگر وزن ثبت نشده باشد، تشخیص ممکن
    نیست و صریح گفته می‌شود — وزن حدس زده نمی‌شود.
    """
    sale_date = d(sale["txn_date"])
    qty = float(sale.get("quantity") or 0)
    w = sale.get("weight_g")

    res = {
        "txn_id": sale["id"],
        "sale_date": sale["txn_date"],
        "quantity": qty,
        "weight_g": w,
        "current_cohort_id": sale.get("cohort_id"),
        "candidates": [],
        "best": None,
        "confidence": "none",
        "message_fa": "",
    }
    if not w:
        res["message_fa"] = ("وزن این فروش ثبت نشده است. بدون وزن، تشخیص cohort "
                             "ممکن نیست و وزن حدس زده نمی‌شود — لطفاً وزن واقعی "
                             "را وارد کنید.")
        res["needs_weight"] = True
        return res

    w = float(w)
    ipd, iage = implied_purchase_date(bio, sale_date, w)
    res["implied_purchase_date"] = ipd.isoformat() if ipd else None
    res["implied_age_days"] = round(iage, 1) if iage else None

    uniq = [score_cohort(A, bio, state, c, sale_date, w, qty)
            for c in state.cohorts.values()]
    uniq.sort(key=lambda r: -r["score"])
    res["candidates"] = uniq

    best = uniq[0] if uniq else None
    if best and best["score"] >= GOOD_SCORE and abs(best.get("z_score", 9)) < 1.5:
        res["best"] = best["cohort_id"]
        res["confidence"] = "high"
        res["message_fa"] = (f"محتمل‌ترین مبدأ: {best['cohort_id']} — "
                             + "؛ ".join(best["reasons"][:2]))
    elif best and best["score"] >= MIN_SCORE_SUGGEST:
        res["best"] = best["cohort_id"]
        res["confidence"] = "medium" if best.get("needs_growth_recalibration") else "low"
        res["message_fa"] = (
            f"محتمل‌ترین گزینه {best['cohort_id']} است. "
            + "؛ ".join(best["reasons"][:1] or best["blockers"][:1])
            + ". تأیید این تخصیص یعنی نرخ رشد واقعی با منحنی مدل فرق دارد؛ "
              "می‌توانید بعداً منحنی را در تب فرضیات کالیبره کنید.")
    else:
        res["confidence"] = "none"
        res["message_fa"] = (
            "هیچ cohort ثبت‌شده‌ای با این وزن و تاریخ نمی‌خواند. "
            + (f"ماهی {w:g} گرمی در {sale['txn_date']} یعنی cohortی که حدود "
               f"{ipd.isoformat()} خریداری شده — که در داده‌های ثبت‌شده وجود ندارد. "
               if ipd else "")
            + "به‌احتمال زیاد این فروش از یک cohort قدیمی‌تر است که هنوز ثبت نشده.")
        if ipd:
            res["missing_cohort_hint"] = {
                "purchase_date": ipd.isoformat(),
                "implied_age_days": round(iage, 1),
                "suggested_egg_count": _implied_eggs(bio, iage, qty),
            }
    # تخصیص پیشنهادی بین چند cohort
    res["suggested_split"] = _split(uniq, qty)
    res["split_confidence"] = _split_confidence(res["suggested_split"], uniq)
    if len(res["suggested_split"]) > 1:
        parts = "، ".join(f"{r['cohort_id']}: {r['quantity']:,.0f}"
                          for r in res["suggested_split"])
        res["message_fa"] += (f" این فروش می‌تواند ترکیبی باشد — تقسیم پیشنهادی: {parts}.")
    return res


def _split(cands: list, qty: float) -> list:
    """
    تقسیم پیشنهادی یک فروش بین چند cohort.

    وقتی دو cohort با فاصله خرید کم در تاریخ فروش وزن‌های هم‌پوشان دارند،
    فرض «هر فروش از یک cohort» غلط است. سهم هر cohort متناسب با تعداد
    ماهی‌هایی است که در آن تاریخ واقعاً نزدیک وزن فروخته‌شده بوده‌اند، و
    هرگز از موجودی همان cohort بیشتر نمی‌شود.
    """
    usable = [c for c in cands
              if c.get("eligible") and c["score"] > 0 and c.get("fish_in_band", 0) > 0]
    if not usable:
        return []
    total_band = sum(c["fish_in_band"] for c in usable)
    rows, remaining = [], qty
    for c in sorted(usable, key=lambda r: -r["score"]):
        if remaining <= 1e-6:
            break
        share = qty * (c["fish_in_band"] / total_band) if total_band else 0.0
        take = min(share, c.get("available_fish", 0.0), remaining)
        if take < 1:
            continue
        rows.append({"cohort_id": c["cohort_id"], "quantity": round(take),
                     "confidence": round(c["score"], 4),
                     "expected_weight_g": c.get("expected_weight_g")})
        remaining -= take
    # باقیمانده به بهترین گزینه‌ای که ظرفیت دارد
    if rows and remaining > 1:
        for r in rows:
            c = next(x for x in usable if x["cohort_id"] == r["cohort_id"])
            room = max(0.0, c.get("available_fish", 0.0) - r["quantity"])
            add = min(room, remaining)
            r["quantity"] = round(r["quantity"] + add)
            remaining -= add
            if remaining <= 1:
                break
    if rows:
        diff = qty - sum(r["quantity"] for r in rows)
        if abs(diff) >= 1:
            rows[0]["quantity"] = round(rows[0]["quantity"] + diff)
        rows = [r for r in rows if r["quantity"] > 0]
    return rows


def _split_confidence(split: list, cands: list) -> str:
    if not split:
        return "none"
    total = sum(r["quantity"] for r in split) or 1
    w = sum(r["confidence"] * r["quantity"] for r in split) / total
    if w >= GOOD_SCORE and len(split) == 1:
        return "high"
    if w >= GOOD_SCORE:
        return "medium"
    return "low" if w >= MIN_SCORE_SUGGEST else "none"


def coverage(db, state) -> dict:
    """وضعیت reconciliation موجودی — چقدر از فروش‌ها هنوز تخصیص نیافته است."""
    rows = db.q("SELECT * FROM transactions WHERE txn_type='sale' AND status='active'")
    total = allocated = 0.0
    pending = []
    for t in rows:
        q = float(t.get("quantity") or 0)
        total += q
        if t.get("cohort_id") and t["cohort_id"] in state.cohorts:
            allocated += q
            continue
        a = sum(float(r["quantity"])
                for r in db.sale_allocations(t["id"], basis="confirmed"))
        allocated += min(a, q)
        if a < q - 1e-6:
            pending.append({"txn_id": t["id"], "date": t["txn_date"],
                            "quantity": q, "allocated": a,
                            "unallocated": q - a, "weight_g": t.get("weight_g")})
    return {
        "total_sold_fish": total,
        "allocated_fish": allocated,
        "unallocated_fish": max(0.0, total - allocated),
        "coverage_ratio": (allocated / total) if total else 1.0,
        "pending": pending,
        "reconciled": not pending,
        "status": "reconciled" if not pending else "reconciliation_required",
        "plan_status": "FINAL" if not pending else "PROVISIONAL",
    }


def confirm_split(db, state, txn_id: int, rows: list, reason: str = "") -> dict:
    """
    تأیید تخصیص چند-cohort یک فروش.

    قید سخت: مجموع تخصیص‌ها = تعداد کل فروش، و هیچ cohort بیش از موجودی
    تخمینی خودش در آن تاریخ از دست ندهد.
    """
    t = db.one("SELECT * FROM transactions WHERE id=?", (txn_id,))
    if not t:
        raise KeyError(f"تراکنش یافت نشد: {txn_id}")
    if t["txn_type"] != "sale":
        raise ValueError("تخصیص چند-cohort فقط برای فروش معنا دارد")
    qty = float(t.get("quantity") or 0)

    clean = []
    for r in rows:
        cid = r.get("cohort_id")
        q = float(r.get("quantity") or 0)
        if not cid or q <= 0:
            continue
        if cid not in state.cohorts:
            raise ValueError(f"cohort ناشناخته: {cid}")
        c = state.cohorts[cid]
        sd = d(t["txn_date"])
        avail = state.alive_on_past(c, sd) if sd < state.as_of else c.alive
        if q > avail + 1e-6:
            raise ValueError(
                f"{cid}: تخصیص {q:,.0f} قطعه بیش از موجودی تخمینی آن تاریخ "
                f"({avail:,.0f} قطعه) است")
        clean.append({"cohort_id": cid, "quantity": q,
                      "confidence": r.get("confidence"), "note": r.get("note")})

    tot = sum(r["quantity"] for r in clean)
    if abs(tot - qty) > 1:
        raise ValueError(f"مجموع تخصیص‌ها {tot:,.0f} با تعداد کل فروش "
                         f"{qty:,.0f} برابر نیست")

    db.set_sale_allocations(txn_id, clean, basis="confirmed")
    db.clear_sale_allocations(txn_id, basis="suggested")

    payload = _payload(t)
    payload.update({"cohort_unassigned": False, "multi_cohort": len(clean) > 1,
                    "attribution_method": "split_confirmed",
                    "reconciliation": "resolved"})
    patch = {"payload": payload}
    if len(clean) == 1:
        patch["cohort_id"] = clean[0]["cohort_id"]
    new_id = db.correct_txn(
        txn_id, reason=reason or "تأیید تخصیص فروش بین cohortها", **patch)
    # تخصیص‌ها به نسخه جدید تراکنش منتقل می‌شوند
    db.ex("UPDATE sale_allocations SET txn_id=? WHERE txn_id=?", (new_id, txn_id))
    return {"ok": True, "new_id": new_id, "allocations": clean,
            "total": tot, "sale_quantity": qty}


def _payload(t) -> dict:
    import json
    try:
        return t["payload"] if isinstance(t["payload"], dict) \
            else json.loads(t["payload"] or "{}")
    except Exception:
        return {}


def _implied_eggs(bio, age_days: float, sold_qty: float) -> float:
    """تعداد تخم لازم برای اینکه این تعداد ماهی در این سن زنده مانده باشد."""
    surv = bio.survival(age_days)
    return round(sold_qty / surv, -3) if surv > 0 else sold_qty


def suggest_all(A, bio, state, db) -> list:
    """پیشنهاد برای همه فروش‌های بدون cohort."""
    rows = db.q("SELECT * FROM transactions WHERE txn_type='sale' AND status='active' "
                "AND (cohort_id IS NULL OR cohort_id='') ORDER BY txn_date")
    return [suggest(A, bio, state, r) for r in rows]


#: نگهبان برای تفکیک «تغییر نده» از «پاک کن»
UNSET = object()


def assign(db, txn_id: int, cohort_id=UNSET, weight_g: float | None = None,
           reason: str = "", method: str = "manual") -> dict:
    """
    تخصیص cohort به یک فروش — با حفظ کامل audit trail.

    رکورد قبلی حذف نمی‌شود؛ یک نسخه اصلاح‌شده ثبت می‌شود و نسخه قبلی با
    مقدار اولیه خود (cohort خالی) در تاریخچه می‌ماند. کاربر هر زمان
    می‌تواند دوباره تغییر دهد.
    """
    old = db.one("SELECT * FROM transactions WHERE id=?", (txn_id,))
    if not old:
        raise KeyError(f"تراکنش یافت نشد: {txn_id}")
    if old["txn_type"] != "sale":
        raise ValueError("تخصیص cohort فقط برای تراکنش فروش معنا دارد")
    if old["status"] != "active":
        raise ValueError("این تراکنش فعال نیست")

    patch = {}
    if cohort_id is not UNSET:
        # None یا رشته خالی = پاک کردن تخصیص (بازگشت به Unassigned)
        patch["cohort_id"] = cohort_id or None
        cohort_id = cohort_id or None
    else:
        cohort_id = old.get("cohort_id")
    if weight_g is not None:
        patch["weight_g"] = float(weight_g)

    raw = old.get("payload")
    if isinstance(raw, dict):
        payload = dict(raw)
    else:
        try:
            import json
            payload = json.loads(raw or "{}")
        except Exception:
            payload = {}
    payload.update({
        "cohort_unassigned": not bool(cohort_id),
        "attribution_method": method,          # auto_accepted | manual | cleared
        "weight_known": bool(weight_g or old["weight_g"]),
        "reconciliation": "resolved" if cohort_id else "needs_more_info",
    })
    patch["payload"] = payload

    why = reason or ("تخصیص cohort بر مبنای وزن فروخته‌شده"
                     if cohort_id else "حذف تخصیص cohort")
    new_id = db.correct_txn(txn_id, reason=why, **patch)
    return {"ok": True, "new_id": new_id, "cohort_id": cohort_id,
            "chain": db.txn_chain(new_id)}
