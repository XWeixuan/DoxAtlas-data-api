# DoxAtlas Data API

Standalone mainland data service for CN/HK news and EastMoney Guba collection. It exposes a fixed-port API for the Hong Kong DoxAtlas backend, writes to local Postgres, and does not import or run the DoxAtlas backend service.

## What It Contains

- CN/HK news crawler copied from the current DoxAtlas `cn_hk_media_client` flow.
- EastMoney Guba crawler copied from the current DoxAtlas `guba_client` flow.
- Long-content filtering for `raw_media` and `raw_social`.
- Raw-media content relevance labeling for CN/HK ticker analysis.
- Local Postgres schema for `ticker_entities`, `raw_media`, and `raw_social`.
- Local Postgres `crawler_tasks` for API task state and one-time result pulls.
- FastAPI service for task creation, status query, and one-time result pull.
- CLI commands for initialization, ticker entity import, collection, and smoke runs.

## Setup

```powershell
cd doxatlas_data_api
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a local database and configure `.env` from `.env.example`:

```powershell
createdb doxatlas_collector
cn-hk-collector init-db
cn-hk-collector refresh-tickers --market cn hk
```

Run the API service:

```powershell
doxatlas-data-api
```

Default API URL shape:

```text
http://<MAINLAND_PUBLIC_IP>:8028
```

Only `43.135.22.202` is allowed by default. See `API_PROTOCOL.md` for the full protocol.

## Run Collection

Collect one A-share ticker:

```powershell
cn-hk-collector collect --market cn --ticker 600519 --lookback-days 1
```

Collect one HK ticker:

```powershell
cn-hk-collector collect --market hk --ticker 0700 --lookback-days 1
```

Run a two-market smoke check:

```powershell
cn-hk-collector smoke --cn-ticker 600519 --hk-ticker 0700 --lookback-days 1 --require-rows
```

## Proxy Mode

The copied Guba crawler can run in direct mode or through an API-backed proxy pool.

```powershell
$env:GUBA_PROXY_MODE="direct"
```

For proxy-pool deployment:

```powershell
$env:GUBA_PROXY_MODE="api_pool"
$env:GUBA_PROXY_API_URL="https://your-proxy-provider.example/api"
$env:GUBA_PROXY_MAX_PER_TASK="3"
```

## Docker

```powershell
docker build -t doxatlas-data-api .
docker run --rm --env-file .env -p 8028:8028 doxatlas-data-api
```

For a self-contained server deployment with Postgres:

```powershell
docker compose up -d postgres api
docker compose run --rm collector init-db
docker compose run --rm collector refresh-tickers --market cn hk
docker compose run --rm collector smoke --cn-ticker 600519 --hk-ticker 0700 --lookback-days 1 --require-rows
```

## Notes

- The project is intentionally limited to collection, raw-table persistence, and API handoff.
- It does not call DoxAtlas task orchestration, Supabase clients, frontend APIs, or LLM analysis code.
- `raw_media.is_content_relevant=false` is used for long media and media classified as irrelevant.
- `raw_social.is_content_relevant=false` is used for long Guba posts.
