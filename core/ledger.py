"""
ledger.py — دفتر نقدی و سرمایه در گردش  (Fix 4)
=================================================
سرمایه در گردش از یک متغیر آزاد optimizer استنتاج نمی‌شود.
بعد از ساخت state، یک دفتر نقدی واقعی از روی تراکنش‌ها ساخته می‌شود:

  خروج نقد : خرید تخم، خرید خوراک، هزینه ثابت ماهانه، هزینه عملیاتی، payment
  ورود نقد : فروش (با تأخیر دریافت)، receipt

سپس مستقیماً محاسبه می‌شوند:
  minimum cash balance · peak cash deficit · peak funding requirement
  peak inventory capital · cash conversion cycle · return on peak working capital

Timeline کاملاً غیرچرخه‌ای و بر مبنای تاریخ واقعی است (Fix 2) — هیچ modulo-52.
"""
from __future__ import annotations

from datetime import date, timedelta

from .state import d


def _month_starts(a: date, b: date):
    y, m = a.year, a.month
    cur = date(y, m, 1)
    while cur <= b:
        if cur >= a:
            yield cur
        m += 1
        if m > 12:
            m = 1
            y += 1
        cur = date(y, m, 1)


class CashLedger:
    def __init__(self, db, A, bio, state):
        self.db, self.A, self.bio, self.state = db, A, bio, state
        self.fixed_mode = str(A.get("cost.fixed_cost_mode", "top_up"))
        self.upfront_share = float(A.get("finance.customer_upfront_share"))
        self.balance_delay = int(A.get("finance.customer_balance_delay_days"))
        self.rows = []
        self.build()

    # ---------------------------------------------------------------- build
    def build(self):
        A = self.A
        sup_days = int(A.get("finance.supplier_credit_days"))
        cus_days = int(A.get("finance.customer_payment_delay_days"))
        rows = []

        explicit_payment = any(t["txn_type"] in ("payment", "receipt")
                               for t in self.db.active_txns())

        # هزینه‌های عملیاتی واقعی ثبت‌شده، به تفکیک ماه — برای جلوگیری از
        # double counting با هزینه ثابت پایه (اصلاح ۴)
        self.actual_cost_by_month: dict[str, float] = {}
        self.cost_by_category: dict[str, float] = {}
        for t in self.db.active_txns():
            if t["txn_type"] == "operating_cost" and d(t["txn_date"]) <= self.state.as_of:
                a = float(t.get("amount") or 0)
                self.actual_cost_by_month[t["txn_date"][:7]] = \
                    self.actual_cost_by_month.get(t["txn_date"][:7], 0.0) + a
                cat = t.get("category") or "سایر"
                self.cost_by_category[cat] = self.cost_by_category.get(cat, 0.0) + a

        for t in self.db.active_txns():
            td = d(t["txn_date"])
            amt = float(t.get("amount") or 0)
            qty = float(t.get("quantity") or 0)
            up = float(t.get("unit_price") or 0)
            if not amt and qty and up:
                amt = qty * up
            typ = t["txn_type"]
            if typ == "operating_cost" and self.fixed_mode == "baseline_only":
                continue          # هزینه واقعی داخل baseline دیده می‌شود
            if typ == "egg_purchase":
                rows.append(self._row(td + timedelta(days=sup_days), -amt,
                                      "خرید تخم", t, accrual=td))
            elif typ == "feed_purchase":
                rows.append(self._row(td + timedelta(days=sup_days), -amt,
                                      "خرید خوراک", t, accrual=td))
            elif typ == "operating_cost":
                rows.append(self._row(td, -amt, "هزینه عملیاتی", t, accrual=td))
            elif typ == "sale":
                # شناسایی درآمد در تاریخ فروش، ولی دریافت وجه طبق شرایط
                # پرداخت مشتری: بخشی نقد و باقیمانده با تأخیر.
                # اگر پرداخت واقعی ثبت شده باشد، همان اولویت دارد.
                if explicit_payment:
                    continue
                for when, part, label in self._receipt_schedule(td, amt):
                    rows.append(self._row(when, +part, label, t, accrual=td))
            elif typ == "payment" and explicit_payment:
                rows.append(self._row(td, -amt, "پرداخت", t, accrual=td))
            elif typ == "receipt" and explicit_payment:
                rows.append(self._row(td, +amt, "دریافت", t, accrual=td))

        # هزینه ثابت ماهانه (farm-level) — نرخ هر ماه با تاریخ اعتبار خودش
        # خوانده می‌شود تا تغییر بعدی هزینه retroactive نشود (اصلاح ۶).
        if self.fixed_mode != "actual_only":
            start = d(A.get("finance.fixed_cost_start_date"))
            first_txn = min([d(t["txn_date"]) for t in self.db.active_txns()],
                            default=self.state.as_of)
            start = min(start, first_txn)
            for ms in _month_starts(start, self.state.as_of):
                monthly = float(A.get_at("cost.fixed_monthly", ms))
                actual = self.actual_cost_by_month.get(ms.isoformat()[:7], 0.0)
                if self.fixed_mode == "top_up":
                    charge = max(0.0, monthly - actual)
                    label = ("هزینه ثابت ماهانه (مابه‌التفاوت پس از هزینه واقعی)"
                             if actual > 0 else "هزینه ثابت ماهانه")
                else:                                   # baseline_only
                    charge, label = monthly, "هزینه ثابت ماهانه (پایه)"
                if charge <= 0:
                    continue
                rows.append({"date": ms.isoformat(), "amount": -charge,
                             "label": label, "type": "fixed_cost",
                             "txn_id": None, "accrual": ms.isoformat()})

        rows.sort(key=lambda r: r["date"])
        self.rows = rows

    @staticmethod
    def _row(when: date, amount: float, label: str, t, accrual: date):
        return {"date": when.isoformat(), "amount": amount, "label": label,
                "type": t["txn_type"], "txn_id": t["id"],
                "accrual": accrual.isoformat(),
                "counterparty": t.get("counterparty")}

    # -------------------------------------------------------------- series
    def balance_series(self) -> list:
        bal = float(self.A.get("finance.opening_cash"))
        out, cur, acc = [], None, 0.0
        for r in self.rows:
            if cur is None:
                cur = r["date"]
            if r["date"] != cur:
                out.append({"date": cur, "balance": bal, "flow": acc})
                cur, acc = r["date"], 0.0
            bal += r["amount"]
            acc += r["amount"]
        if cur is not None:
            out.append({"date": cur, "balance": bal, "flow": acc})
        return out

    # ------------------------------------------------------------ metrics
    def metrics(self) -> dict:
        ser = self.balance_series()
        balances = [s["balance"] for s in ser] or [0.0]
        min_bal = min(balances)
        peak_deficit = -min(0.0, min_bal)

        inflow = sum(r["amount"] for r in self.rows if r["amount"] > 0)
        outflow = -sum(r["amount"] for r in self.rows if r["amount"] < 0)

        # سرمایه قفل‌شده در موجودی زنده (inventory capital)
        inv_capital = 0.0
        for c in self.state.cohorts.values():
            share = (c.alive / c.egg_count) if c.egg_count else 0.0
            inv_capital += c.egg_count * c.egg_price * share
            inv_capital += self.state._est_feed_cost(c) * share
        feed_value = sum(f["value"] for f in self.state.feed.values())

        # ---- سه عدد مجزای سرمایه در گردش (اصلاح ۱) --------------------
        # A) موجود/فرضی: یک ورودی قابل تغییر در فرضیات، نه خروجی optimizer
        wc_available = float(self.A.get("finance.working_capital_available"))
        # B) قفل‌شده هم‌اکنون: ارزش سرمایه‌ای که همین حالا داخل عملیات گیر
        #    کرده است = بهای تمام‌شده موجودی زنده + ارزش انبار خوراک.
        #    کسری نقدی اینجا اضافه نمی‌شود؛ آن یک شاخص «تأمین مالی» است و
        #    جداگانه گزارش می‌شود، وگرنه همان دارایی دوبار شمرده می‌شود.
        tied_up = inv_capital + feed_value
        # C) اوج پیش‌بینی‌شده موردنیاز: از projection رو به جلو
        fwd = self.forward_projection()
        peak_wc = max(peak_deficit, inv_capital + feed_value, fwd["peak_requirement"])
        realised = inflow - outflow
        ret_on_wc = (realised / peak_wc) if peak_wc > 0 else None

        # cash conversion cycle (ساده): از پرداخت تخم تا دریافت وجه فروش
        ccc = None
        sales = [r for r in self.rows if r["type"] == "sale"]
        eggs = [r for r in self.rows if r["type"] == "egg_purchase"]
        if sales and eggs:
            ccc = (d(sales[-1]["date"]) - d(eggs[0]["date"])).days

        return {
            "opening_cash": float(self.A.get("finance.opening_cash")),
            "closing_balance": balances[-1],
            "minimum_cash_balance": min_bal,
            "peak_cash_deficit": peak_deficit,
            "peak_funding_requirement": peak_deficit,
            "inventory_capital": inv_capital,
            "feed_inventory_value": feed_value,
            "peak_working_capital": peak_wc,
            "total_inflow": inflow,
            "total_outflow": outflow,
            "realised_net_cash": realised,
            "return_on_peak_wc": ret_on_wc,
            "cash_conversion_cycle_days": ccc,

            # --- سه عدد مجزای سرمایه در گردش (اصلاح ۱) ---
            "wc_available": wc_available,
            "wc_tied_up_now": tied_up,
            "wc_forecast_peak": fwd["peak_requirement"],
            "wc_forecast_peak_date": fwd["peak_date"],
            "wc_forecast_days": fwd["days"],
            "wc_headroom": wc_available - max(tied_up, fwd["peak_requirement"]),
            "wc_breach": max(tied_up, fwd["peak_requirement"]) > wc_available,
            "forward": fwd,

            # --- هزینه‌های واقعی ثبت‌شده (اصلاح ۴) ---
            "fixed_cost_mode": self.fixed_mode,
            "actual_operating_cost_total": sum(self.actual_cost_by_month.values()),
            "cost_by_category": self.cost_by_category,
        }

    def _receipt_schedule(self, sale_date, amount: float) -> list:
        """
        زمان‌بندی دریافت وجه یک فروش.

        منطق پایه: بخشی (پیش‌فرض ۵۰٪) در تاریخ فروش و باقیمانده حدود ۴۵ روز
        بعد. تاریخ شناسایی درآمد همان تاریخ فروش می‌ماند و از تاریخ دریافت
        وجه جداست.
        """
        up = max(0.0, min(1.0, self.upfront_share))
        out = []
        if up > 0:
            out.append((sale_date, amount * up, "دریافت نقدی فروش"))
        if up < 1.0:
            out.append((sale_date + timedelta(days=self.balance_delay),
                        amount * (1.0 - up),
                        f"دریافت باقیمانده فروش (+{self.balance_delay} روز)"))
        return out

    # ------------------------------------------- projection رو به جلو (۱)
    def forward_projection(self, days: int | None = None) -> dict:
        """
        سرمایه در گردش موردنیاز آینده را از وضعیت واقعی همین لحظه بازمحاسبه
        می‌کند — نه از یک عدد ثابت. هر تغییر در cohortها، خریدها، فروش‌ها،
        هزینه‌ها یا زمان‌بندی پرداخت این عدد را جابه‌جا می‌کند.

        جریان‌های در نظر گرفته‌شده:
          خروج : خوراک موردنیاز رشد، هزینه ثابت ماهانه، تعهدات پرداخت‌نشده
          ورود : فروش مورد انتظار در وزن برداشت پایه، دریافت‌های معوق

        این یک plan بهینه نیست (مرحله ۲)؛ فقط projection سیاست پایه است.
        """
        A, st, bio = self.A, self.state, self.bio
        days = int(days or A.get("finance.wc_forecast_days"))
        as_of = st.as_of
        cus_days = int(A.get("finance.customer_payment_delay_days"))
        harvest_w = float(A.get("plan.baseline_harvest_weight_g"))
        assume_harvest = bool(A.get("plan.assume_harvest_in_forecast"))

        flows: dict[str, float] = {}

        def add(when: date, amount: float):
            if when < as_of:
                when = as_of
            flows[when.isoformat()] = flows.get(when.isoformat(), 0.0) + amount

        # ۱) جریان‌های ثبت‌شده‌ای که هنوز سررسید نشده‌اند (payment terms)
        for r in self.rows:
            if r["date"] > as_of.isoformat():
                add(d(r["date"]), r["amount"])

        # ۲) خوراک و فروش هر cohort، روزبه‌روز
        for c in st.cohorts.values():
            if c.alive < 1:
                continue
            harvested = False
            for k in range(1, days + 1):
                day = as_of + timedelta(days=k)
                if harvested:
                    break
                n0 = st.alive_at(c, day - timedelta(days=1))
                w0 = st.weight_of(c, day - timedelta(days=1))
                w1 = st.weight_of(c, day)
                if assume_harvest and w1 >= harvest_w:
                    n = st.alive_at(c, day)
                    gross = n * bio.sale_price(min(w1, harvest_w))
                    for when, part, _lbl in self._receipt_schedule(day, gross):
                        add(when, +part)
                    harvested = True
                    continue
                kg = bio.feed_kg_for_growth(n0, w0, w1)
                if kg > 0:
                    add(day, -kg * bio.feed_price(w0))

        # ۳) هزینه ثابت ماهانه آینده (با نرخ effective در همان ماه)
        if self.fixed_mode != "actual_only":
            for ms in _month_starts(as_of + timedelta(days=1), as_of + timedelta(days=days)):
                add(ms, -float(A.get_at("cost.fixed_monthly", ms)))

        # ۴) اجرای projection
        bal = self.balance_series()[-1]["balance"] if self.rows else \
            float(A.get("finance.opening_cash"))
        opening = bal
        series, worst, worst_date = [], bal, as_of.isoformat()
        for key in sorted(flows):
            bal += flows[key]
            series.append({"date": key, "balance": bal, "flow": flows[key]})
            if bal < worst:
                worst, worst_date = bal, key
        stock_capital = 0.0
        for c in st.cohorts.values():
            share = (c.alive / c.egg_count) if c.egg_count else 0.0
            stock_capital += c.egg_count * c.egg_price * share
            stock_capital += st._est_feed_cost(c) * share
        peak_req = max(0.0, -worst) + stock_capital
        return {
            "days": days,
            "opening_balance": opening,
            "closing_balance": bal,
            "worst_balance": worst,
            "worst_date": worst_date,
            "peak_requirement": peak_req,
            "peak_date": worst_date,
            "stock_capital_component": stock_capital,
            "cash_component": max(0.0, -worst),
            "series": series[:400],
        }
