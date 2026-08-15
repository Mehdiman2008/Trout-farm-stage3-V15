"""
manual_mix.py — Manual Sales Mix Comparison (افزونه مستقل، Rolling Plan)
=========================================================================
یک قابلیت کوچک و کاملاً جدا: کاربر تعداد فروش دلخواه در وزن‌های
۱/۲/۵/۱۰/۱۵ گرم را وارد می‌کند و همان ترکیب — از همان جمعیت زندهٔ فعلی
مزرعه به‌علاوهٔ همان lotهای جدیدی که برنامهٔ بهینه برای خرید انتخاب کرده —
دقیقاً با همان موتور شبیه‌سازی (`PlanModel._simulate`، رشد/تلفات/هزینه
خوراک/سهولت فروش/سقف تقاضا) و همان مسیر تجمیع/گزارش‌دهی (`Plan._aggregate`،
`PlannedCashLedger`) ارزش‌گذاری می‌شود.

معماری اصلی `Plan` و `core/optimizer.py` **هیچ تغییری نکردند**. این ماژول
فقط یک زیرکلاس از `Plan` است (`ManualPlan`) که به‌جای فراخوانی
`optimizer.solve()`، یک `PlanSolution` دستی می‌سازد — سپس دقیقاً همان
تابع `_aggregate()` و همان `PlannedCashLedger` بدون تغییر فراخوانی
می‌شوند. یعنی هر تغییری که در آینده روی موتور شبیه‌سازی/گزارش‌دهی اعمال
شود، خودکار روی این قابلیت هم اثر می‌کند — بدون کد تکراری.

محدودهٔ صریح (Modelling Assumption): ترکیب دستی فقط نحوهٔ فروشِ جمعیت
موجود (cohortهای فعلی + همان lotهای جدیدی که برنامهٔ بهینه‌شده برای خرید
انتخاب کرده) را بازتوزیع می‌کند؛ خودِ تصمیم خرید تخم (چه زمانی، چقدر) از
برنامهٔ بهینه گرفته می‌شود و در سناریوی دستی تغییر نمی‌کند — تا مقایسه،
مقایسهٔ «همان جمعیت، دو ترکیب وزن فروش» باشد، نه دو برنامهٔ خرید متفاوت.
cohortهایی که از تمام ۵ وزن عبور کرده‌اند (فراتر از منحنی قیمت، مثل یک
cohort ۴۶ گرمی) در هر دو سناریو عیناً در همان وزن فعلی خودشان فروخته
می‌شوند — این بخش اصلاً از تصمیم دستی متأثر نیست.

محدودیت شناخته‌شده: اگر cohortی هنوز به هیچ‌کدام از ۵ وزن رسمی نرسیده
باشد (مثلاً میانگین وزن فعلی‌اش ۱.۴۶g است، بین ۱g و ۲g)، تنها گزینهٔ
واقعی‌اش «فروش فوری در همان وزن فعلی» است که یکی از ۵ ورودی رسمی نیست.
چنین cohortی در سناریوی دستی باید از میان ۵ وزن رسمی (نزدیک‌ترینِ
دست‌یافتنی) انتخاب شود — یعنی نتیجهٔ دستی و بهینه، برای همین بخش کوچک
از جمعیت، دقیقاً یکی نمی‌شود؛ فاصله معمولاً چند درصد است، نه بیشتر.

واحد ورودی/هدف همه‌جا «قطعهٔ واقعاً فروخته‌شده» است (`Profile.sold_fish`)،
نه جمعیت خام پیش از تلفات (`Profile.quantity`). نسخهٔ اول این ماژول از
جمعیت خام استفاده می‌کرد؛ چون کاربر «چند قطعه بفروشیم» می‌خواند و می‌نویسد،
آن انتخاب باعث می‌شد مقدار پیش‌فرض دکمهٔ «بازنشانی به ترکیب بهینه» به‌طور
محسوسی (روی داده واقعی، حدود ۲۵٪ تا ۴۰٪) از «فروش برنامه» واقعی بزرگ‌تر
باشد — خودِ محاسبهٔ مقایسه‌ای درست بود (چون هر دو طرف با همان واحد کار
می‌کردند)، ولی عدد نمایش‌داده‌شده گمراه‌کننده بود. رفع شد: LP تخصیص
(`_allocate`) عرضهٔ هر (گروه، وزن) را با نرخ فروش همان کاندید
(`sold_fish/quantity`) مقیاس می‌کند، و `optimal_target_mix` هم از
`sold_fish` می‌خواند — سقف Σf≤1 هنوز روی جمعیت خام است (نمی‌توان بیش از
۱۰۰٪ یک گروه را تخصیص داد)، فقط «هدف» با واحد درست سنجیده می‌شود.
"""
from __future__ import annotations

from collections import defaultdict

import pulp

from .optimizer import PlanSolution
from .plan_cash import PlannedCashLedger
from .plan_model import jalali_seasonality_factor, saleability_band
from .planner import Plan

MANUAL_WEIGHTS = (1.0, 2.0, 5.0, 10.0, 15.0)


def _allocate(groups: dict, targets: dict, value_density: dict,
              sold_rate: dict) -> tuple[dict, dict]:
    """
    یک LP کوچک و کاملاً جدا (نه Optimizer اصلی): با ثابت نگه‌داشتن جمعیت
    هر گروه (cohort موجود یا lot جدیدِ از قبل انتخاب‌شده)، فراکسیون هر
    (گروه، وزن) را طوری تعیین می‌کند که مجموع هر وزن تا حد ممکن به هدف
    درخواستی کاربر نزدیک باشد.

    چون معمولاً چند ترکیب فراکسیونی متفاوت می‌توانند به یک مجموع یکسان
    برسند (مسئله انحطاطی)، یک جملهٔ ثانویهٔ کوچک اضافه شده: در میان
    راه‌حل‌های هم‌ارز از نظر پوشش هدف، آن‌هایی که جمعیت را از میان
    گروه‌های *باارزش‌تر* (`contribution` به‌ازای هر قطعه، از همان Profile
    شبیه‌سازی‌شده) عبور می‌دهند ترجیح داده می‌شوند — دقیقاً همان انتخابی
    که یک تصمیم‌گیرندهٔ منطقی (یا Optimizer اصلی، اگر آزاد بود) می‌کرد.
    ضریب این جمله عمداً خیلی کوچک است تا هرگز روی پوشش هدف اثر نگذارد؛
    فقط بین راه‌حل‌های هم‌ارز تصمیم می‌گیرد.

    نکتهٔ حیاتی: هدف کاربر «چند قطعه در این وزن بفروشیم» است — یعنی
    شمارش پس از تلفات، نه جمعیت خام تخصیص‌یافته. عرضهٔ هر (گروه، وزن)
    با `sold_rate` (نسبت قطعهٔ فروخته‌شده به جمعیت همان کاندید، از همان
    Profile شبیه‌سازی‌شده) مقیاس می‌شود. سقف Σf≤1 همچنان روی *جمعیت
    خام* می‌ماند — یک گروه، صرف‌نظر از تلفات، هرگز نمی‌تواند بیش از
    ۱۰۰٪ جمعیت خودش تخصیص یابد.

    groups: {group_id: {"alive": float, "valid_weights": [w, ...]}}
    targets: {w: qty}   (فقط باقیماندهٔ هدف پس از کسر سهم اجباری/forced،
        به تعداد قطعهٔ *فروخته‌شده*)
    value_density: {(group_id, w): contribution به‌ازای هر قطعه جمعیت}
    sold_rate: {(group_id, w): نسبت قطعهٔ فروخته‌شده به جمعیت خام}

    برمی‌گرداند: (fractions: {(group_id, w): frac}, diagnostics)
    """
    weights = sorted(targets)
    prob = pulp.LpProblem("manual_sales_mix", pulp.LpMinimize)
    f: dict = {}
    for gid, info in groups.items():
        for w in info["valid_weights"]:
            if w in targets:
                f[(gid, w)] = pulp.LpVariable(f"f_{gid}_{w:g}", 0, 1)
    short = {w: pulp.LpVariable(f"short_{w:g}", 0) for w in weights}
    over = {w: pulp.LpVariable(f"over_{w:g}", 0) for w in weights}

    for w in weights:
        supply = pulp.lpSum(
            sold_rate.get((gid, w), 1.0) * groups[gid]["alive"] * f[(gid, w)]
            for gid in groups if (gid, w) in f)
        prob += supply + short[w] - over[w] == targets[w]

    for gid, info in groups.items():
        relevant = [f[(gid, w)] for w in info["valid_weights"] if (gid, w) in f]
        if relevant:
            prob += pulp.lpSum(relevant) <= 1.0

    max_v = max((abs(v) for v in value_density.values()), default=1.0) or 1.0
    tie_break = pulp.lpSum(
        (value_density.get((gid, w), 0.0) / max_v) * groups[gid]["alive"] * f[(gid, w)]
        for (gid, w) in f)
    # اولویت اول کمینه‌کردن کمبود/مازاد (شدنی‌بودن) است؛ جملهٔ ارزش فقط
    # زیر سایهٔ آن، برای شکستن انحطاط، اثر می‌کند.
    prob += 1000 * pulp.lpSum(short.values()) + pulp.lpSum(over.values()) - 1e-4 * tie_break
    prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=15))

    fractions = {k: (v.value() or 0.0) for k, v in f.items()}
    diagnostics = {
        "status": pulp.LpStatus[prob.status],
        "shortfall": {w: (short[w].value() or 0.0) for w in weights},
        "overshoot": {w: (over[w].value() or 0.0) for w in weights},
    }
    return fractions, diagnostics


class ManualPlan(Plan):
    """
    `Plan` که به‌جای حل MILP اصلی، ترکیب دستی کاربر را به‌عنوان راه‌حل
    اعمال می‌کند. تمام مسیر تجمیع/گزارش‌دهی از کلاس پایه، بدون تغییر،
    استفاده می‌شود (`_aggregate`، `PlannedCashLedger`، `_classify_pond_breach`،
    `summary`، `plan_status`، `sales_by_weight`، …).
    """

    def __init__(self, A, bio, state, targets: dict, baseline_plan: Plan,
                 variant_name: str = "balanced"):
        self.manual_targets = {float(w): float(q) for w, q in targets.items() if q}
        self._baseline_chosen_lots = list(baseline_plan.solution.chosen_lots)
        self.manual_warnings: list[str] = []
        self.manual_feasible = True
        self.manual_diagnostics: dict = {}
        super().__init__(A, bio, state, variant_name)

    # ---------------------------------------------------- بای‌پس MILP
    def _solve_with_repair(self):
        op = int(self.A.get("farm.operational_ponds"))
        self.solution = self._build_manual_solution()
        self.repair_rounds = 0
        self.repair_log = []
        self._aggregate()
        self.cash = PlannedCashLedger(self, self.extra_cash_rows)
        self._classify_pond_breach(op)
        self.wc_feasible = not self.cash.metrics()["wc_breach"]
        self.wc_scale_used = 1.0
        self._check_demand_capacity()
        if not self.pond_feasible:
            self.manual_feasible = False
            self.manual_warnings.append(
                f"Pond capacity breach: اوج نیاز استخر {max(self.ponds):.0f} از "
                f"{op} عملیاتی بیشتر است.")
        if not self.wc_feasible:
            self.manual_feasible = False
            need = self.cash.metrics()["peak_funding_requirement"]
            self.manual_warnings.append(
                f"Working Capital shortfall: اوج نیاز نقدی {need:,.0f} تومان از "
                f"سرمایه موجود {self.wc_available:,.0f} تومان بیشتر است.")

    def _value_density(self, eligible: dict) -> dict:
        """حاشیهٔ اسمی هر (گروه، وزن) به‌ازای هر قطعه — از همان Profile
        شبیه‌سازی‌شده، بدون هیچ محاسبهٔ موازی."""
        out = {}
        for gid, info in eligible.items():
            for w in info["valid_weights"]:
                key = f"{gid}|{w:g}"
                pool = self.cand_base["new_lots"] if gid.startswith("L|") \
                    else self.cand_base["existing"]
                p = pool.get(key)
                if p and p.quantity:
                    out[(gid, w)] = p.contribution() / p.quantity
        return out

    def _sold_rate(self, eligible: dict) -> dict:
        """
        نسبت قطعهٔ *واقعاً فروخته‌شده* به جمعیت خام هر (گروه، وزن) — از
        همان Profile شبیه‌سازی‌شده (`p.sold_fish / p.quantity`).

        بدون این، هدف کاربر با جمعیت خامِ پیش از تلفات مقایسه می‌شد، نه
        با تعداد قطعهٔ واقعاً فروخته‌شده — یعنی وقتی کاربر دقیقاً همان
        ترکیب برنامهٔ بهینه را می‌خواست بازتولید کند، ورودی‌های
        پیش‌فرض («بازنشانی به ترکیب بهینه») عددی به‌مراتب بزرگ‌تر از
        «فروش برنامه» واقعی نشان می‌داد (مجموع ~۲٫۸ میلیون در برابر فروش
        واقعی ~۲٫۰ میلیون) — گمراه‌کننده، هرچند خودِ محاسبهٔ مقایسه‌ای
        (چون از همان جمعیت خام استفاده می‌کرد) درست بود.
        """
        out = {}
        for gid, info in eligible.items():
            for w in info["valid_weights"]:
                key = f"{gid}|{w:g}"
                pool = self.cand_base["new_lots"] if gid.startswith("L|") \
                    else self.cand_base["existing"]
                p = pool.get(key)
                if p and p.quantity:
                    out[(gid, w)] = p.sold_fish / p.quantity
        return out

    def _build_manual_solution(self) -> PlanSolution:
        sol = PlanSolution()
        sol.solver, sol.status = "manual_mix", "manual"
        targets = dict(self.manual_targets)
        req_weights = set(targets)

        # ------------------------------------------------------- گروه‌ها
        groups: dict[str, dict] = {}
        for c in self.state.cohorts.values():
            if c.alive < 1:
                continue
            gid = f"C|{c.cohort_id}"
            valid = [w for w in req_weights
                     if f"{gid}|{w:g}" in self.cand_base["existing"]]
            groups[gid] = {"alive": c.alive, "valid_weights": valid,
                          "kind": "existing", "cohort": c}

        lot_groups: dict[str, float] = {}
        for lot in self._baseline_chosen_lots:
            gid = f"L|{lot['purchase_date']}|{int(lot['quantity'])}"
            lot_groups[gid] = lot_groups.get(gid, 0.0) + lot["quantity"]
        for gid, qty in lot_groups.items():
            valid = [w for w in req_weights
                     if f"{gid}|{w:g}" in self.cand_base["new_lots"]]
            groups[gid] = {"alive": qty, "valid_weights": valid, "kind": "new"}

        eligible = {g: info for g, info in groups.items() if info["valid_weights"]}
        forced = {g: info for g, info in groups.items() if not info["valid_weights"]}

        # سهم اجباری (cohort/لاتی که از هر ۵ وزن عبور کرده) از هدف کاربر
        # کسر می‌شود تا LP فقط روی بخش واقعاً قابل‌تصمیم حل کند — وگرنه
        # وقتی کاربر دقیقاً همان مجموع برنامهٔ بهینه را وارد می‌کند، سهم
        # اجباری دوبار شمرده می‌شود. اینجا هم واحد باید «قطعهٔ فروخته‌شده»
        # باشد، نه جمعیت خام — برای cohortهای اجباری این دو تقریباً یکی
        # است (فروش فوری، بدون فرصت زیاد برای تلفات بیشتر) ولی برای دقت،
        # از همان Profile واقعی (`sold_fish`) خوانده می‌شود، نه `alive`.
        forced_bucket = defaultdict(float)
        for gid, info in forced.items():
            forced_bucket[self._actual_weight_bucket(
                _fallback_weight(info))] += _forced_sold_fish(self, gid, info)
        lp_targets = {}
        for w in req_weights:
            remaining = max(0.0, targets[w] - forced_bucket.get(w, 0.0))
            if remaining > 1e-9:
                lp_targets[w] = remaining

        sold_rate = self._sold_rate(eligible)
        # سقف بالای «چند قطعه واقعاً قابل فروش است» — برای هر گروه، بهترین
        # نرخ فروش در میان وزن‌های معتبرش (نه جمعیت خام، که همیشه بزرگ‌تر
        # از فروش واقعی است و چک کمبود را بی‌اثر می‌کرد).
        total_eligible_sold = sum(
            info["alive"] * max((sold_rate.get((gid, w), 1.0)
                                 for w in info["valid_weights"]), default=0.0)
            for gid, info in eligible.items())
        total_target = sum(lp_targets.values())
        if total_target > total_eligible_sold + 1e-6:
            self.manual_feasible = False
            self.manual_warnings.append(
                f"ماهی کافی وجود ندارد: مجموع درخواستی {sum(targets.values()):,.0f} "
                f"قطعه، ولی حداکثر حدود "
                f"{total_eligible_sold + sum(forced_bucket.values()):,.0f} قطعه "
                f"در مجموع cohortها/لات‌های موجود قابل‌فروش است.")

        fractions, diag = ({}, {"shortfall": {}, "overshoot": {}}) \
            if not (eligible and lp_targets) else \
            _allocate(eligible, lp_targets, self._value_density(eligible), sold_rate)
        self.manual_diagnostics = diag

        for w in sorted(lp_targets):
            short = diag["shortfall"].get(w, 0.0)
            if short > max(1.0, 0.005 * lp_targets[w]):
                self.manual_feasible = False
                self.manual_warnings.append(
                    f"برای وزن {w:g}g، {short:,.0f} قطعه کمتر از هدف قابل‌تأمین "
                    f"است — cohort/لات قابل‌دسترس برای این وزن به این اندازه "
                    f"کافی نیست (Demand/Population shortfall).")

        for (gid, w), frac in fractions.items():
            if frac <= 1e-9:
                continue
            key = f"{gid}|{w:g}"
            sol.selected[key] = sol.selected.get(key, 0.0) + frac
            if groups[gid]["kind"] == "existing":
                cid = gid.split("|", 1)[1]
                sol.cohort_split.setdefault(cid, {})[w] = frac

        for gid, info in forced.items():
            if info["kind"] != "existing":
                continue        # لات جدید همیشه حداقل یک وزن معتبر دارد
            c = info["cohort"]
            wnow = round(max(c.mean_weight, 1.0), 1)
            key = f"{gid}|{wnow:g}"
            if key in self.cand_base["existing"]:
                sol.selected[key] = 1.0

        sol.chosen_lots = list(self._baseline_chosen_lots)
        sol.notes = list(self.manual_warnings)
        return sol

    # -------------------------------------------- سقف تقاضای مشترک هفتگی
    def _check_demand_capacity(self):
        """
        هر Profile به‌تنهایی طوری شبیه‌سازی شده که گویی تنها رقیب همان
        بازه/هفته است (دقیقاً مثل هر کاندید دیگری در `PlanModel`). اینجا
        پس از انتخاب، حجم *ترکیبیِ* همهٔ cohort/لات‌های انتخاب‌شده را در
        هر (بازه، هفته) جمع می‌زنیم و با سقف واقعی همان بازه مقایسه
        می‌کنیم — دقیقاً همان بررسی‌ای که محدودیت سراسری Optimizer اصلی
        (`core/optimizer.py`) برای یک حل MILP انجام می‌دهد؛ اینجا فقط
        برای یک ترکیب ثابتِ دستی، به‌صورت پس‌رویدادی و بدون حل دوباره.

        یک نکتهٔ ظریف که باید عیناً از Optimizer اصلی تکرار شود: cohortی
        که فقط **یک** کاندید وزن دارد (فراتر از منحنی قیمت رفته، هیچ
        اختیاری برای تصمیم باقی نمانده) در محدودیت سراسری MILP هم معاف
        است — چون فروشش قبلاً زیستی تعیین شده، نه یک انتخاب برنامه‌ریزی.
        بدون این معافیت، حتی خودِ برنامهٔ بهینه‌شدهٔ واقعی هم اینجا به‌غلط
        «نقض سقف تقاضا» نشان می‌داد.
        """
        by_cohort: dict[str, int] = defaultdict(int)
        for k in self.cand_base["existing"]:
            by_cohort[k.split("|")[1]] += 1
        weekly = defaultdict(float)
        caps = {}
        for p, wgt in self._chosen("all"):
            if p.kind == "existing" and by_cohort.get(p.key.split("|")[1], 0) <= 1:
                continue    # فروش اجباری/بدون‌اختیار — دقیقاً مثل Optimizer اصلی معاف است
            for t, n in enumerate(p.harvest_fish):
                if n <= 0:
                    continue
                band = saleability_band(self.A, p.weight[t])
                cap = band["demand_cap_per_week"]
                if cap is None:
                    continue
                # اصلاح ۴ — همان Seasonality شمسیِ optimizer.py/_simulate،
                # وگرنه این بررسیِ پس‌رویدادی در نیمهٔ دوم سال شمسی
                # به‌غلط «نقض سقف» نشان می‌دهد.
                cap = cap * jalali_seasonality_factor(self.A, self.grid.dates[t])
                key = (t, band["label"])
                weekly[key] += n * wgt
                caps[key] = cap
        breaches = [(t, label, tot, caps[(t, label)])
                    for (t, label), tot in weekly.items()
                    if tot > caps[(t, label)] + 1e-6]
        if breaches:
            self.manual_feasible = False
            t0, label, tot, cap = max(breaches, key=lambda b: b[2] - b[3])
            when = self.grid.dates[t0].isoformat()
            self.manual_warnings.append(
                f"Demand Capacity کافی نیست: در هفتهٔ {when}، بازهٔ «{label}» "
                f"{tot:,.0f} قطعه هم‌زمان می‌خواهد بفروشد، ولی سقف واقعی همان "
                f"هفته {cap:,.0f} قطعه است ({len(breaches)} هفته با این مشکل).")


def _fallback_weight(info: dict) -> float:
    if info["kind"] == "existing":
        return round(max(info["cohort"].mean_weight, 1.0), 1)
    return 0.0    # نباید رخ دهد؛ لات جدید همیشه حداقل یک وزن معتبر دارد


def _forced_sold_fish(plan: Plan, gid: str, info: dict) -> float:
    """قطعهٔ *واقعاً فروخته‌شده*ٔ یک cohort اجباری (فراتر از منحنی
    قیمت)، از همان Profile — نه جمعیت خام. برای فروش فوری این دو معمولاً
    خیلی نزدیک‌اند، ولی برای یکدست ماندن واحدها با بقیهٔ محاسبه لازم است."""
    if info["kind"] != "existing":
        return info["alive"]
    wnow = round(max(info["cohort"].mean_weight, 1.0), 1)
    p = plan.cand_base["existing"].get(f"{gid}|{wnow:g}")
    return p.sold_fish if p else info["alive"]


def optimal_target_mix(baseline_plan: Plan) -> dict:
    """
    ترکیب «هدف‌گذاری‌شده» برنامهٔ بهینه به تفکیک وزنِ *انتخاب‌شدهٔ همان
    کاندید* (`Profile.harvest_w`) — نه وزن *واقعیِ نهاییِ فروش* (که به‌خاطر
    carry-over می‌تواند به بازهٔ سنگین‌تری سرریز کند، همان چیزی که
    `sales_by_weight` گزارش می‌دهد).

    چرا این دو یکی نیستند و چرا اینجا لازم است: اگر دکمهٔ «بازنشانی به
    ترکیب بهینه» ورودی‌ها را از `sales_by_weight` (گزارش پس از سرریز) پر
    کند، همان لحظه که کاربر بدون تغییر «محاسبه» را بزند، عدد به‌دست‌آمده
    به‌خاطر همان پویایی سرریز، با برنامهٔ بهینه فاصلهٔ چشمگیر می‌گیرد —
    نه به‌خاطر خطا، بلکه چون معیار درست برای بازتولید یک ترکیب، «به کدام
    وزن هدف‌گذاری شده» است، نه «نهایتاً کجا فروخته شده». این تابع همان
    سطح ورودی را برمی‌گرداند که `ManualPlan._allocate` هم با آن کار
    می‌کند، تا دکمهٔ بازنشانی و محاسبه با هم سازگار بمانند.

    نکتهٔ دوم: مقدار برگشتی «قطعهٔ فروخته‌شده» است (`p.sold_fish`)، نه
    جمعیت خام تخصیص‌یافته (`p.quantity`، پیش از تلفات). قبلاً از
    `p.quantity` استفاده می‌شد که باعث می‌شد جمع ورودی‌های پیش‌فرض به‌طور
    محسوسی (روی داده واقعی، حدود ۲۵٪ تا ۴۰٪) بزرگ‌تر از «فروش برنامه»
    واقعی نشان داده شود — نه یک باگ محاسباتی (چون هر دو طرف مقایسه با
    همان واحد جمعیت خام کار می‌کردند)، ولی به‌شدت گمراه‌کننده برای
    کاربر، چون عنوان ورودی‌ها «چند قطعه بفروشیم» است، نه «چند قطعه
    تخصیص دهیم».
    """
    out: dict = {}
    for k, wgt in baseline_plan.solution.selected.items():
        pool = baseline_plan.cand_base["new_lots"] if k.startswith("L|") \
            else baseline_plan.cand_base["existing"]
        p = pool.get(k)
        if not p or p.harvest_w not in MANUAL_WEIGHTS:
            continue
        out[p.harvest_w] = out.get(p.harvest_w, 0.0) + p.sold_fish * wgt
    return out


def compare_to_optimised(A, bio, state, targets: dict, baseline_plan: Plan,
                         variant_name: str = "balanced") -> dict:
    """خروجی نهایی مقایسه‌ای برای API/UI."""
    mp = ManualPlan(A, bio, state, targets, baseline_plan, variant_name)
    ms, bs = mp.summary(), baseline_plan.summary()

    def pick(s):
        return {
            "revenue_total": s["revenue_total"],
            "rolling_12m": s["rolling_12m"]["contribution_nominal"],
            "full_lifecycle": s["full_lifecycle"]["contribution_nominal"],
            "contribution_risk_adjusted": s["contribution_risk_adjusted"],
            "peak_ponds": s["peak_ponds"],
            "peak_funding_requirement": s["peak_funding_requirement"],
        }

    m, o = pick(ms), pick(bs)
    return {
        "manual": m, "optimised": o,
        "difference": {k: m[k] - o[k] for k in m},
        "feasible": mp.manual_feasible,
        "warnings": mp.manual_warnings,
        "manual_targets": mp.manual_targets,
        "manual_sales_by_weight": ms["sales_by_weight"],
        "optimised_sales_by_weight": bs["sales_by_weight"],
        "pond_breach": ms["pond_breach"],
        "wc_breach": ms["wc_breach"],
        "operational_ponds": ms["operational_ponds"],
        "wc_available": ms["wc_available"],
    }
