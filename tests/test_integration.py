"""تست‌های یکپارچه مرحله ۱ — بدون نیاز به سرور HTTP."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as APP                                     # noqa: E402
from app import Engine, api                           # noqa: E402


def fresh():
    p = os.path.join(tempfile.mkdtemp(), "t.db")
    e = Engine(p)
    APP.ENGINE = e
    return e


def call(path, method="GET", body=None):
    return api(path, method, {}, body or {})


# ---------------------------------------------------------------- fixtures
def test_seed_and_bootstrap():
    fresh()
    b = call("/api/bootstrap")
    assert len(b["ponds"]) == 21
    assert len(b["cohorts"]) == 5      # خرید اشتباه ۳ آوریل ابطال شد
    assert sum(c["egg_count"] for c in b["cohorts"]) == 650_000
    assert b["summary"]["operational_ponds_total"] == 19


def test_mass_and_cohort_balance():
    fresh()
    call("/api/demo/seed", "POST", {})
    b = call("/api/bootstrap")
    for c in b["cohorts"]:
        assert c["alive"] + c["sold_count"] <= c["egg_count"] + 0.5
        assert abs(sum(c["ponds"].values()) - c["alive"]) <= max(2.0, 0.002 * c["alive"])
    names = {x["id"]: x["status"] for x in b["validation"]["checks"]}
    assert names["mass_balance"] == "pass"
    assert names["cohort_balance"] == "pass"
    assert names["regression_core"] == "pass"
    assert b["validation"]["failed"] == 0


def test_mortality_not_retroactively_suppressed():
    """یک رکورد تلفات نباید کل تاریخچه تلفات مدل را حذف کند."""
    e = fresh()
    b0 = call("/api/bootstrap")
    a0 = {c["cohort_id"]: c["alive"] for c in b0["cohorts"]}
    cid = b0["cohorts"][0]["cohort_id"]
    call("/api/transactions", "POST", {"txn_type": "mortality", "txn_date": "2026-07-25",
                                       "cohort_id": cid, "quantity": 3500})
    b1 = call("/api/bootstrap")
    a1 = {c["cohort_id"]: c["alive"] for c in b1["cohorts"]}
    # تلفات تجمعی باید همچنان نزدیک منحنی پایه بماند، نه ۲٪
    c = [x for x in b1["cohorts"] if x["cohort_id"] == cid][0]
    assert c["cum_mortality"] > 0.35, c["cum_mortality"]
    assert a1[cid] < a0[cid]


def test_weight_sample_reanchors_forecast():
    fresh()
    b0 = call("/api/bootstrap")
    c = b0["cohorts"][3]
    w0 = c["mean_weight_g"]
    call("/api/transactions", "POST", {"txn_type": "weight_sample", "txn_date": "2026-08-10",
                                       "cohort_id": c["cohort_id"], "weight_g": w0 * 2,
                                       "payload": {"sd_g": w0 * 0.2}})
    b1 = call("/api/bootstrap")
    c1 = [x for x in b1["cohorts"] if x["cohort_id"] == c["cohort_id"]][0]
    assert c1["weight_basis"] == "actual"
    assert c1["mean_weight_g"] > w0 * 1.6      # forecast از داده واقعی ادامه یافته
    assert c1["growth_offset_days"] > 0


def test_actual_never_overwritten_by_assumption_change():
    fresh()
    cid = call("/api/bootstrap")["cohorts"][0]["cohort_id"]
    call("/api/transactions", "POST", {"txn_type": "count_observation",
                                       "txn_date": "2026-08-01",
                                       "cohort_id": cid, "quantity": 90_000})
    before = call("/api/transactions")["transactions"]
    call("/api/assumptions", "POST", {"key": "mortality.cum_at_1g", "value": 0.40})
    after = call("/api/transactions")["transactions"]
    assert len(before) == len(after)
    assert [t["quantity"] for t in before] == [t["quantity"] for t in after]
    call("/api/assumptions/reset", "POST", {"key": "mortality.cum_at_1g"})


def test_correction_keeps_audit_trail():
    fresh()
    cid = call("/api/bootstrap")["cohorts"][0]["cohort_id"]
    r = call("/api/transactions", "POST", {"txn_type": "mortality", "txn_date": "2026-07-01",
                                           "cohort_id": cid, "quantity": 1000})
    call(f"/api/transactions/{r['id']}/correct", "POST", {"quantity": 2000})
    rows = call("/api/transactions")["transactions"]
    old = [t for t in rows if t["id"] == r["id"]][0]
    new = [t for t in rows if t["corrects_id"] == r["id"]][0]
    assert old["status"] == "corrected"          # حذف نشده
    assert new["quantity"] == 2000
    assert new["status"] == "active"


def test_offer_flow_partial_accept():
    fresh()
    o = call("/api/offers/egg", "POST", {"offer_date": "2026-08-11", "supplier": "S",
                                         "quantity": 150_000, "price_per_egg": 6400})
    d = call("/api/offers/egg/decide", "POST", {"offer_id": o["offer_id"],
                                                "decision": "accept", "quantity": 120_000})
    assert d["status"] == "partial"
    offers = call("/api/offers")["egg_offers"]
    acc = [x for x in offers if x["offer_id"] == o["offer_id"]][0]
    assert acc["accepted_quantity"] == 120_000
    b = call("/api/bootstrap")
    assert any(c["cohort_id"] == d["cohort_id"] for c in b["cohorts"])
    # رد کردن آفر هم باید حفظ شود
    o2 = call("/api/offers/egg", "POST", {"offer_date": "2026-08-11", "supplier": "S2",
                                          "quantity": 50_000, "price_per_egg": 7000})
    call("/api/offers/egg/decide", "POST", {"offer_id": o2["offer_id"],
                                            "decision": "reject", "note": "گران"})
    rej = [x for x in call("/api/offers")["egg_offers"] if x["offer_id"] == o2["offer_id"]][0]
    assert rej["status"] == "rejected" and rej["decision_note"] == "گران"


def test_assumption_bounds_and_reset():
    fresh()
    try:
        call("/api/assumptions", "POST", {"key": "heterogeneity.cv_at_1g", "value": 99})
        assert False, "باید خطا می‌داد"
    except ValueError:
        pass
    call("/api/assumptions", "POST", {"key": "heterogeneity.cv_at_1g", "value": 0.25})
    a = call("/api/assumptions")
    p = [x for g in a["groups"] for x in g["params"]
         if x["key"] == "heterogeneity.cv_at_1g"][0]
    assert p["overridden"] and abs(p["value"] - 0.25) < 1e-9 and p["default"] == 0.12
    call("/api/assumptions/reset", "POST", {"key": "heterogeneity.cv_at_1g"})
    a = call("/api/assumptions")
    p = [x for g in a["groups"] for x in g["params"]
         if x["key"] == "heterogeneity.cv_at_1g"][0]
    assert not p["overridden"] and abs(p["value"] - 0.12) < 1e-9


def test_feed_inventory_reconciles():
    fresh()
    call("/api/transactions", "POST", {"txn_type": "feed_purchase", "txn_date": "2026-07-01",
                                       "quantity": 1000, "unit_price": 300000,
                                       "payload": {"feed_name": "FP-1"}})
    call("/api/transactions", "POST", {"txn_type": "feed_purchase", "txn_date": "2026-07-15",
                                       "quantity": 1000, "unit_price": 320000,
                                       "payload": {"feed_name": "FP-1"}})
    call("/api/transactions", "POST", {"txn_type": "feed_consumption", "txn_date": "2026-07-20",
                                       "quantity": 500, "payload": {"feed_name": "FP-1"}})
    f = [x for x in call("/api/bootstrap")["feed"] if x["name"] == "FP-1"][0]
    assert abs(f["qty_kg"] - 1500) < 1e-6
    assert abs(f["avg_cost"] - 310000) < 1                 # میانگین موزون
    chk = {x["id"]: x["status"] for x in call("/api/validate")["checks"]}
    assert chk["feed_reconciliation"] == "pass"


def test_cash_ledger_identity():
    fresh()
    call("/api/demo/seed", "POST", {})
    L = call("/api/ledger")
    total = sum(r["amount"] for r in L["rows"])
    ser = L["series"]
    assert abs(ser[-1]["balance"] - (L["metrics"]["opening_cash"] + total)) < 1.0
    assert L["metrics"]["peak_funding_requirement"] >= 0


def test_capacity_forecast_is_bounded():
    fresh()
    f = call("/api/forecast")
    assert f["peak"]["peak_ponds_required"] < 60      # با فرض برداشت در ۱۵ گرم
    assert all(c["ponds_required"] >= 0 for c in f["curve"])


def test_fx_quarters():
    fresh()
    fx = call("/api/fx")
    assert fx["kpi"]["available"]
    assert fx["kpi"]["fx_start"] > 0 and fx["kpi"]["fx_latest"] > 0
    for q in fx["quarters"]:
        assert abs((q["fx_end"] / q["fx_start"] - 1) - q["fx_return"]) < 1e-9
        if q["capital"]:
            assert abs(q["usd_alternative_gain"] -
                       q["capital"] * q["benchmark_share"] * q["fx_return"]) < 1.0
