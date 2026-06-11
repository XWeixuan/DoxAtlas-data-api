from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List
from zoneinfo import ZoneInfo


DEFAULT_MARKET = "cn"
SUPPORTED_MARKETS: List[str] = ["cn", "hk"]


class MarketUnavailableError(Exception):
    """Raised when a recognised market has no active provider yet."""

    def __init__(self, market_id: str, label: str):
        self.market_id = market_id
        self.label = label
        super().__init__(f"Market '{market_id}' ({label}) data providers are not yet connected.")


@dataclass(frozen=True)
class MarketContext:
    id: str
    label: str
    timezone: str
    currency: str
    currency_symbol: str
    price_provider: str
    enabled: bool = True
    ticker_pattern: str = r"^[A-Z]{1,10}$"
    ticker_example: str = "TSLA"
    exchange_name: str = "NYSE / NASDAQ"

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


MARKET_REGISTRY = {
    "cn": MarketContext(
        id="cn",
        label="China A-Shares",
        timezone="Asia/Shanghai",
        currency="CNY",
        currency_symbol="\u00a5",
        price_provider="akshare",
        enabled=True,
        ticker_pattern=r"^[0-9]{6}$",
        ticker_example="600519",
        exchange_name="SSE / SZSE",
    ),
    "hk": MarketContext(
        id="hk",
        label="Hong Kong",
        timezone="Asia/Hong_Kong",
        currency="HKD",
        currency_symbol="HK$",
        price_provider="akshare",
        enabled=True,
        ticker_pattern=r"^[0-9]{4,5}$",
        ticker_example="0700",
        exchange_name="HKEX",
    ),
}


def normalize_market(market: str | None = None) -> str:
    value = str(market or DEFAULT_MARKET).strip().lower()
    if value not in MARKET_REGISTRY:
        return DEFAULT_MARKET
    return value


def get_market_context(market: str | None = None) -> MarketContext:
    """Return the context for an *enabled* market; raise otherwise."""
    market_id = normalize_market(market)
    ctx = MARKET_REGISTRY.get(market_id)
    if not ctx:
        raise ValueError(f"Unknown market: {market_id}")
    if not ctx.enabled:
        raise MarketUnavailableError(market_id, ctx.label)
    return ctx


def get_market_context_unchecked(market: str | None = None) -> MarketContext:
    """Return the context for any registered market, regardless of enabled state."""
    market_id = normalize_market(market)
    ctx = MARKET_REGISTRY.get(market_id)
    if not ctx:
        raise ValueError(f"Unknown market: {market_id}")
    return ctx


def is_market_enabled(market: str | None = None) -> bool:
    market_id = normalize_market(market)
    ctx = MARKET_REGISTRY.get(market_id)
    return bool(ctx and ctx.enabled)


def normalize_ticker(ticker: str, market: str | None = None) -> str:
    ctx = get_market_context(market)
    value = str(ticker or "").upper().strip()
    if not value:
        raise ValueError("ticker is required")
    if not re.match(ctx.ticker_pattern, value):
        raise ValueError(f"Invalid ticker '{value}' for market '{ctx.id}'. Expected pattern: {ctx.ticker_pattern}")
    return value
