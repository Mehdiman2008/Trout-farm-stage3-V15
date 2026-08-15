"""
assumptions.py — رجیستری فرضیات
=================================
مقادیر پیش‌فرض از config.yaml خوانده می‌شوند و override های کاربر از
جدول assumption_overrides اعمال می‌گردند.

  effective value = default (config.yaml)  ⊕  override (DB)

config.yaml هرگز توسط برنامه بازنویسی نمی‌شود؛ بنابراین «Reset to Default»
همیشه امکان‌پذیر است.

هر تغییر فرضیات باعث recalculate شدن state/forecast می‌شود ولی هیچ داده
تاریخی واقعی را تغییر نمی‌دهد.
"""
from __future__ import annotations

import os
import yaml

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "config.yaml")


def _coerce(ptype: str, value):
    """تبدیل امن مقدار ورودی UI به نوع درست."""
    if value is None:
        return None
    try:
        if ptype == "int":
            return int(round(float(value)))
        if ptype == "float":
            return float(value)
        if ptype == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on", "بله")
        if ptype == "list_float":
            if isinstance(value, str):
                value = [v for v in value.replace("،", ",").split(",") if v.strip()]
            return [float(v) for v in value]
        if ptype == "list_str":
            if isinstance(value, str):
                value = [v.strip() for v in value.replace("،", ",").split(",") if v.strip()]
            return [str(v).strip() for v in value if str(v).strip()]
        if ptype == "table":
            if not isinstance(value, list):
                raise ValueError("جدول باید لیست باشد")
            return value
        return value  # str / date / choice
    except Exception as e:  # pragma: no cover
        raise ValueError(f"مقدار نامعتبر ({ptype}): {value} — {e}")


class Assumptions:
    def __init__(self, db=None, config_path: str = CONFIG_PATH):
        self.config_path = config_path
        with open(config_path, encoding="utf-8") as f:
            self.cfg = yaml.safe_load(f)
        self.meta = self.cfg.get("meta", {})
        self.groups = self.cfg.get("groups", [])
        self.defs = {}
        for p in self.cfg.get("parameters", []):
            self.defs[p["key"]] = p
        self.db = db
        self.overrides = {}
        self.hist = {}
        # overlay موقت و فقط-در-حافظه برای سناریوهای What-If (اصلاح ۶ مرحله ۳).
        # هرگز در پایگاه داده نوشته نمی‌شود، پس یک سناریوی فرضی هیچ اثری روی
        # داده واقعی یا روی درخواست‌های همزمان دیگر ندارد.
        self._overlay: dict = {}
        self.refresh()

    # ------------------------------------------------ overlay فرضی (موقت)
    def push_overlay(self, values: dict):
        """اعمال موقت چند فرض؛ خروجی، overlay قبلی برای بازگرداندن است."""
        prev = dict(self._overlay)
        for k, v in (values or {}).items():
            if k not in self.defs:
                raise KeyError(f"پارامتر ناشناخته: {k}")
            self._overlay[k] = _coerce(self.defs[k].get("type", "float"), v)
        return prev

    def pop_overlay(self, prev: dict | None = None):
        self._overlay = dict(prev or {})

    @property
    def overlay(self) -> dict:
        return dict(self._overlay)

    # ----------------------------------------------------------- reload
    def refresh(self):
        if self.db:
            self.overrides = self.db.get_overrides()
            self.hist = {}
            for r in self.db.assumption_history():
                self.hist.setdefault(r["key"], []).append(
                    {"effective_from": r["effective_from"], "value": r["value"],
                     "note": r.get("note"), "changed_at": r["changed_at"], "id": r["id"]})
            for v in self.hist.values():
                v.sort(key=lambda x: (x["effective_from"], x["id"]))
        return self

    # -------------------------------------------------------------- get
    def get(self, key: str, default=None):
        if key in self._overlay:                       # سناریوی فرضی
            return self._overlay[key]
        if key in self.overrides:
            return self.overrides[key]["value"]
        d = self.defs.get(key)
        if d is None:
            if default is not None:
                return default
            raise KeyError(f"پارامتر ناشناخته: {key}")
        return d.get("value", default)

    # ------------------------------------------- effective-dated lookup (۶)
    def is_effective_dated(self, key: str) -> bool:
        return bool(self.defs.get(key, {}).get("effective_dated"))

    def get_at(self, key: str, on, default=None):
        """
        مقدار یک پارامتر در یک تاریخ مشخص.

        قاعده: تغییر قیمت هرگز retroactive نیست. اگر برای این کلید تاریخچه
        effective_from ثبت شده باشد، آخرین رکوردی که تاریخ اعتبارش ≤ تاریخ
        موردنظر است برگردانده می‌شود؛ اگر هیچ رکوردی تا آن تاریخ نباشد، مقدار
        جاری (override یا پیش‌فرض config) اعمال می‌شود.

        این تابع فقط برای forecast و برای تراکنش‌هایی است که قیمت واقعی
        ندارند. قیمت واقعی ثبت‌شده در یک تراکنش همیشه اولویت دارد.
        """
        if key in self._overlay:                       # سناریوی فرضی
            return self._overlay[key]
        if on is None or not self.hist.get(key):
            return self.get(key, default)
        on = str(on)[:10]
        best = None
        for h in self.hist[key]:                      # مرتب بر اساس effective_from
            if h["effective_from"] <= on:
                best = h
            else:
                break
        return best["value"] if best else self.get(key, default)

    def set_effective(self, key: str, value, effective_from: str,
                      note: str = "", changed_by: str = "user"):
        """ثبت یک مقدار جدید با تاریخ اعتبار، بدون تغییر مقادیر گذشته."""
        if key not in self.defs:
            raise KeyError(f"پارامتر ناشناخته: {key}")
        if not self.is_effective_dated(key):
            raise ValueError("این پارامتر effective-dated نیست؛ از ویرایش معمولی استفاده کنید")
        d = self.defs[key]
        val = _coerce(d.get("type", "float"), value)
        for bound, cmpf in (("min", lambda a, b: a < b), ("max", lambda a, b: a > b)):
            if d.get(bound) is not None and isinstance(val, (int, float)):
                if cmpf(val, d[bound]):
                    raise ValueError(f"مقدار خارج از بازه مجاز ({d.get('min')} .. {d.get('max')})")
        if not self.db:
            raise ValueError("برای ثبت تاریخچه، پایگاه داده لازم است")
        hid = self.db.add_assumption_history(key, val, effective_from, note, changed_by)
        self.refresh()
        return {"id": hid, "key": key, "value": val, "effective_from": effective_from[:10]}

    def history(self, key: str | None = None) -> list:
        if not self.db:
            return []
        rows = self.db.assumption_history(key)
        for r in rows:
            r["label_fa"] = self.defs.get(r["key"], {}).get("label_fa", r["key"])
            r["unit"] = self.defs.get(r["key"], {}).get("unit", "")
        return rows

    def effective_dated_keys(self) -> list:
        return [k for k, d in self.defs.items() if d.get("effective_dated")]

    def fingerprint(self) -> str:
        """
        اثر انگشت مجموعه فرضیات مؤثر.

        برای cache کردن نتایج حل مدل لازم است: اگر این رشته عوض نشود، همان
        ورودی‌ها همان خروجی را می‌دهند (reproducible).
        """
        import hashlib
        import json
        payload = json.dumps(
            {"ov": {k: v["value"] for k, v in sorted(self.overrides.items())},
             "ol": {k: v for k, v in sorted(self._overlay.items())},
             "hist": {k: [(h["effective_from"], h["value"]) for h in v]
                      for k, v in sorted(self.hist.items())}},
            sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]

    def default_of(self, key: str):
        return self.defs[key].get("value")

    def is_overridden(self, key: str) -> bool:
        return key in self.overrides

    # -------------------------------------------------------------- set
    def set(self, key: str, value, changed_by: str = "user"):
        if key not in self.defs:
            raise KeyError(f"پارامتر ناشناخته: {key}")
        d = self.defs[key]
        val = _coerce(d.get("type", "float"), value)
        if d.get("type") == "choice" and d.get("choices") is not None:
            choices = d["choices"]
            if val not in choices:
                # تلاش برای تطبیق عددی
                try:
                    fv = float(val)
                    match = [c for c in choices if isinstance(c, (int, float)) and abs(float(c) - fv) < 1e-12]
                    if match:
                        val = match[0]
                    else:
                        raise ValueError
                except Exception:
                    raise ValueError(f"مقدار مجاز نیست؛ گزینه‌ها: {choices}")
        for bound, cmpf in (("min", lambda a, b: a < b), ("max", lambda a, b: a > b)):
            if d.get(bound) is not None and isinstance(val, (int, float)):
                if cmpf(val, d[bound]):
                    raise ValueError(f"مقدار خارج از بازه مجاز ({d.get('min')} .. {d.get('max')})")
        if self.db:
            self.db.set_override(key, val, changed_by)
            self.refresh()
        else:
            self.defs[key]["value"] = val
        return val

    def reset(self, key: str):
        if self.db:
            self.db.clear_override(key)
            self.refresh()

    def reset_all(self):
        if self.db:
            self.db.clear_all_overrides()
            self.refresh()

    # ------------------------------------------------------ description
    def describe(self) -> dict:
        """خروجی کامل برای تب فرضیات."""
        groups = []
        by_group = {}
        for key, d in self.defs.items():
            ov = self.overrides.get(key)
            item = {
                "key": key,
                "group": d.get("group", "other"),
                "label_fa": d.get("label_fa", key),
                "value": self.get(key),
                "default": d.get("value"),
                "unit": d.get("unit", ""),
                "source": d.get("source", "MA"),
                "type": d.get("type", "float"),
                "choices": d.get("choices"),
                "columns": d.get("columns"),
                "min": d.get("min"),
                "max": d.get("max"),
                "note_fa": d.get("note_fa"),
                "overridden": ov is not None,
                "changed_at": ov["changed_at"] if ov else None,
                "changed_by": ov["changed_by"] if ov else None,
                "effective_dated": bool(d.get("effective_dated")),
                "history": self.hist.get(key, []),
            }
            by_group.setdefault(item["group"], []).append(item)
        for g in self.groups:
            groups.append({"id": g["id"], "title_fa": g["title_fa"],
                           "params": by_group.get(g["id"], [])})
        # گروه‌های تعریف‌نشده
        known = {g["id"] for g in self.groups}
        for gid, params in by_group.items():
            if gid not in known:
                groups.append({"id": gid, "title_fa": gid, "params": params})
        return {"groups": groups,
                "override_count": len(self.overrides),
                "config_path": os.path.basename(self.config_path)}
