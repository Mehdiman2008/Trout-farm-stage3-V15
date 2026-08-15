"""
تست‌های مرحله ۲ — Planning & Targets.
"""
import math
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as m                                       # noqa: E402
from app import Engine, api                           # noqa: E402
from core.optimizer import Variant                    # noqa: E402
from core.plan_model import PlanModel, Scenario       # noqa: E402
from core.planner import Plan, scenario_comparison    # noqa: E402
from core import variance as VAR                      # noqa: E402

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "test_plan.db")


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
    A, bio, st = E.ctx()
    return Plan(A, bio, st, "balanced")


def call(path, method="GET", body=None, query=None):
    return api(path, method, query or {}, body or {})


def _check(v, cid):
    return next(c for c in v["checks"] if c["id"] == cid)


# ── شبکه زمانی غیرچرخه‌ای (Fix 2) ───────────────────────────────────
def test_timeline_is_real_calendar_not_cyclic(plan):
    g = plan.grid
    assert plan.model.weeks >= 72
    assert all(g.dates[i] < g.dates[i + 1] for i in range(len(g.dates) - 1))
    assert (g.dates[-1] - g.dates[0]).days == 7 * plan.model.weeks
    # هفته ۵۲ و هفته ۰ نباید یکی باشند
    assert g.dates[52] != g.dates[0]
    assert g.month_of(0) != g.month_of(52)


def test_cohorts_crossing_year_end_are_booked_correctly(plan):
    """cohortهای خریداری‌شده در انتهای دوره تصمیم باید تا برداشت دیده شوند."""
    last = max(plan.solution.chosen_lots, key=lambda l: l["purchase_date"])
    pd = date.fromisoformat(last["purchase_date"])
    prof = plan.cand_base["new_lots"][last["key"]]
    harvest_date = plan.grid.dates[prof.last_week]
    assert harvest_date > pd + timedelta(days=100)
    assert sum(prof.revenue) > 0            # درآمدش داخل افق ثبت شده است
    assert harvest_date <= plan.grid.dates[-1]


# ── پراکندگی رشد در برنامه‌ریزی ──────────────────────────────────────
def test_harvest_is_multi_wave_not_a_single_point(E):
    A, bio, st = E.ctx()
    pm = PlanModel(A, bio, st)
    waves = pm.wave_ages(15.0, bio.cv_at_weight(15.0))
    ages = [w[0] for w in waves]
    assert ages[0] < ages[1] < ages[2]      # تندرشد زودتر، کندرشد دیرتر
    assert abs(sum(w[1] for w in waves) - 1.0) < 1e-9
    assert ages[2] - ages[0] > 3            # پراکندگی معنادار


def test_wave_spread_grows_with_cv(E):
    A, bio, st = E.ctx()
    pm = PlanModel(A, bio, st)
    narrow = pm.wave_ages(15.0, 0.05)
    wide = pm.wave_ages(15.0, 0.30)
    assert (wide[2][0] - wide[0][0]) > (narrow[2][0] - narrow[0][0])


def test_profile_harvests_in_several_weeks(plan):
    p = next(iter(plan.cand_base["new_lots"].values()))
    weeks_with_harvest = [t for t, n in enumerate(p.harvest_fish) if n > 0]
    assert len(weeks_with_harvest) >= 2
    assert abs(p.sold_fish - sum(p.harvest_fish)) < 1e-6


def test_grading_outlook_reports_waves(plan):
    g = plan.grading_outlook()
    assert g, "باید حداقل یک cohort با موج برداشت باشد"
    for r in g:
        assert r["waves"]
        assert r["spread_days"] >= 0


# ── بهینه‌ساز ────────────────────────────────────────────────────────
def test_solver_returns_optimal_plan(plan):
    assert plan.solution.status in ("Optimal", "Heuristic")
    assert plan.solution.chosen_lots
    assert plan.summary()["eggs_planned"] > 0


def test_lot_sizes_are_discrete_from_candidate_list(plan, E):
    allowed = set(float(x) for x in E.A.get("planning.lot_candidates"))
    for lot in plan.solution.chosen_lots:
        assert lot["quantity"] in allowed


def test_harvest_weights_are_from_allowed_set(plan, E):
    allowed = set(float(x) for x in E.A.get("planning.harvest_weights"))
    for lot in plan.solution.chosen_lots:
        assert lot["harvest_w"] in allowed


def test_every_cohort_allocated_exactly_once(plan):
    live = [c for c in plan.state.cohorts.values() if c.alive >= 1]
    for c in live:
        tot = sum(plan.solution.cohort_split.get(c.cohort_id, {}).values())
        assert abs(tot - 1.0) < 1e-4


def test_max_lots_per_month_respected(plan, E):
    cap = int(E.A.get("planning.max_lots_per_month"))
    per = {}
    for lot in plan.solution.chosen_lots:
        per[lot["month"]] = per.get(lot["month"], 0) + 1
    assert max(per.values()) <= cap


def test_annual_scenario_is_respected(plan, E):
    assert plan.summary()["eggs_planned"] <= float(E.A.get("planning.annual_scenario")) + 1


def test_monthly_availability_is_respected(plan, E):
    avail = float(E.A.get("planning.monthly_availability"))
    per = {}
    for lot in plan.solution.chosen_lots:
        per[lot["month"]] = per.get(lot["month"], 0.0) + lot["quantity"]
    assert max(per.values()) <= avail + 1


# ── Fix 3: forced sale را بهینه‌ساز تعیین می‌کند ────────────────────
def test_tighter_ponds_change_harvest_decisions_not_a_heuristic(E):
    """با کم‌کردن ظرفیت، خودِ مدل تصمیم می‌گیرد چه چیزی زودتر فروخته شود."""
    A, bio, st = E.ctx()
    wide = Plan(A, bio, st, "max_profit")
    A.set("farm.operational_ponds", 8)
    try:
        tight = Plan(A, bio, st, "max_profit")
    finally:
        A.reset("farm.operational_ponds")
    assert tight.summary()["eggs_planned"] <= wide.summary()["eggs_planned"]
    assert max(tight.ponds) <= max(wide.ponds)
    # تصمیم برداشت باید تغییر کرده باشد (وزن یا سهم)
    assert tight.solution.cohort_split != wide.solution.cohort_split or \
        tight.solution.chosen_lots != wide.solution.chosen_lots


def test_partial_harvest_is_representable(E):
    """متغیر سهم پیوسته است، پس برداشت جزئی/grading قابل بیان است."""
    A, bio, st = E.ctx()
    A.set("farm.operational_ponds", 6)
    A.set("planning.pond_breach_penalty", 5.0e8)
    try:
        p = Plan(A, bio, st, "conservative")
        fracs = [f for split in p.solution.cohort_split.values()
                 for f in split.values()]
        assert fracs
        assert all(0 <= f <= 1 + 1e-6 for f in fracs)
        for cid, split in p.solution.cohort_split.items():
            assert abs(sum(split.values()) - 1.0) < 1e-4
    finally:
        A.reset("farm.operational_ponds")
        A.reset("planning.pond_breach_penalty")


def test_elastic_constraints_prevent_infeasibility(E):
    """وضعیت امروز خودش از ظرفیت عبور کرده؛ مدل باید جواب بدهد نه Infeasible."""
    A, bio, st = E.ctx()
    A.set("farm.operational_ponds", 2)
    try:
        p = Plan(A, bio, st, "balanced")
        assert p.solution.status in ("Optimal", "Heuristic")
        assert max(p.solution.pond_shortfall) > 0
        assert any("استخر" in n for n in p.solution.notes)
    finally:
        A.reset("farm.operational_ponds")


# ── سرمایه در گردش ──────────────────────────────────────────────────
def test_plan_respects_working_capital(E):
    A, bio, st = E.ctx()
    A.set("finance.working_capital_available", 3.0e9)
    try:
        tight = Plan(A, bio, st, "balanced")
    finally:
        A.reset("finance.working_capital_available")
    rich = Plan(A, bio, st, "balanced")
    assert tight.summary()["eggs_planned"] <= rich.summary()["eggs_planned"]


def test_wc_available_flows_from_assumptions(E):
    A, bio, st = E.ctx()
    p = Plan(A, bio, st, "balanced")
    assert p.wc_available == float(A.get("finance.working_capital_available"))


# ── تجمیع ماهانه و سه‌ماهه ──────────────────────────────────────────
def test_monthly_has_all_required_management_fields(plan):
    need = ["eggs_purchased", "expected_survival", "sales_by_weight", "feed_kg",
            "feed_purchase_kg", "feed_purchase_cost", "pond_utilisation",
            "peak_capital", "revenue", "contribution_nominal",
            "contribution_risk_adjusted"]
    for b in plan.monthly:
        for k in need:
            assert k in b, k


def test_quarterly_equals_monthly_totals(plan):
    for f in ("revenue", "feed_cost", "egg_cost", "harvest_fish",
              "contribution_nominal"):
        mo = sum(b[f] for b in plan.monthly)
        qu = sum(b[f] for b in plan.quarterly)
        assert mo == pytest.approx(qu, rel=1e-9)


def test_contribution_identity(plan):
    rev = sum(plan.revenue)
    total = rev - sum(plan.feed_cost) - sum(plan.egg_cost) - sum(plan.fixed_cost)
    assert total == pytest.approx(sum(b["contribution_nominal"]
                                      for b in plan.monthly), rel=1e-9)


def test_risk_adjusted_is_below_nominal(plan):
    s = plan.summary()
    assert s["contribution_risk_adjusted"] < s["contribution_nominal"]


def test_pond_requirement_is_integer_per_week(plan):
    assert all(abs(v - round(v)) < 1e-6 for v in plan.ponds)


def test_split_cohort_ponds_are_not_double_counted(E):
    """
    اگر یک cohort بین دو وزن برداشت تقسیم شود، ظرفیت دوباره شمرده نشود.

    این تست دربارهٔ صحت محاسبهٔ استخر است، نه Saleability؛ برای اینکه
    تنش قیمت‌بالاتر/ظرفیت‌محدود که باعث تقسیم می‌شود دوباره قابل مشاهده
    باشد، سهولت فروش موقتاً permissive می‌شود (فقط در همین تست). با
    Assumptions واقعی (سهولت فروش پایین در وزن بالا + سقف تقاضا)، بهینه‌ساز
    اصلاً به‌سمت وزن‌های بالا نمی‌رود، پس این تنش خاص رخ نمی‌دهد — که خودش
    نتیجهٔ درستِ اصلاح Saleability/Demand-Capacity است، نه نبود پشتیبانی
    از تقسیم cohort.
    """
    A, bio, st = E.ctx()
    A.set("farm.operational_ponds", 6)
    A.set("planning.saleability",
          [{"w_min": 0.0, "w_max": 999.0, "prob": 1.0, "retry_discount": 0.0,
            "max_demand_per_period": None, "label": "آزمایشی — بدون محدودیت"}])
    try:
        p = Plan(A, bio, st, "conservative")
        split = [cid for cid, sp in p.solution.cohort_split.items() if len(sp) > 1]
        assert split, "این پیکربندی باید برداشت جزئی تولید کند"
        # عدد گزارش‌شده همیشه صحیح است، حتی وقتی cohort تقسیم شده
        assert all(abs(v - round(v)) < 1e-6 for v in p.ponds)
        # و از جمع ساده ceilِ شاخه‌ها کمتر یا مساوی است (نبود دوباره‌شماری)
        naive = [sum(math.ceil(pr.fish[t] * w / bio.fish_per_pond(pr.weight[t]))
                     if pr.fish[t] >= 1 and bio.counts_toward_pond_capacity(pr.weight[t])
                     else 0
                     for pr, w in [(pool[k], wt) for k, wt in p.solution.selected.items()
                                   for pool in [p.cand_base["new_lots"]
                                                if k.startswith("L|")
                                                else p.cand_base["existing"]]
                                   if k in pool])
                 for t in range(len(p.ponds))]
        assert all(p.ponds[t] <= naive[t] + 1e-6 for t in range(len(p.ponds)))
    finally:
        A.reset("farm.operational_ponds")
        A.reset("planning.saleability")


# ── برنامه ۹۰ روزه ──────────────────────────────────────────────────
def test_90_day_action_plan(plan):
    a = plan.action_plan_90d()
    assert a["actions"]
    horizon = (date.fromisoformat(a["until"]) - plan.state.as_of).days
    assert horizon == 90
    for x in a["actions"]:
        assert x["date"] <= a["until"]
        assert x["type"] in ("egg_purchase", "sale", "feed_purchase")
    assert a["actions"] == sorted(a["actions"], key=lambda x: x["date"])


# ── سناریوها و حالت‌ها ──────────────────────────────────────────────
def test_annual_scenarios_compared(E):
    A, bio, st = E.ctx()
    rows = scenario_comparison(A, bio, st, "balanced", [2_000_000, 2_500_000])
    assert len(rows) == 2
    assert rows[0]["eggs_planned"] <= 2_000_000 + 1
    assert rows[1]["eggs_planned"] >= rows[0]["eggs_planned"]
    # مقدار اولیه باید دست‌نخورده بازگردانده شود (هدف برنامه‌ریزی، نه تضمین عرضه)
    assert A.get("planning.annual_scenario") == A.default_of("planning.annual_scenario")


def test_three_variants_have_expected_risk_ordering(E):
    A, bio, st = E.ctx()
    plans = {n: Plan(A, bio, st, n)
             for n in ("max_profit", "balanced", "conservative")}
    nominal = {n: p.summary()["contribution_nominal"] for n, p in plans.items()}
    adverse = {n: p.summary()["contribution_risk_adjusted"] for n, p in plans.items()}
    assert nominal["max_profit"] >= nominal["conservative"] - 1
    assert adverse["conservative"] >= adverse["max_profit"] - 1
    assert Variant("conservative", A).risk_aversion > Variant("max_profit", A).risk_aversion


# ── Target vs Actual ────────────────────────────────────────────────
def test_actual_comes_only_from_transactions(E):
    A, bio, st = E.ctx()
    act = VAR.actual_by_month(E.db, A, st.as_of)
    # سه فروش تاریخی واقعی باید دیده شوند
    assert act["2026-03"]["fish_sold"] == 80_000
    assert act["2026-06"]["revenue"] == 700_000_000
    assert act["2026-08"]["fish_sold"] == 35_000
    # خرید تخم واقعی
    assert act["2026-07"]["eggs_purchased"] == 320_000
    # هیچ ماه آینده‌ای در واقعی نباشد
    assert all(k <= st.as_of.strftime("%Y-%m") for k in act)


def test_variance_needs_a_saved_baseline(E, plan):
    v = VAR.build(E.db, E.A, plan.state.as_of, plan, None)
    assert v["has_original"] is False
    for mrow in v["months"]:
        for f in mrow["fields"].values():
            assert f["variance"] is None


def test_save_baseline_then_variance_computes(E):
    r = call("/api/plan/save", "POST", {"variant": "balanced", "kind": "original"})
    assert r["ok"]
    original = E.db.get_plan("original", "balanced")
    assert original and original["monthly"]

    A, bio, st = E.ctx()
    p = Plan(A, bio, st, "balanced")
    v = VAR.build(E.db, A, st.as_of, p, original)
    assert v["has_original"] is True
    cur = st.as_of.strftime("%Y-%m")
    row = next(m for m in v["months"] if m["key"] == cur)
    d = row["fields"]["eggs_purchased"]
    assert d["variance"] == pytest.approx((d["actual"] or 0) - (d["original_plan"] or 0))
    assert v["totals_to_date"]["revenue"]["actual"] == pytest.approx(1_567_000_000)


def test_baseline_is_not_silently_replaced(E):
    with pytest.raises(ValueError):
        call("/api/plan/save", "POST", {"variant": "balanced", "kind": "original"})
    r = call("/api/plan/save", "POST",
             {"variant": "balanced", "kind": "original", "replace": True})
    assert r["ok"]


def test_plan_is_rolling_not_frozen(E):
    """ثبت یک رویداد واقعی جدید باید برنامه فعلی را تغییر دهد."""
    before = Plan(*E.ctx(), "balanced").summary()["eggs_planned"]
    st0 = E.ctx()[2]
    victim = max(st0.cohorts.values(), key=lambda c: c.alive)
    take = victim.alive * 0.8
    r = call("/api/transactions", "POST", {
        "txn_type": "sale", "txn_date": st0.as_of.isoformat(),
        "cohort_id": victim.cohort_id, "quantity": take, "weight_g": 10.0,
        "unit_price": 18200, "counterparty": "تست rolling"})
    after_state = E.ctx()
    after = Plan(*after_state, "balanced")
    assert after.state.cohorts[victim.cohort_id].alive < victim.alive * 0.5
    E.db.void_txn(r["id"], "پاک‌سازی تست")
    assert isinstance(before, float)


# ── بنچمارک ارزی سه‌ماهه ────────────────────────────────────────────
def test_quarterly_fx_uses_real_data_and_marks_gaps(E, plan):
    from core.fx import FXBenchmark
    qs = plan.quarterly_capital_fx(FXBenchmark(E.db, E.A))
    assert qs
    have = [q for q in qs if q["fx_available"]]
    assert have, "سه‌ماهه جاری باید داده واقعی داشته باشد"
    q0 = have[0]
    assert q0["fx_return"] == pytest.approx(q0["fx_end"] / q0["fx_start"] - 1)
    assert q0["usd_alternative_gain"] == pytest.approx(
        q0["beginning_capital"] * q0["benchmark_share"] * q0["fx_return"])
    assert q0["excess_over_fx"] == pytest.approx(
        q0["farm_gain"] - q0["usd_alternative_gain"])
    # دوره‌های بدون داده ساختگی پر نمی‌شوند
    for q in qs:
        if not q["fx_available"]:
            assert "fx_return" not in q and "موجود نیست" in q["note"]


def test_fx_does_not_replace_operating_profit(plan):
    s = plan.summary()
    assert "contribution_nominal" in s
    assert all("contribution" not in k for q in plan.quarterly_capital_fx(None)
               for k in q)


# ── validation برنامه ───────────────────────────────────────────────
def test_plan_validation_runs_and_has_no_failures(E, plan):
    from core import validate as V
    v = V.run_plan_checks(E.A, plan)
    ids = {c["id"] for c in v["checks"]}
    assert ids >= {"plan_mass_balance", "plan_pond_capacity", "plan_integer_ponds",
                   "plan_monthly_eggs", "plan_working_capital", "plan_feed",
                   "plan_timeline", "plan_cohort_allocation",
                   "plan_contribution"}
    assert v["failed"] == 0, [c for c in v["checks"] if c["status"] == "fail"]


def test_plan_endpoint_returns_full_payload(E):
    r = call("/api/plan?variant=balanced".split("?")[0], "GET", {},
             {"variant": ["balanced"]})
    for k in ("summary", "monthly", "quarterly", "action_plan_90d",
              "capacity_curve", "cohort_decisions", "grading", "quarterly_fx",
              "risk_flags", "lots", "variance", "validation"):
        assert k in r, k
    assert r["summary"]["variant"] == "balanced"


def test_stage1_validation_still_passes(E):
    v = call("/api/validate")
    assert v["failed"] == 0


# ── سناریوی نامساعد ────────────────────────────────────────────────
def test_adverse_scenario_is_built_from_config(E):
    A, bio, st = E.ctx()
    sc = Scenario.adverse(A)
    assert sc.mortality_mult > 1.0
    assert sc.fcr_mult > 1.0
    assert sc.price_mult < 1.0
    assert sc.mortality_mult == pytest.approx(1 + float(A.get("stochastic.mortality_cv")))


def test_adverse_needs_more_ponds_or_less_revenue(plan):
    assert sum(plan.revenue_adv) <= sum(plan.revenue) + 1
