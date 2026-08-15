"""
forecast.py — پیش‌بینی رو به جلو از روی Live Farm State
========================================================
همه چیز از وضعیت واقعی همین لحظه شروع می‌شود، نه از یک baseline ثابت.
هیچ داده تاریخی اینجا نوشته یا تغییر داده نمی‌شود.

خروجی‌ها:
  * منحنی ظرفیت مورد نیاز استخر روز‌به‌روز (Fix 2: تقویم واقعی، بدون modulo)
  * checkpoint های 30/60/90/140 روز
  * milestoneهای پیش‌روی هر cohort (۱، ۲، ۵، ۱۰، ۱۵ گرم)
  * سهمی از هر cohort که زودتر/دیرتر به وزن فروش می‌رسد (growth heterogeneity)
  * نیاز خوراک و درآمد بالقوه
"""
from __future__ import annotations

from datetime import timedelta

from .biology import MILESTONE_WEIGHTS
from .state import _age


class Forecast:
    def __init__(self, A, bio, state):
        self.A, self.bio, self.state = A, bio, state
        self.horizon = int(A.get("run.timeline_horizon_days"))
        self.assume_harvest = bool(A.get("plan.assume_harvest_in_forecast"))
        self.harvest_w = float(A.get("plan.baseline_harvest_weight_g"))
        # cohortهایی که همین حالا از وزن برداشت عبور کرده‌اند: در forecast
        # فرض می‌شود فروخته شده‌اند، وگرنه منحنی ظرفیت بی‌معنا بزرگ می‌شود.
        self.excluded = [c.cohort_id for c in state.cohorts.values()
                         if c.alive >= 1 and self.assume_harvest
                         and c.mean_weight >= self.harvest_w]

    def _harvest_date(self, c):
        """تاریخی که cohort به وزن پایه برداشت می‌رسد (فرض فروش)."""
        age = self.bio.age_at_weight(self.harvest_w) - c.growth_offset_days
        return c.purchase_date + timedelta(days=int(round(age)))

    def _on_farm(self, c, day) -> bool:
        if not self.assume_harvest:
            return True
        return day <= self._harvest_date(c)

    # -------------------------------------------------- capacity forecast
    def capacity_curve(self, step: int = 2) -> list:
        st, bio = self.state, self.bio
        out = []
        for k in range(0, self.horizon + 1, step):
            day = st.as_of + timedelta(days=k)
            need, fish, biomass = 0.0, 0.0, 0.0
            for c in st.cohorts.values():
                if c.alive < 1 or not self._on_farm(c, day):
                    continue
                n = st.alive_at(c, day)
                w = min(st.weight_of(c, day), self.harvest_w) if self.assume_harvest \
                    else st.weight_of(c, day)
                need += bio.ponds_required(n, w)
                fish += n
                biomass += n * w / 1000.0
            out.append({"date": day.isoformat(), "day_offset": k,
                        "ponds_required": need, "fish": fish,
                        "biomass_kg": biomass})
        return out

    def checkpoints(self) -> list:
        pts = [int(x) for x in self.A.get("run.forecast_checkpoints_days")]
        curve = {c["day_offset"]: c for c in self.capacity_curve(step=1)}
        op = int(self.A.get("farm.operational_ponds"))
        out = []
        for p in pts:
            c = curve.get(min(p, self.horizon))
            if not c:
                continue
            out.append({"days": p, "date": c["date"],
                        "ponds_required": c["ponds_required"],
                        "operational_ponds": op,
                        "headroom": op - c["ponds_required"],
                        "breach": c["ponds_required"] > op,
                        "fish": c["fish"], "biomass_kg": c["biomass_kg"]})
        return out

    def peak_pressure(self) -> dict:
        curve = self.capacity_curve(step=1)
        op = int(self.A.get("farm.operational_ponds"))
        if not curve:
            return {}
        peak = max(curve, key=lambda r: r["ponds_required"])
        breaches = [r for r in curve if r["ponds_required"] > op]
        return {"peak_ponds_required": peak["ponds_required"],
                "peak_date": peak["date"],
                "operational_ponds": op,
                "assume_harvest": self.assume_harvest,
                "harvest_weight_g": self.harvest_w,
                "excluded_cohorts": self.excluded,
                "first_breach_date": breaches[0]["date"] if breaches else None,
                "breach_days": len(breaches)}

    # ------------------------------------------------------- milestones
    def milestones(self) -> list:
        st, bio = self.state, self.bio
        out = []
        for c in sorted(st.cohorts.values(), key=lambda x: x.purchase_date):
            if c.alive < 1:
                continue
            cur_w = c.mean_weight
            for mw in MILESTONE_WEIGHTS:
                target_age = bio.age_at_weight(mw) - c.growth_offset_days
                dt = c.purchase_date + timedelta(days=int(round(target_age)))
                days = (dt - st.as_of).days
                if days < -400:
                    continue
                n = st.alive_at(c, dt) if days > 0 else c.alive
                out.append({
                    "cohort_id": c.cohort_id, "weight_g": mw,
                    "date": dt.isoformat(), "days_from_now": days,
                    "passed": cur_w >= mw,
                    "fish": n,
                    "ponds_required": bio.ponds_required(n, mw),
                    "pct_already_above": bio.fraction_above(
                        cur_w, mw, st.cv_of(c, cur_w)),
                    "potential_revenue": n * bio.sale_price(mw),
                })
        out.sort(key=lambda r: r["date"])
        return out

    def upcoming_milestones(self, limit: int = 12) -> list:
        return [m for m in self.milestones() if not m["passed"]][:limit]

    # --------------------------------------------------------- timeline
    def cohort_timeline(self) -> list:
        """
        برای هر cohort: تاریخ رسیدن به هر milestone، برای رسم Gantt.
        گروه کند/معمول/تند هم برآورد می‌شود (growth heterogeneity).
        """
        st, bio = self.state, self.bio
        ql = float(self.A.get("heterogeneity.slow_quantile"))
        qh = float(self.A.get("heterogeneity.fast_quantile"))
        rows = []
        for c in sorted(st.cohorts.values(), key=lambda x: x.purchase_date):
            if c.alive < 1:
                continue
            cv = st.cv_of(c)
            marks = []
            for mw in MILESTONE_WEIGHTS:
                base_age = bio.age_at_weight(mw) - c.growth_offset_days
                # زمان رسیدن گروه تند/کند: با نگاشت وزن چندک به سن معادل
                w_ratio_fast = bio.weight_quantile(mw, qh, cv) / mw
                w_ratio_slow = bio.weight_quantile(mw, ql, cv) / mw
                age_fast = bio.age_at_weight(mw / max(w_ratio_fast, 1e-6)) - c.growth_offset_days
                age_slow = bio.age_at_weight(mw / max(w_ratio_slow, 1e-6)) - c.growth_offset_days
                mk = lambda a: (c.purchase_date + timedelta(days=int(round(a)))).isoformat()
                marks.append({"weight_g": mw, "date": mk(base_age),
                              "date_fast": mk(age_fast), "date_slow": mk(age_slow),
                              "passed": c.mean_weight >= mw})
            rows.append({
                "cohort_id": c.cohort_id,
                "purchase_date": c.purchase_date.isoformat(),
                "age_days": _age(c.purchase_date, st.as_of),
                "alive": c.alive,
                "mean_weight_g": c.mean_weight,
                "cv": cv,
                "end_date": marks[-1]["date_slow"],
                "marks": marks,
            })
        return rows

    # ------------------------------------------------------------- feed
    def feed_outlook(self, days: int = 90) -> dict:
        st, bio = self.state, self.bio
        need = {}
        total_kg, total_cost = 0.0, 0.0
        for k in range(days):
            day = st.as_of + timedelta(days=k)
            nxt = day + timedelta(days=1)
            for c in st.cohorts.values():
                if c.alive < 1 or not self._on_farm(c, day):
                    continue
                n0 = st.alive_at(c, day)
                w0 = st.weight_of(c, day)
                w1 = st.weight_of(c, nxt)
                kg = bio.feed_kg_for_growth(n0, w0, w1)
                band = bio.feed_name(w0)
                need[band] = need.get(band, 0.0) + kg
                total_kg += kg
                total_cost += kg * bio.feed_price(w0)
        stock = {k: v["qty_kg"] for k, v in st.feed.items()}
        return {"days": days, "by_feed_kg": need, "total_kg": total_kg,
                "total_cost": total_cost, "current_stock_kg": stock,
                "shortfall_kg": max(0.0, total_kg - sum(stock.values()))}

    # ----------------------------------------------------------- revenue
    def revenue_outlook(self) -> dict:
        """ارزش بالقوه موجودی زنده اگر همه تا ۱۵ گرم نگه داشته شوند."""
        st, bio = self.state, self.bio
        rev, cost = 0.0, 0.0
        for c in st.cohorts.values():
            if c.alive < 1:
                continue
            age15 = bio.age_at_weight(15.0) - c.growth_offset_days
            dt = c.purchase_date + timedelta(days=int(round(age15)))
            n = st.alive_at(c, dt)
            rev += n * bio.sale_price(15.0)
            cost += bio.feed_cost_per_gram_gain(c.mean_weight, 15.0) * \
                (15.0 - c.mean_weight) * ((n + c.alive) / 2)
        months = self.horizon / 30.4
        fixed = float(self.A.get("cost.fixed_monthly")) * months
        return {"potential_revenue_at_15g": rev,
                "remaining_feed_cost": cost,
                "fixed_cost_over_horizon": fixed,
                "gross_margin_estimate": rev - cost - fixed}
