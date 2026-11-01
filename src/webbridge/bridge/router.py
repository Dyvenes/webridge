"""Роутинг model alias → (adapter, service, service_model)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from webbridge.config import AppConfig
from webbridge.errors import ModelNotFound


@dataclass
class Route:
    adapter: Any
    service_name: str
    service_model: str
    alias: str


class Router:
    def __init__(self, config: AppConfig, adapters: dict[str, Any]):
        self._config = config
        self._adapters = adapters
        self._alias_map: dict[str, tuple[str, str]] = {}
        for svc_name, svc in config.services.items():
            if not svc.enabled:
                continue
            for alias, service_model in svc.models.items():
                self._alias_map[alias] = (svc_name, service_model)

    def _resolve(self, alias: str) -> tuple[str, str] | None:
        resolved = self._alias_map.get(alias)
        if resolved is not None:
            return resolved
        # Допускаем форму "service:model" и "service/model".
        for sep in (":", "/"):
            if sep in alias:
                prefix, _, rest = alias.partition(sep)
                if rest and prefix in self._config.services:
                    return prefix, rest
        return None

    def route(self, model_alias: str) -> Route:
        chain = [model_alias] + list(self._config.routing.fallbacks.get(model_alias, []))
        for alias in chain:
            resolved = self._resolve(alias)
            if resolved is None:
                continue
            svc_name, service_model = resolved
            adapter = self._adapters.get(svc_name)
            if adapter is None:
                continue
            return Route(adapter, svc_name, service_model, alias)
        raise ModelNotFound(model_alias, self.list_models())

    def list_models(self) -> list[str]:
        return sorted(self._alias_map.keys())
