"""Универсальный браузерный адаптер, настраиваемый YAML-селекторами.

Конкретные сайты пока подключаются через этот адаптер + отдельный YAML.
При необходимости специализации — наследоваться от BrowserChatAdapter
и переопределять методы (см. раздел 8.1).
"""
from __future__ import annotations

from .browser_base import BrowserChatAdapter

GenericBrowserAdapter = BrowserChatAdapter