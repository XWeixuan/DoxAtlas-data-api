ALTER TABLE raw_social
  ADD COLUMN IF NOT EXISTS social_quality_score INTEGER,
  ADD COLUMN IF NOT EXISTS social_quality_tier TEXT,
  ADD COLUMN IF NOT EXISTS social_quality_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS social_body_quality TEXT,
  ADD COLUMN IF NOT EXISTS social_detail_required BOOLEAN,
  ADD COLUMN IF NOT EXISTS social_selected_for_analysis BOOLEAN,
  ADD COLUMN IF NOT EXISTS social_sampling_bucket TEXT,
  ADD COLUMN IF NOT EXISTS social_sampling_reason TEXT,
  ADD COLUMN IF NOT EXISTS social_content_chars INTEGER,
  ADD COLUMN IF NOT EXISTS social_read_count INTEGER,
  ADD COLUMN IF NOT EXISTS social_comment_count INTEGER,
  ADD COLUMN IF NOT EXISTS social_like_count INTEGER,
  ADD COLUMN IF NOT EXISTS social_forward_count INTEGER,
  ADD COLUMN IF NOT EXISTS social_has_image BOOLEAN,
  ADD COLUMN IF NOT EXISTS social_is_top BOOLEAN;

CREATE INDEX IF NOT EXISTS idx_raw_social_market_ticker_selected
  ON raw_social(market, ticker, social_selected_for_analysis)
  WHERE social_selected_for_analysis IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_raw_social_market_ticker_quality
  ON raw_social(market, ticker, social_quality_tier)
  WHERE social_quality_tier IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_raw_social_market_ticker_sampling_bucket
  ON raw_social(market, ticker, social_sampling_bucket)
  WHERE social_sampling_bucket IS NOT NULL;
