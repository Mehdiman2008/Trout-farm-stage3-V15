"""
state.py — موتور Live Farm State
==================================
وضعیت جاری مزرعه هرگز ذخیره نمی‌شود؛ همیشه از بازپخش (replay) تاریخچه
تراکنش‌ها ساخته می‌شود. بنابراین:
  * هیچ داده واقعی با تخمین overwrite نمی‌شود،
  * تغییر فرضیات فقط تخمین‌ها را عوض می‌کند نه تاریخ را،
  * محاسبات کاملاً reproducible هستند.

قواعد تفکیک Actual / Estimated:
  تعداد زنده
    - anchor واقعی = خرید تخم، یا آخرین count_observation
    - اگر cohort رکورد تلفات واقعی داشته باشد، تا تاریخِ آخرین رکورد تلفات،
      «گزارش واقعی» جایگزین منحنی تلفات مدل می‌شود (جلوگیری از double-count)؛
      بعد از آن تاریخ، مدل دوباره ادامه می‌دهد.
  وزن متوسط
    - anchor واقعی = آخرین weight_sample
    - منحنی رشد از همان نقطه با یک offset زمانی ادامه می‌یابد،
      یعنی forecast بعدی از آخرین داده واقعی شروع می‌شود.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

TROUGH = "TROUGH"          # محل نگهداری قبل از تخصیص استخر (۸۰ روز اول)
UNASSIGNED = "UNASSIGNED"  # ماهی بالای ۱ گرم که هنوز استخر واقعی نگرفته


def d(x) -> date:
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def _age(purchase: date, on: date) -> int:
    return (on - purchase).days


class CohortState:
    __slots__ = ("cohort_id", "purchase_date", "supplier", "egg_count", "egg_price",
                 "alive", "count_basis", "count_anchor_date",
                 "mean_weight", "weight_basis", "weight_anchor_date",
                 "growth_offset_days", "cv_shift", "alloc",
                 "recorded_mortality", "sold_count", "sold_revenue",
                 "feed_kg_actual", "feed_cost_actual", "note", "closed")

    def __init__(self, cohort_id, purchase_date, supplier, egg_count, egg_price, note=""):
        self.cohort_id = cohort_id
        self.purchase_date = purchase_date
        self.supplier = supplier
        self.egg_count = float(egg_count)
        self.egg_price = float(egg_price or 0)
        self.alive = float(egg_count)
        self.count_basis = "actual"           # actual در لحظه خرید
        self.count_anchor_date = purchase_date
        self.mean_weight = 0.0
        self.weight_basis = "estimated"
        self.weight_anchor_date = None
        self.growth_offset_days = 0.0
        self.cv_shift = 0.0
        self.alloc = {TROUGH: float(egg_count)}
        self.recorded_mortality = 0.0
        self.sold_count = 0.0
        self.sold_revenue = 0.0
        self.feed_kg_actual = 0.0
        self.feed_cost_actual = 0.0
        self.note = note or ""
        self.closed = False

    # -------------------------------------------------------------- clone
    def copy(self) -> "CohortState":
        """کپی کامل و مستقل — برای سناریوهای فرضی (hypothetical state)."""
        c = CohortState.__new__(CohortState)
        for k in CohortState.__slots__:
            v = getattr(self, k)
            setattr(c, k, dict(v) if isinstance(v, dict) else v)
        return c

    # -------------------------------------------------------- allocation
    def scale_alloc(self, factor: float):
        if factor >= 0.999999999 and factor <= 1.000000001:
            return
        for k in list(self.alloc):
            self.alloc[k] *= factor

    def remove(self, n: float, pond: str | None = None):
        """حذف n قطعه؛ اگر استخر مشخص باشد از همان، وگرنه نسبتی."""
        n = min(n, sum(self.alloc.values()))
        if n <= 0:
            return 0.0
        if pond and pond in self.alloc and self.alloc[pond] > 0:
            take = min(n, self.alloc[pond])
            self.alloc[pond] -= take
            n -= take
        if n > 1e-9:
            tot = sum(self.alloc.values())
            if tot > 0:
                f = max(0.0, 1.0 - n / tot)
                self.scale_alloc(f)
        self._clean()
        return n

    def move(self, n: float, src: str | None, dst: str):
        src = src or self._largest()
        avail = self.alloc.get(src, 0.0)
        take = min(n, avail)
        if take <= 0:
            return 0.0
        self.alloc[src] = avail - take
        self.alloc[dst] = self.alloc.get(dst, 0.0) + take
        self._clean()
        return take

    def _largest(self):
        """
        مبدأ پیش‌فرض یک انتقال بدون استخر مبدأ.

        اگر ماهی تخصیص‌نیافته (تراف) موجود باشد، همان مبدأ است — نه بزرگ‌ترین
        استخر. وگرنه تخصیص یک cohort به استخرها می‌توانست ماهی را از استخری
        که تازه پر شده بیرون بکشد.
        """
        for pool in (TROUGH, UNASSIGNED):
            if self.alloc.get(pool, 0.0) >= 1:
                return pool
        return max(self.alloc, key=lambda k: self.alloc[k]) if self.alloc else TROUGH

    def _clean(self):
        for k in [k for k, v in self.alloc.items() if v < 0.5]:
            self.alloc.pop(k, None)


class FarmState:
    """وضعیت کامل مزرعه در یک تاریخ مشخص."""

    def __init__(self, db, A, bio, as_of: date | None = None):
        self.db = db
        self.A = A
        self.bio = bio
        self.as_of = as_of or date.today()
        self.cohorts: dict[str, CohortState] = {}
        self.feed = {}
        self.water_readings = {}
        self.warnings = []
        self._build()

    # ================================================================ clone
    def clone(self) -> "FarmState":
        """
        یک نسخه کاملاً مستقل از وضعیت فعلی، برای سناریوهای فرضی.

        تاریخچه تراکنش‌ها دوباره بازپخش نمی‌شود (پرهزینه و غیرلازم است)؛
        فقط وضعیت محاسبه‌شده کپی می‌گردد. پایگاه داده، فرضیات و زیست‌شناسی
        مشترک می‌مانند ولی هرگز از این مسیر نوشته نمی‌شوند.

        هر تغییری روی نسخه کپی، **هیچ اثری روی داده واقعی ندارد**.
        """
        st = FarmState.__new__(FarmState)
        st.db, st.A, st.bio, st.as_of = self.db, self.A, self.bio, self.as_of
        st.cohorts = {cid: c.copy() for cid, c in self.cohorts.items()}
        st.feed = {k: dict(v) for k, v in self.feed.items()}
        st.water_readings = {k: dict(v) for k, v in self.water_readings.items()}
        st.warnings = list(self.warnings)
        st.unassigned_sales = [dict(x) for x in getattr(self, "unassigned_sales", [])]
        st.sale_alloc_by_cohort = {k: dict(v) for k, v
                                   in getattr(self, "sale_alloc_by_cohort", {}).items()}
        st.sale_alloc_by_txn = {k: list(v) for k, v
                                in getattr(self, "sale_alloc_by_txn", {}).items()}
        st.suggested = {k: [dict(x) for x in v]
                        for k, v in getattr(self, "suggested", {}).items()}
        st.hypothetical = True
        st.hypothetical_events = list(getattr(self, "hypothetical_events", []))
        return st

    # ================================================================ build
    def _build(self):
        txns = self.db.active_txns()

        # ---- 0. تخصیص‌های چند-cohort فروش (فقط تأییدشده‌ها اثر دارند)
        self.sale_alloc_by_cohort: dict[str, dict] = {}
        self.sale_alloc_by_txn: dict[int, list] = {}
        for r in self.db.sale_allocations(basis="confirmed"):
            self.sale_alloc_by_cohort.setdefault(r["cohort_id"], {})[r["txn_id"]] = \
                float(r["quantity"])
            self.sale_alloc_by_txn.setdefault(r["txn_id"], []).append(r)

        # ---- 1. cohorts از خرید تخم
        for t in txns:
            if t["txn_type"] == "egg_purchase":
                cid = t["cohort_id"] or f"C-{t['txn_date']}-{t['id']}"
                c = CohortState(cid, d(t["txn_date"]), t.get("counterparty"),
                                t.get("quantity") or 0, t.get("unit_price") or 0,
                                t.get("note"))
                if t.get("pond_id"):
                    c.alloc = {t["pond_id"]: c.egg_count}
                self.cohorts[cid] = c

        # ---- 2. رویدادهای هر cohort
        by_cohort: dict[str, list] = {cid: [] for cid in self.cohorts}
        for t in txns:
            if t["txn_type"] == "egg_purchase":
                continue
            cid = t.get("cohort_id")
            if cid in by_cohort:
                by_cohort[cid].append(t)
            elif t["txn_type"] == "sale" and t["id"] in self.sale_alloc_by_txn:
                # فروشی که فیلد cohort_id ندارد ولی بین چند cohort تقسیم شده
                for r in self.sale_alloc_by_txn[t["id"]]:
                    if r["cohort_id"] in by_cohort:
                        by_cohort[r["cohort_id"]].append(t)

        for cid, c in self.cohorts.items():
            self._replay_cohort(c, sorted(by_cohort[cid],
                                          key=lambda r: (r["txn_date"], r["id"])))

        # ---- 3. فروش‌های تاریخی بدون cohort مشخص
        # هرگز به یک cohort حدسی نسبت داده نمی‌شوند.
        self.unassigned_sales = []
        known = set(self.cohorts)
        for t in txns:
            if t["txn_type"] != "sale" or d(t["txn_date"]) > self.as_of:
                continue
            allocated = sum(float(r["quantity"])
                            for r in self.sale_alloc_by_txn.get(t["id"], []))
            qty = float(t.get("quantity") or 0)
            fully = allocated >= qty - 1e-6 and qty > 0
            if (t.get("cohort_id") or None) not in known and not fully:
                self.unassigned_sales.append({
                    "txn_id": t["id"], "date": t["txn_date"],
                    "quantity": float(t.get("quantity") or 0),
                    "weight_g": t.get("weight_g"),
                    "amount": float(t.get("amount") or 0),
                    "unit_price": (float(t["amount"]) / float(t["quantity"]))
                    if t.get("amount") and t.get("quantity") else t.get("unit_price"),
                    "counterparty": t.get("counterparty"),
                    "note": t.get("note"),
                    "allocated": allocated,
                    "unallocated": max(0.0, qty - allocated),
                })

        # ---- 4. خوراک
        self._build_feed(txns)

        # ---- 5. آخرین قرائت آب هر استخر
        for t in txns:
            if t["txn_type"] == "water_reading" and t.get("pond_id"):
                self.water_readings[t["pond_id"]] = {
                    "date": t["txn_date"],
                    **(t.get("payload") or {}),
                }

        # ---- 6. تخصیص پیشنهادی (Estimated) برای ماهی بدون استخر
        self._suggest_allocation()

        # ---- 7. هشدارهای کامل‌نبودن داده
        self._data_gap_warnings()

    def _data_gap_warnings(self):
        for c in self.cohorts.values():
            if c.alive >= 1 and c.mean_weight > 15.0:
                self.warnings.append(
                    f"cohort {c.cohort_id}: وزن تخمینی {c.mean_weight:.1f}g از بازه "
                    f"معتبر مدل (تا ۱۵ گرم) عبور کرده و فروشی ثبت نشده است — "
                    f"احتمالاً رکورد فروش وارد نشده یا رشد کندتر بوده است.")
            if c.alive >= 1 and c.alloc.get(TROUGH, 0) >= 1 and \
                    self.bio.counts_toward_pond_capacity(c.mean_weight):
                self.warnings.append(
                    f"cohort {c.cohort_id}: از ۱ گرم عبور کرده ولی استخر واقعی "
                    f"برایش ثبت نشده — تخصیص نمایش‌داده‌شده «تخمینی» است.")
        if not any(t for t in self.db.active_txns() if t["txn_type"] == "feed_purchase"):
            self.warnings.append("هیچ خرید خوراکی ثبت نشده است؛ موجودی خوراک صفر فرض شده.")
        if self.unassigned_sales:
            n = sum(s["quantity"] for s in self.unassigned_sales)
            self.warnings.append(
                f"{len(self.unassigned_sales)} فروش تاریخی با مجموع {n:,.0f} قطعه به هیچ "
                f"cohort مشخصی نسبت داده نشده است (Historical Sale — Cohort Unassigned). "
                f"درآمد آن‌ها در دفتر نقدی ثبت شده، ولی موجودی cohortها تا مشخص شدن "
                f"cohort مبدأ کاهش نیافته است.")

    # ------------------------------------------------------ cohort replay
    def _replay_cohort(self, c: CohortState, events: list):
        bio, as_of = self.bio, self.as_of
        alloc = self.sale_alloc_by_cohort.get(c.cohort_id, {})

        mort_dates = sorted(d(e["txn_date"]) for e in events
                            if e["txn_type"] == "mortality" and d(e["txn_date"]) <= as_of)
        # پنجره‌ای که گزارش واقعی تلفات آن را پوشش می‌دهد: از اولین گزارش تا آخرین
        # گزارش. قبل از اولین گزارش، منحنی مدل اعمال می‌شود (مزرعه از یک تاریخی
        # شروع به ثبت کرده است و نباید کل گذشته را بدون تلفات فرض کنیم).
        mort_start = mort_dates[0] if mort_dates else None
        t_act = mort_dates[-1] if mort_dates else None

        cursor = c.purchase_date
        has_actual_count = False

        def advance(to_day: date):
            nonlocal cursor
            if to_day <= cursor:
                cursor = max(cursor, to_day)
                return
            a, b = cursor, to_day
            if mort_start is None:
                segs = [(a, b)]
            else:
                segs = []
                if a < mort_start:
                    segs.append((a, min(b, mort_start)))
                if b > t_act:
                    segs.append((max(a, t_act), b))
            f = 1.0
            for s, e in segs:
                if e > s:
                    f *= bio.survival_ratio(_age(c.purchase_date, s),
                                            _age(c.purchase_date, e))
            if f != 1.0:
                c.alive *= f
                c.scale_alloc(f)
            cursor = to_day

        for e in events:
            ed = d(e["txn_date"])
            if ed > as_of:
                continue
            typ = e["txn_type"]
            qty = float(e.get("quantity") or 0)

            if typ == "mortality":
                advance(ed)
                dead = min(qty, c.alive)
                c.alive -= dead
                c.recorded_mortality += dead
                c.remove(dead, e.get("pond_id"))
                has_actual_count = True

            elif typ == "count_observation":
                advance(ed)
                pl = e.get("payload") or {}
                # Pond Details > «سایر موارد» همچنان از count_observation موجود
                # استفاده می‌کند، اما delta فقط روی همان استخر اعمال می‌شود.
                # سایر count_observationهای قدیمی بدون تغییر، anchor مطلق cohort هستند.
                if pl.get("entry") == "pond_panel" and pl.get("pond_adjustment_delta") is not None:
                    delta = float(pl.get("pond_adjustment_delta") or 0.0)
                    pond = e.get("pond_id") or TROUGH
                    if delta >= 0:
                        c.alive += delta
                        c.alloc[pond] = c.alloc.get(pond, 0.0) + delta
                    else:
                        take = min(-delta, c.alive, c.alloc.get(pond, 0.0))
                        c.alive -= take
                        c.remove(take, pond)
                    c.count_anchor_date = ed
                    c.count_basis = "actual"
                    has_actual_count = True
                else:
                    old = c.alive
                    c.alive = qty
                    c.count_anchor_date = ed
                    c.count_basis = "actual"
                    has_actual_count = True
                    if old > 0:
                        c.scale_alloc(qty / old)
                    else:
                        c.alloc = {e.get("pond_id") or TROUGH: qty}

            elif typ == "weight_sample":
                advance(ed)
                w = float(e.get("weight_g") or 0)
                if w > 0:
                    implied = bio.age_at_weight(w)
                    c.growth_offset_days = implied - _age(c.purchase_date, ed)
                    c.weight_basis = "actual"
                    c.weight_anchor_date = ed
                    pl = e.get("payload") or {}
                    sd = pl.get("sd_g")
                    if sd:
                        obs_cv = float(sd) / w
                        c.cv_shift = obs_cv - bio.cv_at_weight(w)

            elif typ == "sale":
                advance(ed)
                # اگر این فروش بین چند cohort تقسیم شده، فقط سهم همین cohort
                # کم می‌شود. سهم تأییدشده بر مقدار کل تراکنش اولویت دارد.
                share = alloc.get(e["id"])
                q_eff = share if share is not None else qty
                n = min(q_eff, c.alive)
                c.alive -= n
                c.sold_count += n
                total_q = qty or 1.0
                amt = float(e.get("amount") or
                            (total_q * float(e.get("unit_price") or 0)))
                c.sold_revenue += amt * (n / total_q) if total_q else 0.0
                c.remove(n, e.get("pond_id"))

            elif typ == "transfer":
                advance(ed)
                c.move(qty if qty > 0 else c.alive, e.get("pond_id"),
                       e.get("to_pond_id") or TROUGH)

            elif typ == "feed_consumption":
                advance(ed)
                c.feed_kg_actual += qty
                c.feed_cost_actual += float(e.get("amount") or 0)

        advance(as_of)
        c.alive = max(0.0, c.alive)
        if c.alive < 1:
            c.closed = True
        if has_actual_count and c.count_anchor_date >= c.purchase_date:
            c.count_basis = "actual" if cursor >= as_of - timedelta(days=0) and \
                (t_act == as_of) else "estimated_from_actual"
        else:
            c.count_basis = "estimated"
        # وزن
        c.mean_weight = self.weight_of(c, as_of)

    # ------------------------------------------------------------ helpers
    def weight_of(self, c: CohortState, on: date) -> float:
        eff_age = _age(c.purchase_date, on) + c.growth_offset_days
        return self.bio.weight_at_age(eff_age)

    def cv_of(self, c: CohortState, w: float | None = None) -> float:
        w = c.mean_weight if w is None else w
        return max(0.02, self.bio.cv_at_weight(w) + c.cv_shift)

    def alive_on_past(self, c: CohortState, on: date) -> float:
        """
        تعداد زنده تخمینی در یک تاریخ **گذشته**.

        از تعداد تخم شروع می‌کند، منحنی تلفات مدل را تا آن تاریخ اعمال
        می‌کند و فروش‌ها/تلفات واقعی ثبت‌شده تا آن تاریخ را کم می‌کند.
        برای تشخیص مبدأ فروش‌های تاریخی لازم است.
        """
        if on >= self.as_of:
            return c.alive
        if on < c.purchase_date:
            return 0.0
        age = _age(c.purchase_date, on)
        n = c.egg_count * self.bio.survival(age)
        for t in self.db.active_txns():
            if t.get("cohort_id") != c.cohort_id:
                continue
            if d(t["txn_date"]) > on:
                continue
            if t["txn_type"] in ("sale", "mortality"):
                n -= float(t.get("quantity") or 0)
        return max(0.0, n)

    def alive_at(self, c: CohortState, on: date) -> float:
        """پیش‌بینی تعداد زنده در تاریخ آینده (بدون فروش برنامه‌ریزی‌نشده)."""
        if on < self.as_of:
            return self.alive_on_past(c, on)
        if on == self.as_of:
            return c.alive
        a0 = _age(c.purchase_date, self.as_of)
        a1 = _age(c.purchase_date, on)
        return c.alive * self.bio.survival_ratio(a0, a1)

    # --------------------------------------------------------------- feed
    def _build_feed(self, txns: list):
        inv: dict[str, dict] = {}
        for t in txns:
            if d(t["txn_date"]) > self.as_of:
                continue
            pl = t.get("payload") or {}
            name = pl.get("feed_name") or t.get("note") or "نامشخص"
            if t["txn_type"] == "feed_purchase":
                rec = inv.setdefault(name, self._empty_feed(name, pl))
                kg = float(t.get("quantity") or 0)
                amt = float(t.get("amount") or (kg * float(t.get("unit_price") or 0)))
                rec["purchased_kg"] += kg
                rec["purchased_cost"] += amt
                rec["qty_kg"] += kg
                rec["value"] += amt
                rec["last_purchase"] = t["txn_date"]
                if pl.get("w_min") is not None:
                    rec["w_min"] = float(pl["w_min"])
                    rec["w_max"] = float(pl.get("w_max", rec["w_max"]))
            elif t["txn_type"] == "feed_consumption":
                rec = inv.setdefault(name, self._empty_feed(name, pl))
                kg = float(t.get("quantity") or 0)
                avg = rec["value"] / rec["qty_kg"] if rec["qty_kg"] > 0 else 0.0
                rec["consumed_kg"] += kg
                rec["qty_kg"] -= kg
                rec["value"] -= kg * avg
        for r in inv.values():
            r["avg_cost"] = r["purchased_cost"] / r["purchased_kg"] if r["purchased_kg"] else 0.0
            r["qty_kg"] = round(r["qty_kg"], 3)
            r["has_consumption_records"] = r["consumed_kg"] > 0
        self.feed = inv

    @staticmethod
    def _empty_feed(name, pl):
        return {"name": name, "w_min": pl.get("w_min"), "w_max": pl.get("w_max"),
                "qty_kg": 0.0, "value": 0.0, "purchased_kg": 0.0,
                "purchased_cost": 0.0, "consumed_kg": 0.0,
                "last_purchase": None, "avg_cost": 0.0}

    def daily_feed_demand(self) -> dict:
        """نیاز خوراک امروز به تفکیک نوع خوراک (kg) — Estimated."""
        out = {}
        total = 0.0
        for c in self.cohorts.values():
            if c.alive < 1:
                continue
            w0 = c.mean_weight
            w1 = self.weight_of(c, self.as_of + timedelta(days=1))
            kg = self.bio.feed_kg_for_growth(c.alive, w0, w1)
            band = self.bio.feed_name(w0)
            out[band] = out.get(band, 0.0) + kg
            total += kg
        out["__total__"] = total
        return out

    # ----------------------------------------------- suggested allocation
    def _suggest_allocation(self):
        """
        ماهی‌هایی که از آستانه ظرفیت عبور کرده‌اند ولی استخر واقعی ندارند،
        به‌صورت «تخمینی» روی استخرهای آزاد چیده می‌شوند.
        هیچ تراکنشی نوشته نمی‌شود؛ فقط پیشنهاد نمایشی است.
        """
        self.suggested = {}
        ponds = [p["pond_id"] for p in self.db.ponds()
                 if p["role"] == "operational"]
        used = set()
        for c in self.cohorts.values():
            for pid, n in c.alloc.items():
                if pid not in (TROUGH, UNASSIGNED) and n > 0:
                    used.add(pid)
        free = [p for p in ponds if p not in used]
        for c in sorted(self.cohorts.values(), key=lambda x: x.purchase_date):
            n = c.alloc.get(TROUGH, 0.0)
            if n < 1 or not self.bio.counts_toward_pond_capacity(c.mean_weight):
                continue
            cap = self.bio.fish_per_pond(c.mean_weight)
            need = int(math.ceil(n / cap))
            per = n / need if need else 0
            for _ in range(need):
                if not free:
                    self.warnings.append(
                        f"cohort {c.cohort_id}: استخر آزاد کافی برای تخصیص وجود ندارد")
                    break
                pid = free.pop(0)
                self.suggested.setdefault(pid, []).append(
                    {"cohort_id": c.cohort_id, "count": per,
                     "mean_weight": c.mean_weight})

    # ======================================================= public views
    def pond_view(self) -> list:
        ponds = self.db.ponds()
        cap_target = float(self.A.get("capacity.target_utilisation"))
        out = []
        for p in ponds:
            pid = p["pond_id"]
            occ, est = [], False
            for c in self.cohorts.values():
                n = c.alloc.get(pid, 0.0)
                if n >= 1:
                    occ.append({"cohort_id": c.cohort_id, "count": n,
                                "mean_weight": c.mean_weight,
                                "age_days": _age(c.purchase_date, self.as_of),
                                "basis": "actual"})
            for s in getattr(self, "suggested", {}).get(pid, []):
                occ.append({**s, "age_days": None, "basis": "estimated"})
                est = True
            count = sum(o["count"] for o in occ)
            biomass = sum(o["count"] * o["mean_weight"] for o in occ) / 1000.0
            avg_w = (sum(o["count"] * o["mean_weight"] for o in occ) / count) if count else 0.0
            cap = self.bio.fish_per_pond(avg_w) if count else 0.0
            util = (count / cap) if cap else 0.0
            constrained = self.bio.counts_toward_pond_capacity(avg_w) if count else False
            status = "empty"
            if count > 0:
                status = "over" if (constrained and util > cap_target) else "occupied"
            if p["role"] == "reserve" and count == 0:
                status = "reserve"
            nxt = self._next_milestone_for_pond(occ)
            o2 = self.bio.oxygen_headroom(biomass)
            out.append({
                "pond_id": pid, "label": p["label"], "role": p["role"],
                "volume_m3": p["volume_m3"], "status": status,
                "occupants": occ, "count": count, "biomass_kg": biomass,
                "avg_weight_g": avg_w, "capacity": cap,
                "utilisation": util, "capacity_applies": constrained,
                "basis": "estimated" if est else ("actual" if count else "-"),
                "next_milestone": nxt,
                "water": self.water_readings.get(pid),
                "oxygen_load": o2["load_ratio"],
            })
        return out

    def _next_milestone_for_pond(self, occ):
        best = None
        for o in occ:
            c = self.cohorts.get(o["cohort_id"])
            if not c:
                continue
            m = self.next_milestone(c)
            if m and (best is None or m["date"] < best["date"]):
                best = m
        return best

    def next_milestone(self, c: CohortState):
        from .biology import MILESTONE_WEIGHTS
        w = c.mean_weight
        for mw in MILESTONE_WEIGHTS:
            if w < mw - 1e-9:
                target_age = self.bio.age_at_weight(mw) - c.growth_offset_days
                dt = c.purchase_date + timedelta(days=int(round(target_age)))
                return {"weight_g": mw, "date": dt.isoformat(),
                        "days": (dt - self.as_of).days}
        return None

    def cohort_view(self) -> list:
        out = []
        for c in sorted(self.cohorts.values(), key=lambda x: x.purchase_date):
            w = c.mean_weight
            cv = self.cv_of(c, w)
            bands = self.bio.weight_bands(w, cv)
            age = _age(c.purchase_date, self.as_of)
            biomass = c.alive * w / 1000.0
            feed_est = self.bio.feed_kg_for_growth(
                c.alive, float(self.A.get("growth.start_weight_g")), w,
                n_died=max(0.0, c.egg_count - c.alive))
            feed_cost_est = self._est_feed_cost(c)
            out.append({
                "cohort_id": c.cohort_id,
                "purchase_date": c.purchase_date.isoformat(),
                "supplier": c.supplier,
                "egg_count": c.egg_count,
                "egg_price": c.egg_price,
                "egg_cost": c.egg_count * c.egg_price,
                "age_days": age,
                "alive": c.alive,
                "count_basis": c.count_basis,
                "count_anchor_date": c.count_anchor_date.isoformat(),
                "recorded_mortality": c.recorded_mortality,
                "cum_mortality": 1 - (c.alive + c.sold_count) / c.egg_count
                if c.egg_count else 0,
                "mean_weight_g": w,
                "weight_basis": c.weight_basis,
                "weight_anchor_date": c.weight_anchor_date.isoformat()
                if c.weight_anchor_date else None,
                "growth_offset_days": round(c.growth_offset_days, 1),
                "cv": cv,
                "w_slow": bands["slow"], "w_typical": bands["typical"],
                "w_fast": bands["fast"],
                "biomass_kg": biomass,
                "ponds_required": self.bio.ponds_required(c.alive, w),
                "ponds": {k: v for k, v in c.alloc.items() if v >= 1},
                "next_milestone": self.next_milestone(c),
                "sold_count": c.sold_count,
                "sold_revenue": c.sold_revenue,
                "feed_kg_actual": c.feed_kg_actual,
                "feed_kg_estimated": feed_est,
                "feed_cost_estimated": feed_cost_est,
                "cumulative_cost": c.egg_count * c.egg_price +
                (c.feed_cost_actual or feed_cost_est),
                "stock_value_at_current_price": c.alive * self.bio.sale_price(w),
                "closed": c.closed,
                "beyond_model_range": c.mean_weight > 15.0,
                "note": c.note,
            })
        return out

    def _est_feed_cost(self, c: CohortState) -> float:
        """هزینه خوراک تجمعی تخمینی از روز صفر تا امروز (piecewise by weight)."""
        w0 = float(self.A.get("growth.start_weight_g"))
        w1 = c.mean_weight
        if w1 <= w0:
            return 0.0
        per_g = self.bio.feed_cost_per_gram_gain(w0, w1)
        avg_pop = (c.egg_count + c.alive + c.sold_count) / 2.0
        return per_g * (w1 - w0) * avg_pop

    # ------------------------------------------------------------ totals
    def summary(self) -> dict:
        ponds = self.pond_view()
        live = sum(c.alive for c in self.cohorts.values())
        biomass = sum(c.alive * c.mean_weight for c in self.cohorts.values()) / 1000.0
        active = [c for c in self.cohorts.values() if c.alive >= 1]
        op_used = len([p for p in ponds if p["role"] == "operational" and p["count"] > 0])
        res_used = len([p for p in ponds if p["role"] == "reserve" and p["count"] > 0])
        feed_kg = sum(f["qty_kg"] for f in self.feed.values())
        demand = self.daily_feed_demand()["__total__"]
        days_left = (feed_kg / demand) if demand > 0 else None
        ponds_req = sum(self.bio.ponds_required(c.alive, c.mean_weight)
                        for c in self.cohorts.values())
        return {
            "as_of": self.as_of.isoformat(),
            "live_fish": live,
            "active_cohorts": len(active),
            "operational_ponds_used": op_used,
            "operational_ponds_total": int(self.A.get("farm.operational_ponds")),
            "reserve_ponds_used": res_used,
            "reserve_ponds_total": int(self.A.get("farm.reserve_ponds")),
            "ponds_required_now": ponds_req,
            "total_biomass_kg": biomass,
            "feed_inventory_kg": feed_kg,
            "feed_daily_demand_kg": demand,
            "feed_days_remaining": days_left,
            "stock_value": sum(c.alive * self.bio.sale_price(c.mean_weight)
                               for c in self.cohorts.values()),
            "eggs_purchased_total": sum(c.egg_count for c in self.cohorts.values()),
            "fish_sold_total": sum(c.sold_count for c in self.cohorts.values())
            + sum(s["quantity"] for s in self.unassigned_sales),
            "sales_revenue_total": sum(c.sold_revenue for c in self.cohorts.values())
            + sum(s["amount"] for s in self.unassigned_sales),
            "unassigned_sales_count": len(self.unassigned_sales),
            "unassigned_sales_fish": sum(s["quantity"] for s in self.unassigned_sales),
            "unassigned_sales_revenue": sum(s["amount"] for s in self.unassigned_sales),
            "warnings": self.warnings,
        }
