"""تست‌های واحد موتور زیستی/اقتصادی."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest                               # noqa: E402

from core.assumptions import Assumptions    # noqa: E402
from core.biology import Biology            # noqa: E402

A = Assumptions()
B = Biology(A)


def test_growth_hits_observed_milestones():
    """میلستون‌های وزنی باید دقیقاً با ستون Base جدول Growth Curve مرجع
    یکی باشند (Modelling Assumption، از config.yaml/growth.day_*)."""
    assert abs(B.weight_at_age(70) - 0.5) < 1e-9
    assert abs(B.weight_at_age(82) - 1.0) < 1e-9
    assert abs(B.weight_at_age(103) - 2.0) < 1e-9
    assert abs(B.weight_at_age(126) - 5.0) < 1e-9
    assert abs(B.weight_at_age(146) - 10.0) < 1e-9
    assert abs(B.weight_at_age(158) - 15.0) < 1e-9


def test_five_gram_is_a_direct_milestone_not_interpolated():
    """قبل از این اصلاح، ۵ گرم روی پلهٔ ۲→۱۰ گرم درون‌یابی می‌شد. اکنون
    یک لنگر مستقل از Growth Curve مرجع است — دقیقاً روز ۱۲۶، نه یک
    تخمین میان‌یابی‌شده."""
    assert B.age_at_weight(5.0) == pytest.approx(126.0, abs=1e-6)


def test_hatch_and_swimup_stages_are_tracked_but_not_weight_anchors():
    """مدل باید Hatch/Swim-up را برای milestone/تاریخ بشناسد، ولی چون
    وزن دقیقشان ساختگی نیست، در منحنی وزن-سن لنگر نمی‌شوند — یعنی نباید
    در فهرست لنگرهای وزنی (knots) ظاهر شوند."""
    assert B.day_hatch == 34
    assert B.day_swimup == 52
    assert B.day_hatch < B.day_swimup < B.day_05g
    knot_days = {d for d, _ in B.knots}
    assert B.day_hatch not in knot_days
    assert B.day_swimup not in knot_days


def test_growth_monotone_and_invertible():
    for w in (0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0, 25.0):
        assert abs(B.weight_at_age(B.age_at_weight(w)) - w) < 1e-6
    prev = -1
    for day in range(0, 200):
        w = B.weight_at_age(day)
        assert w > prev
        prev = w


def test_mortality_matches_observed_and_is_front_loaded():
    assert abs(B.cum_mortality(B.day_1g) - 0.28) < 1e-12
    assert abs(B.cum_mortality(B.day_2g) - 0.32) < 1e-12
    assert abs(B.cum_mortality(B.day_15g) - 0.40) < 1e-12
    assert B.cum_mortality(0) == 0.0
    # front-loaded: در نیمه اول مرحله تخم→۱g بیش از نصف تلفات آن مرحله رخ دهد
    assert B.cum_mortality(B.day_1g / 2) > 0.5 * 0.28


def test_survival_ratio_composes():
    r1 = B.survival_ratio(0, 50)
    r2 = B.survival_ratio(50, 120)
    assert abs(r1 * r2 - B.survival_ratio(0, 120)) < 1e-12


def test_capacity_anchors_and_monotonicity():
    assert abs(B.fish_per_pond(1.0) - 40000) < 1
    assert abs(B.fish_per_pond(2.0) - 27500) < 1
    assert abs(B.fish_per_pond(15.0) - 13500) < 1
    c5, c10 = B.fish_per_pond(5.0), B.fish_per_pond(10.0)
    assert 13500 < c10 < c5 < 27500
    assert not B.counts_toward_pond_capacity(0.9)
    assert B.counts_toward_pond_capacity(1.0)


def test_heterogeneity_quantiles():
    q10, q50, q90 = (B.weight_quantile(5.0, q) for q in (0.1, 0.5, 0.9))
    assert q10 < q50 < q90
    # میانگین توزیع لاگ‌نرمال باید همان mean_weight باشد
    import math
    cv = B.cv_at_weight(5.0)
    mu, sig = B._lognorm_params(5.0, cv)
    assert abs(math.exp(mu + sig * sig / 2) - 5.0) < 1e-9
    # CV با وزن زیاد می‌شود
    assert B.cv_at_weight(1.0) < B.cv_at_weight(15.0)
    # سهم بالای میانگین برای لاگ‌نرمال کمی زیر ۵۰٪ است
    f = B.fraction_above(5.0, 5.0)
    assert 0.4 < f < 0.5


def test_fraction_above_is_monotone():
    prev = 1.1
    for t in (1, 2, 5, 10, 15, 20):
        f = B.fraction_above(10.0, t)
        assert f <= prev + 1e-12
        prev = f


def test_feed_price_is_piecewise_not_flat():
    assert B.feed_price(0.8) == 310000
    assert B.feed_price(4.0) == 305000
    assert B.feed_price(12.0) == 199000
    assert B.feed_price(12.0) < B.feed_price(0.8)


def test_feed_cost_per_gram_uses_bands():
    c_small = B.feed_cost_per_gram_gain(0.5, 1.5)
    c_big = B.feed_cost_per_gram_gain(10.0, 15.0)
    assert abs(c_small - 310.0) < 1e-6      # 310,000/kg → 310 تومان/گرم با FCR=1
    assert abs(c_big - 199.0) < 1e-6


def test_feed_kg_charges_dead_fish_growth():
    a = B.feed_kg_for_growth(1000, 1.0, 2.0, n_died=0)
    b = B.feed_kg_for_growth(1000, 1.0, 2.0, n_died=200)
    assert abs(a - 1.0) < 1e-9              # 1000 fish × 1 g = 1 kg با FCR=1
    assert b > a


def test_sale_price_curve():
    assert B.sale_price(1.0) == 11000
    assert B.sale_price(2.0) == 11800
    assert B.sale_price(5.0) == 14200
    assert B.sale_price(10.0) == 18200
    assert B.sale_price(15.0) == 22200


def test_oxygen_is_diagnostic_only():
    o = B.oxygen_headroom(200.0)
    assert o["max_biomass_kg"] > 0
    assert 0 <= o["load_ratio"]


def test_speed_multiplier_shifts_curve():
    A2 = Assumptions()
    A2.defs["growth.speed_multiplier"]["value"] = 1.2
    B2 = Biology(A2)
    assert B2.weight_at_age(80) > B.weight_at_age(80)
    assert B2.age_at_weight(1.0) < 80


# ═══════════════════════ Feed gating at Swim-up (بخش ۴ درخواست)
def test_feed_zero_before_swimup():
    """قبل از Swim-up (کیسه زرده)، خوراک دستی باید دقیقاً صفر باشد."""
    fsw = B.feed_start_weight_g
    assert fsw > 0
    # کل بازهٔ رشد کاملاً قبل از وزن شروع تغذیه
    w_before = fsw * 0.5
    assert B.feed_kg_for_growth(1000, B.knots[0][1], w_before) == 0.0
    assert B.feed_cost_per_gram_gain(B.knots[0][1], w_before) == 0.0


def test_feed_positive_after_swimup_and_before_1g():
    """از Swim-up به بعد — حتی قبل از ۱ گرم (یعنی در بازهٔ ۰.۵ گرم) —
    خوراک باید محاسبه و مثبت باشد، نه صفر."""
    fsw = B.feed_start_weight_g
    assert fsw < 0.5, "برای معنادار بودن این تست، وزن شروع تغذیه باید زیر ۰.۵g باشد"
    kg = B.feed_kg_for_growth(1000, fsw, 0.5)
    assert kg > 0.0
    per_g = B.feed_cost_per_gram_gain(fsw, 0.5)
    assert per_g > 0.0


def test_feed_spanning_swimup_only_charges_post_swimup_growth():
    """اگر بازهٔ رشد از قبل از Swim-up تا بعد از آن کشیده شده باشد، فقط
    سهم بعد از Swim-up باید هزینه داشته باشد — یعنی محاسبه از یک وزنِ
    قبل از Swim-up دقیقاً همان نتیجهٔ محاسبه از خودِ وزن Swim-up را بدهد."""
    fsw = B.feed_start_weight_g
    w_before = fsw * 0.5
    full = B.feed_kg_for_growth(1000, w_before, 1.0)
    from_fsw = B.feed_kg_for_growth(1000, fsw, 1.0)
    assert full == pytest.approx(from_fsw, rel=1e-9)

    # نرخ «تومان به‌ازای هر گرم» وقتی در گین *کامل* (شامل بخش رایگان قبل
    # از Swim-up) ضرب شود، باید همان هزینهٔ کل صحیح را بدهد — نه بیشتر و
    # نه کمتر — یعنی مصرف‌کننده‌ای که این نرخ را در (w_to-w_from) اصلی
    # ضرب می‌کند (مثل core/offers.py)، دچار محاسبهٔ اشتباه نمی‌شود.
    rate_spanning = B.feed_cost_per_gram_gain(w_before, 1.0)
    rate_from_fsw = B.feed_cost_per_gram_gain(fsw, 1.0)
    cost_spanning = rate_spanning * (1.0 - w_before)
    cost_from_fsw = rate_from_fsw * (1.0 - fsw)
    assert cost_spanning == pytest.approx(cost_from_fsw, rel=1e-9)


# ═══════════════════════ تغییر Assumptions روی forecast اثر می‌گذارد (بخش ۵)
def test_changing_growth_assumption_shifts_forecast():
    """تغییر یکی از روزهای Growth Curve باید بلافاصله روی weight_at_age/
    age_at_weight اثر بگذارد — یعنی واقعاً forecast را عوض می‌کند، نه فقط
    UI را."""
    A2 = Assumptions()
    A2.defs["growth.day_5g"]["value"] = 110     # به‌جای ۱۲۶
    B2 = Biology(A2)
    assert B2.age_at_weight(5.0) == pytest.approx(110.0, abs=1e-6)
    assert B2.age_at_weight(5.0) != B.age_at_weight(5.0)
    # milestoneهای دیگر (که تغییر نکردند) باید دست‌نخورده بمانند
    assert B2.age_at_weight(1.0) == pytest.approx(B.age_at_weight(1.0), abs=1e-6)


def test_changing_feed_start_weight_shifts_feed_forecast():
    """تغییر feed.start_weight_g باید مستقیماً روی محاسبهٔ خوراک اثر بگذارد."""
    A2 = Assumptions()
    A2.defs["feed.start_weight_g"]["value"] = 0.4
    B2 = Biology(A2)
    kg_default = B.feed_kg_for_growth(1000, B.knots[0][1], 0.35)
    kg_shifted = B2.feed_kg_for_growth(1000, B2.knots[0][1], 0.35)
    assert kg_default > 0.0        # با آستانهٔ پیش‌فرض (۰.۱۵) قبلاً شروع شده
    assert kg_shifted == 0.0       # با آستانهٔ ۰.۴، هنوز به ۰.۳۵ هم نرسیده


# ═══════════════════════ Actual weight همچنان forecast را re-anchor می‌کند
def test_actual_weight_reanchors_forecast_with_new_curve():
    """مکانیزم re-anchor (استفاده‌شده در state.py هنگام ثبت weight_sample
    واقعی) باید با منحنی رشد جدید هم درست کار کند: اگر وزن واقعی از منحنی
    پایه جلوتر/عقب‌تر باشد، offset محاسبه‌شده باید forecastهای بعدی را
    درست جابه‌جا کند."""
    real_age = 100.0
    observed_w = 3.0                      # جلوتر از منحنی پایه در روز ۱۰۰
    implied_age = B.age_at_weight(observed_w)
    offset = implied_age - real_age
    assert offset > 0                     # چون در روز ۱۰۰ باید ~۲g باشد نه ۳g
    # forecast چند روز بعد باید از همان offset استفاده کند
    future_real_age = 120.0
    forecast_w = B.weight_at_age(future_real_age + offset)
    assert forecast_w > B.weight_at_age(future_real_age)  # جلوتر از منحنی پایه
