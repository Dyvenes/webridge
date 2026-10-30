"""Сохранение артефактов при сбоях (раздел 8.2)."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

ARTIFACTS_ROOT = Path.home() / ".webbridge" / "artifacts"


def _safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:64] or "x"


def save_artifacts(
    service: str,
    kind: str,
    *,
    screenshot: bytes | None = None,
    html: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Path:
    ts = time.strftime("%Y%m%dT%H%M%S")
    out = ARTIFACTS_ROOT / f"{ts}_{_safe_name(service)}_{_safe_name(kind)}"
    out.mkdir(parents=True, exist_ok=True)
    if screenshot is not None:
        (out / "screenshot.png").write_bytes(screenshot)
    if html is not None:
        (out / "page.html").write_text(html, encoding="utf-8")
    if extra is not None:
        (out / "extra.json").write_text(
            json.dumps(extra, ensure_ascii=False, default=str, indent=2),
            encoding="utf-8",
        )
    return out


def list_artifacts() -> list[Path]:
    if not ARTIFACTS_ROOT.exists():
        return []
    return sorted((p for p in ARTIFACTS_ROOT.iterdir() if p.is_dir()), reverse=True)