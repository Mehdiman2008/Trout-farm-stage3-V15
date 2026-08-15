"""
seed.py — بارگذاری داده واقعی تثبیت‌شده (Observed Data)
========================================================
فقط چیزهایی که در specification به‌عنوان داده واقعی آمده‌اند اینجا وارد
می‌شوند. هیچ فروش، تلفات، وزن‌کشی یا خرید خوراکی ساخته نمی‌شود، چون
چنین داده‌ای در اختیار ما نیست.

هر خرید واقعی به‌صورت یک Egg Offer پذیرفته‌شده هم ثبت می‌شود تا معماری
Offer-Based (Fix 1) از همان ابتدا برقرار باشد.

داده نمونه (--demo) اختیاری است، با برچسب [DEMO] در note و قابل حذف.
"""
from __future__ import annotations

import json
from datetime import date

# ---- داده واقعی: خریدهای تخم (specification، بخش «تخم») ---------------
# سال ۲۰۲۶ فرض شده است؛ در فایل مرجع فقط روز و ماه ذکر شده بود.
OBSERVED_EGG_PURCHASES = [
    {"date": "2026-02-17", "qty": 160_000},
    {"date": "2026-03-05", "qty": 70_000},
    {"date": "2026-05-15", "qty": 100_000},
    {"date": "2026-07-04", "qty": 160_000},
    {"date": "2026-07-19", "qty": 160_000},
]
OBSERVED_TOTAL = 650_000

SEED_FLAG = "observed_eggs_seeded_v1"
SALES_FLAG = "observed_sales_seeded_v1"
DEMO_FLAG = "demo_seeded_v1"

# ---- داده واقعی: سه فروش تاریخی (اصلاح ۱۰) ---------------------------
# این‌ها «Observed Historical Discounted Sales» هستند و شامل تخفیف بوده‌اند.
# به هیچ عنوان نباید مبنای قیمت پایه، قیمت مورد انتظار آینده یا reservation
# price قرار بگیرند. فقط برای تاریخچه، درآمد واقعی و دفتر نقدی استفاده شوند.
# cohort مبدأ مشخص نیست و حدس زده نمی‌شود → Cohort Unassigned.
OBSERVED_SALES = [
    # وزن این فروش ابتدا نامشخص بود و حدس زده نشد؛ بعداً توسط کاربر اعلام شد.
    {"date": "2026-03-16", "qty": 80_000, "weight_g": 6.0, "amount": 467_000_000},
    {"date": "2026-06-14", "qty": 80_000, "weight_g": 1.0, "amount": 700_000_000},
    {"date": "2026-08-01", "qty": 35_000, "weight_g": 2.0, "amount": 400_000_000},
]
SALE_NOTE = "Historical Sale — Cohort Unassigned · فروش تاریخی با تخفیف؛ مبنای قیمت‌گذاری نیست"


# اصلاحات وزنی که بعداً از سوی کاربر اعلام شده‌اند.
# روی پایگاه داده‌های موجود به‌صورت correction (نه overwrite) اعمال می‌شوند.
WEIGHT_BACKFILL = [
    {"date": "2026-03-16", "qty": 80_000, "weight_g": 6.0,
     "reason": "وزن واقعی فروش ۱۶ مارس توسط کاربر اعلام شد: ۶ گرم"},
]
WEIGHT_FLAG = "observed_sale_weights_v2"

# خریدهایی که کاربر اعلام کرده اشتباه ثبت شده‌اند.
# حذف خام نمی‌شوند؛ با ابطال (void) و دلیل، در تاریخچه باقی می‌مانند.
RETRACTED_EGG_PURCHASES = [
    {"date": "2026-04-03", "qty": 200_000,
     "reason": "کاربر اعلام کرد این خرید اشتباهاً وارد شده بود"},
]
RETRACT_FLAG = "retracted_egg_purchases_v1"


def retract_mistaken_purchases(db) -> dict:
    """ابطال خریدهای اشتباه، با حفظ کامل audit trail."""
    if db.meta_get(RETRACT_FLAG):
        return {"applied": False, "reason": "قبلاً اعمال شده"}
    done = []
    for rec in RETRACTED_EGG_PURCHASES:
        rows = db.q("SELECT * FROM transactions WHERE txn_type='egg_purchase' "
                    "AND txn_date=? AND quantity=? AND status='active'",
                    (rec["date"], rec["qty"]))
        for row in rows:
            db.void_txn(row["id"], rec["reason"])
            done.append({"txn_id": row["id"], "cohort_id": row["cohort_id"],
                         "date": rec["date"], "quantity": rec["qty"]})
            # آفر متناظر هم باید رد شود، نه پذیرفته‌شده
            db.ex("UPDATE egg_offers SET status='rejected', decision_note=? "
                  "WHERE linked_txn_id=?", (rec["reason"], row["id"]))
    db.meta_set(RETRACT_FLAG, "1")
    return {"applied": True, "retracted": done}


def backfill_observed_weights(db) -> dict:
    """
    ثبت وزن‌هایی که بعداً معلوم شده‌اند، با حفظ audit trail.

    رکورد قبلی (با وزن خالی) حذف نمی‌شود؛ یک نسخه اصلاح‌شده ثبت می‌شود.
    """
    if db.meta_get(WEIGHT_FLAG):
        return {"applied": False, "reason": "قبلاً اعمال شده"}
    done = []
    for rec in WEIGHT_BACKFILL:
        rows = db.q("SELECT * FROM transactions WHERE txn_type='sale' "
                    "AND txn_date=? AND quantity=? AND status='active' "
                    "AND weight_g IS NULL", (rec["date"], rec["qty"]))
        for row in rows:
            payload = row["payload"] if isinstance(row["payload"], dict) \
                else json.loads(row["payload"] or "{}")
            payload.update({"weight_known": True, "weight_source": "user_reported"})
            new_id = db.correct_txn(row["id"], reason=rec["reason"],
                                    weight_g=rec["weight_g"], payload=payload)
            done.append({"old_id": row["id"], "new_id": new_id,
                         "weight_g": rec["weight_g"]})
    db.meta_set(WEIGHT_FLAG, "1")
    return {"applied": True, "corrections": done}


def seed_observed_sales(db, force: bool = False) -> dict:
    """سه فروش واقعی گذشته. وزن فروش ۱۶ مارس نامشخص است و حدس زده نمی‌شود."""
    if db.meta_get(SALES_FLAG) and not force:
        return {"seeded": False, "reason": "قبلاً بارگذاری شده"}
    added = []
    for rec in OBSERVED_SALES:
        if db.one("SELECT id FROM transactions WHERE txn_type='sale' AND txn_date=? "
                  "AND quantity=? AND amount=?",
                  (rec["date"], rec["qty"], rec["amount"])):
            continue
        realised = rec["amount"] / rec["qty"]
        tid = db.add_txn(
            "sale", rec["date"], cohort_id=None, quantity=rec["qty"],
            weight_g=rec["weight_g"],           # None = نامشخص، عمداً خالی
            unit_price=realised, amount=rec["amount"],
            counterparty="نامشخص", data_source="actual", note=SALE_NOTE,
            payload={"observed": True, "cohort_unassigned": True,
                     "discounted": True, "exclude_from_price_model": True,
                     "weight_known": rec["weight_g"] is not None,
                     "realised_price_per_fish": round(realised, 1),
                     "reconciliation": "needs_more_info"})
        added.append(tid)
    db.meta_set(SALES_FLAG, "1")
    return {"seeded": True, "txns": added,
            "total_fish": sum(r["qty"] for r in OBSERVED_SALES),
            "total_value": sum(r["amount"] for r in OBSERVED_SALES)}


def cohort_id_for(dt: str, idx: int) -> str:
    return f"C{idx:02d}-{dt.replace('-', '')}"


def seed_observed(db, A, force: bool = False) -> dict:
    if db.meta_get(SEED_FLAG) and not force:
        return {"seeded": False, "reason": "قبلاً بارگذاری شده"}
    added = []
    for i, rec in enumerate(OBSERVED_EGG_PURCHASES, start=1):
        # قیمت تخم با تاریخ اعتبارِ همان روز خرید خوانده می‌شود؛ تغییر بعدی
        # قیمت در فرضیات، خریدهای گذشته را عوض نمی‌کند (اصلاح ۶).
        price = float(A.get_at("egg.base_price", rec["date"]))
        cid = cohort_id_for(rec["date"], i)
        if db.one("SELECT id FROM transactions WHERE cohort_id=? AND txn_type='egg_purchase'",
                  (cid,)):
            continue
        txn_id = db.add_txn(
            "egg_purchase", rec["date"], cohort_id=cid, quantity=rec["qty"],
            unit_price=price, amount=rec["qty"] * price,
            counterparty="نامشخص", data_source="actual",
            note="داده واقعی تثبیت‌شده (Observed) — قیمت پایه ۶٬۰۰۰ تومان اعمال شده",
            payload={"observed": True, "price_source": "egg.base_price"})
        db.add_egg_offer(offer_date=rec["date"], supplier="نامشخص",
                         quantity=rec["qty"], price_per_egg=price,
                         status="accepted", accepted_quantity=rec["qty"],
                         decision_date=rec["date"], linked_txn_id=txn_id,
                         decision_note="خرید واقعی انجام‌شده (ثبت گذشته‌نگر)")
        added.append(cid)
    db.meta_set(SEED_FLAG, "1")
    return {"seeded": True, "cohorts": added, "total_eggs": OBSERVED_TOTAL}


# --------------------------------------------------------------- demo data
DEMO_NOTE = "[DEMO] داده نمونه برای آزمایش رابط — واقعی نیست"


def seed_demo(db, A, force: bool = False) -> dict:
    """داده نمونه برای دیدن قابلیت‌ها؛ کاملاً قابل حذف."""
    if db.meta_get(DEMO_FLAG) and not force:
        return {"seeded": False, "reason": "قبلاً بارگذاری شده"}
    cohorts = [r["cohort_id"] for r in db.q(
        "SELECT DISTINCT cohort_id FROM transactions WHERE txn_type='egg_purchase' "
        "ORDER BY txn_date")]
    if not cohorts:
        return {"seeded": False, "reason": "ابتدا داده واقعی تخم را بارگذاری کنید"}

    # خرید خوراک
    for name, kg, price, dt in [("FP-1", 4000, 310000, "2026-06-01"),
                                ("FP-3", 6000, 305000, "2026-06-20"),
                                ("GR-1", 12000, 199000, "2026-07-10")]:
        db.add_txn("feed_purchase", dt, quantity=kg, unit_price=price,
                   amount=kg * price, counterparty="تأمین‌کننده خوراک",
                   note=DEMO_NOTE, payload={"feed_name": name})

    # وزن‌کشی واقعی روی cohort سوم
    if len(cohorts) >= 3:
        db.add_txn("weight_sample", "2026-08-01", cohort_id=cohorts[2],
                   weight_g=9.2, quantity=120, note=DEMO_NOTE,
                   payload={"sd_g": 1.4, "n_sampled": 120})
    # تلفات واقعی
    db.add_txn("mortality", "2026-07-25", cohort_id=cohorts[0], quantity=3500,
               note=DEMO_NOTE)
    # انتقال به استخر
    db.add_txn("transfer", "2026-07-01", cohort_id=cohorts[0], pond_id=None,
               to_pond_id="P01", quantity=40000, note=DEMO_NOTE)
    db.add_txn("transfer", "2026-07-01", cohort_id=cohorts[0], pond_id=None,
               to_pond_id="P02", quantity=40000, note=DEMO_NOTE)
    # فروش جزئی
    db.add_txn("sale", "2026-08-05", cohort_id=cohorts[0], pond_id="P01",
               quantity=25000, weight_g=15.0, unit_price=22200,
               amount=25000 * 22200, counterparty="خریدار نمونه",
               note=DEMO_NOTE)
    # هزینه جانبی واقعی (اصلاح ۴)
    db.add_txn("operating_cost", "2026-07-20", amount=18_000_000,
               category="نگهداری و تعمیرات", note=DEMO_NOTE + " — تعمیر پمپ")
    # قرائت آب
    db.add_txn("water_reading", "2026-08-10", pond_id="P01", note=DEMO_NOTE,
               payload={"temperature_c": 11.2, "do_in": 8.0, "do_out": 6.1,
                        "flow_l_s": 15.0})
    db.meta_set(DEMO_FLAG, "1")
    return {"seeded": True}


def clear_demo(db) -> dict:
    n = db.ex("UPDATE transactions SET status='void' WHERE note LIKE '[DEMO]%' "
              "AND status='active'").rowcount
    db.meta_set(DEMO_FLAG, "")
    return {"voided": n}
