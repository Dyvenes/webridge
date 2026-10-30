"""Роутинг model → (adapter, service_model) (раздел 3.3 п.2)."""
from __future__ import annotations

from typing import Any

from webbridge.config import AppConfig
from webbridge.errors import WebBridgeError


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
        return self._alias_map.get(alias)

    def route(self, model_alias: str) -> tuple[Any, str, str]:
        """Возвращает (adapter, service_name, service_model)."""
        chain = [model_alias] + list(self._config.routing.fallbacks.get(model_alias, []))
        for alias in chain:
            resolved = self._resolve(alias)
            if resolved is None:
                continue
            svc_name, service_model = resolved
            adapter = self._adapters.get(svc_name)
            if adapter is None:
                continue
            return adapter, svc_name, service_model
        raise WebBridgeError(
            f"no route for model {model_alias!r}; known aliases: {sorted(self._alias_map)}"
        )

    def list_models(self) -> list[str]:
        return sorted(self._alias_map.keys())