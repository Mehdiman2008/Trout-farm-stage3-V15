#!/usr/bin/env python3
"""
app.py — سرور داشبورد مزرعه قزل‌آلا
====================================
بدون وابستگی سنگین: فقط کتابخانه استاندارد پایتون + PyYAML (+ openpyxl برای فایل ارز).

    python3 app.py            # http://127.0.0.1:8000
    python3 app.py --port 8080 --seed-demo

هر درخواست، وضعیت را از تاریخچه تراکنش‌ها بازسازی می‌کند؛ هیچ state ای
در حافظه کش نمی‌شود تا نتیجه همیشه reproducible باشد.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import traceback
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.assumptions import Assumptions            # noqa: E402
from core.biology import Biology                    # noqa: E402
from core.db import DB, DEFAULT_DB, TXN_TYPES, UNIT_OF  # noqa: E402
from core.forecast import Forecast                  # noqa: E402
from core.fx import FXBenchmark, load_fx_into_db, _quarter_of  # noqa: E402
from core.fx_service import FXService                         # noqa: E402
from core.ledger import CashLedger                  # noqa: E402
from core.seed import (backfill_observed_weights,            # noqa: E402
                       clear_demo, seed_demo, seed_observed,
                       seed_observed_sales)
from core.planner import (Plan, scenario_comparison,      # noqa: E402
                          variant_comparison)
from core.state import FarmState, d                 # noqa: E402
from core import attribution as ATTR                # noqa: E402
from core import offers as OFFERS                    # noqa: E402
from core import pond_alloc as PALLOC               # noqa: E402
from core import pond_optimize as POPT              # noqa: E402
from core.optimizer import (OptimizerUnavailable,        # noqa: E402
                            check_solver, solver_status)
from core import variance as VAR                    # noqa: E402
from core import validate as V                      # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, "static")


# ====================================================================== core
class Engine:
    """یک facade نازک؛ هر بار state تازه می‌سازد."""

    def __init__(self, db_path=DEFAULT_DB):
        self.db = DB(db_path)
        self.A = Assumptions(self.db)
        self.db.ensure_ponds(int(self.A.get("farm.total_ponds")),
                             int(self.A.get("farm.operational_ponds")),
                             float(self.A.get("farm.pond_volume_m3")))
        seed_observed(self.db, self.A)
        seed_observed_sales(self.db)          # سه فروش تاریخی (اصلاح ۱۰)
        backfill_observed_weights(self.db)    # وزن‌های اعلام‌شده بعدی
        self.fx_load = load_fx_into_db(self.db, self.A)
        self.fx_service = FXService(self.db, self.A)
        # One non-blocking-ish attempt per application day. Failure is recorded
        # and the verified Excel/manual history remains available as fallback.
        self.fx_live_load = self.fx_service.refresh_live(force=False)

    # -------------------------------------------------------------- build
    def ctx(self, as_of: date | None = None):
        self.A.refresh()
        # قیمت‌های effective-dated در تاریخ مرجع خوانده می‌شوند (اصلاح ۶)
        bio = Biology(self.A, on_date=(as_of or date.today()).isoformat())
        st = FarmState(self.db, self.A, bio, as_of)
        return self.A, bio, st

    # -------------------------------------------------- planning (مرحله ۲)
    def plan(self, variant: str = "balanced", as_of: date | None = None):
        """برنامه همیشه از وضعیت واقعی همین لحظه ساخته می‌شود (rolling)."""
        A, bio, st = self.ctx(as_of)
        return Plan(A, bio, st, variant)

    def full(self, as_of: date | None = None):
        A, bio, st = self.ctx(as_of)
        led = CashLedger(self.db, A, bio, st)
        fc = Forecast(A, bio, st)
        return A, bio, st, led, fc

    # ------------------------------------------------------------ capital
    def capital_at(self, when: date) -> float:
        """سرمایه درگیر در مزرعه در یک تاریخ (تخم + خوراک مصرف‌شده + موجودی خوراک)."""
        A, bio, st = self.ctx(when)
        cap = 0.0
        for c in st.cohorts.values():
            share = (c.alive / c.egg_count) if c.egg_count else 0.0
            cap += c.egg_count * c.egg_price * share
            cap += st._est_feed_cost(c) * share
        cap += sum(f["value"] for f in st.feed.values())
        return cap

    def stock_value_at(self, when: date) -> float:
        A, bio, st = self.ctx(when)
        return sum(c.alive * bio.sale_price(c.mean_weight) for c in st.cohorts.values())

    # ----------------------------------------------------------------- fx
    def fx_block(self, st, led):
        fxb = FXBenchmark(self.db, self.A)
        as_of = st.as_of
        y, q = _quarter_of(as_of)
        q_start = date(y, 3 * (q - 1) + 1, 1)
        cap = self.capital_at(q_start)
        sv0 = self.stock_value_at(q_start)
        sv1 = sum(c.alive * st.bio.sale_price(c.mean_weight)
                  for c in st.cohorts.values()) if hasattr(st, "bio") else None
        net_cash = sum(r["amount"] for r in led.rows
                       if q_start.isoformat() <= r["date"] <= as_of.isoformat())
        farm_change = (self.stock_value_at(as_of) - sv0) + net_cash
        kpi = fxb.current_quarter_kpi(cap, as_of, farm_value_change=farm_change)
        caps = {}
        for k in range(0, 6):
            yy, qq = y, q - k
            while qq <= 0:
                qq += 4
                yy -= 1
            caps[(yy, qq)] = self.capital_at(date(yy, 3 * (qq - 1) + 1, 1))
        return {"kpi": kpi, "quarters": fxb.quarters(caps, as_of, n=6),
                "load": self.fx_load, "live_load": self.fx_live_load,
                "live": FXService(self.db, self.A, as_of).status(),
                "series_tail": fxb.series[-180:]}


ENGINE: Engine | None = None


# =================================================================== routes
def api(path: str, method: str, query: dict, body: dict):
    E = ENGINE
    # Lightweight Railway health check: avoid rebuilding full farm state.
    if path == "/api/health" and method == "GET":
        return {"ok": True, "service": "trout-farm",
                "optimizer": solver_status(),
                "fx_provider": "navasan-api" if os.getenv("NAVASAN_API_KEY") else "historical/manual"}
    if method == "POST" and not path.startswith("/api/decide"):
        # هر نوشتنی (تراکنش، فرضیه، تخصیص و…) می‌تواند برنامه بهینه را عوض
        # کند؛ cache برنامه‌ها پاک می‌شود تا نتیجه همیشه از وضعیت تازه باشد.
        from core.hypothetical import cache_clear
        cache_clear()
    A, bio, st, led, fc = E.full(_as_of(query))

    # ---------------------------------------------------------- bootstrap
    if path == "/api/bootstrap" and method == "GET":
        fxblk = E.fx_block(st, led)
        return {
            "summary": st.summary(),
            "ponds": st.pond_view(),
            "cohorts": st.cohort_view(),
            "feed": list(st.feed.values()),
            "unassigned_sales": st.unassigned_sales,
            "feed_demand": st.daily_feed_demand(),
            "checkpoints": fc.checkpoints(),
            "capacity_curve": fc.capacity_curve(step=2),
            "peak_pressure": fc.peak_pressure(),
            "milestones": fc.upcoming_milestones(),
            "timeline": fc.cohort_timeline(),
            "ledger": led.metrics(),
            "fx": fxblk,
            "reference": bio.milestone_table(),
            "validation": V.run_all(E.db, A, bio, st, led, fc,
                                    FXBenchmark(E.db, A)),
            "meta": {"as_of": st.as_of.isoformat(),
                     "txn_types": TXN_TYPES,
                     "pond_ids": [p["pond_id"] for p in E.db.ponds()],
                     "cohort_ids": [c.cohort_id for c in st.cohorts.values()],
                     "feed_names": [b["name"] for b in bio.feed_bands],
                     "cost_categories": list(A.get("cost.categories")),
                     "units": UNIT_OF,
                     "effective_dated_keys": A.effective_dated_keys(),
                     "optimizer": solver_status(),
                     "optimizer_available": solver_status()["available"],
                     "stage": A.meta.get("stage")},
        }

    if path == "/api/summary":
        return st.summary()
    if path == "/api/ponds":
        return {"ponds": st.pond_view()}
    if path == "/api/cohorts":
        return {"cohorts": st.cohort_view()}
    if path == "/api/feed":
        return {"feed": list(st.feed.values()), "demand": st.daily_feed_demand(),
                "outlook": fc.feed_outlook(90)}
    if path == "/api/forecast":
        return {"checkpoints": fc.checkpoints(), "curve": fc.capacity_curve(step=2),
                "milestones": fc.milestones(), "timeline": fc.cohort_timeline(),
                "peak": fc.peak_pressure(), "revenue": fc.revenue_outlook()}
    if path == "/api/ledger":
        return {"metrics": led.metrics(), "series": led.balance_series(),
                "rows": led.rows[-400:]}
    if path == "/api/fx" and method == "GET":
        return E.fx_block(st, led)
    if path == "/api/fx/refresh" and method == "POST":
        E.fx_live_load = FXService(E.db, A).refresh_live(force=True)
        return {"refresh": E.fx_live_load, "fx": E.fx_block(st, led)}
    if path == "/api/fx/manual" and method == "POST":
        result = FXService(E.db, A).add_manual(
            body.get("date") or date.today().isoformat(), body.get("close_toman"),
            body.get("verified", True))
        return {"manual": result, "fx": E.fx_block(st, led)}
    if path == "/api/solver":
        return check_solver()

    if path == "/api/validate":
        return V.run_all(E.db, A, bio, st, led, fc, FXBenchmark(E.db, A))
    if path == "/api/reference":
        return {"milestones": bio.milestone_table(),
                "growth_curve": [{"day": k, "weight": bio.weight_at_age(k),
                                  "survival": bio.survival(k)}
                                 for k in range(0, 181, 2)]}

    # ------------------------------------------------------- transactions
    if path == "/api/transactions" and method == "GET":
        rows = E.db.q("SELECT * FROM transactions ORDER BY txn_date DESC, id DESC LIMIT 500")
        return {"transactions": rows}

    if path == "/api/transactions" and method == "POST":
        return _add_txn(E, body)

    if path.startswith("/api/transactions/") and path.endswith("/correct") and method == "POST":
        tid = int(path.split("/")[3])
        fields = ("txn_date", "quantity", "weight_g", "unit_price", "amount",
                  "pond_id", "to_pond_id", "counterparty", "note", "txn_type",
                  "cohort_id", "category", "unit")
        patch = {k: v for k, v in body.items() if k in fields}
        for k in ("quantity", "weight_g", "unit_price", "amount"):
            if patch.get(k) not in (None, ""):
                patch[k] = float(patch[k])
            elif k in patch:
                patch[k] = None
        new_id = E.db.correct_txn(tid, reason=body.get("reason"), **patch)
        return {"ok": True, "new_id": new_id, "chain": E.db.txn_chain(new_id)}

    if path.startswith("/api/transactions/") and path.endswith("/history") and method == "GET":
        tid = int(path.split("/")[3])
        return {"chain": E.db.txn_chain(tid)}

    # ------------------------------------------------ ثبت مشاهده استخر (۲)
    if path == "/api/pond/observe" and method == "POST":
        return _pond_observe(E, body, st)

    if path.startswith("/api/pond/") and method == "GET":
        pid = path.split("/")[3]
        return _pond_detail(E, st, pid)

    # ------------------------------------------------------- backup/export
    if path == "/api/export/json" and method == "GET":
        return E.db.export_dict()

    if path.startswith("/api/transactions/") and path.endswith("/void") and method == "POST":
        tid = int(path.split("/")[3])
        E.db.void_txn(tid, body.get("note", ""))
        return {"ok": True}

    # --------------------------------------------------------- assumptions
    if path == "/api/assumptions" and method == "GET":
        return A.describe()

    if path == "/api/assumptions" and method == "POST":
        key, val = body.get("key"), body.get("value")
        newv = A.set(key, val)
        return {"ok": True, "key": key, "value": newv}

    if path == "/api/assumptions/history" and method == "GET":
        return {"history": A.history((query.get("key") or [None])[0]),
                "effective_dated_keys": A.effective_dated_keys()}

    if path == "/api/assumptions/effective" and method == "POST":
        return A.set_effective(body["key"], body["value"],
                               body.get("effective_from") or date.today().isoformat(),
                               body.get("note", ""))

    if path == "/api/assumptions/effective/delete" and method == "POST":
        E.db.delete_assumption_history(int(body["id"]))
        A.refresh()
        return {"ok": True}

    if path == "/api/assumptions/reset" and method == "POST":
        if body.get("key"):
            A.reset(body["key"])
        else:
            A.reset_all()
        return {"ok": True}

    # -------------------------------------------------------------- offers
    if path == "/api/offers" and method == "GET":
        return {"egg_offers": E.db.egg_offers(), "sale_offers": E.db.sale_offers()}

    if path == "/api/offers/egg" and method == "POST":
        oid = E.db.add_egg_offer(
            offer_date=body["offer_date"], supplier=body.get("supplier"),
            quantity=float(body["quantity"]), price_per_egg=float(body["price_per_egg"]),
            expiry_date=body.get("expiry_date"),
            quality_score=body.get("quality_score"),
            payment_terms_days=int(body.get("payment_terms_days") or 0))
        return {"ok": True, "offer_id": oid}

    if path == "/api/offers/egg/decide" and method == "POST":
        return _decide_egg_offer(E, body)

    # -------------------------------------------------- برنامه (مرحله ۲)
    if path == "/api/plan" and method == "GET":
        variant = (query.get("variant") or ["balanced"])[0]
        if not solver_status()["available"]:
            raise OptimizerUnavailable()
        pl = E.plan(variant, _as_of(query))
        original = E.db.get_plan("original", variant)
        from core.manual_mix import optimal_target_mix
        return {
            "summary": pl.summary(),
            "monthly": pl.monthly,
            "quarterly": pl.quarterly,
            "action_plan_90d": pl.action_plan_90d(),
            "capacity_curve": pl.capacity_curve(),
            "cohort_decisions": pl.cohort_decisions(),
            "grading": pl.grading_outlook(),
            "quarterly_fx": pl.quarterly_capital_fx(FXBenchmark(E.db, A)),
            "risk_flags": pl.risk_flags(),
            "lots": pl.solution.chosen_lots,
            "variance": VAR.build(E.db, A, pl.state.as_of, pl, original),
            "validation": V.run_plan_checks_v2(A, pl),
            "plan_status": pl.plan_status(),
            "horizon": pl.horizon_split(),
            "cash": {"metrics": pl.cash.metrics(),
                     "by_month": pl.cash.by_month(),
                     "series": pl.cash.series[:160],
                     "rows": pl.cash.rows[:400]},
            "repair_log": pl.repair_log,
            "has_original": bool(original),
            "manual_mix_target": optimal_target_mix(pl),
            "variants_available": list(__import__("core.optimizer",
                                                  fromlist=["Variant"]).Variant.ALL),
        }

    # ------------------------ Manual Sales Mix Comparison (افزونه مستقل)
    if path == "/api/plan/manual_mix" and method == "POST":
        if not solver_status()["available"]:
            raise OptimizerUnavailable()
        variant = body.get("variant", "balanced")
        targets = {float(w): float(q) for w, q in (body.get("targets") or {}).items()
                  if q not in (None, "")}
        baseline = Plan(A, bio, st, variant)
        from core.manual_mix import compare_to_optimised
        return compare_to_optimised(A, bio, st, targets, baseline, variant)

    if path == "/api/plan/variants" and method == "GET":
        A2, bio2, st2 = E.ctx(_as_of(query))
        return {"variants": variant_comparison(A2, bio2, st2)}

    if path == "/api/plan/scenarios" and method == "GET":
        A2, bio2, st2 = E.ctx(_as_of(query))
        variant = (query.get("variant") or ["balanced"])[0]
        return {"scenarios": scenario_comparison(A2, bio2, st2, variant)}

    if path == "/api/plan/save" and method == "POST":
        variant = body.get("variant", "balanced")
        kind = body.get("kind", "original")
        pl = E.plan(variant)
        rec = pl.as_record()
        if kind == "original" and E.db.get_plan("original", variant) \
                and not body.get("replace"):
            raise ValueError("برنامه پایه برای این حالت قبلاً ثبت شده است؛ "
                             "برای جایگزینی replace=true بفرستید")
        if kind == "original" and body.get("replace"):
            old = E.db.get_plan("original", variant)
            if old:
                E.db.delete_plan(old["id"])
        pid = E.db.save_plan(kind, variant, pl.state.as_of.isoformat(),
                             rec["summary"], rec["monthly"], rec["lots"],
                             body.get("note", ""))
        return {"ok": True, "plan_id": pid, "kind": kind, "variant": variant}

    if path == "/api/plan/saved" and method == "GET":
        return {"plans": E.db.list_plans()}

    if path == "/api/plan/variance" and method == "GET":
        variant = (query.get("variant") or ["balanced"])[0]
        pl = E.plan(variant, _as_of(query))
        return VAR.build(E.db, A, pl.state.as_of, pl,
                         E.db.get_plan("original", variant))

    # ------------------------------------------- تخصیص خودکار استخر (۱)
    if path == "/api/ponds/allocation" and method == "GET":
        inc = (query.get("include_reserve") or ["0"])[0] in ("1", "true")
        return PALLOC.suggest(A, bio, st, E.db, include_reserve=inc)

    if path == "/api/ponds/allocation/accept" and method == "POST":
        return PALLOC.accept(E.db, st, body["cohort_id"],
                             allocations=body.get("allocations"),
                             reason=body.get("reason", ""), A=A, bio=bio)

    if path == "/api/ponds/allocation/move" and method == "POST":
        return PALLOC.move(E.db, st, body["cohort_id"], body.get("from_pond"),
                           body["to_pond"], body["quantity"],
                           body.get("reason", ""))

    # ---------------------------------- بهینه‌سازی چیدمان استخر (Optimize Pond Allocation)
    if path == "/api/ponds/optimize/preview" and method == "GET":
        horizon = query.get("horizon_days")
        horizon = int(horizon[0]) if horizon else None
        inc = (query.get("include_reserve") or ["0"])[0] in ("1", "true")
        return POPT.optimize_layout(A, bio, st, E.db, horizon_days=horizon,
                                    include_reserve=inc)

    if path == "/api/ponds/optimize/apply" and method == "POST":
        moves = body.get("moves")
        if not moves:
            raise ValueError("moves الزامی است — دقیقاً همان خروجی Preview که کاربر دید")
        return POPT.apply_layout(E.db, st, moves, reason=body.get("reason", ""))

    if path == "/api/ponds/optimize/restore" and method == "POST":
        return POPT.restore_previous(E.db, st, reason=body.get("reason", ""))

    if path == "/api/ponds/forecast" and method == "GET":
        target = d(query["date"][0]) if query.get("date") else st.as_of
        return POPT.forecast_pond_view(A, bio, st, E.db, target)

    # ------------------------------- تخصیص cohort به فروش‌ها (بر مبنای وزن)
    if path == "/api/sales/reconciliation" and method == "GET":
        return ATTR.coverage(E.db, st)

    if path.startswith("/api/sales/") and path.endswith("/split") and method == "POST":
        tid = int(path.split("/")[3])
        return ATTR.confirm_split(E.db, st, tid, body.get("allocations") or [],
                                  body.get("reason", ""))

    if path.startswith("/api/sales/") and path.endswith("/allocations") \
            and method == "GET":
        tid = int(path.split("/")[3])
        return {"allocations": E.db.sale_allocations(tid)}

    if path == "/api/sales/unassigned" and method == "GET":
        return {"sales": ATTR.suggest_all(A, bio, st, E.db),
                "cohort_ids": sorted(st.cohorts)}

    if path.startswith("/api/sales/") and path.endswith("/suggest") and method == "GET":
        tid = int(path.split("/")[3])
        row = E.db.one("SELECT * FROM transactions WHERE id=?", (tid,))
        if not row:
            raise KeyError(f"تراکنش یافت نشد: {tid}")
        out = ATTR.suggest(A, bio, st, row)
        out["cohort_ids"] = sorted(st.cohorts)
        return out

    if path.startswith("/api/sales/") and path.endswith("/assign") and method == "POST":
        tid = int(path.split("/")[3])
        return ATTR.assign(E.db, tid,
                           cohort_id=body.get("cohort_id", ATTR.UNSET),
                           weight_g=body.get("weight_g"),
                           reason=body.get("reason", ""),
                           method=body.get("method", "manual"))

    if path.startswith("/api/sales/") and path.endswith("/implied-cohort") \
            and method == "POST":
        tid = int(path.split("/")[3])
        return _create_implied_cohort(E, tid, body)

    # ============================ موتور تصمیم آفر (مرحله ۳) ============
    if path == "/api/decide/egg" and method == "POST":
        if not solver_status()["available"]:
            raise OptimizerUnavailable()
        return OFFERS.evaluate_egg_offer(
            A, bio, st, body, variant=body.get("variant", "balanced"),
            partial_options=bool(body.get("partial_options", True)))

    if path == "/api/decide/sale" and method == "POST":
        if not solver_status()["available"]:
            raise OptimizerUnavailable()
        return OFFERS.evaluate_sale_offer(
            A, bio, st, body, variant=body.get("variant", "balanced"))

    if path == "/api/decide/sale/allocation" and method == "POST":
        # پیشنهاد تقسیم یک آفر فروش بین cohortها (اصلاح ۴) — بدون حل مدل.
        from core.hypothetical import suggest_allocation, cohort_availability
        qty = float(body.get("quantity") or 0)
        w = float(body["weight_g"]) if body.get("weight_g") else None
        when = d(body["delivery_date"]) if body.get("delivery_date") else st.as_of
        if qty > 0 and w is not None:
            return suggest_allocation(A, bio, st, qty, w, when)
        return {"allocations": [], "requested": qty, "allocated": 0.0,
                "shortfall": qty, "feasible": False,
                "candidates": cohort_availability(A, bio, st, w, when)}

    if path == "/api/decide/what-if" and method == "POST":
        if not solver_status()["available"]:
            raise OptimizerUnavailable()
        return OFFERS.what_if(A, bio, st, body.get("changes") or [],
                              variant=body.get("variant", "balanced"))

    if path == "/api/decide/context" and method == "GET":
        """وضعیت لحظه‌ای مزرعه که همه تصمیم‌ها از آن ساخته می‌شوند."""
        return {
            "as_of": st.as_of.isoformat(),
            "cohorts": [{"cohort_id": c.cohort_id, "alive": c.alive,
                         "mean_weight_g": c.mean_weight,
                         "age_days": (st.as_of - c.purchase_date).days,
                         "weight_basis": c.weight_basis,
                         "count_basis": c.count_basis}
                        for c in st.cohorts.values() if c.alive >= 1],
            "ponds_used": sum(1 for p in st.pond_view() if p["count"] >= 1),
            "operational_ponds": int(A.get("farm.operational_ponds")),
            "feed_inventory_kg": sum(f["qty_kg"] for f in st.feed.values()),
            "feed_inventory_value": sum(f["value"] for f in st.feed.values()),
            "cash_balance": CashLedger(E.db, A, bio, st).metrics()["closing_balance"],
            "wc_available": float(A.get("finance.working_capital_available")),
            "harvest_weights": [float(x) for x in A.get("planning.harvest_weights")],
            "reconciliation": ATTR.coverage(E.db, st),
        }

    # ---------------------------------------------------------------- demo
    if path == "/api/demo/seed" and method == "POST":
        return seed_demo(E.db, A, force=bool(body.get("force")))
    if path == "/api/demo/clear" and method == "POST":
        return clear_demo(E.db)

    raise KeyError(f"مسیر ناشناخته: {method} {path}")


def _as_of(query):
    v = (query.get("as_of") or [None])[0]
    return d(v) if v else None


# فیلدهای الزامی هر نوع رویداد. اعتبارسنجی «قبل» از هر نوشتنی انجام می‌شود،
# پس یک رویداد ناقص هرگز در پایگاه داده ذخیره نمی‌شود.
REQUIRED_FIELDS = {
    "egg_purchase":      {"quantity": "تعداد تخم"},
    "feed_purchase":     {"quantity": "مقدار (kg)"},
    "feed_consumption":  {"quantity": "مقدار (kg)"},
    "mortality":         {"cohort_id": "cohort", "quantity": "تعداد تلفات"},
    "count_observation": {"cohort_id": "cohort", "quantity": "تعداد شمارش‌شده"},
    "weight_sample":     {"cohort_id": "cohort", "weight_g": "وزن متوسط"},
    "transfer":          {"cohort_id": "cohort", "to_pond_id": "استخر مقصد",
                          "quantity": "تعداد"},
    "sale":              {"quantity": "تعداد"},
    "payment":           {"amount": "مبلغ"},
    "receipt":           {"amount": "مبلغ"},
    "operating_cost":    {"amount": "مبلغ"},
    "water_reading":     {"pond_id": "استخر"},
}
POSITIVE_FIELDS = {"quantity", "amount", "weight_g", "unit_price"}


def _validate_txn(E, typ: str, dt: str, kw: dict, payload: dict, st=None):
    """اعتبارسنجی کامل پیش از ذخیره. هر خطا با پیام قابل فهم فارسی."""
    errs = []
    try:
        d(dt)
    except Exception:
        errs.append("تاریخ رویداد نامعتبر است")

    for field, label in REQUIRED_FIELDS.get(typ, {}).items():
        val = kw.get(field)
        if val in (None, "", 0) and not (field == "quantity" and typ == "count_observation"
                                         and val == 0):
            errs.append(f"«{label}» الزامی است")

    for field in POSITIVE_FIELDS:
        v = kw.get(field)
        if v is not None and v < 0:
            errs.append(f"مقدار «{field}» نمی‌تواند منفی باشد")

    # cohort باید واقعاً وجود داشته باشد (به‌جز خرید تخم که خودش می‌سازد)
    if typ != "egg_purchase" and kw.get("cohort_id"):
        known = set(st.cohorts) if st is not None else set()
        if known and kw["cohort_id"] not in known:
            errs.append(f"cohort ناشناخته: {kw['cohort_id']}")

    valid_ponds = {p["pond_id"] for p in E.db.ponds()}
    for field in ("pond_id", "to_pond_id"):
        if kw.get(field) and kw[field] not in valid_ponds:
            errs.append(f"استخر ناشناخته: {kw[field]}")

    if typ in ("feed_purchase", "feed_consumption") and not payload.get("feed_name"):
        errs.append("«نوع خوراک» الزامی است")
    if typ == "feed_purchase" and not (kw.get("unit_price") or kw.get("amount")):
        errs.append("برای خرید خوراک، قیمت واحد یا مبلغ کل لازم است")
    if typ == "sale" and not (kw.get("unit_price") or kw.get("amount")):
        # ثبت سریع فروش از پنل استخر می‌تواند قیمت را موقتاً خالی بگذارد؛
        # رویداد در Transaction system باقی می‌ماند و به‌وضوح price_pending است.
        if not (payload.get("entry") == "pond_panel" and payload.get("price_pending")):
            errs.append("برای فروش، قیمت واحد یا مبلغ کل لازم است")

    # رویدادهای سریع پنل استخر باید با همان موجودی‌ای اعتبارسنجی شوند که کاربر
    # روی کارت استخر می‌بیند. این موجودی ممکن است هنوز Estimated/Suggested باشد.
    if payload.get("entry") == "pond_panel" and st is not None and kw.get("cohort_id") \
            and kw.get("pond_id") and typ in ("transfer", "mortality", "sale"):
        c = st.cohorts.get(kw["cohort_id"])
        actual = float(c.alloc.get(kw["pond_id"], 0.0)) if c else 0.0
        estimated = 0.0
        pv = next((p for p in st.pond_view() if p["pond_id"] == kw["pond_id"]), None)
        if pv:
            estimated = sum(float(o.get("count") or 0.0) for o in pv.get("occupants", [])
                            if o.get("cohort_id") == kw["cohort_id"]
                            and o.get("basis") == "estimated")
        available = actual + estimated
        q = float(kw.get("quantity") or 0.0)
        if q > available + 1e-6:
            errs.append(f"تعداد واردشده ({q:,.0f}) از موجودی این cohort در استخر ({available:,.0f}) بیشتر است")

    if typ == "count_observation" and payload.get("entry") == "pond_panel" \
            and payload.get("pond_adjustment_delta") is not None and st is not None:
        c = st.cohorts.get(kw.get("cohort_id"))
        available = float(c.alloc.get(kw.get("pond_id"), 0.0)) if c else 0.0
        delta = float(payload.get("pond_adjustment_delta") or 0.0)
        if delta < 0 and -delta > available + 1e-6:
            errs.append("مقدار کاهش از تعداد فعلی این cohort در استخر بیشتر است")
    if typ == "water_reading" and not any(
            payload.get(k) is not None for k in
            ("temperature_c", "do_in", "do_out", "flow_l_s")):
        errs.append("حداقل یکی از دما، DO یا دبی را وارد کنید")
    if typ == "transfer" and kw.get("pond_id") and \
            kw["pond_id"] == kw.get("to_pond_id"):
        errs.append("استخر مبدأ و مقصد یکی است")

    if errs:
        raise ValueError(" · ".join(errs))


def _add_txn(E, body):
    typ = body.get("txn_type")
    if typ not in TXN_TYPES:
        raise ValueError(f"نوع رویداد نامعتبر: {typ}")
    dt = body.get("txn_date") or date.today().isoformat()
    kw = {k: body.get(k) for k in ("cohort_id", "pond_id", "to_pond_id",
                                   "counterparty", "note", "category", "unit")}
    for k in ("quantity", "weight_g", "unit_price", "amount"):
        v = body.get(k)
        kw[k] = float(v) if v not in (None, "") else None
    payload = body.get("payload") or {}
    if typ == "egg_purchase":
        cid = kw.get("cohort_id") or f"C-{dt.replace('-', '')}-{int(E.db.one('SELECT COUNT(*) n FROM transactions')['n'])+1}"
        kw["cohort_id"] = cid
        # قیمت واقعی واردشده اولویت دارد؛ در نبود آن، قیمت با تاریخ اعتبار همان روز
        if not kw.get("unit_price") and not kw.get("amount"):
            kw["unit_price"] = float(E.A.get_at("egg.base_price", dt))
            payload.setdefault("price_source", "assumption_at_date")
    if typ == "operating_cost" and not kw.get("category"):
        kw["category"] = "سایر"
    # مبلغ کل = مقدار × قیمت واحد، وقتی صریحاً وارد نشده باشد
    if kw.get("unit_price") and kw.get("quantity") and not kw.get("amount"):
        kw["amount"] = kw["quantity"] * kw["unit_price"]
    if kw.get("amount") and kw.get("quantity") and not kw.get("unit_price"):
        kw["unit_price"] = kw["amount"] / kw["quantity"]

    # اعتبارسنجی پیش از هر نوشتنی — رویداد ناقص ذخیره نمی‌شود.
    # برای پنل استخر، validator موجودی Estimated نمایش‌داده‌شده را هم می‌بیند.
    st_now = E.ctx()[2]
    _validate_txn(E, typ, dt, kw, payload, st=st_now)

    # اگر کاربر از یک استخر Suggested/Estimated یک رویداد واقعی ثبت کند، همین
    # عمل کاربر تأیید عملیِ موجودی نمایش‌داده‌شده در آن استخر است. فقط همان
    # تخصیص همان cohort/pond با Transaction architecture موجود Actual می‌شود؛
    # سپس رویداد اصلی ثبت می‌گردد. هیچ state موازی ساخته نمی‌شود.
    if payload.get("entry") == "pond_panel" and typ in ("transfer", "mortality", "sale") \
            and kw.get("cohort_id") and kw.get("pond_id"):
        c = st_now.cohorts.get(kw["cohort_id"])
        actual = float(c.alloc.get(kw["pond_id"], 0.0)) if c else 0.0
        q = float(kw.get("quantity") or 0.0)
        if q > actual + 1e-6:
            pv = next((p for p in st_now.pond_view() if p["pond_id"] == kw["pond_id"]), None)
            estimated = sum(float(o.get("count") or 0.0) for o in (pv or {}).get("occupants", [])
                            if o.get("cohort_id") == kw["cohort_id"]
                            and o.get("basis") == "estimated")
            if estimated > 0:
                PALLOC.accept(
                    E.db, st_now, kw["cohort_id"],
                    allocations=[{"pond_id": kw["pond_id"], "quantity": estimated}],
                    reason="تأیید خودکار تخصیص تخمینی هنگام ثبت رویداد واقعی از پنل استخر",
                    A=st_now.A, bio=st_now.bio)
                payload.setdefault("auto_confirmed_estimated_pond", True)
                payload.setdefault("auto_confirmed_quantity", estimated)

    tid = E.db.add_txn(typ, dt, payload=payload, data_source="actual", **kw)
    row = E.db.one("SELECT * FROM transactions WHERE id=?", (tid,))
    return {"ok": True, "id": tid, "cohort_id": kw.get("cohort_id"),
            "txn_type": typ, "txn_date": dt,
            "created_at": row["created_at"], "unit": row["unit"],
            "amount": row["amount"], "quantity": row["quantity"],
            "message": "Transaction saved successfully",
            "message_fa": "رویداد با موفقیت ثبت شد"}


def _create_implied_cohort(E, txn_id: int, body: dict) -> dict:
    """
    ثبت یک cohort تاریخی استنتاجی از روی وزن یک فروش.

    وقتی وزن فروخته‌شده با هیچ cohort ثبت‌شده‌ای نمی‌خواند، یعنی این ماهی از
    یک خرید قدیمی‌تر آمده که در سیستم نیست. این تابع آن خرید را با برچسب
    صریح **Estimated / Inferred** می‌سازد — نه Observed.

    بهای تخم صفر ثبت می‌شود تا دفتر نقدی با یک عدد ساختگی آلوده نشود؛
    کاربر هر زمان قیمت واقعی را وارد کند، از همان لحظه اعمال می‌شود.
    """
    A, bio, st = E.ctx()
    row = E.db.one("SELECT * FROM transactions WHERE id=?", (txn_id,))
    if not row:
        raise KeyError(f"تراکنش یافت نشد: {txn_id}")
    sug = ATTR.suggest(A, bio, st, row)
    hint = sug.get("missing_cohort_hint")
    pdate = body.get("purchase_date") or (hint or {}).get("purchase_date")
    if not pdate:
        raise ValueError("تاریخ خرید ضمنی قابل محاسبه نیست؛ ابتدا وزن فروش را ثبت کنید")
    eggs = float(body.get("egg_count") or (hint or {}).get("suggested_egg_count") or 0)
    if eggs <= 0:
        raise ValueError("تعداد تخم برآوردی نامعتبر است")
    cid = body.get("cohort_id") or f"CH-{pdate.replace('-', '')}-INF"
    if E.db.one("SELECT 1 FROM transactions WHERE cohort_id=? AND "
                "txn_type='egg_purchase' AND status='active'", (cid,)):
        raise ValueError(f"cohort با شناسه {cid} از قبل وجود دارد")

    price = float(body.get("unit_price") or 0)
    tid = E.db.add_txn(
        "egg_purchase", pdate, cohort_id=cid, quantity=eggs,
        unit_price=price or None, amount=(eggs * price) if price else 0,
        counterparty=body.get("supplier") or "نامشخص",
        data_source="estimated",
        note=body.get("note") or
        f"cohort استنتاجی از وزن فروش #{txn_id} — Estimated، نه Observed. "
        f"بهای تخم نامشخص است و صفر ثبت شده تا دفتر نقدی با عدد ساختگی آلوده نشود.",
        payload={"inferred": True, "inferred_from_txn": txn_id,
                 "basis": "sale_weight_backsolve",
                 "implied_age_days": (hint or {}).get("implied_age_days"),
                 "cost_unknown": not bool(price)})
    assign = ATTR.assign(E.db, txn_id, cohort_id=cid,
                         reason=f"تخصیص به cohort استنتاجی {cid} بر مبنای وزن فروخته‌شده",
                         method="inferred_cohort")
    return {"ok": True, "cohort_id": cid, "egg_purchase_id": tid,
            "purchase_date": pdate, "egg_count": eggs,
            "assigned_txn": assign["new_id"],
            "warning_fa": "این cohort برآوردی است. تعداد تخم و تاریخ از وزن فروش "
                          "بازمحاسبه شده‌اند و داده واقعی نیستند."}


def _pond_detail(E, st, pond_id: str) -> dict:
    """جزئیات یک استخر + مقایسه Estimated و آخرین Actual ثبت‌شده."""
    p = next((x for x in st.pond_view() if x["pond_id"] == pond_id), None)
    if not p:
        raise KeyError(f"استخر یافت نشد: {pond_id}")
    hist = E.db.q("SELECT * FROM transactions WHERE pond_id=? OR to_pond_id=? "
                  "ORDER BY txn_date DESC, id DESC LIMIT 60", (pond_id, pond_id))
    cohorts = []
    for o in p["occupants"]:
        c = st.cohorts.get(o["cohort_id"])
        if not c:
            continue
        last_w = E.db.one(
            "SELECT * FROM transactions WHERE cohort_id=? AND txn_type='weight_sample' "
            "AND status='active' ORDER BY txn_date DESC, id DESC LIMIT 1", (c.cohort_id,))
        last_n = E.db.one(
            "SELECT * FROM transactions WHERE cohort_id=? AND txn_type='count_observation' "
            "AND status='active' ORDER BY txn_date DESC, id DESC LIMIT 1", (c.cohort_id,))
        cohorts.append({
            "cohort_id": c.cohort_id,
            "count_in_pond": o["count"],
            "alloc_basis": o["basis"],
            "estimated_mean_weight_g": c.mean_weight,
            "weight_basis": c.weight_basis,
            "weight_anchor_date": c.weight_anchor_date.isoformat()
            if c.weight_anchor_date else None,
            "growth_offset_days": round(c.growth_offset_days, 1),
            "estimated_alive": c.alive,
            "count_basis": c.count_basis,
            "last_actual_weight": last_w,
            "last_actual_count": last_n,
        })
    return {"pond": p, "cohorts": cohorts, "history": hist,
            "water": st.water_readings.get(pond_id)}


def _pond_observe(E, body, st) -> dict:
    """
    ثبت داده واقعی یک استخر (اصلاح ۲).

    هر فیلد پرشده به یک transaction با برچسب Actual/Observed تبدیل می‌شود.
    Estimated قبلی حذف نمی‌شود؛ فقط از این تاریخ به بعد forecast از داده
    واقعی ادامه می‌یابد. مقدار تخمینی قبل از ثبت هم برگردانده می‌شود تا
    تفاوت Estimated و Actual قابل مشاهده باشد.
    """
    pond = body.get("pond_id")
    if not pond:
        raise ValueError("استخر مشخص نشده است")
    cohort = body.get("cohort_id") or None
    when = body.get("measurement_date") or date.today().isoformat()
    note = body.get("note") or ""
    created = []

    def num(k):
        v = body.get(k)
        return float(v) if v not in (None, "") else None

    # وضعیت تخمینی قبل از ثبت — برای نمایش تفاوت
    before = None
    if cohort and cohort in st.cohorts:
        c = st.cohorts[cohort]
        before = {"estimated_alive": c.alive, "estimated_mean_weight_g": c.mean_weight,
                  "count_basis": c.count_basis, "weight_basis": c.weight_basis}

    count = num("actual_count")
    if count is not None:
        if not cohort:
            raise ValueError("برای ثبت تعداد واقعی، cohort لازم است")
        created.append(("count_observation", E.db.add_txn(
            "count_observation", when, cohort_id=cohort, pond_id=pond,
            quantity=count, data_source="actual",
            note=f"ثبت از پنل استخر {pond}. {note}".strip(),
            payload={"entry": "pond_panel"})))

    w = num("actual_mean_weight_g")
    if w is not None:
        if not cohort:
            raise ValueError("برای ثبت وزن واقعی، cohort لازم است")
        pl = {"entry": "pond_panel"}
        if num("sd_g") is not None:
            pl["sd_g"] = num("sd_g")
        if num("n_sampled") is not None:
            pl["n_sampled"] = num("n_sampled")
        created.append(("weight_sample", E.db.add_txn(
            "weight_sample", when, cohort_id=cohort, pond_id=pond, weight_g=w,
            quantity=num("n_sampled"), data_source="actual",
            note=f"نمونه‌برداری وزن در استخر {pond}. {note}".strip(), payload=pl)))

    mort = num("mortality")
    if mort is not None and mort > 0:
        if not cohort:
            raise ValueError("برای ثبت تلفات، cohort لازم است")
        created.append(("mortality", E.db.add_txn(
            "mortality", when, cohort_id=cohort, pond_id=pond, quantity=mort,
            data_source="actual", note=f"تلفات استخر {pond}. {note}".strip(),
            payload={"entry": "pond_panel"})))

    water = {k: num(k) for k in ("temperature_c", "do_in", "do_out", "flow_l_s")}
    water = {k: v for k, v in water.items() if v is not None}
    if water:
        created.append(("water_reading", E.db.add_txn(
            "water_reading", when, pond_id=pond, data_source="actual",
            note=f"قرائت آب استخر {pond}. {note}".strip(),
            payload={**water, "entry": "pond_panel"})))

    feed_kg = num("feed_kg")
    if feed_kg is not None and feed_kg > 0:
        created.append(("feed_consumption", E.db.add_txn(
            "feed_consumption", when, cohort_id=cohort, pond_id=pond,
            quantity=feed_kg, amount=num("feed_amount"), data_source="actual",
            note=f"مصرف خوراک استخر {pond}. {note}".strip(),
            payload={"feed_name": body.get("feed_name"), "entry": "pond_panel"})))

    biomass = num("biomass_kg")
    if biomass is not None:
        # زیست‌توده مشاهده‌شده فقط ثبت می‌شود؛ تعداد یا وزن را overwrite نمی‌کند
        created.append(("water_reading", E.db.add_txn(
            "water_reading", when, pond_id=pond, cohort_id=cohort,
            data_source="actual", note=f"زیست‌توده مشاهده‌شده استخر {pond}. {note}".strip(),
            payload={"observed_biomass_kg": biomass, "entry": "pond_panel"})))

    if not created:
        raise ValueError("هیچ مقداری برای ثبت وارد نشده است")

    # وضعیت پس از ثبت
    A2, bio2, st2 = E.ctx(st.as_of)
    after = None
    if cohort and cohort in st2.cohorts:
        c2 = st2.cohorts[cohort]
        after = {"estimated_alive": c2.alive, "mean_weight_g": c2.mean_weight,
                 "count_basis": c2.count_basis, "weight_basis": c2.weight_basis,
                 "growth_offset_days": round(c2.growth_offset_days, 1)}
    return {"ok": True, "created": [{"type": t, "id": i} for t, i in created],
            "before_estimated": before, "after": after}


def _decide_egg_offer(E, body):
    oid = body["offer_id"]
    decision = body["decision"]          # accept | partial | reject
    o = E.db.one("SELECT * FROM egg_offers WHERE offer_id=?", (oid,))
    if not o:
        raise ValueError("آفر یافت نشد")
    today = body.get("decision_date") or date.today().isoformat()
    note = body.get("note", "")
    if decision == "reject":
        E.db.ex("UPDATE egg_offers SET status='rejected', decision_date=?, decision_note=? "
                "WHERE offer_id=?", (today, note, oid))
        return {"ok": True, "status": "rejected"}
    qty = float(body.get("quantity") or o["quantity"])
    qty = min(qty, float(o["quantity"]))
    status = "accepted" if abs(qty - float(o["quantity"])) < 1e-6 else "partial"
    cid = body.get("cohort_id") or f"C-{o['offer_date'].replace('-', '')}-{oid[-4:]}"
    tid = E.db.add_txn("egg_purchase", o["offer_date"], cohort_id=cid, quantity=qty,
                       unit_price=o["price_per_egg"], amount=qty * o["price_per_egg"],
                       counterparty=o["supplier"], note=f"از آفر {oid}. {note}",
                       payload={"offer_id": oid})
    E.db.ex("UPDATE egg_offers SET status=?, accepted_quantity=?, decision_date=?, "
            "decision_note=?, linked_txn_id=? WHERE offer_id=?",
            (status, qty, today, note, tid, oid))
    return {"ok": True, "status": status, "cohort_id": cid, "txn_id": tid}


# ================================================================== handler
class Handler(BaseHTTPRequestHandler):
    server_version = "TroutFarm/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("TROUT_VERBOSE"):
            super().log_message(fmt, *args)

    # ------------------------------------------------------------- helpers
    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, rel):
        if rel in ("", "/"):
            rel = "index.html"
        p = os.path.normpath(os.path.join(STATIC, rel.lstrip("/")))
        if not p.startswith(STATIC) or not os.path.isfile(p):
            self.send_error(404, "not found")
            return
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(p, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _download(self, data: bytes, filename: str, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _try_export(self, u) -> bool:
        """مسیرهای دانلود پشتیبان (اصلاح ۹). خروجی JSON نیست، پس جدا مدیریت می‌شود."""
        stamp = date.today().isoformat()
        if u.path == "/api/export/sqlite":
            self._download(ENGINE.db.backup_bytes(), f"farm-backup-{stamp}.db",
                           "application/vnd.sqlite3")
            return True
        if u.path == "/api/export/csv":
            table = (parse_qs(u.query).get("table") or ["transactions"])[0]
            csv_text = ENGINE.db.export_csv(table)
            self._download(csv_text.encode("utf-8"), f"{table}-{stamp}.csv",
                           "text/csv; charset=utf-8")
            return True
        if u.path == "/api/export/json/download":
            blob = json.dumps(ENGINE.db.export_dict(), ensure_ascii=False,
                              indent=1, default=str).encode("utf-8")
            self._download(blob, f"farm-backup-{stamp}.json",
                           "application/json; charset=utf-8")
            return True
        return False

    def _dispatch(self, method):
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return self._static(u.path)
        body = {}
        if method == "POST":
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else ""
            body = json.loads(raw) if raw else {}
        try:
            if method == "GET" and self._try_export(u):
                return
            return self._json(api(u.path, method, parse_qs(u.query), body))
        except OptimizerUnavailable as e:
            return self._json({"error": e.message_fa, "error_en": e.message_en,
                               "code": "optimizer_unavailable",
                               "solver": e.status}, 503)
        except KeyError as e:
            return self._json({"error": str(e)}, 404)
        except (ValueError, TypeError) as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:                                # pragma: no cover
            traceback.print_exc()
            return self._json({"error": f"خطای داخلی: {e}"}, 500)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


# ===================================================================== main
def main():
    global ENGINE
    ap = argparse.ArgumentParser()
    # Railway injects PORT. Local defaults remain unchanged when env vars are absent.
    ap.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    ap.add_argument("--host", default=os.getenv("HOST", "0.0.0.0" if os.getenv("RAILWAY_ENVIRONMENT") else "127.0.0.1"))
    ap.add_argument("--db", default=os.getenv("DATABASE_PATH", DEFAULT_DB))
    ap.add_argument("--seed-demo", action="store_true")
    ap.add_argument("--reset", action="store_true", help="حذف پایگاه داده و شروع از نو")
    args = ap.parse_args()

    db_dir = os.path.dirname(os.path.abspath(args.db))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    if args.reset and os.path.exists(args.db):
        os.remove(args.db)
        for ext in ("-wal", "-shm"):
            if os.path.exists(args.db + ext):
                os.remove(args.db + ext)

    ENGINE = Engine(args.db)
    if args.seed_demo:
        print(seed_demo(ENGINE.db, ENGINE.A))
    sv = check_solver()
    if sv["available"]:
        print(f"  بهینه‌ساز   : PuLP {sv['pulp_version']} + CBC — آماده")
    else:
        print("  بهینه‌ساز   : ✗ در دسترس نیست")
        print(f"                {'بسته PuLP نصب نیست' if not sv['pulp_installed'] else 'CBC اجرا نشد'}")
        print(f"                نصب: {sv['install_command']}")
        print("                (بقیه داشبورد بدون آن هم کار می‌کند)")
    print(f"  پایگاه داده : {args.db}")
    print(f"  نرخ ارز     : {ENGINE.fx_load}")
    print(f"  داشبورد     : http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
