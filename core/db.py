"""
db.py — لایه persistence (SQLite)
==================================
اصول:
  * transactions فقط append می‌شوند. هیچ رکوردی حذف یا overwrite نمی‌شود.
  * اصلاح یک تراکنش = درج رکورد جدید با corrects_id + علامت‌گذاری رکورد قبلی
    به‌عنوان 'corrected'  (audit trail کامل).
  * وضعیت جاری مزرعه هرگز ذخیره نمی‌شود؛ همیشه از روی تاریخچه بازپخش می‌شود.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone

DEFAULT_DB = os.getenv(
    "DATABASE_PATH",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "farm.db"),
)
# Railway persistent volumes commonly mount at /data. Ensure the selected DB directory exists.
os.makedirs(os.path.dirname(os.path.abspath(DEFAULT_DB)), exist_ok=True)

# انواع تراکنش مجاز
TXN_TYPES = [
    "egg_purchase",       # خرید تخم (ایجاد cohort)
    "feed_purchase",      # خرید خوراک
    "feed_consumption",   # مصرف خوراک
    "mortality",          # تلفات ثبت‌شده (actual)
    "count_observation",  # شمارش واقعی ماهی (anchor مطلق)
    "weight_sample",      # نمونه‌برداری وزن (anchor رشد)
    "transfer",           # انتقال بین استخرها
    "sale",               # فروش جزئی یا کامل
    "payment",            # پرداخت نقدی
    "receipt",            # دریافت نقدی
    "operating_cost",     # هزینه عملیاتی موردی
    "water_reading",      # قرائت دما / DO / دبی
]

# واحد پیش‌فرض فیلد quantity برای هر نوع تراکنش (اصلاح ۳)
UNIT_OF = {
    "egg_purchase": "عدد", "feed_purchase": "kg", "feed_consumption": "kg",
    "mortality": "قطعه", "count_observation": "قطعه", "weight_sample": "قطعه نمونه",
    "transfer": "قطعه", "sale": "قطعه", "payment": "تومان", "receipt": "تومان",
    "operating_cost": "مورد", "water_reading": "قرائت",
}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_type      TEXT    NOT NULL,
    txn_date      TEXT    NOT NULL,          -- تاریخ واقعی رویداد YYYY-MM-DD
    cohort_id     TEXT,
    pond_id       TEXT,
    to_pond_id    TEXT,
    quantity      REAL,                      -- تعداد قطعه یا kg
    unit          TEXT,                      -- واحد مقدار: قطعه | kg | مورد
    weight_g      REAL,                      -- وزن متوسط مرتبط (NULL = نامشخص)
    unit_price    REAL,
    amount        REAL,                      -- مبلغ کل (تومان)
    category      TEXT,                      -- دسته هزینه (برای operating_cost)
    counterparty  TEXT,                      -- تأمین‌کننده / خریدار
    data_source   TEXT NOT NULL DEFAULT 'actual',  -- actual | estimated
    status        TEXT NOT NULL DEFAULT 'active',  -- active | corrected | void
    corrects_id   INTEGER REFERENCES transactions(id),
    correction_reason TEXT,                  -- دلیل اصلاح (audit trail)
    note          TEXT,
    payload       TEXT,                      -- JSON برای فیلدهای اضافی
    created_at    TEXT NOT NULL,
    created_by    TEXT DEFAULT 'user'
);
CREATE INDEX IF NOT EXISTS ix_txn_date   ON transactions(txn_date);
CREATE INDEX IF NOT EXISTS ix_txn_cohort ON transactions(cohort_id);
CREATE INDEX IF NOT EXISTS ix_txn_type   ON transactions(txn_type);

-- Fix 1: تدارک تخم مبتنی بر Offer؛ آفر رد شده هم نگهداری می‌شود
CREATE TABLE IF NOT EXISTS egg_offers (
    offer_id        TEXT PRIMARY KEY,
    offer_date      TEXT NOT NULL,
    supplier        TEXT,
    quantity        REAL NOT NULL,
    price_per_egg   REAL NOT NULL,
    expiry_date     TEXT,
    quality_score   REAL,
    payment_terms_days INTEGER DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|accepted|partial|rejected|expired
    accepted_quantity REAL DEFAULT 0,
    decision_date   TEXT,
    decision_note   TEXT,
    linked_txn_id   INTEGER REFERENCES transactions(id),
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sale_offers (
    offer_id      TEXT PRIMARY KEY,
    offer_date    TEXT NOT NULL,
    buyer         TEXT,
    cohort_id     TEXT,
    quantity      REAL NOT NULL,
    weight_g      REAL,
    price_per_fish REAL NOT NULL,
    delivery_date TEXT,
    payment_terms_days INTEGER DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending',
    accepted_quantity REAL DEFAULT 0,
    decision_date TEXT,
    decision_note TEXT,
    linked_txn_id INTEGER REFERENCES transactions(id),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ponds (
    pond_id    TEXT PRIMARY KEY,
    label      TEXT,
    role       TEXT NOT NULL DEFAULT 'operational',  -- operational | reserve
    volume_m3  REAL,
    sort_order INTEGER,
    note       TEXT
);

-- قابلیت «بهینه‌سازی چیدمان استخر» — batch رکورد فقط append می‌شود، هرگز
-- overwrite نمی‌شود؛ خودِ جابه‌جایی‌ها همچنان تراکنش transfer معمولی‌اند.
-- snapshot_before برای Restore لازم است (بازگشت به چیدمان پیش از Apply).
CREATE TABLE IF NOT EXISTS pond_alloc_batches (
    batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,        -- 'optimize' | 'restore'
    created_at      TEXT NOT NULL,
    as_of_date      TEXT NOT NULL,
    snapshot_before TEXT NOT NULL,        -- JSON: {cohort_id: {pond_id: qty}}
    moves_json      TEXT NOT NULL,        -- JSON: لیست جابه‌جایی‌های اعمال‌شده
    txn_ids         TEXT NOT NULL,        -- JSON: شناسه تراکنش‌های transfer ساخته‌شده
    restored_from   INTEGER REFERENCES pond_alloc_batches(batch_id),
    status          TEXT NOT NULL DEFAULT 'applied',  -- applied | restored
    note            TEXT
);

CREATE TABLE IF NOT EXISTS assumption_overrides (
    key           TEXT PRIMARY KEY,
    value_json    TEXT NOT NULL,
    previous_json TEXT,
    changed_at    TEXT NOT NULL,
    changed_by    TEXT DEFAULT 'user'
);

-- تاریخچه فرضیات مالی با effective_from (اصلاح ۶)
-- تغییر قیمت هرگز retroactive نیست: هر مقدار از تاریخ اعتبار خودش به بعد
-- اعمال می‌شود و مقادیر قبلی برای دوره خودشان باقی می‌مانند.
CREATE TABLE IF NOT EXISTS assumption_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    key            TEXT NOT NULL,
    value_json     TEXT NOT NULL,
    effective_from TEXT NOT NULL,            -- YYYY-MM-DD
    note           TEXT,
    changed_at     TEXT NOT NULL,
    changed_by     TEXT DEFAULT 'user'
);
CREATE INDEX IF NOT EXISTS ix_ah_key ON assumption_history(key, effective_from);

CREATE TABLE IF NOT EXISTS fx_daily (
    date_g      TEXT PRIMARY KEY,   -- YYYY-MM-DD
    date_j      TEXT,
    close_toman REAL NOT NULL,
    source      TEXT,
    fetched_at  TEXT,
    verified    INTEGER NOT NULL DEFAULT 0,
    stale       INTEGER NOT NULL DEFAULT 0
);

-- تخصیص یک فروش به چند cohort.
-- یک فروش می‌تواند از ترکیب چند cohort باشد؛ رابطه یک‌به‌یک کافی نیست.
-- قید: مجموع تخصیص‌ها = تعداد کل فروش.
CREATE TABLE IF NOT EXISTS sale_allocations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id     INTEGER NOT NULL REFERENCES transactions(id),
    cohort_id  TEXT NOT NULL,
    quantity   REAL NOT NULL,
    basis      TEXT NOT NULL DEFAULT 'confirmed',  -- suggested | confirmed
    confidence REAL,
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sa_txn ON sale_allocations(txn_id);

-- برنامه‌های ذخیره‌شده (مرحله ۲): Original Plan در برابر Re-Optimised
CREATE TABLE IF NOT EXISTS plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,              -- original | snapshot
    variant    TEXT NOT NULL,              -- max_profit | balanced | conservative
    as_of      TEXT NOT NULL,
    summary    TEXT NOT NULL,              -- JSON
    monthly    TEXT NOT NULL,              -- JSON
    lots       TEXT NOT NULL,              -- JSON
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_plans_kind ON plans(kind, variant);

CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: str = DEFAULT_DB):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    # ---------------------------------------------------------- migration
    def _migrate(self):
        """افزودن ستون‌های جدید به پایگاه داده‌های قدیمی، بدون از دست رفتن داده."""
        have = {r["name"] for r in self.q("PRAGMA table_info(transactions)")}
        for col, ddl in (("unit", "TEXT"), ("category", "TEXT"),
                         ("correction_reason", "TEXT")):
            if col not in have:
                self.conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {ddl}")
        fx_have = {r["name"] for r in self.q("PRAGMA table_info(fx_daily)")}
        for col, ddl in (("fetched_at", "TEXT"),
                         ("verified", "INTEGER NOT NULL DEFAULT 0"),
                         ("stale", "INTEGER NOT NULL DEFAULT 0")):
            if col not in fx_have:
                self.conn.execute(f"ALTER TABLE fx_daily ADD COLUMN {col} {ddl}")

    # ------------------------------------------------------------ basics
    def q(self, sql: str, args=()) -> list:
        return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args=()):
        r = self.conn.execute(sql, args).fetchone()
        return dict(r) if r else None

    def ex(self, sql: str, args=()):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    def meta_get(self, key, default=None):
        r = self.one("SELECT value FROM app_meta WHERE key=?", (key,))
        return r["value"] if r else default

    def meta_set(self, key, value):
        self.ex("INSERT INTO app_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))

    # ------------------------------------------------------ transactions
    def add_txn(self, txn_type: str, txn_date: str, **kw) -> int:
        if txn_type not in TXN_TYPES:
            raise ValueError(f"نوع تراکنش نامعتبر: {txn_type}")
        payload = kw.pop("payload", None)
        row = {
            "txn_type": txn_type,
            "txn_date": txn_date,
            "cohort_id": kw.get("cohort_id"),
            "pond_id": kw.get("pond_id"),
            "to_pond_id": kw.get("to_pond_id"),
            "quantity": kw.get("quantity"),
            "unit": kw.get("unit") or UNIT_OF.get(txn_type),
            "weight_g": kw.get("weight_g"),
            "unit_price": kw.get("unit_price"),
            "amount": kw.get("amount"),
            "category": kw.get("category"),
            "counterparty": kw.get("counterparty"),
            "data_source": kw.get("data_source", "actual"),
            "status": kw.get("status", "active"),
            "corrects_id": kw.get("corrects_id"),
            "correction_reason": kw.get("correction_reason"),
            "note": kw.get("note"),
            "payload": json.dumps(payload, ensure_ascii=False) if payload else None,
            "created_at": now_iso(),
            "created_by": kw.get("created_by", "user"),
        }
        cols = ",".join(row.keys())
        marks = ",".join("?" * len(row))
        cur = self.ex(f"INSERT INTO transactions({cols}) VALUES({marks})",
                      tuple(row.values()))
        return cur.lastrowid

    def correct_txn(self, old_id: int, reason: str | None = None, **kw) -> int:
        """اصلاح تراکنش قدیمی: درج نسخه جدید + علامت‌گذاری قدیمی (بدون حذف).

        رکورد قدیمی دست‌نخورده در پایگاه داده می‌ماند و همان «مقدار اولیه» است؛
        رکورد جدید مقدار اصلاح‌شده، تاریخ اصلاح (created_at) و دلیل را دارد.
        """
        old = self.one("SELECT * FROM transactions WHERE id=?", (old_id,))
        if not old:
            raise ValueError("تراکنش یافت نشد")
        if old["status"] != "active":
            raise ValueError("این تراکنش قبلاً اصلاح یا باطل شده است")
        merged = {k: old[k] for k in old.keys()
                  if k not in ("id", "created_at", "status", "corrects_id")}
        if merged.get("payload"):
            try:
                merged["payload"] = json.loads(merged["payload"])
            except Exception:
                merged["payload"] = None
        merged.update({k: v for k, v in kw.items() if v is not None})
        # اجازه صریح برای پاک‌کردن یک مقدار (مثلاً وزن نامشخص)
        for k, v in kw.items():
            if v is None and k in ("weight_g", "unit_price", "amount", "pond_id",
                                   "to_pond_id", "cohort_id", "counterparty"):
                merged[k] = None
        txn_type = merged.pop("txn_type")
        txn_date = merged.pop("txn_date")
        merged.pop("created_by", None)
        merged["correction_reason"] = reason or merged.get("correction_reason")
        new_id = self.add_txn(txn_type, txn_date, corrects_id=old_id, **merged)
        self.ex("UPDATE transactions SET status='corrected' WHERE id=?", (old_id,))
        return new_id

    def txn_chain(self, txn_id: int) -> list:
        """زنجیره کامل audit trail یک تراکنش: نسخه اصلی تا آخرین اصلاح."""
        row = self.one("SELECT * FROM transactions WHERE id=?", (txn_id,))
        if not row:
            return []
        # به عقب تا نسخه اصلی
        first = row
        seen = set()
        while first.get("corrects_id") and first["corrects_id"] not in seen:
            seen.add(first["corrects_id"])
            prev = self.one("SELECT * FROM transactions WHERE id=?", (first["corrects_id"],))
            if not prev:
                break
            first = prev
        chain = [first]
        while True:
            nxt = self.one("SELECT * FROM transactions WHERE corrects_id=?", (chain[-1]["id"],))
            if not nxt or nxt["id"] in {c["id"] for c in chain}:
                break
            chain.append(nxt)
        return chain

    def void_txn(self, txn_id: int, note: str = "") -> None:
        self.ex("UPDATE transactions SET status='void', note=COALESCE(note,'')||? "
                "WHERE id=?", (f" | باطل شد: {note}", txn_id))

    def active_txns(self, cohort_id: str | None = None) -> list:
        sql = ("SELECT * FROM transactions WHERE status='active'")
        args: tuple = ()
        if cohort_id:
            sql += " AND cohort_id=?"
            args = (cohort_id,)
        sql += " ORDER BY txn_date ASC, id ASC"
        rows = self.q(sql, args)
        for r in rows:
            r["payload"] = json.loads(r["payload"]) if r["payload"] else {}
        return rows

    # ------------------------------------------------------------- ponds
    def ensure_ponds(self, total: int, operational: int, volume: float):
        existing = {p["pond_id"] for p in self.q("SELECT pond_id FROM ponds")}
        for i in range(1, total + 1):
            pid = f"P{i:02d}"
            if pid in existing:
                continue
            role = "operational" if i <= operational else "reserve"
            self.ex("INSERT INTO ponds(pond_id,label,role,volume_m3,sort_order) "
                    "VALUES(?,?,?,?,?)", (pid, f"استخر {i}", role, volume, i))

    def ponds(self) -> list:
        return self.q("SELECT * FROM ponds ORDER BY sort_order")

    # ------------------------------------------ pond allocation batches
    def save_pond_batch(self, kind: str, as_of_date: str, snapshot_before: dict,
                        moves: list, txn_ids: list, restored_from: int | None = None,
                        note: str = "") -> int:
        cur = self.ex(
            "INSERT INTO pond_alloc_batches"
            "(kind, created_at, as_of_date, snapshot_before, moves_json, txn_ids,"
            " restored_from, status, note) VALUES(?,?,?,?,?,?,?,?,?)",
            (kind, datetime.now(timezone.utc).isoformat(), as_of_date,
             json.dumps(snapshot_before, ensure_ascii=False),
             json.dumps(moves, ensure_ascii=False),
             json.dumps(txn_ids), restored_from,
             "applied", note))
        return cur.lastrowid

    def latest_pond_batch(self, kind: str = "optimize", status: str = "applied"):
        row = self.one(
            "SELECT * FROM pond_alloc_batches WHERE kind=? AND status=? "
            "ORDER BY batch_id DESC LIMIT 1", (kind, status))
        if not row:
            return None
        row = dict(row)
        row["snapshot_before"] = json.loads(row["snapshot_before"])
        row["moves_json"] = json.loads(row["moves_json"])
        row["txn_ids"] = json.loads(row["txn_ids"])
        return row

    def mark_pond_batch_restored(self, batch_id: int):
        self.ex("UPDATE pond_alloc_batches SET status='restored' WHERE batch_id=?",
                (batch_id,))

    # --------------------------------------------------------- overrides
    def get_overrides(self) -> dict:
        out = {}
        for r in self.q("SELECT * FROM assumption_overrides"):
            out[r["key"]] = {"value": json.loads(r["value_json"]),
                             "changed_at": r["changed_at"],
                             "changed_by": r["changed_by"]}
        return out

    def set_override(self, key: str, value, changed_by: str = "user"):
        prev = self.one("SELECT value_json FROM assumption_overrides WHERE key=?", (key,))
        self.ex("INSERT INTO assumption_overrides(key,value_json,previous_json,changed_at,changed_by) "
                "VALUES(?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
                "previous_json=assumption_overrides.value_json, value_json=excluded.value_json, "
                "changed_at=excluded.changed_at, changed_by=excluded.changed_by",
                (key, json.dumps(value, ensure_ascii=False),
                 prev["value_json"] if prev else None, now_iso(), changed_by))

    def clear_override(self, key: str):
        self.ex("DELETE FROM assumption_overrides WHERE key=?", (key,))

    def clear_all_overrides(self):
        self.ex("DELETE FROM assumption_overrides")

    # ------------------------------------ effective-dated assumption history
    def add_assumption_history(self, key: str, value, effective_from: str,
                               note: str = "", changed_by: str = "user") -> int:
        cur = self.ex(
            "INSERT INTO assumption_history(key,value_json,effective_from,note,"
            "changed_at,changed_by) VALUES(?,?,?,?,?,?)",
            (key, json.dumps(value, ensure_ascii=False), effective_from[:10],
             note, now_iso(), changed_by))
        return cur.lastrowid

    def assumption_history(self, key: str | None = None) -> list:
        sql = "SELECT * FROM assumption_history"
        args: tuple = ()
        if key:
            sql += " WHERE key=?"
            args = (key,)
        sql += " ORDER BY key, effective_from, id"
        rows = self.q(sql, args)
        for r in rows:
            r["value"] = json.loads(r["value_json"])
        return rows

    def delete_assumption_history(self, hist_id: int):
        self.ex("DELETE FROM assumption_history WHERE id=?", (hist_id,))

    # ------------------------------------------ تخصیص چند-cohort فروش
    def set_sale_allocations(self, txn_id: int, rows: list, basis: str = "confirmed"):
        """
        جایگزینی کامل تخصیص‌های یک فروش.

        `rows`: [{"cohort_id":..., "quantity":..., "confidence":..., "note":...}]
        تخصیص‌های «suggested» فقط پیشنهادند و موجودی را تغییر نمی‌دهند؛
        فقط `confirmed` روی Live Farm State اثر می‌گذارد.
        """
        self.ex("DELETE FROM sale_allocations WHERE txn_id=? AND basis=?",
                (txn_id, basis))
        for r in rows:
            q = float(r.get("quantity") or 0)
            if q <= 0:
                continue
            self.ex("INSERT INTO sale_allocations"
                    "(txn_id,cohort_id,quantity,basis,confidence,note,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (txn_id, r["cohort_id"], q, basis,
                     r.get("confidence"), r.get("note"), now_iso()))

    def sale_allocations(self, txn_id: int | None = None, basis: str | None = None) -> list:
        sql = "SELECT * FROM sale_allocations WHERE 1=1"
        args = []
        if txn_id is not None:
            sql += " AND txn_id=?"
            args.append(txn_id)
        if basis:
            sql += " AND basis=?"
            args.append(basis)
        return self.q(sql + " ORDER BY txn_id, id", tuple(args))

    def clear_sale_allocations(self, txn_id: int, basis: str | None = None):
        if basis:
            self.ex("DELETE FROM sale_allocations WHERE txn_id=? AND basis=?",
                    (txn_id, basis))
        else:
            self.ex("DELETE FROM sale_allocations WHERE txn_id=?", (txn_id,))

    # ----------------------------------------------- plans (مرحله ۲)
    def save_plan(self, kind: str, variant: str, as_of: str, summary: dict,
                  monthly: list, lots: list, note: str = "") -> int:
        cur = self.ex(
            "INSERT INTO plans(kind,variant,as_of,summary,monthly,lots,note,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (kind, variant, as_of,
             json.dumps(summary, ensure_ascii=False, default=str),
             json.dumps(monthly, ensure_ascii=False, default=str),
             json.dumps(lots, ensure_ascii=False, default=str),
             note, now_iso()))
        return cur.lastrowid

    def get_plan(self, kind: str, variant: str | None = None) -> dict | None:
        sql = "SELECT * FROM plans WHERE kind=?"
        args = [kind]
        if variant:
            sql += " AND variant=?"
            args.append(variant)
        sql += " ORDER BY id ASC LIMIT 1" if kind == "original" else " ORDER BY id DESC LIMIT 1"
        r = self.one(sql, tuple(args))
        if not r:
            return None
        for k in ("summary", "monthly", "lots"):
            r[k] = json.loads(r[k])
        return r

    def list_plans(self) -> list:
        return self.q("SELECT id,kind,variant,as_of,note,created_at FROM plans "
                      "ORDER BY id DESC LIMIT 50")

    def delete_plan(self, plan_id: int):
        self.ex("DELETE FROM plans WHERE id=?", (plan_id,))

    # ------------------------------------------------------------ offers
    def add_egg_offer(self, **kw) -> str:
        oid = kw.get("offer_id") or f"EO-{datetime.now().strftime('%Y%m%d%H%M%S%f')[:18]}"
        self.ex("INSERT INTO egg_offers(offer_id,offer_date,supplier,quantity,price_per_egg,"
                "expiry_date,quality_score,payment_terms_days,status,accepted_quantity,"
                "decision_date,decision_note,linked_txn_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, kw["offer_date"], kw.get("supplier"), kw["quantity"],
                 kw["price_per_egg"], kw.get("expiry_date"), kw.get("quality_score"),
                 kw.get("payment_terms_days", 0), kw.get("status", "pending"),
                 kw.get("accepted_quantity", 0), kw.get("decision_date"),
                 kw.get("decision_note"), kw.get("linked_txn_id"), now_iso()))
        return oid

    def egg_offers(self) -> list:
        return self.q("SELECT * FROM egg_offers ORDER BY offer_date DESC")

    def sale_offers(self) -> list:
        return self.q("SELECT * FROM sale_offers ORDER BY offer_date DESC")

    def add_sale_offer(self, **kw) -> str:
        oid = kw.get("offer_id") or f"SO-{datetime.now().strftime('%Y%m%d%H%M%S%f')[:18]}"
        self.ex("INSERT INTO sale_offers(offer_id,offer_date,buyer,cohort_id,quantity,weight_g,"
                "price_per_fish,delivery_date,payment_terms_days,status,accepted_quantity,"
                "decision_date,decision_note,linked_txn_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (oid, kw["offer_date"], kw.get("buyer"), kw.get("cohort_id"),
                 kw["quantity"], kw.get("weight_g"), kw["price_per_fish"],
                 kw.get("delivery_date"), kw.get("payment_terms_days", 0),
                 kw.get("status", "pending"), kw.get("accepted_quantity", 0),
                 kw.get("decision_date"), kw.get("decision_note"),
                 kw.get("linked_txn_id"), now_iso()))
        return oid

    # ---------------------------------------------------------------- fx
    def fx_replace_all(self, rows: list, source: str):
        self.ex("DELETE FROM fx_daily")
        self.conn.executemany(
            "INSERT OR REPLACE INTO fx_daily(date_g,date_j,close_toman,source,fetched_at,verified,stale) VALUES(?,?,?,?,?,?,?)",
            [(r["date_g"], r.get("date_j"), float(r["close_toman"]), source,
              r.get("fetched_at"), int(bool(r.get("verified", source != "missing"))),
              int(bool(r.get("stale", False)))) for r in rows])
        self.conn.commit()

    def fx_upsert(self, row: dict, source: str, priority: int = 0):
        existing = self.one("SELECT * FROM fx_daily WHERE date_g=?", (row["date_g"],))
        priorities = {"navasan-api": 30, "tgju-api": 30, "api": 30, "TGJU-excel": 20, "file": 20, "manual": 10}
        oldp = priorities.get((existing or {}).get("source"), 0)
        newp = priority or priorities.get(source, 0)
        if existing and oldp > newp:
            return False
        self.ex("""INSERT OR REPLACE INTO fx_daily
            (date_g,date_j,close_toman,source,fetched_at,verified,stale) VALUES(?,?,?,?,?,?,?)""",
            (row["date_g"], row.get("date_j"), float(row["close_toman"]), source,
             row.get("fetched_at") or now_iso(), int(bool(row.get("verified", False))),
             int(bool(row.get("stale", False)))))
        return True

    def fx_series(self) -> list:
        return self.q("SELECT * FROM fx_daily ORDER BY date_g")

    # -------------------------------------------------- backup / export (۹)
    EXPORT_TABLES = ["transactions", "ponds", "egg_offers", "sale_offers",
                     "assumption_overrides", "assumption_history", "plans",
                     "sale_allocations",
                     "fx_daily", "app_meta"]

    def export_dict(self, include_fx: bool = True) -> dict:
        """خروجی کامل JSON از همه جداول (پشتیبان قابل خواندن انسان)."""
        out = {"exported_at": now_iso(), "db_path": os.path.basename(self.path),
               "tables": {}}
        for t in self.EXPORT_TABLES:
            if t == "fx_daily" and not include_fx:
                continue
            out["tables"][t] = self.q(f"SELECT * FROM {t}")
        out["counts"] = {t: len(v) for t, v in out["tables"].items()}
        return out

    def export_csv(self, table: str) -> str:
        """یک جدول به‌صورت CSV (با BOM تا اکسل فارسی درست باز کند)."""
        import csv
        import io
        if table not in self.EXPORT_TABLES:
            raise ValueError(f"جدول نامعتبر برای export: {table}")
        rows = self.q(f"SELECT * FROM {table}")
        cols = [c["name"] for c in self.q(f"PRAGMA table_info({table})")]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
        return "\ufeff" + buf.getvalue()

    def backup_bytes(self) -> bytes:
        """پشتیبان کامل SQLite با استفاده از backup API (سازگار با WAL)."""
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            dest = sqlite3.connect(tmp)
            with dest:
                self.conn.backup(dest)
            dest.close()
            with open(tmp, "rb") as f:
                return f.read()
        finally:
            for p in (tmp, tmp + "-wal", tmp + "-shm"):
                if os.path.exists(p):
                    os.remove(p)

    def close(self):
        self.conn.close()
