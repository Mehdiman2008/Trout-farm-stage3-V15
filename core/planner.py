"""
planner.py — Rolling Plan و اهداف مدیریتی (مرحله ۲)
=====================================================
برنامه سال را می‌بیند، ولی خروجی مدیریتی آن ماهانه، سه‌ماهه و ۹۰ روز آینده است.

جریان کار:
    وضعیت واقعی → پروفایل‌ها → MILP → تجمیع ماهانه/سه‌ماهه → دفتر نقدی → KPI

هیچ چیز از یک baseline ثابت شروع نمی‌شود؛ همه‌چیز از Live Farm State همین
لحظه ساخته می‌شود. بنابراین با ورود هر داده واقعی، برنامه دوباره ساخته
می‌شود (rolling re-optimisation).
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from .attribution import coverage
from .optimizer import SolverTimeout, Variant, solve
from .plan_cash import PlannedCashLedger
from .plan_model import PlanModel, Scenario, saleability_band


class Plan:
    def __init__(self, A, bio, state, variant_name: str = "balanced",
                 wc_available: float | None = None,
                 extra_lot: dict | None = None,
                 extra_lots: list | None = None,
                 extra_cash_rows: list | None = None):
        """
        extra_lot / extra_lots: lotهایی که حتماً باید در برنامه گنجانده شوند
        (`{"date":..., "quantity":..., "price":...}`). برای ارزیابی آفر
        واقعی خرید تخم و سناریوهای What-If استفاده می‌شود.

        extra_cash_rows: جریان‌های نقدی بیرون از برنامه — وجه یک آفر فروش
        فرضی، یا جابه‌جایی زمان پرداخت تخم طبق شرایط تأمین‌کننده.
        """
        self.A, self.bio, self.state = A, bio, state
        self.db = state.db
        lots = list(extra_lots or [])
        if extra_lot:
            lots.insert(0, extra_lot)
        self.extra_lot = extra_lot
        self.extra_lots = lots
        self.extra_cash_rows = list(extra_cash_rows or [])
        self.force_group = None
        self.variant = Variant(variant_name, A)
        self.model = PlanModel(A, bio, state)
        self.grid = self.model.grid
        self.wc_available = float(
            wc_available if wc_available is not None
            else A.get("finance.working_capital_available"))
        self.cand_base = self.model.build_candidates(Scenario.base())
        self.cand_adv = self.model.build_candidates(Scenario.adverse(A))
        if lots:
            groups = [self._inject_lot(l) for l in lots]
            self.force_group = groups[0] if len(groups) == 1 else groups
        self.repair_rounds = 0
        self.repair_log = []
        self._solve_with_repair()
        self.reconciliation = coverage(self.db, state)

    def _inject_lot(self, lot: dict) -> str:
        """افزودن یک lot مشخص (آفر) به مجموعه گزینه‌ها و اجباری کردن آن."""
        from datetime import date as _date
        pdate = lot["date"] if isinstance(lot["date"], _date) \
            else _date.fromisoformat(str(lot["date"])[:10])
        qty = float(lot["quantity"])
        price = float(lot.get("price") or 0.0)
        # شناسه یکتا: دو آفر فرضی با تاریخ و تعداد یکسان نباید یکی شوند
        seq = len(getattr(self, "_injected", []))
        self._injected = getattr(self, "_injected", []) + [1]
        tag = f"{pdate.isoformat()}|{int(qty)}" + (f"|#{seq}" if seq else "")
        group = f"OFFER|{tag}"
        for w in [float(x) for x in self.A.get("planning.harvest_weights")]:
            for pool, sc in ((self.cand_base, Scenario.base()),
                             (self.cand_adv, Scenario.adverse(self.A))):
                p = self.model.new_lot_profile(pdate, qty, w, sc)
                # قیمت واقعی آفر جای قیمت فرضی تخم را می‌گیرد
                p.egg_cost = [0.0] * len(p.egg_cost)
                p.egg_cost[p.first_week] = qty * price
                p.key = f"L|OFFER|{tag}|{w:g}"
                p.group = group
                p.month = pdate.strftime("%Y-%m")
                pool["new_lots"][p.key] = p
        return group

    # -------------------------------------- حل + اصلاح ظرفیت صحیح (۱۱)
    def _forced_pond_floor(self) -> list:
        """
        کف اجباری نیاز استخر که از **موجودی فعلی مزرعه** می‌آید.

        ماهی‌های موجود در استخر هستند و جایی باید باشند؛ تنها اهرم
        optimizer انتخاب وزن/زمان برداشت آن‌هاست. پس کف واقعی هر هفته
        برابر است با مجموع «کمترین نیاز استخر در میان کاندیدهای هر
        cohort» — نه مجموع کاندید انتخاب‌شده. محدودیت `Σf=1` تضمین می‌کند
        هر cohort دقیقاً یکی از این کاندیدها را بگیرد، پس هیچ انتخابی
        نمی‌تواند از این کف پایین‌تر برود.

        اگر همین کف از ظرفیت عملیاتی عبور کند، عبور از ظرفیت **وضعیت
        موجود** است نه نتیجهٔ تصمیم‌های برنامه — و سفت‌تر کردن سقف همان
        هفته در دور اصلاح، هرگز آن را کم نمی‌کند. تفکیک این دو هم از
        چرخش بی‌نتیجهٔ حلقهٔ اصلاح جلوگیری می‌کند و هم باعث می‌شود گزارش
        مقصر درست را نشان دهد.

        lotهای جدید عمداً شمرده نمی‌شوند: آن‌ها کاملاً اختیاری‌اند.
        """
        if getattr(self, "_forced_floor_cache", None) is not None:
            return self._forced_floor_cache
        weeks = self.model.weeks
        by_cohort: dict[str, list] = {}
        for k, p in self.cand_base["existing"].items():
            by_cohort.setdefault(k.split("|")[1], []).append(p)

        def need(p, t) -> float:
            if p.fish[t] < 1 or not self.bio.counts_toward_pond_capacity(p.weight[t]):
                return 0.0
            return math.ceil(p.fish[t] / self.bio.fish_per_pond(p.weight[t]))

        out = [0.0] * (weeks + 1)
        for profs in by_cohort.values():
            # هر cohort موجود یک گروه فیزیکی جداست (`Profile.group = C|<id>`)،
            # پس گرد کردن per-cohort با `_integer_ponds` هم‌واحد است. حتی اگر
            # optimizer یک cohort را بین دو وزن تقسیم کند، نیاز استخر ترکیب
            # بین دو کاندید قرار می‌گیرد؛ پس `min` یک کف معتبر است.
            for t in range(weeks + 1):
                out[t] += min(need(p, t) for p in profs)
        self._forced_floor_cache = out
        return out

    def _classify_pond_breach(self, op: int) -> dict:
        """
        عبور از ظرفیت را به دو دستهٔ کاملاً متفاوت تفکیک می‌کند:

        * `avoidable` — هفته‌هایی که تصمیم‌های برنامه (lot جدید یا
          cohortهایی که هنوز گزینه دارند) در ایجادشان نقش دارند. اینها
          با برش ظرفیت قابل اصلاح‌اند و اگر باقی بمانند، برنامه واقعاً
          اجراشدنی نیست.
        * `unavoidable` — هفته‌هایی که فقط کف اجباری موجودی فعلی از سقف
          عملیاتی عبور می‌کند. هیچ تصمیمی در مدل فعلی نمی‌تواند آن را کم
          کند؛ صادقانه‌ترین کار، گزارش آن به‌عنوان فشار وضعیت موجود است،
          نه اعلام «برنامه غلط».
        """
        ponds = self.ponds
        forced = self._forced_pond_floor()
        # هفته صفر وضعیت امروز است و با تصمیم‌های برنامه قابل اصلاح نیست
        over = {t: ponds[t] - op for t in range(1, len(ponds)) if ponds[t] > op}
        unavoidable = {t: e for t, e in over.items() if forced[t] > op}
        avoidable = {t: e for t, e in over.items() if t not in unavoidable}
        self.pond_forced_floor = forced
        self.pond_over_avoidable = avoidable
        self.pond_over_unavoidable = unavoidable
        self.pond_reserve_needed = max(over.values(), default=0)
        self.pond_breach_source = ("plan" if avoidable
                                   else ("state" if unavoidable else None))
        self.pond_feasible = not over
        return {"over": over, "avoidable": avoidable, "unavoidable": unavoidable}

    def _solve_with_repair(self):
        """
        Solve → اعتبارسنجی → در صورت نیاز اصلاح محدودیت → حل دوباره.

        دو چیز بعد از حل ممکن است نقض شود:

        ۱) **ظرفیت صحیح استخر** — تقریب خطی داخل MILP ممکن است برنامه‌ای
           بدهد که پس از گرد کردن از ۱۹ استخر عملیاتی عبور کند. سقف همان
           هفته‌ها سفت‌تر می‌شود.
        ۲) **نقدینگی** — محدودیت داخل MILP روی سرمایه در موجودی است، ولی
           نیاز واقعی از دفتر نقدی برنامه‌ریزی‌شده می‌آید (با تأخیر دریافت
           ۵۰٪ وجه). اگر از سرمایه موجود عبور کند، سقف سرمایه داخل مدل
           کوچک‌تر می‌شود.

        فقط Warning دادن کافی نیست: برنامه نهایی باید واقعاً اجراشدنی باشد.
        """
        A = self.A
        enforce = bool(A.get("planning.enforce_pond_feasibility"))
        max_rounds = int(A.get("planning.max_repair_rounds"))
        op = int(A.get("farm.operational_ponds"))
        cuts: dict[int, float] = {}
        wc_scale = 1.0
        best = None          # بهترین راه‌حل دیده‌شده (کمترین نقض)

        while True:
            try:
                self.solution = solve(A, self.model, self.cand_base, self.cand_adv,
                                      self.variant, self.wc_available * wc_scale,
                                      pond_cuts=cuts, force_group=self.force_group)
            except SolverTimeout as e:
                # ناظر زمانی CBC را متوقف کرد. اگر دور قبلی جوابی داده،
                # همان بهترین جواب استفاده می‌شود؛ وگرنه خطای شفاف بالا می‌رود.
                if best is None:
                    raise
                self.repair_log.append(
                    f"دور {self.repair_rounds + 1}: حل‌کننده در سقف زمانی جواب نداد "
                    f"({e}). بهترین جواب دورهای قبلی استفاده شد.")
                break
            self._aggregate()
            self.cash = PlannedCashLedger(self, self.extra_cash_rows)
            cm = self.cash.metrics()

            cls = self._classify_pond_breach(op)
            over, avoidable = cls["over"], cls["avoidable"]
            self.wc_feasible = not cm["wc_breach"]

            breach = (sum(over.values())
                      + max(0.0, cm["peak_funding_requirement"] - self.wc_available)
                      / max(1.0, self.wc_available))
            score = (breach, -self.solution.objective)
            if best is None or score < best[0]:
                best = (score, self.solution, wc_scale)

            if not enforce or self.repair_rounds >= max_rounds or \
                    (not avoidable and not cm["wc_breach"]):
                if cls["unavoidable"] and enforce:
                    self.repair_log.append(
                        f"{len(cls['unavoidable'])} هفته فقط به‌خاطر کف اجباری "
                        f"موجودی فعلی مزرعه از {op} استخر عبور می‌کند — هیچ "
                        f"ترکیبی از وزن‌های برداشت نمی‌تواند آن را پایین بیاورد؛ "
                        f"برش ظرفیت روی این هفته‌ها اثری ندارد و اعمال نشد.")
                if avoidable and enforce:
                    self.repair_log.append(
                        f"پس از {self.repair_rounds} دور اصلاح، هنوز "
                        f"{len(avoidable)} هفته به‌دلیل تصمیم‌های برنامه از {op} "
                        f"استخر عبور می‌کند.")
                if cm["wc_breach"] and enforce:
                    self.repair_log.append(
                        f"پس از {self.repair_rounds} دور اصلاح، اوج نیاز نقدی "
                        f"{cm['peak_funding_requirement']:,.0f} همچنان از سرمایه موجود "
                        f"{self.wc_available:,.0f} بیشتر است.")
                break

            if avoidable:
                for t, excess in avoidable.items():
                    cuts[t] = cuts.get(t, 0.0) + max(1.0, excess)
                self.repair_log.append(
                    f"دور {self.repair_rounds + 1}: {len(avoidable)} هفته پس از گرد "
                    f"کردن صحیح از ظرفیت عبور می‌کرد؛ سقف همان هفته‌ها سفت‌تر شد.")
            if cm["wc_breach"]:
                need = cm["peak_funding_requirement"]
                wc_scale *= max(0.55, min(0.95, self.wc_available / need))
                self.repair_log.append(
                    f"دور {self.repair_rounds + 1}: اوج نیاز نقدی {need:,.0f} از "
                    f"سرمایه موجود بیشتر بود؛ سقف سرمایه داخل مدل به "
                    f"{wc_scale:.0%} کاهش یافت.")
            self.repair_rounds += 1

        # اگر آخرین دور بدتر از بهترین دور بود، بهترین را برمی‌گردانیم
        if best is not None and best[1] is not self.solution:
            self.solution = best[1]
            wc_scale = best[2]
            self._aggregate()
            self.cash = PlannedCashLedger(self, self.extra_cash_rows)
            self._classify_pond_breach(op)
            self.wc_feasible = not self.cash.metrics()["wc_breach"]
            self.repair_log.append("بهترین راه‌حل میان دورهای اصلاح انتخاب شد.")
        self.wc_scale_used = wc_scale

    # ================================================================ core
    def _chosen(self, pool_key: str):
        """پروفایل‌های انتخاب‌شده با ضریبشان."""
        out = []
        for k, weight in self.solution.selected.items():
            pool = self.cand_base["new_lots"] if k.startswith("L|") \
                else self.cand_base["existing"]
            if (pool_key == "new" and k.startswith("L|")) or \
               (pool_key == "existing" and k.startswith("C|")) or pool_key == "all":
                if k in pool:
                    out.append((pool[k], weight))
        return out

    def _series(self, attr: str, scenario: str = "base") -> list:
        weeks = self.model.weeks
        acc = [0.0] * (weeks + 1)
        for k, weight in self.solution.selected.items():
            pools = self.cand_base if scenario == "base" else self.cand_adv
            pool = pools["new_lots"] if k.startswith("L|") else pools["existing"]
            p = pool.get(k) or (self.cand_base["new_lots"].get(k)
                                or self.cand_base["existing"].get(k))
            if not p:
                continue
            arr = getattr(p, attr)
            for t in range(weeks + 1):
                if arr[t]:
                    acc[t] += arr[t] * weight
        return acc

    def _actual_weight_bucket(self, w: float) -> float:
        """
        نزدیک‌ترین وزن هدفِ رسمی (از `planning.harvest_weights`) که ماهی
        واقعاً از آن عبور کرده — منبع مشترک برای هر گزارشی که فروش را به
        تفکیک وزن نشان می‌دهد (`sales_by_weight`، `unsold_by_weight`،
        تفکیک ماهانه/سه‌ماهه در `_sales_in`)، تا با هم واگرا نشوند.
        """
        hws = sorted(float(x) for x in self.A.get("planning.harvest_weights"))
        bucket = hws[0]
        for hw in hws:
            if w >= hw - 1e-9:
                bucket = hw
            else:
                break
        return bucket

    def _integer_ponds(self, scenario: str = "base") -> list:
        """نیاز استخر صحیح، با تجمیع شاخه‌های یک cohort قبل از گرد کردن."""
        weeks = self.model.weeks
        pools = self.cand_base if scenario == "base" else self.cand_adv
        groups: dict[str, list] = {}
        for k, weight in self.solution.selected.items():
            pool = pools["new_lots"] if k.startswith("L|") else pools["existing"]
            p = pool.get(k) or (self.cand_base["new_lots"].get(k)
                                or self.cand_base["existing"].get(k))
            if p:
                groups.setdefault(p.group, []).append((p, weight))
        out = [0.0] * (weeks + 1)
        for _, members in groups.items():
            for t in range(weeks + 1):
                fish = sum(p.fish[t] * w for p, w in members)
                if fish < 1:
                    continue
                wsum = sum(p.fish[t] * w * p.weight[t] for p, w in members)
                mean_w = wsum / fish if fish else 0.0
                if not self.bio.counts_toward_pond_capacity(mean_w):
                    continue
                out[t] += math.ceil(fish / self.bio.fish_per_pond(mean_w))
        return out

    def _aggregate(self):
        g, A = self.grid, self.A
        weeks = self.model.weeks
        # نیاز استخر پس از حل، در سطح «گروه فیزیکی» گرد می‌شود.
        # اگر بهینه‌ساز یک cohort را بین دو وزن برداشت تقسیم کند، ماهی‌ها تا
        # اولین موج برداشت هنوز در همان استخرها هستند؛ پس باید مجموع آن‌ها یک
        # بار گرد شود، نه هر شاخه جداگانه (وگرنه ظرفیت دوباره‌شماری می‌شود).
        self.ponds = self._integer_ponds("base")
        self.ponds_adverse = self._integer_ponds("adverse")
        self.ponds_lp = self._series("ponds")      # همان چیزی که در MILP بود
        self.feed_kg = self._series("feed_kg")
        self.feed_cost = self._series("feed_cost")
        self.revenue = self._series("revenue")
        self.egg_cost = self._series("egg_cost")
        self.capital = self._series("capital")
        self.harvest_fish = self._series("harvest_fish")
        self.revenue_adv = self._series("revenue", "adverse")
        self.feed_cost_adv = self._series("feed_cost", "adverse")

        # خرید تخم به تفکیک هفته
        self.eggs = [0.0] * (weeks + 1)
        for lot in self.solution.chosen_lots:
            t = g.index_of(date.fromisoformat(lot["purchase_date"]))
            self.eggs[t] += lot["quantity"]

        # فروش به تفکیک وزن — بر اساس وزن *واقعیِ* هر رویداد فروش، نه وزن
        # هدف اولیهٔ لات. یک لات با هدف ۵g که بخشی از حجمش در carry-over
        # تا ۲۰+ گرم رشد کرده و در همان‌جا فروخته، قبلاً *همهٔ* آن حجم را
        # زیر «۵g» گزارش می‌کرد — یعنی فروش واقعی در ۱۰-۱۵ و بالای ۱۵ گرم
        # هرگز در این گزارش دیده نمی‌شد، حتی وقتی واقعاً اتفاق افتاده بود.
        self.sales_by_weight = {}
        self.unsold_by_weight = {}
        for p, wgt in self._chosen("all"):
            for t, n in enumerate(p.harvest_fish):
                if n:
                    b = self._actual_weight_bucket(p.weight[t])
                    self.sales_by_weight[b] = self.sales_by_weight.get(b, 0.0) + n * wgt
            if p.unsold_fish > 1e-6:
                last_w = p.weight[p.last_week] if p.last_week < len(p.weight) \
                    else p.weight[-1]
                b = self._actual_weight_bucket(last_w)
                self.unsold_by_weight[b] = \
                    self.unsold_by_weight.get(b, 0.0) + p.unsold_fish * wgt

        # هشدار Unsold Inventory / Sale Bottleneck. دو منبع جدا دارد که هر
        # دو از همان اعداد شبیه‌سازی‌شده می‌آیند، نه محاسبهٔ جدا:
        #   ۱) سهم قابل‌توجهی از فروش در بازه‌ای با احتمال پایین (هشدار
        #      پیشگیرانه، پیش از رسیدن به سقف تقاضا).
        #   ۲) موجودی واقعاً فروش‌نرفته/carry-over (`Profile.unsold_fish`)
        #      که چون سقف تقاضای همان بازه/دوره کافی نبوده، تا انتهای بازهٔ
        #      شبیه‌سازی‌شدهٔ آن profile هنوز فروخته نشده است.
        self.saleability_warnings = []
        total_sold = sum(self.sales_by_weight.values())
        if total_sold > 0:
            warn_p = float(A.get("planning.saleability_warn_threshold"))
            for w, qty in sorted(self.sales_by_weight.items()):
                share = qty / total_sold
                if share < 0.05:            # سهم ناچیز را هشدار نده
                    continue
                band = saleability_band(A, w)
                if band["prob"] < warn_p:
                    self.saleability_warnings.append(
                        f"{qty:,.0f} قطعه ({share:.0%} از کل فروش برنامه) در "
                        f"وزن {w:g}g قرار گرفته که سهولت فروش آن «{band['label']}» "
                        f"(احتمال {band['prob']:.0%}) است — ریسک انباشت "
                        f"موجودی فروش‌نرفته یا نیاز به تخفیف/تأخیر بیشتر وجود دارد.")
        for w, qty in sorted(self.unsold_by_weight.items()):
            if qty < 1:
                continue
            band = saleability_band(A, w)
            cap = band["max_demand_per_period"]
            self.saleability_warnings.append(
                f"{qty:,.0f} قطعه در وزن {w:g}g با سقف تقاضای فرض‌شدهٔ "
                f"{cap:,.0f} قطعه/{A.get('planning.demand_period_days'):g}روز "
                f"در بازهٔ برنامه‌ریزی‌شده قابل فروش نبوده و به‌عنوان "
                f"Unsold Inventory باقی مانده است.")

        # هزینه ثابت ماهانه (نرخ effective هر ماه)
        self.fixed_cost = [0.0] * (weeks + 1)
        seen = set()
        mode = str(A.get("cost.fixed_cost_mode", "top_up"))
        for t in range(weeks + 1):
            mk = g.month_of(t)
            if mk in seen or mode == "actual_only":
                continue
            seen.add(mk)
            self.fixed_cost[t] = float(A.get_at("cost.fixed_monthly", g.dates[t]))

        self.monthly = self._bucket("month")
        self.quarterly = self._bucket("quarter")

    # -------------------------------------------------------- bucketing
    def _bucket(self, kind: str) -> list:
        g = self.grid
        weeks = self.model.weeks
        keyf = g.month_of if kind == "month" else g.quarter_of
        order, buckets = [], {}
        for t in range(weeks + 1):
            k = keyf(t)
            if k not in buckets:
                buckets[k] = {"key": k, "kind": kind, "weeks": [],
                              "eggs_purchased": 0.0, "feed_kg": 0.0,
                              "feed_cost": 0.0, "revenue": 0.0, "egg_cost": 0.0,
                              "fixed_cost": 0.0, "harvest_fish": 0.0,
                              "revenue_adverse": 0.0, "feed_cost_adverse": 0.0,
                              "peak_ponds": 0.0, "peak_ponds_adverse": 0.0,
                              "peak_capital": 0.0, "start": g.dates[t].isoformat()}
                order.append(k)
            b = buckets[k]
            b["weeks"].append(t)
            b["eggs_purchased"] += self.eggs[t]
            b["feed_kg"] += self.feed_kg[t]
            b["feed_cost"] += self.feed_cost[t]
            b["revenue"] += self.revenue[t]
            b["egg_cost"] += self.egg_cost[t]
            b["fixed_cost"] += self.fixed_cost[t]
            b["harvest_fish"] += self.harvest_fish[t]
            b["revenue_adverse"] += self.revenue_adv[t]
            b["feed_cost_adverse"] += self.feed_cost_adv[t]
            b["peak_ponds"] = max(b["peak_ponds"], self.ponds[t])
            b["peak_ponds_adverse"] = max(b["peak_ponds_adverse"], self.ponds_adverse[t])
            b["peak_capital"] = max(b["peak_capital"], self.capital[t])
            b["end"] = g.dates[t].isoformat()

        op = int(self.A.get("farm.operational_ponds"))
        out = []
        for k in order:
            b = buckets[k]
            b["contribution_nominal"] = (b["revenue"] - b["feed_cost"]
                                         - b["egg_cost"] - b["fixed_cost"])
            b["contribution_risk_adjusted"] = (b["revenue_adverse"]
                                               - b["feed_cost_adverse"]
                                               - b["egg_cost"] - b["fixed_cost"])
            b["pond_utilisation"] = b["peak_ponds"] / op if op else 0.0
            b["pond_headroom"] = op - b["peak_ponds"]
            b["feed_purchase_kg"] = b["feed_kg"]      # سیاست پایه: خرید = مصرف
            b["feed_purchase_cost"] = b["feed_cost"]
            b["expected_survival"] = self._survival_in(b)
            b["sales_by_weight"] = self._sales_in(b)
            b.pop("weeks", None)
            out.append(b)
        return out

    def _survival_in(self, bucket) -> float | None:
        """بقای مورد انتظار lotهایی که در این دوره خریداری می‌شوند."""
        tot_q, tot_s = 0.0, 0.0
        for p, w in self._chosen("new"):
            if p.month == bucket["key"] or bucket["kind"] == "quarter" and \
                    p.month[:4] == bucket["key"][:4] and \
                    (int(p.month[5:7]) - 1) // 3 + 1 == int(bucket["key"][-1]):
                tot_q += p.quantity * w
                tot_s += p.sold_fish * w
        return (tot_s / tot_q) if tot_q else None

    def _sales_in(self, bucket) -> dict:
        g = self.grid
        out = {}
        lo, hi = bucket["start"], bucket["end"]
        for k, weight in self.solution.selected.items():
            pool = self.cand_base["new_lots"] if k.startswith("L|") \
                else self.cand_base["existing"]
            p = pool.get(k)
            if not p:
                continue
            for t, n in enumerate(p.harvest_fish):
                if n and lo <= g.dates[t].isoformat() <= hi:
                    b = self._actual_weight_bucket(p.weight[t])
                    out[b] = out.get(b, 0.0) + n * weight
        return out

    # -------------------------------- تفکیک ۱۲ ماه از کل چرخه عمر (۸)
    def horizon_split(self) -> dict:
        """
        دو نتیجه کاملاً جدا:

        A) نتیجه غلتان ۱۲ ماهه — فقط جریان‌های اقتصادی ۱۲ ماه آینده.
        B) ارزش کل چرخه عمر برنامه ۱۲ ماهه — همه جریان‌های cohortهایی که
           در همین سال وارد می‌شوند، حتی اگر فروششان به سال بعد بیفتد.

        عدد چرخه عمر هرگز «سود سالانه» نامیده نمی‌شود.
        """
        g = self.grid
        cutoff = self.state.as_of + timedelta(days=365)
        t_end = g.index_of(cutoff)

        def agg(lo, hi):
            rev = sum(self.revenue[lo:hi + 1])
            feed = sum(self.feed_cost[lo:hi + 1])
            egg = sum(self.egg_cost[lo:hi + 1])
            fixed = sum(self.fixed_cost[lo:hi + 1])
            adv = sum(self.revenue_adv[lo:hi + 1]) - sum(self.feed_cost_adv[lo:hi + 1]) \
                - egg - fixed
            return {"revenue": rev, "feed_cost": feed, "egg_cost": egg,
                    "fixed_cost": fixed,
                    "contribution_nominal": rev - feed - egg - fixed,
                    "contribution_risk_adjusted": adv,
                    "fish_sold": sum(self.harvest_fish[lo:hi + 1]),
                    "eggs_purchased": sum(self.eggs[lo:hi + 1])}

        a = agg(0, t_end)
        b = agg(0, self.model.weeks)
        unsold = b["fish_sold"] - a["fish_sold"]
        return {
            "rolling_12m": {
                **a, "label_fa": "نتیجه عملیاتی غلتان ۱۲ ماهه",
                "from": g.dates[0].isoformat(), "to": g.dates[t_end].isoformat(),
                "weeks": t_end,
                "note_fa": "فقط جریان‌های همین ۱۲ ماه؛ این عدد «سود سالانه» است.",
            },
            "full_lifecycle": {
                **b, "label_fa": "ارزش کل چرخه عمر برنامه ۱۲ ماهه",
                "from": g.dates[0].isoformat(),
                "to": g.dates[self.model.weeks].isoformat(),
                "weeks": self.model.weeks,
                "spillover_fish": unsold,
                "spillover_revenue": b["revenue"] - a["revenue"],
                "note_fa": ("شامل فروش cohortهایی است که خریدشان در همین سال است "
                            "ولی برداشتشان به سال بعد می‌افتد. این عدد سود سالانه "
                            "نیست."),
            },
        }

    # ------------------------------------------- ارزش اقتصادی برنامه (مرحله ۳)
    def npv(self) -> float:
        """
        ارزش امروزِ خالص کل برنامه — معیار اصلی مقایسه دو تصمیم.

        چرا NPV و نه فقط حاشیه اسمی؟ چون «فروش امروز» و «فروش چهار ماه بعد»
        دو جریان نقدی هم‌ارز نیستند. تنزیل ساده و قابل تنظیم است و از همان
        نرخ فرصتی می‌آید که برای شرایط پرداخت به کار می‌رود.

        نکته صادقانه: تابع هدف MILP همچنان حاشیه اسمی است. یعنی بهینه‌ساز
        بهترین برنامه را بر مبنای حاشیه اسمی پیدا می‌کند و ما آن برنامه را
        با معیار NPV ارزش‌گذاری می‌کنیم. برای مقایسه دو سناریو کافی است،
        ولی «بهینه دقیق NPV» نیست.
        """
        return self.cash.npv()

    def value_digest(self) -> dict:
        """خلاصه ارزش و feasibility یک برنامه — برای مقایسه سناریوها."""
        s = self.summary()
        cm = self.cash.metrics()
        return {
            "npv": self.npv(),
            "contribution_nominal": s["contribution_nominal"],
            "contribution_risk_adjusted": s["contribution_risk_adjusted"],
            "rolling_12m": s["rolling_12m"]["contribution_nominal"],
            "revenue": s["revenue_total"],
            "feed_cost": s["feed_cost_total"],
            "egg_cost": s["egg_cost_total"],
            "fish_sold": s["fish_to_sell"],
            "eggs_planned": s["eggs_planned"],
            "peak_ponds": s["peak_ponds"],
            "pond_feasible": s["pond_feasible"],
            "pond_shortfall": max(self.solution.pond_shortfall or [0.0]),
            "peak_funding": cm["peak_funding_requirement"],
            "minimum_cash_balance": cm["minimum_cash_balance"],
            "wc_available": cm["wc_available"],
            "wc_breach": cm["wc_breach"],
            "plan_status": s["plan_status"],
        }

    def plan_status(self) -> dict:   # noqa: C901
        """وضعیت برنامه: تا وقتی موجودی reconcile نشده، PROVISIONAL است."""
        rec = self.reconciliation
        reasons = []
        status = "FINAL"
        if not rec["reconciled"]:
            status = "PROVISIONAL"
            reasons.append(
                f"{rec['unallocated_fish']:,.0f} قطعه فروش هنوز به cohort تخصیص "
                f"نیافته است؛ موجودی زنده و در نتیجه برنامه قطعی نیست.")
        if not getattr(self, "pond_feasible", True):
            status = "PROVISIONAL"
            if getattr(self, "pond_breach_source", "plan") == "state":
                reasons.append(
                    f"اوج نیاز استخر از ظرفیت عملیاتی عبور می‌کند، ولی منشأ آن "
                    f"موجودی فعلی مزرعه است نه تصمیم‌های برنامه — "
                    f"{self.pond_reserve_needed:.0f} استخر رزرو لازم می‌شود.")
            else:
                reasons.append("برنامه پس از گرد کردن صحیح از ظرفیت استخر عبور می‌کند.")
        cm = self.cash.metrics()
        if cm["wc_breach"]:
            status = "PROVISIONAL"
            reasons.append(
                f"اوج نیاز نقدی {cm['peak_funding_requirement']:,.0f} تومان از سرمایه "
                f"در گردش موجود {cm['wc_available']:,.0f} تومان بیشتر است.")
        return {"status": status, "reasons": reasons,
                "reconciliation": rec,
                "pond_feasible": getattr(self, "pond_feasible", True),
                "repair_rounds": self.repair_rounds,
                "repair_log": self.repair_log}

    # ==================================================== public reports
    def summary(self) -> dict:
        op = int(self.A.get("farm.operational_ponds"))
        cm = self.cash.metrics()
        hs = self.horizon_split()
        ps = self.plan_status()
        dec_months = {m for m, _ in self.model.decision_months()}
        plan_months = [b for b in self.monthly if b["key"] in dec_months]
        rev = sum(b["revenue"] for b in self.monthly)
        return {
            "variant": self.variant.name,
            "variant_label_fa": self.variant.label_fa,
            "solver": self.solution.solver,
            "status": self.solution.status,
            "as_of": self.state.as_of.isoformat(),
            "decision_months": len(dec_months),
            "eval_weeks": self.model.weeks,
            "eggs_planned": sum(l["quantity"] for l in self.solution.chosen_lots),
            "lots_planned": len(self.solution.chosen_lots),
            "fish_to_sell": sum(self.harvest_fish),
            "revenue_total": rev,
            "egg_cost_total": sum(self.egg_cost),
            "feed_cost_total": sum(self.feed_cost),
            "fixed_cost_total": sum(self.fixed_cost),
            "contribution_nominal": sum(b["contribution_nominal"] for b in self.monthly),
            "contribution_risk_adjusted": sum(b["contribution_risk_adjusted"]
                                              for b in self.monthly),
            "peak_ponds": max(self.ponds) if self.ponds else 0,
            "peak_ponds_adverse": max(self.ponds_adverse) if self.ponds_adverse else 0,
            "operational_ponds": op,
            "pond_breach": max(self.ponds, default=0) > op,
            "pond_breach_adverse": max(self.ponds_adverse, default=0) > op,
            "peak_capital": max(self.capital) if self.capital else 0,
            "wc_available": self.wc_available,
            "wc_headroom": cm["wc_headroom"],
            "peak_funding_requirement": cm["peak_funding_requirement"],
            "peak_funding_date": cm["peak_funding_date"],
            "minimum_cash_balance": cm["minimum_cash_balance"],
            "wc_breach": cm["wc_breach"],
            "rolling_12m": hs["rolling_12m"],
            "full_lifecycle": hs["full_lifecycle"],
            "plan_status": ps["status"],
            "plan_status_reasons": ps["reasons"],
            "pond_feasible": ps["pond_feasible"],
            "repair_rounds": self.repair_rounds,
            "sales_by_weight": self.sales_by_weight,
            "unsold_by_weight": self.unsold_by_weight,
            "saleability_warnings": self.saleability_warnings,
            "notes": self.solution.notes,
            "plan_months": len(plan_months),
        }

    @staticmethod
    def _flush_sale_batch(acts_sale, hw, start, end, qty, sources, min_qty, typ_min,
                          typ_max, as_of):
        """یک دستهٔ تجمیع‌شدهٔ فروش (از یک یا چند بازهٔ متوالی) را به فهرست
        اقدام‌ها اضافه می‌کند، با برچسب استثنای متناسب با کف مطلق/بازهٔ معمول."""
        span = "" if start == end else f" تا {end.isoformat()}"
        note = ""
        if qty < min_qty:
            note = (f" — استثنایی و بسیار کوچک (کمتر از کف مطلق "
                    f"{min_qty:,.0f})؛ معمولاً باقیماندهٔ آخر یک cohort")
        elif qty < typ_min:
            note = (f" — کمتر از بازهٔ معمول فروش ({typ_min:,.0f} تا "
                    f"{typ_max:,.0f})؛ جمعیت کافی برای رساندن به بازهٔ معمول "
                    f"در دسترس نبود")
        src_txt = "، ".join(
            f"{lb} ({sv['qty']:,.0f}{' · سهم جزئی' if sv['partial'] else ''})"
            for lb, sv in sources.items())
        acts_sale.append({
            "date": start.isoformat(), "type": "sale",
            "title": f"فروش {qty:,.0f} قطعه در {hw:g} گرم",
            "detail": f"از {src_txt}{span}" + note,
            "quantity": qty, "days": (start - as_of).days,
            "below_typical_range": qty < typ_min,
            "very_small_exception": qty < min_qty})

    def action_plan_90d(self) -> dict:
        """
        برنامه اقدام ۹۰ روز آینده — خروجی عملیاتی اصلی برای مدیر.

        پیشنهادهای خرید/فروش در بازه‌های `planning.decision_window_days`
        روزه تجمیع می‌شوند (اصلاح ۲) — این فقط cadence گزارش‌دهی است؛
        سقف تقاضا/ظرفیت استخر هنوز روی شبکهٔ هفتگیِ زیرین محاسبه شده‌اند
        و اینجا فقط جمع‌بندی می‌شوند. تاریخِ هر اقدام تجمیعی، ابتدای همان
        بازهٔ تصمیم است — یعنی داخل همان window می‌ماند، طبق درخواست.
        ثبت تراکنش واقعی همچنان با تاریخ واقعی خودش انجام می‌شود؛ این
        تابع فقط یک برنامهٔ پیشنهادی/گزارشی است.
        """
        g = self.grid
        end = self.state.as_of + timedelta(days=90)
        win_days = max(1, int(self.A.get("planning.decision_window_days")))

        def win_start(d):
            idx = (d - self.state.as_of).days // win_days
            return self.state.as_of + timedelta(days=idx * win_days)

        # -------------------------------------------------------- خرید تخم
        buys: dict = {}
        for lot in self.solution.chosen_lots:
            dt = date.fromisoformat(lot["purchase_date"])
            if dt > end:
                continue
            ws = win_start(dt)
            key = (ws, round(float(lot["harvest_w"]), 2))
            b = buys.setdefault(key, {"qty": 0.0, "n": 0})
            b["qty"] += lot["quantity"]
            b["n"] += 1

        acts = []
        for (ws, hw), b in buys.items():
            acts.append({
                "date": ws.isoformat(), "type": "egg_purchase",
                "title": f"خرید {b['qty']:,.0f} تخم",
                "detail": f"هدف برداشت در {hw:g} گرم"
                          + (f" ({b['n']} لات در همین بازه)" if b["n"] > 1 else "")
                          + " · در روزهای منتهی به این بازه آفر بگیرید",
                "quantity": b["qty"], "days": (ws - self.state.as_of).days})

        # ----------------------------------------------------------- فروش
        min_qty = float(self.A.get("sale.min_qty"))
        typ_min = float(self.A.get("sale.typical_min_qty"))
        typ_max = float(self.A.get("sale.typical_max_qty"))
        sales: dict = {}
        for k, weight in self.solution.selected.items():
            pool = self.cand_base["new_lots"] if k.startswith("L|") \
                else self.cand_base["existing"]
            p = pool.get(k)
            if not p:
                continue
            label = p.key.split("|")[1] if p.kind == "existing" else "lot جدید"
            for t, n in enumerate(p.harvest_fish):
                if not n or g.dates[t] > end or n * weight < 1:
                    continue
                ws = win_start(g.dates[t])
                key = (ws, round(float(p.harvest_w), 2))
                s = sales.setdefault(key, {"qty": 0.0, "sources": {}})
                s["qty"] += n * weight
                src = s["sources"].setdefault(label, {"qty": 0.0, "partial": False})
                src["qty"] += n * weight
                if weight < 0.999:
                    src["partial"] = True

        # تجمیع بیشتر (اصلاح ۱): اقدام‌های فروشِ پیاپی و کوچک برای همان
        # وزن هدف، در چند بازهٔ متوالی، تا رسیدن به کف بازهٔ معمول با هم
        # ادغام می‌شوند — هدف کاهش تعداد Sale Actionهای کوچک/غیرعملی در
        # برنامهٔ ۹۰روزه است. این فقط تجمیع گزارشی است؛ سقف تقاضا/ظرفیت
        # هنوز روی شبکهٔ هفتگیِ زیرین محاسبه شده و اینجا دست‌نخورده
        # می‌ماند. اگر جمعیت کافی برای رسیدن به کف واقعاً وجود نداشته
        # باشد (مثلاً باقیماندهٔ آخر یک cohort)، آخرین دسته همچنان کوچک
        # می‌ماند و صادقانه به‌عنوان استثنا علامت می‌خورد.
        by_weight: dict[float, list] = {}
        for (ws, hw), s in sales.items():
            by_weight.setdefault(hw, []).append((ws, s))

        acts_sale = []
        for hw, entries in by_weight.items():
            entries.sort(key=lambda e: e[0])
            batch_qty, batch_sources, batch_start, batch_end = 0.0, {}, None, None
            for ws, s in entries:
                if batch_start is not None and batch_qty >= typ_min:
                    self._flush_sale_batch(acts_sale, hw, batch_start, batch_end,
                                           batch_qty, batch_sources, min_qty, typ_min, typ_max,
                                           self.state.as_of)
                    batch_qty, batch_sources, batch_start = 0.0, {}, None
                if batch_start is None:
                    batch_start = ws
                batch_end = ws
                batch_qty += s["qty"]
                for lb, sv in s["sources"].items():
                    b = batch_sources.setdefault(lb, {"qty": 0.0, "partial": False})
                    b["qty"] += sv["qty"]
                    b["partial"] = b["partial"] or sv["partial"]
                if batch_qty >= typ_max:
                    self._flush_sale_batch(acts_sale, hw, batch_start, batch_end,
                                           batch_qty, batch_sources, min_qty, typ_min, typ_max,
                                           self.state.as_of)
                    batch_qty, batch_sources, batch_start = 0.0, {}, None
            if batch_start is not None:
                self._flush_sale_batch(acts_sale, hw, batch_start, batch_end,
                                       batch_qty, batch_sources, min_qty, typ_min, typ_max,
                                           self.state.as_of)
        acts.extend(acts_sale)

        # خوراک — به تفکیک Feed Type (نه یک مقدار کلی برای کل ماه).
        # همان جدول قیمت خوراک بر اساس وزن (feed.price_table، از طریق
        # bio.feed_name) که همه‌جای دیگر مدل استفاده می‌شود، اینجا هم
        # منبع تشخیص نوع خوراک است — منطق یا جدول جدیدی ساخته نشد. برای
        # هر کاندید انتخاب‌شده، وزن واقعیِ همان هفته (p.weight[t]) نوع
        # خوراک را تعیین می‌کند.
        #
        # زمان سفارش: از موجودی *همان نوع* شروع می‌کنیم و هفته‌به‌هفته
        # طبق مصرف پیش‌بینی‌شدهٔ همان نوع کم می‌کنیم تا به آستانهٔ هشدار
        # برسد (feed.reorder_alert_days — از قبل در config تعریف شده
        # بود، فقط جایی استفاده نمی‌شد). اگر موجودی فعلی برای کل افق
        # ۹۰روزه کافی باشد، اصلاً اقدامی پیشنهاد نمی‌شود.
        weekly_by_type: dict = {}
        for p, wgt in self._chosen("all"):
            for t in range(min(len(p.feed_kg), len(g.dates))):
                kg = p.feed_kg[t] * wgt
                if kg <= 1e-9 or g.dates[t] > end:
                    continue
                ftype = self.bio.feed_name(p.weight[t])
                rec = weekly_by_type.setdefault(ftype, {})
                w = rec.setdefault(t, {"kg": 0.0, "cost": 0.0})
                w["kg"] += kg
                w["cost"] += p.feed_cost[t] * wgt

        reorder_days = int(self.A.get("feed.reorder_alert_days"))
        for ftype, by_week in weekly_by_type.items():
            stock = self.state.feed.get(ftype, {}).get("qty_kg", 0.0)
            total_kg = sum(w["kg"] for w in by_week.values())
            total_cost = sum(w["cost"] for w in by_week.values())
            order_t = None
            for t in sorted(by_week):
                weekly_kg = by_week[t]["kg"]
                alert_kg = (weekly_kg / 7.0) * reorder_days
                if stock <= alert_kg:
                    order_t = t
                    break
                stock -= weekly_kg
            if order_t is None:
                continue    # موجودی فعلی این نوع برای کل افق ۹۰روزه کافی است
            order_date = win_start(g.dates[order_t])
            acts.append({
                "date": order_date.isoformat(), "type": "feed_purchase",
                "title": f"سفارش {total_kg:,.0f} kg خوراک {ftype}",
                "detail": f"نوع خوراک: {ftype} · موجودی فعلی تا حدود "
                          f"{reorder_days} روز کفاف می‌دهد · هزینه تقریبی "
                          f"{total_cost:,.0f} تومان برای کل نیاز ۹۰روزهٔ همین نوع",
                "quantity": total_kg, "feed_type": ftype,
                "days": (order_date - self.state.as_of).days})
        acts.sort(key=lambda a: a["date"])
        w_end = g.index_of(end)
        return {
            "until": end.isoformat(),
            "decision_window_days": win_days,
            "actions": acts,
            "eggs_to_buy": sum(a["quantity"] for a in acts if a["type"] == "egg_purchase"),
            "fish_to_sell": sum(a["quantity"] for a in acts if a["type"] == "sale"),
            "feed_kg": sum(self.feed_kg[:w_end + 1]),
            "revenue": sum(self.revenue[:w_end + 1]),
            "peak_ponds": max(self.ponds[:w_end + 1]) if self.ponds else 0,
            "peak_capital": max(self.capital[:w_end + 1]) if self.capital else 0,
        }

    def capacity_curve(self) -> list:
        g = self.grid
        return [{"date": g.dates[t].isoformat(), "week": t,
                 "ponds": self.ponds[t], "ponds_adverse": self.ponds_adverse[t],
                 "capital": self.capital[t]}
                for t in range(self.model.weeks + 1)]

    def grading_outlook(self) -> list:
        """
        اثر پراکندگی رشد: چه سهمی از هر cohort زودتر/دیرتر به وزن فروش می‌رسد
        و چه زمانی grading یا برداشت جزئی لازم می‌شود.
        """
        g = self.grid
        out = []
        for k, weight in self.solution.selected.items():
            if not k.startswith("C|"):
                continue
            p = self.cand_base["existing"].get(k)
            if not p or weight < 1e-6:
                continue
            waves = [{"date": g.dates[t].isoformat(),
                      "fish": n * weight,
                      "days": (g.dates[t] - self.state.as_of).days}
                     for t, n in enumerate(p.harvest_fish) if n]
            if not waves:
                continue
            out.append({
                "cohort_id": k.split("|")[1],
                "harvest_w": p.harvest_w,
                "share_of_cohort": weight,
                "waves": waves,
                "spread_days": waves[-1]["days"] - waves[0]["days"],
                "grading_needed": len(waves) > 1 and
                (waves[-1]["days"] - waves[0]["days"]) >= 7,
            })
        out.sort(key=lambda r: (r["cohort_id"], r["harvest_w"]))
        return out

    def cohort_decisions(self) -> list:
        """تصمیم بهینه‌ساز برای هر cohort موجود — شامل برداشت جزئی."""
        out = []
        for cid, split in self.solution.cohort_split.items():
            c = self.state.cohorts.get(cid)
            rows = []
            for w, frac in sorted(split.items()):
                p = self.cand_base["existing"].get(f"C|{cid}|{w:g}")
                rows.append({
                    "harvest_w": w, "fraction": frac,
                    "fish": (c.alive * frac) if c else 0.0,
                    "revenue": (sum(p.revenue) * frac) if p else 0.0,
                    "first_harvest": next((self.grid.dates[t].isoformat()
                                           for t, n in enumerate(p.harvest_fish) if n), None)
                    if p else None,
                })
            out.append({
                "cohort_id": cid,
                "alive": c.alive if c else 0.0,
                "mean_weight_g": c.mean_weight if c else 0.0,
                "split": rows,
                "partial_harvest": len(rows) > 1,
            })
        out.sort(key=lambda r: r["cohort_id"])
        return out

    # ------------------------------------- بنچمارک سه‌ماهه سرمایه / ارز
    def quarterly_capital_fx(self, fxb) -> list:
        """
        برای هر سه‌ماهه: سرمایه ابتدای دوره، ارزش پایان دوره از دید مزرعه،
        بازده مزرعه، بازده جایگزین دلاری و مازاد.

        این شاخص فقط یک بنچمارک ارزش زمانی سرمایه است و هرگز جای NPV یا
        سود عملیاتی را نمی‌گیرد.
        """
        g = self.grid
        share = float(self.A.get("fx.benchmark_share"))
        by_q = {}
        for t in range(self.model.weeks + 1):
            q = g.quarter_of(t)
            b = by_q.setdefault(q, {"label": q, "weeks": [], "start": g.dates[t],
                                    "end": g.dates[t]})
            b["weeks"].append(t)
            b["end"] = g.dates[t]

        out = []
        for q in sorted(by_q):
            b = by_q[q]
            t0, t1 = b["weeks"][0], b["weeks"][-1]
            cap0 = self.capital[t0]
            cap1 = self.capital[t1]
            net = sum(self.revenue[t] - self.feed_cost[t] - self.egg_cost[t]
                      - self.fixed_cost[t] for t in b["weeks"])
            farm_gain = (cap1 - cap0) + net
            farm_return = (farm_gain / cap0) if cap0 > 0 else None

            # برای نرخ ارز از مرزهای تقویمی واقعی سه‌ماهه استفاده می‌شود،
            # نه از بازه‌ای که شبکه هفتگی برنامه پوشش می‌دهد. طبق specification:
            #   FX_start = اولین نرخ موجود در سه‌ماهه · FX_end = آخرین نرخ موجود
            yy = int(q[:4])
            qq = int(q[-1])
            qs = date(yy, 3 * (qq - 1) + 1, 1)
            qe = date(yy + (1 if qq == 4 else 0),
                      1 if qq == 4 else 3 * qq + 1, 1) - timedelta(days=1)
            fx0 = fxb._first_in(qs, qe) if fxb else None
            fx1 = fxb._last_in(qs, qe) if fxb else None
            complete = qe <= self.state.as_of
            future = qs > self.state.as_of
            suffix = "" if complete else (" (برنامه)" if future else " (تا امروز)")
            rec = {
                "label": q + suffix,
                "quarter": q,
                "complete": complete,
                "partial": not complete and not future,
                "future": future,
                "return_label_fa": ("بازده کامل سه‌ماهه" if complete else
                                    ("دوره آینده" if future else f"بازده {q} تا امروز")),
                "return_label_en": (f"{q} Full Quarter Return" if complete else
                                    (f"{q} Planned" if future
                                     else f"{q}-to-Date FX Return")),
                "start": b["start"].isoformat(), "end": b["end"].isoformat(),
                "quarter_start": qs.isoformat(), "quarter_end": qe.isoformat(),
                "beginning_capital": cap0,
                "ending_capital": cap1,
                "net_economic_value": net,
                "farm_gain": farm_gain,
                "farm_return": farm_return,
                "benchmark_share": share,
                "fx_available": bool(fx0 and fx1),
            }
            if fx0 and fx1 and fx0["close_toman"]:
                r = fx1["close_toman"] / fx0["close_toman"] - 1
                rec.update({
                    "fx_start": fx0["close_toman"], "fx_end": fx1["close_toman"],
                    "fx_return": r,
                    "usd_alternative_end_value": cap0 * share * (1 + r),
                    "usd_alternative_gain": cap0 * share * r,
                    "excess_over_fx": farm_gain - cap0 * share * r,
                })
            else:
                rec["note"] = "داده واقعی نرخ دلار برای این دوره موجود نیست"
            out.append(rec)
        return out

    # ------------------------------------------------------- risk flags
    def risk_flags(self) -> list:
        A = self.A
        op = int(A.get("farm.operational_ponds"))
        flags = []
        peak = max(self.ponds, default=0)
        if peak > op:
            wk = self.ponds.index(peak)
            flags.append({"level": "high", "id": "pond_capacity",
                          "title_fa": "کمبود ظرفیت استخر",
                          "detail_fa": f"اوج نیاز {peak:.0f} استخر در "
                                       f"{self.grid.dates[wk].isoformat()} در برابر "
                                       f"{op} استخر عملیاتی."})
        peak_a = max(self.ponds_adverse, default=0)
        if peak_a > op >= peak:
            flags.append({"level": "medium", "id": "pond_capacity_adverse",
                          "title_fa": "ریسک ظرفیت در سناریوی نامساعد",
                          "detail_fa": f"در سناریوی نامساعد نیاز به {peak_a:.0f} "
                                       f"استخر می‌رسد."})
        pk = max(self.capital, default=0)
        if pk > self.wc_available:
            flags.append({"level": "high", "id": "working_capital",
                          "title_fa": "کمبود سرمایه در گردش",
                          "detail_fa": f"اوج سرمایه لازم {pk:,.0f} در برابر "
                                       f"{self.wc_available:,.0f} تومان موجود."})
        base = sum(b["contribution_nominal"] for b in self.monthly)
        adv = sum(b["contribution_risk_adjusted"] for b in self.monthly)
        if base > 0 and adv < 0:
            flags.append({"level": "high", "id": "downside_negative",
                          "title_fa": "حاشیه در سناریوی نامساعد منفی می‌شود",
                          "detail_fa": f"اسمی {base:,.0f} در برابر نامساعد {adv:,.0f} تومان."})
        elif base > 0 and adv < 0.5 * base:
            flags.append({"level": "medium", "id": "downside_thin",
                          "title_fa": "حساسیت بالای سود به فرضیات",
                          "detail_fa": f"حاشیه نامساعد {adv / base:.0%} حاشیه اسمی است."})
        beyond = [c.cohort_id for c in self.state.cohorts.values()
                  if c.alive >= 1 and c.mean_weight >
                  float(A.get("price.max_priced_weight_g"))]
        if beyond:
            flags.append({"level": "medium", "id": "beyond_price_curve",
                          "title_fa": "cohort خارج از بازه قیمت‌گذاری",
                          "detail_fa": "برای " + "، ".join(beyond) +
                                       " داده قیمت بالای ۱۵ گرم نداریم؛ قیمت در سقف "
                                       "نگه داشته شده و برون‌یابی نشده است."})
        if getattr(self.state, "unassigned_sales", None):
            n = sum(x["quantity"] for x in self.state.unassigned_sales)
            flags.append({"level": "medium", "id": "unassigned_sales",
                          "title_fa": "فروش‌های تاریخی بدون cohort",
                          "detail_fa": f"{n:,.0f} قطعه هنوز از موجودی زنده کم نشده‌اند؛ "
                                       f"برنامه تا تخصیص cohort محافظه‌کارانه است."})
        return flags

    # ------------------------------------------------------------ export
    def as_record(self) -> dict:
        return {"summary": self.summary(), "monthly": self.monthly,
                "lots": self.solution.chosen_lots}


def scenario_comparison(A, bio, state, variant: str = "balanced",
                        scenarios: list | None = None) -> list:
    """
    مقایسه سناریوهای حجم سالانه تخم (۲.۰M / ۲.۵M / ۳.۰M).
    عرضه واقعی ماهانه همچنان محدودکننده است، پس سناریوی بالاتر لزوماً
    به همان اندازه خرید نمی‌کند.
    """
    scen = scenarios or [float(x) for x in A.get("egg.annual_scenarios")]
    out = []
    original = A.get("planning.annual_scenario")
    try:
        for s in scen:
            A.set("planning.annual_scenario", s) if A.db else None
            if not A.db:
                A.defs["planning.annual_scenario"]["value"] = s
            p = Plan(A, bio, state, variant)
            su = p.summary()
            out.append({
                "scenario": s,
                "eggs_planned": su["eggs_planned"],
                "binding": su["eggs_planned"] < s - 1,
                "fish_to_sell": su["fish_to_sell"],
                "revenue": su["revenue_total"],
                "contribution_nominal": su["contribution_nominal"],
                "contribution_risk_adjusted": su["contribution_risk_adjusted"],
                "peak_ponds": su["peak_ponds"],
                "peak_capital": su["peak_capital"],
                "pond_breach": su["pond_breach"],
            })
    finally:
        if A.db:
            A.set("planning.annual_scenario", original)
        else:
            A.defs["planning.annual_scenario"]["value"] = original
    return out


def variant_comparison(A, bio, state) -> list:
    """سه برنامه مدیریتی: بیشینه سود / متعادل / محافظه‌کارانه."""
    out = []
    for name in ("max_profit", "balanced", "conservative"):
        p = Plan(A, bio, state, name)
        su = p.summary()
        out.append({
            "variant": name, "label_fa": su["variant_label_fa"],
            "eggs_planned": su["eggs_planned"],
            "lots_planned": su["lots_planned"],
            "fish_to_sell": su["fish_to_sell"],
            "revenue": su["revenue_total"],
            "contribution_nominal": su["contribution_nominal"],
            "contribution_risk_adjusted": su["contribution_risk_adjusted"],
            "peak_ponds": su["peak_ponds"],
            "peak_ponds_adverse": su["peak_ponds_adverse"],
            "peak_capital": su["peak_capital"],
            "wc_headroom": su["wc_headroom"],
            "risk_flags": len(p.risk_flags()),
        })
    return out
