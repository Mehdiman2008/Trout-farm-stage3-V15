# Railway deployment — Version 17

This repository is ready to deploy directly from GitHub to Railway.

## 1. Upload to GitHub
Extract the ZIP and upload **the contents** directly to the repository root. `app.py`, `requirements.txt`, `railway.json`, and `Procfile` should all be at repo root.

## 2. Railway variables
Set these service variables:

```text
DATABASE_PATH=/data/farm.db
NAVASAN_API_KEY=YOUR_REAL_KEY
NAVASAN_API_BASE_URL=https://api.navasan.tech
NAVASAN_ITEM=usd_sell
NAVASAN_LOOKBACK_DAYS=10
NAVASAN_VALUE_DIVISOR=1
```

Do not set `PORT`; Railway injects it automatically. The app listens on Railway's `PORT` and `0.0.0.0`.

## 3. Persistent database
Create a Railway Volume and mount it at:

```text
/data
```

This keeps `farm.db` across redeploys/restarts.

## 4. Navasan
The application already contains a Navasan OHLC provider. The API key is read only from `NAVASAN_API_KEY`. If live fetch fails, the application records the error and retains historical/manual FX data as fallback.

Current default item: `usd_sell`.

## 5. Health check
Railway health check path:

```text
/api/health
```

Expected response includes `"ok": true`.
