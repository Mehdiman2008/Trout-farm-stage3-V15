"""
pond_alloc.py — تخصیص خودکار استخر (Suggested → Actual)
=========================================================
وقتی یک cohort از ۱ گرم عبور می‌کند دیگر در تراف نمی‌ماند و به grow-out pond
نیاز دارد. به‌جای اینکه سیستم فقط هشدار بدهد، یک **تخصیص پیشنهادی** می‌سازد:

    C04 → P07 (۱۳٬۵۰۰) · P08 (۱۳٬۵۰۰) · P09 (۱۳٬۰۰۰)

اما تا وقتی مدیر تأیید نکرده، این تخصیص `Suggested / Estimated` است و
هرگز Actual تلقی نمی‌شود. با تأیید، تراکنش `transfer` ساخته می‌شود و از آن
لحظه تخصیص واقعی است.

کاربر می‌تواند: تأیید کند، ویرایش کند، بین چند استخر تقسیم کند، یا ماهی را
به استخر دیگری منتقل کند.

سیاست‌ها:
  * استخرهای رزرو (P20/P21) به‌صورت پیش‌فرض دست‌نخورده می‌مانند
  * ظرفیت هر استخر از منحنی تجربی همان وزن می‌آید
  * اگر استخر آزاد کافی نباشد، هشدار ظرفیت صریح ساخته می‌شود — پنهان نمی‌شود
"""
from __future__ import annotations

import math

from .state import TROUGH, UNASSIGNED


def _free_ponds(db, state, include_reserve: bool = False) -> list:
    """استخرهایی که هیچ cohort واقعی در آن‌ها نیست."""
    used = set()
    for c in state.cohorts.values():
        for pid, n in c.alloc.items():
            if pid not in (TROUGH, UNASSIGNED) and n >= 1:
                used.add(pid)
    out = []
    for p in db.ponds():
        if p["pond_id"] in used:
            continue
        if p["role"] != "operational" and not include_reserve:
            continue
        out.append(p["pond_id"])
    return out


def needs_pond(bio, c) -> bool:
    """آیا این cohort از مرحله تراف عبور کرده و به استخر نیاز دارد؟"""
    return (c.alive >= 1
            and bio.counts_toward_pond_capacity(c.mean_weight)
            and c.alloc.get(TROUGH, 0.0) + c.alloc.get(UNASSIGNED, 0.0) >= 1)


def suggest(A, bio, state, db, include_reserve: bool = False) -> dict:
    """
    تخصیص پیشنهادی برای همه cohortهایی که استخر واقعی ندارند.

    خروجی هرگز روی داده اعمال نمی‌شود؛ فقط پیشنهاد است.
    """
    free = _free_ponds(db, state, include_reserve)
    reserve = [p["pond_id"] for p in db.ponds() if p["role"] == "reserve"]
    target_util = float(A.get("capacity.target_utilisation"))
    rows, warnings = [], []

    pending = [c for c in state.cohorts.values() if needs_pond(bio, c)]
    # قدیمی‌ترین (سنگین‌ترین) اول — فوریت بیشتری دارد
    pending.sort(key=lambda c: c.purchase_date)

    for c in pending:
        n = c.alloc.get(TROUGH, 0.0) + c.alloc.get(UNASSIGNED, 0.0)
        cap_raw = bio.fish_per_pond(c.mean_weight)
        cap = max(1.0, cap_raw * target_util)
        need = int(math.ceil(n / cap))
        take = free[:need]
        del free[:len(take)]
        per = (n / len(take)) if take else 0.0
        row = {
            "cohort_id": c.cohort_id,
            "fish": n,
            "mean_weight_g": c.mean_weight,
            "weight_basis": c.weight_basis,
            "count_basis": c.count_basis,
            "capacity_per_pond": cap_raw,
            "planning_capacity_per_pond": cap,
            "ponds_needed": need,
            "ponds_offered": len(take),
            "basis": "suggested",
            "allocations": [{"pond_id": p, "quantity": round(per)} for p in take],
            "shortfall_ponds": need - len(take),
        }
        if row["allocations"]:
            diff = n - sum(a["quantity"] for a in row["allocations"])
            row["allocations"][0]["quantity"] = round(
                row["allocations"][0]["quantity"] + diff)
        if row["shortfall_ponds"] > 0:
            msg = (f"cohort {c.cohort_id}: به {need} استخر نیاز دارد ولی تنها "
                   f"{len(take)} استخر آزاد است — کمبود {row['shortfall_ponds']} استخر.")
            if reserve and not include_reserve:
                msg += (f" استخرهای رزرو ({'، '.join(reserve)}) عمداً کنار گذاشته "
                        f"شده‌اند؛ در صورت اضطرار می‌توانید آن‌ها را وارد کنید.")
            warnings.append(msg)
            row["capacity_warning"] = msg
        rows.append(row)

    return {
        "suggestions": rows,
        "free_ponds_remaining": free,
        "reserve_ponds": reserve,
        "include_reserve": include_reserve,
        "warnings": warnings,
        "target_utilisation": target_util,
        "any_shortfall": any(r["shortfall_ponds"] > 0 for r in rows),
    }


def accept(db, state, cohort_id: str, allocations: list | None = None,
           reason: str = "", A=None, bio=None) -> dict:
    """
    تبدیل تخصیص پیشنهادی به Actual.

    برای هر استخر یک تراکنش `transfer` ثبت می‌شود؛ از آن لحظه تخصیص واقعی
    است و دیگر «تخمینی» نیست. اگر `allocations` داده نشود، پیشنهاد سیستم
    استفاده می‌شود.
    """
    c = state.cohorts.get(cohort_id)
    if not c:
        raise KeyError(f"cohort یافت نشد: {cohort_id}")

    if allocations is None:
        if A is None or bio is None:
            raise ValueError("برای استفاده از پیشنهاد خودکار، A و bio لازم است")
        sug = suggest(A, bio, state, db)
        row = next((r for r in sug["suggestions"] if r["cohort_id"] == cohort_id), None)
        if not row:
            raise ValueError(f"برای {cohort_id} تخصیص پیشنهادی وجود ندارد")
        allocations = row["allocations"]

    pool = c.alloc.get(TROUGH, 0.0) + c.alloc.get(UNASSIGNED, 0.0)
    total = sum(float(a.get("quantity") or 0) for a in allocations)
    if total <= 0:
        raise ValueError("هیچ مقداری برای تخصیص وارد نشده است")
    if total > pool + 1:
        raise ValueError(f"مجموع تخصیص {total:,.0f} از ماهی تخصیص‌نیافته "
                         f"({pool:,.0f} قطعه) بیشتر است")

    valid = {p["pond_id"] for p in db.ponds()}
    created = []
    for a in allocations:
        pid = a.get("pond_id")
        q = float(a.get("quantity") or 0)
        if q <= 0:
            continue
        if pid not in valid:
            raise ValueError(f"استخر نامعتبر: {pid}")
        # مبدأ صریح: استخر ماهی تخصیص‌نیافته، نه «بزرگ‌ترین استخر»
        src = TROUGH if c.alloc.get(TROUGH, 0.0) >= 1 else UNASSIGNED
        tid = db.add_txn("transfer", state.as_of.isoformat(), cohort_id=cohort_id,
                         pond_id=src, to_pond_id=pid, quantity=q,
                         data_source="actual",
                         note=(reason or "تأیید تخصیص پیشنهادی استخر"),
                         payload={"source": "pond_allocation",
                                  "was_suggested": True})
        created.append({"pond_id": pid, "quantity": q, "txn_id": tid})
    return {"ok": True, "cohort_id": cohort_id, "created": created,
            "total_assigned": total, "basis": "actual"}


def move(db, state, cohort_id: str, from_pond: str | None, to_pond: str,
         quantity: float, reason: str = "") -> dict:
    """جابه‌جایی ماهی بین استخرها — همیشه Actual و همیشه یک تراکنش."""
    c = state.cohorts.get(cohort_id)
    if not c:
        raise KeyError(f"cohort یافت نشد: {cohort_id}")
    valid = {p["pond_id"] for p in db.ponds()}
    if to_pond not in valid:
        raise ValueError(f"استخر مقصد نامعتبر: {to_pond}")
    src = from_pond or c._largest()
    have = c.alloc.get(src, 0.0)
    q = float(quantity or 0)
    if q <= 0:
        raise ValueError("تعداد باید بزرگ‌تر از صفر باشد")
    if q > have + 1:
        raise ValueError(f"استخر {src} تنها {have:,.0f} قطعه از این cohort دارد")
    tid = db.add_txn("transfer", state.as_of.isoformat(), cohort_id=cohort_id,
                     pond_id=src, to_pond_id=to_pond, quantity=q,
                     data_source="actual",
                     note=reason or f"انتقال از {src} به {to_pond}",
                     payload={"source": "pond_move"})
    return {"ok": True, "txn_id": tid, "from": src, "to": to_pond, "quantity": q}
