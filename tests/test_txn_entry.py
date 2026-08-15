"""
تست‌های end-to-end ثبت رویداد (تب تراکنش‌ها).

باگ اصلی: فرم فقط وقتی ساخته می‌شد که فهرست تراکنش‌ها خالی بود، و چون فهرست
هنگام راه‌اندازی پر می‌شد، فرم هرگز ساخته نمی‌شد و دکمه «ثبت رویداد» روی یک
DOM خالی کار می‌کرد.

اینجا هم مسیر API و هم اثر واقعی هر رویداد روی Live Farm State آزمایش می‌شود.
"""
import json
import os
import re
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as m                                        # noqa: E402
from app import Engine, api                            # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "test_txn.db")


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


def today(E):
    return E.ctx()[2].as_of.isoformat()


# ── رگرسیون خودِ باگ ────────────────────────────────────────────────
def test_form_is_built_even_when_transactions_exist():
    """
    رگرسیون باگ: ساخت فرم نباید به خالی بودن فهرست تراکنش‌ها مشروط باشد.
    """
    js = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
    assert "if (t === \"txns\" && !TXNS.length) { buildTxnForm(); loadTxns(); }" not in js
    # فرم هنگام راه‌اندازی هم ساخته می‌شود
    assert re.search(r"await loadTxns\(\);\s*\n\s*buildTxnForm\(\);", js)
    # و هنگام باز شدن تب، بر مبنای خالی بودن خود فرم
    assert 'if (!$("#txnForm").children.length) buildTxnForm();' in js


def test_form_result_area_exists():
    html = open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    assert 'id="txnResult"' in html
    assert 'id="btnAddTxn"' in html
    assert 'id="txnForm"' in html


def test_every_form_type_has_field_definitions():
    js = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
    labels = set(re.findall(r"(\w+): \"[^\"]+\"", js.split("const TXN_LABELS")[1]
                            .split("};")[0]))
    fields = set(re.findall(r"^  (\w+):", js.split("const TXN_FIELDS")[1]
                            .split("};")[0], re.M))
    assert labels <= fields, f"بدون تعریف فیلد: {labels - fields}"


# ── ۱. خرید تخم → cohort ساخته می‌شود ───────────────────────────────
def test_egg_purchase_creates_cohort(E):
    before = set(E.ctx()[2].cohorts)
    r = call("/api/transactions", "POST", {
        "txn_type": "egg_purchase", "txn_date": "2026-08-05",
        "cohort_id": "C-TEST-EGG", "quantity": 120_000, "unit_price": 6100,
        "counterparty": "تأمین‌کننده تست", "note": "تست ثبت رویداد"})
    assert r["ok"] and r["id"] > 0
    assert r["message"] == "Transaction saved successfully"
    assert r["created_at"] and r["txn_date"] == "2026-08-05"

    st = E.ctx()[2]
    assert "C-TEST-EGG" in set(st.cohorts) - before
    c = st.cohorts["C-TEST-EGG"]
    assert c.egg_count == 120_000
    assert c.egg_price == 6100
    assert c.alive < 120_000            # منحنی تلفات اعمال شده
    assert c.alive > 0

    row = E.db.one("SELECT * FROM transactions WHERE id=?", (r["id"],))
    assert row["amount"] == 120_000 * 6100
    assert row["unit"] == "عدد"
    assert row["data_source"] == "actual"
    assert row["status"] == "active"


def test_egg_purchase_appears_in_cash_ledger(E):
    rows = call("/api/ledger")["rows"]
    assert any(r["type"] == "egg_purchase" and abs(r["amount"] + 120_000 * 6100) < 1
               for r in rows)


# ── ۲. خرید خوراک → موجودی انبار ────────────────────────────────────
def test_feed_purchase_updates_inventory(E):
    before = {f["name"]: f["qty_kg"] for f in call("/api/feed")["feed"]}
    name = E.ctx()[1].feed_bands[0]["name"]
    r = call("/api/transactions", "POST", {
        "txn_type": "feed_purchase", "txn_date": today(E), "quantity": 800,
        "unit_price": 312_000, "counterparty": "فروشنده خوراک",
        "payload": {"feed_name": name}})
    assert r["ok"]
    after = {f["name"]: f["qty_kg"] for f in call("/api/feed")["feed"]}
    assert after[name] == pytest.approx(before.get(name, 0) + 800)
    inv = next(f for f in call("/api/feed")["feed"] if f["name"] == name)
    assert inv["avg_cost"] > 0
    assert inv["value"] == pytest.approx(inv["qty_kg"] * inv["avg_cost"], rel=1e-6)


def test_feed_consumption_reduces_inventory(E):
    name = E.ctx()[1].feed_bands[0]["name"]
    before = next(f for f in call("/api/feed")["feed"] if f["name"] == name)["qty_kg"]
    call("/api/transactions", "POST", {
        "txn_type": "feed_consumption", "txn_date": today(E), "quantity": 120,
        "payload": {"feed_name": name}})
    after = next(f for f in call("/api/feed")["feed"] if f["name"] == name)["qty_kg"]
    assert after == pytest.approx(before - 120)


# ── ۳. تلفات → کاهش موجودی زنده ─────────────────────────────────────
def test_mortality_reduces_live_fish(E):
    st = E.ctx()[2]
    cid = "C-TEST-EGG"
    before_cohort = st.cohorts[cid].alive
    before_total = call("/api/summary")["live_fish"]

    r = call("/api/transactions", "POST", {
        "txn_type": "mortality", "txn_date": today(E),
        "cohort_id": cid, "quantity": 5_000, "note": "تلفات تست"})
    assert r["ok"]

    st2 = E.ctx()[2]
    assert st2.cohorts[cid].alive == pytest.approx(before_cohort - 5_000, abs=2)
    assert st2.cohorts[cid].recorded_mortality >= 5_000
    assert call("/api/summary")["live_fish"] < before_total


# ── ۴. فروش → cohort و دفتر نقدی ────────────────────────────────────
def test_sale_updates_cohort_and_cash_ledger(E):
    cid = "C-TEST-EGG"
    st = E.ctx()[2]
    before_alive = st.cohorts[cid].alive
    before_in = call("/api/ledger")["metrics"]["total_inflow"]

    r = call("/api/transactions", "POST", {
        "txn_type": "sale", "txn_date": today(E), "cohort_id": cid,
        "quantity": 10_000, "weight_g": 1.0, "unit_price": 11_000,
        "counterparty": "خریدار تست"})
    assert r["ok"]
    assert r["amount"] == pytest.approx(110_000_000)

    st2 = E.ctx()[2]
    assert st2.cohorts[cid].alive == pytest.approx(before_alive - 10_000, abs=2)
    assert st2.cohorts[cid].sold_count >= 10_000
    assert st2.cohorts[cid].sold_revenue >= 110_000_000

    L = call("/api/ledger")
    assert L["metrics"]["total_inflow"] > before_in
    # دریافت طبق شرایط پرداخت به دو بخش تقسیم می‌شود
    parts = [x for x in L["rows"]
             if x["type"] == "sale" and x["accrual"] == today(E)]
    assert len(parts) == 2
    assert sum(x["amount"] for x in parts) == pytest.approx(110_000_000)


# ── ۵. هزینه نگهداری → دفتر نقدی ────────────────────────────────────
def test_maintenance_cost_appears_in_cash_ledger(E):
    before = call("/api/ledger")["metrics"]["cost_by_category"].get(
        "نگهداری و تعمیرات", 0)
    r = call("/api/transactions", "POST", {
        "txn_type": "operating_cost", "txn_date": today(E),
        "amount": 25_000_000, "category": "نگهداری و تعمیرات",
        "counterparty": "تعمیرکار", "note": "تعمیر پمپ اکسیژن"})
    assert r["ok"]
    L = call("/api/ledger")
    assert L["metrics"]["cost_by_category"]["نگهداری و تعمیرات"] == \
        pytest.approx(before + 25_000_000)
    assert any(x["type"] == "operating_cost" and x["amount"] == -25_000_000
               for x in L["rows"])
    row = E.db.one("SELECT * FROM transactions WHERE id=?", (r["id"],))
    assert row["category"] == "نگهداری و تعمیرات"


def test_maintenance_alias_maps_to_operating_cost():
    js = open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
    assert "TXN_ALIAS" in js
    assert '"operating_cost"' in js.split("TXN_ALIAS")[1][:300]


# ── بقیه انواع رویداد ───────────────────────────────────────────────
def test_weight_sample_moves_the_growth_anchor(E):
    cid = "C-TEST-EGG"
    before = E.ctx()[2].cohorts[cid]
    r = call("/api/transactions", "POST", {
        "txn_type": "weight_sample", "txn_date": today(E), "cohort_id": cid,
        "weight_g": 0.4, "quantity": 100, "payload": {"sd_g": 0.05}})
    assert r["ok"]
    after = E.ctx()[2].cohorts[cid]
    assert after.weight_basis == "actual"
    assert after.mean_weight == pytest.approx(0.4, rel=0.05)
    assert after.growth_offset_days != before.growth_offset_days


def test_pond_transfer_moves_fish(E):
    cid = "C-TEST-EGG"
    r = call("/api/transactions", "POST", {
        "txn_type": "transfer", "txn_date": today(E), "cohort_id": cid,
        "to_pond_id": "P19", "quantity": 3_000})
    assert r["ok"]
    st = E.ctx()[2]
    assert st.cohorts[cid].alloc.get("P19", 0) == pytest.approx(3_000, abs=5)
    pond = next(p for p in st.pond_view() if p["pond_id"] == "P19")
    assert any(o["cohort_id"] == cid and o["basis"] == "actual"
               for o in pond["occupants"])


def test_count_observation_pins_the_count(E):
    cid = "C-TEST-EGG"
    r = call("/api/transactions", "POST", {
        "txn_type": "count_observation", "txn_date": today(E),
        "cohort_id": cid, "quantity": 70_000})
    assert r["ok"]
    st = E.ctx()[2]
    assert st.cohorts[cid].alive == pytest.approx(70_000, abs=2)
    assert st.cohorts[cid].count_basis.startswith("actual") or \
        st.cohorts[cid].count_basis == "estimated_from_actual"


def test_water_reading_is_stored(E):
    r = call("/api/transactions", "POST", {
        "txn_type": "water_reading", "txn_date": today(E), "pond_id": "P19",
        "payload": {"temperature_c": 11.2, "do_in": 8.1, "do_out": 6.4,
                    "flow_l_s": 15.0}})
    assert r["ok"]
    st = E.ctx()[2]
    w = st.water_readings.get("P19")
    assert w and w["temperature_c"] == 11.2 and w["do_out"] == 6.4


def test_payment_and_receipt(E):
    for typ, sign in (("payment", -1), ("receipt", +1)):
        r = call("/api/transactions", "POST", {
            "txn_type": typ, "txn_date": today(E), "amount": 50_000_000,
            "counterparty": "طرف تست"})
        assert r["ok"]
        rows = call("/api/ledger")["rows"]
        assert any(x["txn_id"] == r["id"] and
                   abs(x["amount"] - sign * 50_000_000) < 1 for x in rows)


# ── اعتبارسنجی: هیچ رویداد ناقصی ذخیره نمی‌شود ──────────────────────
def _count(E):
    return E.db.one("SELECT COUNT(*) n FROM transactions")["n"]


@pytest.mark.parametrize("body,expect", [
    ({"txn_type": "mortality", "quantity": 100}, "cohort"),
    ({"txn_type": "mortality", "cohort_id": "C-TEST-EGG"}, "تعداد"),
    ({"txn_type": "weight_sample", "cohort_id": "C-TEST-EGG"}, "وزن"),
    ({"txn_type": "transfer", "cohort_id": "C-TEST-EGG", "quantity": 10}, "مقصد"),
    ({"txn_type": "feed_purchase", "quantity": 10, "unit_price": 1}, "خوراک"),
    ({"txn_type": "sale", "quantity": 10}, "قیمت"),
    ({"txn_type": "operating_cost"}, "مبلغ"),
    ({"txn_type": "water_reading", "pond_id": "P19"}, "دما"),
    ({"txn_type": "mortality", "cohort_id": "NOPE", "quantity": 5}, "ناشناخته"),
    ({"txn_type": "mortality", "cohort_id": "C-TEST-EGG", "quantity": 5,
      "pond_id": "P99"}, "استخر ناشناخته"),
    ({"txn_type": "egg_purchase", "quantity": -5}, "منفی"),
])
def test_invalid_transaction_is_rejected_and_nothing_saved(E, body, expect):
    before = _count(E)
    body = {**body, "txn_date": today(E)}
    with pytest.raises(ValueError) as ex:
        call("/api/transactions", "POST", body)
    assert expect in str(ex.value)
    assert _count(E) == before, "رویداد ناقص نباید ذخیره شود"


def test_unknown_type_is_rejected(E):
    before = _count(E)
    with pytest.raises(ValueError, match="نوع رویداد نامعتبر"):
        call("/api/transactions", "POST", {"txn_type": "nonsense"})
    assert _count(E) == before


def test_bad_date_is_rejected(E):
    before = _count(E)
    with pytest.raises(ValueError):
        call("/api/transactions", "POST", {
            "txn_type": "operating_cost", "txn_date": "not-a-date",
            "amount": 1000})
    assert _count(E) == before


# ── ویرایش پس از ثبت، با حفظ audit trail ────────────────────────────
def test_edit_after_create_keeps_audit_trail(E):
    r = call("/api/transactions", "POST", {
        "txn_type": "operating_cost", "txn_date": today(E),
        "amount": 3_000_000, "category": "سایر", "note": "قبل از اصلاح"})
    c = call(f"/api/transactions/{r['id']}/correct", "POST", {
        "amount": 4_500_000, "reason": "فاکتور واقعی متفاوت بود"})

    old = E.db.one("SELECT * FROM transactions WHERE id=?", (r["id"],))
    new = E.db.one("SELECT * FROM transactions WHERE id=?", (c["new_id"],))
    assert old["status"] == "corrected" and old["amount"] == 3_000_000
    assert new["status"] == "active" and new["amount"] == 4_500_000
    assert new["corrects_id"] == r["id"]
    assert new["correction_reason"] == "فاکتور واقعی متفاوت بود"

    chain = call(f"/api/transactions/{c['new_id']}/history")["chain"]
    assert [x["id"] for x in chain] == [r["id"], c["new_id"]]
    # اثر مالی هم به‌روز شده است
    assert any(x["txn_id"] == c["new_id"] and abs(x["amount"] + 4_500_000) < 1
               for x in call("/api/ledger")["rows"])


def test_void_keeps_the_record(E):
    r = call("/api/transactions", "POST", {
        "txn_type": "operating_cost", "txn_date": today(E),
        "amount": 1_000_000, "category": "سایر"})
    call(f"/api/transactions/{r['id']}/void", "POST", {"note": "اشتباه بود"})
    row = E.db.one("SELECT * FROM transactions WHERE id=?", (r["id"],))
    assert row is not None and row["status"] == "void"
    assert not any(x["txn_id"] == r["id"] for x in call("/api/ledger")["rows"])


# ── همه‌چیز پس از ثبت بازمحاسبه می‌شود ──────────────────────────────
def test_everything_refreshes_after_a_transaction(E):
    b1 = call("/api/bootstrap")
    call("/api/transactions", "POST", {
        "txn_type": "mortality", "txn_date": today(E),
        "cohort_id": "C-TEST-EGG", "quantity": 2_000})
    b2 = call("/api/bootstrap")
    assert b2["summary"]["live_fish"] < b1["summary"]["live_fish"]
    assert b2["validation"]["failed"] == 0
    # forecast، ظرفیت و دفتر نقدی همگی دوباره ساخته می‌شوند
    assert b2["checkpoints"] != b1["checkpoints"] or b2["checkpoints"]
    assert b2["capacity_curve"]
    assert b2["ledger"]["closing_balance"] is not None


def test_transaction_list_endpoint_reflects_new_rows(E):
    before = len(call("/api/transactions")["transactions"])
    call("/api/transactions", "POST", {
        "txn_type": "operating_cost", "txn_date": today(E),
        "amount": 900_000, "category": "اداری"})
    after = call("/api/transactions")["transactions"]
    assert len(after) == before + 1
    assert after[0]["amount"] == 900_000 or any(
        x["amount"] == 900_000 for x in after)


def test_all_saved_transactions_have_id_and_created_at(E):
    for t in E.db.q("SELECT * FROM transactions"):
        assert t["id"] and t["created_at"] and t["txn_date"]
        assert t["txn_type"]
        json.loads(t["payload"] or "{}")     # payload همیشه JSON معتبر است
