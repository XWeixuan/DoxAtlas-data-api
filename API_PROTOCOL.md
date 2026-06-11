# DoxAtlas Data API Protocol

This API runs on the mainland crawler server and exposes CN/HK crawler data to the Hong Kong DoxAtlas backend.

## Network Contract

- Base URL: `http://<MAINLAND_PUBLIC_IP>:8028`
- Fixed port: `8028`
- Allowed client IP by default: `43.135.22.202`
- Default allowlist environment variable: `DATA_API_ALLOWED_CLIENT_IPS=43.135.22.202`
- If a reverse proxy is placed in front of the API, set `DATA_API_TRUST_PROXY_HEADERS=true` only after the proxy strips untrusted `X-Forwarded-For` headers.

The API intentionally has no public anonymous access surface beyond the IP allowlist.

## Status Values

- `queued`: task row exists but background crawler has not started.
- `running`: crawler is executing.
- `succeeded`: crawler finished and data is ready to pull once.
- `failed`: crawler failed; see `error`.

## Create Crawler Task

`POST /v1/crawl-tasks`

Request:

```json
{
  "market": "cn",
  "ticker": "600519",
  "lookback_days": 1,
  "window_start": null,
  "window_end": null,
  "collect_media": true,
  "collect_social": true
}
```

Rules:

- `market` must be `cn` or `hk`.
- CN ticker format is six digits, for example `600519`.
- HK ticker format is four or five digits, for example `0700`.
- If both `window_start` and `window_end` are supplied, `window_end` must be later.
- The server writes crawler output to local Postgres `raw_media` and `raw_social` with the created `task_id`.

Response `202`:

```json
{
  "task_id": "97a25b97-7b53-4afb-a313-39095601cf89",
  "status": "queued",
  "status_url": "/v1/crawl-tasks/97a25b97-7b53-4afb-a313-39095601cf89",
  "pull_url": "/v1/crawl-tasks/97a25b97-7b53-4afb-a313-39095601cf89/pull"
}
```

## Query Task Status

`GET /v1/crawl-tasks/{task_id}`

Response:

```json
{
  "task_id": "97a25b97-7b53-4afb-a313-39095601cf89",
  "market": "cn",
  "ticker": "600519",
  "status": "succeeded",
  "lookback_days": 1,
  "window_start": null,
  "window_end": null,
  "collect_media": true,
  "collect_social": true,
  "media_fetched": 93,
  "media_written": 93,
  "social_fetched": 334,
  "social_written": 334,
  "media_relevant": 89,
  "media_irrelevant": 4,
  "error": null,
  "created_at": "2026-06-11T12:00:00Z",
  "started_at": "2026-06-11T12:00:01Z",
  "finished_at": "2026-06-11T12:04:10Z",
  "pulled_at": null
}
```

## Pull Results Once

`POST /v1/crawl-tasks/{task_id}/pull`

This endpoint is intentionally mutating. The first successful call returns all records and sets `crawler_tasks.pulled_at`. A second call returns `409 task_already_pulled`.

Response:

```json
{
  "task": {
    "task_id": "97a25b97-7b53-4afb-a313-39095601cf89",
    "market": "cn",
    "ticker": "600519",
    "status": "succeeded",
    "lookback_days": 1,
    "collect_media": true,
    "collect_social": true,
    "media_fetched": 93,
    "media_written": 93,
    "social_fetched": 334,
    "social_written": 334,
    "media_relevant": 89,
    "media_irrelevant": 4
  },
  "raw_media": [
    {
      "market": "cn",
      "ticker": "600519",
      "source_type": "media",
      "channel": "eastmoney_news",
      "title": "...",
      "content": "...",
      "url": "https://...",
      "is_content_relevant": true,
      "content_relevance_reason": "target_in_title_and_repeated"
    }
  ],
  "raw_social": [
    {
      "market": "cn",
      "ticker": "600519",
      "source_type": "social",
      "channel": "guba",
      "title": "...",
      "content": "...",
      "url": "https://..."
    }
  ]
}
```

Errors:

- `404 task_not_found`: unknown task id.
- `409 task_not_succeeded`: task is still queued/running or failed.
- `409 task_already_pulled`: results were already pulled once.
- `403 client_ip_not_allowed`: caller IP is not in the allowlist.

## Hong Kong Backend Polling Flow

1. `POST /v1/crawl-tasks`
2. Poll `GET /v1/crawl-tasks/{task_id}` until `succeeded` or `failed`.
3. If `succeeded`, call `POST /v1/crawl-tasks/{task_id}/pull` exactly once.
4. Insert returned `raw_media` and `raw_social` into the DoxAtlas Supabase-backed raw tables.
5. If create/status/pull fails or times out, use the local DoxAtlas crawler fallback.

## Hong Kong Backend Environment Variables

The DoxAtlas backend uses the remote API only when `CN_HK_DATA_API_BASE_URL` is set.

```text
CN_HK_DATA_API_BASE_URL=http://<MAINLAND_PUBLIC_IP>:8028
CN_HK_DATA_API_TIMEOUT_SECONDS=30
CN_HK_DATA_API_MAX_WAIT_SECONDS=420
CN_HK_DATA_API_POLL_INTERVAL_SECONDS=3
```

If `CN_HK_DATA_API_BASE_URL` is empty or any remote step fails, CN/HK collection falls back to the local DoxAtlas crawler. US collection is unchanged.
