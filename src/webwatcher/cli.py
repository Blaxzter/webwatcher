"""Command line interface: run, check, list, history, reset, telegram helpers."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path

from webwatcher import __version__
from webwatcher.browser import Renderer
from webwatcher.config import (
    Config,
    ConfigError,
    SiteConfig,
    format_duration,
    load_config,
)
from webwatcher.notify.telegram import TelegramError, TelegramNotifier
from webwatcher.runner import Runner
from webwatcher.sites import SiteRepository
from webwatcher.store import Store, from_iso, utcnow

log = logging.getLogger("webwatcher")

DEFAULT_CONFIG = os.environ.get("WEBWATCHER_CONFIG", "config.yaml")


def setup_output_encoding() -> None:
    """Emoji must not crash the CLI on legacy code pages (Windows cp1252 pipes)."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def select_sites(config: Config, names: list[str] | None) -> list[SiteConfig]:
    if not names:
        return [site for site in config.sites if site.enabled]
    selected: list[SiteConfig] = []
    for name in names:
        site = config.get_site(name)
        if site is None:
            known = ", ".join(s.key for s in config.sites)
            raise SystemExit(f"Unbekannte Seite: {name!r}. Verfügbar: {known}")
        selected.append(site)
    return selected


def make_notifier(
    config: Config, required: bool, need_chat: bool = True
) -> TelegramNotifier | None:
    """`need_chat=False` for calls that only query the bot itself (chat-id)."""
    telegram = config.telegram
    if telegram.configured or (not need_chat and telegram.has_token):
        return TelegramNotifier(telegram)

    if not telegram.has_token:
        message = (
            "TELEGRAM_BOT_TOKEN fehlt - Bot bei @BotFather anlegen (/newbot) und den Token "
            "in die .env eintragen."
        )
    else:
        message = (
            "TELEGRAM_CHAT_ID fehlt - schreibe deinem Bot einmal in Telegram und ermittle "
            "sie dann mit: webwatcher chat-id"
        )
    if required:
        raise SystemExit(message)
    log.warning("%s Es werden keine Nachrichten verschickt.", message)
    return None


def open_repo(config: Config, store: Store) -> SiteRepository:
    """Die Seiten kommen aus der Datenbank, nicht mehr aus der config.yaml."""
    repo = SiteRepository(store, config.site_defaults, config.screenshot_dir)
    entries = repo.entries()
    if not entries and config.seed_sites:
        log.warning(
            "In %s stehen %d Seite(n), die noch nicht übernommen wurden. "
            "Einmalig ausführen: webwatcher import-config",
            config.source_path.name if config.source_path else "der Konfiguration",
            len(config.seed_sites),
        )
    config.replace_sites(repo.sites())
    return repo


@contextlib.contextmanager
def open_config(config: Config):
    """Store öffnen und die Seiten aus der Datenbank nachladen."""
    with Store(config.db_path) as store:
        yield store, open_repo(config, store)


@contextlib.asynccontextmanager
async def build_runner(config: Config, notify: bool):
    store = Store(config.db_path)
    repo = open_repo(config, store)
    notifier = make_notifier(config, required=False) if notify else None
    renderer = Renderer()
    try:
        await renderer.start()
        yield Runner(config, store, renderer, notifier, repo)
    finally:
        await renderer.close()
        if notifier:
            await notifier.aclose()
        store.close()


# -- commands ---------------------------------------------------------------


async def start_web(config: Config, runner: Runner):
    """Startet das Web-UI im selben Event-Loop; None, wenn es aus ist."""
    if not config.web.enabled:
        return None
    try:
        from webwatcher.web.server import serve
    except ImportError as exc:
        raise SystemExit(
            f"Das Web-UI braucht aiohttp ({exc}). Installieren mit:\n"
            "  pip install 'webwatcher[web]'"
        ) from None
    return await serve(config, runner.store, runner.repo, runner)


async def cmd_run(args: argparse.Namespace, config: Config) -> int:
    if args.web:
        config.web = replace(config.web, enabled=True)
    if args.web_port:
        config.web = replace(config.web, port=args.web_port, enabled=True)
    if args.web_host:
        config.web = replace(config.web, host=args.web_host, enabled=True)

    async with build_runner(config, notify=True) as runner:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, runner.request_stop)
            except (NotImplementedError, AttributeError):
                # Windows: fall back to KeyboardInterrupt handling below.
                pass

        web_runner = await start_web(config, runner)
        try:
            await runner.run_forever()
        except KeyboardInterrupt:
            runner.request_stop()
        finally:
            if web_runner is not None:
                await web_runner.cleanup()
    return 0


def cmd_import(args: argparse.Namespace, config: Config) -> int:
    """Seiten aus der config.yaml in die Datenbank übernehmen."""
    source = config.source_path
    mappings = config.seed_sites
    if not mappings:
        print(f"In {source} steht kein 'sites:'-Block – nichts zu übernehmen.")
        return 0

    with Store(config.db_path) as store:
        repo = SiteRepository(store, config.site_defaults, config.screenshot_dir)
        added, skipped = repo.import_mappings(mappings, actor="import-config")

    for key in added:
        print(f"übernommen: {key}")
    for label, reason in skipped:
        print(f"übersprungen: {label} ({reason})")
    print(f"\n{len(added)} übernommen, {len(skipped)} übersprungen.")
    if added:
        print(
            "Die Seiten liegen jetzt in der Datenbank und werden dort gepflegt.\n"
            f"Der 'sites:'-Block in {source.name} wird nicht mehr gelesen und kann weg."
        )
    return 0


NO_SITES_HINT = (
    "Es sind noch keine Seiten eingerichtet. Eine anlegen:\n"
    "  im Web-UI (webwatcher run --web) unter 'Neue Seite', oder\n"
    "  webwatcher pick https://die-seite-die-du-beobachten-willst.de\n"
    "Aus einer bestehenden config.yaml übernehmen: webwatcher import-config"
)


async def cmd_check(args: argparse.Namespace, config: Config) -> int:
    async with build_runner(config, notify=not args.no_notify) as runner:
        # Erst hier stehen die Seiten aus der Datenbank in der Config.
        sites = select_sites(config, args.site)
        if not sites:
            print(NO_SITES_HINT)
            return 0
        outcomes = await runner.check_once(sites, notify=not args.no_notify)

    failed = 0
    for outcome in outcomes:
        marker = {"changed": "🔔", "error": "⚠️ ", "baseline": "👀", "unchanged": "  "}.get(
            outcome.status, "  "
        )
        print(f"{marker} {outcome.site.name}: {outcome.describe()}")
        if outcome.diff and outcome.diff.text and args.show_diff:
            print("\n".join(f"    {line}" for line in outcome.diff.text.splitlines()))
        if outcome.status == "error":
            failed += 1
    return 1 if failed else 0


def cmd_list(args: argparse.Namespace, config: Config) -> int:
    now = utcnow()
    with open_config(config) as (store, repo):
        broken = [entry for entry in repo.entries() if not entry.valid]
        if not config.sites and not broken:
            print(NO_SITES_HINT)
            return 0

        print(f"{'KEY':<24} {'INTERVAL':>9}  {'LETZTER CHECK':<20} STATUS")
        for site in config.sites:
            state = store.get_state(site.key)
            last = from_iso(state["last_checked_at"]) if state else None
            last_str = last.astimezone().strftime("%Y-%m-%d %H:%M:%S") if last else "-"
            if not site.enabled:
                status = "deaktiviert"
            elif state is not None and state["consecutive_failures"]:
                status = f"{state['consecutive_failures']}x Fehler: {state['last_error']}"
            elif state is None or not state["content_hash"]:
                status = "noch keine Baseline"
            else:
                changed = from_iso(state["last_changed_at"])
                status = (
                    f"ok (zuletzt geändert {changed.astimezone():%Y-%m-%d %H:%M})"
                    if changed
                    else "ok"
                )
            # Zeigt den Takt, der gerade gilt - '*' heisst: aus einem Zeitfenster.
            window = site.active_window(now)
            interval = format_duration(site.interval_at(now)) + ("*" if window else "")
            print(f"{site.key:<24} {interval:>9}  {last_str:<20} {status}")
            if args.verbose:
                print(f"{'':<24} {site.url}")
                for configured in site.interval_windows:
                    marker = "*" if configured is window else " "
                    print(f"{'':<24}{marker}Fenster {configured.describe()} ({site.timezone})")

        for entry in broken:
            print(f"{entry.key:<24} {'-':>9}  {'-':<20} ungültig: {entry.error}")

    if any(site.interval_windows for site in config.sites):
        print("\n* = Zeitfenster gerade aktiv, der Takt kommt von dort (alle: webwatcher -v list)")
    return 0


def cmd_history(args: argparse.Namespace, config: Config) -> int:
    with open_config(config) as (store, _repo):
        site_key = None
        if args.site:
            site = config.get_site(args.site)
            if site is None:
                raise SystemExit(f"Unbekannte Seite: {args.site!r}")
            site_key = site.key
        rows = store.recent_checks(site_key, args.limit)
        if not rows:
            print("Keine Checks aufgezeichnet.")
            return 0
        for row in reversed(rows):
            when = from_iso(row["checked_at"])
            stamp = when.astimezone().strftime("%Y-%m-%d %H:%M:%S") if when else "?"
            detail = ""
            if row["status"] in {"changed", "minor"}:
                detail = f" (+{row['added_lines']}/-{row['removed_lines']})"
            elif row["error"]:
                detail = f" ({row['error']})"
            duration = f"{row['duration_ms']}ms" if row["duration_ms"] is not None else "-"
            print(f"{stamp}  {row['site_key']:<20} {row['status']:<10} {duration:>8}{detail}")
    return 0


def cmd_reset(args: argparse.Namespace, config: Config) -> int:
    with open_config(config) as (store, _repo):
        for site in select_sites(config, args.site):
            store.reset_site(site.key)
            print(f"Baseline zurückgesetzt: {site.name}")
    return 0


async def cmd_test_telegram(args: argparse.Namespace, config: Config) -> int:
    notifier = make_notifier(config, required=True)
    assert notifier is not None
    try:
        me = await notifier.get_me()
        print(f"Bot: @{me.get('username')} ({me.get('first_name')})")
        await notifier.send_message(
            f"✅ <b>webwatcher</b> ist verbunden.\n{len(config.sites)} Seite(n) konfiguriert, "
            f"{len(config.telegram.chat_ids)} Empfänger."
        )
        empfaenger = ", ".join(config.telegram.chat_ids)
        anzahl = len(config.telegram.chat_ids)
        print(f"Testnachricht an {anzahl} Chat(s) gesendet: {empfaenger}")
        return 0
    except TelegramError as exc:
        print(f"Fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    finally:
        await notifier.aclose()


async def cmd_pick(args: argparse.Namespace, config: Config | None) -> int:
    from webwatcher.picker import run_picker

    store = None
    repo = None
    if config is not None:
        store = Store(config.db_path)
        repo = SiteRepository(store, config.site_defaults, config.screenshot_dir)
    try:
        return await run_picker(
            args.url,
            name=args.name,
            interval=args.interval,
            repo=repo,
            profile=Path(args.profile) if args.profile else None,
            write=not args.no_write,
            headless=args.headless,
        )
    finally:
        if store is not None:
            store.close()


async def cmd_chat_id(args: argparse.Namespace, config: Config) -> int:
    """Print chat ids from recent updates - schreib deinem Bot vorher einmal."""
    # Braucht nur den Token: die chat_id ist ja gerade das Gesuchte.
    notifier = make_notifier(config, required=True, need_chat=False)
    assert notifier is not None
    try:
        updates = await notifier.get_updates()
        if not updates:
            print(
                "Keine Nachrichten gefunden. So geht's:\n"
                "  1. Bot in Telegram suchen (der Name, den dir @BotFather genannt hat)\n"
                "  2. Ihm '/start' oder irgendeine Nachricht schicken\n"
                "  3. Dieses Kommando erneut ausführen\n"
                "Für eine Gruppe: Bot in die Gruppe einladen und dort etwas schreiben.\n"
                "Hinweis: Telegram liefert nur Nachrichten der letzten ~24 Stunden."
            )
            return 1
        seen: set[str] = set()
        for update in updates:
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") or {}
            chat_id = str(chat.get("id", ""))
            if chat_id and chat_id not in seen:
                seen.add(chat_id)
                title = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
                print(f"chat_id: {chat_id}  ({chat.get('type')}) {title}")
        return 0
    except TelegramError as exc:
        print(f"Fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    finally:
        await notifier.aclose()


# -- entry point ------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="webwatcher",
        description="Beobachtet Webseiten mit Playwright und meldet Änderungen per Telegram.",
    )
    parser.add_argument("--version", action="version", version=f"webwatcher {__version__}")
    parser.add_argument(
        "-c",
        "--config",
        default=DEFAULT_CONFIG,
        help=f"Pfad zur config.yaml (Default: {DEFAULT_CONFIG})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug-Logging")

    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Daemon: prüft alle Seiten in ihrem Intervall")
    run.add_argument(
        "--web",
        action="store_true",
        help="Web-UI mitstarten (bringt keine eigene Anmeldung mit - hinter einen Proxy!)",
    )
    run.add_argument("--web-port", type=int, help="Port für das Web-UI (Default: 8080)")
    run.add_argument("--web-host", help="Adresse für das Web-UI (Default: 0.0.0.0)")
    run.set_defaults(func=cmd_run, is_async=True)

    check = sub.add_parser("check", help="Einmalig prüfen (alle Seiten oder ausgewählte)")
    check.add_argument("site", nargs="*", help="Name oder Key der Seite")
    check.add_argument("--no-notify", action="store_true", help="Nichts an Telegram senden")
    check.add_argument("--show-diff", action="store_true", help="Diff im Terminal ausgeben")
    check.set_defaults(func=cmd_check, is_async=True)

    listing = sub.add_parser("list", help="Konfigurierte Seiten und ihren Status anzeigen")
    listing.set_defaults(func=cmd_list, is_async=False)

    history = sub.add_parser("history", help="Letzte Checks anzeigen")
    history.add_argument("site", nargs="?", help="Name oder Key der Seite")
    history.add_argument("-n", "--limit", type=int, default=20, help="Anzahl Einträge")
    history.set_defaults(func=cmd_history, is_async=False)

    reset = sub.add_parser("reset", help="Baseline verwerfen, nächster Check startet neu")
    reset.add_argument("site", nargs="*", help="Name oder Key der Seite")
    reset.set_defaults(func=cmd_reset, is_async=False)

    test = sub.add_parser("test-telegram", help="Testnachricht an den konfigurierten Chat")
    test.set_defaults(func=cmd_test_telegram, is_async=True)

    chat = sub.add_parser("chat-id", help="Chat-ID aus den letzten Bot-Updates auslesen")
    chat.set_defaults(func=cmd_chat_id, is_async=True)

    importer = sub.add_parser(
        "import-config", help="Seiten aus dem 'sites:'-Block der config.yaml übernehmen"
    )
    importer.set_defaults(func=cmd_import, is_async=False)

    pick = sub.add_parser(
        "pick", help="Elemente im Browser anklicken und daraus eine Seite anlegen"
    )
    pick.add_argument("url", help="Zu beobachtende URL")
    pick.add_argument("--name", help="Name der Seite (Default: die URL)")
    pick.add_argument("--interval", default="15m", help="Prüfabstand, z.B. 5m (Default: 15m)")
    pick.add_argument(
        "--profile",
        help="Verzeichnis für ein dauerhaftes Browser-Profil (für Seiten hinter Login)",
    )
    pick.add_argument(
        "--no-write", action="store_true", help="Block nur ausgeben, nichts speichern"
    )
    # Nur für Tests: ohne sichtbares Fenster starten.
    pick.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)
    pick.set_defaults(func=cmd_pick, is_async=True, needs_config=False)

    return parser


def main(argv: list[str] | None = None) -> int:
    setup_output_encoding()
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)

    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        # `pick` funktioniert auch ohne (gültige) Konfiguration.
        if getattr(args, "needs_config", True):
            print(f"Konfigurationsfehler: {exc}", file=sys.stderr)
            return 2
        log.debug("keine nutzbare Konfiguration (%s)", exc)
        config = None

    try:
        if args.is_async:
            return asyncio.run(args.func(args, config))
        return args.func(args, config)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
