"""DeepSeek price table (USD per 1M tokens) and the peak/off-peak clock.

Source: https://api-docs.deepseek.com/quick_start/pricing, checked on CHECKED_ON. Off-peak rates are
half of peak. The docs also exclude Chinese public holidays from peak; that calendar is not modelled,
so a holiday is billed here at the (higher) peak rate and the daily cap errs on the safe side.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

CHECKED_ON = "2026-09-29"
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
KNOWN_MODELS = ("deepseek-flash", "deepseek-v4-pro")

# Peak = Monday-Friday, these [start, end) hours in UTC.
PEAK_HOURS_UTC = ((1, 4), (6, 10))


@dataclass(frozen=True)
class Rate:
    cache_hit: float
    cache_miss: float
    output: float


@dataclass(frozen=True)
class ModelPrice:
    model: str
    serves: str
    context: str
    peak: Rate
    off_peak: Rate
    checked_on: str = CHECKED_ON


PRICES: dict[str, ModelPrice] = {
    "deepseek-flash": ModelPrice("deepseek-flash", "DeepSeek-V4.1-Flash", "1M / 384K",
                                 peak=Rate(0.006, 0.30, 1.20), off_peak=Rate(0.003, 0.15, 0.60)),
    "deepseek-v4-pro": ModelPrice("deepseek-v4-pro", "DeepSeek-V4-Pro-0813", "1M / 384K",
                                  peak=Rate(0.044, 1.32, 3.96), off_peak=Rate(0.022, 0.66, 1.98)),
}

# Retired names the API still accepts and bills as Flash.
ALIASES = {"deepseek-v4-flash": "deepseek-flash", "deepseek-v4-flash-vision-exp": "deepseek-flash"}


def is_off_peak(now_utc: datetime | None = None) -> bool:
    now = now_utc or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    if now.weekday() >= 5:
        return True
    return not any(start <= now.hour < end for start, end in PEAK_HOURS_UTC)


def price_for(model: str) -> ModelPrice:
    """Unknown models are priced as the most expensive known one so the cap never under-counts."""
    model = ALIASES.get(model, model)
    if model in PRICES:
        return PRICES[model]
    return max(PRICES.values(), key=lambda p: p.peak.output)


def rate_for(model: str, at: datetime | None = None) -> Rate:
    p = price_for(model)
    return p.off_peak if is_off_peak(at) else p.peak
