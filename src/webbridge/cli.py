"""CLI WebBridge: запуск API, вход в сервисы и токен для opencode.

Собственного чат-клиента нет. Всё, что нужно пользователю:
  webbridge serve            — поднять OpenAI-совместимый API
  webbridge login deepseek   — открыть браузер и войти в веб-чат
  webbridge token            — показать токен для opencode
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer

from webbridge.bridge.auth import save_token
from webbridge.config import AppConfig, ensure_home
from webbridge.errors import WebBridgeError
from webbridge.observability.logging import configure as configure_logging
from webbridge.observability.logging import get_logger

app = typer.Typer(
    help="WebBridge — локальный OpenAI-совместимый API поверх веб-чатов.",
    no_args_is_help=True,
)
log = get_logger("cli")


def _load_config(path: Path | None) -> AppConfig:
    ensure_home()
    try:
        return AppConfig.load(path)
    except FileNotFoundError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    except Exception as exc:  # pydantic/yaml
        typer.echo(f"error: invalid config: {exc}", err=True)
        raise typer.Exit(code=2) from exc


@app.command()
def serve(
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Путь к config.yaml (по умолчанию ~/.webbridge)."),
    ] = None,
    host: Annotated[str | None, typer.Option(help="Переопределить host.")] = None,
    port: Annotated[int | None, typer.Option(help="Переопределить port.")] = None,
    reload: Annotated[bool, typer.Option(help="Автоперезапуск при изменении кода.")] = False,
) -> None:
    """Запустить локальный OpenAI-совместимый API."""
    import uvicorn

    from webbridge.bridge.app import create_app

    cfg = _load_config(config)
    if host:
        cfg.bridge.host = host
    if port:
        cfg.bridge.port = port

    configure_logging()
    save_token()
    typer.echo(f"webbridge: http://{cfg.bridge.host}:{cfg.bridge.port}/v1")
    typer.echo("api token: hidden (webbridge token)")
    typer.echo("opencode provider: webbridge (см. opencode.json)")

    application = create_app(config=cfg)
    uvicorn.run(
        application,
        host=cfg.bridge.host,
        port=cfg.bridge.port,
        log_level="info",
        reload=reload,
    )


@app.command()
def login(
    service: Annotated[
        str,
        typer.Argument(help="Имя сервиса из config.yaml: deepseek, claude."),
    ],
    config: Annotated[Path | None, typer.Option("--config", "-c")] = None,
    timeout: Annotated[int, typer.Option(help="Сколько секунд ждать входа.")] = 600,
) -> None:
    """Открыть браузер и войти в веб-чат. Профиль сохраняется между запусками."""
    from webbridge.session.login import interactive_login

    cfg = _load_config(config)
    if service not in cfg.services:
        typer.echo(f"error: unknown service {service!r}", err=True)
        typer.echo(f"known: {', '.join(sorted(cfg.services))}", err=True)
        raise typer.Exit(code=2)

    svc = cfg.services[service]
    configure_logging()
    typer.echo(f"opening {svc.base_url} — войдите в аккаунт в открывшемся окне")
    try:
        ok = asyncio.run(interactive_login(service, svc, timeout_s=timeout))
    except WebBridgeError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if not ok:
        typer.echo("login not confirmed (timeout)", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"login ok: {service}")


@app.command()
def token(
    rotate: Annotated[bool, typer.Option("--rotate", help="Сгенерировать новый токен.")] = False,
) -> None:
    """Показать (или перевыпустить) bearer-токен для opencode."""
    ensure_home()
    value = save_token(force=rotate)
    typer.echo(value)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
