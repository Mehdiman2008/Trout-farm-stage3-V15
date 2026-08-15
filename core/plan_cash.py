"""
plan_cash.py — دفتر نقدی برنامه‌ریزی‌شده و سرمایه در گردش واقعی
================================================================
سرمایه در گردش را از یک تقریب inventory حساب نمی‌کنیم. پس از حل مدل، یک
دفتر نقدی کامل هفته‌به‌هفته ساخته می‌شود:

    پرداخت تخم · پرداخت خوراک · هزینه ثابت · هزینه‌های جانبی
    دریافت وجه فروش طبق شرایط پرداخت مشتری (۵۰٪ نقد، ۵۰٪ حدود ۴۵ روز بعد)

تاریخ **شناسایی درآمد** با تاریخ **دریافت وجه** یکی نیست؛ همین تفاوت است
که اوج نیاز به تأمین مالی را می‌سازد.

از روی این دفتر محاسبه می‌شود:
    کمترین مانده نقدی · اوج کسری نقدی · اوج نیاز تأمین مالی بیرونی
    اوج سرمایه در موجودی · چرخه تبدیل وجه نقد · بازده روی اوج سرمایه در گردش
"""
from __future__ import annotations

from datetime import date as _date_cls, timedelta


def _as_date(x):
    if isinstance(x, _date_cls):
        return x
    return _date_cls.fromisoformat(str(x)[:10])


class PlannedCashLedger:
    def __init__(self, plan, extra_rows: list | None = None):
        """
        extra_rows: جریان‌های نقدی خارج از برنامه — مثلاً وجه یک آفر فروش
        فرضی، یا جابه‌جایی زمان پرداخت تخم بر اساس شرایط تأمین‌کننده.
        هر ردیف: {date | week, amount (+ورود / −خروج), type, label}
        """
        self.plan = plan
        self.A = plan.A
        self.grid = plan.grid
        self.weeks = plan.model.weeks
        self.extra_rows = list(extra_rows or [])
        self.upfront = float(self.A.get("finance.customer_upfront_share"))
        self.delay = int(self.A.get("finance.customer_balance_delay_days"))
        self.supplier_credit = int(self.A.get("finance.supplier_credit_days"))
        self.opening = float(self.A.get("finance.opening_cash"))
        self.wc_available = plan.wc_available
        self._build()

    # ------------------------------------------------------------- build
    def _build(self):
        g, p = self.grid, self.plan
        W = self.weeks
        out_ = [0.0] * (W + 1)      # خروج نقدی
        in_ = [0.0] * (W + 1)       # ورود نقدی
        rev = [0.0] * (W + 1)       # درآمد شناسایی‌شده (نه نقدی)
        rows = []

        def shift(t: int, days: int) -> int:
            return min(W, g.index_of(g.dates[t] + timedelta(days=days)))

        for t in range(W + 1):
            if p.egg_cost[t]:
                k = shift(t, self.supplier_credit)
                out_[k] += p.egg_cost[t]
                rows.append({"week": k, "date": g.dates[k].isoformat(),
                             "amount": -p.egg_cost[t], "type": "egg",
                             "label": "پرداخت تخم", "accrual": g.dates[t].isoformat()})
            if p.feed_cost[t]:
                out_[t] += p.feed_cost[t]
                rows.append({"week": t, "date": g.dates[t].isoformat(),
                             "amount": -p.feed_cost[t], "type": "feed",
                             "label": "پرداخت خوراک", "accrual": g.dates[t].isoformat()})
            if p.fixed_cost[t]:
                out_[t] += p.fixed_cost[t]
                rows.append({"week": t, "date": g.dates[t].isoformat(),
                             "amount": -p.fixed_cost[t], "type": "fixed_cost",
                             "label": "هزینه ثابت و جانبی",
                             "accrual": g.dates[t].isoformat()})
            if p.revenue[t]:
                rev[t] += p.revenue[t]
                up = p.revenue[t] * self.upfront
                bal = p.revenue[t] - up
                if up:
                    in_[t] += up
                    rows.append({"week": t, "date": g.dates[t].isoformat(),
                                 "amount": +up, "type": "receipt_upfront",
                                 "label": f"دریافت نقدی فروش ({self.upfront:.0%})",
                                 "accrual": g.dates[t].isoformat()})
                if bal:
                    k = shift(t, self.delay)
                    in_[k] += bal
                    rows.append({"week": k, "date": g.dates[k].isoformat(),
                                 "amount": +bal, "type": "receipt_balance",
                                 "label": f"دریافت باقیمانده (+{self.delay} روز)",
                                 "accrual": g.dates[t].isoformat()})

        # ---- جریان‌های فرضی/بیرونی (آفر فروش، شرایط پرداخت خاص یک آفر تخم)
        for xr in self.extra_rows:
            t = int(xr["week"]) if xr.get("week") is not None else \
                g.index_of(_as_date(xr["date"]))
            t = max(0, min(W, t))
            amt = float(xr.get("amount") or 0.0)
            if amt >= 0:
                in_[t] += amt
            else:
                out_[t] += -amt
            rows.append({"week": t, "date": g.dates[t].isoformat(), "amount": amt,
                         "type": xr.get("type", "hypothetical"),
                         "label": xr.get("label", "جریان فرضی"),
                         "accrual": g.dates[t].isoformat()})

        self.inflow, self.outflow, self.revenue_recognised = in_, out_, rev
        self.rows = sorted(rows, key=lambda r: (r["week"], r["type"]))

        bal = self.opening
        series = []
        for t in range(W + 1):
            bal += in_[t] - out_[t]
            series.append({
                "week": t, "date": g.dates[t].isoformat(),
                "inflow": in_[t], "outflow": out_[t], "balance": bal,
                "revenue_recognised": rev[t],
                "receivables": self._receivables_at(t),
                "inventory_capital": p.capital[t],
                # نیاز به تأمین مالی بیرونی = کسری نقدی. سرمایه در موجودی
                # اینجا اضافه نمی‌شود؛ همان پول قبلاً از حساب خارج شده و در
                # مانده نقدی دیده می‌شود. جمع کردن این دو یعنی دوباره‌شماری.
                "funding_required": max(0.0, -bal),
            })
        self.series = series

    def _receivables_at(self, t: int) -> float:
        """وجه فروش شناسایی‌شده که هنوز دریافت نشده است."""
        g, p = self.grid, self.plan
        acc = 0.0
        for k in range(0, t + 1):
            if not p.revenue[k]:
                continue
            due = g.index_of(g.dates[k] + timedelta(days=self.delay))
            if due > t:
                acc += p.revenue[k] * (1.0 - self.upfront)
        return acc

    # ----------------------------------------------------------- metrics
    def metrics(self) -> dict:
        s = self.series
        bals = [r["balance"] for r in s]
        min_bal = min(bals) if bals else self.opening
        min_i = bals.index(min_bal) if bals else 0
        peak_fund = max((r["funding_required"] for r in s), default=0.0)
        peak_i = max(range(len(s)), key=lambda i: s[i]["funding_required"]) if s else 0
        peak_inv = max((r["inventory_capital"] for r in s), default=0.0)
        total_in = sum(self.inflow)
        total_out = sum(self.outflow)
        net = total_in - total_out
        ccc = self._ccc()
        return {
            "opening_cash": self.opening,
            "closing_balance": bals[-1] if bals else self.opening,
            "minimum_cash_balance": min_bal,
            "minimum_cash_date": s[min_i]["date"] if s else None,
            "peak_cash_deficit": max(0.0, -min_bal),
            "peak_funding_requirement": peak_fund,
            "peak_funding_date": s[peak_i]["date"] if s else None,
            "peak_inventory_capital": peak_inv,
            "peak_capital_employed": max(
                (r["funding_required"] + r["inventory_capital"] for r in s),
                default=0.0),
            "peak_receivables": max((r["receivables"] for r in s), default=0.0),
            "total_inflow": total_in,
            "total_outflow": total_out,
            "net_cash": net,
            "cash_conversion_cycle_days": ccc,
            "return_on_peak_wc": (net / peak_fund) if peak_fund > 0 else None,
            "wc_available": self.wc_available,
            "wc_headroom": self.wc_available - peak_fund,
            "wc_breach": peak_fund > self.wc_available,
            "upfront_share": self.upfront,
            "balance_delay_days": self.delay,
        }

    # --------------------------------------------------------------- NPV
    def npv(self, rate: float | None = None) -> float:
        """
        ارزش امروزِ خالص جریان‌های نقدی برنامه.

        تنزیل ساده و قابل تنظیم (همان نرخ فرصتی که برای شرایط پرداخت به کار
        می‌رود): ضریب = ۱ / (۱ + r × روز/۳۶۵). مدل مالی جدیدی ساخته نشده و
        micro-discounting روزانه انجام نمی‌شود؛ فقط «فروش امروز» با «فروش
        چند ماه بعد» هم‌ارز می‌شود.

        مانده نقدی ابتدای دوره وارد نمی‌شود چون در همه سناریوها یکسان است.
        """
        r = float(self.A.get("offers.opportunity_rate_annual") if rate is None else rate)
        g = self.grid
        d0 = g.dates[0]
        acc = 0.0
        for t in range(self.weeks + 1):
            net = self.inflow[t] - self.outflow[t]
            if not net:
                continue
            days = (g.dates[t] - d0).days
            acc += net / (1.0 + r * days / 365.0)
        return acc

    def _ccc(self):
        """از اولین پرداخت تخم تا آخرین دریافت وجه فروش."""
        pays = [r for r in self.rows if r["type"] == "egg"]
        recs = [r for r in self.rows if r["type"].startswith("receipt")]
        if not pays or not recs:
            return None
        from .state import d
        return (d(recs[-1]["date"]) - d(pays[0]["date"])).days

    def by_month(self) -> list:
        g = self.grid
        out, order = {}, []
        for r in self.series:
            k = g.month_of(r["week"])
            if k not in out:
                out[k] = {"key": k, "inflow": 0.0, "outflow": 0.0,
                          "revenue_recognised": 0.0, "closing_balance": 0.0,
                          "min_balance": r["balance"], "peak_funding": 0.0}
                order.append(k)
            b = out[k]
            b["inflow"] += r["inflow"]
            b["outflow"] += r["outflow"]
            b["revenue_recognised"] += r["revenue_recognised"]
            b["closing_balance"] = r["balance"]
            b["min_balance"] = min(b["min_balance"], r["balance"])
            b["peak_funding"] = max(b["peak_funding"], r["funding_required"])
        for b in out.values():
            b["net_cash"] = b["inflow"] - b["outflow"]
            b["cash_vs_revenue_gap"] = b["revenue_recognised"] - b["inflow"]
        return [out[k] for k in order]
