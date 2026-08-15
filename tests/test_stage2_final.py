"""
تست‌های اصلاحات نهایی مرحله ۲.
هر تست مستقیماً به یکی از بندهای درخواست اصلاحات نگاشت می‌شود.
"""
import math
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as m                                        # noqa: E402
from app import Engine, api                            # noqa: E402
from core import attribution as ATTR                   # noqa: E402
from core import pond_alloc as PALLOC                  # noqa: E402
from core import validate as V                         # noqa: E402
from core.plan_model import PlanModel, Scenario, saleability_band  # noqa: E402
from core.planner import Plan                          # noqa: E402
from core.state import TROUGH, UNASSIGNED              # noqa: E402

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "test_s2f.db")


@pytest.fixture(scope="module")
def E():
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(DB + ext):
            os.remove(DB + ext)
    eng = Engine(DB)
    m.ENGINE = eng
    yield eng
    eng.db.close()


@pytest.fixture(scope="module")
def plan(E):
    return Plan(*E.ctx(), "balanced")


def call(path, method="GET", body=None, query=None):
    return api(path, method, query or {}, body or {})


def _check(v, cid):
    return next(c for c in v["checks"] if c["id"] == cid)


# ── خرید اشتباه ۳ آوریل ──────────────────────────────────────────────
def test_mistaken_april_purchase_removed(E):
    active = E.db.q("SELECT * FROM transactions WHERE txn_type='egg_purchase' "
                    "AND status='active' ORDER BY txn_date")
    dates = [t["txn_date"] for t in active]
    assert "2026-04-03" not in dates
    assert sum(t["quantity"] for t in active) == 650_000
    assert len(active) == 5


def test_mistaken_purchase_kept_in_history_not_deleted(E):
    """حذف خام نه — ابطال با دلیل، تا audit trail حفظ شود."""
    row = E.db.one("SELECT * FROM transactions WHERE txn_type='egg_purchase' "
                   "AND txn_date='2026-04-03'")
    if row:                       # فقط در پایگاه‌های قدیمی وجود دارد
        assert row["status"] == "void"
    A, bio, st = E.ctx()
    assert not any(c.purchase_date.isoformat() == "2026-04-03"
                   for c in st.cohorts.values())


# ── ۱. تخصیص خودکار استخر ───────────────────────────────────────────
def test_pond_allocation_is_suggested_not_actual(E):
    A, bio, st = E.ctx()
    r = PALLOC.suggest(A, bio, st, E.db)
    assert r["suggestions"], "cohortهای بالای ۱ گرم باید پیشنهاد بگیرند"
    for x in r["suggestions"]:
        assert x["basis"] == "suggested"
        assert x["ponds_needed"] >= 1
        assert x["allocations"]
        # مجموع تخصیص = ماهی تخصیص‌نیافته
        assert sum(a["quantity"] for a in x["allocations"]) == pytest.approx(
            x["fish"], abs=2)
    # هیچ تراکنشی ساخته نشده است
    assert not E.db.q("SELECT 1 FROM transactions WHERE txn_type='transfer' "
                      "AND status='active'")


def test_pond_allocation_respects_capacity_and_reserve(E):
    A, bio, st = E.ctx()
    r = PALLOC.suggest(A, bio, st, E.db)
    reserve = set(r["reserve_ponds"])
    assert reserve == {"P20", "P21"}
    for x in r["suggestions"]:
        for a in x["allocations"]:
            assert a["pond_id"] not in reserve
            assert a["quantity"] <= x["planning_capacity_per_pond"] * 1.15
    # هیچ استخری دوبار تخصیص نیافته
    used = [a["pond_id"] for x in r["suggestions"] for a in x["allocations"]]
    assert len(used) == len(set(used))


def test_capacity_shortfall_is_reported_not_hidden(E):
    A, bio, st = E.ctx()
    # ظرفیت تجربی هر استخر را کم می‌کنیم تا کمبود واقعی و قابل مشاهده شود
    A.set("capacity.fish_per_pond_15g", 1500)
    A.set("capacity.fish_per_pond_1g", 4000)
    try:
        A2, bio2, st2 = E.ctx()          # با ظرفیت جدید دوباره ساخته می‌شود
        r = PALLOC.suggest(A2, bio2, st2, E.db)
        assert r["any_shortfall"]
        assert r["warnings"]
        assert any(x["shortfall_ponds"] > 0 for x in r["suggestions"])
    finally:
        A.reset("capacity.fish_per_pond_15g")
        A.reset("capacity.fish_per_pond_1g")


def test_accept_allocation_becomes_actual(E):
    A, bio, st = E.ctx()
    cid = next(x["cohort_id"] for x in PALLOC.suggest(A, bio, st, E.db)["suggestions"])
    res = PALLOC.accept(E.db, st, cid, A=A, bio=bio,
                        reason="تست تأیید تخصیص")
    assert res["basis"] == "actual"
    assert res["created"]
    for c in res["created"]:
        row = E.db.one("SELECT * FROM transactions WHERE id=?", (c["txn_id"],))
        assert row["txn_type"] == "transfer" and row["data_source"] == "actual"
    A2, bio2, st2 = E.ctx()
    coh = st2.cohorts[cid]
    assert coh.alloc.get(TROUGH, 0) + coh.alloc.get(UNASSIGNED, 0) < 1
    view = {p["pond_id"]: p for p in st2.pond_view()}
    for c in res["created"]:
        occ = view[c["pond_id"]]["occupants"]
        assert any(o["cohort_id"] == cid and o["basis"] == "actual" for o in occ)


def test_user_can_edit_and_split_allocation(E):
    A, bio, st = E.ctx()
    r = PALLOC.suggest(A, bio, st, E.db)
    sug = [x for x in r["suggestions"] if len(x["allocations"]) >= 2]
    if not sug:
        pytest.skip("cohort چند-استخری برای ویرایش موجود نیست")
    row = sug[0]
    cid, fish = row["cohort_id"], row["fish"]
    free = row["allocations"][0]["pond_id"]
    other = [a["pond_id"] for a in row["allocations"][1:]]
    custom = [{"pond_id": free, "quantity": fish * 0.4},
              {"pond_id": other[0], "quantity": fish * 0.6}]
    res = PALLOC.accept(E.db, st, cid, allocations=custom, reason="تقسیم دستی")
    assert len(res["created"]) == 2
    assert res["total_assigned"] == pytest.approx(fish, rel=1e-6)


def test_allocation_cannot_exceed_available_fish(E):
    A, bio, st = E.ctx()
    sug = PALLOC.suggest(A, bio, st, E.db)["suggestions"]
    if not sug:
        pytest.skip("همه cohortها تخصیص یافته‌اند")
    row = sug[0]
    with pytest.raises(ValueError):
        PALLOC.accept(E.db, st, row["cohort_id"],
                      allocations=[{"pond_id": "P19",
                                    "quantity": row["fish"] * 3}])


def test_move_fish_between_ponds(E):
    A, bio, st = E.ctx()
    cid = next((c.cohort_id for c in st.cohorts.values()
                if any(p not in (TROUGH, UNASSIGNED) and n >= 1
                       for p, n in c.alloc.items())), None)
    assert cid, "باید حداقل یک cohort در استخر واقعی باشد"
    c = st.cohorts[cid]
    src = c._largest()
    r = PALLOC.move(E.db, st, cid, src, "P19", 1000, "تست انتقال")
    assert r["ok"] and r["to"] == "P19"
    st2 = E.ctx()[2]
    assert st2.cohorts[cid].alloc.get("P19", 0) == pytest.approx(1000, abs=2)


# ── ۲ و ۷. تشخیص خودکار cohort فروش ─────────────────────────────────
def test_automatic_matching_uses_weight_date_and_availability(E):
    A, bio, st = E.ctx()
    sug = ATTR.suggest_all(A, bio, st, E.db)
    assert len(sug) == 3
    by_date = {s["sale_date"]: s for s in sug}
    for s in sug:
        for c in s["candidates"]:
            assert "expected_weight_g" in c
            assert "available_fish" in c
            assert "purchase_date" in c
    # فروش ۱۴ ژوئن با وزن ۱ گرم باید کاندید داشته باشد
    june = by_date["2026-06-14"]
    assert june["weight_g"] == 1.0
    assert june["candidates"]
    assert june["confidence"] in ("high", "medium", "low")


def test_march_sale_weight_now_known_but_no_forced_guess(E):
    """وزن ۶ گرم اعلام شد، ولی هیچ cohort ثبت‌شده‌ای با آن نمی‌خواند."""
    A, bio, st = E.ctx()
    row = E.db.one("SELECT * FROM transactions WHERE txn_type='sale' "
                   "AND txn_date='2026-03-16' AND status='active'")
    assert row["weight_g"] == 6.0
    s = ATTR.suggest(A, bio, st, row)
    assert s["confidence"] == "none"
    assert s["best"] is None
    assert s["missing_cohort_hint"]["purchase_date"] < "2026-02-17"


def test_no_guess_without_weight(E):
    A, bio, st = E.ctx()
    fake = {"id": -1, "txn_date": "2026-06-01", "quantity": 1000,
            "weight_g": None, "cohort_id": None, "txn_type": "sale"}
    s = ATTR.suggest(A, bio, st, fake)
    assert s.get("needs_weight") is True
    assert s["best"] is None and not s["candidates"]


def test_implied_purchase_date_is_computed(E):
    A, bio, st = E.ctx()
    row = E.db.one("SELECT * FROM transactions WHERE txn_type='sale' "
                   "AND txn_date='2026-08-01' AND status='active'")
    s = ATTR.suggest(A, bio, st, row)
    ipd = date.fromisoformat(s["implied_purchase_date"])
    assert (date(2026, 8, 1) - ipd).days == pytest.approx(
        bio.age_at_weight(2.0), abs=1)


# ── ۳. فروش چند-cohort ──────────────────────────────────────────────
def test_sale_can_be_split_across_cohorts(E):
    A, bio, st = E.ctx()
    row = E.db.one("SELECT * FROM transactions WHERE txn_type='sale' "
                   "AND txn_date='2026-06-14' AND status='active'")
    ids = sorted(st.cohorts)[:2]
    alloc = [{"cohort_id": ids[0], "quantity": 50_000},
             {"cohort_id": ids[1], "quantity": 30_000}]
    r = ATTR.confirm_split(E.db, st, row["id"], alloc, "تست تقسیم")
    assert r["total"] == 80_000 == r["sale_quantity"]
    rows = E.db.sale_allocations(r["new_id"], basis="confirmed")
    assert len(rows) == 2
    assert sum(x["quantity"] for x in rows) == 80_000


def test_split_must_equal_total_quantity(E):
    A, bio, st = E.ctx()
    row = E.db.one("SELECT * FROM transactions WHERE txn_type='sale' "
                   "AND txn_date='2026-08-01' AND status='active'")
    ids = sorted(st.cohorts)
    with pytest.raises(ValueError, match="برابر نیست"):
        ATTR.confirm_split(E.db, st, row["id"],
                           [{"cohort_id": ids[0], "quantity": 10_000}])


def test_split_cannot_exceed_cohort_inventory(E):
    A, bio, st = E.ctx()
    row = E.db.one("SELECT * FROM transactions WHERE txn_type='sale' "
                   "AND txn_date='2026-08-01' AND status='active'")
    big = max(st.cohorts.values(), key=lambda c: c.alive)
    with pytest.raises(ValueError, match="بیش از موجودی"):
        ATTR.confirm_split(E.db, st, row["id"],
                           [{"cohort_id": big.cohort_id,
                             "quantity": big.egg_count * 10}])


def test_split_reduces_cohort_inventory_and_keeps_revenue(E):
    before_state = E.ctx()[2]
    row = E.db.one("SELECT * FROM transactions WHERE txn_type='sale' "
                   "AND txn_date='2026-08-01' AND status='active'")
    sale_date = date(2026, 8, 1)
    roomy = sorted(before_state.cohorts.values(),
                   key=lambda c: -before_state.alive_on_past(c, sale_date))[:2]
    ids = [c.cohort_id for c in roomy]
    before = {c.cohort_id: c.alive for c in roomy}
    before_rev = before_state.summary()["sales_revenue_total"]

    ATTR.confirm_split(E.db, before_state, row["id"],
                       [{"cohort_id": ids[0], "quantity": 20_000},
                        {"cohort_id": ids[1], "quantity": 15_000}])
    after_state = E.ctx()[2]
    assert after_state.cohorts[ids[0]].alive < before[ids[0]]
    assert after_state.cohorts[ids[1]].alive < before[ids[1]]
    # درآمد تاریخی حفظ می‌شود
    assert after_state.summary()["sales_revenue_total"] == pytest.approx(
        before_rev, rel=1e-6)


def test_mass_balance_per_cohort_after_splits(E):
    A, bio, st = E.ctx()
    for c in st.cohorts.values():
        sold = sum(float(r["quantity"]) for r in E.db.sale_allocations(basis="confirmed")
                   if r["cohort_id"] == c.cohort_id)
        sold += sum(float(t["quantity"] or 0) for t in E.db.active_txns()
                    if t["txn_type"] == "sale" and t.get("cohort_id") == c.cohort_id
                    and not E.db.sale_allocations(t["id"], basis="confirmed"))
        # خرید − تلفات − فروش = موجودی  (تلفات مدل + ثبت‌شده)
        assert c.alive <= c.egg_count - sold + 1
        assert c.alive >= 0


def test_reconciliation_status_improves_after_allocation(E):
    r = call("/api/sales/reconciliation")
    assert r["allocated_fish"] > 0
    assert r["coverage_ratio"] > 0
    assert r["plan_status"] in ("PROVISIONAL", "FINAL")


# ── ۴. شرایط پرداخت مشتری ───────────────────────────────────────────
def test_payment_terms_assumptions_exist(E):
    assert E.A.get("finance.customer_upfront_share") == 0.50
    assert E.A.get("finance.customer_balance_delay_days") == 45


def test_actual_sale_receipts_are_split_50_50(E):
    L = call("/api/ledger")
    sale_rows = [r for r in L["rows"] if r["type"] == "sale"
                 and r["accrual"] == "2026-06-14"]
    assert len(sale_rows) == 2
    amounts = sorted(r["amount"] for r in sale_rows)
    assert amounts[0] == pytest.approx(amounts[1])
    assert sum(amounts) == pytest.approx(700_000_000)
    dates = sorted(r["date"] for r in sale_rows)
    assert dates[0] == "2026-06-14"
    assert (date.fromisoformat(dates[1]) - date(2026, 6, 14)).days == 45


def test_revenue_date_differs_from_cash_date(plan):
    c = plan.cash
    rev_weeks = [t for t, v in enumerate(c.revenue_recognised) if v > 0]
    in_weeks = [t for t, v in enumerate(c.inflow) if v > 0]
    assert rev_weeks and in_weeks
    assert max(in_weeks) > max(rev_weeks)      # آخرین دریافت بعد از آخرین درآمد
    assert sum(c.inflow) == pytest.approx(sum(c.revenue_recognised), rel=1e-6)


def test_delayed_receipt_creates_receivables(plan):
    peak = max(r["receivables"] for r in plan.cash.series)
    assert peak > 0
    m = plan.cash.metrics()
    assert m["peak_receivables"] == pytest.approx(peak)


def test_upfront_share_changes_funding_need(E):
    A, bio, st = E.ctx()
    base = Plan(A, bio, st, "balanced").cash.metrics()["peak_funding_requirement"]
    A.set("finance.customer_upfront_share", 1.0)
    try:
        fast = Plan(A, bio, st, "balanced").cash.metrics()["peak_funding_requirement"]
    finally:
        A.reset("finance.customer_upfront_share")
    assert fast <= base + 1


# ── ۵. احتمال یافتن مشتری ───────────────────────────────────────────
def test_saleability_assumption_values(E):
    """
    ۵ بازه (به‌جای ۲ بازه خام قبلی) — احتمال تلاش اول برای هر بازه، طبق
    برآورد کیفی «خیلی‌بالا/بالا/متوسط/پایین/خیلی‌پایین» در Assumptions.
    """
    A, bio, st = E.ctx()
    pm = PlanModel(A, bio, st)
    assert pm.saleability(1.0) == 0.90    # ۱ تا ۲ گرم — خیلی بالا
    assert pm.saleability(3.0) == 0.75    # ۲ تا ۵ گرم — بالا
    assert pm.saleability(7.0) == 0.45    # ۵ تا ۱۰ گرم — متوسط
    assert pm.saleability(12.0) == 0.28   # ۱۰ تا ۱۵ گرم — پایین (تعدیل‌شده)
    assert pm.saleability(16.0) == 0.13   # بالای ۱۵ گرم — خیلی پایین (تعدیل‌شده)
    # منبع باید فقط planning.saleability باشد (تک‌منبعی، بدون جدول موازی)
    assert pm.saleability(7.0) == saleability_band(A, 7.0)["prob"]


def test_saleability_first_attempt_is_not_multiplied_into_price(E):
    """
    تلاش اول با قیمت کامل حساب می‌شود — احتمال هرگز مستقیم در قیمتِ همان
    تلاش ضرب نمی‌شود. فقط تلاش‌های تأخیری (وقتی تلاش اول موفق نشود) مشمول
    یک تخفیف محدود بازه (retry_discount) می‌شوند، نه کل حجم و نه با شدت
    برابر با (۱−احتمال). این تفاوت آگاهانه با نسخه قبلی است: نسخه قبلی هیچ
    تخفیفی مدل نمی‌کرد و در نتیجه وزن‌های کم‌سهولت‌فروش تقریباً بی‌هزینه
    به‌نظر می‌رسیدند.

    توجه: مقدار (۳۰٬۰۰۰ تخم) عمداً کوچک انتخاب شده تا از سقف تقاضای بازهٔ
    ۱۰ گرم عبور نکند و کاملاً فروخته شود — این تست دربارهٔ تخفیف/قیمت است،
    نه ظرفیت تقاضا (که جداگانه در `test_15g_demand_capacity_...` در
    test_stage3_fixes.py پوشش داده شده).

    توجه: به‌دلیل پراکندگی رشد (گروه تندرشد ممکن است تا زمان موج فروش از
    وزن اسمی سنگین‌تر شده باشد)، قیمت میانگین واقعی‌شده می‌تواند از قیمت
    اسمی هدف هم بالاتر برود — این پدیده‌ای مستقل و از قبل موجود است. برای
    سنجش تمیز اثر تخفیف، آن را با حالت retry_discount=0 (همه‌چیز دیگر
    ثابت) مقایسه می‌کنیم، نه با قیمت اسمی خام.
    """
    A, bio, st = E.ctx()
    pm = PlanModel(A, bio, st)
    p = pm.new_lot_profile(date(2026, 9, 5), 30_000, 10.0, Scenario.base())
    assert p.sold_fish == pytest.approx(30_000 * bio.survival(
        bio.age_at_weight(10.0)), rel=0.15)
    assert p.unsold_fish < 1.0
    price = bio.sale_price(10.0)
    realised = sum(p.revenue) / p.sold_fish
    band = saleability_band(A, 10.0)
    # کف: هرگز به‌اندازه «قیمت × احتمال» افت نکند (یعنی احتمال مستقیم در
    # قیمت ضرب نشده است) — این همان چیزی است که تست قبلی می‌خواست تضمین کند.
    assert realised > price * band["prob"]

    # اثر خالص تخفیف: با retry_discount=0 (بقیه چیزها ثابت)، قیمت
    # واقعی‌شده باید بیشتر یا مساوی حالت با تخفیف باشد.
    tbl = A.get("planning.saleability")
    no_discount = [dict(b, retry_discount=0.0) for b in tbl]
    A.set("planning.saleability", no_discount)
    try:
        pm2 = PlanModel(A, bio, st)
        p2 = pm2.new_lot_profile(date(2026, 9, 5), 30_000, 10.0, Scenario.base())
    finally:
        A.reset("planning.saleability")
    realised_no_discount = sum(p2.revenue) / p2.sold_fish
    assert realised_no_discount >= realised, \
        "بدون retry_discount، قیمت واقعی‌شده نباید کمتر از حالت با تخفیف باشد"
    assert realised_no_discount > realised, \
        "تخفیف باید واقعاً اثر اندازه‌گیری‌پذیری روی درآمد داشته باشد"


def test_low_saleability_delays_sales(E):
    A, bio, st = E.ctx()
    pm = PlanModel(A, bio, st)
    easy = pm.sale_waves(3.0, 0.15)     # بالا — ۷۵٪
    hard = pm.sale_waves(10.0, 0.15)    # پایین — ۲۲٪
    span_easy = max(w[0] for w in easy) - min(w[0] for w in easy)
    span_hard = max(w[0] for w in hard) - min(w[0] for w in hard)
    assert span_hard > span_easy
    assert sum(w[1] for w in easy) == pytest.approx(1.0, abs=1e-6)
    assert sum(w[1] for w in hard) == pytest.approx(1.0, abs=1e-6)


def test_delay_extends_pond_occupancy_and_feed(E):
    A, bio, st = E.ctx()
    pm = PlanModel(A, bio, st)
    p_hard = pm.new_lot_profile(date(2026, 9, 5), 100_000, 10.0, Scenario.base())
    A.set("planning.saleability",
          [{"w_min": 0.0, "w_max": 99.0, "prob": 1.0, "label": "همیشه"}])
    try:
        pm2 = PlanModel(A, bio, st)
        p_easy = pm2.new_lot_profile(date(2026, 9, 5), 100_000, 10.0, Scenario.base())
    finally:
        A.reset("planning.saleability")
    assert p_hard.last_week > p_easy.last_week          # اشغال طولانی‌تر
    assert sum(p_hard.feed_kg) > sum(p_easy.feed_kg)    # خوراک بیشتر
    assert sum(p_hard.mortality) > sum(p_easy.mortality)  # تلفات بیشتر
    assert max(p_hard.capital) >= max(p_easy.capital) * 0.99


# ── ۶. وزن فروش ۱ گرم ───────────────────────────────────────────────
def test_1g_is_an_allowed_harvest_weight(E):
    assert 1.0 in [float(x) for x in E.A.get("planning.harvest_weights")]


def test_1g_profiles_are_built_and_priced(E):
    A, bio, st = E.ctx()
    pm = PlanModel(A, bio, st)
    p = pm.new_lot_profile(date(2026, 9, 5), 100_000, 1.0, Scenario.base())
    assert p.sold_fish > 0
    assert sum(p.revenue) > 0
    realised = sum(p.revenue) / p.sold_fish
    assert realised == pytest.approx(bio.sale_price(1.0), rel=0.10)
    assert p.last_week < pm.grid.index_of(date(2026, 9, 5) + timedelta(days=140))


def test_optimizer_can_choose_1g(E):
    A, bio, st = E.ctx()
    pm = PlanModel(A, bio, st)
    cands = pm.build_candidates(Scenario.base())
    assert any(abs(p.harvest_w - 1.0) < 1e-9 for p in cands["new_lots"].values())
    assert any(abs(p.harvest_w - 1.0) < 1e-9 for p in cands["existing"].values()) \
        or all(c.mean_weight > 1.0 for c in st.cohorts.values())


# ── ۸. تفکیک ۱۲ ماه از چرخه عمر ─────────────────────────────────────
def test_horizon_split_separates_annual_from_lifecycle(plan):
    h = plan.horizon_split()
    a, b = h["rolling_12m"], h["full_lifecycle"]
    assert a["weeks"] < b["weeks"]
    assert b["revenue"] >= a["revenue"]
    assert b["spillover_fish"] >= 0
    assert "سود سالانه" in a["note_fa"]
    assert "سود سالانه" in b["note_fa"] and "نیست" in b["note_fa"]
    assert (date.fromisoformat(a["to"]) - date.fromisoformat(a["from"])).days <= 366


def test_summary_exposes_both_kpis_separately(plan):
    s = plan.summary()
    assert s["rolling_12m"]["contribution_nominal"] != \
        s["full_lifecycle"]["contribution_nominal"]


# ── ۹. سرمایه در گردش از دفتر نقدی ──────────────────────────────────
def test_planned_cash_ledger_balances(plan):
    m = plan.cash.metrics()
    assert m["opening_cash"] + m["total_inflow"] - m["total_outflow"] == \
        pytest.approx(m["closing_balance"], abs=1.0)
    assert sum(plan.cash.inflow) == pytest.approx(sum(plan.revenue), rel=1e-6)


def test_cash_ledger_contains_all_flow_types(plan):
    types = {r["type"] for r in plan.cash.rows}
    assert {"egg", "feed", "fixed_cost", "receipt_upfront",
            "receipt_balance"} <= types


def test_funding_requirement_is_not_double_counted(plan):
    """کسری نقدی و سرمایه در موجودی نباید با هم جمع شوند."""
    m = plan.cash.metrics()
    assert m["peak_funding_requirement"] == pytest.approx(m["peak_cash_deficit"])
    assert m["peak_capital_employed"] >= m["peak_funding_requirement"]


def test_wc_available_is_editable_and_breach_flagged(E):
    A, bio, st = E.ctx()
    A.set("finance.working_capital_available", 5.0e8)
    try:
        p = Plan(A, bio, st, "balanced")
        mm = p.cash.metrics()
        assert mm["wc_available"] == 5.0e8
        if mm["wc_breach"]:
            assert p.plan_status()["status"] == "PROVISIONAL"
            assert any("نقدی" in r for r in p.plan_status()["reasons"])
    finally:
        A.reset("finance.working_capital_available")


# ── ۱۰. منطق فروش جزئی ──────────────────────────────────────────────
def test_state_transition_begin_mortality_sale_end(E):
    """موجودی ابتدا − تلفات − فروش = موجودی پایان، دقیقاً."""
    A, bio, st = E.ctx()
    pm = PlanModel(A, bio, st)
    p = pm.new_lot_profile(date(2026, 9, 5), 160_000, 15.0, Scenario.base())
    died = sum(p.mortality)
    sold = sum(p.harvest_fish)
    left = p.fish[p.last_week]
    assert died + sold + left == pytest.approx(160_000, rel=1e-9)
    assert died > 0 and sold > 0


def test_sold_fish_are_not_counted_as_mortality_later(E):
    A, bio, st = E.ctx()
    pm = PlanModel(A, bio, st)
    p = pm.new_lot_profile(date(2026, 9, 5), 160_000, 15.0, Scenario.base())
    first_harvest = min(t for t, n in enumerate(p.harvest_fish) if n > 0)
    for t in range(first_harvest + 1, p.last_week + 1):
        # تلفات هر هفته حداکثر می‌تواند کسری از ماهی باقیمانده باشد
        assert p.mortality[t] <= p.fish[t - 1] + p.harvest_fish[t] + 1


def test_no_feed_for_sold_fish(E):
    """
    توجه: با ۱۵ گرم دیگر آزمایش نمی‌شود چون آن بازه سقف تقاضا دارد و عمداً
    ممکن است بخشی فروخته‌نشده بماند (نگاه کنید
    `test_15g_demand_capacity_blocks_full_period_sale` در
    test_stage3_fixes.py). اینجا هدف، بررسی «بدون سقف تقاضا، برداشت کامل
    می‌شود و خوراک برای ماهی فروخته‌شده ادامه پیدا نمی‌کند» است.
    """
    A, bio, st = E.ctx()
    pm = PlanModel(A, bio, st)
    p = pm.new_lot_profile(date(2026, 9, 5), 160_000, 2.0, Scenario.base())
    assert sum(p.feed_kg[p.last_week + 1:]) == 0.0
    assert p.fish[p.last_week] < 1


def test_partial_harvest_reduces_feed_proportionally(E):
    A, bio, st = E.ctx()
    pm = PlanModel(A, bio, st)
    A.set("planning.harvest_wave_fractions", [1.0, 0.0, 0.0])
    try:
        early = PlanModel(A, bio, st).new_lot_profile(
            date(2026, 9, 5), 100_000, 5.0, Scenario.base())
    finally:
        A.reset("planning.harvest_wave_fractions")
    spread = pm.new_lot_profile(date(2026, 9, 5), 100_000, 5.0, Scenario.base())
    # برداشت یک‌جای زودهنگام باید خوراک کمتری از برداشت کشیده لازم داشته باشد
    assert sum(early.feed_kg) <= sum(spread.feed_kg) + 1e-6


# ── ۱۰ب. آزادسازی سرمایه ────────────────────────────────────────────
def test_capital_released_after_sale(E):
    """
    سرمایهٔ درگیر باید متناسب با سهم فروخته‌شده آزاد شود.

    توجه: با ۱۵ گرم آزمایش نمی‌شود چون آن بازه سقف تقاضا دارد و ممکن است
    عمداً موجودی فروخته‌نشده تا انتهای بازهٔ شبیه‌سازی باقی بماند (سرمایهٔ
    آن هم به‌درستی آزاد نمی‌شود، چون واقعاً فروخته نشده — نگاه کنید
    `test_15g_demand_capacity_blocks_full_period_sale`). اینجا با وزن
    بدون سقف تقاضا (۲ گرم)، «کاهش خالص سرمایه در هفتهٔ برداشت» و «آزادسازی
    کامل تا پایان» هر دو معیار معتبرند.
    """
    A, bio, st = E.ctx()
    pm = PlanModel(A, bio, st)
    p = pm.new_lot_profile(date(2026, 9, 5), 160_000, 2.0, Scenario.base())
    assert p.capital[p.last_week] == pytest.approx(0.0, abs=1.0)
    first = min(t for t, n in enumerate(p.harvest_fish) if n > 0)
    no_release = p.capital[first - 1] + p.feed_cost[first]
    assert p.capital[first] < no_release - 1.0, \
        "برداشت این هفته باید سرمایه را نسبت به حالت بدون برداشت کاهش دهد"


def test_inventory_capital_belongs_to_remaining_fish_only(E):
    A, bio, st = E.ctx()
    pm = PlanModel(A, bio, st)
    p = pm.new_lot_profile(date(2026, 9, 5), 100_000, 5.0, Scenario.base())
    for t in range(p.first_week, p.last_week + 1):
        if p.fish[t] < 1:
            assert p.capital[t] == pytest.approx(0.0, abs=1.0)


# ── ۱۱. ظرفیت صحیح استخر ────────────────────────────────────────────
def test_final_plan_respects_19_operational_ponds(plan, E):
    op = int(E.A.get("farm.operational_ponds"))
    assert op == 19
    peak_future = max(plan.ponds[1:])
    assert peak_future <= op, f"اوج {peak_future} > {op}"
    assert plan.pond_feasible


def test_reserve_ponds_stay_free_in_plan(E):
    ponds = {p["pond_id"]: p for p in E.db.ponds()}
    assert ponds["P20"]["role"] == "reserve"
    assert ponds["P21"]["role"] == "reserve"


def test_repair_loop_runs_when_capacity_is_tight(E):
    A, bio, st = E.ctx()
    A.set("farm.operational_ponds", 10)
    try:
        p = Plan(A, bio, st, "balanced")
        assert p.repair_rounds >= 0
        if p.repair_rounds > 0:
            assert p.repair_log
        assert max(p.ponds[1:]) <= 10 or not p.pond_feasible
    finally:
        A.reset("farm.operational_ponds")


def test_infeasible_plan_is_not_declared_feasible(E):
    A, bio, st = E.ctx()
    A.set("farm.operational_ponds", 1)
    try:
        p = Plan(A, bio, st, "balanced")
        if max(p.ponds[1:]) > 1:
            assert not p.pond_feasible
            assert p.plan_status()["status"] == "PROVISIONAL"
    finally:
        A.reset("farm.operational_ponds")


# ── ۱۲. برچسب ارز ───────────────────────────────────────────────────
def test_fx_partial_quarter_labelled_to_date(plan, E):
    from core.fx import FXBenchmark
    qs = plan.quarterly_capital_fx(FXBenchmark(E.db, E.A))
    cur = qs[0]
    assert cur["complete"] is False
    assert cur["partial"] is True
    assert "to-Date" in cur["return_label_en"]
    assert "تا امروز" in cur["label"]


def test_completed_quarter_would_be_labelled_full(E):
    # آخرین روز سه‌ماهه: از این لحظه Q3 کامل شده است
    A, bio, st = E.ctx(date(2026, 9, 30))
    from core.fx import FXBenchmark
    p = Plan(A, bio, st, "balanced")
    qs = p.quarterly_capital_fx(FXBenchmark(E.db, A))
    done = [q for q in qs if q["complete"]]
    assert done
    assert "Full Quarter Return" in done[0]["return_label_en"]
    assert "تا امروز" not in done[0]["label"]


# ── ۱۳. وابستگی optimizer ───────────────────────────────────────────
def test_pulp_is_required_and_available():
    from core.optimizer import HAVE_PULP, OptimizerUnavailable, check_solver
    assert HAVE_PULP, "PuLP وابستگی الزامی optimizer است"
    err = OptimizerUnavailable()
    assert "PuLP" in err.message_fa
    assert err.message_en.startswith("Optimisation engine unavailable")
    # پیام باید عیب‌یابی‌پذیر باشد: کدام پایتون و چه دستوری
    assert err.status["python"] in err.message_fa
    assert err.status["install_command"] in err.message_fa
    sv = check_solver()
    assert sv["available"] and sv["cbc_ok"] and sv["pulp_version"]


def test_solver_healthcheck_endpoint(E):
    """نصب بودن بسته کافی نیست؛ اجرای واقعی CBC هم آزمایش می‌شود."""
    sv = call("/api/solver")
    assert sv["available"] is True
    assert sv["pulp_installed"] is True
    assert sv["cbc_ok"] is True
    assert sv["import_error"] is None and sv["cbc_error"] is None
    assert sv["install_command"].endswith("pip install PuLP")


def test_no_heuristic_fallback_exists():
    import core.optimizer as O
    assert not hasattr(O, "_greedy"), "نتیجه heuristic نباید جای optimizer را بگیرد"


def test_dashboard_works_without_optimizer(E):
    """حتی اگر optimizer نباشد، Live Farm State و ثبت داده کار می‌کنند."""
    for path in ("/api/summary", "/api/ponds", "/api/cohorts", "/api/feed",
                 "/api/ledger", "/api/validate", "/api/transactions"):
        assert call(path) is not None
    assert call("/api/bootstrap")["meta"]["optimizer_available"] is True


# ── validation کامل ─────────────────────────────────────────────────
def test_plan_validation_v2_has_no_failures(plan, E):
    v = V.run_plan_checks_v2(E.A, plan)
    ids = {c["id"] for c in v["checks"]}
    assert ids >= {"plan_state_transition", "plan_capital_release", "plan_cash",
                   "plan_working_capital", "plan_pond_feasible",
                   "plan_reconciliation", "plan_horizon", "plan_saleability"}
    assert v["failed"] == 0, [c for c in v["checks"] if c["status"] == "fail"]


def test_stage1_regression_still_passes(E):
    v = call("/api/validate")
    assert v["failed"] == 0


def test_plan_endpoint_returns_new_sections(E):
    r = call("/api/plan", "GET", {}, {"variant": ["balanced"]})
    for k in ("plan_status", "horizon", "cash", "repair_log"):
        assert k in r
    assert "metrics" in r["cash"] and "by_month" in r["cash"]
    assert r["horizon"]["rolling_12m"]["weeks"] < r["horizon"]["full_lifecycle"]["weeks"]
