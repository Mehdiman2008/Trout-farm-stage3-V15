# Railway deployment — Trout Farm Glass UI v15

This package is GitHub-ready: upload the **contents of this folder directly to the root of your GitHub repository**.

## 1) Railway service

1. Push/upload this project to GitHub.
2. In Railway choose **New Project → Deploy from GitHub Repo**.
3. Railway/Nixpacks will install `requirements.txt` and run `python app.py`.
4. `PORT` is supplied automatically by Railway; do not set it manually.

## 2) Persistent database (strongly recommended)

Create a Railway Volume and mount it at:

```text
/data
```

Then add this Railway Variable:

```text
DATABASE_PATH=/data/farm.db
```

Without a persistent volume, SQLite data can be lost when the container is replaced/redeployed.

## 3) Navasan API variables

Add these Railway Variables:

```text
NAVASAN_API_KEY=YOUR_REAL_NAVASAN_API_KEY
NAVASAN_API_BASE_URL=https://api.navasan.tech
NAVASAN_ITEM=usd_sell
NAVASAN_LOOKBACK_DAYS=10
NAVASAN_VALUE_DIVISOR=1
```

`usd_sell` is the Navasan code for Tehran USD sell. The application requests completed daily OHLC observations and stores only completed prior-day closes as verified closes. Historical TGJU Excel data remains available as fallback/backfill.

Do **not** commit the real API key to GitHub.

### About `NAVASAN_VALUE_DIVISOR`

Keep it at `1` if the returned `usd_sell` value matches the Toman value expected by the dashboard. If your Navasan plan/account returns the rate in Rial, set it to `10` so the app stores Toman. Verify the first live value against Navasan after deployment.

## 4) Health check

Railway health check:

```text
/api/health
```

## 5) Free Navasan API usage

The application attempts one automatic live FX refresh per application day. This is intentionally conservative for API quotas. A manual refresh endpoint/UI may make additional requests.

## 6) Local run

```bash
python -m pip install -r requirements.txt
python app.py
```

Local default URL: `http://127.0.0.1:8000`.
