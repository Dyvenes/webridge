"""CLI (раздел 12 PROJECT.md)."""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import typer

from webbridge.config import DEFAULT_CONFIG_DIR, DEFAULT_CONFIG_PATH, AppConfig
from webbridge.errors import WebBridgeError  # noqa: F401  (используется в M1/M2)
from webbridge.observability.artifacts import list_artifacts
from webbridge.observability.logging import configure as configure_logging, get_logger

app = typer.Typer(help='WebBridge — локальный мост над веб-чатами.')
log = get_logger('cli')

_FALLBACK_CONFIG = """\
bridge:
  host: 127.0.0.1
  port: 8000

routing:
  default_model: default

services: {}
"""


def _config_or_die(path: Path | None) -> AppConfig:
    try:
        return AppConfig.load(path)
    except FileNotFoundError as e:
        typer.echo(f"error: {e}", err=True)
        typer.echo("run `webbridge init` first", err=True)
        raise typer.Exit(code=2)

@app.command()
def init(force: bool = typer.Option(False, help="Перезаписать существующий конфиг")):
    """Создать ~/.webbridge, пример конфига, селекторы и токен API."""
    configure_logging()
    DEFAULT_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for sub in ("profiles", "artifacts", "logs", "selectors"):
        (DEFAULT_CONFIG_DIR / sub).mkdir(exist_ok=True)

    if DEFAULT_CONFIG_PATH.exists() and not force:
        typer.echo(f"config exists: {DEFAULT_CONFIG_PATH} (use --force to overwrite)")
    else:
        DEFAULT_CONFIG_PATH.write_text(_FALLBACK_CONFIG, encoding="utf-8")
        typer.echo(f"created {DEFAULT_CONFIG_PATH}")

    from webbridge.bridge.auth import save_token
    token = save_token()
    typer.echo(f"api token saved (…{token[-6:]}); use `webbridge token` to print")
    typer.echo("next: edit config, add a service, then `webbridge login <service>`")


@app.command()
def token():
    """Показать локальный API-токен."""
    from webbridge.bridge.auth import _load_token
    t = _load_token()
    if not t:
        typer.echo("no token; run `webbridge init`", err=True)
        raise typer.Exit(code=2)
    typer.echo(t)


@app.command()
def serve(
    config: Path = typer.Option(None, help="Путь к config.yaml"),
    log_level: str = typer.Option("info"),
):
    """Запустить мост."""
    configure_logging(level=getattr(logging, log_level.upper(), logging.INFO))
    cfg = _config_or_die(config)
    from webbridge.bridge.app import run
    run(cfg)


@app.command()
def login(service: str):
    """Открыть браузер для ручного входа в сервис."""
    import asyncio

    cfg = _config_or_die(None)
    svc = cfg.services.get(service)
    if not svc:
        typer.echo(f"unknown service: {service}", err=True)
        raise typer.Exit(code=2)
    from webbridge.session.login import interactive_login

    ok = asyncio.run(interactive_login(service, svc))
    if ok:
        typer.echo(f"OK: {service} logged in")
    else:
        typer.echo(f"FAILED: {service} not logged in", err=True)
        raise typer.Exit(code=1)

@app.command()
def doctor(service: str = typer.Argument(None)):
    """Проверить сессию и селекторы."""
    import asyncio

    cfg = _config_or_die(None)
    services = (
        [service]
        if service
        else [s for s, c in cfg.services.items() if c.enabled]
    )
    rc = asyncio.run(_doctor(cfg, services))
    raise typer.Exit(code=rc)

async def _doctor(cfg, services: list[str]) -> int:
    from webbridge.config import load_selectors
    from webbridge.session.manager import SessionManager

    sm = SessionManager()
    rc = 0
    try:
        for name in services:
            svc = cfg.services.get(name)
            if not svc:
                typer.echo(f"✗ {name}: unknown service", err=True)
                rc = 2
                continue
            sel = load_selectors(svc)
            session = sm.get(name, svc)
            try:
                page = await session.get_page()
                await page.goto(svc.base_url, wait_until="domcontentloaded")
                status = await session.check_health(page, sel)
                marker = "✓" if status == "OK" else "✗"
                typer.echo(f"{marker} {name}: {status}")
                if status != "OK":
                    rc = 1
            except Exception as e:
                typer.echo(f"✗ {name}: {type(e).__name__}: {e}", err=True)
                rc = 1
    finally:
        await sm.close_all()
    return rc

@app.command()
def chat(service: str, model: str = typer.Option(None, help="alias модели")):
    """Интерактивный чат в терминале (отладка адаптера)."""
    import asyncio

    cfg = _config_or_die(None)
    asyncio.run(_chat(cfg, service, model))


async def _chat(cfg, service: str, model: str | None) -> None:
    from webbridge.adapters.browser_base import BrowserChatAdapter
    from webbridge.session.manager import SessionManager

    svc = cfg.services.get(service)
    if not svc:
        typer.echo(f"unknown service: {service}", err=True)
        return

    sm = SessionManager()
    session = sm.get(service, svc)
    adapter = BrowserChatAdapter(service, svc, session, cfg.humanize)
    try:
        status = await adapter.ensure_session()
        if status != "OK":
            typer.echo(f"session: {status} (run `webbridge login {service}`)", err=True)
            return

        service_model = model or (next(iter(svc.models.values())) if svc.models else None)
        handle = await adapter.new_chat(service_model)
        typer.echo(f"chat: {handle.chat_url}  (Ctrl-D to exit)")

        while True:
            try:
                line = typer.prompt("you")
            except (EOFError, typer.Abort, KeyboardInterrupt):
                break
            if not line.strip():
                continue
            try:
                log.warning("DEBUG: before send")

                await adapter.send(handle, line)

                log.warning("DEBUG: after send")

                page = await session.get_page()

                log.warning(
                    "DEBUG: page state",
                    url=page.url,
                    title=await page.title(),
                )

                count, text = await adapter._assistant_state(page)

                log.warning(
                    "DEBUG: assistant state",
                    count=count,
                    text_length=len(text),
                    text_preview=text[:300],
                )

                reply = await adapter.wait_done(handle, timeout_s=300.0)

                log.warning(
                    "DEBUG: wait_done returned",
                    finished=reply.finished,
                    text_length=len(reply.text),
                    text_preview=reply.text[:300],
                )

                typer.echo(f"model: {reply.text}")
                if not reply.finished:
                    typer.echo("(generation timeout)", err=True)
            except Exception as e:
                typer.echo(f"error: {type(e).__name__}: {e}", err=True)
    finally:
        await adapter.close()


@app.command()
def probe(service: str):
    """Показать найденные селекторы."""
    import asyncio

    cfg = _config_or_die(None)
    svc = cfg.services.get(service)
    if not svc:
        typer.echo(f"unknown service: {service}", err=True)
        raise typer.Exit(code=2)

    from webbridge.config import load_selectors
    from webbridge.session.manager import SessionManager

    sel = load_selectors(svc)
    sm = SessionManager()
    session = sm.get(service, svc)

    async def _probe():
        try:
            page = await session.get_page()
            await page.goto(svc.base_url, wait_until="domcontentloaded")
            for key, candidates in sel.items():
                found = None
                for c in candidates:
                    try:
                        if await page.locator(c).first.is_visible():
                            found = c
                            break
                    except Exception:
                        continue
                typer.echo(f"{'✓' if found else '✗'} {key}: {found or '(none)'}")
        finally:
            await sm.close_all()

    asyncio.run(_probe())


@app.command()
def record(service: str):
    """Запись HAR для разведки tier 2 (заготовка)."""
    typer.echo('`record` is implemented in milestone M7.', err=True)
    raise typer.Exit(2)


@app.command()
def budget():
    """Остаток лимитов по сервисам."""
    typer.echo('`budget` is implemented in milestone M5.', err=True)
    raise typer.Exit(2)


@app.command()
def artifacts(last: bool = typer.Option(False, help='Только последний')):
    """Показать артефакты сбоев."""
    items = list_artifacts()
    if not items:
        typer.echo('No artifacts.')
        return
    if last:
        items = items[:1]
    for p in items:
        typer.echo(str(p))