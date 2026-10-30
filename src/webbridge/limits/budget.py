"""Часовые и суточные бюджеты (раздел 8.8)."""
from __future__ import annotations

from datetime import datetime, timezone

from webbridge.config import LimitsConfig
from webbridge.conversation.store import Store
from webbridge.errors import RateLimited


def _hour_start() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")


def _day_start() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")


class BudgetManager:
    def __init__(self, store: Store):
        self._store = store

    async def check(self, service: str, limits: LimitsConfig) -> None:
        hour = await self._store.get_budget(service, _hour_start(), "hour")
        if hour >= limits.max_requests_per_hour:
            raise RateLimited(
                f"hourly budget exceeded for {service} ({hour}/{limits.max_requests_per_hour})"
            )
        day = await self._store.get_budget(service, _day_start(), "day")
        if day >= limits.max_requests_per_day:
            raise RateLimited(
                f"daily budget exceeded for {service} ({day}/{limits.max_requests_per_day})"
            )

    async def consume(self, service: str) -> None:
        await self._store.bump_budget(service, _hour_start(), "hour")
        await self._store.bump_budget(service, _day_start(), "day")

    async def status(self, service: str, limits: LimitsConfig) -> dict:
        return {
            "hour": {
                "used": await self._store.get_budget(service, _hour_start(), "hour"),
                "limit": limits.max_requests_per_hour,
            },
            "day": {
                "used": await self._store.get_budget(service, _day_start(), "day"),
                "limit": limits.max_requests_per_day,
            },
        }