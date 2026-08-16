# Railway deployment

This package is GitHub-root ready. Upload the extracted contents directly to the repository root.

## Railway Variables

```text
DATABASE_PATH=/data/farm.db
NAVASAN_API_KEY=YOUR_KEY
NAVASAN_API_BASE_URL=https://api.navasan.tech
NAVASAN_ITEM=usd_sell
NAVASAN_LOOKBACK_DAYS=10
NAVASAN_VALUE_DIVISOR=1
```

Do not set `PORT`; Railway provides it automatically.

## Persistent database

Create a Railway Volume and mount it at:

```text
/data
```

The application will then keep SQLite at `/data/farm.db` across deployments.

## Navasan

The application already contains `core/fx_service.py`. It reads the API key only from environment variables, requests completed daily USD/Toman closes, stores verified observations in SQLite, and falls back to historical Excel/manual data if the live request fails.

## Health check

Railway health endpoint:

```text
/api/health
```
