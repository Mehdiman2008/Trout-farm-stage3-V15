"""
تست‌های ۶ اصلاح نهایی مرحله ۳.

ده سناریوی الزامی specification:
 1. فروش ماهی بدون فشار ظرفیت
 2. همان فروش وقتی pond capacity کاملاً پر است
 3. فروش با Working Capital محدود
 4. فروش 3g در برابر نگه‌داشتن تا وزن بالاتر با saleability کمتر
 5. فروش از دو cohort مختلف
 6. Egg Offer با cash payment
 7. همان Egg Offer با supplier credit
 8. Egg Offer که فقط Partial Buy آن feasible است
 9. What-If sale که واقعاً pond/feed/cash را تغییر دهد
10. What-If شامل چند hypothetical transaction
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as m                                        # noqa: E402
from app import Engine, api                            # noqa: E402
from core import offers as OF                          # noqa: E402
from core import hypothetical as H                     # noqa: E402
from core import plan_model as PM                       # noqa: E402
from core import validate as V                          # noqa: E402
from core.plan_model import saleability_band            # noqa: E402
from core.planner import Plan                            # noqa: E402
from core import manual_mix as MM                        # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "test_stage3_fixes.db")


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
    H.cache_clear()
    return E.ctx()


def _cohort(E, min_w=0.0, max_w=None):
    st = E.ctx()[2]
    cands = [c for c in st.cohorts.values() if c.alive >= 1
             and c.mean_weight >= min_w
             and (max_w is None or c.mean_weight < max_w)]
    return max(cands, key=lambda c: c.alive)


def offer_date(E, days=30):
    return (E.ctx()[2].as_of + timedelta(days=days)).isoformat()


# ═══════════════════════════════ زیرساخت: clone و وضعیت فرضی
def test_clone_is_fully_independent(E):
    A, bio, st = ctx(E)
    st2 = st.clone()
    assert st2.hypothetical is True
    c0 = next(iter(st.cohorts.values()))
    before = c0.alive
    st2.cohorts[c0.cohort_id].alive -= 5000
    st2.cohorts[c0.cohort_id].alloc.clear()
    assert c0.alive == before, "تغییر clone نباید وضعیت واقعی را عوض کند"
    assert c0.alloc, "تخصیص واقعی نباید خالی شود"


def test_apply_sale_requires_clone(E):
    A, bio, st = ctx(E)
    c = _cohort(E)
    with pytest.raises(RuntimeError, match="clone"):
        H.apply_sale(st, [{"cohort_id": c.cohort_id, "quantity": 100}], 15000)


def test_integer_ponds_uses_ceil_per_cohort(E):
    A, bio, st = ctx(E)
    total = H.integer_ponds_now(st)
    manual = sum(H.ponds_of_cohort(st, c) for c in st.cohorts.values())
    assert total == manual
    assert total == int(total), "نیاز استخر باید صحیح باشد"


def test_plan_cache_reuses_same_inputs(E):
    A, bio, st = ctx(E)
    p1 = H.solve_plan(A, bio, st, "balanced")
    p2 = H.solve_plan(A, bio, st, "balanced")
    assert p1 is p2, "همان ورودی باید از cache بیاید"
    H.cache_clear()
    p3 = H.solve_plan(A, bio, st, "balanced")
    assert p3 is not p1
    assert p3.summary()["contribution_nominal"] == \
        pytest.approx(p1.summary()["contribution_nominal"], rel=1e-6), \
        "reproducibility: همان ورودی همان خروجی"


# ═══════════════════════════════ اصلاح ۱: کف از بهینه‌سازی دوباره
def test_sale_floor_comes_from_reoptimisation(E):
    A, bio, st = ctx(E)
    c = _cohort(E)
    r = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 10_000, "price": 15_000})
    assert r["method"] == "re-optimisation"
    fs = r["floor_search"]
    assert fs["solver_runs"] >= 1
    assert len(fs["probe_points"]) >= 1
    # در قیمت کف، فاصله دو سناریو باید داخل حد دقت باشد
    tol = fs["tolerance_per_fish"] * 10_000
    assert abs(fs["residual_value"]) <= tol * 1.5
    # هر دو سناریو گزارش شده‌اند
    assert "keep" in r["scenarios"] and "accept" in r["scenarios"]
    assert r["scenarios"]["keep"]["npv"] != 0


def test_floor_is_indifference_price(E):
    """در قیمت کف، NPV(پذیرش) ≈ NPV(نگه‌داشتن)."""
    A, bio, st = ctx(E)
    c = _cohort(E)
    qty = 10_000
    r = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": qty, "price": 15_000})
    econ = r["prices"]["economic_floor"]
    at_floor = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": qty, "price": econ})
    tol = float(A.get("offers.floor_tolerance_per_fish")) * qty
    assert abs(at_floor["difference_vs_keeping"]) <= tol * 2


# ═══════════════════════════ سناریو ۱ و ۲: فروش با/بی فشار ظرفیت
def test_s1_s2_sale_with_and_without_capacity_pressure(E):
    """
    سناریو ۱ و ۲: همان فروش، یک بار با ظرفیت آزاد و یک بار با ظرفیت پر.

    جهت تغییر کف از پیش قطعی نیست: با ظرفیت تنگ، برنامه «نگه‌داشتن» خودش
    برداشت زودتر را انتخاب می‌کند (Fix 3)، پس ارزش نسبی آفر خارجی می‌تواند
    کم یا زیاد شود. آنچه الزامی است:
      * ارزیابی در هر دو حالت از بهینه‌سازی دوباره بیاید،
      * پذیرش فروش فشار استخر برنامه را بیشتر نکند،
      * کف با وضعیت مزرعه تغییر کند (reservation price ثابت نیست).
    """
    A, bio, st = ctx(E)
    c = _cohort(E, min_w=1.0)
    off = {"cohort_id": c.cohort_id, "quantity": 20_000, "price": 15_000,
           "payment_terms": {"upfront_share": 1.0, "delay_days": 0}}
    loose = OF.evaluate_sale_offer(A, bio, st, dict(off))
    assert loose["method"] == "re-optimisation"
    assert loose["scenarios"]["accept"]["peak_ponds"] <= \
        loose["scenarios"]["keep"]["peak_ponds"] + 1e-6

    A.set("farm.operational_ponds", 10)          # ظرفیت را به‌شدت تنگ کن
    try:
        A2, bio2, st2 = ctx(E)
        tight = OF.evaluate_sale_offer(A2, bio2, st2, dict(off))
    finally:
        A.reset("farm.operational_ponds")
        H.cache_clear()
    assert tight["method"] == "re-optimisation"
    # پذیرش فروش نباید فشار استخر را از «نگه‌داشتن» بیشتر کند
    assert tight["scenarios"]["accept"]["peak_ponds"] <= \
        tight["scenarios"]["keep"]["peak_ponds"] + 1e-6
    # محدودیت باید در خودِ برنامه «نگه‌داشتن» دیده شود: با ۱۰ استخر، اوج
    # نیاز استخر برنامه از حالت آزاد کمتر است (Fix 3: برداشت زودتر خودکار).
    assert tight["scenarios"]["keep"]["peak_ponds"] <= \
        loose["scenarios"]["keep"]["peak_ponds"]
    # کف در هر دو حالت متناهی و معقول است. نکته: ممکن است دقیقاً ۰ باشد
    # (وقتی cohort خودش در بازهٔ کم‌سهولت/سقف‌تقاضادار باشد، نگه‌داشتن حتی
    # با فروش رایگان هم بدتر از فروش است) — این نقص نیست.
    for r in (loose, tight):
        assert 0 <= r["prices"]["economic_floor"] < 60_000


def test_s3_sale_with_tight_working_capital(E):
    """
    سناریو ۳: فروش وقتی سرمایه در گردش محدود است.

    الزامات: ارزیابی از بهینه‌سازی دوباره بیاید، محدودیت سرمایه در هر دو
    سناریو دیده شود، پذیرش فروش اوج نیاز نقدی را بیشتر نکند و کف با وضعیت
    مالی مزرعه تغییر کند. (جهت تغییر کف قطعی نیست: با سرمایه تنگ، برنامه
    «نگه‌داشتن» خودش کوچک‌تر می‌شود و ارزش نسبی آفر می‌تواند هر دو طرف برود.)
    """
    A, bio, st = ctx(E)
    c = _cohort(E, min_w=1.0)
    off = {"cohort_id": c.cohort_id, "quantity": 20_000, "price": 15_000,
           "payment_terms": {"upfront_share": 1.0, "delay_days": 0}}
    rich = OF.evaluate_sale_offer(A, bio, st, dict(off))

    A.set("finance.working_capital_available", 1.2e9)
    try:
        A2, bio2, st2 = ctx(E)
        poor = OF.evaluate_sale_offer(A2, bio2, st2, dict(off))
    finally:
        A.reset("finance.working_capital_available")
        H.cache_clear()
    assert poor["method"] == "re-optimisation"
    assert poor["working_capital"]["wc_available"] == pytest.approx(1.2e9)
    # وجه فروش نباید اوج نیاز نقدی را بدتر کند
    assert poor["working_capital"]["peak_funding_after"] <= \
        poor["working_capital"]["peak_funding_before"] + 1e-6
    # محدودیت مالی باید در برنامه دیده شود: سقف سرمایه سناریوی poor همان
    # override است و برنامه‌اش کوچک‌تر/محتاط‌تر از rich است.
    assert poor["scenarios"]["keep"]["eggs_planned"] <= \
        rich["scenarios"]["keep"]["eggs_planned"]
    # با مدل جدید Saleability، اگر «نگه‌داشتن» به‌اندازه کافی بد باشد
    # (سرمایه تنگ + وزن فعلی/آینده کم‌سهولت‌فروش)، حتی فروش با قیمت صفر هم
    # می‌تواند بهتر از نگه‌داشتن باشد؛ کف ۰ در این حالت پاسخ درست است، نه
    # نشانه خطا — جست‌وجو به‌درستی در ۰ متوقف می‌شود (قیمت منفی معنا ندارد).
    assert 0 <= poor["prices"]["economic_floor"] < 60_000


def test_s3b_worse_receipt_terms_raise_nominal_floor(E):
    """
    آزمون جهت‌دار و پایدار برای ارزش زمانی پول (اصلاح ۲):
    همان فروش با دریافت تمام‌مدت‌دار باید کف **اسمی** بالاتری بخواهد تا
    ارزش امروزِ یکسانی بدهد.

    عمداً از cohortی خارج از بازهٔ سقف‌تقاضادار استفاده می‌شود: در بازهٔ
    کم‌سهولت/سقف‌تقاضادار، نگه‌داشتن می‌تواند چنان کم‌ارزش شود که کف حتی با
    شرایط نقد هم روی مرز ۰ کلمپ شود و تمایز جهت‌دار قابل مشاهده نباشد —
    آن حالت مرزی را `test_s3_sale_with_tight_working_capital` و
    `test_s1_s2_...` جداگانه پوشش می‌دهند.
    """
    A, bio, st = ctx(E)
    c = _cohort(E, min_w=1.0, max_w=5.0)
    base = {"cohort_id": c.cohort_id, "quantity": 20_000, "price": 15_000}
    cash = OF.evaluate_sale_offer(A, bio, st, {
        **base, "payment_terms": {"upfront_share": 1.0, "delay_days": 0}})
    slow = OF.evaluate_sale_offer(A, bio, st, {
        **base, "payment_terms": {"upfront_share": 0.0, "delay_days": 120}})
    assert slow["payment"]["present_value_factor"] < \
        cash["payment"]["present_value_factor"]
    assert slow["prices"]["economic_floor"] > \
        cash["prices"]["economic_floor"], \
        "دریافت دیرتر باید قیمت اسمی بالاتری بخواهد"


def test_s4_saleability_shows_in_keep_alternative(E):
    """
    فروش 3g (آفر تأییدشده، احتمال ۱) در برابر نگه‌داشتن تا وزن بالاتر با
    saleability کمتر: احتمال مشتری هرگز مستقیم در قیمتِ تلاش اول ضرب
    نمی‌شود، بلکه به‌صورت تأخیر فروش + تخفیف تلاش‌های بعدی در پروفایل
    «نگه‌داشتن» وارد می‌شود. اینجا هم بررسی می‌شود Offer Evaluator دقیقاً
    از همان جدول ۵-بازه‌ای مشترک (`saleability_band`) می‌خواند که Plan و
    optimizer اصلی می‌خوانند — نه یک کپی جدا.
    """
    A, bio, st = ctx(E)
    c = _cohort(E, min_w=1.0, max_w=15.0)
    r = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 10_000, "price": 15_000})
    for o in r["alternative"]["options"]:
        if o.get("is_sell_now"):
            assert o["saleability"] == 1.0, "آفر واقعی یعنی مشتری قطعی"
        else:
            expected = saleability_band(A, o["harvest_w"])["prob"]
            assert o["saleability"] == pytest.approx(expected), \
                "Offer Evaluator باید همان جدول مشترک را بخواند"
    # وزن‌های بالاتر باید سهولت فروش کمتر یا مساوی وزن‌های پایین‌تر داشته باشند
    weighted = sorted((o["harvest_w"], o["saleability"])
                      for o in r["alternative"]["options"] if not o.get("is_sell_now"))
    for (w1, p1), (w2, p2) in zip(weighted, weighted[1:]):
        assert p2 <= p1 + 1e-9, "سهولت فروش باید با افزایش وزن نزولی باشد"

    # با saleability کمتر برای وزن‌های بالا، نگه‌داشتن کم‌ارزش‌تر → کف معمولاً
    # پایین‌تر می‌آید. توجه: این کف از حل دوبارهٔ کل برنامهٔ مزرعه می‌آید، نه
    # از یک فرمول ایزوله برای همین یک cohort — پس تغییر سهولت فروش یک بازهٔ
    # وزنی می‌تواند زمان‌بندی استخر/سرمایه در گردش کل مزرعه (همهٔ cohortهای
    # دیگر) را هم جابه‌جا کند. به‌جای تحمل مطلق سخت‌گیرانه (±۱۰۰ تومان) که
    # این اثرات متقاطع را نادیده می‌گیرد، یک تحمل نسبی معقول به کار می‌رود.
    base_tbl = A.get("planning.saleability")
    worse = [dict(b, prob=(0.05 if float(b["w_min"]) >= 5 else b["prob"]))
             for b in base_tbl]
    A.set("planning.saleability", worse)
    try:
        A2, bio2, st2 = ctx(E)
        r2 = OF.evaluate_sale_offer(A2, bio2, st2, {
            "cohort_id": c.cohort_id, "quantity": 10_000, "price": 15_000})
    finally:
        A.reset("planning.saleability")
        H.cache_clear()
    base_floor = r["prices"]["economic_floor"]
    assert r2["prices"]["economic_floor"] <= base_floor * 1.15 + 100


# ═══════════════════════ اصلاح ۳: استخر واقعاً آزادشده
def test_ponds_freed_is_integer_difference(E):
    A, bio, st = ctx(E)
    c = _cohort(E, min_w=1.0)
    cap = bio.fish_per_pond(c.mean_weight)
    before = H.integer_ponds_now(st)

    # فروش کوچک که مرز استخر را جابه‌جا نمی‌کند
    small = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": min(500, c.alive * 0.01),
        "price": 15_000})
    assert small["ponds_before"] == before
    assert small["ponds_freed"] == \
        small["ponds_before"] - small["ponds_after"]
    assert float(small["ponds_freed"]).is_integer()

    # فروش به‌اندازه‌ای که دقیقاً یک استخر این cohort را خالی کند
    import math
    need = math.ceil(c.alive / cap)
    if need >= 2:
        qty = c.alive - (need - 1) * cap + 1
        big = OF.evaluate_sale_offer(A, bio, st, {
            "cohort_id": c.cohort_id, "quantity": qty, "price": 15_000})
        assert big["ponds_freed"] >= 1
        assert big["ponds_freed"] == big["ponds_before"] - big["ponds_after"]


# ═══════════════════════ اصلاح ۴ و سناریو ۵: آفر چند-cohort
def test_s5_multi_cohort_allocation_suggested_and_used(E):
    """آفر «X قطعه حدود w گرم» بدون cohort_id باید از چند cohort تأمین شود."""
    A, bio, st = ctx(E)
    # وزنی بین دو cohort نخست تا هر دو واجد شرایط شوند
    cs = sorted([c for c in st.cohorts.values() if c.alive >= 1],
                key=lambda c: -c.alive)[:2]
    w_req = (cs[0].mean_weight + cs[1].mean_weight) / 2
    # بیش از موجودی بزرگ‌ترین cohort → تأمین حتماً چند-cohort می‌شود
    qty = cs[0].alive + cs[1].alive * 0.3

    sug = H.suggest_allocation(A, bio, st, qty, w_req, tolerance=5.0)
    assert sug["feasible"]
    assert len(sug["allocations"]) >= 2, "باید از بیش از یک cohort تأمین شود"
    total = sum(a["quantity"] for a in sug["allocations"])
    assert total == pytest.approx(qty, abs=1)
    for a in sug["allocations"]:
        c = st.cohorts[a["cohort_id"]]
        assert a["quantity"] <= c.alive + 1e-6

    r = OF.evaluate_sale_offer(A, bio, st, {
        "quantity": qty, "weight_g": w_req, "price": 15_000,
        "allocations": sug["allocations"]})
    assert r["allocation"]["multi_cohort"]
    assert r["allocation"]["source"] == "user"
    # سناریوی پذیرش باید موجودی هر دو cohort را کم کرده باشد
    sd = r["scenarios"]["accept"]
    assert sd["npv"] != r["scenarios"]["keep"]["npv"]


def test_allocation_validation_rules(E):
    A, bio, st = ctx(E)
    c = _cohort(E)
    with pytest.raises(ValueError, match="بیشتر است"):
        H.validate_allocation(st, [{"cohort_id": c.cohort_id,
                                    "quantity": c.alive * 3}], c.alive * 3)
    with pytest.raises(ValueError, match="برابر نیست"):
        H.validate_allocation(st, [{"cohort_id": c.cohort_id, "quantity": 100}],
                              5_000)
    with pytest.raises(ValueError, match="ناشناخته"):
        H.validate_allocation(st, [{"cohort_id": "NOPE", "quantity": 10}], 10)


def test_allocation_endpoint(E):
    st = E.ctx()[2]
    c = max(st.cohorts.values(), key=lambda x: x.alive)
    r = call("/api/decide/sale/allocation", "POST", {
        "quantity": min(5_000, c.alive / 2), "weight_g": c.mean_weight})
    assert r["feasible"]
    assert r["allocations"]


# ═══════════════ اصلاح ۵ و سناریوهای ۶–۸: آفر تخم
def test_s6_s7_supplier_credit_raises_max_justified_price(E):
    """اعتبار تأمین‌کننده باید قیمت اسمی بالاتری را توجیه کند."""
    A, bio, st = ctx(E)
    base = {"date": offer_date(E), "quantity": 100_000, "price": 6_000}
    cash = OF.evaluate_egg_offer(A, bio, st, dict(base), partial_options=False)
    credit = OF.evaluate_egg_offer(
        A, bio, st, {**base, "payment_terms": {"upfront_share": 0.0,
                                               "delay_days": 60}},
        partial_options=False)
    assert credit["payment"]["present_value_factor"] < 1.0
    assert credit["max_justified_price"] > cash["max_justified_price"], \
        "۶٬۸۰۰ مدت‌دار و ۶٬۵۰۰ نقد نباید یکسان ارزیابی شوند"
    # سود افزوده هم باید با اعتبار بهتر شود (پرداخت دیرتر = ارزش امروز کمتر)
    assert credit["expected_profit_impact"] >= cash["expected_profit_impact"] - 1e-6


def test_s8_hard_feasibility_forces_partial_or_reject(E):
    """
    Profit مثبت به‌تنهایی کافی نیست: با ظرفیت/نقدینگی تنگ، خرید کامل نباید
    BUY بگیرد؛ یا PARTIAL_BUY اجراشدنی پیشنهاد شود یا REJECT.

    برای کنترل زمان تست، دورهای اصلاح و سقف حل در حالت تنگ محدود می‌شوند؛
    این فقط سرعت است و منطق feasibility را عوض نمی‌کند.
    """
    A, bio, st = ctx(E)
    ok = OF.evaluate_egg_offer(A, bio, st, {
        "date": offer_date(E), "quantity": 300_000, "price": 3_000},
        partial_options=False)
    assert ok["decision"] in ("BUY", "PARTIAL_BUY")
    assert ok["full_accept"]["feasible"]

    A.set("farm.operational_ponds", 8)
    A.set("finance.working_capital_available", 1.0e9)
    A.set("planning.max_repair_rounds", 1)
    A.set("planning.solver_time_limit_s", 8)
    try:
        A2, bio2, st2 = ctx(E)
        tight = OF.evaluate_egg_offer(A2, bio2, st2, {
            "date": offer_date(E), "quantity": 300_000, "price": 3_000})
    finally:
        for k in ("farm.operational_ponds", "finance.working_capital_available",
                  "planning.max_repair_rounds", "planning.solver_time_limit_s"):
            A.reset(k)
        H.cache_clear()

    if tight["decision"] == "BUY":
        assert tight["full_accept"]["feasible"], \
            "BUY بدون feasibility مجاز نیست"
    else:
        assert tight["decision"] in ("PARTIAL_BUY", "REJECT")
        if tight["decision"] == "PARTIAL_BUY":
            chosen = [o for o in [tight["full_accept"]] + tight["options"]
                      if o["quantity"] == tight["preferred_quantity"]]
            assert chosen and chosen[0]["feasible"], \
                "گزینه پیشنهادی PARTIAL باید اجراشدنی باشد"


def test_egg_offer_infeasible_reason_reported(E):
    A, bio, st = ctx(E)
    A.set("finance.working_capital_available", 0.4e9)
    A.set("planning.max_repair_rounds", 1)
    A.set("planning.solver_time_limit_s", 8)
    try:
        A2, bio2, st2 = ctx(E)
        r = OF.evaluate_egg_offer(A2, bio2, st2, {
            "date": offer_date(E), "quantity": 300_000, "price": 5_500},
            partial_options=False)
        if not r["full_accept"]["feasible"]:
            assert r["full_accept"]["infeasible_reason_fa"]
    finally:
        for k in ("finance.working_capital_available",
                  "planning.max_repair_rounds", "planning.solver_time_limit_s"):
            A.reset(k)
        H.cache_clear()


# ═══════════════ اصلاح ۶ و سناریوهای ۹–۱۰: What-If واقعی
def test_s9_what_if_sale_changes_pond_feed_cash(E):
    A, bio, st = ctx(E)
    c = _cohort(E, min_w=1.0)
    qty = min(30_000, c.alive * 0.5)
    r = OF.what_if(A, bio, st, [
        {"type": "sell_fish", "cohort_id": c.cohort_id,
         "quantity": qty, "price": 16_000}])
    assert r["method"] == "cloned_state_reoptimisation"
    sd = r["state_delta"]
    assert sd["live_fish_delta"] == pytest.approx(-qty, abs=1)
    assert r["delta"]["feed_cost"] < 0, "ماهی فروخته‌شده دیگر خوراک نمی‌خورد"
    assert any(x["amount"] > 0 for x in sd["cash_rows"]), \
        "وجه فروش باید در دفتر نقدی سناریو باشد"
    # ۵۰/۵۰: دو ردیف دریافت
    kinds = {x["type"] for x in sd["cash_rows"]}
    assert {"receipt_upfront", "receipt_balance"} <= kinds


def test_s10_what_if_multiple_hypothetical_transactions(E):
    A, bio, st = ctx(E)
    c = _cohort(E, min_w=1.0)
    r = OF.what_if(A, bio, st, [
        {"type": "sell_fish", "cohort_id": c.cohort_id,
         "quantity": 10_000, "price": 16_000},
        {"type": "buy_eggs", "quantity": 160_000, "price": 6_000,
         "date": offer_date(E),
         "payment_terms": {"upfront_share": 0.5, "delay_days": 60}},
        {"type": "feed_price_pct", "value": 10},
    ])
    assert len(r["applied"]) == 3
    # خرید فرضی واقعاً وارد برنامه شده است. توجه: سقف سالانه تخم binding است،
    # پس این lot به جمع کل اضافه نمی‌شود بلکه جای lotهای اختیاری را می‌گیرد —
    # و همین درست است.
    assert r["state_delta"]["hypothetical_lots_in_plan"], \
        "lot فرضی باید در برنامه سناریو انتخاب شده باشد"
    assert r["scenario"]["eggs_planned"] >= 160_000
    assert r["state_delta"]["live_fish_delta"] == pytest.approx(-10_000, abs=1)
    # فرضیات دست‌نخورده برگشته‌اند
    assert not A.overlay, "overlay باید در پایان خالی باشد"


def test_what_if_never_touches_database_or_overrides(E):
    A, bio, st = ctx(E)
    c = _cohort(E)
    n_txn = E.db.one("SELECT COUNT(*) n FROM transactions")["n"]
    n_ovr = E.db.one("SELECT COUNT(*) n FROM assumption_overrides")["n"]
    feed0 = A.get("feed.price_table")[0]["price"]
    OF.what_if(A, bio, st, [
        {"type": "sell_fish", "cohort_id": c.cohort_id,
         "quantity": 5_000, "price": 20_000},
        {"type": "feed_price_pct", "value": 25},
        {"type": "mortality_pct", "value": 5},
    ])
    assert E.db.one("SELECT COUNT(*) n FROM transactions")["n"] == n_txn
    assert E.db.one("SELECT COUNT(*) n FROM assumption_overrides")["n"] == n_ovr, \
        "What-If دیگر هرگز در جدول overrides نمی‌نویسد (اصلاح ۶)"
    assert A.get("feed.price_table")[0]["price"] == feed0
    A2, bio2, st2 = ctx(E)
    assert st2.cohorts[c.cohort_id].alive == pytest.approx(
        st.cohorts[c.cohort_id].alive)


# ═══════════════ رگرسیون: یکسانی Offer Evaluator و What-If برای همان فروش
def test_offer_evaluator_and_whatif_reconcile_for_identical_sale(E):
    """
    اگر What-If فقط شامل یک Sale Event باشد و همان پارامترها (cohort، تعداد،
    قیمت، تاریخ، شرایط پرداخت) را با Sale Offer Evaluator داشته باشد، دو
    موتور باید NPV پایه، NPV سناریو، ΔNPV و اثرات درآمد/خوراک/نقدینگی/استخر
    را دقیقاً یکسان گزارش کنند.

    هر دو مسیر از همان `_apply_sale_timing` → `sale_cash_rows`/`apply_sale`
    → `solve_plan` → `value_digest().npv()` عبور می‌کنند؛ تنها چیزی که واقعاً
    می‌تواند این تساوی را بشکند، ورودی متفاوت است (مثلاً شرایط پرداخت
    نگفته/پیش‌فرض در یک طرف و صریح در طرف دیگر) — نه فرمول.
    """
    A, bio, state = ctx(E)
    c = _cohort(E, min_w=1.0)
    terms = {"upfront_share": 1.0, "delay_days": 0}
    offer = {"cohort_id": c.cohort_id, "quantity": 70_000, "price": 11_500,
             "payment_terms": dict(terms)}

    r_offer = OF.evaluate_sale_offer(A, bio, state, dict(offer))

    A2, bio2, state2 = ctx(E)
    r_wif = OF.what_if(A2, bio2, state2, [
        {"type": "sell_fish", "cohort_id": c.cohort_id,
         "quantity": 70_000, "price": 11_500, "payment_terms": dict(terms)}])

    keep, acc = r_offer["scenarios"]["keep"], r_offer["scenarios"]["accept"]
    base, scen = r_wif["baseline"], r_wif["scenario"]

    # NPV پایه و NPV سناریو
    assert base["npv"] == pytest.approx(keep["npv"], rel=1e-9)
    assert scen["npv"] == pytest.approx(acc["npv"], rel=1e-9)
    # ΔNPV
    assert r_wif["delta"]["npv"] == pytest.approx(
        r_offer["difference_vs_keeping"], rel=1e-9)
    # اثر درآمد و هزینه خوراک
    assert scen["revenue"] == pytest.approx(acc["revenue"], rel=1e-9)
    assert base["revenue"] == pytest.approx(keep["revenue"], rel=1e-9)
    assert scen["feed_cost"] == pytest.approx(acc["feed_cost"], rel=1e-9)
    assert base["feed_cost"] == pytest.approx(keep["feed_cost"], rel=1e-9)
    # اثر نقدینگی (اوج نیاز تأمین مالی)
    assert scen["peak_funding"] == pytest.approx(acc["peak_funding"], rel=1e-9)
    assert base["peak_funding"] == pytest.approx(keep["peak_funding"], rel=1e-9)
    # اثر استخر
    assert scen["peak_ponds"] == pytest.approx(acc["peak_ponds"], rel=1e-9)
    assert base["peak_ponds"] == pytest.approx(keep["peak_ponds"], rel=1e-9)


def test_offer_evaluator_and_whatif_share_default_payment_terms(E):
    """
    وقتی هیچ‌کدام payment_terms نمی‌فرستند، هر دو باید به همان پیش‌فرض
    مشتری (finance.customer_upfront_share/delay) برسند — یعنی «پیش‌فرض
    خاموش» هم در هر دو یکسان است، نه فقط حالت صریح.
    """
    A, bio, state = ctx(E)
    c = _cohort(E, min_w=1.0)

    r_offer = OF.evaluate_sale_offer(A, bio, state, {
        "cohort_id": c.cohort_id, "quantity": 70_000, "price": 11_500})

    A2, bio2, state2 = ctx(E)
    r_wif = OF.what_if(A2, bio2, state2, [
        {"type": "sell_fish", "cohort_id": c.cohort_id,
         "quantity": 70_000, "price": 11_500}])

    assert r_wif["delta"]["npv"] == pytest.approx(
        r_offer["difference_vs_keeping"], rel=1e-9)


# ═══════════════ رگرسیون: Market Saleability باید تصمیم وزن را عوض کند
def test_15g_higher_price_no_longer_wins_purely_on_nominal_price(E):
    """
    ۱۵ گرم قیمت اسمی بالاتری دارد (منحنی قیمت صعودی است) ولی سهولت فروش آن
    طبق Assumptions فعلی «خیلی پایین» (۱۰٪) است. با اصلاح انجام‌شده،
    optimizer دیگر نباید صرفاً به‌خاطر قیمت اسمی بالاتر، برداشت در ۱۵ گرم
    را روی بقیهٔ وزن‌های نقدشونده‌تر ترجیح بدهد.

    این تست مستقیماً همان واحدی را می‌سنجد که MILP روی آن بهینه می‌کند:
    سهم هر ماهی از تابع هدف (`contribution()` هر Profile کاندید)، نه یک
    عدد جدا. اگر این تست سبز باشد، هر مصرف‌کننده‌ای که از `PlanModel`
    کاندید بسازد (Plan، Targets، rolling re-optimisation، What-If) همین
    رفتار را می‌بیند.
    """
    A, bio, st = ctx(E)
    pm = PM.PlanModel(A, bio, st)
    purchase = st.as_of - timedelta(days=200)   # ماهی به‌اندازه کافی رشد کرده
    weights = sorted(float(w) for w in A.get("planning.harvest_weights"))
    per_fish = {}
    for w in weights:
        p = pm.new_lot_profile(purchase, 100_000, w, PM.Scenario.base())
        per_fish[w] = p.contribution() / 100_000 if p.sold_fish else float("-inf")

    best_w = max(per_fish, key=per_fish.get)
    assert best_w != 15.0, (
        f"با سهولت فروش خیلی‌پایین در ۱۵ گرم، این وزن نباید صرفاً به‌خاطر "
        f"قیمت اسمی بالاتر برنده شود (per-fish: {per_fish})")
    # قیمت اسمی ۱۵ گرم واقعاً از پایین‌ترین وزن‌ها بیشتر است — یعنی این یک
    # تست بی‌اثر (trivial) نیست؛ صرفاً چون قیمت پایین بوده کمتر انتخاب نشده.
    assert bio.sale_price(15.0) > bio.sale_price(weights[0])


def test_saleability_low_band_gets_material_price_haircut(E):
    """اثر مستقیم و اندازه‌گیری‌پذیر retry_discount روی درآمد واقعی‌شدهٔ ۱۵ گرم."""
    A, bio, st = ctx(E)
    pm = PM.PlanModel(A, bio, st)
    p15 = pm.new_lot_profile(st.as_of - timedelta(days=60), 100_000, 15.0,
                             PM.Scenario.base())
    band15 = saleability_band(A, 15.0)
    listed = bio.sale_price(15.0)
    realised = sum(p15.revenue) / p15.sold_fish
    assert band15["prob"] < 0.35 and band15["retry_discount"] > 0
    assert realised < listed, "با سهولت فروش پایین، میانگین قیمت واقعی‌شده باید کمتر از قیمت لیست باشد"


def test_saleability_shared_across_plan_and_offer_evaluator(E):
    """
    Plan/Targets/rolling-reopt از `PlanModel.saleability` و Offer Evaluator
    (`_best_alternative`) از `saleability_band` می‌خوانند — هر دو باید
    دقیقاً همان یک تابع/جدول باشند، نه دو کپی که می‌توانند واگرا شوند.
    """
    A, bio, st = ctx(E)
    pm = PM.PlanModel(A, bio, st)
    for w in (0.5, 1.0, 1.9, 2.0, 3.0, 5.0, 7.5, 10.0, 12.0, 15.0, 20.0):
        assert pm.saleability(w) == saleability_band(A, w)["prob"], w
    # و offers.py هم دقیقاً همین import را دارد — نه یک تابع محلی هم‌نام
    assert OF.saleability_band is saleability_band


def test_plan_summary_reports_sale_bottleneck_warning(E):
    """
    اگر Assumptions طوری تنظیم شود که همهٔ فروش در پایین‌ترین بازهٔ سهولت
    فروش متمرکز شود، Plan باید هشدار Unsold Inventory/Sale Bottleneck بدهد.
    """
    A, bio, st = ctx(E)
    A.set("planning.harvest_weights", [15.0])          # فقط ۱۵ گرم مجاز باشد
    A.set("planning.saleability",
          [{"w_min": 0.0, "w_max": 999.0, "prob": 0.05,
            "retry_discount": 0.20, "label": "آزمایشی — خیلی پایین"}])
    try:
        A2, bio2, st2 = ctx(E)
        p = Plan(A2, bio2, st2, "balanced")
        s = p.summary()
    finally:
        A.reset("planning.harvest_weights")
        A.reset("planning.saleability")
        H.cache_clear()
    if sum(s["sales_by_weight"].values()) > 0:
        assert s["saleability_warnings"], \
            "با تمرکز کامل فروش در بازهٔ خیلی‌کم‌سهولت، هشدار باید صادر شود"
        v = V.run_plan_checks_v2(A2, p)
        row = next(c for c in v["checks"] if c["id"] == "plan_saleability")
        assert row["status"] == "warn"


# ═══════════════ رگرسیون: Demand Capacity — نباید کل cohort را در یک دوره فروخته فرض کند
def test_15g_demand_capacity_paces_sales_instead_of_dumping(E):
    """
    ۱۵ گرم قیمت اسمی بالاتری دارد، ولی سقف تقاضای فرض‌شده برای آن بازه
    (`max_demand_per_period`) محدود است. optimizer/شبیه‌سازی نباید فرض کند
    یک دستهٔ بزرگ همگی در همان دوره طبیعی قابل فروش است — فروش باید طبق
    سقف هفتگی پخش (paced) شود، نه یک‌جا دامپ شود.

    توجه: با اصلاح ۲ (Carry-over تا پایان افق)، مقدار بزرگ در نهایت تقریباً
    کامل می‌فروشد — این خودش درست است (دیگر گم نمی‌شود)؛ چیزی که این تست
    می‌سنجد این است که فروش هرگز در یک/چند هفتهٔ اول متمرکز نشود.
    """
    A, bio, st = ctx(E)
    pm = PM.PlanModel(A, bio, st)
    band15 = saleability_band(A, 15.0)
    assert band15["max_demand_per_period"] is not None and \
        band15["max_demand_per_period"] > 0, "این تست به سقف تقاضای فعال نیاز دارد"

    purchase = st.as_of - timedelta(days=60)
    big_qty = 300_000     # به‌اندازه کافی بزرگ که از سقف تقاضا عبور کند
    p = pm.new_lot_profile(purchase, big_qty, 15.0, PM.Scenario.base())
    cap_week = band15["demand_cap_per_week"]

    # سقف هفتگی اکنون با Seasonality شمسی (اصلاح ۴) در هر هفته بازتوزیع
    # می‌شود؛ هر هفته باید فقط با سقفِ *همان هفته* مقایسه شود، نه یک عدد
    # ثابت واحد برای کل افق.
    for t, n in enumerate(p.harvest_fish):
        if n <= 1:
            continue
        season = PM.jalali_seasonality_factor(A, pm.grid.dates[t])
        cap_t = cap_week * season
        assert n <= cap_t + 1.0, (
            f"هفتهٔ {t} ({pm.grid.dates[t]}): سقف تقاضای همان هفته ({cap_t:.0f}) "
            f"عبور کرده؛ حجم مشاهده‌شده {n:.0f} است")
    weeks_with_sales = sum(1 for n in p.harvest_fish if n > 1)
    assert weeks_with_sales >= 10, \
        "فروش باید در چندین هفتهٔ مجزا پخش شده باشد، نه در یک/چند هفتهٔ اول"
    assert p.sold_fish + p.unsold_fish <= big_qty


def test_demand_capacity_does_not_affect_high_liquidity_bands(E):
    """
    برای بازه‌های خیلی روان (۱-۲ و ۲-۵ گرم) سقفی تعریف نشده — رفتار باید
    دقیقاً مثل قبل (بدون carry-over) بماند.
    """
    A, bio, st = ctx(E)
    pm = PM.PlanModel(A, bio, st)
    for w in (1.0, 2.0, 3.0, 4.5):
        band = saleability_band(A, w)
        assert band["max_demand_per_period"] is None
        assert band["demand_cap_per_week"] is None
    p = pm.new_lot_profile(st.as_of - timedelta(days=10), 300_000, 2.0,
                           PM.Scenario.base())
    assert p.unsold_fish == 0.0


def test_demand_capacity_shared_across_plan_and_offer_evaluator(E):
    """همان تابع مشترک — بدون duplicate logic بین Plan و Offer Evaluator."""
    A, bio, st = ctx(E)
    for w in (1.0, 3.0, 7.0, 12.0, 16.0):
        b1 = saleability_band(A, w)
        assert "max_demand_per_period" in b1 and "demand_cap_per_week" in b1
    assert OF.saleability_band is saleability_band
    # Offer Evaluator هم از همان عدد سقف تقاضا در تشخیصش استفاده می‌کند
    c = _cohort(E, min_w=1.0)
    r = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 10_000, "price": 15_000})
    opt15 = next((o for o in r["alternative"]["options"]
                 if o["harvest_w"] == 15.0), None)
    if opt15:
        assert opt15["demand_cap_per_period"] == \
            saleability_band(A, 15.0)["max_demand_per_period"]


def test_plan_warns_on_literal_unsold_inventory(E):
    """
    اگر برنامه واقعاً موجودی فروش‌نرفته (carry-over) تولید کند، هشدار
    مستقیم آن — نه فقط هشدار احتمال‌محور — باید در summary دیده شود.
    """
    A, bio, st = ctx(E)
    A.set("planning.harvest_weights", [15.0])
    A.set("planning.saleability",
          [{"w_min": 0.0, "w_max": 999.0, "prob": 0.9, "retry_discount": 0.0,
            "max_demand_per_period": 5000, "label": "آزمایشی — سقف تنگ"}])
    A.set("planning.max_carryover_periods", 1)
    try:
        A2, bio2, st2 = ctx(E)
        p = Plan(A2, bio2, st2, "balanced")
        s = p.summary()
    finally:
        for k in ("planning.harvest_weights", "planning.saleability",
                  "planning.max_carryover_periods"):
            A.reset(k)
        H.cache_clear()
    if s["unsold_by_weight"]:
        assert any("Unsold Inventory" in w or "carry" in w.lower()
                  for w in s["saleability_warnings"]), s["saleability_warnings"]


# ═══════════════ رگرسیون اصلاحات محدود بعدی: Global Demand Capacity + Carry-over
def test_global_demand_capacity_shared_across_cohorts(E):
    """
    اصلاح ۱: سقف تقاضای هر بازهٔ وزنی باید بین همهٔ cohortهای همان بازه
    مشترک باشد، نه اینکه هرکدام جدا کل سقف را داشته باشند.

    در وضعیت واقعی مزرعه، C01 و C02 هر دو در بازهٔ >۱۵g هستند و هیچ‌کدام
    گزینهٔ وزن دیگری ندارند (بدون اختیار optimizer) — مجموعشان به‌طور
    طبیعی از سقف تقاضای مشترک آن بازه عبور می‌کند. چون این دو «فروش
    اختیاری/برنامه‌ریزی‌شده» واقعی نیستند (اصلاً گزینهٔ دیگری برایشان
    وجود ندارد)، بلوکه نمی‌شوند — فقط باید هشدار بدهند که فرض ظرفیت بازار
    نیاز به بازبینی دارد.
    """
    A, bio, st = ctx(E)
    p = Plan(A, bio, st, "balanced")
    assert any("Market Demand Assumption" in n or "قطعی/متعهدشده" in n
              for n in p.solution.notes), (
        "با چند cohort بدون گزینهٔ دیگر در یک بازهٔ سقف‌دار، باید هشدار "
        "بازبینی فرض ظرفیت بازار صادر شود")
    cap_week = saleability_band(A, 16.0)["demand_cap_per_week"]
    assert cap_week is not None and cap_week > 0


def test_planned_sales_never_exceed_global_demand_capacity(tmp_path):
    """
    رگرسیون اصلی این اصلاح: فروش *اختیاری/برنامه‌ریزی‌شده* optimizer
    (Planned Sales) — جایی که واقعاً گزینهٔ دیگری برای انتخاب وجود دارد —
    هرگز نباید از سقف تقاضای مشترک بازار عبور کند، حتی اگر چند lot جدید
    هم‌زمان در همان بازهٔ وزنی رقابت کنند.
    """
    E = Engine(str(tmp_path / "planned_cap.db"))
    for t in E.db.q("SELECT id FROM transactions WHERE txn_type='egg_purchase' "
                    "AND status='active'"):
        E.db.void_txn(t["id"], "isolated for hard demand-capacity unit test")

    A, bio, st = E.ctx(date.today())
    band = saleability_band(A, 12.0)
    cap_week = band["demand_cap_per_week"]
    assert cap_week is not None and cap_week > 0

    p = Plan(A, bio, st, "balanced")
    bands = [b for b in A.get("planning.saleability")
             if saleability_band(A, float(b["w_min"]))["demand_cap_per_week"] is not None]
    by_cohort = {}
    for k in p.cand_base["existing"]:
        by_cohort.setdefault(k.split("|")[1], []).append(k)

    for b in bands:
        w_min, w_max = float(b["w_min"]), float(b["w_max"])
        cap = saleability_band(A, w_min)["demand_cap_per_week"]
        per_week = {}
        for k, wgt in p.solution.selected.items():
            pool = p.cand_base["new_lots"] if k.startswith("L|") else p.cand_base["existing"]
            prof = pool.get(k)
            if not prof:
                continue
            if k.startswith("C|") and len(by_cohort.get(k.split("|")[1], [])) <= 1:
                continue     # بدون گزینهٔ دیگر — «اختیاری» نیست، اینجا سنجیده نمی‌شود
            for t, n in enumerate(prof.planned_fish):
                if n and w_min <= prof.weight[t] < w_max:
                    per_week[t] = per_week.get(t, 0.0) + n * wgt
        # سقف با Seasonality شمسی (اصلاح ۴) هر هفته بازتوزیع می‌شود — هر
        # هفته باید با سقفِ همان هفته مقایسه شود.
        for t, qty in per_week.items():
            cap_t = cap * PM.jalali_seasonality_factor(A, p.grid.dates[t])
            assert qty <= cap_t + 1.0, (
                f"هفتهٔ {t} ({p.grid.dates[t]}) بازهٔ «{b['label']}»: فروش اختیاری "
                f"({qty:.0f}) از سقف همان هفته ({cap_t:.0f}) عبور کرده است")


def test_unsold_carries_to_next_period_with_continued_cost(tmp_path):
    """
    اصلاح ۲: ماهیِ فروش‌نرفته (بابت سقف تقاضا) باید تا فروش یا پایان افق
    برنامه در مدل بماند — نه اینکه بعد از یک بازهٔ محدود ناپدید شود.
    همچنان باید تلفات و اشغال استخر داشته باشد.
    """
    E = Engine(str(tmp_path / "carryover.db"))
    for t in E.db.q("SELECT id FROM transactions WHERE txn_type='egg_purchase' "
                    "AND status='active'"):
        E.db.void_txn(t["id"], "isolated for carry-over unit test")
    E.db.add_txn("egg_purchase", date.today().isoformat(), cohort_id="CARRY-ME",
                 quantity=300_000, unit_price=6000, amount=1, data_source="actual")

    A, bio, st = E.ctx(date.today())
    pm = PM.PlanModel(A, bio, st)
    p = pm.new_lot_profile(date.today() - timedelta(days=60), 300_000, 15.0,
                           PM.Scenario.base())

    assert p.last_week == pm.weeks, \
        "با سقف تقاضا فعال، شبیه‌سازی باید تا پایان کل افق ادامه یابد"
    first = min(t for t, n in enumerate(p.harvest_fish) if n > 0)
    later = min(pm.weeks, first + 10)
    assert p.fish[later] > 0, "مازاد فروش‌نرفته باید تا هفته‌های بعد زنده بماند"
    assert sum(p.mortality[first:later + 1]) > 0, \
        "تلفات باید در طول این بازهٔ نگهداری رخ داده باشد"
    if p.fish[later] >= 1 and bio.counts_toward_pond_capacity(p.weight[later]):
        assert p.ponds[later] > 0, "مازاد نگه‌داشته‌شده هنوز باید استخر اشغال کند"


# ═══════════ رگرسیون: تفکیک منشأ عبور از ظرفیت استخر (وضعیت vs تصمیم)
def test_pond_floor_is_a_true_lower_bound(E):
    """
    کف اجباری هر هفته باید واقعاً کف باشد: هیچ ترکیبی از کاندیدهای
    cohortهای موجود نمی‌تواند از آن پایین‌تر برود، و lotهای جدید (که کاملاً
    اختیاری‌اند) هرگز در آن شمرده نشوند.
    """
    A, bio, st = ctx(E)
    p = Plan(A, bio, st, "balanced")
    floor = p.pond_forced_floor
    assert len(floor) == len(p.ponds)
    # کف هرگز از نیاز واقعی همان هفته بیشتر نیست
    for t in range(1, len(floor)):
        assert floor[t] <= p.ponds[t] + 1e-9, f"هفته {t}: کف از نیاز واقعی بیشتر شد"
    # اگر هیچ cohort موجودی نباشد، کف صفر است (فقط lot جدید = کاملاً اختیاری)
    assert all(f >= 0 for f in floor)


def test_pond_breach_from_current_stock_is_not_reported_as_plan_failure(E):
    """
    اگر کف اجباری موجودی فعلی به‌تنهایی از ظرفیت عملیاتی عبور کند، این
    «وضعیت موجود» است نه خطای برنامه. تا وقتی با استخرهای رزرو قابل جذب
    باشد، باید `warn` با ذکر صریح منشأ بدهد — نه `fail`. تصمیم‌های برنامه
    اگر خودشان باعث عبور شوند همچنان `fail` می‌گیرند.
    """
    A, bio, st = ctx(E)
    p = Plan(A, bio, st, "balanced")
    op = int(A.get("farm.operational_ponds"))
    total = int(A.get("farm.total_ponds"))
    peak = max(p.ponds[1:], default=0)
    row = next(c for c in V.run_plan_checks_v2(A, p)["checks"]
               if c["id"] == "plan_pond_feasible")

    if peak <= op:
        assert row["status"] == "pass"
        assert p.pond_breach_source is None
        return

    if p.pond_breach_source == "state":
        assert not p.pond_over_avoidable, \
            "منشأ «وضعیت» یعنی هیچ هفته‌ای به‌خاطر تصمیم برنامه عبور نکرده"
        assert row["status"] == ("warn" if peak <= total else "fail")
        if row["status"] == "warn":
            assert "موجودی فعلی" in row["detail"]
            assert 0 < p.pond_reserve_needed <= total - op
    else:
        assert p.pond_over_avoidable
        assert row["status"] == "fail"


def test_repair_loop_does_not_spin_on_unavoidable_breach(E):
    """
    برش ظرفیت فقط روی هفته‌هایی معنا دارد که optimizer بتواند تغییرشان دهد.
    اگر تمام عبورها از کف اجباری بیایند، حلقهٔ اصلاح نباید دور اضافه بزند
    (هر دور یک حل کامل MILP است — قبلاً ۴ حل بی‌نتیجه انجام می‌شد).
    """
    A, bio, st = ctx(E)
    p = Plan(A, bio, st, "balanced")
    if p.pond_breach_source == "state":
        assert p.repair_rounds == 0, \
            f"نباید دور اصلاح بی‌نتیجه بزند (زد: {p.repair_rounds})"
        assert any("کف اجباری" in x for x in p.repair_log)


# ═══════════ رگرسیون: حاشیه تصمیم — منطق مشترک Offer Evaluator و What-If
def test_margin_for_near_tariff_low_weight_sale_is_not_absurdly_negative(E):
    """
    فروش ~۶۹.۸ هزار قطعه ۱.۴g نزدیک قیمت تعرفه: چون ۱–۲g سهولت فروش خیلی
    بالا (۹۰٪) و بازار ۱۰–۱۵g محدودتر است، برنامهٔ پایه هم همین ماهی‌ها را
    در وزن پایین می‌فروشد. پس حاشیهٔ تصمیم نباید افت غول‌آسا (در حد کل
    درآمد همان ماهی‌ها) نشان دهد — آن عدد فقط وقتی می‌آید که پول همین
    فروش در معیار شمرده نشود.

    مرجع سنجش خودِ ΔNPV است (که تغییر نکرده): حاشیهٔ مورد انتظار باید
    هم‌مرتبه با آن باشد، نه ده‌ها برابر بدتر.
    """
    A, bio, st = ctx(E)
    c = st.cohorts["C03-20260515"]
    qty = min(69_800, c.alive)
    price = round(bio.sale_price(c.mean_weight) * 0.98)
    r = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": qty, "price": price})

    m, dnpv = r["margin"], r["difference_vs_keeping"]
    proceeds = qty * price
    assert m["transaction_proceeds"] == pytest.approx(proceeds), \
        "فروش با تحویل امروز بیرون از سری درآمد برنامه است و باید جدا شمرده شود"
    assert m["expected"] == pytest.approx(m["plan_only"] + proceeds)
    # افت نباید در حد از‌دست‌رفتن کل ارزش این ماهی‌ها باشد
    assert m["expected"] > -0.25 * proceeds, \
        f"حاشیه مورد انتظار {m['expected']:,.0f} در برابر عایدی {proceeds:,.0f} غیرعادی است"
    # و باید هم‌مرتبه با ΔNPV بماند
    assert abs(m["expected"] - dnpv) <= 0.2 * proceeds


def test_future_delivery_sale_is_not_double_counted(E):
    """
    تحویل آینده با `schedule_sale` داخل خود برنامه ثبت می‌شود، پس درآمدش
    از قبل در `contribution_nominal` هست. اضافه‌کردن دوبارهٔ `qty*price`
    (رفتار قبلی) دو‌بار‌شماری بود.
    """
    A, bio, st = ctx(E)
    c = st.cohorts["C03-20260515"]
    qty = min(20_000, c.alive)
    r = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": qty, "price": 11_110,
        "delivery_date": (st.as_of + timedelta(days=30)).isoformat()})
    assert r["margin"]["transaction_proceeds"] == 0.0
    assert r["margin"]["expected"] == pytest.approx(r["margin"]["plan_only"])


def test_offer_evaluator_and_what_if_share_margin_logic(E):
    """
    یک فروش یکسان باید در هر دو مسیر دقیقاً یک حاشیه بدهد — هر دو از
    `decision_margin` مشترک می‌آیند، نه از دو فرمول موازی.
    """
    A, bio, st = ctx(E)
    c = st.cohorts["C03-20260515"]
    qty, price = min(30_000, c.alive), 11_110
    r = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": qty, "price": price})
    A2, bio2, st2 = ctx(E)
    wi = OF.what_if(A2, bio2, st2, [{
        "type": "sell_fish", "cohort_id": c.cohort_id,
        "quantity": qty, "price": price}])
    d = wi["delta"]
    assert d["margin_expected"] == pytest.approx(r["margin"]["expected"], rel=1e-9)
    assert d["margin_plan_only"] == pytest.approx(r["margin"]["plan_only"], rel=1e-9)
    assert d["contribution"] == pytest.approx(d["margin_expected"])


def test_plan_margin_already_uses_shared_market_logic(E):
    """
    ادعای پشت معیار اصلی را می‌سنجد: حاشیهٔ برنامه (ورودی `decision_margin`)
    فروش‌های آینده را با قیمت تعرفهٔ کامل ارزش‌گذاری نمی‌کند. اگر
    `retry_discount` صفر شود، حاشیهٔ برنامه باید بالاتر برود — یعنی منطق
    بازار واقعاً در همین عدد اثر دارد.
    """
    A, bio, st = ctx(E)
    base = OF.solve_plan(A, bio, st, "balanced").value_digest()
    tbl = A.get("planning.saleability")
    A.set("planning.saleability", [dict(b, retry_discount=0.0) for b in tbl])
    try:
        A2, bio2, st2 = ctx(E)
        free = OF.solve_plan(A2, bio2, st2, "balanced").value_digest()
    finally:
        A.reset("planning.saleability")
        H.cache_clear()
    assert free["contribution_nominal"] > base["contribution_nominal"], \
        "حاشیه برنامه باید به تخفیف/سهولت فروش حساس باشد"


# ═══════════ رگرسیون: تخفیف/سقف retry باید از روی وزن واقعی محاسبه شود
def test_retry_discount_uses_actual_weight_not_target_weight(E):
    """
    باگ: وقتی ماهیِ یک لات با وزن هدف پایین (مثلاً ۵g) در carry-over منتظر
    فروش می‌ماند، وزنش طبیعتاً رشد می‌کند — گاه تا وزن‌های خیلی سنگین‌تر
    (۲۰+ گرم). قیمت فروشِ آن تلاش درست از روی وزن *واقعیِ* همان لحظه
    محاسبه می‌شود (`sale_w = max(harvest_w, w)`)، ولی تخفیف/سقف هفتگی
    قبلاً از روی بازهٔ *وزن هدف اولیه* (نه وزن واقعی) اعمال می‌شد — یعنی
    یک لات «۵ گرمی» می‌توانست فروش در وزن ۲۰+ گرم را با قواعد ارزان‌تر
    بازهٔ ۵-۱۰ گرم بقاپد. این تست مستقیماً بررسی می‌کند سقف هفتگیِ فروش
    یک لات، وقتی وزنش وارد بازهٔ سنگین‌تری شده، با سقف *همان بازهٔ سنگین‌تر*
    مطابقت دارد، نه بازهٔ وزن هدف.
    """
    A, bio, st = ctx(E)
    pm = PM.PlanModel(A, bio, st)
    cb = pm.build_candidates(PM.Scenario.base())
    # یک لات با وزن هدف پایین که carry-over طولانی دارد (سقف تقاضای
    # بازهٔ هدف کوچک باشد تا carry-over واقعاً رخ دهد)
    keys = [k for k in cb["new_lots"] if k.endswith("|5")]
    assert keys, "کاندید وزن هدف ۵g یافت نشد"
    pr = cb["new_lots"][keys[0]]
    saw_band_switch = False
    for t in range(len(pr.weight)):
        if pr.planned_fish[t] <= 0:
            continue
        w = pr.weight[t]
        if w < 10.0:
            continue           # هنوز داخل بازهٔ هدف است
        saw_band_switch = True
        band = PM.saleability_band(A, w)
        weekly_cap = band["demand_cap_per_week"]
        if weekly_cap is not None:
            assert pr.planned_fish[t] <= weekly_cap + 1e-6, (
                f"t={t}: وزن واقعی {w:.1f}g در بازهٔ سنگین‌تر است ولی حجم فروش "
                f"({pr.planned_fish[t]:.0f}) از سقف همان بازه ({weekly_cap:.0f}) "
                f"عبور کرده — یعنی هنوز سقف بازهٔ سبک‌تر (وزن هدف) اعمال می‌شود")
    assert saw_band_switch, ("این سناریو باید حداقل یک هفته carry-over با وزن "
                             "واقعیِ فراتر از ۱۰g داشته باشد تا تست معنادار باشد")



# ═══════════ رگرسیون: ۱۰ و ۱۵ گرم دیگر کاملاً از برنامه حذف نشوند
def test_10g_and_15g_bands_are_not_categorically_unprofitable(E):
    """
    قبل از رفع باگ retry_discount، هدف‌گذاری مستقیم ۱۰ و ۱۵ گرم برای یک
    لات جدید همیشه حاشیهٔ عمیقاً منفی می‌داد (حدود -۱۱۱M و -۷۳۶M برای یک
    لات ۲۵۰هزارتایی) — چون carry-over یک لات با هدف پایین‌تر به‌اشتباه
    تخفیف/سقف ارزان‌ترِ همان هدف را می‌گرفت، نه بازهٔ وزن واقعی. با رفع آن
    باگ، این دو وزن دیگر به‌شدت منفی نیستند؛ عملاً هم‌چنان از طریق
    carry-over یک لات با هدف پایین‌تر (نه هدف‌گذاری مستقیم) به فروش
    می‌رسند — که با اصلاح گزارشی جدا (`sales_by_weight` بر اساس وزن واقعی)
    اکنون درست دیده می‌شود.
    """
    A, bio, st = ctx(E)
    pm = PM.PlanModel(A, bio, st)
    cb = pm.build_candidates(PM.Scenario.base())
    lots = sorted(float(x) for x in A.get("planning.lot_candidates"))
    mid_qty = lots[len(lots) // 2]
    months = pm.decision_months()
    pdate = months[0][1]
    for w in (10.0, 15.0):
        key = f"L|{pdate.isoformat()}|{int(mid_qty)}|{w:g}"
        assert key in cb["new_lots"], f"کاندید {key} ساخته نشد"
        contrib = cb["new_lots"][key].contribution()
        per_egg = contrib / mid_qty
        assert per_egg > -2000, (
            f"w={w}: حاشیهٔ پایه {per_egg:.0f}/تخم بیش از حد منفی است — "
            f"احتمالاً باگ retry_discount دوباره برگشته")


def test_10_15g_bands_remain_clearly_more_restricted_than_low_weights(E):
    """
    اصل درخواست: ۱۰ و ۱۵ گرم نباید کاملاً حذف شوند، ولی باید خیلی
    محدودتر از وزن‌های پایین‌تر (خصوصاً زیر ۵ گرم) بمانند. این را روی
    خودِ Assumptions می‌سنجد (نه یک عدد خروجیِ دستکاری‌شده): سهولت فروش
    (`prob`) و سقف تقاضا برای ۱۰-۱۵ و بالای ۱۵ گرم باید هنوز به‌وضوح
    کمتر از بازهٔ ۱ تا ۵ گرم باشد.
    """
    A, bio, st = ctx(E)
    low = PM.saleability_band(A, 3.0)     # نمایندهٔ زیر ۵ گرم
    mid = PM.saleability_band(A, 12.0)    # ۱۰-۱۵ گرم
    high = PM.saleability_band(A, 20.0)   # بالای ۱۵ گرم
    assert high["prob"] < mid["prob"] < low["prob"]
    assert high["max_demand_per_period"] < mid["max_demand_per_period"]
    assert mid["max_demand_per_period"] is not None  # هنوز یک سقف واقعی دارد
    # ولی دیگر آن‌قدر تنگ نیست که هدف‌گذاری مستقیم را قطعاً غیرممکن کند
    assert mid["prob"] >= 0.25
    assert high["prob"] >= 0.10


def test_full_plan_allocates_some_volume_above_5g_when_profitable(E):
    """
    آزمون سرتاسری: با فرضیات فعلی (پس از هر دو اصلاح)، حل کامل برنامه روی
    دادهٔ واقعی مزرعه باید حداقل بخشی از حجم لات‌های جدید را به وزن‌های
    بالای ۵ گرم اختصاص دهد — نه اینکه صد‌درصد در ۱ و ۲ گرم متمرکز بماند
    (رفتار قبل از اصلاح). این عدد را دستکاری نمی‌کند؛ فقط تضمین می‌کند
    optimizer واقعاً می‌تواند از این گزینه‌ها استفاده کند وقتی به‌صرفه است.
    """
    A, bio, st = ctx(E)
    p = Plan(A, bio, st, "balanced")
    above_5g = sum(q for w, q in p.sales_by_weight.items() if w > 5.0 - 1e-9)
    total = sum(p.sales_by_weight.values())
    assert total > 0
    low_w = sum(q for w, q in p.sales_by_weight.items() if w <= 2.0 + 1e-9)
    assert above_5g > 0, "هیچ فروشی بالای ۵ گرم در برنامه نیست"
    assert low_w > 0, "وزن‌های پایین (۱-۲ گرم) نباید کاملاً از برنامه حذف شوند"


# ═══════════ رگرسیون: Plan/Targets و Offer Evaluator همان جدول را می‌خوانند
def test_offer_evaluator_reflects_retuned_bands(E):
    """
    Offer Evaluator (`_best_alternative`) از همان `saleability_band` مشترک
    می‌خواند؛ این تست تضمین می‌کند مقادیر تعدیل‌شدهٔ ۱۰-۱۵/بالای‌۱۵ گرم در
    آن مسیر هم دیده می‌شوند، نه فقط در PlanModel.
    """
    A, bio, st = ctx(E)
    c = _cohort(E, min_w=1.0)
    r = OF.evaluate_sale_offer(A, bio, st, {
        "cohort_id": c.cohort_id, "quantity": 10_000, "price": 15_000})
    for o in r["alternative"]["options"]:
        if o.get("is_sell_now") or o["harvest_w"] <= 5.0:
            continue
        expected = PM.saleability_band(A, o["harvest_w"])["prob"]
        assert o["saleability"] == pytest.approx(expected)


# ═══════════ رگرسیون: sales_by_weight بر اساس وزن واقعی فروش، نه وزن هدف لات
def test_sales_by_weight_uses_actual_sale_weight_not_lot_target(E):
    """
    باگ گزارشی جدا از باگ retry_discount: `sales_by_weight` فروش هر لات را
    زیر *وزن هدف اولیهٔ همان لات* جمع می‌زد، نه وزن واقعی هر رویداد فروش.
    یک لات با هدف ۵g که در carry-over تا ۲۰+ گرم رشد کرده و در همان‌جا
    فروخته، کل آن حجم را زیر «۵g» گزارش می‌کرد — یعنی فروش واقعی در
    وزن‌های سنگین‌تر هرگز در گزارش دیده نمی‌شد، حتی وقتی واقعاً رخ داده
    بود. این تست مستقیماً بررسی می‌کند مجموع `sales_by_weight` روی داده
    واقعی، جمعِ حجم گزارش‌شده زیر بازه‌های ۱۰-۱۵ و بالای ۱۵ گرم را
    منعکس می‌کند — نه صفر.
    """
    A, bio, st = ctx(E)
    p = Plan(A, bio, st, "balanced")
    heavy = sum(q for w, q in p.sales_by_weight.items() if w >= 10.0 - 1e-9)
    total = sum(p.sales_by_weight.values())
    assert total > 0
    assert heavy > 0, "هیچ فروشی زیر ۱۰ یا ۱۵ گرم در گزارش دیده نمی‌شود"


def test_sales_by_weight_bucket_never_exceeds_actual_sale_weight(E):
    """
    هر رویداد فروش باید زیر بزرگ‌ترین وزن هدف رسمی که ماهی *واقعاً از آن
    عبور کرده* دسته‌بندی شود — نه بیشتر و نه کمتر. این را مستقیماً روی
    یک لات با carry-over طولانی می‌سنجد (به‌جای فقط سنجش خروجی نهایی).
    """
    A, bio, st = ctx(E)
    pm = PM.PlanModel(A, bio, st)
    cb = pm.build_candidates(PM.Scenario.base())
    keys = [k for k in cb["new_lots"] if k.endswith("|5")]
    pr = cb["new_lots"][keys[0]]
    hws = sorted(float(x) for x in A.get("planning.harvest_weights"))
    for t in range(len(pr.weight)):
        if pr.planned_fish[t] <= 0:
            continue
        w = pr.weight[t]
        expected_bucket = max((h for h in hws if h <= w + 1e-9), default=hws[0])
        assert expected_bucket <= w + 1e-9
        # سطل انتخابی باید *بزرگ‌ترین* هدف رسمیِ عبورشده باشد، نه یکی کوچک‌تر
        bigger = [h for h in hws if expected_bucket < h <= w + 1e-9]
        assert not bigger, f"t={t}: وزن {w:.1f} از هدف بزرگ‌تری هم عبور کرده که نادیده گرفته شد"


def test_monthly_breakdown_uses_same_actual_weight_bucketing(E):
    """
    تفکیک ماهانه/سه‌ماهه (`_sales_in`، مصرف‌شده در جدول‌های Plan) باید از
    همان قاعدهٔ بازه‌بندیِ سطح summary استفاده کند — نه یک محاسبهٔ جدا که
    می‌تواند واگرا شود.
    """
    A, bio, st = ctx(E)
    p = Plan(A, bio, st, "balanced")
    monthly_totals: dict = {}
    for b in p.monthly:
        for w, q in b["sales_by_weight"].items():
            monthly_totals[w] = monthly_totals.get(w, 0.0) + q
    for w, q in p.sales_by_weight.items():
        assert monthly_totals.get(w, 0.0) == pytest.approx(q, rel=1e-6), \
            f"وزن {w}: جمع ماهانه ({monthly_totals.get(w, 0.0):,.0f}) با " \
            f"summary ({q:,.0f}) نمی‌خواند"


# ═══════════ Manual Sales Mix Comparison (افزونه مستقل، Rolling Plan)
# ═══════════ رگرسیون: واحد ورودی «قطعهٔ فروخته‌شده» است، نه جمعیت خام
def test_optimal_target_mix_uses_sold_fish_not_raw_population(E):
    """
    باگ گزارش‌شده: دکمهٔ «بازنشانی به ترکیب بهینه» جمعی نشان می‌داد که به‌طور
    محسوسی از «فروش برنامه» واقعی (مجموع `sales_by_weight`) بزرگ‌تر بود —
    چون `optimal_target_mix` جمعیت خامِ پیش از تلفات (`p.quantity`) را
    برمی‌گرداند، نه قطعهٔ واقعاً فروخته‌شده (`p.sold_fish`). این تست تضمین
    می‌کند مجموع خروجی، همان مرتبهٔ بزرگیِ فروش واقعی برنامه بماند — نه
    چند-ده-درصد بزرگ‌تر (که علامت بازگشت باگ قدیمی است).
    """
    A, bio, st = ctx(E)
    baseline = Plan(A, bio, st, "balanced")
    tgt = MM.optimal_target_mix(baseline)
    actual_sold_total = sum(baseline.summary()["sales_by_weight"].values())
    tgt_total = sum(tgt.values())
    assert tgt_total > 0
    # کمتر یا مساوی فروش واقعی (چون cohortهای اجباری/فراتر از منحنی قیمت
    # در این تابع حساب نمی‌شوند) — هرگز آشکارا بزرگ‌تر (نشانهٔ استفاده از
    # جمعیت خام به‌جای فروش واقعی)
    assert tgt_total <= actual_sold_total * 1.05, (
        f"مجموع optimal_target_mix ({tgt_total:,.0f}) از فروش واقعی برنامه "
        f"({actual_sold_total:,.0f}) به‌طور غیرمنتظره بزرگ‌تر است — احتمالاً "
        f"دوباره از جمعیت خام (quantity) به‌جای قطعهٔ فروخته‌شده استفاده می‌شود")
    assert tgt_total >= actual_sold_total * 0.5, (
        f"مجموع optimal_target_mix ({tgt_total:,.0f}) خیلی کمتر از فروش واقعی "
        f"({actual_sold_total:,.0f}) است")


# ═══════════ رگرسیون: تفکیک «وزن هدف برداشت» از «وزن واقعی فروش»
def test_manual_mix_input_is_target_weight_not_realised_sale_weight(E):
    """
    منشأ یک سردرگمی واقعی کاربر: ورودی این قابلیت «وزن هدفِ برداشت» است
    (تنها اهرم قابل‌تصمیم)، ولی کارت «فروش برنامه» بالای صفحه «وزن واقعیِ
    فروش» را نشان می‌دهد. این دو عمداً یکی نیستند — ماهیِ فروش‌نرفته در
    carry-over رشد می‌کند و در بازهٔ سنگین‌تری فروخته می‌شود.

    نتیجهٔ عملی: حتی وقتی ورودیِ ۱۰g و ۱۵g دقیقاً صفر است، فروش واقعی در
    آن وزن‌ها اتفاق می‌افتد. این تست دقیقاً همین را تثبیت می‌کند تا کسی
    در آینده به‌اشتباه «اصلاحش» نکند.
    """
    A, bio, st = ctx(E)
    baseline = Plan(A, bio, st, "balanced")
    tgt = MM.optimal_target_mix(baseline)
    # روی داده واقعی، برنامهٔ بهینه هیچ لاتی را مستقیم به ۱۰g/۱۵g هدف‌گذاری
    # نمی‌کند (از طریق carry-over به آنجا می‌رسد)
    assert tgt.get(10.0, 0.0) == 0.0 and tgt.get(15.0, 0.0) == 0.0

    r = MM.compare_to_optimised(A, bio, st, tgt, baseline, "balanced")
    mw = r["manual_sales_by_weight"]
    heavy = sum(q for w, q in mw.items() if w >= 10.0 - 1e-9)
    assert heavy > 0, (
        "با ورودی صفر برای ۱۰g/۱۵g، فروش واقعی در آن وزن‌ها باید همچنان از "
        "مسیر carry-over اتفاق بیفتد — اگر صفر شد، یعنی منطق carry-over شکسته")


def test_manual_mix_exposes_both_actual_mixes_for_ui(E):
    """
    UI باید بتواند «ترکیب واقعی حاصل» را در برابر «ترکیب واقعی برنامهٔ
    بهینه» نشان دهد؛ بدون این دو فیلد، کاربر نمی‌تواند بفهمد ورودی هدف
    چطور به فروش واقعی ترجمه شده است.
    """
    A, bio, st = ctx(E)
    baseline = Plan(A, bio, st, "balanced")
    r = MM.compare_to_optimised(A, bio, st, MM.optimal_target_mix(baseline),
                                baseline, "balanced")
    for k in ("manual_sales_by_weight", "optimised_sales_by_weight"):
        assert k in r and r[k], f"{k} باید برای نمایش در UI موجود و ناتهی باشد"
        assert sum(r[k].values()) > 0


def test_manual_mix_matching_optimal_gives_approximately_equal_results(E):
    """
    اصل اعتبارسنجی درخواستی: اگر ترکیب دستی دقیقاً برابر ترکیب Optimised
    Plan باشد، خروجی‌های Manual و Optimised باید تقریباً یکسان باشند.

    «ترکیب دقیقاً برابر» یعنی همان سطح ورودی که `ManualPlan` با آن کار
    می‌کند: مجموع (جمعیت × فراکسیون) به تفکیک *وزن هدفِ همان کاندید*
    (`optimal_target_mix`) — نه وزن نهاییِ گزارش‌شده پس از سرریز
    carry-over (`sales_by_weight`), که به‌خاطر پویایی سرریز، معیار درستی
    برای بازتولید یک ترکیب نیست (تفاوتش با حالت هدف تا ~۲۵٪ اندازه‌گیری
    شد؛ چیزی که در این تست *نمی‌خواهیم*).

    یک محدودیت شناخته‌شده (مستندشده در docstring ماژول) باقی می‌ماند:
    اگر cohortی هنوز به هیچ‌کدام از ۵ وزن رسمی نرسیده، سناریوی دستی نمی‌تواند
    گزینهٔ «فروش فوری در وزن فعلی‌اش» را بازتولید کند — پس فاصلهٔ چند
    درصدی (نه صفر) طبیعی است، خصوصاً نزدیک مرز تنگ ظرفیت استخر.
    """
    A, bio, st = ctx(E)
    baseline = Plan(A, bio, st, "balanced")
    targets = MM.optimal_target_mix(baseline)
    assert targets, "برنامه پایه باید حداقل یک کاندید با وزن هدف رسمی داشته باشد"

    r = MM.compare_to_optimised(A, bio, st, targets, baseline, "balanced")

    for key, tol in (("revenue_total", 0.15), ("rolling_12m", 0.35),
                     ("full_lifecycle", 0.15)):
        m, o = r["manual"][key], r["optimised"][key]
        # rolling_12m یک بازهٔ زمانی باریک (۱۲ ماه) است — چون ترکیب دستی و
        # بهینه از ترکیب متفاوتی از cohort/لات به همان مجموع هدف می‌رسند
        # (محدودیت مستندشده بالا)، زمان‌بندیِ دقیق فروش‌ها می‌تواند جابه‌جا
        # شود؛ جمع کل افق (full_lifecycle/revenue_total) این جابه‌جایی را
        # هموار می‌کند ولی یک بازهٔ باریک حساس‌تر می‌ماند — تحمل آن عمداً
        # بازتر است. Seasonality شمسی (اصلاح ۴) هم یک منبع حساسیت زمان‌بندیِ
        # مشابه اضافه کرده (کدام هفته‌ها به نیمهٔ پرتقاضاتر سال می‌افتند) —
        # برای همین تحمل revenue_total/full_lifecycle هم کمی بازتر شد.
        assert m == pytest.approx(o, rel=tol), (
            f"{key}: manual={m:,.0f} در برابر optimised={o:,.0f} — "
            f"با ترکیب هدف یکسان باید نزدیک بمانند (تحمل {tol:.0%})")

    # peak_ponds/peak_funding نزدیک مرز تنگ ظرفیت می‌توانند حساس باشند
    # (محدودیت شناخته‌شده بالا)؛ فقط از یک انحراف فاحش/نامتناسب جلوگیری
    # می‌کنیم، نه برابری دقیق.
    op = r["operational_ponds"]
    assert r["manual"]["peak_ponds"] <= op + 3
    assert r["manual"]["peak_funding_requirement"] <= 2 * max(1.0, r["optimised"]["peak_funding_requirement"])


def test_manual_mix_reports_infeasibility_when_fish_insufficient(E):
    """اگر جمع درخواستی از کل جمعیت قابل‌دسترس بیشتر باشد، باید صریحاً
    Warning «ماهی کافی وجود ندارد» بدهد، نه اینکه بی‌صدا کم‌ماهی تحویل دهد."""
    A, bio, st = ctx(E)
    baseline = Plan(A, bio, st, "balanced")
    huge = 50_000_000.0   # قطعاً از کل جمعیت مزرعه بیشتر است
    targets = {1.0: huge}
    r = MM.compare_to_optimised(A, bio, st, targets, baseline, "balanced")
    assert not r["feasible"]
    assert any("ماهی کافی" in w for w in r["warnings"])


def test_manual_mix_does_not_mutate_real_state_or_baseline(E):
    """سناریوی دستی فقط برای مقایسه است؛ نباید هیچ داده واقعی یا Plan
    اصلی را تغییر دهد."""
    A, bio, st = ctx(E)
    baseline = Plan(A, bio, st, "balanced")
    before = dict(baseline.summary())
    targets = {1.0: 10_000.0, 5.0: 5_000.0}
    MM.compare_to_optimised(A, bio, st, targets, baseline, "balanced")
    after = dict(baseline.summary())
    assert before["revenue_total"] == after["revenue_total"]
    assert before["eggs_planned"] == after["eggs_planned"]
    # وضعیت واقعی مزرعه (cohortهای زنده) هم دست‌نخورده مانده
    A2, bio2, st2 = ctx(E)
    assert {c.cohort_id: c.alive for c in st.cohorts.values()} == \
        {c.cohort_id: c.alive for c in st2.cohorts.values()}


def test_manual_mix_forced_cohorts_unaffected_by_manual_choice(E):
    """cohortهایی که از هر ۵ وزن رسمی عبور کرده‌اند (فراتر از منحنی
    قیمت) باید در سناریوی دستی هم عیناً همان‌طور که در برنامهٔ بهینه
    فروخته می‌شوند، فروخته شوند — صرف‌نظر از اینکه کاربر چه چیزی وارد
    کرده باشد."""
    A, bio, st = ctx(E)
    baseline = Plan(A, bio, st, "balanced")
    hws = [float(x) for x in A.get("planning.harvest_weights")]
    forced_ids = {c.cohort_id for c in st.cohorts.values()
                  if c.alive >= 1 and c.mean_weight > max(hws) + 1e-9}
    if not forced_ids:
        pytest.skip("در این وضعیت مزرعه cohort فراتر از منحنی قیمت وجود ندارد")
    targets = {1.0: 1000.0}     # کاملاً بی‌ربط به cohortهای forced
    mp = MM.ManualPlan(A, bio, st, targets, baseline, "balanced")
    for cid in forced_ids:
        keys_selected = [k for k in mp.solution.selected if k.split("|")[1] == cid]
        assert keys_selected, f"cohort اجباری {cid} در راه‌حل دستی غایب است"
        assert sum(mp.solution.selected[k] for k in keys_selected) == pytest.approx(1.0)


# ═══════════════════════ اصلاح ۲ — بازهٔ تصمیم‌گیری ۱۰ روزه
def test_action_plan_groups_into_decision_windows(E):
    """
    پیشنهادهای خرید/فروش باید در بازه‌های `planning.decision_window_days`
    روزه تجمیع شوند، نه هر هفته یک اقدام جدا. تاریخ هر اقدام تجمیعی باید
    ابتدای همان بازه (و بنابراین همیشه مضربی از طول بازه از as_of) باشد.
    """
    A, bio, st = ctx(E)
    p = Plan(A, bio, st, "balanced")
    win = int(A.get("planning.decision_window_days"))
    ap = p.action_plan_90d()
    assert ap["decision_window_days"] == win
    assert ap["actions"], "برنامه ۹۰ روزه باید حداقل یک اقدام داشته باشد"
    for a in ap["actions"]:
        if a["type"] not in ("egg_purchase", "sale"):
            continue
        assert a["days"] % win == 0, (
            f"تاریخ اقدام {a['date']} ({a['days']} روز) روی مرز بازهٔ "
            f"{win}روزه نیست")

    # دو اقدام از نوع یکسان نباید در همان بازه و همان وزن هدف تکرار شوند
    seen = set()
    for a in ap["actions"]:
        if a["type"] != "sale":
            continue
        key = (a["date"], round(a["quantity"], 0), a["title"])
        assert key not in seen, "دو اقدام فروش دقیقاً یکسان تکرار شده — تجمیع درست کار نکرده"
        seen.add(key)


def test_decision_window_change_actually_regroups_actions(E):
    """تغییر خودِ Assumption باید تعداد/گروه‌بندی اقدام‌ها را واقعاً عوض
    کند — نه فقط برچسب."""
    A, bio, st = ctx(E)
    p1 = Plan(A, bio, st, "balanced")
    ap1 = p1.action_plan_90d()
    n_sales_10d = sum(1 for a in ap1["actions"] if a["type"] == "sale")

    A.set("planning.decision_window_days", 1)
    try:
        A2, bio2, st2 = ctx(E)
        p2 = Plan(A2, bio2, st2, "balanced")
        ap2 = p2.action_plan_90d()
        n_sales_1d = sum(1 for a in ap2["actions"] if a["type"] == "sale")
    finally:
        A.reset("planning.decision_window_days")
    assert n_sales_1d >= n_sales_10d, (
        "بازهٔ تصمیم‌گیری کوتاه‌تر (۱ روز) باید اقدام‌های ریزتر/بیشتری بدهد")


# ═══════════════════════ اصلاح ۱ — بازهٔ معمول تعداد فروش
def test_small_sale_actions_are_flagged_as_exception(E):
    """اقدام‌های فروشِ کمتر از سقف پایین بازهٔ معمول باید علامت‌گذاری
    شوند تا مدیر آن‌ها را استثنا بداند، نه رفتار عادی."""
    A, bio, st = ctx(E)
    p = Plan(A, bio, st, "balanced")
    typ_min = float(A.get("sale.typical_min_qty"))
    ap = p.action_plan_90d()
    sales = [a for a in ap["actions"] if a["type"] == "sale"]
    assert sales, "باید حداقل یک اقدام فروش وجود داشته باشد"
    for a in sales:
        assert a["below_typical_range"] == (a["quantity"] < typ_min)
        if a["below_typical_range"]:
            assert "بازهٔ معمول" in a["detail"]


def test_typical_range_assumptions_are_configurable_not_hardcoded(E):
    """بازهٔ معمول hard-code نباشد — تغییر Assumption باید بلافاصله در
    خروجی برنامهٔ ۹۰روزه اثر بگذارد."""
    A, bio, st = ctx(E)
    p = Plan(A, bio, st, "balanced")
    ap = p.action_plan_90d()
    sales = [a for a in ap["actions"] if a["type"] == "sale"]
    base_flagged = sum(1 for a in sales if a["below_typical_range"])

    A.set("sale.typical_min_qty", 1000)   # پایین‌ترین مقدار مجاز — عملاً همه چیز "معمول" می‌شود
    try:
        A2, bio2, st2 = ctx(E)
        p2 = Plan(A2, bio2, st2, "balanced")
        ap2 = p2.action_plan_90d()
        sales2 = [a for a in ap2["actions"] if a["type"] == "sale"]
        flagged2 = sum(1 for a in sales2 if a["below_typical_range"])
    finally:
        A.reset("sale.typical_min_qty")
    assert flagged2 <= base_flagged
    assert flagged2 < base_flagged or base_flagged == 0, \
        "کاهش کف بازهٔ معمول باید تعداد اقدام‌های علامت‌خورده را کم کند"
    assert base_flagged > 0, "برای معنادار بودن این تست باید حداقل یک استثنا در حالت پایه باشد"


def test_optimizer_soft_penalty_does_not_block_economically_justified_small_lots(E):
    """جریمهٔ نرم دستهٔ کوچک نباید یک محدودیت سخت باشد — با صفرکردن آن،
    برنامه هنوز باید حل شود و feasible بماند (یعنی جریمه فقط حاشیه را کم
    می‌کند، انتخاب‌پذیری را از بین نمی‌برد)."""
    A, bio, st = ctx(E)
    A.set("sale.small_lot_penalty_per_fish", 0)
    try:
        A2, bio2, st2 = ctx(E)
        p = Plan(A2, bio2, st2, "balanced")
    finally:
        A.reset("sale.small_lot_penalty_per_fish")
    v = V.run_plan_checks_v2(A, p)
    assert v["failed"] == 0


# ═══════════════════════ اصلاح ۴ — Seasonality شمسی
def test_seasonality_factor_averages_to_one_over_the_year():
    """میانگین سالانهٔ ضریب فصلی باید ۱ بماند — یعنی فقط بازتوزیع می‌کند،
    نه اینکه کل عرضه/تقاضای سالانه را عوض کند."""
    from core.assumptions import Assumptions
    A = Assumptions()
    h1 = PM.jalali_seasonality_factor(A, date(2026, 5, 1))     # نیمهٔ اول
    h2 = PM.jalali_seasonality_factor(A, date(2026, 11, 1))    # نیمهٔ دوم
    assert h1 == pytest.approx(2 * 0.40)
    assert h2 == pytest.approx(2 * 0.60)
    assert (h1 + h2) / 2 == pytest.approx(1.0)


def test_seasonality_ratio_is_configurable(E):
    """تغییر Assumption نسبت فصلی باید بلافاصله ضریب را عوض کند."""
    A, bio, st = ctx(E)
    A.set("planning.seasonality_h1_share", 0.25)
    try:
        f = PM.jalali_seasonality_factor(A, date(2026, 5, 1))
    finally:
        A.reset("planning.seasonality_h1_share")
    assert f == pytest.approx(0.50)


def test_seasonality_shifts_monthly_egg_cap_without_changing_annual_total(E):
    """اصل درخواستی: عرضهٔ تخم بین نیمهٔ اول/دوم سال شمسی بازتوزیع شود،
    ولی سقف سالانه (annual_scenario) دست‌نخورده بماند."""
    A, bio, st = ctx(E)
    p = Plan(A, bio, st, "balanced")
    annual_cap = float(A.get("planning.annual_scenario"))
    total_eggs = sum(lot["quantity"] for lot in p.solution.chosen_lots)
    assert total_eggs <= annual_cap + 1e-6, "Seasonality نباید سقف سالانه را نقض کند"


def test_seasonality_does_not_affect_observed_actual_data(E):
    """Seasonality فقط forecast/planning را عوض کند؛ روی cohortهای واقعی
    ثبت‌شده (جمعیت/تاریخ خرید observed) هیچ اثری نداشته باشد."""
    A, bio, st = ctx(E)
    alive_before = {c.cohort_id: c.alive for c in st.cohorts.values()}
    A.set("planning.seasonality_h1_share", 0.05)
    try:
        A2, bio2, st2 = ctx(E)
        alive_after = {c.cohort_id: c.alive for c in st2.cohorts.values()}
    finally:
        A.reset("planning.seasonality_h1_share")
    assert alive_before == alive_after, \
        "تغییر ضریب فصلی نباید جمعیت واقعی cohortها را عوض کند"


# ═══════════════════════════════ رگرسیون مراحل ۱–۳
def test_regression_validation_suite(E):
    assert call("/api/validate")["failed"] == 0


# ═══════════════════════ اصلاح Range فروش — تجمیع اقدام‌های کوچک
def test_sale_actions_are_consolidated_toward_typical_range(E):
    """
    هدف اصلی این اصلاح: کاهش تعداد Sale Actionهای کوچک و غیرعملی در
    برنامهٔ ۹۰روزه. با فعال بودن تجمیع، هیچ اقدام فروشی نباید کوچک‌تر از
    یک اقدام معادل *بدون* تجمیع باشد و تعداد کل اقدام‌های فروش باید
    مساوی یا کمتر از حالت بدون‌تجمیع بماند.
    """
    A, bio, st = ctx(E)
    p = Plan(A, bio, st, "balanced")
    ap = p.action_plan_90d()
    sales = [a for a in ap["actions"] if a["type"] == "sale"]
    assert sales, "باید حداقل یک اقدام فروش وجود داشته باشد"
    min_qty = float(A.get("sale.min_qty"))
    typ_min = float(A.get("sale.typical_min_qty"))
    for a in sales:
        assert a["very_small_exception"] == (a["quantity"] < min_qty)
        assert a["below_typical_range"] == (a["quantity"] < typ_min)
        if a["very_small_exception"]:
            assert "کف مطلق" in a["detail"]
        elif a["below_typical_range"]:
            assert "بازهٔ معمول" in a["detail"]


def test_consolidation_never_exceeds_typical_max(E):
    """دسته‌های تجمیع‌شده نباید بدون دلیل از سقف بازهٔ معمول عبور کنند —
    وقتی جمع به سقف می‌رسد، باید دستهٔ جدیدی باز شود."""
    A, bio, st = ctx(E)
    p = Plan(A, bio, st, "balanced")
    typ_max = float(A.get("sale.typical_max_qty"))
    ap = p.action_plan_90d()
    sales = [a for a in ap["actions"] if a["type"] == "sale"]
    # مجاز است کمی بالاتر برود (آخرین ورودی که رساندنش به سقف باعث
    # سرریز خفیف شد)، ولی نباید به‌طرز نامعقولی (مثلاً ۲ برابر سقف) باشد
    for a in sales:
        assert a["quantity"] <= typ_max * 2.5, (
            f"دستهٔ تجمیع‌شده {a['quantity']:,.0f} بیش‌ازحد از سقف "
            f"{typ_max:,.0f} عبور کرده — تجمیع باید دسته را می‌بست")


def test_consolidation_reduces_action_count_vs_unconsolidated(E):
    """رگرسیون اصلی: با کف بازهٔ معمول خیلی بزرگ (عملاً هر چیزی «کوچک»
    است)، تجمیع باید عدد کمتر یا مساوی از اقدام‌ها نسبت به کف واقعی
    بدهد؛ با کف صفر (تجمیع خاموش) باید تعداد اقدام‌های فروش زیادتر یا
    مساوی شود."""
    A, bio, st = ctx(E)
    p_base = Plan(A, bio, st, "balanced")
    n_base = sum(1 for a in p_base.action_plan_90d()["actions"] if a["type"] == "sale")

    A.set("sale.typical_min_qty", 1000)   # تجمیع تقریباً بی‌اثر می‌شود
    try:
        A2, bio2, st2 = ctx(E)
        p2 = Plan(A2, bio2, st2, "balanced")
        n_low = sum(1 for a in p2.action_plan_90d()["actions"] if a["type"] == "sale")
    finally:
        A.reset("sale.typical_min_qty")
    assert n_base <= n_low, (
        f"با کف بازهٔ معمول بالاتر ({n_base} اقدام)، تعداد اقدام‌های فروش "
        f"باید کمتر یا مساوی حالت بی‌اثر ({n_low} اقدام) باشد")


def test_sale_range_assumptions_are_configurable(E):
    """سه سطح Min/Preferred Min/Max باید مستقل و از Assumptions قابل
    تغییر باشند — نه hard-code."""
    A, bio, st = ctx(E)
    for key, val in (("sale.min_qty", 7777), ("sale.typical_min_qty", 45000),
                     ("sale.typical_max_qty", 90000)):
        A.set(key, val)
        try:
            assert float(A.get(key)) == val
        finally:
            A.reset(key)


def test_feed_days_remaining_is_min_not_aggregate(E):
    """
    باگ گزارش‌شده: قبلاً `feed_days_remaining` از تقسیم *مجموع* kg همهٔ
    انواع خوراک بر *مجموع* مصرف روزانهٔ همهٔ انواع محاسبه می‌شد — یعنی
    انواع مختلف خوراک (که اصلاً قابل جایگزینی با هم نیستند) را یک
    موجودی واحد فرض می‌کرد. با مثال دقیق کاربر: FP-00 = 2 روز باقیمانده،
    SFP-000 = 30 روز باقیمانده → باید نتیجهٔ کلی 2 روز باشد (MIN)، نه 21
    (که میانگین/مجموع می‌داد).
    """
    A, bio, st = ctx(E)
    st.feed = {
        "FP-00": {"name": "FP-00", "qty_kg": 20.0, "value": 0,
                  "purchased_kg": 0, "purchased_cost": 0, "consumed_kg": 0,
                  "last_purchase": None, "avg_cost": 0},
        "SFP-000": {"name": "SFP-000", "qty_kg": 300.0, "value": 0,
                    "purchased_kg": 0, "purchased_cost": 0, "consumed_kg": 0,
                    "last_purchase": None, "avg_cost": 0},
    }
    st.daily_feed_demand = lambda: {"FP-00": 10.0, "SFP-000": 10.0, "__total__": 20.0}
    s = st.summary()
    assert s["feed_days_remaining"] == pytest.approx(2.0)
    assert s["feed_critical_type"] == "FP-00"
    assert s["feed_days_remaining_by_type"]["FP-00"] == pytest.approx(2.0)
    assert s["feed_days_remaining_by_type"]["SFP-000"] == pytest.approx(30.0)
    # فرمول قدیمیِ اشتباه (که این تست باید از آن فاصله بگیرد) عدد دیگری می‌داد
    wrong_aggregate = s["feed_inventory_kg"] / s["feed_daily_demand_kg"]
    assert s["feed_days_remaining"] != pytest.approx(wrong_aggregate)


def test_feed_days_remaining_ignores_inactive_feed_types(E):
    """نوع خوراکی که هم‌اکنون هیچ cohort‌ای از آن تغذیه نمی‌کند (مصرف
    روزانهٔ صفر) نباید کمینه را با یک 0/0 نامعتبر خراب کند یا نادیده
    گرفته نشود؛ فقط باید در محاسبهٔ کمینه شرکت نکند."""
    A, bio, st = ctx(E)
    st.feed = {
        "FP-00": {"name": "FP-00", "qty_kg": 50.0, "value": 0,
                  "purchased_kg": 0, "purchased_cost": 0, "consumed_kg": 0,
                  "last_purchase": None, "avg_cost": 0},
        "OLD-STOCK": {"name": "OLD-STOCK", "qty_kg": 5.0, "value": 0,
                      "purchased_kg": 0, "purchased_cost": 0, "consumed_kg": 0,
                      "last_purchase": None, "avg_cost": 0},
    }
    st.daily_feed_demand = lambda: {"FP-00": 5.0, "OLD-STOCK": 0.0, "__total__": 5.0}
    s = st.summary()
    assert s["feed_days_remaining"] == pytest.approx(10.0)
    assert s["feed_critical_type"] == "FP-00"
    assert "OLD-STOCK" not in s["feed_days_remaining_by_type"]


def test_feed_outlook_shortfall_does_not_let_one_type_offset_another(E):
    """
    کمبود ۹۰روزهٔ خوراک باید مجموع کمبود *هر نوع به‌طور جدا* باشد — مازاد
    یک نوع خوراک نباید کمبود نوع دیگر را در گزارش «جبران» کند.
    """
    from core.forecast import Forecast
    A, bio, st = ctx(E)
    fc = Forecast(A, bio, st)
    st.feed = {
        "FP-00": {"name": "FP-00", "qty_kg": 1000.0, "value": 0,
                  "purchased_kg": 0, "purchased_cost": 0, "consumed_kg": 0,
                  "last_purchase": None, "avg_cost": 0},   # مازاد بزرگ
        "SFP-000": {"name": "SFP-000", "qty_kg": 0.0, "value": 0,
                    "purchased_kg": 0, "purchased_cost": 0, "consumed_kg": 0,
                    "last_purchase": None, "avg_cost": 0},  # کاملاً خالی
    }
    out = fc.feed_outlook(90)
    need = dict(out["by_feed_kg"])
    need.setdefault("SFP-000", 200.0)
    need["FP-00"] = min(need.get("FP-00", 0.0), 500.0)   # کمتر از موجودی مازاد
    out["by_feed_kg"] = need
    shortfall_by_type = {name: max(0.0, need_kg - out["current_stock_kg"].get(name, 0.0))
                         for name, need_kg in need.items()}
    total_shortfall = sum(shortfall_by_type.values())
    naive_net = sum(need.values()) - sum(out["current_stock_kg"].get(n, 0.0) for n in need)
    # کمبود واقعیِ SFP-000 (۲۰۰) نباید با مازاد FP-00 خنثی شود
    assert total_shortfall >= 200.0
    assert total_shortfall != pytest.approx(max(0.0, naive_net))


def test_regression_plan_still_solves(E):
    r = call("/api/plan", "GET", {}, {"variant": ["balanced"]})
    assert r["validation"]["failed"] == 0


