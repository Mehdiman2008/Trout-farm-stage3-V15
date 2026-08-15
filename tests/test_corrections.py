"""
تست‌های ده اصلاح نهایی مرحله ۱.
هر تست مستقیماً به یکی از بندهای درخواست اصلاحات نگاشت می‌شود.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as m                                    # noqa: E402
from app import Engine, api                        # noqa: E402
from core.seed import OBSERVED_SALES               # noqa: E402

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "test_corr.db")


@pytest.fixture(scope="module")
def E():
    for ext in ("", "-wal", "-shm"):
        if os.path.exists(DB + ext):
            os.remove(DB + ext)
    eng = Engine(DB)
    m.ENGINE = eng
    yield eng
    eng.db.close()


def call(path, method="GET", body=None, query=None):
    return api(path, method, query or {}, body or {})


# ── ۱ سرمایه در گردش ────────────────────────────────────────────────
def test_wc_three_distinct_numbers(E):
    L = call("/api/ledger")["metrics"]
    for k in ("wc_available", "wc_tied_up_now", "wc_forecast_peak"):
        assert k in L
    assert L["wc_available"] == 8.0e9              # فرض پایه، نه خروجی optimizer
    assert L["wc_tied_up_now"] > 0
    assert L["wc_forecast_peak"] > 0
    assert L["wc_forecast_days"] == 180


def test_wc_available_is_editable_assumption(E):
    call("/api/assumptions", "POST", {"key": "finance.working_capital_available",
                                      "value": 12.0e9})
    assert call("/api/ledger")["metrics"]["wc_available"] == 12.0e9
    call("/api/assumptions/reset", "POST", {"key": "finance.working_capital_available"})
    assert call("/api/ledger")["metrics"]["wc_available"] == 8.0e9


def test_wc_required_recomputes_after_real_transaction(E):
    before = call("/api/ledger")["metrics"]["wc_tied_up_now"]
    call("/api/transactions", "POST", {
        "txn_type": "feed_purchase", "txn_date": "2026-08-02", "quantity": 5000,
        "unit_price": 300000, "payload": {"feed_name": "FP-1"}, "note": "تست WC"})
    after = call("/api/ledger")["metrics"]["wc_tied_up_now"]
    assert after > before                          # ۱.۵ میلیارد خوراک اضافه شد
    assert after - before == pytest.approx(1.5e9, rel=0.02)


# ── ۲ ثبت داده واقعی از پنل استخر ───────────────────────────────────
def test_pond_observe_creates_actual_records_and_moves_anchor(E):
    cid = call("/api/bootstrap")["meta"]["cohort_ids"][3]
    call("/api/transactions", "POST", {"txn_type": "transfer", "txn_date": "2026-08-01",
                                       "cohort_id": cid, "to_pond_id": "P05",
                                       "quantity": 60000})
    det = call("/api/pond/P05")
    est_w = det["cohorts"][0]["estimated_mean_weight_g"]
    assert det["cohorts"][0]["weight_basis"] == "estimated"

    r = call("/api/pond/observe", "POST", {
        "pond_id": "P05", "cohort_id": cid, "measurement_date": "2026-08-09",
        "actual_count": 58000, "actual_mean_weight_g": 3.4, "sd_g": 0.5,
        "n_sampled": 100, "mortality": 250, "temperature_c": 11.4,
        "do_in": 8.1, "do_out": 6.2, "flow_l_s": 15.0, "note": "تست"})
    types = {c["type"] for c in r["created"]}
    assert {"count_observation", "weight_sample", "mortality", "water_reading"} <= types
    # مبنا به داده واقعی منتقل شد و forecast از همان‌جا ادامه می‌یابد
    assert r["after"]["weight_basis"] == "actual"
    assert r["after"]["mean_weight_g"] != pytest.approx(est_w)
    assert r["after"]["growth_offset_days"] != 0
    # تخمین قبلی حذف نشده و قابل مقایسه است
    assert r["before_estimated"]["estimated_mean_weight_g"] == pytest.approx(est_w)
    # همه رکوردها Actual هستند
    for c in r["created"]:
        row = E.db.one("SELECT * FROM transactions WHERE id=?", (c["id"],))
        assert row["data_source"] == "actual"


def test_pond_observe_rejects_empty_submission(E):
    with pytest.raises(ValueError):
        call("/api/pond/observe", "POST", {"pond_id": "P07"})


# ── ۳ همه رویدادها به‌صورت transaction با فیلدهای کامل ──────────────
def test_all_event_types_are_transactions_with_required_fields(E):
    rows = E.db.active_txns()
    assert {t["txn_type"] for t in rows} >= {
        "egg_purchase", "feed_purchase", "sale", "mortality",
        "count_observation", "weight_sample", "transfer", "water_reading"}
    for t in rows:
        assert t["txn_date"] and t["txn_type"] and t["created_at"]
        assert t["unit"]                                   # واحد همیشه ثبت می‌شود
    fp = [t for t in rows if t["txn_type"] == "feed_purchase"][0]
    assert fp["unit"] == "kg" and fp["amount"] and fp["unit_price"]


def test_amount_and_unit_price_derive_from_each_other(E):
    r = call("/api/transactions", "POST", {
        "txn_type": "sale", "txn_date": "2026-08-08", "quantity": 1000,
        "amount": 15_000_000, "counterparty": "تست"})
    row = E.db.one("SELECT * FROM transactions WHERE id=?", (r["id"],))
    assert row["unit_price"] == pytest.approx(15000)
    E.db.void_txn(r["id"], "پاک‌سازی تست")


# ── ۴ هزینه‌های جانبی و نبود double counting ────────────────────────
def test_operating_cost_categories_and_no_double_counting(E):
    base = call("/api/ledger")["metrics"]
    fixed_before = -sum(r["amount"] for r in
                        call("/api/ledger")["rows"] if r["type"] == "fixed_cost")
    r = call("/api/transactions", "POST", {
        "txn_type": "operating_cost", "txn_date": "2026-06-10",
        "amount": 40_000_000, "category": "نگهداری و تعمیرات",
        "note": "تعویض توری"})
    L = call("/api/ledger")["metrics"]
    assert L["cost_by_category"]["نگهداری و تعمیرات"] >= 40_000_000
    fixed_after = -sum(x["amount"] for x in
                       call("/api/ledger")["rows"] if x["type"] == "fixed_cost")
    # حالت top_up: هزینه ثابت پایه همان ماه به اندازه هزینه واقعی کم می‌شود
    assert fixed_after == pytest.approx(fixed_before - 40_000_000, rel=1e-6)
    assert L["total_outflow"] == pytest.approx(base["total_outflow"], rel=1e-6)
    E.db.void_txn(r["id"], "پاک‌سازی تست")


def test_fixed_cost_modes(E):
    r = call("/api/transactions", "POST", {
        "txn_type": "operating_cost", "txn_date": "2026-06-11",
        "amount": 30_000_000, "category": "برق و سوخت"})
    out = {}
    for mode in ("top_up", "baseline_only", "actual_only"):
        call("/api/assumptions", "POST", {"key": "cost.fixed_cost_mode", "value": mode})
        out[mode] = call("/api/ledger")["metrics"]["total_outflow"]
    # baseline_only هزینه واقعی را جدا نمی‌شمارد؛ top_up هم دوباره‌شماری نمی‌کند
    assert out["baseline_only"] == pytest.approx(out["top_up"], rel=1e-9)
    assert out["actual_only"] < out["top_up"]
    call("/api/assumptions/reset", "POST", {"key": "cost.fixed_cost_mode"})
    E.db.void_txn(r["id"], "پاک‌سازی تست")


def test_uncategorised_cost_defaults_to_other(E):
    r = call("/api/transactions", "POST", {
        "txn_type": "operating_cost", "txn_date": "2026-07-02", "amount": 1_000_000})
    assert E.db.one("SELECT category FROM transactions WHERE id=?",
                    (r["id"],))["category"] == "سایر"
    E.db.void_txn(r["id"], "پاک‌سازی تست")


# ── ۵ ویرایش رویدادها با حفظ audit trail ────────────────────────────
def test_edit_keeps_original_and_records_reason(E):
    r = call("/api/transactions", "POST", {
        "txn_type": "feed_purchase", "txn_date": "2026-05-02", "quantity": 1000,
        "unit_price": 310000, "payload": {"feed_name": "FP-1"}})
    old_id = r["id"]
    c = call(f"/api/transactions/{old_id}/correct", "POST",
             {"quantity": 1200, "amount": 1200 * 310000,
              "reason": "فاکتور واقعی ۱۲۰۰ کیلو بود"})
    new_id = c["new_id"]

    old = E.db.one("SELECT * FROM transactions WHERE id=?", (old_id,))
    new = E.db.one("SELECT * FROM transactions WHERE id=?", (new_id,))
    assert old is not None                       # حذف نشده
    assert old["status"] == "corrected" and old["quantity"] == 1000   # مقدار اولیه
    assert new["status"] == "active" and new["quantity"] == 1200      # مقدار اصلاح‌شده
    assert new["corrects_id"] == old_id
    assert new["correction_reason"] == "فاکتور واقعی ۱۲۰۰ کیلو بود"
    assert new["created_at"]                     # تاریخ اصلاح

    chain = call(f"/api/transactions/{new_id}/history")["chain"]
    assert [x["id"] for x in chain] == [old_id, new_id]


def test_edit_triggers_recalculation_everywhere(E):
    feed_before = call("/api/feed")
    kg_before = sum(f["qty_kg"] for f in feed_before["feed"])
    row = E.db.q("SELECT * FROM transactions WHERE txn_type='feed_purchase' "
                 "AND status='active' ORDER BY id DESC LIMIT 1")[0]
    call(f"/api/transactions/{row['id']}/correct", "POST",
         {"quantity": row["quantity"] + 500,
          "amount": (row["quantity"] + 500) * row["unit_price"],
          "reason": "تست بازمحاسبه"})
    kg_after = sum(f["qty_kg"] for f in call("/api/feed")["feed"])
    assert kg_after == pytest.approx(kg_before + 500)
    assert call("/api/validate")["checks"]  # همه بررسی‌ها دوباره اجرا می‌شوند


def test_corrected_records_never_disappear(E):
    total = E.db.one("SELECT COUNT(*) n FROM transactions")["n"]
    row = E.db.q("SELECT * FROM transactions WHERE status='active' "
                 "AND txn_type='feed_purchase' ORDER BY id DESC LIMIT 1")[0]
    call(f"/api/transactions/{row['id']}/correct", "POST",
         {"note": "یادداشت جدید", "reason": "اصلاح یادداشت"})
    assert E.db.one("SELECT COUNT(*) n FROM transactions")["n"] == total + 1


# ── ۶ تاریخ اعتبار قیمت‌ها ───────────────────────────────────────────
def test_price_change_is_not_retroactive(E):
    A = E.A
    A.set_effective("egg.base_price", 7200, "2026-10-01", "افزایش مهر")
    assert A.get_at("egg.base_price", "2026-09-30") == 6000    # گذشته دست‌نخورده
    assert A.get_at("egg.base_price", "2026-10-01") == 7200
    assert A.get_at("egg.base_price", "2026-12-31") == 7200


def test_actual_transaction_price_always_wins(E):
    """قیمت ثبت‌شده در تراکنش هرگز با تغییر assumption عوض نمی‌شود."""
    old = E.db.q("SELECT * FROM transactions WHERE txn_type='egg_purchase' "
                 "AND status='active' ORDER BY txn_date")[0]
    E.A.set_effective("egg.base_price", 9999, "2026-01-01", "تست سرایت به گذشته")
    again = E.db.one("SELECT * FROM transactions WHERE id=?", (old["id"],))
    assert again["unit_price"] == old["unit_price"]
    assert again["amount"] == old["amount"]
    # پاک‌سازی
    h = [x for x in E.A.history("egg.base_price") if x["value"] == 9999][0]
    E.db.delete_assumption_history(h["id"])
    E.A.refresh()


def test_feed_price_effective_dating(E):
    A = E.A
    table = json.loads(json.dumps(A.get("feed.price_table")))
    for row in table:
        row["price"] = 320000
    A.set_effective("feed.price_table", table, "2026-10-01", "قیمت جدید خوراک")
    from core.biology import Biology
    assert Biology(A, on_date="2026-09-30").feed_price(1.0) == 310000
    assert Biology(A, on_date="2026-10-05").feed_price(1.0) == 320000


def test_fixed_cost_effective_dating_in_ledger(E):
    A = E.A
    call("/api/assumptions", "POST", {"key": "cost.fixed_cost_mode", "value": "baseline_only"})
    before = -sum(r["amount"] for r in call("/api/ledger")["rows"]
                  if r["type"] == "fixed_cost")
    A.set_effective("cost.fixed_monthly", 150_000_000, "2026-07-01", "افزایش هزینه ثابت")
    rows = call("/api/ledger")["rows"]
    july = [r for r in rows if r["type"] == "fixed_cost" and r["date"] == "2026-07-01"]
    march = [r for r in rows if r["type"] == "fixed_cost" and r["date"] == "2026-03-01"]
    assert july and -july[0]["amount"] == 150_000_000       # از تاریخ اعتبار
    assert march and -march[0]["amount"] == 120_000_000     # گذشته تغییر نکرد
    after = -sum(r["amount"] for r in rows if r["type"] == "fixed_cost")
    assert after > before
    h = [x for x in A.history("cost.fixed_monthly")][0]
    E.db.delete_assumption_history(h["id"])
    A.refresh()
    call("/api/assumptions/reset", "POST", {"key": "cost.fixed_cost_mode"})


def test_effective_dating_rejects_non_dated_keys(E):
    with pytest.raises(ValueError):
        E.A.set_effective("feed.fcr", 1.1, "2026-10-01")


# ── ۷ سقف ماهانه تخم = راهنما، نه محدودیت مطلق ──────────────────────
def test_monthly_egg_limit_is_a_guideline(E):
    chk = _check(call("/api/validate"), "monthly_egg_limit")
    # داده واقعی جولای ۳۲۰k است و نباید تخلف شمرده شود
    assert chk["status"] == "pass"
    assert "320,000" in chk["detail"]


def test_hard_monthly_max_is_optional_and_off_by_default(E):
    assert E.A.get("egg.enforce_hard_monthly_max") is False
    call("/api/assumptions", "POST", {"key": "egg.enforce_hard_monthly_max", "value": True})
    call("/api/assumptions", "POST", {"key": "egg.hard_monthly_max", "value": 300000})
    assert _check(call("/api/validate"), "monthly_egg_limit")["status"] == "fail"
    call("/api/assumptions", "POST", {"key": "egg.hard_monthly_max", "value": 400000})
    assert _check(call("/api/validate"), "monthly_egg_limit")["status"] == "pass"
    call("/api/assumptions/reset", "POST", {"key": "egg.enforce_hard_monthly_max"})
    call("/api/assumptions/reset", "POST", {"key": "egg.hard_monthly_max"})


def test_purchase_above_guideline_is_accepted(E):
    r = call("/api/transactions", "POST", {
        "txn_type": "egg_purchase", "txn_date": "2026-09-05", "quantity": 320000,
        "unit_price": 6000, "counterparty": "تست"})
    assert r["ok"]
    assert _check(call("/api/validate"), "monthly_egg_limit")["status"] in ("pass", "warn")
    E.db.void_txn(r["id"], "پاک‌سازی تست")


# ── ۸ داده واقعی دلار ────────────────────────────────────────────────
def test_fx_uses_real_excel_no_placeholder(E):
    assert E.fx_load["source"] == "file"
    assert E.fx_load["rows"] > 800
    assert E.db.meta_get("fx_source") == "file"
    assert not E.db.q("SELECT 1 FROM fx_daily WHERE source!='TGJU-excel' LIMIT 1")


def test_fx_quarterly_math_matches_spec(E):
    fx = call("/api/fx")
    k = fx["kpi"]
    assert k["available"] and k.get("missing") is False
    assert k["fx_return"] == pytest.approx(k["fx_latest"] / k["fx_start"] - 1)
    assert k["usd_alternative_end_value"] == pytest.approx(
        k["capital"] * k["benchmark_share"] * k["fx_latest"] / k["fx_start"])
    for q in fx["quarters"]:
        if q.get("available") is False:
            assert "موجود نیست" in q["note"]          # ساخته نمی‌شود
        else:
            assert q["fx_return"] == pytest.approx(q["fx_end"] / q["fx_start"] - 1)


def test_fx_known_values_from_file(E):
    """چند نقطه از فایل واقعی، برای اطمینان از خواندن ستون درست."""
    first = E.db.one("SELECT * FROM fx_daily ORDER BY date_g LIMIT 1")
    last = E.db.one("SELECT * FROM fx_daily ORDER BY date_g DESC LIMIT 1")
    assert first["date_g"] == "2023-08-10" and first["close_toman"] == 49673
    assert last["date_g"] == "2026-08-09" and last["close_toman"] == 185400


# ── ۹ پشتیبان‌گیری و خروجی ───────────────────────────────────────────
def test_json_export_contains_everything(E):
    x = call("/api/export/json")
    for t in ("transactions", "ponds", "egg_offers", "assumption_overrides",
              "assumption_history", "fx_daily", "app_meta"):
        assert t in x["tables"]
    assert x["counts"]["ponds"] == 21
    assert x["counts"]["transactions"] > 0
    # audit trail هم داخل خروجی هست
    assert any(r.get("corrects_id") for r in x["tables"]["transactions"])


def test_sqlite_backup_is_valid_database(E):
    import sqlite3
    import tempfile
    blob = E.db.backup_bytes()
    assert blob[:15] == b"SQLite format 3"
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(blob)
        con = sqlite3.connect(path)
        n = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        assert n == E.db.one("SELECT COUNT(*) n FROM transactions")["n"]
        con.close()
    finally:
        os.remove(path)


def test_csv_export(E):
    csv_text = E.db.export_csv("transactions")
    assert csv_text.startswith("\ufeff")
    header = csv_text.splitlines()[0]
    for col in ("txn_type", "txn_date", "quantity", "unit", "amount",
                "counterparty", "category", "created_at", "correction_reason"):
        assert col in header
    with pytest.raises(ValueError):
        E.db.export_csv("sqlite_master")


# ── ۱۰ سه فروش تاریخی ────────────────────────────────────────────────
def test_three_historical_sales_recorded(E):
    rows = E.db.q("SELECT * FROM transactions WHERE txn_type='sale' "
                  "AND cohort_id IS NULL AND status='active' ORDER BY txn_date")
    assert len(rows) == 3
    assert sum(r["quantity"] for r in rows) == 195_000
    assert sum(r["amount"] for r in rows) == 1_567_000_000
    for r, spec in zip(rows, OBSERVED_SALES):
        assert r["unit_price"] == pytest.approx(spec["amount"] / spec["qty"])


def test_march_sale_weight_came_from_the_user_not_a_guess(E):
    """
    وزن این فروش ابتدا نامشخص بود و سیستم آن را حدس نزد. بعداً کاربر اعلام
    کرد ۶ گرم بوده است؛ از آن لحظه به‌عنوان داده واقعی ثبت شده.
    """
    r = E.db.one("SELECT * FROM transactions WHERE txn_type='sale' "
                 "AND txn_date='2026-03-16' AND status='active'")
    assert r["weight_g"] == 6.0
    pl = json.loads(r["payload"])
    assert pl["weight_known"] is True
    assert r["data_source"] == "actual"


def test_historical_sales_stay_cohort_unassigned(E):
    s = call("/api/summary")
    assert s["unassigned_sales_count"] == 3
    assert s["unassigned_sales_fish"] == 195_000
    chk = _check(call("/api/validate"), "historical_sales")
    assert chk["status"] == "warn" and "195,000" in chk["detail"]
    # هیچ cohort ی حدس زده نشده است
    assert not E.db.q("SELECT 1 FROM transactions WHERE txn_type='sale' "
                      "AND cohort_id IS NOT NULL AND note LIKE 'Historical Sale%'")


def test_historical_sales_are_in_cash_ledger(E):
    """
    درآمد کامل ثبت می‌شود، ولی دریافت وجه طبق شرایط پرداخت مشتری
    (۵۰٪ نقد، ۵۰٪ پس از ۴۵ روز) در دو تاریخ می‌نشیند. پس جمع باید بر مبنای
    تاریخ شناسایی درآمد (accrual) گرفته شود، نه تاریخ دریافت.
    """
    rows = call("/api/ledger")["rows"]
    sale_dates = ("2026-03-16", "2026-06-14", "2026-08-01")
    total = sum(r["amount"] for r in rows
                if r["type"] == "sale" and r["accrual"] in sale_dates)
    assert total == pytest.approx(1_567_000_000)
    # هر فروش دقیقاً به دو دریافت تقسیم شده است
    for dt in sale_dates:
        parts = [r for r in rows if r["type"] == "sale" and r["accrual"] == dt]
        assert len(parts) == 2
        assert parts[0]["amount"] == pytest.approx(parts[1]["amount"])


def test_historical_prices_do_not_affect_baseline_price_curve(E):
    """قیمت‌های تخفیف‌دار گذشته نباید وارد منحنی قیمت پایه شوند."""
    _, bio, _ = E.ctx()
    assert bio.sale_price(1.0) == 11000            # نه ۸٬۷۵۰ فروش ۱۴ ژوئن
    assert bio.sale_price(2.0) == 11800            # نه ۱۱٬۴۲۹ فروش ۱ اوت
    assert bio.sale_price(15.0) == 22200


# ── validation کلی ───────────────────────────────────────────────────
def _check(v, cid):
    return next(c for c in v["checks"] if c["id"] == cid)


def test_all_ten_plus_checks_run_and_none_fail(E):
    v = call("/api/validate")
    ids = {c["id"] for c in v["checks"]}
    assert ids >= {"mass_balance", "cohort_balance", "pond_capacity", "integer_ponds",
                   "feed_reconciliation", "cash_ledger", "monthly_egg_limit",
                   "offer_consistency", "fx_reconciliation", "regression_core",
                   "historical_sales", "effective_dating", "audit_trail",
                   "transaction_fields"}
    assert v["failed"] == 0, [c for c in v["checks"] if c["status"] == "fail"]


def test_mass_and_cohort_balance_after_all_edits(E):
    v = call("/api/validate")
    assert _check(v, "mass_balance")["status"] == "pass"
    assert _check(v, "cohort_balance")["status"] == "pass"
    assert _check(v, "audit_trail")["status"] == "pass"
    assert _check(v, "pond_capacity")["status"] in ("pass", "warn")
    assert _check(v, "feed_reconciliation")["status"] == "pass"
