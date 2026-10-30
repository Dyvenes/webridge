"""Человекоподобные паузы (раздел 8.8)."""
from __future__ import annotations

import asyncio
import random

from webbridge.config import HumanizeConfig


def _delay(cfg: HumanizeConfig) -> float:
    """Случайная задержка с «хвостом»: чаще ближе к min, изредка к max."""
    lo, hi = cfg.min_delay_s, cfg.max_delay_s
    if hi <= lo:
        return max(0.0, lo)
    return lo + (hi - lo) * (random.random() ** 2)


async def pre_send(cfg: HumanizeConfig) -> None:
    await asyncio.sleep(_delay(cfg))


async def between_requests(cfg: HumanizeConfig) -> None:
    lo, hi = cfg.between_requests_s
    if hi <= lo:
        await asyncio.sleep(max(0.0, lo))
    else:
        await asyncio.sleep(random.uniform(lo, hi))


async def backoff(attempt: int) -> None:
    """Экспоненциальный backoff с джиттером; attempt начинается с 1."""
    base = min(30.0, 2.0 ** max(0, attempt))
    await asyncio.sleep(base + random.uniform(0.0, base * 0.5))