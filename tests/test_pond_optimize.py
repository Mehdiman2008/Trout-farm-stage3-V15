"""
تست‌های «بهینه‌سازی چیدمان استخر» (Optimize Pond Allocation).

۸ سناریوی الزامی specification:
 1. cohort بزرگ بین چند استخر پخش شود بدون عبور از ظرفیت
 2. دو cohort با اختلاف وزن زیاد هرگز mix نشوند
 3. دو cohort با اختلاف وزن داخل tolerance در صورت نیاز قابل mix باشند
 4. optimizer تعداد transfer را نسبت به یک allocation ساده کاهش دهد
 5. cohort زیر ۱g در future forecast برای استخرهای آینده دیده شود
 6. Date Selector وضعیت آینده متفاوت را درست نشان دهد
 7. Restore allocation قبلی را دقیق برگرداند
 8. Apply بدون تأیید کاربر انجام نشود
"""
from datetime import date, timedelta

import pytest

from app import Engine
from core import pond_optimize as PO


def _mk(tmp_path, name="pond_opt.db"):
    return Engine(str(tmp_path / name))


def _isolate(E):
    """
    Engine() همیشه ۵ cohort واقعی مزرعه را seed می‌کند (مطابق بقیه فایل‌های
    تست). برای سناریوهایی که دقیقاً به ظرفیت/تعداد استخر حساس‌اند، آن‌ها
    را باطل می‌کنیم — با `void_txn` (audit-trail کامل، نه حذف)، تا فقط
    cohortهای مصنوعی همان تست باقی بمانند.
    """
    for t in E.db.q("SELECT id FROM transactions WHERE txn_type='egg_purchase' "
                    "AND status='active'"):
        E.db.void_txn(t["id"], "isolated for pond-optimize unit test")


def _buy(E, cid, qty, day, price=6000):
    E.db.add_txn("egg_purchase", day, cohort_id=cid, quantity=qty,
                 unit_price=price, amount=qty * price, data_source="actual",
                 note="test cohort")


def _set_weight(E, cid, day, weight_g):
    """anchor وزن یک cohort به مقدار دلخواه (از طریق weight_sample واقعی)."""
    E.db.add_txn("weight_sample", day, cohort_id=cid, weight_g=weight_g,
                 quantity=30, data_source="actual", note="test weight anchor")


def _today():
    return date.today().isoformat()


# ═══════════════════════════════ ۱. پخش cohort بزرگ بدون عبور از ظرفیت
def test_large_cohort_split_without_exceeding_capacity(tmp_path):
    E = _mk(tmp_path)
    _isolate(E)
    day = _today()
    _buy(E, "BIG1", 90_000, day)
    _set_weight(E, "BIG1", day, 10.0)          # ظرفیت هر استخر در ۱۰g محدود است

    A, bio, st = E.ctx(date.today())
    r = PO.optimize_layout(A, bio, st, E.db)
    assert r["feasible"]

    cap = bio.fish_per_pond(10.0)
    per_pond_qty = {}
    for mv in r["moves"]:
        per_pond_qty[mv["to_pond"]] = per_pond_qty.get(mv["to_pond"], 0) + mv["quantity"]
    assert len(per_pond_qty) > 1, "این تعداد باید بین چند استخر پخش شود"
    for pid, qty in per_pond_qty.items():
        assert qty <= cap + 1, f"{pid} از ظرفیت تجربی ({cap:.0f}) عبور کرده: {qty}"


# ═══════════════════════════════ ۲. اختلاف وزن زیاد → هرگز mix نشوند
def test_large_weight_gap_never_mixed(tmp_path):
    E = _mk(tmp_path)
    _isolate(E)
    day = _today()
    _buy(E, "LOW-W", 40_000, day)
    _set_weight(E, "LOW-W", day, 2.0)
    _buy(E, "HIGH-W", 40_000, day)
    _set_weight(E, "HIGH-W", day, 14.0)        # اختلاف ~۸۵٪ — خیلی فراتر از tolerance

    A, bio, st = E.ctx(date.today())
    A.set("pond.max_weight_mix_tolerance_percent", 8)
    r = PO.optimize_layout(A, bio, st, E.db)

    for p in r["proposed"]:
        cids = {o["cohort_id"] for o in p["occupants"]}
        if "LOW-W" in cids and "HIGH-W" in cids:
            pytest.fail(f"استخر {p['pond_id']} دو cohort با اختلاف وزن زیاد را mix کرده")


# ═══════════════════ ۳. اختلاف وزن داخل tolerance → در صورت نیاز mix ممکن باشد
def test_small_weight_gap_can_be_mixed_when_needed(tmp_path):
    E = _mk(tmp_path)
    _isolate(E)
    day = _today()
    # دو cohort کوچک با وزن نزدیک (اختلاف ~۵٪، داخل tolerance ۸٪) — هرکدام
    # به‌تنهایی فقط ۱ استخر لازم دارد و مجموعشان هم واقعاً زیر ظرفیت یک
    # استخر می‌ماند (فقط تنگی تعداد استخر، نه تنگی ظرفیت، وادار به mix می‌کند).
    cap10 = 1.0  # placeholder, real cap computed below
    _buy(E, "A-CLOSE", 6_000, day)
    _set_weight(E, "A-CLOSE", day, 10.0)
    _buy(E, "B-CLOSE", 6_000, day)
    _set_weight(E, "B-CLOSE", day, 10.5)

    A, bio, st = E.ctx(date.today())
    A.set("pond.max_weight_mix_tolerance_percent", 8)
    cap = bio.fish_per_pond(10.5)
    assert 6_000 * 2 <= cap, "پیش‌فرض تست: مجموع باید واقعاً زیر ظرفیت یک استخر باشد"
    # فقط ۱ استخر عملیاتی در دسترس بگذار تا mixing واقعاً لازم شود
    E.db.ex("UPDATE ponds SET role='reserve' WHERE pond_id NOT IN "
           "(SELECT pond_id FROM ponds WHERE role='operational' ORDER BY sort_order LIMIT 1)")
    try:
        A2, bio2, st2 = E.ctx(date.today())
        r = PO.optimize_layout(A2, bio2, st2, E.db)
        mixed = [p for p in r["proposed"]
                if {"A-CLOSE", "B-CLOSE"} <= {o["cohort_id"] for o in p["occupants"]}]
        assert mixed, "با ظرفیت محدود و وزن‌های نزدیک، باید mixing رخ دهد"
        for p in mixed:
            assert p["occupants"][0].get("mixed") or len(p["occupants"]) > 1
    finally:
        E.db.ex("UPDATE ponds SET role='operational' WHERE pond_id LIKE 'P%'")


def test_weight_mix_allowed_helper():
    assert PO.weight_mix_allowed(10.0, 10.5, 8) is True     # ~4.8٪ اختلاف — داخل tolerance
    assert PO.weight_mix_allowed(10.0, 10.9, 8) is False    # ~8.3٪ اختلاف — فراتر از tolerance
    assert PO.weight_mix_allowed(2.0, 14.0, 8) is False


# ═══════════════════ ۴. کاهش تعداد transfer نسبت به allocation ساده
def test_optimizer_prefers_current_placement_over_naive_reassignment(tmp_path):
    """
    اگر یک cohort از قبل درست جا افتاده باشد (دقیقاً هم‌اندازه نیاز امروزش)،
    optimizer نباید آن را جابه‌جا کند — یعنی نسبت به یک بازچینی ساده (که
    فرض می‌کند از صفر شروع می‌شود) تعداد transfer کمتری تولید می‌کند.
    """
    E = _mk(tmp_path)
    _isolate(E)
    day = _today()
    _buy(E, "PLACED", 50_000, day)
    _set_weight(E, "PLACED", day, 10.0)

    A, bio, st = E.ctx(date.today())
    need = bio.fish_per_pond(10.0)
    import math
    n_ponds = math.ceil(50_000 / need)
    ponds = [p["pond_id"] for p in E.db.ponds() if p["role"] == "operational"][:n_ponds]
    per = 50_000 / n_ponds
    for pid in ponds:
        E.db.add_txn("transfer", day, cohort_id="PLACED", pond_id="TROUGH",
                     to_pond_id=pid, quantity=per, data_source="actual",
                     note="pre-placed for test")

    A2, bio2, st2 = E.ctx(date.today())
    r = PO.optimize_layout(A2, bio2, st2, E.db)
    moves_for_placed = [m for m in r["moves"] if m["cohort_id"] == "PLACED"]
    assert len(moves_for_placed) == 0, \
        "cohortی که از قبل درست جا افتاده نباید بدون دلیل جابه‌جا شود"

    # در برابر یک allocation «ساده» فرضی که از صفر anew همه را می‌چیند
    # (یعنی n_ponds transfer برای همین یک cohort) — optimizer عملاً صفر
    # transfer لازم داشت، پس واقعاً کمتر از حالت ساده است.
    assert n_ponds > 0


# ═══════════════════ ۵. cohort زیر ۱g در future forecast دیده شود
def test_sub_1g_cohort_shows_in_future_forecast(tmp_path):
    E = _mk(tmp_path)
    day = _today()
    _buy(E, "BABY", 160_000, day)
    _set_weight(E, "BABY", day, 0.15)          # هنوز خیلی زیر ۱ گرم

    A, bio, st = E.ctx(date.today())
    horizon = int(A.get("pond.optimize_horizon_days"))
    entry = PO.grow_out_entry(A, bio, st, st.cohorts["BABY"], horizon)
    assert entry is not None, "این cohort باید در همان افق وارد grow-out شود"
    assert entry["ponds_needed"] >= 1

    future = st.as_of + timedelta(days=entry["days"] + 2)
    fv = PO.forecast_pond_view(A, bio, st, E.db, future)
    kinds = {pt["cohort_id"]: pt["type"] for pt in fv["planned_transfers"]}
    assert kinds.get("BABY") in ("grow_out_entry", "awaiting_pond_assignment")


# ═══════════════════ ۶. Date Selector وضعیت‌های مختلف را درست نشان دهد
def test_date_selector_shows_different_future_states(tmp_path):
    E = _mk(tmp_path)
    day = _today()
    _buy(E, "GROWER", 100_000, day)
    _set_weight(E, "GROWER", day, 3.0)

    A, bio, st = E.ctx(date.today())
    v_today = PO.forecast_pond_view(A, bio, st, E.db, st.as_of)
    v_60 = PO.forecast_pond_view(A, bio, st, E.db, st.as_of + timedelta(days=60))
    assert v_today["is_forecast"] is False
    assert v_60["is_forecast"] is True

    w_today = next(o["mean_weight_g"] for p in v_today["ponds"]
                  for o in p["occupants"] if o["cohort_id"] == "GROWER") \
        if any(o["cohort_id"] == "GROWER" for p in v_today["ponds"] for o in p["occupants"]) \
        else st.cohorts["GROWER"].mean_weight
    w_60 = st.weight_of(st.cohorts["GROWER"], st.as_of + timedelta(days=60))
    assert w_60 > w_today, "وزن پیش‌بینی‌شده در ۶۰ روز بعد باید بیشتر از امروز باشد"
    assert v_60["date"] == (st.as_of + timedelta(days=60)).isoformat()


# ═══════════════════ ۷. Restore دقیقاً چیدمان قبلی را برگرداند
def test_restore_reverts_exactly_to_pre_apply_snapshot(tmp_path):
    E = _mk(tmp_path)
    day = _today()
    _buy(E, "RESTORE-ME", 70_000, day)
    _set_weight(E, "RESTORE-ME", day, 8.0)

    A, bio, st = E.ctx(date.today())
    before_snapshot = PO.current_snapshot(st)

    r = PO.optimize_layout(A, bio, st, E.db)
    assert r["moves"]
    apply_res = PO.apply_layout(E.db, st, r["moves"], reason="test")
    assert apply_res["ok"] and apply_res["applied"] > 0

    A2, bio2, st2 = E.ctx(date.today())
    after_apply = dict(st2.cohorts["RESTORE-ME"].alloc)
    assert after_apply != before_snapshot["RESTORE-ME"], \
        "Apply باید واقعاً چیدمان را عوض کرده باشد"

    restore_res = PO.restore_previous(E.db, st2, reason="test restore")
    assert restore_res["ok"]

    A3, bio3, st3 = E.ctx(date.today())
    after_restore = dict(st3.cohorts["RESTORE-ME"].alloc)
    for k, v in before_snapshot["RESTORE-ME"].items():
        assert after_restore.get(k, 0.0) == pytest.approx(v, abs=1.0), \
            f"مقدار استخر {k} پس از Restore با قبل از Apply یکی نیست"


def test_restore_itself_has_audit_trail(tmp_path):
    E = _mk(tmp_path)
    day = _today()
    _buy(E, "AUDIT-ME", 40_000, day)
    _set_weight(E, "AUDIT-ME", day, 6.0)
    A, bio, st = E.ctx(date.today())
    r = PO.optimize_layout(A, bio, st, E.db)
    PO.apply_layout(E.db, st, r["moves"])
    A2, bio2, st2 = E.ctx(date.today())
    n_txn_before = E.db.one("SELECT COUNT(*) n FROM transactions")["n"]
    res = PO.restore_previous(E.db, st2)
    n_txn_after = E.db.one("SELECT COUNT(*) n FROM transactions")["n"]
    assert n_txn_after > n_txn_before, \
        "Restore باید تراکنش transfer جدید بسازد، نه چیزی را حذف/overwrite کند"
    batch = E.db.one("SELECT * FROM pond_alloc_batches WHERE batch_id=?",
                     (res["new_batch_id"],))
    assert batch["kind"] == "restore"
    assert batch["restored_from"] is not None


# ═══════════════════ ۸. Apply بدون تأیید کاربر انجام نشود
def test_apply_never_runs_without_explicit_confirmed_moves(tmp_path):
    E = _mk(tmp_path)
    day = _today()
    _buy(E, "NOAPPLY", 30_000, day)
    _set_weight(E, "NOAPPLY", day, 5.0)

    A, bio, st = E.ctx(date.today())
    n_txn_before = E.db.one("SELECT COUNT(*) n FROM transactions")["n"]

    # صرف صدا زدن optimize_layout (Preview) هرگز نباید چیزی بنویسد
    PO.optimize_layout(A, bio, st, E.db)
    PO.optimize_layout(A, bio, st, E.db)
    n_txn_after_preview = E.db.one("SELECT COUNT(*) n FROM transactions")["n"]
    assert n_txn_after_preview == n_txn_before, \
        "Preview نباید هیچ تراکنشی بسازد"

    # apply_layout با moves خالی هم چیزی نمی‌نویسد
    res = PO.apply_layout(E.db, st, [], reason="empty")
    assert res["applied"] == 0
    n_txn_after_empty = E.db.one("SELECT COUNT(*) n FROM transactions")["n"]
    assert n_txn_after_empty == n_txn_before

    # از طریق API هم بدون moves صریح، درخواست رد می‌شود
    import app as m
    m.ENGINE = E
    with pytest.raises(Exception):
        m.api("/api/ponds/optimize/apply", "POST", {}, {"reason": "no moves given"})
    n_txn_final = E.db.one("SELECT COUNT(*) n FROM transactions")["n"]
    assert n_txn_final == n_txn_before


# ═══════════════ رگرسیون اصلاحات محدود بعدی: Mixed Capacity + Future Reservation
def test_mixed_pond_never_exceeds_100pct_capacity(tmp_path):
    """
    اصلاح ۳: حتی وقتی mixing لازم و مجاز (داخل tolerance) است، هیچ استخر
    مخلوط نباید از ۱۰۰٪ ظرفیت فیزیکی (بر مبنای سنگین‌ترین وزن) عبور کند.
    """
    E = _mk(tmp_path)
    _isolate(E)
    day = _today()
    _buy(E, "OVER-A", 60_000, day)
    _set_weight(E, "OVER-A", day, 10.0)
    _buy(E, "OVER-B", 60_000, day)
    _set_weight(E, "OVER-B", day, 10.5)

    A, bio, st = E.ctx(date.today())
    A.set("pond.max_weight_mix_tolerance_percent", 8)
    # فقط ۲ استخر عملیاتی — حتی با mixing هم کافی نیست (هرکدام تنها ~۴ لازم دارد)
    E.db.ex("UPDATE ponds SET role='reserve' WHERE pond_id NOT IN "
           "(SELECT pond_id FROM ponds WHERE role='operational' ORDER BY sort_order LIMIT 2)")
    try:
        A2, bio2, st2 = E.ctx(date.today())
        r = PO.optimize_layout(A2, bio2, st2, E.db)
        for p in r["proposed"]:
            if len(p["occupants"]) <= 1:
                continue
            count = sum(o["count"] for o in p["occupants"])
            w = max(o["mean_weight_g"] for o in p["occupants"])   # محافظه‌کارانه
            cap = bio2.fish_per_pond(w)
            assert count <= cap + 1e-6, (
                f"{p['pond_id']}: {count:.0f} قطعه از ظرفیت {cap:.0f} عبور کرده")
    finally:
        E.db.ex("UPDATE ponds SET role='operational' WHERE pond_id LIKE 'P%'")


def test_future_pond_need_does_not_cause_transfer_today(tmp_path):
    """
    اصلاح ۴: اگر cohort امروز کمتر و در آینده بیشتر به استخر نیاز دارد،
    فقط نیاز *امروز* باید Current Allocation واقعی (و انتقال واقعی) بگیرد؛
    ظرفیت اضافه برای آینده باید Future Reserved باشد، نه انتقال امروز.
    """
    E = _mk(tmp_path)
    _isolate(E)
    day = _today()
    _buy(E, "GROWS-FAST", 40_000, day)
    _set_weight(E, "GROWS-FAST", day, 3.0)

    A, bio, st = E.ctx(date.today())
    c = st.cohorts["GROWS-FAST"]
    need_today = PO._ponds_needed(bio, c.alive, c.mean_weight)
    horizon = int(A.get("pond.optimize_horizon_days"))
    pk = PO.peak_future_need(A, bio, st, c, horizon)
    assert pk["ponds"] > need_today, \
        "پیش‌فرض تست: این cohort باید در آینده به استخر بیشتری نیاز پیدا کند"

    r = PO.optimize_layout(A, bio, st, E.db)
    my_moves = [mv for mv in r["moves"] if mv["cohort_id"] == "GROWS-FAST"]
    assert len(my_moves) == need_today, (
        f"تعداد انتقال امروز ({len(my_moves)}) باید دقیقاً برابر نیاز امروز "
        f"({need_today}) باشد، نه نیاز آیندهٔ بزرگ‌تر")
    reserved = [fr for fr in r["future_reserved"] if fr["cohort_id"] == "GROWS-FAST"]
    assert reserved, "ظرفیت اضافهٔ آینده باید به‌عنوان Future Reserved نشان داده شود، نه انتقال امروز"
    # استخرهای رزروشدهٔ آینده در moves امروز ظاهر نشوند
    reserved_ponds = {fr["pond_id"] for fr in reserved}
    moved_ponds = {mv["to_pond"] for mv in my_moves}
    assert not (reserved_ponds & moved_ponds), \
        "استخر رزروشدهٔ آینده نباید هم‌زمان مقصد یک انتقال امروز باشد"


# ═══════════════ رگرسیون: ماژول سالم pond_alloc.py دست‌نخورده مانده
def test_existing_pond_alloc_suggest_still_works_unmodified(tmp_path):
    E = _mk(tmp_path)
    day = _today()
    _buy(E, "SUGGEST-ME", 45_000, day)
    _set_weight(E, "SUGGEST-ME", day, 3.0)
    A, bio, st = E.ctx(date.today())
    from core import pond_alloc as PALLOC
    sug = PALLOC.suggest(A, bio, st, E.db)
    assert any(row["cohort_id"] == "SUGGEST-ME" for row in sug["suggestions"])
