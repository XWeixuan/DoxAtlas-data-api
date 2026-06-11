from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class CrawlTaskCreate(BaseModel):
    market: Literal["cn", "hk"]
    ticker: str = Field(min_length=1)
    lookback_days: int = Field(default=7, ge=1, le=30)
    window_start: datetime | None = None
    window_end: datetime | None = None
    collect_media: bool = True
    collect_social: bool = True


class CrawlTaskSummary(BaseModel):
    task_id: str
    market: str
    ticker: str
    status: str
    lookback_days: int
    window_start: datetime | None = None
    window_end: datetime | None = None
    collect_media: bool
    collect_social: bool
    media_fetched: int = 0
    media_written: int = 0
    social_fetched: int = 0
    social_written: int = 0
    media_relevant: int = 0
    media_irrelevant: int = 0
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    pulled_at: datetime | None = None


class CrawlTaskCreateResponse(BaseModel):
    task_id: str
    status: str
    status_url: str
    pull_url: str


class CrawlTaskPullResponse(BaseModel):
    task: CrawlTaskSummary
    raw_media: list[dict[str, Any]]
    raw_social: list[dict[str, Any]]
