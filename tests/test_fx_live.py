from datetime import date

from core.assumptions import Assumptions
from core.db import DB
from core.fx_service import FXService


def make(tmp_path):
    db = DB(str(tmp_path / "farm.db"))
    A = Assumptions(db)
    return db, A


def test_stale_verified_close_is_not_presented_as_today(tmp_path):
    db, A = make(tmp_path)
    db.fx_upsert({"date_g": "2026-08-10", "close_toman": 100000,
                  "verified": True, "fetched_at": "2026-08-10T20:00:00+00:00"},
                 "TGJU-excel", 20)
    s = FXService(db, A, today=date(2026, 8, 11)).status()
    assert s["available"] is True
    assert s["date_g"] == "2026-08-10"
    assert s["fresh"] is False
    assert s["stale"] is True
    assert s["age_days"] == 1
    assert s["status"] == "Stale"


def test_provider_priority_api_over_excel_over_manual(tmp_path):
    db, A = make(tmp_path)
    day = "2026-08-10"
    db.fx_upsert({"date_g": day, "close_toman": 100000, "verified": True},
                 "TGJU-excel", 20)
    # Manual is fallback only and must not overwrite Excel.
    assert db.fx_upsert({"date_g": day, "close_toman": 90000, "verified": True},
                        "manual", 10) is False
    assert db.one("SELECT close_toman FROM fx_daily WHERE date_g=?", (day,))["close_toman"] == 100000
    # API has highest priority and replaces Excel for the same verified close date.
    assert db.fx_upsert({"date_g": day, "close_toman": 101000, "verified": True},
                        "navasan-api", 30) is True
    row = db.one("SELECT close_toman,source FROM fx_daily WHERE date_g=?", (day,))
    assert row["close_toman"] == 101000
    assert row["source"] == "navasan-api"


def test_live_metrics_daily_quarter_year_change(tmp_path):
    db, A = make(tmp_path)
    for d, v in [("2026-01-02", 80000), ("2026-07-01", 90000),
                 ("2026-08-12", 99000), ("2026-08-13", 100000)]:
        db.fx_upsert({"date_g": d, "close_toman": v, "verified": True}, "TGJU-excel", 20)
    s = FXService(db, A, today=date(2026, 8, 13)).status()
    assert s["fresh"] is True
    assert round(s["daily_change"], 6) == round(100000 / 99000 - 1, 6)
    assert round(s["change_since_quarter_start"], 6) == round(100000 / 90000 - 1, 6)
    assert round(s["change_since_year_start"], 6) == round(100000 / 80000 - 1, 6)


def test_navasan_provider_uses_completed_ohlc_days_only(monkeypatch):
    import json
    from datetime import datetime
    from zoneinfo import ZoneInfo
    import core.fx_service as fxs

    monkeypatch.setenv("NAVASAN_API_KEY", "test-secret")
    monkeypatch.setenv("NAVASAN_API_BASE_URL", "https://api.navasan.tech")

    def ts(y, m, d, hour=20):
        return int(datetime(y, m, d, hour, tzinfo=ZoneInfo("Asia/Tehran")).timestamp())

    payload = [
        {"timestamp": ts(2026, 8, 11), "close": "101000"},
        {"timestamp": ts(2026, 8, 12), "close": "102000"},
        # Even if an API unexpectedly returns an intraday/current-day row,
        # it must never be stored as a verified close.
        {"timestamp": ts(2026, 8, 13, 12), "close": "103000"},
    ]

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps(payload).encode()

    seen = {}
    def fake_urlopen(req, timeout=0):
        seen["url"] = req.full_url
        return Resp()

    monkeypatch.setattr(fxs, "urlopen", fake_urlopen)
    p = fxs.NavasanAPIProvider(today=date(2026, 8, 13))
    rows = p.fetch()
    assert [r.date_g for r in rows] == ["2026-08-11", "2026-08-12"]
    assert rows[-1].close_toman == 102000
    assert rows[-1].verified is True
    assert "ohlcSearch" in seen["url"]
    assert "item=usd_sell" in seen["url"]
    assert "api_key=test-secret" in seen["url"]


def test_refresh_navasan_overrides_excel_but_not_with_intraday(monkeypatch, tmp_path):
    import core.fx_service as fxs
    db, A = make(tmp_path)
    db.fx_upsert({"date_g": "2026-08-12", "close_toman": 100000, "verified": True},
                 "TGJU-excel", 20)

    monkeypatch.setenv("NAVASAN_API_KEY", "test-secret")
    monkeypatch.setattr(
        fxs.NavasanAPIProvider,
        "fetch",
        lambda self: [fxs.FXObservation("2026-08-12", 102000, "navasan-api", "now", True)],
    )
    r = fxs.FXService(db, A, today=date(2026, 8, 13)).refresh_live(force=True)
    assert r["ok"] is True
    row = db.one("SELECT close_toman,source,verified FROM fx_daily WHERE date_g=?", ("2026-08-12",))
    assert row["close_toman"] == 102000
    assert row["source"] == "navasan-api"
    assert row["verified"] == 1
