"""Загрузка конфигурации (раздел 6 PROJECT.md)."""
from __future__ import annotations
import os

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_DIR = Path.home() / '.webbridge'
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / 'config.yaml'


class BridgeConfig(BaseModel):
    host: str = '127.0.0.1'
    port: int = 8000
    api_token_ref: str = 'keyring:webbridge/api_token'


class WorkspaceConfig(BaseModel):
    root: Path = Path.home() / 'projects' / 'current'
    dangerous_commands: str = 'confirm'


class HumanizeConfig(BaseModel):
    min_delay_s: float = 1.2
    max_delay_s: float = 4.5
    typing_mode: str = 'paste'
    between_requests_s: tuple[float, float] = (3.0, 9.0)


class BrowserConfig(BaseModel):
    channel: str | None = 'chrome'
    headless: bool = False


class LimitsConfig(BaseModel):
    max_message_chars: int = 60000
    max_requests_per_hour: int = 40
    max_requests_per_day: int = 250
    max_parallel_chats: int = 1


class Tier2Config(BaseModel):
    enabled: bool = False
    recon_notes: str | None = None


class ServiceConfig(BaseModel):
    enabled: bool = True
    base_url: str
    profile_dir: Path
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    selectors_file: Path
    models: dict[str, str] = Field(default_factory=dict)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    tier2: Tier2Config = Field(default_factory=Tier2Config)
    protocol_format: str = 'xml'

    @field_validator("profile_dir", "selectors_file", mode="after")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return Path(os.path.expanduser(str(v)))


class RoutingConfig(BaseModel):
    default_model: str
    fallbacks: dict[str, list[str]] = Field(default_factory=dict)


class AppConfig(BaseModel):
    bridge: BridgeConfig = Field(default_factory=BridgeConfig)
    workspace: WorkspaceConfig = Field(default_factory=WorkspaceConfig)
    humanize: HumanizeConfig = Field(default_factory=HumanizeConfig)
    services: dict[str, ServiceConfig] = Field(default_factory=dict)
    routing: RoutingConfig

    @field_validator("workspace", mode="after")
    @classmethod
    def _expand_workspace(cls, v: WorkspaceConfig) -> WorkspaceConfig:
        v.root = Path(os.path.expanduser(str(v.root)))
        return v

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        path = path or DEFAULT_CONFIG_PATH
        if not path.exists():
            raise FileNotFoundError(f"config not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)


def load_selectors(service: ServiceConfig) -> dict[str, list[str]]:
    """Загружает YAML с селекторами для сервиса.

    Отсутствие файла — не ошибка: возвращаем пустой dict, адаптер
    будет работать в no-op режиме (полезно в тестах и при первом
    запуске до `webbridge init --force`).
    """
    try:
        raw = service.selectors_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    data = yaml.safe_load(raw) or {}
    return {
        k: v if isinstance(v, list) else [v]
        for k, v in data.items()
        if k != "version"
    }