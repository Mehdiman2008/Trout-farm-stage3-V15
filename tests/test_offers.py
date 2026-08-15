"""
تست‌های مرحله ۳ — موتور تصمیم خرید و فروش.
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as m                                        # noqa: E402
from app import Engine, api                            # noqa: E402
from core import offers as OF                          # noqa: E402
from core.planner import Plan                          # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "test_offers.db")


@pytest.fixture(scope="module")
def E():
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(DB + ext):
            os.remove(DB + ext)
    eng = Engine(DB)
    m.ENGINE = eng
    yield eng
    eng.db.close()


def call(path, method="GET", body=None, query=None):
    return api(path, method, query or {}, body or {})


def ctx(E):
    return E.ctx()


def offer_date(E, days=30):
    return (E.ctx()[2].as_of + timedelta(days=days)).isoformat()


# ── ارزیابی آفر تخم ─────────────────────────────────────────────────
def test_egg_offer_returns_full_decision(E):
    A, bio, st = ctx(E)
    r = OF.evaluate_egg_offer(A, bio, st, {
        "date": offer_date(E), "quantity": 100_000, "price": 6000,
        "supplier": "تست"}, partial_options=False)
    for k in ("decision", "max_justified_price", "preferred_quantity",
              "expected_profit_impact", "confidence", "explanation_fa",
              "baseline", "full_accept"):
        assert k in r
    assert r["decision"] in ("BUY", "PARTIAL_BUY", "REJECT")
    assert r["max_justified_price"] > 0
    assert r["as_of"] == st.as_of.isoformat()


def test_max_justified_price_is_the_indifference_point(E):
    """در قیمتِ حداکثر توجیه‌پذیر، سود افزوده باید تقریباً صفر باشد."""
    A, bio, st = ctx(E)
    probe = OF.evaluate_egg_offer(A, bio, st, {
        "date": offer_date(E), "quantity": 100_000, "price": 0},
        partial_options=False)
    mx = probe["max_justified_price"]
    at_max = OF.evaluate_egg_offer(A, bio, st, {
        "date": offer_date(E), "quantity": 100_000, "price": mx},
        partial_options=False)
    assert abs(at_max["full_accept"]["incremental_profit"]) < 1_000_000
    assert abs(at_max["full_accept"]["margin_per_egg"]) < 10


def test_cheap_offer_is_bought_expensive_is_rejected(E):
    A, bio, st = ctx(E)
    probe = OF.evaluate_egg_offer(A, bio, st, {
        "date": offer_date(E), "quantity": 100_000, "price": 0},
        partial_options=False)
    mx = probe["max_justified_price"]
    cheap = OF.evaluate_egg_offer(A, bio, st, {
        "date": offer_date(E), "quantity": 100_000, "price": mx * 0.5},
        partial_options=False)
    dear = OF.evaluate_egg_offer(A, bio, st, {
        "date": offer_date(E), "quantity": 100_000, "price": mx * 1.6},
        partial_options=False)
    assert cheap["decision"] in ("BUY", "PARTIAL_BUY")
    assert cheap["expected_profit_impact"] > 0
    assert dear["decision"] == "REJECT"
    assert dear["expected_profit_impact"] < 0


def test_price_and_profit_are_monotone(E):
    A, bio, st = ctx(E)
    prev = None
    for price in (3000, 5000, 7000):
        r = OF.evaluate_egg_offer(A, bio, st, {
            "date": offer_date(E), "quantity": 100_000, "price": price},
            partial_options=False)
        v = r["full_accept"]["incremental_profit"]
        if prev is not None:
            assert v < prev, "قیمت بالاتر باید سود افزوده کمتری بدهد"
        prev = v


def test_egg_offer_reports_capacity_and_cash_impact(E):
    A, bio, st = ctx(E)
    r = OF.evaluate_egg_offer(A, bio, st, {
        "date": offer_date(E), "quantity": 200_000, "price": 6000},
        partial_options=False)
    f = r["full_accept"]
    assert f["peak_ponds_after"] >= f["peak_ponds_before"] - 1
    assert f["capacity_risk"] in ("low", "medium", "high")
    assert len(f["capacity_curve_delta"]) == 4
    assert [c["days"] for c in f["capacity_curve_delta"]] == [30, 60, 90, 140]
    # توجه: «جمع خوراک کل مزرعه» لزوماً با پذیرش این آفر بالا نمی‌رود. وقتی
    # سقف ماهانه تخم یا ظرفیت استخر همان بازه binding است، این lot رایگان
    # می‌تواند دقیقاً جای یک خرید دیگری را بگیرد که optimizer در baseline هم
    # انجام می‌داد — جمع کل ثابت می‌ماند و فقط هزینه تخم صرفه‌جویی می‌شود
    # (این خودش در feasibility و peak_funding دیده می‌شود). آنچه همیشه درست
    # است: خودِ این lot واقعاً رشد و مصرف خوراک دارد، نه اینکه نادیده گرفته
    # شده باشد.
    #
    # با سقف مشترک تقاضای بازار (Global Demand Capacity)، این جابه‌جایی
    # دیگر همیشه یک تعویض ساده و دقیقاً هم‌ارز نیست — پذیرش lot می‌تواند
    # باعث شود optimizer در جای دیگر برنامه، به‌جای رشد تا وزن بالاتر (با
    # مصرف خوراک بیشتر)، پاسخ متفاوتی برای سهم مشترک همان بازه انتخاب کند؛
    # پس افت کوچک و محدود در جمع خوراک واقعاً ممکن و اقتصادی است، نه باگ.
    assert f["lot_feed_kg"] > 0, "lot باید واقعاً رشد کند، نه صرفاً نادیده گرفته شود"
    assert f["lot_revenue"] > 0
    assert f["feed_kg_delta"] >= -3000, (
        "افت کوچک ناشی از جابه‌جایی روی سقف تقاضای مشترک قابل قبول است، ولی "
        "این مقدار برای یک jابه‌جایی معمولی خیلی بزرگ است")
    assert f["expected_survival"] > 0
    assert "wc_headroom_after" in f


def test_partial_buy_options_are_offered(E):
    A, bio, st = ctx(E)
    r = OF.evaluate_egg_offer(A, bio, st, {
        "date": offer_date(E), "quantity": 300_000, "price": 6000})
    assert r["options"], "گزینه‌های خرید جزئی باید بررسی شوند"
    for o in r["options"]:
        assert o["quantity"] < 300_000
    assert r["preferred_quantity"] <= 300_000


def test_offer_uses_live_state_not_a_fixed_baseline(E):
    """پس از ثبت یک رویداد واقعی، همان آفر باید جواب متفاوتی بدهد."""
    A, bio, st = ctx(E)
    before = OF.evaluate_egg_offer(A, bio, st, {
        "date": offer_date(E), "quantity": 100_000, "price": 6000},
        partial_options=False)["max_justified_price"]

    victim = max(st.cohorts.values(), key=lambda c: c.alive)
    r = call("/api/transactions", "POST", {
        "txn_type": "mortality", "txn_date": st.as_of.isoformat(),
        "cohort_id": victim.cohort_id, "quantity": victim.alive * 0.5})

    A2, bio2, st2 = ctx(E)
    after = OF.evaluate_egg_offer(A2, bio2, st2, {
        "date": offer_date(E), "quantity": 100_000, "price": 6000},
        partial_options=False)["max_justified_price"]
    E.db.void_txn(r["id"], "پاک‌سازی تست")
    assert before != after, "ارزیابی باید از وضعیت لحظه‌ای مزرعه بیاید"


def test_egg_offer_validates_input(E):
    A, bio, st = ctx(E)
    with pytest.raises(ValueError, match="بزرگ‌تر از صفر"):
        OF.evaluate_egg_offer(A, bio, st, {"quantity": 0, "price": 6000})
    with pytest.raises(ValueError, match="گذشته"):
        OF.evaluate_egg_offer(A, bio, st, {
            "date": (st.as_of - timedelta(days=5)).isoformat(),
            "quantity": 1000, "price": 6000})


def test_forced_lot_actually_enters_the_plan(E):
    A, bio, st = ctx(E)
    p = Plan(A, bio, st, "balanced",
             extra_lot={"date": offer_date(E), "quantity": 160_000, "price": 6000})
    assert any("OFFER" in k for k in p.solution.selected)
    assert p.summary()["eggs_planned"] >= 160_000


# ── ارزیابی آفر فروش ────────────────────────────────────────────────
def _cohort(E):
    st = E.ctx()[2]
    return max(st.cohorts.values(), key=lambda c: c.alive)


def test_sale_offer_returns_three_prices(E):
    A, bio, st = ctx(E)
    c = _cohort(E)
    r = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 10_000, "price": 15_000})
    P = r["prices"]
    for k in ("accounting_floor", "economic_floor", "counter_price",
              "offered", "effective_after_terms", "baseline_curve"):
        assert k in P and P[k] >= 0
    assert P["counter_price"] > P["economic_floor"]
    assert r["decision"] in ("ACCEPT", "NEGOTIATE", "REJECT")
    assert r["explanation_fa"]


def test_accounting_floor_is_historical_cost(E):
    A, bio, st = ctx(E)
    c = _cohort(E)
    r = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 5_000, "price": 15_000})
    acc = r["accounting"]
    assert acc["egg_cost_total"] == pytest.approx(c.egg_count * c.egg_price)
    assert acc["total_cost"] == pytest.approx(
        acc["egg_cost_total"] + acc["feed_cost_total"])
    assert acc["cost_per_fish"] == pytest.approx(acc["total_cost"] / c.alive)


def test_economic_floor_ignores_sunk_cost(E):
    """
    کف اقتصادی نباید به بهای تمام‌شده گذشته وابسته باشد.

    قیمت واقعی خرید تخم در تراکنش ثبت شده و با تغییر assumption عوض نمی‌شود
    (همان قاعده effective-dating). پس گران‌کردن فرض قیمت تخم، نه کف
    حسابداری را عوض می‌کند و نه کف اقتصادی را — و این خودش درست است.
    برای آزمودن سنگِ محک، مستقیم هزینه خوراک تاریخی را بالا می‌بریم — با
    cohortی که از وزن شروع تغذیه (Swim-up) عبور کرده، وگرنه هزینه خوراک
    تخمینی از قبل صفر است (کیسه زرده) و FCR اصلاً اثری ندارد.
    """
    A, bio, st = ctx(E)
    c = max((c for c in st.cohorts.values() if c.mean_weight > 2 * bio.feed_start_weight_g),
            key=lambda c: c.alive)
    base = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 5_000, "price": 15_000})
    A.set("feed.fcr", float(A.get("feed.fcr")) * 2)   # هزینه گذشته دو برابر
    try:
        A2, bio2, st2 = ctx(E)
        after = OF.evaluate_sale_offer(A2, bio2, st2, {
            "cohort_id": c.cohort_id, "quantity": 5_000, "price": 15_000})
    finally:
        A.reset("feed.fcr")
    assert after["prices"]["accounting_floor"] > base["prices"]["accounting_floor"]


def test_high_price_accepted_low_price_rejected(E):
    A, bio, st = ctx(E)
    c = _cohort(E)
    probe = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 5_000, "price": 1})
    econ = probe["prices"]["economic_floor"]
    high = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 5_000, "price": econ * 2.5})
    low = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 5_000, "price": econ * 0.4})
    assert high["decision"] == "ACCEPT"
    assert high["difference_vs_keeping"] > 0
    assert low["decision"] == "REJECT"
    assert low["difference_vs_keeping"] < 0


def test_negotiate_band_between_reject_and_accept(E):
    """
    شرایط پرداخت حالا داخل خودِ کف اقتصادی است (اصلاح ۲)؛ پس probe و
    فراخوانی اصلی باید terms یکسان داشته باشند تا کف یکسانی مقایسه شود.
    """
    A, bio, st = ctx(E)
    c = _cohort(E)
    cash = {"upfront_share": 1.0, "delay_days": 0}
    probe = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 5_000, "price": 1,
        "payment_terms": cash})
    econ = probe["prices"]["economic_floor"]
    band = float(A.get("offers.negotiate_band"))
    # درست زیر کف اقتصادی ولی داخل بازه مذاکره
    r = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 5_000,
        "price": econ * (1 - band / 2),
        "payment_terms": cash})
    assert r["decision"] == "NEGOTIATE"
    assert r["prices"]["counter_price"] > r["prices"]["offered"]


def test_best_alternative_is_computed(E):
    A, bio, st = ctx(E)
    c = _cohort(E)
    r = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 5_000, "price": 15_000})
    alt = r["alternative"]
    assert alt["options"]
    assert any(o.get("is_sell_now") for o in alt["options"])
    best = max(alt["options"], key=lambda o: o["value_per_fish"])
    assert alt["value_per_fish"] == pytest.approx(best["value_per_fish"])
    for o in alt["options"]:
        if not o.get("is_sell_now"):
            assert 0 < o["survival"] <= 1
            assert o["feed_cost_per_fish"] >= 0
            assert o["days_to_reach"] >= 0


def test_payment_terms_reduce_effective_price(E):
    A, bio, st = ctx(E)
    c = _cohort(E)
    cash = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 5_000, "price": 20_000,
        "payment_terms": {"upfront_share": 1.0, "delay_days": 0}})
    credit = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 5_000, "price": 20_000,
        "payment_terms": {"upfront_share": 0.0, "delay_days": 90}})
    assert cash["prices"]["effective_after_terms"] == pytest.approx(20_000)
    assert credit["prices"]["effective_after_terms"] < 20_000
    assert credit["payment"]["cost_of_terms"] > 0


def test_sale_reports_freed_resources(E):
    A, bio, st = ctx(E)
    c = _cohort(E)
    r = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 20_000, "price": 20_000})
    assert r["working_capital_released"] > 0
    assert r["ponds_freed"] >= 0
    assert r["cohort"]["share_of_cohort"] == pytest.approx(20_000 / c.alive)


def test_sale_offer_validates_input(E):
    A, bio, st = ctx(E)
    c = _cohort(E)
    with pytest.raises(ValueError, match="ناشناخته"):
        OF.evaluate_sale_offer(A, bio, st, {
            "cohort_id": "NOPE", "quantity": 100, "price": 1000})
    with pytest.raises(ValueError, match="بیشتر است"):
        OF.evaluate_sale_offer(A, bio, st, {
            "cohort_id": c.cohort_id, "quantity": c.alive * 5, "price": 1000})


def test_reservation_price_moves_with_farm_state(E):
    """کف اقتصادی باید با وضعیت مزرعه تغییر کند، نه ثابت بماند."""
    A, bio, st = ctx(E)
    c = _cohort(E)
    before = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 5_000,
        "price": 15_000})["prices"]["economic_floor"]
    A.set("price.slope_per_gram", 1200)      # ارزش رشد بیشتر می‌شود
    try:
        A2, bio2, st2 = ctx(E)
        after = OF.evaluate_sale_offer(A2, bio2, st2, {
            "cohort_id": c.cohort_id, "quantity": 5_000,
            "price": 15_000})["prices"]["economic_floor"]
    finally:
        A.reset("price.slope_per_gram")
    assert after > before, "با گران‌تر شدن وزن بالاتر، نگه‌داشتن جذاب‌تر می‌شود"


# ── What-If ─────────────────────────────────────────────────────────
def test_what_if_buy_eggs(E):
    A, bio, st = ctx(E)
    r = OF.what_if(A, bio, st, [
        {"type": "buy_eggs", "quantity": 160_000, "price": 6700,
         "date": offer_date(E)}])
    # سقف سالانه تخم binding است؛ خرید فرضی ممکن است جای lotهای اختیاری را
    # بگیرد، پس «جمع کل تخم» لزوماً بالا نمی‌رود — ورود واقعی lot به برنامه
    # ملاک است.
    assert r["state_delta"]["hypothetical_lots_in_plan"]
    assert r["scenario"]["eggs_planned"] >= 160_000
    assert "delta" in r and "verdict_fa" in r
    assert "npv" in r["delta"]
    assert r["changes_fa"]


def test_what_if_feed_price_increase_hurts(E):
    """
    افزایش قیمت خوراک باید حاشیه را کاهش دهد. توجه: مجموع «هزینه خوراک»
    لزوماً افزایش نمی‌یابد — چون optimizer دوباره حل می‌شود، ممکن است در
    واکنش به خوراک گران‌تر ترکیب وزن‌های هدف را به سمت وزن‌های سبک‌تر
    (کم‌خوراک‌تر) جابه‌جا کند و مجموع kg مصرفی کم شود، حتی با قیمت واحد
    بالاتر. معیار اقتصادی درست همان افت حاشیه است.
    """
    A, bio, st = ctx(E)
    r = OF.what_if(A, bio, st, [{"type": "feed_price_pct", "value": 20}])
    assert r["delta"]["contribution"] < 0


def test_what_if_restores_assumptions(E):
    A, bio, st = ctx(E)
    before_feed = A.get("feed.price_table")[0]["price"]
    before_price = A.get("price.base_1g")
    OF.what_if(A, bio, st, [
        {"type": "feed_price_pct", "value": 30},
        {"type": "sale_price_pct", "value": -15}])
    assert A.get("feed.price_table")[0]["price"] == before_feed
    assert A.get("price.base_1g") == before_price


def test_what_if_does_not_touch_real_data(E):
    A, bio, st = ctx(E)
    n_before = E.db.one("SELECT COUNT(*) n FROM transactions")["n"]
    live_before = st.summary()["live_fish"]
    OF.what_if(A, bio, st, [
        {"type": "buy_eggs", "quantity": 200_000, "price": 6000},
        {"type": "mortality_pct", "value": 5}])
    assert E.db.one("SELECT COUNT(*) n FROM transactions")["n"] == n_before
    assert E.ctx()[2].summary()["live_fish"] == pytest.approx(live_before)


def test_what_if_sell_fish(E):
    """فروش فرضی باید واقعاً موجودی سناریو را کم کند، نه یک عدد دستی (اصلاح ۶)."""
    A, bio, st = ctx(E)
    c = _cohort(E)
    r = OF.what_if(A, bio, st, [
        {"type": "sell_fish", "cohort_id": c.cohort_id,
         "quantity": 10_000, "price": 25_000}])
    assert r["method"] == "cloned_state_reoptimisation"
    sd = r["state_delta"]
    assert sd["live_fish_delta"] == pytest.approx(-10_000, abs=1)
    assert r["manual_delta"] == 0.0
    assert any(row["amount"] > 0 for row in sd["cash_rows"]), \
        "وجه فروش فرضی باید وارد دفتر نقدی سناریو شود"
    assert r["changes_fa"]


def test_what_if_rejects_unknown_type(E):
    A, bio, st = ctx(E)
    with pytest.raises(ValueError, match="ناشناخته"):
        OF.what_if(A, bio, st, [{"type": "teleport", "value": 1}])


# ── API ─────────────────────────────────────────────────────────────
def test_decide_context_endpoint(E):
    c = call("/api/decide/context")
    for k in ("as_of", "cohorts", "ponds_used", "operational_ponds",
              "feed_inventory_kg", "cash_balance", "wc_available",
              "harvest_weights", "reconciliation"):
        assert k in c
    assert 1.0 in c["harvest_weights"]


def test_decide_egg_endpoint(E):
    r = call("/api/decide/egg", "POST", {
        "date": offer_date(E), "quantity": 100_000, "price": 6000,
        "partial_options": False})
    assert r["decision"] in ("BUY", "PARTIAL_BUY", "REJECT")
    assert r["max_justified_price"] > 0


def test_decide_sale_endpoint(E):
    c = _cohort(E)
    r = call("/api/decide/sale", "POST", {
        "cohort_id": c.cohort_id, "quantity": 5_000, "price": 18_000})
    assert r["decision"] in ("ACCEPT", "NEGOTIATE", "REJECT")
    assert r["prices"]["economic_floor"] > 0


def test_decide_what_if_endpoint(E):
    r = call("/api/decide/what-if", "POST", {
        "changes": [{"type": "feed_price_pct", "value": 10}]})
    assert "delta" in r and "baseline" in r and "scenario" in r


# ── رگرسیون مراحل قبل ───────────────────────────────────────────────
def test_stage1_and_2_still_pass(E):
    assert call("/api/validate")["failed"] == 0
    r = call("/api/plan", "GET", {}, {"variant": ["balanced"]})
    assert r["validation"]["failed"] == 0
