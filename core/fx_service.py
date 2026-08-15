"""Live USD/Toman service: Provider -> Fetch -> Validate -> Store -> Dashboard.

Historical Excel remains the backfill/benchmark source. Live providers are
optional and configured only through environment variables; no secrets are
stored in source or config.yaml.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


def _utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class FXObservation:
    date_g: str
    close_toman: float
    source: str
    fetched_at: str
    verified: bool = True
    stale: bool = False

    def asdict(self):
        return self.__dict__.copy()


class FXProvider:
    name = "provider"
    priority = 0

    def fetch(self) -> list[FXObservation]:  # pragma: no cover - interface
        raise NotImplementedError


class NavasanAPIProvider(FXProvider):
    """Navasan daily OHLC adapter for verified USD/Toman closes.

    Navasan documents ``ohlcSearch`` for daily open/high/low/close data and
    accepts Unix timestamps for ``start`` and ``end``.  We deliberately end
    the request on yesterday in Iran time, so an intraday quote is never
    labelled as a verified closing price.

    Environment:
      NAVASAN_API_KEY          required
      NAVASAN_API_BASE_URL     optional; default https://api.navasan.tech
      NAVASAN_ITEM             optional; default usd_sell (Tehran USD sell)
      NAVASAN_LOOKBACK_DAYS    optional; default 10
      NAVASAN_VALUE_DIVISOR    optional; default 1 (Navasan market values are Toman)
    """

    name = "navasan-api"
    priority = 30

    def __init__(self, today: date | None = None):
        self.key = os.getenv("NAVASAN_API_KEY", "").strip()
        if not self.key:
            raise ValueError("NAVASAN_API_KEY is not configured")
        self.base = os.getenv("NAVASAN_API_BASE_URL", "https://api.navasan.tech").strip().rstrip("/")
        self.item = os.getenv("NAVASAN_ITEM", "usd_sell").strip() or "usd_sell"
        self.lookback = max(2, int(os.getenv("NAVASAN_LOOKBACK_DAYS", "10")))
        self.divisor = float(os.getenv("NAVASAN_VALUE_DIVISOR", "1"))
        self.today = today or datetime.now(ZoneInfo("Asia/Tehran")).date()

    @staticmethod
    def _unix_for_iran_day(d: date, end=False) -> int:
        t = datetime.combine(d, datetime.max.time() if end else datetime.min.time(), tzinfo=ZoneInfo("Asia/Tehran"))
        return int(t.timestamp())

    def fetch(self):
        # Conservative verified-close policy: only completed calendar days.
        end_day = self.today - timedelta(days=1)
        start_day = end_day - timedelta(days=self.lookback - 1)
        params = {
            "api_key": self.key,
            "item": self.item,
            "start": self._unix_for_iran_day(start_day),
            "end": self._unix_for_iran_day(end_day, end=True),
        }
        url = f"{self.base}/ohlcSearch/?{urlencode(params)}"
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "trout-farm-fx/1.1"})
        with urlopen(req, timeout=12) as resp:
            payload = json.loads(resp.read().decode("utf-8"))

        if isinstance(payload, dict):
            # Be tolerant of wrappers such as {"data": [...]} while rejecting errors.
            if isinstance(payload.get("data"), list):
                payload = payload["data"]
            elif all(k in payload for k in ("timestamp", "close")):
                payload = [payload]
            else:
                raise ValueError("Unexpected Navasan OHLC response")
        if not isinstance(payload, list):
            raise ValueError("Unexpected Navasan OHLC response")

        fetched = _utcnow()
        out = []
        for row in payload:
            if not isinstance(row, dict) or row.get("close") in (None, ""):
                continue
            ts = int(float(row["timestamp"]))
            dg = datetime.fromtimestamp(ts, ZoneInfo("Asia/Tehran")).date()
            # Never accept current/future day as a verified closing observation.
            if dg >= self.today:
                continue
            close = float(str(row["close"]).replace(",", "")) / self.divisor
            out.append(FXObservation(dg.isoformat(), close, self.name, fetched, verified=True))

        # Deduplicate by Gregorian date and keep the last returned close for each day.
        dedup = {o.date_g: o for o in out}
        return [dedup[d] for d in sorted(dedup)]


class FXService:
    def __init__(self, db, A, today: date | None = None):
        self.db, self.A = db, A
        self.today = today or date.today()

    def _validate(self, obs: FXObservation):
        if obs.close_toman <= 0:
            raise ValueError("FX close must be positive")
        try:
            od = date.fromisoformat(obs.date_g)
        except Exception as e:
            raise ValueError("FX observation date must be YYYY-MM-DD") from e
        if od > self.today:
            raise ValueError("FX observation cannot be in the future")
        if obs.close_toman < 1_000 or obs.close_toman > 10_000_000:
            raise ValueError("USD/Toman close is outside sanity bounds")
        return obs

    def refresh_live(self, force=False):
        last = self.db.meta_get("fx_live_attempt_date")
        if not force and last == self.today.isoformat():
            return {"ok": True, "skipped": True, "reason": "already_attempted_today"}
        self.db.meta_set("fx_live_attempt_date", self.today.isoformat())
        try:
            p = NavasanAPIProvider(today=self.today)
            observations = [self._validate(o) for o in p.fetch()]
            if not observations:
                raise ValueError("Navasan returned no completed daily close in lookback window")
            written = 0
            for obs in observations:
                written += int(bool(self.db.fx_upsert(obs.asdict(), p.name, p.priority)))
            latest = observations[-1]
            self.db.meta_set("fx_live_last_error", "")
            return {
                "ok": True,
                "provider": p.name,
                "written": written,
                "observation": latest.asdict(),
            }
        except Exception as e:
            self.db.meta_set("fx_live_last_error", str(e))
            return {"ok": False, "provider": "navasan-api", "error": str(e)}

    def add_manual(self, obs_date: str, close_toman: float, verified=True):
        obs = self._validate(FXObservation(str(obs_date)[:10], float(close_toman),
                                           "manual", _utcnow(), bool(verified)))
        written = self.db.fx_upsert(obs.asdict(), "manual", priority=10)
        return {"ok": True, "written": written, "observation": obs.asdict()}

    def status(self):
        self.db.ex("UPDATE fx_daily SET stale=CASE WHEN date_g < ? THEN 1 ELSE 0 END",
                   (self.today.isoformat(),))
        rows = self.db.fx_series()
        verified = [r for r in rows if bool(r.get("verified"))]
        latest = verified[-1] if verified else (rows[-1] if rows else None)
        if not latest:
            return {"available": False, "fresh": False, "stale": True,
                    "status": "Unavailable", "last_error": self.db.meta_get("fx_live_last_error")}
        ld = date.fromisoformat(latest["date_g"])
        age = max(0, (self.today - ld).days)
        latest = dict(latest)
        latest["age_days"] = age
        latest["stale"] = age > 0
        latest["fresh"] = age == 0
        latest["status"] = "Fresh" if age == 0 else "Stale"
        prev = next((r for r in reversed(verified[:-1]) if r["date_g"] < latest["date_g"]), None)
        latest["daily_change"] = ((latest["close_toman"] / prev["close_toman"] - 1) if prev and prev["close_toman"] else None)
        qmonth = ((self.today.month - 1)//3)*3 + 1
        qstart = date(self.today.year, qmonth, 1).isoformat()
        ystart = date(self.today.year, 1, 1).isoformat()

        def first_from(d):
            return next((r for r in verified if r["date_g"] >= d), None)

        q0, y0 = first_from(qstart), first_from(ystart)
        latest["change_since_quarter_start"] = ((latest["close_toman"]/q0["close_toman"]-1) if q0 and q0["close_toman"] else None)
        latest["change_since_year_start"] = ((latest["close_toman"]/y0["close_toman"]-1) if y0 and y0["close_toman"] else None)
        latest["last_error"] = self.db.meta_get("fx_live_last_error")
        return {"available": True, **latest}
