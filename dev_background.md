# doxatlas_data_api Development Background

## Project Context

DoxAtlas main service is deployed in Hong Kong. CN/HK news and EastMoney Guba collection works better from a mainland server because mainland financial sites are faster and less likely to block local mainland access. This project packages the CN/HK crawler as an independent mainland data service.

The Hong Kong DoxAtlas backend remains the source of truth for user tasks, analysis, LLM pipelines, and final Supabase persistence. The mainland service is only responsible for:

- accepting one-off CN/HK crawl tasks,
- running the existing CN/HK media and Guba crawler logic,
- preserving long-content filtering and raw-media content relevance labels,
- writing crawler output to local Postgres,
- returning each task result once to the Hong Kong backend.

## Source Boundaries

The crawler kernel lives under `src/cn_hk_collector`. It is intentionally independent from the DoxAtlas backend service and must not import:

- `backend.services.*`
- `backend.collectors.*`
- Supabase clients from the main service
- DoxAtlas LLM analysis modules

The API layer lives under `src/doxatlas_data_api` and wraps the crawler kernel with FastAPI.

## API Contract

See `API_PROTOCOL.md` for the detailed protocol.

Main flow:

1. Hong Kong backend creates a task through `POST /v1/crawl-tasks`.
2. Mainland server records the task in local Postgres `crawler_tasks`.
3. Background execution writes rows to `raw_media` and `raw_social`.
4. Hong Kong backend polls `GET /v1/crawl-tasks/{task_id}`.
5. Hong Kong backend calls `POST /v1/crawl-tasks/{task_id}/pull` once and inserts returned rows into DoxAtlas.

Hong Kong backend config:

```text
CN_HK_DATA_API_BASE_URL=http://<MAINLAND_PUBLIC_IP>:8028
CN_HK_DATA_API_TIMEOUT_SECONDS=30
CN_HK_DATA_API_MAX_WAIT_SECONDS=420
CN_HK_DATA_API_POLL_INTERVAL_SECONDS=3
```

Leave `CN_HK_DATA_API_BASE_URL` empty to disable the remote path and use local CN/HK crawlers only.

## Security Notes

- Default allowed client IP is `43.135.22.202`.
- Keep `DATA_API_ALLOWED_CLIENT_IPS=43.135.22.202` in production.
- Do not enable `DATA_API_DISABLE_IP_ALLOWLIST` in production.
- Only set `DATA_API_TRUST_PROXY_HEADERS=true` behind a trusted reverse proxy that strips spoofed forwarding headers.
- Firewall rules should also restrict inbound access to TCP `8028` from the Hong Kong server.

## Deployment Notes

Default public endpoint shape:

```text
http://<MAINLAND_PUBLIC_IP>:8028
```

Local compose startup:

```powershell
docker compose up -d postgres api
docker compose run --rm collector refresh-tickers --market cn hk
```

The API initializes schema on startup when `DATA_API_INIT_DB_ON_STARTUP=true`.

## Development Notes

- Keep crawler output compatible with DoxAtlas `raw_media` and `raw_social` tables.
- Preserve `is_content_relevant` and `content_relevance_reason` fields.
- Preserve long-content filtering:
  - media content over 5000 chars is marked irrelevant,
  - social content over 250 chars is marked irrelevant.
- Preserve EastMoney Guba proxy scheduling in production proxy mode:
  - `GUBA_PROXY_MODE=api_pool` must use the original SmartBatch scheduler,
  - SmartBatch shape is 8 URL pools x 8 worker threads per pool,
  - each pool keeps 4 proxy slots and a 15-IP budget,
  - `GUBA_PROXY_MAX_PER_TASK` applies to the single `ProxyManager` list/direct path, not to SmartBatch's per-pool detail budget,
  - `direct`/`off` modes are allowed only to keep local standalone runs working without a proxy API.
- The pull endpoint must remain one-time. This prevents accidental duplicate ingestion by the Hong Kong backend.
- Main-service US data collection must not be routed through this API.
