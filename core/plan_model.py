"""
plan_model.py — ساخت پروفایل‌های هفتگی برای برنامه‌ریزی (مرحله ۲)
====================================================================
هر «تصمیم ممکن» (یک lot تخم با اندازه و وزن برداشت مشخص، یا یک cohort
موجود که در وزن مشخصی برداشت شود) به یک مجموعه ضریب هفتگی تبدیل می‌شود:

    ponds[t] · feed_kg[t] · feed_cost[t] · revenue[t] · capital[t]

چون این ضرایب از پیش محاسبه می‌شوند، مدل بهینه‌سازی خطی می‌ماند و در عین
حال «استخر صحیح» (ceil) و «برداشت چندموجی» را حفظ می‌کند.

### برداشت چندموجی بر اساس پراکندگی رشد
یک cohort در یک روز به یک وزن نمی‌رسد. اگر وزن تک‌ماهی لاگ‌نرمال با میانگین
`mean(t)` باشد، ماهی در چندک q وزنی برابر `mean(t) × k_q` دارد. پس ماهیِ
تندرشد وقتی به وزن هدف wH می‌رسد که میانگین cohort هنوز `wH / k_90` باشد:

    سن رسیدن گروه تندرشد  = age_at_weight(wH / k_90)
    سن رسیدن گروه معمول   = age_at_weight(wH)
    سن رسیدن گروه کندرشد  = age_at_weight(wH / k_10)

به این ترتیب برداشت به‌صورت سه موج (grading / partial harvest) انجام می‌شود،
فشار استخر پله‌پله آزاد می‌شود و forecast واقع‌بینانه‌تر است.

### تقویم غیرچرخه‌ای (Fix 2)
همه شاخص‌های زمانی هفته‌های واقعی تقویمی از تاریخ مبنا هستند؛ هیچ modulo-52.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import jdatetime

from .state import _age


# ═══════════════════════════════════ Seasonality شمسی (اصلاح ۴)
def jalali_seasonality_factor(A, on_date) -> float:
    """
    ضریب فصلی سادهٔ Modelling Assumption بر اساس نیمهٔ سال شمسی: نیمهٔ
    اول (فروردین–شهریور) `h1_share` از عرضه/تقاضای سالانه را می‌گیرد،
    نیمهٔ دوم (مهر–اسفند) باقیمانده را. ضریب طوری نرمال شده که میانگین
    سالانه‌اش ۱ بماند — یعنی سقف‌های ماهانه/هفتگیِ فعلی (که میانگین سالانه
    را نمایندگی می‌کنند) فقط *بازتوزیع* می‌شوند، نه اینکه کلاً بزرگ‌تر/
    کوچک‌تر شوند.

    منبع واحد: هم سقف ماهانهٔ خرید تخم (`optimizer.py`) و هم سقف هفتگی
    تقاضای فروش (`optimizer.py` و `_simulate` در همین فایل) از همین یک
    تابع می‌خوانند تا واگرا نشوند — دقیقاً همان الگوی `saleability_band`.

    فقط در forecast/planning استفاده می‌شود؛ داده واقعی/observed
    (تراکنش‌های ثبت‌شده) هرگز از این ضریب عبور نمی‌کند.
    """
    if isinstance(on_date, str):
        on_date = date.fromisoformat(on_date[:10])
    h1_share = float(A.get("planning.seasonality_h1_share"))
    jm = jdatetime.date.fromgregorian(date=on_date).month
    return 2.0 * h1_share if jm <= 6 else 2.0 * (1.0 - h1_share)


# ═══════════════════════════════════ Market Saleability / Demand Liquidity
# منبع واحد این مفهوم — همه مصرف‌کننده‌ها (PlanModel، Offer Evaluator،
# What-If) از همین یک تابع می‌خوانند تا هرگز از هم واگرا نشوند.
def saleability_band(A, w: float) -> dict:
    """
    بازهٔ سهولت فروش بازار برای وزن `w`.

    سه عامل جدا نگه داشته می‌شوند (طبق درخواست):
      * `prob`            — احتمال فروش در همان تلاش اول، با قیمت کامل.
      * `retry_discount`  — تخفیفی که برای فروش در تلاش‌های بعدی (وقتی
        تلاش اول موفق نشود) لازم است — «ریسک/تخفیف ماهی کندفروش»، نه اینکه
        کل حجم یا تلاش اول با تخفیف حساب شود.
      * `expected_days_to_sale` — میانگین موزون روزهای انتظار تا فروش،
        محاسبه‌شده از `prob` و آهنگ تلاش مجدد (`planning.sale_delay_days` /
        `planning.max_sale_delay_periods`)؛ عدد مستقل و اختراعی نیست تا با
        شبیه‌سازی واقعی موج فروش (`sale_waves`) واگرا نشود.

    و یک چهارمی، برای اینکه فرض «همه‌چیز نهایتاً با retry فروخته می‌شود»
    اصلاح شود:
      * `max_demand_per_period` — حداکثر تعدادی که در یک دورهٔ
        `planning.demand_period_days` روزه (پیش‌فرض ۳۰ روز) واقعاً قابل
        فروش است، در Assumptions به‌صورت Modelling Assumption مشخص.
        `null`/خالی یعنی «داده کافی برای محدودکردن نداریم»، نه «نامحدود
        واقعی» — در این حالت مدل مثل قبل رفتار می‌کند.
      * `demand_cap_per_week` — همان عدد، تبدیل‌شده به سقف هفتگی برای
        استفادهٔ مستقیم شبیه‌سازی؛ `None` یعنی بدون سقف.

    مازاد بر این سقف در همان دوره فروخته نمی‌شود و به‌جای فروش خودکار در
    آخرین retry، به‌عنوان `Unsold Inventory / Carry-over` باقی می‌ماند
    (نگاه کنید به `PlanModel._simulate`).
    """
    bands = A.get("planning.saleability")
    row = None
    for b in bands:
        if float(b["w_min"]) <= w < float(b["w_max"]):
            row = b
            break
    if row is None:
        row = {"prob": 1.0, "retry_discount": 0.0, "label": "نامشخص",
               "w_min": w, "w_max": w}

    prob = max(0.0, min(1.0, float(row.get("prob", 1.0))))
    discount = max(0.0, min(0.9, float(row.get("retry_discount", 0.0))))

    delay = int(A.get("planning.sale_delay_days"))
    kmax = int(A.get("planning.max_sale_delay_periods"))
    if prob >= 0.999:
        exp_days = 0.0
    else:
        rem, exp_days = 1.0, 0.0
        for k in range(kmax):
            share = rem * prob
            exp_days += share * (k * delay)
            rem -= share
        exp_days += rem * (kmax * delay)      # باقیمانده در دور آخر فروخته می‌شود

    max_demand = row.get("max_demand_per_period")
    if max_demand in (None, "", 0, 0.0):
        max_demand, cap_week = None, None
    else:
        period_days = max(1.0, float(A.get("planning.demand_period_days")))
        max_demand = max(0.0, float(max_demand))
        cap_week = max_demand * 7.0 / period_days

    return {"prob": prob, "sale_probability": prob,
            "retry_discount": discount,
            "expected_days_to_sale": exp_days,
            "max_demand_per_period": max_demand,
            "demand_cap_per_week": cap_week,
            "label": row.get("label", ""),
            "w_min": float(row["w_min"]), "w_max": float(row["w_max"])}


class Grid:
    """شبکه زمانی هفتگی غیرچرخه‌ای."""

    def __init__(self, start: date, weeks: int):
        self.start = start
        self.weeks = weeks
        self.dates = [start + timedelta(days=7 * t) for t in range(weeks + 1)]

    def index_of(self, day: date) -> int:
        return max(0, min(self.weeks, (day - self.start).days // 7))

    def month_of(self, t: int) -> str:
        return self.dates[t].strftime("%Y-%m")

    def quarter_of(self, t: int) -> str:
        d0 = self.dates[t]
        return f"{d0.year}-Q{(d0.month - 1) // 3 + 1}"


class Scenario:
    """
    ضرایب سناریو. سناریوی نامساعد از پارامترهای stochastic در config ساخته
    می‌شود (Fix 5) — هیچ عددی در پایتون hard-code نیست.
    """

    def __init__(self, name: str, mortality_mult=1.0, fcr_mult=1.0,
                 price_mult=1.0, feed_price_mult=1.0, growth_mult=1.0):
        self.name = name
        self.mortality_mult = mortality_mult
        self.fcr_mult = fcr_mult
        self.price_mult = price_mult
        self.feed_price_mult = feed_price_mult
        self.growth_mult = growth_mult

    @staticmethod
    def base():
        return Scenario("base")

    @staticmethod
    def adverse(A, k: float = 1.0):
        """یک انحراف معیار در جهت نامساعد روی متغیرهای کلیدی."""
        return Scenario(
            "adverse",
            mortality_mult=1.0 + k * float(A.get("stochastic.mortality_cv")),
            fcr_mult=1.0 + k * float(A.get("stochastic.fcr_cv")),
            price_mult=1.0 - k * float(A.get("stochastic.sale_price_cv")),
            feed_price_mult=1.0 + k * float(A.get("stochastic.feed_price_cv")),
            growth_mult=1.0 - k * float(A.get("stochastic.growth_duration_cv")),
        )


class Profile:
    """ضرایب هفتگی یک تصمیم ممکن، برای یک سناریو."""

    __slots__ = ("key", "kind", "label", "month", "quantity", "harvest_w",
                 "purchase_date", "ponds", "feed_kg", "feed_cost", "revenue",
                 "egg_cost", "capital", "harvest_fish", "harvest_weeks",
                 "sold_fish", "survival_at_harvest", "first_week", "last_week",
                 "fish", "weight", "group", "mortality", "unsold_fish",
                 "planned_fish")

    def __init__(self, key, kind, label, weeks):
        self.key = key
        self.kind = kind                      # "new_lot" | "existing"
        self.label = label
        self.month = None
        self.quantity = 0.0
        self.harvest_w = 0.0
        self.purchase_date = None
        z = [0.0] * (weeks + 1)
        self.ponds = list(z)
        self.feed_kg = list(z)
        self.feed_cost = list(z)
        self.revenue = list(z)
        self.egg_cost = list(z)
        self.capital = list(z)
        self.harvest_fish = list(z)
        self.planned_fish = list(z)      # فقط سهم برداشت اختیاری optimizer (بدون فروش اجباری/firm)
        self.fish = list(z)              # تعداد زنده در پایان هفته
        self.mortality = list(z)         # تلفات هفته (جدا از برداشت)
        self.weight = list(z)            # وزن متوسط در هر هفته
        self.group = key                 # شناسه گروه فیزیکی (cohort یا lot)
        self.harvest_weeks = []
        self.sold_fish = 0.0
        self.survival_at_harvest = 0.0
        self.unsold_fish = 0.0            # Unsold Inventory / Carry-over باقی‌مانده تا افق مدل
        self.first_week = 0
        self.last_week = 0

    # ---------------------------------------------------------- aggregates
    def contribution(self) -> float:
        return sum(self.revenue) - sum(self.feed_cost) - sum(self.egg_cost)

    def peak_capital(self) -> float:
        return max(self.capital) if self.capital else 0.0


class PlanModel:
    """سازنده پروفایل‌ها از روی وضعیت واقعی مزرعه."""

    def __init__(self, A, bio, state, weeks: int | None = None):
        self.A, self.bio, self.state = A, bio, state
        self.weeks = int(weeks or A.get("planning.eval_horizon_weeks"))
        self.grid = Grid(state.as_of, self.weeks)
        self.wave_fracs = [float(x) for x in A.get("planning.harvest_wave_fractions")]
        self.slow_q = float(A.get("heterogeneity.slow_quantile"))
        self.fast_q = float(A.get("heterogeneity.fast_quantile"))

    # ------------------------------------------------------ heterogeneity
    def wave_ages(self, harvest_w: float, cv: float, growth_mult: float = 1.0) -> list:
        """
        سن رسیدن سه گروه (تندرشد، معمول، کندرشد) به وزن هدف.
        بازگشت: [(سن روز, سهم), ...] مرتب از زودترین به دیرترین.
        """
        bio = self.bio
        k_fast = bio.weight_quantile(1.0, self.fast_q, cv)     # نسبت چندک به میانگین
        k_slow = bio.weight_quantile(1.0, self.slow_q, cv)
        f_fast, f_typ, f_slow = self.wave_fracs
        targets = [(harvest_w / max(k_fast, 1e-6), f_fast),
                   (harvest_w, f_typ),
                   (harvest_w / max(k_slow, 1e-6), f_slow)]
        out = []
        for mean_w_needed, frac in targets:
            age = bio.age_at_weight(max(mean_w_needed, bio.knots[0][1] * 1.0001))
            if growth_mult != 1.0:
                age = age / max(growth_mult, 1e-6)    # رشد کندتر = دیرتر
            out.append((age, frac))
        return out

    # ------------------------------------------------- saleability (۵)
    def saleability(self, w: float) -> float:
        """احتمال یافتن مشتری در همان تلاش اول — Modelling Assumption."""
        return saleability_band(self.A, w)["prob"]

    def retry_discount(self, w: float) -> float:
        """تخفیف لازم برای فروش در تلاش‌های بعدی (وقتی تلاش اول موفق نشود)."""
        return saleability_band(self.A, w)["retry_discount"]

    def demand_cap_per_week(self, w: float):
        """سقف هفتگی قابل‌فروش («None» یعنی بدون سقف تعیین‌شده)."""
        return saleability_band(self.A, w)["demand_cap_per_week"]

    def sale_waves(self, harvest_w: float, cv: float, growth_mult: float = 1.0) -> list:
        """
        موج‌های فروش = پراکندگی رشد × احتمال یافتن مشتری.

        هر موج یک سه‌تایی `(سن, سهم, k)` است که `k` دفعهٔ تلاش را نشان
        می‌دهد (۰ = تلاش اول). خودِ این تابع فقط زمان‌بندی و سهم را می‌سازد؛
        قیمت هر موج جای دیگری (در `_simulate`) بر اساس `k` تعیین می‌شود:
        تلاش اول با قیمت کامل، تلاش‌های بعدی با `retry_discount` همان بازه
        وزنی (نه اینکه کل احتمال در قیمت ضرب شود).

        اگر احتمال فروش p باشد، سهم فروش در دوره k برابر (1-p)^k · p است و
        باقیمانده پس از آخرین دوره در همان دوره فروخته می‌شود.
        """
        p = self.saleability(harvest_w)
        delay = int(self.A.get("planning.sale_delay_days"))
        kmax = int(self.A.get("planning.max_sale_delay_periods"))
        out = []
        for age, frac in self.wave_ages(harvest_w, cv, growth_mult):
            if p >= 0.999:
                out.append((age, frac, 0))
                continue
            rem = 1.0
            for k in range(kmax):
                share = rem * p
                if share > 1e-9:
                    out.append((age + k * delay, frac * share, k))
                rem -= share
            if rem > 1e-9:
                out.append((age + kmax * delay, frac * rem, kmax))
        out.sort(key=lambda x: x[0])
        return out

    # ------------------------------------------------ شبیه‌سازی یک cohort
    def _simulate(self, p, t0: int, anchor, base_count: float, sc: Scenario,
                  harvest_w: float, waves: list, weight_fn, survive_fn,
                  unit_cost: float, forced_sales: list | None = None):
        """
        هسته مشترک شبیه‌سازی، با گذار حالت صریح:

            موجودی ابتدای هفته
            − تلفات
            − برداشت/فروش
            = موجودی پایان هفته

        خوراک فقط برای ماهی زنده همان هفته محاسبه می‌شود؛ ماهی فروخته‌شده
        در هفته‌های بعد نه خوراک می‌خورد و نه دوباره به‌عنوان تلفات شمرده
        می‌شود. سرمایه هم به نسبت ماهی فروخته‌شده آزاد می‌گردد.
        """
        bio, g = self.bio, self.grid
        wave_at: dict[int, list] = {}
        for age, frac, k in waves:
            wk = g.index_of(anchor(age))
            wave_at.setdefault(wk, []).append((frac, k))
        # نکته حیاتی: `retry_disc`/`demand_cap_week` اینجا فقط برای تصمیم
        # اولیهٔ «آیا اصلاً افق شبیه‌سازی باید برای carry-over کش بیاید»
        # از بازهٔ *وزن هدف* استفاده می‌کنند (پایین‌تر). مقدار واقعی تخفیف
        # و سقف هفتگیِ هر فروش، داخل حلقه و از روی *وزن واقعی همان هفته*
        # دوباره محاسبه می‌شود (نگاه کنید به نکته زیر داخل حلقه) — چون در
        # موج‌های تأخیری/carry-over ماهی واقعاً رشد می‌کند و ممکن است به
        # بازهٔ وزنی دیگری برسد؛ استفاده از تخفیف/سقفِ بازهٔ هدف برای
        # فروشی که واقعاً در بازهٔ سنگین‌تری اتفاق می‌افتد، ارزش آن بازهٔ
        # سنگین‌تر را به‌اشتباه با قواعد بازهٔ سبک‌تر حساب می‌کرد.
        retry_disc = self.retry_discount(harvest_w)
        demand_cap_week = self.demand_cap_per_week(harvest_w)
        forced_at: dict[int, list] = {}
        for e in (forced_sales or []):
            days = max(0, (e["date"] - g.start).days)
            wk = min(self.weeks, (days + 6) // 7)  # هرگز قبل از delivery اعمال نشود
            forced_at.setdefault(wk, []).append(e)
        p.harvest_weeks = sorted(set(wave_at) | set(forced_at))
        natural_last = min(self.weeks, max(p.harvest_weeks) if p.harvest_weeks else t0)
        if demand_cap_week is not None:
            # مازاد بر سقف تقاضا نباید بعد از پایان دوره از مدل ناپدید شود:
            # تا فروش یا پایان کل افق برنامه (self.weeks) در شبیه‌سازی
            # می‌ماند — همچنان استخر اشغال می‌کند و تلفات می‌گیرد (حلقهٔ زیر
            # طبیعتاً این کار را می‌کند چون `alive` کم نمی‌شود). وزن برای این
            # هفته‌های اضافه ثابت می‌ماند (زیر) تا برون‌یابی رشد غیرواقعی
            # نشود.
            p.last_week = self.weeks
        else:
            p.last_week = natural_last

        alive = base_count                 # موجودی ابتدای دوره
        remaining_frac = 1.0               # سهم برداشت‌نشده از cohort اولیه
        capital_pool = base_count * unit_cost
        prev_w = weight_fn(t0)
        carry_qty = 0.0                    # Unsold Inventory / Carry-over —
        # مقداری که موج فروش خواسته ولی سقف تقاضای همان هفته اجازه نداده؛
        # به‌جای فروش خودکار در آخرین retry، همچنان زنده می‌ماند (در
        # `alive` باقی است، خوراک/تلفات/استخر طبیعی خودش را می‌گیرد) و در
        # هفته‌های بعد دوباره امتحان می‌شود، تا سقف هفتگیِ همان بازه.

        for t in range(t0, min(self.weeks, p.last_week) + 1):
            # در هفته‌های اضافهٔ carry-over (فراتر از آخرین موج طبیعی)، وزن
            # روی مقدار همان آخرین هفتهٔ طبیعی ثابت می‌ماند — منحنی رشد
            # برای این بازهٔ اضافه هرگز validate نشده و ادامه‌دادنش باعث
            # برون‌یابی غیرواقعی وزن/خوراک می‌شود. ماهیِ فروش‌نرفته در
            # واقعیت هم عملاً به یک وزن سقف نزدیک می‌شود، نه اینکه بی‌نهایت
            # رشد کند.
            w = weight_fn(t if t <= natural_last else natural_last)

            # ۱) تلفات هفته — فقط روی ماهی زنده
            if t > t0 and alive > 0:
                sr = survive_fn(t - 1, t)
                sr = max(0.0, 1.0 - (1.0 - sr) * sc.mortality_mult)
                died = alive * (1.0 - sr)
                alive -= died
                p.mortality[t] = died

            # ۲) خوراک — برای ماهی‌ای که این هفته زنده بوده و رشد کرده
            if t > t0 and alive > 0:
                kg = bio.feed_kg_for_growth(alive, prev_w, w,
                                            n_died=p.mortality[t]) * sc.fcr_mult
                p.feed_kg[t] = kg
                cost = kg * bio.feed_price(prev_w) * sc.feed_price_mult
                p.feed_cost[t] = cost
                capital_pool += cost

            # ۳) فروش زمان‌دار اجباری — تا این هفته ماهی کاملاً در cohort مانده است.
            for ev in forced_at.get(t, []):
                if alive <= 0:
                    break
                n = min(float(ev["quantity"]), alive)
                before = alive
                p.revenue[t] += n * float(ev["price"]) * sc.price_mult
                p.harvest_fish[t] += n
                p.sold_fish += n
                alive -= n
                if before > 0:
                    capital_pool *= max(0.0, 1.0 - n / before)

            # ۴) برداشت/فروش اختیاری optimizer. ماهی‌های متعهد به deliveryهای
            # آینده قابل برداشت زودتر نیستند.
            #
            # هر (frac, k) یک زیرسهم از همین هفته است: k=۰ یعنی تلاش اول
            # (قیمت کامل — احتمال هرگز مستقیم در قیمت ضرب نمی‌شود)، k≥۱
            # یعنی این ماهی در تلاش اول فروش نرفته و برای واقعاً جابه‌جا شدن
            # به یک تخفیف (retry_discount همان بازهٔ وزنی) نیاز دارد. این
            # همان «possible discount required for slow-moving weights» است؛
            # بدون آن، وزن بالاتر با احتمال فروش پایین در تابع هدف اسمی
            # تقریباً بی‌هزینه به‌نظر می‌رسید چون فقط دیرتر (نه ارزان‌تر)
            # فروخته می‌شد.
            #
            # سقف تقاضا (در صورت تعریف‌شدن): هر چیزی که این هفته به‌خاطر
            # سقف نتواند بفروشد، force نمی‌شود — به carry_qty اضافه می‌شود
            # و هفتهٔ بعد دوباره امتحان می‌شود (با تخفیف retry، چون خودش
            # نوعی فروش تأخیری است).
            entries = wave_at.get(t, [])
            sale_w = max(harvest_w, w)
            # بازهٔ *وزن واقعیِ همین هفته* — نه وزن هدف — تخفیف/سقف را
            # تعیین می‌کند. اگر ماهی در انتظار فروش رشد کرده و از بازهٔ
            # هدف عبور کرده، از این به بعد باید طبق قواعد بازهٔ سنگین‌تر
            # (سقف تقاضای کمتر، تخفیف retry بیشتر) فروخته شود — دقیقاً
            # همان بازه‌ای که محدودیت سراسری MILP هم برای این هفته از
            # روی همین وزن واقعی تشخیص می‌دهد (نگاه کنید optimizer.py،
            # «سقف تقاضای بازار» که از `weight[t]` استفاده می‌کند، نه
            # وزن هدف).
            band_now = saleability_band(self.A, sale_w)
            retry_disc_now = band_now["retry_discount"]
            cap_now = band_now["demand_cap_per_week"]
            if cap_now is None:
                cap = math.inf
            else:
                # اصلاح ۴ — همان Seasonality شمسیِ optimizer.py، تا سقف
                # هفتگیِ محلیِ این کاندید با محدودیت سراسری MILP واگرا نشود.
                cap = cap_now * jalali_seasonality_factor(self.A, g.dates[t])
            if (entries or carry_qty > 1e-9) and alive > 0 and cap > 1e-9:
                reserved = sum(float(ev["quantity"])
                               for wk, evs in forced_at.items() if wk > t
                               for ev in evs)
                max_optional = max(0.0, alive - reserved)
                budget = min(max_optional, cap)
                price_full = bio.sale_price(sale_w, on_date=g.dates[t].isoformat())

                if carry_qty > 1e-9 and budget > 1e-9:
                    n = min(carry_qty, budget, max_optional)
                    if n > 1e-9:
                        before = alive
                        p.revenue[t] += n * price_full * (1.0 - retry_disc_now) * sc.price_mult
                        p.harvest_fish[t] += n
                        p.planned_fish[t] += n
                        p.sold_fish += n
                        alive -= n
                        if before > 0:
                            capital_pool *= max(0.0, 1.0 - n / before)
                        carry_qty -= n
                        budget -= n
                        max_optional = max(0.0, max_optional - n)

                if remaining_frac > 1e-9:
                    for frac, k in entries:
                        if remaining_frac <= 1e-9:
                            break
                        desired_share = min(1.0, frac / remaining_frac)
                        desired_n = alive * desired_share
                        if desired_n <= 1e-9:
                            continue
                        n = min(desired_n, budget, max_optional)
                        before = alive
                        if n > 1e-9:
                            price = price_full * (1.0 - retry_disc_now) if k >= 1 else price_full
                            p.revenue[t] += n * price * sc.price_mult
                            p.harvest_fish[t] += n
                            p.planned_fish[t] += n
                            p.sold_fish += n
                            alive -= n
                            if before > 0:
                                capital_pool *= max(0.0, 1.0 - n / before)
                            budget -= n
                            max_optional = max(0.0, max_optional - n)
                        shortfall = desired_n - n
                        if shortfall > 1e-9:
                            carry_qty += shortfall     # Unsold Inventory / Carry-over
                        remaining_frac = max(0.0, remaining_frac * (1.0 - desired_share))

            # ۵) ثبت وضعیت پایان هفته
            p.fish[t] = alive
            p.weight[t] = w
            if alive >= 1 and bio.counts_toward_pond_capacity(w):
                p.ponds[t] = math.ceil(alive / bio.fish_per_pond(w))
            p.capital[t] = capital_pool if alive >= 1 else 0.0
            prev_w = w

        p.survival_at_harvest = p.sold_fish / base_count if base_count else 0.0
        # هرچه در پایان بازهٔ شبیه‌سازی هنوز زنده و فروخته‌نشده مانده —
        # شامل carry_qty که سقف تقاضا مهلت فروشش را نداد — همان
        # Unsold Inventory است؛ در تابع هدف با صفر لحاظ می‌شود (چون اصلاً
        # وارد revenue نشده)، فقط برای هشدار/گزارش نگه داشته می‌شود.
        p.unsold_fish = max(0.0, alive) if demand_cap_week is not None else 0.0
        return p

    # -------------------------------------------------------- new-lot plan
    def new_lot_profile(self, purchase_date: date, qty: float, harvest_w: float,
                        sc: Scenario) -> Profile:
        A, bio, g = self.A, self.bio, self.grid
        key = f"L|{purchase_date.isoformat()}|{int(qty)}|{harvest_w:g}"
        p = Profile(key, "new_lot",
                    f"{int(qty):,} تخم در {purchase_date.isoformat()} → برداشت {harvest_w:g}g",
                    self.weeks)
        p.month = purchase_date.strftime("%Y-%m")
        p.group = f"L|{purchase_date.isoformat()}|{int(qty)}"
        p.quantity = qty
        p.harvest_w = harvest_w
        p.purchase_date = purchase_date

        egg_price = float(A.get_at("egg.base_price", purchase_date.isoformat()))
        t0 = g.index_of(purchase_date)
        p.first_week = t0
        p.egg_cost[t0] = qty * egg_price

        cv = bio.cv_at_weight(harvest_w)
        waves = self.sale_waves(harvest_w, cv, sc.growth_mult)

        def age_of(t):
            a = (g.dates[t] - purchase_date).days
            return a * sc.growth_mult if sc.growth_mult != 1.0 else a

        return self._simulate(
            p, t0,
            anchor=lambda age: purchase_date + timedelta(days=int(round(age))),
            base_count=qty, sc=sc, harvest_w=harvest_w, waves=waves,
            weight_fn=lambda t: bio.weight_at_age(max(0.0, age_of(t))),
            survive_fn=lambda a, b: bio.survival_ratio(
                max(0.0, (g.dates[a] - purchase_date).days),
                max(0.0, (g.dates[b] - purchase_date).days)),
            unit_cost=egg_price)

    # ------------------------------------------------- existing cohort plan
    def existing_profile(self, c, harvest_w: float, sc: Scenario) -> Profile:
        """
        پروفایل یک cohort موجود اگر «کل» آن در وزن harvest_w برداشت شود.
        بهینه‌ساز می‌تواند کسری از cohort را به هر وزن اختصاص دهد
        (partial harvest / grading) چون این پروفایل‌ها خطی ترکیب می‌شوند.
        """
        A, bio, st, g = self.A, self.bio, self.state, self.grid
        key = f"C|{c.cohort_id}|{harvest_w:g}"
        p = Profile(key, "existing",
                    f"{c.cohort_id} → برداشت {harvest_w:g}g", self.weeks)
        p.quantity = c.alive
        p.group = f"C|{c.cohort_id}"
        p.harvest_w = harvest_w
        p.purchase_date = c.purchase_date
        p.first_week = 0

        cv = st.cv_of(c, max(harvest_w, c.mean_weight))
        waves = self.sale_waves(harvest_w, cv, sc.growth_mult)

        def anchor(age):
            day = c.purchase_date + timedelta(
                days=int(round(age - c.growth_offset_days)))
            return max(day, st.as_of)

        def weight_fn(t):
            if sc.growth_mult != 1.0:
                eff = _age(c.purchase_date, g.dates[t]) + c.growth_offset_days
                return bio.weight_at_age(eff * sc.growth_mult)
            return st.weight_of(c, g.dates[t])

        def survive_fn(a, b):
            a0 = _age(c.purchase_date, g.dates[a])
            a1 = _age(c.purchase_date, g.dates[b])
            return bio.survival_ratio(a0, a1)

        unit_cost = ((c.egg_count * c.egg_price / c.alive) if c.alive else 0.0)
        forced_sales = []
        for ev in getattr(st, "hypothetical_events", []):
            if ev.get("type") != "scheduled_sale":
                continue
            for a in ev.get("allocations") or []:
                if a.get("cohort_id") == c.cohort_id:
                    forced_sales.append({"date": date.fromisoformat(str(ev["date"])[:10]),
                                         "quantity": float(a["quantity"]),
                                         "price": float(ev.get("price") or 0),
                                         "weight_g": ev.get("weight_g")})
        return self._simulate(p, 0, anchor, c.alive, sc, harvest_w, waves,
                              weight_fn, survive_fn, unit_cost, forced_sales=forced_sales)

    # ---------------------------------------------------- candidate builder
    def decision_months(self, n: int | None = None) -> list:
        n = int(n or self.A.get("planning.decision_months"))
        day = int(self.A.get("planning.purchase_day_of_month"))
        out = []
        y, m = self.state.as_of.year, self.state.as_of.month
        for k in range(n):
            mm = m + k
            yy = y + (mm - 1) // 12
            mm = (mm - 1) % 12 + 1
            dt = date(yy, mm, day)
            if dt < self.state.as_of:
                dt = self.state.as_of + timedelta(days=1)
            out.append((f"{yy}-{mm:02d}", dt))
        return out

    def build_candidates(self, sc: Scenario) -> dict:
        """همه پروفایل‌های ممکن: lotهای جدید + cohortهای موجود."""
        lots = [float(x) for x in self.A.get("planning.lot_candidates")]
        hws = [float(x) for x in self.A.get("planning.harvest_weights")]
        new_lots, existing = {}, {}
        for (mkey, pdate) in self.decision_months():
            for q in lots:
                for w in hws:
                    pr = self.new_lot_profile(pdate, q, w, sc)
                    pr.month = mkey
                    new_lots[pr.key] = pr
        for c in self.state.cohorts.values():
            if c.alive < 1:
                continue
            for w in hws:
                if w < c.mean_weight - 1e-9:
                    continue          # از این وزن گذشته است
                existing[f"C|{c.cohort_id}|{w:g}"] = self.existing_profile(c, w, sc)
            # همیشه گزینه «فروش فوری در وزن فعلی» موجود باشد
            wnow = round(max(c.mean_weight, 1.0), 1)
            k = f"C|{c.cohort_id}|{wnow:g}"
            if k not in existing:
                existing[k] = self.existing_profile(c, wnow, sc)
        return {"new_lots": new_lots, "existing": existing}
