"""
pond_optimize.py — بهینه‌سازی چیدمان استخر (Optimize Pond Allocation)
======================================================================
قابلیتی مستقل و محدود، کنار شماتیک ۲۱ استخر. معماری Stage 1–3 و ماژول
سالم `pond_alloc.py` (تخصیص استخر اول برای cohortهایی که تازه از تراف
عبور می‌کنند) دست‌نخورده می‌ماند — این ماژول یک لایه‌ی دیگر است:
**بازآرایی** چیدمانی که از قبل وجود دارد، با توجه به وضعیت فعلی و
forecast تا ۹۰–۱۴۰ روز آینده.

سه فاز مجزا، دقیقاً مطابق درخواست:

    Preview  (optimize_layout)   → هیچ نوشتنی در DB انجام نمی‌شود.
    Apply    (apply_layout)      → همان moves تأییدشده در Preview را به
                                    تراکنش‌های `transfer` معمولی تبدیل
                                    می‌کند + یک batch رکورد برای Restore.
    Restore  (restore_previous)  → آخرین batch اعمال‌شده را با تراکنش‌های
                                    `transfer` معکوس برمی‌گرداند — خودش هم
                                    یک batch جدید (audit trail کامل) است.

## الگوریتم (heuristic صریح و توضیح‌پذیر، نه MILP)

برای هر cohort موجود، نیاز استخر در چند checkpoint آینده (هر ۱۵ روز تا
`horizon_days`) محاسبه می‌شود — چون با رشد، ظرفیت هر استخر (تجربی) کم
می‌شود، حتی وقتی تعداد زنده به‌خاطر تلفات کم می‌شود، ممکن است نیاز به
استخر **بیشتر** شود. بیشینه‌ی این نیاز روی افق، هدف تخصیص است — دقیقاً
همان چیزی که «کاهش انتقال‌های آینده» می‌خواهد.

سپس cohortها (بزرگ‌ترین اول) به ترتیب زیر جا داده می‌شوند:
  1. استخرهایی که همین الان دارند (اگر هنوز کافی/سازگارند) — حفظ چیدمان.
  2. استخرهای آزاد.
  3. فقط اگر واقعاً کم آوردیم و وزن‌ها داخل tolerance‌اند: mixing با یک
     cohort دیگر (صریحاً علامت‌گذاری‌شده).

استخرهای رزرو دست‌نخورده می‌مانند مگر صراحتاً `include_reserve=True`.
"""
from __future__ import annotations

import json
import math
from datetime import date, timedelta

from .state import TROUGH, UNASSIGNED, _age
from .pond_alloc import needs_pond

CHECKPOINT_STEP_DAYS = 15


# ═══════════════════════════════════════════════════════════ کمکی‌های وزن
def weight_mix_allowed(w1: float, w2: float, tol_pct: float) -> bool:
    """آیا اختلاف دو وزن داخل tolerance مجاز mixing است؟"""
    base = max(w1, w2, 1e-6)
    return abs(w1 - w2) / base * 100.0 <= tol_pct


def _ponds_needed(bio, alive: float, weight: float) -> int:
    if alive < 1 or not bio.counts_toward_pond_capacity(weight):
        return 0
    return math.ceil(alive / max(1.0, bio.fish_per_pond(weight)))


# ═══════════════════════════════════════════════════ فرافکنی به‌جلو (بدون فروش)
def _project(bio, state, c, target: date) -> tuple[float, float]:
    """(alive تخمینی, وزن تخمینی) یک cohort در تاریخ `target` — فقط تلفات
    و رشد طبیعی؛ هیچ فروشی فرض نمی‌شود (این تصمیم فروش نیست، فقط جا لازم
    را پیش‌بینی می‌کند).

    اگر cohort همین الان از بالاترین وزن calibrate‌شدهٔ مدل رشد
    (`planning.harvest_weights`) عبور کرده باشد، وزن برای برنامه‌ریزی
    استخر ثابت فرض می‌شود، نه اینکه رشد نمایی بدون کالیبراسیون برون‌یابی
    شود — همان نکته‌ای که در موتور Saleability هم رعایت شد (وزنِ چند صد
    گرمی برون‌یابی‌شده برای یک ماهی که همین الان ۴۵ گرم است، غیرواقعی است).
    """
    max_calibrated = max(float(w) for w in state.A.get("planning.harvest_weights"))
    if c.mean_weight >= max_calibrated:
        return c.alive * bio.survival_ratio(
            _age(c.purchase_date, state.as_of) + c.growth_offset_days,
            _age(c.purchase_date, target) + c.growth_offset_days), c.mean_weight

    age_now = _age(c.purchase_date, state.as_of) + c.growth_offset_days
    age_t = _age(c.purchase_date, target) + c.growth_offset_days
    w = min(bio.weight_at_age(age_t), max_calibrated)
    sr = bio.survival_ratio(age_now, age_t) if age_t > age_now else 1.0
    return c.alive * sr, w


def _checkpoints(state, horizon_days: int) -> list:
    out, d = [], CHECKPOINT_STEP_DAYS
    while d <= horizon_days:
        out.append(state.as_of + timedelta(days=d))
        d += CHECKPOINT_STEP_DAYS
    if not out or (horizon_days - (len(out) - 1) * CHECKPOINT_STEP_DAYS) > 1:
        out.append(state.as_of + timedelta(days=horizon_days))
    return out


def peak_future_need(A, bio, state, c, horizon_days: int) -> dict:
    """بیشینه نیاز استخر این cohort روی کل افق + اولین تاریخی که آن نیاز رخ می‌دهد."""
    today_alive, today_w = c.alive, c.mean_weight
    best_n, best_date, best_w, best_alive = _ponds_needed(bio, today_alive, today_w), \
        state.as_of, today_w, today_alive
    for d in _checkpoints(state, horizon_days):
        alive, w = _project(bio, state, c, d)
        n = _ponds_needed(bio, alive, w)
        if n > best_n:
            best_n, best_date, best_w, best_alive = n, d, w, alive
    return {"ponds": best_n, "date": best_date, "weight_g": best_w, "alive": best_alive}


def grow_out_entry(A, bio, state, c, horizon_days: int):
    """کی این cohort (هنوز زیر آستانه ظرفیت) از تراف عبور می‌کند؟ None = خارج افق."""
    if bio.counts_toward_pond_capacity(c.mean_weight):
        return None
    for d in _checkpoints(state, horizon_days):
        alive, w = _project(bio, state, c, d)
        if bio.counts_toward_pond_capacity(w) and alive >= 1:
            return {"date": d, "weight_g": w, "alive": alive,
                    "ponds_needed": _ponds_needed(bio, alive, w),
                    "days": (d - state.as_of).days}
    return None


# ═══════════════════════════════════════════════════════ نمای فعلی/آینده
def current_snapshot(state) -> dict:
    """{cohort_id: {pond_or_bucket: qty}} — کپی کامل، برای Restore."""
    return {cid: dict(c.alloc) for cid, c in state.cohorts.items() if c.alive >= 1}


def _pond_role_map(db) -> dict:
    return {p["pond_id"]: p["role"] for p in db.ponds()}


def forecast_pond_view(A, bio, state, db, target: date) -> dict:
    """
    نمای شماتیک استخرها برای یک تاریخ آینده (یا امروز) — فقط پیش‌بینی،
    هیچ انتساب واقعی نیست. cohortهای دارای استخر واقعی همان‌جا فرض
    می‌شوند مگر ظرفیت کافی نباشد (که به‌عنوان تعارض علامت می‌خورد).
    """
    roles = _pond_role_map(db)
    is_future = target > state.as_of
    per_pond: dict[str, list] = {pid: [] for pid in roles}
    planned_transfers = []

    for c in state.cohorts.values():
        if c.alive < 1:
            continue
        alive, w = (_project(bio, state, c, target) if is_future
                    else (c.alive, c.mean_weight))
        if alive < 1:
            continue
        entry = grow_out_entry(A, bio, state, c,
                               max(1, (target - state.as_of).days)) \
            if not bio.counts_toward_pond_capacity(c.mean_weight) else None
        real_ponds = [pid for pid in c.alloc if pid in roles and c.alloc[pid] >= 1]
        if real_ponds:
            # نسبت فعلی بین استخرهایش را حفظ کن (فرض: بدون انتقال جدید)
            tot_now = sum(c.alloc[p] for p in real_ponds) or 1.0
            for pid in real_ponds:
                share = c.alloc[pid] / tot_now
                per_pond[pid].append({
                    "cohort_id": c.cohort_id, "count": alive * share,
                    "mean_weight_g": w, "basis": "forecast" if is_future else "actual",
                })
            need_now = _ponds_needed(bio, sum(c.alloc[p] for p in real_ponds), w)
            if need_now > len(real_ponds):
                planned_transfers.append({
                    "cohort_id": c.cohort_id, "type": "capacity_conflict",
                    "detail": f"با وزن تخمینی {w:.1f}g به {need_now} استخر نیاز "
                              f"دارد ولی فقط {len(real_ponds)} استخر دارد."})
        elif needs_pond(bio, c):
            planned_transfers.append({
                "cohort_id": c.cohort_id, "type": "awaiting_pond_assignment",
                "detail": f"از تراف عبور کرده ({c.mean_weight:.1f}g) ولی هنوز "
                          f"استخر واقعی ندارد — تخصیص هرچه زودتر لازم است."})
        elif entry and entry["days"] <= (target - state.as_of).days:
            planned_transfers.append({
                "cohort_id": c.cohort_id, "type": "grow_out_entry",
                "detail": f"تا {entry['date'].isoformat()} وارد grow-out می‌شود "
                          f"و به {entry['ponds_needed']} استخر نیاز خواهد داشت."})

    free_now = [pid for pid, role in roles.items() if not per_pond[pid]]
    reserved_future = []
    if is_future:
        # چه‌قدر از ظرفیت آزاد را cohortهای زیر ۱ گرم (که هنوز استخر واقعی
        # ندارند) ممکن است تا آن تاریخ لازم داشته باشند؟
        horizon = max(1, (target - state.as_of).days)
        pending = 0
        for c in state.cohorts.values():
            if c.alive < 1 or c.alloc.get(TROUGH, 0) + c.alloc.get(UNASSIGNED, 0) < 1:
                continue
            entry = grow_out_entry(A, bio, state, c, horizon)
            if entry and entry["days"] <= horizon:
                pending += entry["ponds_needed"]
        op_free = [pid for pid in free_now if roles[pid] == "operational"]
        reserved_future = op_free[:pending]

    pond_rows = []
    for p in db.ponds():
        occ = per_pond[p["pond_id"]]
        count = sum(o["count"] for o in occ)
        biomass = sum(o["count"] * o["mean_weight_g"] for o in occ) / 1000.0
        avg_w = (sum(o["count"] * o["mean_weight_g"] for o in occ) / count) if count else 0.0
        cap = bio.fish_per_pond(avg_w) if count else 0.0
        cohort_ids = sorted({o["cohort_id"] for o in occ})
        mixed = len(cohort_ids) > 1
        status = "empty"
        if count > 0:
            status = "over" if (bio.counts_toward_pond_capacity(avg_w) and cap
                                and count / cap > float(A.get("capacity.target_utilisation"))) \
                else "occupied"
        elif p["pond_id"] in reserved_future:
            status = "planned"
        elif p["role"] == "reserve":
            status = "reserve"
        pond_rows.append({
            "pond_id": p["pond_id"], "label": p["label"], "role": p["role"],
            "status": status, "occupants": occ, "count": count,
            "biomass_kg": biomass, "avg_weight_g": avg_w, "capacity": cap,
            "utilisation": (count / cap) if cap else 0.0, "mixed": mixed,
            "basis": "forecast" if is_future else ("actual" if count else "-"),
        })

    return {
        "date": target.isoformat(), "is_forecast": is_future,
        "ponds": pond_rows, "planned_transfers": planned_transfers,
        "reserved_future_ponds": reserved_future,
    }


# ═══════════════════════════════════════════════════════ بهینه‌سازی چیدمان
def optimize_layout(A, bio, state, db, horizon_days: int | None = None,
                    include_reserve: bool = False) -> dict:
    """
    Preview چیدمان پیشنهادی — **هیچ نوشتنی در DB انجام نمی‌شود.**

    خروجی شامل current/proposed/moves/آمار مقایسه‌ای است تا کاربر پیش از
    Apply ببیند دقیقاً چه تغییری در جریان است.
    """
    horizon_days = int(horizon_days or A.get("pond.optimize_horizon_days"))
    tol = float(A.get("pond.max_weight_mix_tolerance_percent"))
    roles = _pond_role_map(db)
    op_ponds = [pid for pid, r in roles.items() if r == "operational"]
    reserve_ponds = [pid for pid, r in roles.items() if r == "reserve"]

    cohorts = [c for c in state.cohorts.values() if c.alive >= 1]
    need = {}                      # cohort_id -> {"ponds", "weight_g", "alive", "future_date"}
    for c in cohorts:
        if not bio.counts_toward_pond_capacity(c.mean_weight):
            continue                                   # هنوز در تراف — این تابع کاری ندارد
        pk = peak_future_need(A, bio, state, c, horizon_days)
        need[c.cohort_id] = {
            "ponds": max(1, pk["ponds"]), "weight_g": c.mean_weight,
            "future_weight_g": pk["weight_g"], "alive": c.alive,
            "future_ponds": pk["ponds"],
            "today_ponds": _ponds_needed(bio, c.alive, c.mean_weight),
            "grows_into_more_space": pk["ponds"] >
                _ponds_needed(bio, c.alive, c.mean_weight),
            "future_date": pk["date"].isoformat(),
        }

    total_today = sum(v["today_ponds"] for v in need.values())
    pool = list(op_ponds) + (list(reserve_ponds) if include_reserve else [])
    feasible = total_today <= len(pool)
    reason = None
    if not feasible:
        reason = (f"مجموع نیاز امروز {total_today} استخر است ولی فقط "
                 f"{len(pool)} استخر {'(با احتساب رزرو) ' if include_reserve else ''}"
                 f"در دسترس است — {total_today - len(pool)} استخر کم است.")

    # ------------------------------------------------- current
    current_by_pond: dict[str, list] = {pid: [] for pid in roles}
    for c in cohorts:
        for pid, q in c.alloc.items():
            if pid in current_by_pond and q >= 1:
                current_by_pond[pid].append({"cohort_id": c.cohort_id, "count": q,
                                             "mean_weight_g": c.mean_weight})

    # ------------------------------------------------- تخصیص حریصانه، دو مرحله
    # مرحله ۱: نیاز *امروز* هر cohort — این هرگز نباید فدای رزرو آینده شود.
    # مرحله ۲: فقط با ظرفیت آزادِ باقی‌مانده، سمت نیاز آیندهٔ (peak) می‌رویم؛
    # اولویت با cohortی که زودتر به آن نیاز می‌رسد.
    order_today = sorted(need, key=lambda cid: -need[cid]["today_ponds"])
    assigned: dict[str, dict] = {pid: None for pid in pool}   # pid -> {cohort_id, mixed}
    proposed_by_pond: dict[str, list] = {pid: [] for pid in roles}
    moves, cohorts_affected = [], set()
    future_conflicts_avoided = 0

    def _take_current_ponds(cid):
        c = state.cohorts[cid]
        return [pid for pid in c.alloc if pid in pool and c.alloc[pid] >= 1]

    free_pool = [p for p in pool if roles.get(p) != "reserve" or include_reserve]

    def _ponds_of(cid2):
        return sum(1 for info2 in assigned.values()
                  if info2 and (info2["cohort_id"] == cid2 or info2.get("also") == cid2))

    for cid in order_today:
        n = need[cid]
        target_n = n["today_ponds"]
        c = state.cohorts[cid]
        keep = [pid for pid in _take_current_ponds(cid) if assigned.get(pid) is None]
        for pid in keep[:target_n]:
            assigned[pid] = {"cohort_id": cid, "mixed": False}
            if pid in free_pool:
                free_pool.remove(pid)
        got = min(len(keep), target_n)
        # استخرهای آزاد باقی
        while got < target_n and free_pool:
            pid = free_pool.pop(0)
            assigned[pid] = {"cohort_id": cid, "mixed": False}
            got += 1
        # اگر هنوز کم داریم: mixing فقط داخل tolerance، فقط وقتی واقعاً لازم است
        # و فقط اگر مجموع دو cohort واقعاً در ظرفیت (سنگین‌ترین وزن) جا شود —
        # قبل از allocation، نه بعد از آن؛ هیچ استخر مخلوط نباید از ۱۰۰٪
        # ظرفیت فیزیکی عبور کند (اصلاح ۳).
        if got < target_n and target_n > 0:
            c_share = c.alive / target_n
            for pid, info in list(assigned.items()):
                if got >= target_n:
                    break
                if info is None or info["cohort_id"] == cid or info.get("also"):
                    continue          # یک pond حداکثر دو cohort (ساده و امن)
                other = state.cohorts.get(info["cohort_id"])
                if not other:
                    continue
                if not weight_mix_allowed(c.mean_weight, other.mean_weight, tol):
                    continue
                other_ponds = max(1, _ponds_of(info["cohort_id"]))
                other_share = other.alive / other_ponds
                heavy_w = max(c.mean_weight, other.mean_weight)
                hard_cap = bio.fish_per_pond(heavy_w)     # ۱۰۰٪ ظرفیت فیزیکی
                if c_share + other_share > hard_cap + 1e-6:
                    continue           # این pond واقعاً جا ندارد؛ بعدی را امتحان کن
                info["mixed"] = True
                assigned[pid] = {**info, "also": cid, "mixed": True}
                got += 1

    # مرحله ۲: رزرو ظرفیت آیندهٔ واقعاً بلااستفاده — زودترین نیاز اول
    order_future = sorted(
        (cid for cid in need if need[cid]["grows_into_more_space"]),
        key=lambda cid: need[cid]["future_date"])
    for cid in order_future:
        n = need[cid]
        got = sum(1 for info in assigned.values()
                 if info and (info["cohort_id"] == cid or info.get("also") == cid))
        if got >= n["future_ponds"] or not free_pool:
            continue
        reserve_take = min(len(free_pool), n["future_ponds"] - got)
        for _ in range(reserve_take):
            pid = free_pool.pop(0)
            assigned[pid] = {"cohort_id": cid, "mixed": False, "for_future": True}
        if reserve_take > 0:
                future_conflicts_avoided += 1

    cohort_pond_count: dict[str, list] = {}
    for pid, info in assigned.items():
        if not info or info.get("for_future"):
            continue          # رزرو آینده در شمارش «سهم واقعی هر استخر» دخیل نیست
        cohort_pond_count.setdefault(info["cohort_id"], []).append(pid)
        if info.get("also"):
            cohort_pond_count.setdefault(info["also"], []).append(pid)

    future_reserved = []      # Future Planned/Reserved — هرگز Current Allocation نیست
    for pid, info in assigned.items():
        if not info:
            continue
        if info.get("for_future"):
            c = state.cohorts[info["cohort_id"]]
            future_reserved.append({
                "pond_id": pid, "cohort_id": info["cohort_id"],
                "needed_by_date": need[info["cohort_id"]]["future_date"]})
            # هیچ ماهی واقعی اینجا نیست؛ فقط رزرو — شمارش صفر تا نه در
            # utilisation و نه در «انتقال امروز» اشتباهی وارد شود.
            proposed_by_pond[pid].append({
                "cohort_id": info["cohort_id"], "mean_weight_g": c.mean_weight,
                "count": 0.0, "mixed": False, "for_future": True})
            continue
        c = state.cohorts[info["cohort_id"]]
        n_ponds = max(1, len(cohort_pond_count.get(info["cohort_id"], [pid])))
        proposed_by_pond[pid].append({
            "cohort_id": info["cohort_id"], "mean_weight_g": c.mean_weight,
            "count": c.alive / n_ponds,
            "mixed": info.get("mixed", False), "for_future": False})
        if info.get("also"):
            c2 = state.cohorts[info["also"]]
            n2 = max(1, len(cohort_pond_count.get(info["also"], [pid])))
            proposed_by_pond[pid].append({
                "cohort_id": info["also"], "mean_weight_g": c2.mean_weight,
                "count": c2.alive / n2,
                "mixed": True, "for_future": False})

    # ------------------------------------------------- moves (current → proposed)
    # فقط تخصیص واقعیِ *امروز* (نه رزروهای آینده) وارد محاسبهٔ moves می‌شود —
    # دقیقاً همان چیزی که اصلاح ۴ می‌خواهد: نیاز آینده هرگز به‌تنهایی باعث
    # انتقال امروز نشود.
    current_pond_of = {}
    for pid, occ in current_by_pond.items():
        for o in occ:
            current_pond_of.setdefault(o["cohort_id"], []).append((pid, o["count"]))
    proposed_pond_of: dict[str, list] = {}
    for pid, occ in proposed_by_pond.items():
        for o in occ:
            if o.get("for_future"):
                continue
            proposed_pond_of.setdefault(o["cohort_id"], []).append(pid)

    all_cids = set(current_pond_of) | set(proposed_pond_of)
    for cid in all_cids:
        cur_list = current_pond_of.get(cid, [])
        cur_ponds = {p for p, _ in cur_list}
        target_ponds = set(proposed_pond_of.get(cid, cur_ponds))
        leave = cur_ponds - target_ponds
        enter = target_ponds - cur_ponds
        if not leave and not enter:
            continue
        cohorts_affected.add(cid)
        c = state.cohorts[cid]
        total = sum(q for _, q in cur_list) or c.alloc.get(TROUGH, 0.0) \
            + c.alloc.get(UNASSIGNED, 0.0)
        per_target = total / len(target_ponds) if target_ponds else 0
        leave_list = list(leave)
        src_default = TROUGH if c.alloc.get(TROUGH, 0.0) >= 1 else \
            (UNASSIGNED if c.alloc.get(UNASSIGNED, 0.0) >= 1 else None)
        for i, dst in enumerate(sorted(enter)):
            src = leave_list[i] if i < len(leave_list) else src_default
            moves.append({
                "cohort_id": cid, "from_pond": src, "to_pond": dst,
                "quantity": round(per_target),
                "reason": "بازتوزیع برای کاهش انتقال‌های آینده"})

    ponds_before = {pid for pid, occ in current_by_pond.items() if occ}
    ponds_after = {pid for pid, occ in proposed_by_pond.items()
                  if any(not o.get("for_future") for o in occ)}
    ponds_freed = len(ponds_before - ponds_after)

    def _max_util(by_pond):
        best = 0.0
        for occ in by_pond.values():
            if not occ:
                continue
            count = sum(o["count"] for o in occ)
            w = (sum(o["count"] * o["mean_weight_g"] for o in occ) / count) if count else 0
            cap = bio.fish_per_pond(w) if count else 0
            if cap:
                best = max(best, count / cap)
        return best

    return {
        "feasible": feasible, "reason_if_infeasible": reason,
        "horizon_days": horizon_days, "as_of": state.as_of.isoformat(),
        "include_reserve": include_reserve,
        "current": [{"pond_id": pid, "occupants": occ} for pid, occ in current_by_pond.items()],
        "proposed": [{"pond_id": pid, "occupants": occ} for pid, occ in proposed_by_pond.items()],
        "moves": moves,
        "transfers_count": len(moves),
        "ponds_freed": ponds_freed,
        "max_utilisation_before": _max_util(current_by_pond),
        "max_utilisation_after": _max_util(proposed_by_pond),
        "future_capacity_conflicts_avoided": future_conflicts_avoided,
        "cohorts_affected": sorted(cohorts_affected),
        "cohort_needs": need,
        "future_reserved": future_reserved,
    }


# ═══════════════════════════════════════════════════════ Apply / Restore
def apply_layout(db, state, moves: list, reason: str = "") -> dict:
    """
    Apply — فقط با moves تأییدشدهٔ کاربر (همان خروجی Preview) اجرا می‌شود.

    هرگز از سرور دوباره optimize نمی‌شود؛ یعنی دقیقاً همان چیزی که کاربر
    دید Apply می‌گردد. هر move یک تراکنش `transfer` معمولی است — ساختار
    مالکیت داده هیچ تغییری نمی‌کند.
    """
    if not moves:
        return {"ok": True, "applied": 0, "txn_ids": [], "batch_id": None,
               "note": "هیچ جابه‌جایی‌ای لازم نبود."}

    snapshot = current_snapshot(state)
    txn_ids = []
    for mv in moves:
        cid = mv["cohort_id"]
        c = state.cohorts.get(cid)
        if not c:
            raise KeyError(f"cohort یافت نشد: {cid}")
        q = float(mv.get("quantity") or 0)
        if q <= 0:
            continue
        tid = db.add_txn("transfer", state.as_of.isoformat(), cohort_id=cid,
                         pond_id=mv.get("from_pond"), to_pond_id=mv["to_pond"],
                         quantity=q, data_source="actual",
                         note=reason or mv.get("reason") or "بهینه‌سازی چیدمان استخر",
                         payload={"source": "pond_optimize"})
        txn_ids.append(tid)
        c.move(q, mv.get("from_pond"), mv["to_pond"])   # وضعیت درون‌حافظه هم به‌روز شود

    batch_id = db.save_pond_batch("optimize", state.as_of.isoformat(), snapshot,
                                  moves, txn_ids, note=reason)
    return {"ok": True, "applied": len(txn_ids), "txn_ids": txn_ids, "batch_id": batch_id}


def restore_previous(db, state, reason: str = "") -> dict:
    """آخرین چیدمان اعمال‌شده را به حالت پیش از آن برمی‌گرداند — با batch جدید (audit trail)."""
    batch = db.latest_pond_batch("optimize", status="applied")
    if not batch:
        return {"ok": False, "reason": "هیچ بهینه‌سازی اعمال‌شده‌ای برای بازگردانی وجود ندارد."}

    snapshot_before = batch["snapshot_before"]
    txn_ids = []
    for cid, target_alloc in snapshot_before.items():
        c = state.cohorts.get(cid)
        if not c:
            continue
        # مقصد: دقیقاً همان توزیع قبل از Apply. مبدأ: توزیع فعلی.
        current_alloc = dict(c.alloc)
        # اول مازاد استخرهایی که در snapshot نبودند/کمتر بودند را به سمت هدف ببر
        for pid, want in target_alloc.items():
            have = current_alloc.get(pid, 0.0)
            deficit = want - have
            if deficit <= 1e-6:
                continue
            # از استخرهایی که در حال حاضر بیش از سهم snapshot دارند بردار
            for src_pid in list(current_alloc):
                if deficit <= 1e-6:
                    break
                if src_pid == pid:
                    continue
                src_have = current_alloc.get(src_pid, 0.0)
                src_target = target_alloc.get(src_pid, 0.0)
                excess = src_have - src_target
                if excess <= 1e-6:
                    continue
                take = min(excess, deficit)
                tid = db.add_txn("transfer", state.as_of.isoformat(), cohort_id=cid,
                                 pond_id=src_pid, to_pond_id=pid, quantity=take,
                                 data_source="actual",
                                 note=reason or "بازگردانی چیدمان قبل از بهینه‌سازی",
                                 payload={"source": "pond_optimize_restore",
                                          "restored_from_batch": batch["batch_id"]})
                txn_ids.append(tid)
                c.move(take, src_pid, pid)
                current_alloc[src_pid] = current_alloc.get(src_pid, 0.0) - take
                current_alloc[pid] = current_alloc.get(pid, 0.0) + take
                deficit -= take

    new_batch_id = db.save_pond_batch(
        "restore", state.as_of.isoformat(), current_snapshot(state), [],
        txn_ids, restored_from=batch["batch_id"], note=reason)
    db.mark_pond_batch_restored(batch["batch_id"])
    return {"ok": True, "restored_batch": batch["batch_id"], "new_batch_id": new_batch_id,
           "txn_ids": txn_ids}
