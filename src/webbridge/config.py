"""Конфигурация WebBridge.

WebBridge — локальный OpenAI-совместимый endpoint для opencode. Конфиг
описывает веб-сервисы (DeepSeek, Claude), их браузерные профили, селекторы
и лимиты. Никакого собственного чат-клиента больше нет.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_DIR = Path.home() / ".webbridge"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.yaml"
PACKAGE_DIR = Path(__file__).resolve().parent
SELECTORS_DIR = PACKAGE_DIR / "adapters" / "selectors"


DEFAULT_CONFIG_YAML = """\
bridge:
  host: 127.0.0.1
  port: 8000

services:
  deepseek:
    enabled: true
    base_url: https://chat.deepseek.com/
    profile_dir: ~/.webbridge/profiles/deepseek
    browser:
      channel: null
      headless: false
    selectors_file: ~/.webbridge/selectors/deepseek.yaml
    models:
      ds-chat: "DeepSeek"
      ds-reasoner: "DeepSeek-R1"
    limits:
      max_message_chars: 100000000
      max_requests_per_hour: 100
      max_requests_per_day: 1000
      max_parallel_chats: 1
    tier2:
      enabled: false
    protocol_format: dsml

  claude:
    type: claude
    enabled: true
    base_url: https://claude.ai/new
    profile_dir: ~/.webbridge/profiles/claude
    selectors_file: ~/.webbridge/selectors/claude.yaml
    browser:
      channel: chrome
      headless: false
    models:
      claude-sonnet: "Claude Sonnet 4.5"
      claude-opus: "Claude Opus 4.5"
    context_window: 200000
    max_output_tokens: 8192
    limits:
      max_message_chars: 100000
      max_requests_per_hour: 25
      max_requests_per_day: 200
      max_parallel_chats: 1
      max_generation_s: 300
    protocol_format: xml

  chatGPT:
    type: chatGPT
    enabled: true
    base_url: https://chatgpt.com//new
    profile_dir: ~/.webbridge/profiles/chatGPT
    selectors_file: ~/.webbridge/selectors/chatGPT.yaml
    browser:
      channel: chrome
      headless: false
    models:
      chatGPT: "chatGPT"
    context_window: 200000
    max_output_tokens: 8192
    limits:
      max_message_chars: 100000
      max_requests_per_hour: 25
      max_requests_per_day: 200
      max_parallel_chats: 1
      max_generation_s: 300
    protocol_format: xml

routing:
  default_model: ds-chat
  fallbacks:
    ds-chat: [ds-reasoner]
"""


class BridgeConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    api_token_ref: str = "file:~/.webbridge/api_token"
    db_path: Path = Path.home() / ".webbridge" / "state.sqlite3"
    enable_docs: bool = False

    @field_validator("db_path", mode="after")
    @classmethod
    def _expand_db_path(cls, v: Path) -> Path:
        return Path(os.path.expanduser(str(v)))


class HumanizeConfig(BaseModel):
    min_delay_s: float = 1.2
    max_delay_s: float = 4.5
    typing_mode: str = "paste"
    between_requests_s: tuple[float, float] = (3.0, 9.0)


class BrowserConfig(BaseModel):
    channel: str | None = "chrome"
    headless: bool = False


class LimitsConfig(BaseModel):
    max_message_chars: int = 100000000
    max_requests_per_hour: int = 40
    max_requests_per_day: int = 250
    max_parallel_chats: int = 1
    max_generation_s: float = 300.0



class ServiceConfig(BaseModel):
    # deepseek | claude | generic
    type: str = "generic"
    display_name: str | None = None
    enabled: bool = True
    base_url: str
    profile_dir: Path
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    selectors_file: Path
    models: dict[str, str] = Field(default_factory=dict[str: Any])
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    protocol_format: str = "xml"
    context_window: int = 128000
    max_output_tokens: int = 8192
    # Если true, невозможность выбрать модель в UI — ошибка, а не warning.
    strict_model: bool = False

    @field_validator("profile_dir", "selectors_file", mode="after")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return Path(os.path.expanduser(str(v)))


class RoutingConfig(BaseModel):
    default_model: str = "deepseek-chat"
    fallbacks: dict[str, list[str]] = Field(default_factory=dict[str: Any])


class AppConfig(BaseModel):
    bridge: BridgeConfig = Field(default_factory=BridgeConfig)
    humanize: HumanizeConfig = Field(default_factory=HumanizeConfig)
    services: dict[str, ServiceConfig] = Field(default_factory=dict[str: Any])
    routing: RoutingConfig = Field(default_factory=RoutingConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> AppConfig:
        path = path or DEFAULT_CONFIG_PATH
        if not path.exists():
            raise FileNotFoundError(f"config not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)


def ensure_home(force_config: bool = False) -> Path:
    """Создаёт ~/.webbridge, конфиг и копии селекторов. Возвращает путь к конфигу."""
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for sub in ("profiles", "artifacts", "logs", "selectors"):
        (DEFAULT_CONFIG_DIR / sub).mkdir(exist_ok=True)

    if force_config or not DEFAULT_CONFIG_PATH.exists():
        DEFAULT_CONFIG_PATH.write_text(DEFAULT_CONFIG_YAML, encoding="utf-8")

    for name in ("deepseek", "claude", "generic"):
        src = SELECTORS_DIR / f"{name}.yaml"
        dst = DEFAULT_CONFIG_DIR / "selectors" / f"{name}.yaml"
        if src.exists() and (force_config or not dst.exists()):
            shutil.copyfile(src, dst)

    return DEFAULT_CONFIG_PATH


def load_selectors(service: ServiceConfig) -> dict[str, list[str]]:
    """Загружает YAML с селекторами.

    Если пользовательский файл отсутствует, используется встроенный набор
    для типа сервиса. Это делает первый запуск рабочим без ручного копирования.
    """
    candidates = [service.selectors_file]
    if service.type:
        candidates.append(SELECTORS_DIR / f"{service.type}.yaml")
    candidates.append(SELECTORS_DIR / "generic.yaml")

    raw: str | None = None
    for path in candidates:
        try:
            raw = Path(path).read_text(encoding="utf-8")
            break
        except FileNotFoundError:
            continue
        except OSError:
            continue

    if raw is None:
        return {}

    data = yaml.safe_load(raw) or {}
    return {
        key: value if isinstance(value, list) else [value]
        for key, value in data.items()
        if key != "version"
    }


def model_limits(service: ServiceConfig) -> dict[str, Any]:
    return {
        "context": service.context_window,
        "output": service.max_output_tokens,
    }
