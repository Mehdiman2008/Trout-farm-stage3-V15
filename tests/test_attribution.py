"""
تست‌های تشخیص cohort از روی وزن فروخته‌شده.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as m                                    # noqa: E402
from app import Engine, api                        # noqa: E402
from core import attribution as ATTR               # noqa: E402
from core.seed import OBSERVED_SALES               # noqa: E402

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "test_attr.db")


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


def sale_on(E, day):
    return E.db.one("SELECT * FROM transactions WHERE txn_type='sale' "
                    "AND txn_date=? AND status='active'", (day,))


# ── وزن اعلام‌شده ۱۶ مارس ───────────────────────────────────────────
def test_march_weight_is_now_recorded_as_six_grams(E):
    r = sale_on(E, "2026-03-16")
    assert r["weight_g"] == 6.0
    assert json.loads(r["payload"])["weight_known"] is True


def test_weight_backfill_corrects_without_overwriting(E):
    """
    روی پایگاه داده‌ای که وزن نداشته، اعلام وزن جدید باید به‌صورت correction
    اعمال شود و نسخه اولیه (وزن خالی) در تاریخچه بماند.
    """
    from core.seed import WEIGHT_FLAG, backfill_observed_weights
    old_id = E.db.add_txn("sale", "2026-03-16", quantity=80_000, weight_g=None,
                          amount=467_000_000, unit_price=5837.5,
                          data_source="actual", note="تست backfill")
    E.db.meta_set(WEIGHT_FLAG, "")
    try:
        res = backfill_observed_weights(E.db)
        assert res["applied"]
        corr = [c for c in res["corrections"] if c["old_id"] == old_id]
        assert corr, "باید این رکورد اصلاح می‌شد"
        chain = E.db.txn_chain(corr[0]["new_id"])
        assert len(chain) == 2
        assert chain[0]["weight_g"] is None and chain[0]["status"] == "corrected"
        assert chain[1]["weight_g"] == 6.0
        assert "۶ گرم" in (chain[1]["correction_reason"] or "")
        E.db.void_txn(corr[0]["new_id"], "پاک‌سازی تست")
    finally:
        E.db.meta_set(WEIGHT_FLAG, "1")


def test_seed_definition_carries_the_observed_weight():
    march = OBSERVED_SALES[0]
    assert march["date"] == "2026-03-16" and march["weight_g"] == 6.0


# ── منطق تشخیص ──────────────────────────────────────────────────────
def test_suggestion_needs_a_weight(E):
    A, bio, st = E.ctx()
    fake = {"id": -1, "txn_date": "2026-06-14", "quantity": 1000,
            "weight_g": None, "cohort_id": None}
    s = ATTR.suggest(A, bio, st, fake)
    assert s["needs_weight"] is True
    assert s["confidence"] == "none"
    assert not s["candidates"]


def test_implied_purchase_date_backsolves_the_growth_curve(E):
    A, bio, st = E.ctx()
    s = ATTR.suggest(A, bio, st, sale_on(E, "2026-03-16"))
    # ۶ گرم ≈ روز ۱۳۱ (Growth Curve جدید: ۵g=۱۲۶، ۱۰g=۱۴۶) → اوایل نوامبر ۲۰۲۵
    assert s["implied_purchase_date"] == "2025-11-05"
    assert 126 < s["implied_age_days"] < 146


def test_impossible_weight_scores_zero_even_with_stock(E):
    """موجودی کافی نباید یک وزن فیزیکاً ناممکن را نجات دهد."""
    A, bio, st = E.ctx()
    s = ATTR.suggest(A, bio, st, sale_on(E, "2026-03-16"))
    c01 = next(c for c in s["candidates"] if c["cohort_id"] == "C01-20260217")
    assert c01["available_fish"] > 80_000        # موجودی هست
    assert c01["expected_weight_g"] < 1.0        # ولی وزن اصلاً نمی‌خواند
    assert c01["score"] == 0.0
    assert any("ناممکن" in b for b in c01["blockers"])


def test_march_sale_matches_no_recorded_cohort(E):
    A, bio, st = E.ctx()
    s = ATTR.suggest(A, bio, st, sale_on(E, "2026-03-16"))
    assert s["confidence"] == "none"
    assert s["best"] is None
    hint = s["missing_cohort_hint"]
    assert hint["purchase_date"] == "2025-11-05"
    assert hint["suggested_egg_count"] > 80_000   # با احتساب تلفات


def st_cohort_ids(E):
    return set(E.ctx()[2].cohorts)


@pytest.fixture(scope="module")
def jmatch(E):
    """
    cohortی که سیستم برای فروش ۱۴ ژوئن پیشنهاد می‌دهد.

    یک بار روی داده اولیه محاسبه می‌شود و به شناسه ثابت وابسته نیست، چون
    شناسه‌ها با تغییر خریدهای ثبت‌شده جابه‌جا می‌شوند.
    """
    A, bio, st = E.ctx()
    row = E.db.one("SELECT * FROM transactions WHERE txn_type='sale' "
                   "AND txn_date='2026-06-14' AND status='active'")
    return ATTR.suggest(A, bio, st, row)["best"]


def test_june_sale_suggests_a_candidate_with_explicit_caveats(E):
    """
    پس از حذف خرید اشتباه ۳ آوریل، هیچ cohort ثبت‌شده‌ای نمی‌تواند ۸۰٬۰۰۰
    قطعه ۱ گرمی را در ۱۴ ژوئن تأمین کند. سیستم باید این را صریح بگوید،
    نه اینکه یک تطابق ساختگی بسازد.
    """
    A, bio, st = E.ctx()
    s = ATTR.suggest(A, bio, st, sale_on(E, "2026-06-14"))
    assert s["best"] in st_cohort_ids(E) or s["best"] is None
    best = s["candidates"][0]
    assert best["available_fish"] < 80_000
    assert any("موجودی" in b for b in best["blockers"])
    assert s["implied_purchase_date"]


def test_growth_multiplier_direction_is_correct(E):
    """gm>1 یعنی سریع‌تر از مدل، gm<1 یعنی کندتر — برای هر کاندید."""
    A, bio, st = E.ctx()
    for day, sold_w in (("2026-06-14", 1.0), ("2026-08-01", 2.0)):
        s = ATTR.suggest(A, bio, st, sale_on(E, day))
        for c in s["candidates"]:
            if not c["eligible"] or c.get("implied_growth_multiplier") is None:
                continue
            gm = c["implied_growth_multiplier"]
            if c["expected_weight_g"] < sold_w:
                assert gm > 1.0        # باید سریع‌تر از مدل رشد کرده باشد
            elif c["expected_weight_g"] > sold_w:
                assert gm < 1.0        # کندتر از مدل


def test_cohort_bought_after_the_sale_is_ineligible(E):  # noqa: D103
    A, bio, st = E.ctx()
    s = ATTR.suggest(A, bio, st, sale_on(E, "2026-03-16"))
    later = next(c for c in s["candidates"]
                 if c["purchase_date"] > "2026-03-16")
    assert later["eligible"] is False
    assert later["score"] == 0.0
    assert any("خریداری نشده" in b for b in later["blockers"])


def test_candidates_are_ranked(E):
    A, bio, st = E.ctx()
    s = ATTR.suggest(A, bio, st, sale_on(E, "2026-06-14"))
    scores = [c["score"] for c in s["candidates"]]
    assert scores == sorted(scores, reverse=True)


# ── تخصیص و ویرایش توسط کاربر ───────────────────────────────────────
def test_assign_updates_state_and_keeps_audit_trail(E, jmatch):
    sale = sale_on(E, "2026-06-14")
    before = call("/api/summary")["unassigned_sales_count"]
    r = call(f"/api/sales/{sale['id']}/assign", "POST",
             {"cohort_id": jmatch, "reason": "تست تخصیص"})
    assert r["ok"] and r["cohort_id"] == jmatch

    old = E.db.one("SELECT * FROM transactions WHERE id=?", (sale["id"],))
    assert old["status"] == "corrected" and old["cohort_id"] is None   # مقدار اولیه
    new = E.db.one("SELECT * FROM transactions WHERE id=?", (r["new_id"],))
    assert new["cohort_id"] == jmatch
    assert new["correction_reason"] == "تست تخصیص"
    assert json.loads(new["payload"])["reconciliation"] == "resolved"

    s = call("/api/summary")
    assert s["unassigned_sales_count"] == before - 1
    c3 = next(c for c in call("/api/cohorts")["cohorts"]
              if c["cohort_id"] == jmatch)
    # هرگز بیش از موجودی واقعی از یک cohort کم نمی‌شود
    assert 0 < c3["sold_count"] <= 80_000
    assert c3["alive"] < c3["egg_count"]


def test_user_can_change_the_assignment_afterwards(E):
    other = sorted(st_cohort_ids(E))[0]
    cur = E.db.one("SELECT * FROM transactions WHERE txn_type='sale' "
                   "AND txn_date='2026-06-14' AND status='active'")
    r = call(f"/api/sales/{cur['id']}/assign", "POST",
             {"cohort_id": other, "reason": "تصحیح دستی مدیر"})
    new = E.db.one("SELECT * FROM transactions WHERE id=?", (r["new_id"],))
    assert new["cohort_id"] == other
    assert len(E.db.txn_chain(r["new_id"])) >= 3      # زنجیره کامل حفظ شده


def test_user_can_clear_the_assignment(E, jmatch):
    cur = E.db.one("SELECT * FROM transactions WHERE txn_type='sale' "
                   "AND txn_date='2026-06-14' AND status='active'")
    r = call(f"/api/sales/{cur['id']}/assign", "POST",
             {"cohort_id": None, "reason": "بازگشت به نامشخص", "method": "cleared"})
    new = E.db.one("SELECT * FROM transactions WHERE id=?", (r["new_id"],))
    assert new["cohort_id"] is None
    assert json.loads(new["payload"])["cohort_unassigned"] is True
    assert json.loads(new["payload"])["reconciliation"] == "needs_more_info"
    # دوباره تخصیص بده تا وضعیت برای تست‌های بعدی پایدار بماند
    call(f"/api/sales/{new['id']}/assign", "POST",
         {"cohort_id": jmatch, "reason": "بازگردانی"})


def test_assign_can_also_record_the_weight(E):
    aug = sale_on(E, "2026-08-01")
    r = call(f"/api/sales/{aug['id']}/assign", "POST",
             {"weight_g": 2.5, "reason": "وزن دقیق‌تر"})
    new = E.db.one("SELECT * FROM transactions WHERE id=?", (r["new_id"],))
    assert new["weight_g"] == 2.5
    assert new["cohort_id"] is None            # هنوز تخصیص نیافته
    call(f"/api/sales/{new['id']}/assign", "POST",
         {"weight_g": 2.0, "reason": "بازگردانی وزن اصلی"})


def test_assign_rejects_non_sale(E):
    egg = E.db.one("SELECT * FROM transactions WHERE txn_type='egg_purchase' LIMIT 1")
    with pytest.raises(ValueError):
        ATTR.assign(E.db, egg["id"], "C01-20260217")


# ── cohort استنتاجی ─────────────────────────────────────────────────
def test_create_implied_cohort_from_march_sale(E):
    march = E.db.one("SELECT * FROM transactions WHERE txn_type='sale' "
                     "AND txn_date='2026-03-16' AND status='active'")
    r = call(f"/api/sales/{march['id']}/implied-cohort", "POST", {})
    assert r["ok"]
    assert r["purchase_date"] == "2025-11-05"
    assert r["egg_count"] > 80_000

    egg = E.db.one("SELECT * FROM transactions WHERE id=?", (r["egg_purchase_id"],))
    assert egg["data_source"] == "estimated"       # هرگز Observed نیست
    pl = json.loads(egg["payload"])
    assert pl["inferred"] is True and pl["cost_unknown"] is True
    assert egg["amount"] == 0                      # دفتر نقدی آلوده نمی‌شود

    # فروش به همان cohort تخصیص یافت
    sale = E.db.one("SELECT * FROM transactions WHERE id=?", (r["assigned_txn"],))
    assert sale["cohort_id"] == r["cohort_id"]


def test_inferred_cohort_appears_in_state_and_absorbs_the_sale(E):
    b = call("/api/bootstrap")
    inf = next(c for c in b["cohorts"] if c["cohort_id"].endswith("-INF"))
    assert inf["sold_count"] == 80_000
    # فروش مارس جذب شد؛ بقیه فروش‌ها هنوز تخصیص نیافته‌اند
    assert not any(x["date"] == "2026-03-16"
                   for x in b["unassigned_sales"])


def test_inferred_cohort_is_flagged_by_validation(E):
    v = call("/api/validate")
    chk = next(c for c in v["checks"] if c["id"] == "inferred_cohorts")
    assert chk["status"] == "warn"
    assert "Estimated" in chk["detail"]
    assert v["failed"] == 0


def test_duplicate_implied_cohort_is_refused(E):
    march = E.db.one("SELECT * FROM transactions WHERE txn_type='sale' "
                     "AND txn_date='2026-03-16' AND status='active'")
    with pytest.raises(ValueError):
        call(f"/api/sales/{march['id']}/implied-cohort", "POST",
             {"cohort_id": "CH-20251116-INF"})


# ── اثر روی وضعیت کلی ───────────────────────────────────────────────
def test_attribution_relieves_pond_pressure(E):
    """با تخصیص فروش‌ها، موجودی زنده و فشار ظرفیت واقعی‌تر می‌شود."""
    b = call("/api/bootstrap")
    chk = next(c for c in b["validation"]["checks"] if c["id"] == "integer_ponds")
    assert chk["status"] in ("pass", "warn")
    # تخصیص فروش‌ها موجودی زنده را از کل تخم خریداری‌شده کمتر می‌کند
    assert b["summary"]["live_fish"] < 650_000


def test_unassigned_list_endpoint(E):
    r = call("/api/sales/unassigned")
    assert r["cohort_ids"]
    assert all("2026-03-16" != s["sale_date"] for s in r["sales"])
    for s in r["sales"]:
        assert "candidates" in s and "confidence" in s


def test_mass_balance_holds_after_all_attribution(E):
    v = call("/api/validate")
    assert next(c for c in v["checks"] if c["id"] == "mass_balance")["status"] == "pass"
    assert next(c for c in v["checks"]
                if c["id"] == "cohort_balance")["status"] == "pass"
    assert next(c for c in v["checks"] if c["id"] == "audit_trail")["status"] == "pass"
    assert v["failed"] == 0
