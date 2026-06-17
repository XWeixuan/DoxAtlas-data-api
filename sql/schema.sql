CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS ticker_entities (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  market TEXT NOT NULL,
  ticker TEXT NOT NULL,
  company_name TEXT,
  short_name TEXT,
  english_name TEXT,
  exchange TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (market, ticker)
);

CREATE INDEX IF NOT EXISTS idx_ticker_entities_market_short_name
  ON ticker_entities(market, short_name);

CREATE INDEX IF NOT EXISTS idx_ticker_entities_market_company_name
  ON ticker_entities(market, company_name);

CREATE TABLE IF NOT EXISTS raw_media (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id TEXT,
  market TEXT NOT NULL,
  ticker TEXT NOT NULL,
  published_at TIMESTAMPTZ,
  source_type TEXT NOT NULL DEFAULT 'media',
  channel TEXT,
  source_name TEXT,
  title TEXT,
  summary TEXT,
  content TEXT,
  url TEXT NOT NULL,
  origin_keyword_type TEXT DEFAULT 'Base',
  is_content_relevant BOOLEAN,
  content_relevance_reason TEXT,
  duplicate_count INTEGER NOT NULL DEFAULT 1,
  duplicate_urls JSONB NOT NULL DEFAULT '[]'::jsonb,
  content_hash TEXT,
  simhash TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (market, ticker, url)
);

CREATE INDEX IF NOT EXISTS idx_raw_media_market_ticker_published_at
  ON raw_media(market, ticker, published_at DESC);

CREATE INDEX IF NOT EXISTS idx_raw_media_market_ticker_content_relevance
  ON raw_media(market, ticker, is_content_relevant)
  WHERE is_content_relevant IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_raw_media_market_ticker_content_hash
  ON raw_media(market, ticker, content_hash)
  WHERE content_hash IS NOT NULL AND content_hash <> '';

CREATE INDEX IF NOT EXISTS idx_raw_media_market_ticker_simhash
  ON raw_media(market, ticker, simhash)
  WHERE simhash IS NOT NULL AND simhash <> '';

CREATE TABLE IF NOT EXISTS raw_social (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id TEXT,
  market TEXT NOT NULL,
  ticker TEXT NOT NULL,
  published_at TIMESTAMPTZ,
  source_type TEXT NOT NULL DEFAULT 'social',
  channel TEXT,
  source_name TEXT,
  title TEXT,
  summary TEXT,
  content TEXT,
  url TEXT NOT NULL,
  origin_keyword_type TEXT DEFAULT 'Base',
  is_content_relevant BOOLEAN,
  content_relevance_reason TEXT,
  social_quality_score INTEGER,
  social_quality_tier TEXT,
  social_quality_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
  social_body_quality TEXT,
  social_detail_required BOOLEAN,
  social_selected_for_analysis BOOLEAN,
  social_sampling_bucket TEXT,
  social_sampling_reason TEXT,
  social_content_chars INTEGER,
  social_read_count INTEGER,
  social_comment_count INTEGER,
  social_like_count INTEGER,
  social_forward_count INTEGER,
  social_has_image BOOLEAN,
  social_is_top BOOLEAN,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (market, ticker, url)
);

CREATE INDEX IF NOT EXISTS idx_raw_social_market_ticker_published_at
  ON raw_social(market, ticker, published_at DESC);

CREATE INDEX IF NOT EXISTS idx_raw_social_market_ticker_content_relevance
  ON raw_social(market, ticker, is_content_relevant)
  WHERE is_content_relevant IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_raw_social_market_ticker_selected
  ON raw_social(market, ticker, social_selected_for_analysis)
  WHERE social_selected_for_analysis IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_raw_social_market_ticker_quality
  ON raw_social(market, ticker, social_quality_tier)
  WHERE social_quality_tier IS NOT NULL;

CREATE TABLE IF NOT EXISTS crawler_tasks (
  task_id TEXT PRIMARY KEY,
  market TEXT NOT NULL,
  ticker TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'queued',
  lookback_days INTEGER NOT NULL DEFAULT 7,
  window_start TIMESTAMPTZ,
  window_end TIMESTAMPTZ,
  collect_media BOOLEAN NOT NULL DEFAULT TRUE,
  collect_social BOOLEAN NOT NULL DEFAULT TRUE,
  media_fetched INTEGER NOT NULL DEFAULT 0,
  media_written INTEGER NOT NULL DEFAULT 0,
  social_fetched INTEGER NOT NULL DEFAULT 0,
  social_written INTEGER NOT NULL DEFAULT 0,
  media_relevant INTEGER NOT NULL DEFAULT 0,
  media_irrelevant INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  started_at TIMESTAMPTZ,
  finished_at TIMESTAMPTZ,
  pulled_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (status IN ('queued', 'running', 'succeeded', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_crawler_tasks_market_ticker_created_at
  ON crawler_tasks(market, ticker, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_crawler_tasks_status_created_at
  ON crawler_tasks(status, created_at DESC);
