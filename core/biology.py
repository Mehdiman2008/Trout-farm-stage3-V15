"""
biology.py — منحنی‌های زیستی و اقتصادی مزرعه
================================================
هیچ عددی اینجا hard-code نیست؛ همه از Assumptions می‌آید (Fix 5).

منطق کلیدی:
  * منحنی رشد: درون‌یابی log-linear بین milestoneهای وزنی (Modelling
    Assumption، از Growth Curve مرجع — ستون Base):
        روز 0                     → start_weight_g   (MA)
        روز growth.day_05g        → 0.5 g             (MA)
        روز growth.day_1g         → 1 g               (MA)
        روز growth.day_2g         → 2 g               (MA)
        روز growth.day_5g         → 5 g               (MA)
        روز growth.day_10g        → 10 g              (MA)
        روز growth.day_15g        → 15 g              (MA)
    مراحل Hatch و Swim-up (growth.day_hatch / growth.day_swimup) هم در
    مدل شناخته می‌شوند، ولی چون وزن دقیقشان معلوم نیست، در منحنی وزن-سن
    به‌عنوان لنگر استفاده نمی‌شوند — فقط برای milestone/تاریخ تخمینی و
    برای گیت خوراک (پایین) به کار می‌روند.

  * خوراک: قبل از Swim-up (کیسه زرده) خوراک دستی صفر است. از Swim-up به
    بعد — حتی قبل از ۱ گرم — خوراک با همان جدول قیمت/FCR فعلی محاسبه
    می‌شود. چون وزن دقیق Swim-up ساختگی نیست، گیت با یک assumption مستقل
    (`feed.start_weight_g`) اعمال می‌شود، نه از منحنی رشد.

  * تلفات: cumulative mortality در milestoneها OBS است.
    داخل مرحله تخم→1g با توان front_load_exponent (<1) توزیع می‌شود
    تا تلفات front-loaded باشد؛ بین بقیه milestoneها خطی در زمان.

  * ناهمگنی رشد (growth heterogeneity):
    وزن تک‌ماهی در یک cohort ~ Lognormal با میانگین = mean_weight
    و CV که به‌صورت خطی از cv_at_1g تا cv_at_15g با سن زیاد می‌شود.
"""
from __future__ import annotations

import math
from bisect import bisect_right

# ---------------------------------------------------------------- helpers
_SQRT2 = math.sqrt(2.0)


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / _SQRT2))


def _norm_ppf(p: float) -> float:
    """Acklam inverse normal CDF — دقت کافی برای کاربرد داشبورد."""
    if p <= 0.0:
        return -8.0
    if p >= 1.0:
        return 8.0
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    plow, phigh = 0.02425, 1 - 0.02425
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


MILESTONE_WEIGHTS = [1.0, 2.0, 5.0, 10.0, 15.0]


class Biology:
    """موتور محاسبات زیستی؛ با یک شیء Assumptions ساخته می‌شود."""

    def __init__(self, A, on_date=None):
        """on_date: تاریخی که پارامترهای effective-dated (قیمت‌ها) با آن خوانده
        می‌شوند. تغییر قیمت هرگز retroactive نیست (اصلاح ۶)."""
        self.A = A
        self.on_date = on_date
        self._build()

    def _p(self, key):
        """مقدار یک پارامتر در تاریخ مرجع این نمونه."""
        return self.A.get_at(key, self.on_date)

    # ------------------------------------------------------------ build
    def _build(self):
        A = self.A
        w0 = float(A.get("growth.start_weight_g"))
        d_hatch = int(A.get("growth.day_hatch"))
        d_swimup = int(A.get("growth.day_swimup"))
        d05 = int(A.get("growth.day_05g"))
        d1 = int(A.get("growth.day_1g"))
        d2 = int(A.get("growth.day_2g"))
        d5 = int(A.get("growth.day_5g"))
        d10 = int(A.get("growth.day_10g"))
        d15 = int(A.get("growth.day_15g"))

        # anchor knots (day, weight) — log-linear between knots، دقیقاً همان
        # روش قبلی. Hatch و Swim-up عمداً اینجا نیستند: وزن دقیقی برایشان
        # ساختگی گذاشته نشده (نگاه کنید day_hatch/day_swimup پایین‌تر و
        # feed.start_weight_g برای محاسبهٔ خوراک).
        self.knots = [(0.0, w0), (float(d05), 0.5), (float(d1), 1.0),
                      (float(d2), 2.0), (float(d5), 5.0),
                      (float(d10), 10.0), (float(d15), 15.0)]
        self._kd = [k[0] for k in self.knots]
        self._klw = [math.log(k[1]) for k in self.knots]
        self.day_hatch, self.day_swimup = d_hatch, d_swimup
        self.day_05g, self.day_1g, self.day_2g = d05, d1, d2
        self.day_5g, self.day_10g, self.day_15g = d5, d10, d15
        self.sgr_after = float(A.get("growth.sgr_after_15g_per_day"))
        self.speed = float(A.get("growth.speed_multiplier")) or 1.0
        self.feed_start_weight_g = float(A.get("feed.start_weight_g"))

        # mortality knots: (day, cumulative mortality) — لنگرهای وزنی
        # همان‌هایی هستند که قبلاً بودند (۱،۲،۵،۱۰،۱۵ گرم)؛ فقط روزشان
        # اکنون از جدول رشد جدید می‌آید، نه از درون‌یابی مشتق‌شده.
        self.mort_knots = [
            (0.0, 0.0),
            (float(d1), float(A.get("mortality.cum_at_1g"))),
            (float(d2), float(A.get("mortality.cum_at_2g"))),
            (float(d5), float(A.get("mortality.cum_at_5g"))),
            (float(d10), float(A.get("mortality.cum_at_10g"))),
            (float(d15), float(A.get("mortality.cum_at_15g"))),
        ]
        self.front_exp = float(A.get("mortality.front_load_exponent"))
        self.daily_after_15 = float(A.get("mortality.daily_after_15g"))

        # pond capacity knots: (weight, fish/pond) — log-log interpolation
        self.cap_knots = [
            (1.0, float(A.get("capacity.fish_per_pond_1g"))),
            (2.0, float(A.get("capacity.fish_per_pond_2g"))),
            (15.0, float(A.get("capacity.fish_per_pond_15g"))),
        ]
        self.no_constraint_below = float(A.get("capacity.no_constraint_below_g"))

        # feed price bands
        self.feed_bands = sorted(
            [dict(r) for r in self._p("feed.price_table")], key=lambda r: float(r["w_min"]))
        self.price_base_1g = float(self._p("price.base_1g"))
        self.price_slope = float(self._p("price.slope_per_gram"))

        self.cv1 = float(A.get("heterogeneity.cv_at_1g"))
        self.cv15 = float(A.get("heterogeneity.cv_at_15g"))
        self.dist = str(A.get("heterogeneity.distribution"))

    # ----------------------------------------------------------- growth
    def weight_at_age(self, day: float, speed: float | None = None) -> float:
        """وزن متوسط cohort در سن مشخص (روز از تاریخ خرید تخم)."""
        s = self.speed if speed is None else speed
        d = max(0.0, day) * s
        if d >= self._kd[-1]:
            extra = d - self._kd[-1]
            return math.exp(self._klw[-1] + self.sgr_after * extra)
        i = bisect_right(self._kd, d) - 1
        i = max(0, min(i, len(self._kd) - 2))
        d0, d1 = self._kd[i], self._kd[i + 1]
        l0, l1 = self._klw[i], self._klw[i + 1]
        f = 0.0 if d1 == d0 else (d - d0) / (d1 - d0)
        return math.exp(l0 + f * (l1 - l0))

    def age_at_weight(self, w: float, speed: float | None = None,
                      _raw: bool = False) -> float:
        """سن (روز) که cohort به وزن متوسط w می‌رسد."""
        w = max(w, 1e-9)
        lw = math.log(w)
        if lw <= self._klw[0]:
            out = 0.0
        elif lw >= self._klw[-1]:
            out = self._kd[-1] + (lw - self._klw[-1]) / self.sgr_after
        else:
            i = bisect_right(self._klw, lw) - 1
            i = max(0, min(i, len(self._klw) - 2))
            l0, l1 = self._klw[i], self._klw[i + 1]
            d0, d1 = self._kd[i], self._kd[i + 1]
            f = 0.0 if l1 == l0 else (lw - l0) / (l1 - l0)
            out = d0 + f * (d1 - d0)
        if _raw:
            return out
        s = self.speed if speed is None else speed
        return out / (s or 1.0)

    # -------------------------------------------------------- mortality
    def cum_mortality(self, day: float) -> float:
        """تلفات تجمعی مدل‌شده تا سن day."""
        s = self.speed or 1.0
        d = max(0.0, day) * s
        k = self.mort_knots
        if d >= k[-1][0]:
            extra = d - k[-1][0]
            surv = (1 - k[-1][1]) * ((1 - self.daily_after_15) ** extra)
            return 1 - surv
        if d <= k[0][0]:
            return 0.0
        for i in range(len(k) - 1):
            d0, m0 = k[i]
            d1, m1 = k[i + 1]
            if d0 <= d <= d1:
                if d1 == d0:
                    return m1
                f = (d - d0) / (d1 - d0)
                if i == 0:
                    # مرحله تخم → 1g : front-loaded با توان < 1
                    f = f ** self.front_exp
                return m0 + f * (m1 - m0)
        return k[-1][1]

    def survival(self, day: float) -> float:
        return max(1e-9, 1.0 - self.cum_mortality(day))

    def survival_ratio(self, day_from: float, day_to: float) -> float:
        """نسبت بقا بین دو سن — برای پیش‌بردن یک anchor واقعی."""
        if day_to <= day_from:
            return 1.0
        return min(1.0, self.survival(day_to) / max(1e-9, self.survival(day_from)))

    # ---------------------------------------------- growth heterogeneity
    def cv_at_weight(self, mean_w: float) -> float:
        """CV وزن داخل cohort؛ خطی بین 1g و 15g، بیرون بازه clamp می‌شود."""
        lo, hi = 1.0, 15.0
        if mean_w <= lo:
            return self.cv1
        if mean_w >= hi:
            return self.cv15
        f = (math.log(mean_w) - math.log(lo)) / (math.log(hi) - math.log(lo))
        return self.cv1 + f * (self.cv15 - self.cv1)

    def _lognorm_params(self, mean_w: float, cv: float | None = None):
        cv = self.cv_at_weight(mean_w) if cv is None else cv
        cv = max(1e-6, cv)
        sigma = math.sqrt(math.log(1.0 + cv * cv))
        mu = math.log(max(mean_w, 1e-9)) - 0.5 * sigma * sigma
        return mu, sigma

    def weight_quantile(self, mean_w: float, q: float, cv: float | None = None) -> float:
        """وزن در چندک q — P10 / P50 / P90 برای slow / typical / fast."""
        if mean_w <= 0:
            return 0.0
        cvv = self.cv_at_weight(mean_w) if cv is None else cv
        if self.dist == "normal_truncated":
            w = mean_w * (1.0 + cvv * _norm_ppf(q))
            return max(0.05 * mean_w, w)
        mu, sigma = self._lognorm_params(mean_w, cvv)
        return math.exp(mu + sigma * _norm_ppf(q))

    def fraction_above(self, mean_w: float, threshold: float,
                       cv: float | None = None) -> float:
        """سهمی از cohort که وزنش از threshold بیشتر است."""
        if mean_w <= 0:
            return 0.0
        cvv = self.cv_at_weight(mean_w) if cv is None else cv
        if self.dist == "normal_truncated":
            z = (threshold - mean_w) / max(1e-9, cvv * mean_w)
            return 1.0 - _norm_cdf(z)
        mu, sigma = self._lognorm_params(mean_w, cvv)
        z = (math.log(max(threshold, 1e-9)) - mu) / sigma
        return 1.0 - _norm_cdf(z)

    def weight_bands(self, mean_w: float, cv: float | None = None) -> dict:
        """سه گروه قابل فهم: کندرشد / معمول / تندرشد."""
        A = self.A
        ql = float(A.get("heterogeneity.slow_quantile"))
        qh = float(A.get("heterogeneity.fast_quantile"))
        return {
            "cv": round(self.cv_at_weight(mean_w) if cv is None else cv, 4),
            "slow": self.weight_quantile(mean_w, ql, cv),
            "typical": self.weight_quantile(mean_w, 0.5, cv),
            "fast": self.weight_quantile(mean_w, qh, cv),
            "slow_q": ql, "fast_q": qh,
        }

    # --------------------------------------------------------- capacity
    def fish_per_pond(self, mean_w: float) -> float:
        """ظرفیت تجربی استخر (قطعه) در وزن مشخص — درون‌یابی log-log."""
        k = self.cap_knots
        w = max(mean_w, 1e-6)
        if w <= k[0][0]:
            return k[0][1]
        if w >= k[-1][0]:
            w0, c0 = k[-2]
            w1, c1 = k[-1]
            a = (math.log(c1) - math.log(c0)) / (math.log(w1) - math.log(w0))
            return math.exp(math.log(c1) + a * (math.log(w) - math.log(w1)))
        for i in range(len(k) - 1):
            w0, c0 = k[i]
            w1, c1 = k[i + 1]
            if w0 <= w <= w1:
                f = (math.log(w) - math.log(w0)) / (math.log(w1) - math.log(w0))
                return math.exp(math.log(c0) + f * (math.log(c1) - math.log(c0)))
        return k[-1][1]

    def counts_toward_pond_capacity(self, mean_w: float) -> bool:
        return mean_w >= self.no_constraint_below

    def ponds_required(self, count: float, mean_w: float) -> float:
        if count <= 0:
            return 0.0
        if not self.counts_toward_pond_capacity(mean_w):
            return 0.0
        return count / max(1.0, self.fish_per_pond(mean_w))

    # ---------------------------------------------------- oxygen check
    def oxygen_headroom(self, biomass_kg: float) -> dict:
        """diagnostic فقط؛ constraint اصلی همان ظرفیت تجربی است."""
        A = self.A
        flow_l_s = float(A.get("water.flow_l_s"))
        do_in = float(A.get("water.do_inlet_mg_l"))
        do_min = float(A.get("water.min_safe_do_outlet_mg_l"))
        spec = float(A.get("water.o2_consumption_mg_per_kg_h"))
        # mg O2 available per hour = flow(L/s)*3600*(DO_in - DO_min) mg/L
        avail_mg_h = flow_l_s * 3600.0 * max(0.0, do_in - do_min)
        used_mg_h = biomass_kg * spec
        return {
            "available_mg_per_h": avail_mg_h,
            "used_mg_per_h": used_mg_h,
            "load_ratio": (used_mg_h / avail_mg_h) if avail_mg_h > 0 else 0.0,
            "max_biomass_kg": avail_mg_h / spec if spec > 0 else 0.0,
        }

    # ------------------------------------------------------------- feed
    def feed_band(self, mean_w: float) -> dict:
        for b in self.feed_bands:
            if float(b["w_min"]) <= mean_w < float(b["w_max"]):
                return b
        return self.feed_bands[-1] if mean_w >= float(self.feed_bands[-1]["w_max"]) \
            else self.feed_bands[0]

    def feed_price(self, mean_w: float) -> float:
        return float(self.feed_band(mean_w)["price"])

    def feed_name(self, mean_w: float) -> str:
        return str(self.feed_band(mean_w)["name"])

    def feed_cost_per_gram_gain(self, w_from: float, w_to: float) -> float:
        """
        هزینه خوراک (تومان) برای هر گرم رشد — piecewise by weight (Fix)،
        به‌علاوهٔ گیت Swim-up: بخشی از رشد که قبل از وزن شروع تغذیه
        (`feed.start_weight_g`) اتفاق می‌افتد، هزینه‌اش صفر است. مخرج
        عمداً همان بازهٔ کامل درخواستی (w_to-w_from) می‌ماند، نه فقط
        بخش بعد از Swim-up — تا اگر صدازننده این نرخ را در گین اصلی
        (w_to-w_from) ضرب کند، همان هزینهٔ کل درست به دست بیاید.
        """
        if w_to <= w_from:
            return 0.0
        fcr = float(self.A.get("feed.fcr"))
        eff_from = max(w_from, self.feed_start_weight_g)
        total_cost = 0.0
        if eff_from < w_to:
            w = eff_from
            for b in self.feed_bands:
                lo, hi, pr = float(b["w_min"]), float(b["w_max"]), float(b["price"])
                seg_lo, seg_hi = max(w, lo), min(w_to, hi)
                if seg_hi > seg_lo:
                    total_cost += (seg_hi - seg_lo) * fcr * pr / 1000.0   # g → kg
            if w_to > float(self.feed_bands[-1]["w_max"]):
                gain = w_to - max(eff_from, float(self.feed_bands[-1]["w_max"]))
                if gain > 0:
                    total_cost += gain * fcr * float(self.feed_bands[-1]["price"]) / 1000.0
        total_gain = w_to - w_from
        return total_cost / total_gain if total_gain > 0 else 0.0

    def feed_kg_for_growth(self, n_survivors: float, w_from: float, w_to: float,
                           n_died: float = 0.0) -> float:
        """
        خوراک لازم (kg) برای رشد یک گروه از w_from به w_to.
        اگر charge_growth_of_dead_fish روشن باشد، رشد نیمه‌راه تلف‌شده‌ها هم حساب می‌شود.

        گیت Swim-up: قبل از رسیدن به `feed.start_weight_g`، ماهی از کیسه
        زرده تغذیه می‌کند و خوراک دستی صفر است — چون وزن دقیق Hatch/Swim-up
        ساختگی نیست، این گیت با یک وزن مستقل (نه از منحنی رشد) اعمال می‌شود.
        """
        if w_to <= w_from:
            return 0.0
        eff_from = max(w_from, self.feed_start_weight_g)
        if eff_from >= w_to:
            return 0.0          # کاملاً قبل از Swim-up
        fcr = float(self.A.get("feed.fcr"))
        gain_g = n_survivors * (w_to - eff_from)
        if bool(self.A.get("feed.charge_growth_of_dead_fish")) and n_died > 0:
            gain_g += n_died * 0.5 * (w_to - eff_from)
        return gain_g * fcr / 1000.0

    # ------------------------------------------------------------ price
    def sale_price(self, mean_w: float, on_date=None) -> float:
        """قیمت پایه فروش. توجه: این منحنی baseline است و از فروش‌های تاریخی
        تخفیف‌دار ساخته نمی‌شود (اصلاح ۱۰)."""
        if on_date is not None:
            base = float(self.A.get_at("price.base_1g", on_date))
            slope = float(self.A.get_at("price.slope_per_gram", on_date))
        else:
            base, slope = self.price_base_1g, self.price_slope
        # بالاتر از سقف داده قیمت، برون‌یابی نمی‌کنیم
        w = min(mean_w, float(self.A.get("price.max_priced_weight_g")))
        return max(0.0, base + slope * (w - 1.0))

    def feed_price_at(self, mean_w: float, on_date) -> float:
        """قیمت خوراک در یک تاریخ مشخص (برای تخمین هزینه گذشته)."""
        table = sorted([dict(r) for r in self.A.get_at("feed.price_table", on_date)],
                       key=lambda r: float(r["w_min"]))
        for b in table:
            if float(b["w_min"]) <= mean_w < float(b["w_max"]):
                return float(b["price"])
        return float(table[-1]["price"] if mean_w >= float(table[-1]["w_max"])
                     else table[0]["price"])

    # -------------------------------------------------------- summary
    def milestone_table(self) -> list:
        """جدول milestoneها برای نمایش در UI — همه از Growth Curve مرجع (MA)."""
        out = []
        for w in MILESTONE_WEIGHTS:
            day = self.age_at_weight(w)
            out.append({
                "weight_g": w,
                "day": round(day, 1),
                "cum_mortality": round(self.cum_mortality(day), 4),
                "survival": round(self.survival(day), 4),
                "fish_per_pond": round(self.fish_per_pond(w)),
                "sale_price": round(self.sale_price(w)),
                "feed_name": self.feed_name(w),
                "feed_price": self.feed_price(w),
                "source": "MA",
            })
        return out
