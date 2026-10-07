"""aiohttp-Anwendung für das Web-UI.

Läuft als Task im selben Event-Loop wie der Daemon und hat damit direkten
Zugriff auf Runner, Store und Renderer - kein zweiter Prozess, kein zweites
Chromium, keine Zwischenschicht. Authentifizierung ist bewusst nicht Teil davor
liegender Reverse Proxy (siehe WebConfig).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aiohttp import web
from playwright.async_api import Error as PlaywrightError

from webwatcher.config import (
    KIND_VALUES,
    MODE_VALUES,
    RESOURCE_TYPES,
    SCREENSHOT_VALUES,
    WAIT_UNTIL_VALUES,
    Config,
    ConfigError,
    format_duration,
)
from webwatcher.hetzner import LOCATION_NAMES
from webwatcher.runner import Runner
from webwatcher.sites import SiteEntry, SiteRepository
from webwatcher.store import Store, from_iso, utcnow
from webwatcher.web.picker import PickerError, PickerRegistry

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")

# Beschreibt das Formular im Browser. Steht hier und nicht im Frontend, damit
# Feldliste, Beschriftung und Erklärung neben den Defaults liegen, die sie
# beschreiben - eine neue Option in SITE_DEFAULTS braucht genau einen Eintrag.
FIELDS: list[dict[str, Any]] = [
    # -- Grundeinstellungen
    {
        "name": "kind",
        "type": "enum",
        "group": "Grunddaten",
        "label": "Art",
        "options": sorted(KIND_VALUES),
        "help": "page = Webseite im Browser, hetzner_stock = Verfügbarkeit eines "
        "Hetzner-Cloud-Servertyps (braucht HCLOUD_TOKEN in der .env).",
    },
    {
        "name": "server_type",
        "type": "text",
        "group": "Grunddaten",
        "label": "Servertyp",
        "required": True,
        "only": "hetzner_stock",
        "help": "Wie in der Console, z.B. cx53.",
    },
    {
        "name": "locations",
        "type": "multi",
        "group": "Grunddaten",
        "label": "Standorte",
        "options": list(LOCATION_NAMES),
        "only": "hetzner_stock",
        "help": "nbg1 Nürnberg, fsn1 Falkenstein, hel1 Helsinki, ash Ashburn, hil Hillsboro, "
        "sin Singapur. Keiner gewählt = alle.",
    },
    {
        "name": "url",
        "type": "text",
        "group": "Grunddaten",
        "label": "URL",
        "required": True,
        "help": "Vollständig mit http:// oder https://",
    },
    {
        "name": "name",
        "type": "text",
        "group": "Grunddaten",
        "label": "Name",
        "help": "Erscheint in der Telegram-Nachricht. Leer = die URL.",
    },
    {
        "name": "interval",
        "type": "text",
        "group": "Grunddaten",
        "label": "Intervall",
        "help": "z.B. 30s, 15m, 2h",
    },
    {"name": "enabled", "type": "bool", "group": "Grunddaten", "label": "Aktiv"},
    {
        "name": "interval_windows",
        "type": "windows",
        "group": "Grunddaten",
        "label": "Zeitfenster",
        "help": "Abweichender Takt zu bestimmten Zeiten - schlägt das normale Intervall.",
    },
    # -- Was verglichen wird
    {
        "name": "selector",
        "type": "text",
        "group": "Inhalt",
        "label": "Selektor",
        "help": "CSS-Selektor des beobachteten Bereichs. Leer = die ganze Seite.",
    },
    {
        "name": "mode",
        "type": "enum",
        "group": "Inhalt",
        "label": "Modus",
        "options": sorted(MODE_VALUES),
        "help": "text vergleicht sichtbaren Text, html das Markup.",
    },
    {
        "name": "ignore_selectors",
        "type": "list",
        "group": "Inhalt",
        "label": "Ignorierte Elemente",
        "help": "Werden vor dem Vergleich entfernt (Werbung, Uhrzeiten, Zähler).",
    },
    {
        "name": "ignore_patterns",
        "type": "list",
        "group": "Inhalt",
        "label": "Ignorierte Muster",
        "help": "Reguläre Ausdrücke; passende Zeilen fallen aus dem Vergleich.",
    },
    {
        "name": "track_attributes",
        "type": "list",
        "group": "Inhalt",
        "label": "Attribute verfolgen",
        "help": "z.B. class - wenn der Zustand nur im Markup steckt, nicht im Text.",
    },
    # -- Laden
    {
        "name": "wait_until",
        "type": "enum",
        "group": "Laden",
        "label": "Warten bis",
        "options": sorted(WAIT_UNTIL_VALUES),
    },
    {
        "name": "wait_for",
        "type": "text",
        "group": "Laden",
        "label": "Warten auf Selektor",
        "help": "Erst weitermachen, wenn dieses Element da ist.",
    },
    {
        "name": "settle",
        "type": "text",
        "group": "Laden",
        "label": "Nachlaufzeit",
        "help": "Zusätzlich warten, z.B. 2s - für Seiten, die nachladen.",
    },
    {"name": "timeout", "type": "text", "group": "Laden", "label": "Timeout"},
    {
        "name": "js",
        "type": "textarea",
        "group": "Laden",
        "label": "Eigenes JavaScript",
        "help": "Wird in der Seite ausgeführt, bevor verglichen wird (Cookie-Banner wegklicken).",
    },
    # -- Browser
    {"name": "viewport", "type": "viewport", "group": "Browser", "label": "Fenstergröße"},
    {"name": "user_agent", "type": "text", "group": "Browser", "label": "User-Agent"},
    {"name": "locale", "type": "text", "group": "Browser", "label": "Sprache"},
    {
        "name": "timezone",
        "type": "text",
        "group": "Browser",
        "label": "Zeitzone",
        "help": "Gilt auch für die Zeitfenster oben.",
    },
    {"name": "headers", "type": "map", "group": "Browser", "label": "Zusätzliche Header"},
    {
        "name": "block_resources",
        "type": "multi",
        "group": "Browser",
        "label": "Blockieren",
        "options": sorted(RESOURCE_TYPES),
        "help": "Spart Zeit und Bandbreite; image und font sind meist gefahrlos.",
    },
    {
        "name": "ignore_https_errors",
        "type": "bool",
        "group": "Browser",
        "label": "Zertifikatsfehler ignorieren",
    },
    # -- Meldungen
    {
        "name": "recipients",
        "type": "recipients",
        "group": "Meldungen",
        "label": "Empfänger",
        "help": "Nichts angehakt = alle aus TELEGRAM_CHAT_ID. Weitere IDs müssen dem Bot "
        "vorher selbst geschrieben haben.",
    },
    {
        "name": "screenshot",
        "type": "enum",
        "group": "Meldungen",
        "label": "Screenshot",
        "options": sorted(SCREENSHOT_VALUES),
        "help": "auto folgt dem Selektor.",
    },
    {
        "name": "min_changed_lines",
        "type": "int",
        "group": "Meldungen",
        "label": "Ab wie vielen Zeilen",
        "help": "Kleinere Änderungen werden still übernommen.",
    },
    {"name": "max_diff_lines", "type": "int", "group": "Meldungen", "label": "Diff-Zeilen maximal"},
    {
        "name": "notify_on_error_after",
        "type": "int",
        "group": "Meldungen",
        "label": "Fehler melden ab",
        "help": "Nach wie vielen Fehlversuchen in Folge.",
    },
    {
        "name": "notify_first_check",
        "type": "bool",
        "group": "Meldungen",
        "label": "Erste Prüfung melden",
    },
]

# Was nur beim Rendern im Browser eine Rolle spielt - bei hetzner_stock blendet
# das UI diese Felder aus.
_PAGE_ONLY = {
    "url",
    "selector",
    "mode",
    "ignore_selectors",
    "ignore_patterns",
    "track_attributes",
    "wait_until",
    "wait_for",
    "settle",
    "js",
    "viewport",
    "user_agent",
    "locale",
    "headers",
    "block_resources",
    "ignore_https_errors",
    "screenshot",
    "min_changed_lines",
    "max_diff_lines",
}
for _field in FIELDS:
    if _field["name"] in _PAGE_ONLY:
        _field["only"] = "page"


class Context:
    """Alles, was die Handler brauchen - hängt unter app['ctx']."""

    def __init__(
        self,
        config: Config,
        store: Store,
        repo: SiteRepository,
        runner: Runner,
    ) -> None:
        self.config = config
        self.store = store
        self.repo = repo
        self.runner = runner
        self.picker = PickerRegistry(runner.renderer)
        # Chat-ID -> Anzeigename, einmal bei Telegram nachgefragt.
        self.chat_names: dict[str, str | None] = {}

    async def chat_options(self) -> list[dict[str, str]]:
        """Die konfigurierten Empfänger mit Namen, für die Häkchen im Formular."""
        notifier = self.runner.notifier
        options = []
        for chat_id in self.config.telegram.chat_ids:
            if chat_id not in self.chat_names:
                name = await notifier.get_chat_name(chat_id) if notifier else None
                if name is None:
                    # Fehlschlag nicht merken - beim nächsten Öffnen neu versuchen.
                    options.append({"value": chat_id, "label": chat_id})
                    continue
                self.chat_names[chat_id] = name
            name = self.chat_names[chat_id]
            options.append({"value": chat_id, "label": f"{name} ({chat_id})"})
        return options


def ctx(request: web.Request) -> Context:
    return request.app["ctx"]


def actor(request: web.Request) -> str | None:
    """Wer die Änderung auslöst - kommt vom Reverse Proxy, rein fürs Protokoll."""
    header = ctx(request).config.web.user_header
    return request.headers.get(header) if header else None


async def body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 - jede kaputte Nutzlast ist derselbe Fehler
        raise web.HTTPBadRequest(
            text='{"error": "kein gültiges JSON"}', content_type="application/json"
        ) from None
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(
            text='{"error": "Objekt erwartet"}', content_type="application/json"
        )
    return data


# -- Serialisierung ---------------------------------------------------------


def site_payload(entry: SiteEntry, state: Any, now: Any) -> dict[str, Any]:
    """Sollzustand plus beobachteter Zustand, so wie das UI beides zeigt."""
    site = entry.site
    payload: dict[str, Any] = {
        "key": entry.key,
        "name": entry.name,
        "url": site.url if site else str(entry.mapping.get("url") or ""),
        "kind": site.kind if site else str(entry.mapping.get("kind") or "page"),
        "valid": entry.valid,
        "error": entry.error,
        "enabled": bool(site.enabled) if site else bool(entry.mapping.get("enabled", True)),
        "mapping": entry.mapping,
        "position": entry.position,
        "updated_at": entry.updated_at,
        "updated_by": entry.updated_by,
    }

    if site:
        window = site.active_window(now)
        payload["interval"] = format_duration(site.interval_at(now))
        payload["window"] = window.describe() if window else None
        payload["windows"] = [w.describe() for w in site.interval_windows]
        payload["timezone"] = site.timezone

    if state is None:
        payload["status"] = "neu"
        return payload

    last = from_iso(state["last_checked_at"])
    changed = from_iso(state["last_changed_at"])
    due = from_iso(state["next_due_at"])
    failures = int(state["consecutive_failures"] or 0)

    if not (site and site.enabled):
        status = "deaktiviert"
    elif failures:
        status = "fehler"
    elif not state["content_hash"]:
        status = "neu"
    else:
        status = "ok"

    payload.update(
        {
            "status": status,
            "last_checked_at": last.isoformat() if last else None,
            "last_changed_at": changed.isoformat() if changed else None,
            "next_due_at": due.isoformat() if due else None,
            "failures": failures,
            "last_error": state["last_error"],
            "has_baseline": bool(state["content_hash"]),
            "has_screenshot": bool(state["screenshot_path"]),
        }
    )
    return payload


# -- Handler: Zustand -------------------------------------------------------


async def get_state(request: web.Request) -> web.Response:
    context = ctx(request)
    now = utcnow()
    sites = [
        site_payload(entry, context.store.get_state(entry.key), now)
        for entry in context.repo.entries()
    ]
    telegram = context.config.telegram
    return web.json_response(
        {
            "sites": sites,
            "revision": context.repo.revision(),
            "telegram": {
                "configured": telegram.configured,
                "has_token": telegram.has_token,
                "chats": len(telegram.chat_ids),
            },
            "config_path": str(context.config.source_path or ""),
            "concurrency": context.config.concurrency,
            "user": actor(request),
        }
    )


async def get_schema(request: web.Request) -> web.Response:
    """Feldliste plus Defaults - das Frontend baut das Formular daraus."""
    context = ctx(request)
    defaults = {
        key: value
        for key, value in context.repo.defaults.items()
        if key in {field["name"] for field in FIELDS}
    }
    chats = await context.chat_options()
    fields = [
        {**field, "options": chats} if field["name"] == "recipients" else field for field in FIELDS
    ]
    return web.json_response({"fields": fields, "defaults": defaults})


async def get_history(request: web.Request) -> web.Response:
    context = ctx(request)
    key = request.match_info["key"]
    limit = min(int(request.query.get("limit", 50)), 500)
    rows = context.store.recent_checks(key, limit)
    return web.json_response(
        {
            "checks": [
                {
                    "checked_at": row["checked_at"],
                    "status": row["status"],
                    "added_lines": row["added_lines"],
                    "removed_lines": row["removed_lines"],
                    "duration_ms": row["duration_ms"],
                    "http_status": row["http_status"],
                    "error": row["error"],
                }
                for row in rows
            ]
        }
    )


async def get_content(request: web.Request) -> web.Response:
    """Der gespeicherte Vergleichstext - zeigt, was tatsächlich beobachtet wird."""
    context = ctx(request)
    state = context.store.get_state(request.match_info["key"])
    if state is None:
        raise web.HTTPNotFound()
    return web.json_response({"content": state["content"] or "", "hash": state["content_hash"]})


async def get_screenshot(request: web.Request) -> web.StreamResponse:
    context = ctx(request)
    state = context.store.get_state(request.match_info["key"])
    if state is None or not state["screenshot_path"]:
        raise web.HTTPNotFound()

    path = Path(state["screenshot_path"])
    # Der Pfad kommt aus der eigenen Datenbank, aber ausbrechen soll er trotzdem nicht.
    try:
        path.resolve().relative_to(context.config.screenshot_dir.resolve())
    except ValueError:
        raise web.HTTPNotFound() from None
    if not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


# -- Handler: Seiten pflegen ------------------------------------------------


async def create_site(request: web.Request) -> web.Response:
    context = ctx(request)
    entry = context.repo.save(await body(request), actor=actor(request))
    context.runner.wake()
    now = utcnow()
    return web.json_response(
        site_payload(entry, context.store.get_state(entry.key), now), status=201
    )


async def update_site(request: web.Request) -> web.Response:
    context = ctx(request)
    key = request.match_info["key"]
    if context.repo.get(key) is None:
        raise web.HTTPNotFound()
    entry = context.repo.save(await body(request), original_key=key, actor=actor(request))
    context.runner.wake()
    return web.json_response(site_payload(entry, context.store.get_state(entry.key), utcnow()))


async def delete_site(request: web.Request) -> web.Response:
    context = ctx(request)
    if not context.repo.delete(request.match_info["key"]):
        raise web.HTTPNotFound()
    context.runner.wake()
    return web.json_response({"deleted": True})


async def reorder_sites(request: web.Request) -> web.Response:
    context = ctx(request)
    data = await body(request)
    keys = data.get("keys")
    if not isinstance(keys, list):
        raise ConfigError("keys: Liste von Seiten-Keys erwartet")
    context.repo.reorder([str(key) for key in keys])
    return web.json_response({"ok": True})


async def validate_site(request: web.Request) -> web.Response:
    """Prüft ein Mapping, ohne es zu speichern - das UI ruft das beim Tippen."""
    context = ctx(request)
    try:
        site = context.repo.validate(await body(request))
    except ConfigError as exc:
        return web.json_response({"ok": False, "error": str(exc)})
    now = utcnow()
    return web.json_response(
        {
            "ok": True,
            "key": site.key,
            "interval": format_duration(site.interval_at(now)),
            "windows": [window.describe() for window in site.interval_windows],
        }
    )


async def check_site(request: web.Request) -> web.Response:
    context = ctx(request)
    entry = context.repo.get(request.match_info["key"])
    if entry is None:
        raise web.HTTPNotFound()
    if entry.site is None:
        raise ConfigError(entry.error or "Die Konfiguration ist ungültig.")

    notify = request.query.get("notify", "1") not in {"0", "false", "no"}
    outcomes = await context.runner.check_once([entry.site], notify=notify)
    outcome = outcomes[0]
    return web.json_response(
        {
            "status": outcome.status,
            "describe": outcome.describe(),
            "duration_ms": outcome.duration_ms,
            "notified": outcome.notified,
            "diff": outcome.diff.text if outcome.diff else None,
            "error": outcome.error,
        }
    )


async def reset_site(request: web.Request) -> web.Response:
    context = ctx(request)
    key = request.match_info["key"]
    if context.repo.get(key) is None:
        raise web.HTTPNotFound()
    context.store.reset_site(key)
    context.runner.wake()
    return web.json_response({"ok": True})


async def preview_site(request: web.Request) -> web.Response:
    """Zweimal rendern und melden, was zwischen den Läufen wackelt."""
    context = ctx(request)
    site = context.repo.validate(await body(request))
    if site.kind == "hetzner_stock":
        # Eine API-Antwort wackelt nicht - einmal abfragen genügt.
        try:
            result = await context.runner.fetch(site)
        except Exception as exc:  # noqa: BLE001 - im Panel anzeigen
            return web.json_response({"ok": False, "error": str(exc)[:300]})
        lines = result.text.splitlines()
        return web.json_response(
            {
                "ok": True,
                "count": len(lines),
                "chars": len(result.text),
                "sample": lines,
                "unstable": [],
                "suggestions": [],
                "matches": 1,
            }
        )
    return web.json_response(await context.picker.preview(site))


async def test_telegram(request: web.Request) -> web.Response:
    context = ctx(request)
    notifier = context.runner.notifier
    if notifier is None:
        return web.json_response(
            {"ok": False, "error": "Telegram ist nicht konfiguriert (Token/Chat-ID fehlen)."}
        )
    try:
        me = await notifier.get_me()
        await notifier.send_message("✅ <b>webwatcher</b>: Test aus dem Web-UI.")
    except Exception as exc:  # noqa: BLE001 - dem Nutzer den Grund zeigen
        return web.json_response({"ok": False, "error": str(exc)})
    return web.json_response({"ok": True, "bot": me.get("username")})


# -- Handler: Picker --------------------------------------------------------


async def picker_create(request: web.Request) -> web.Response:
    context = ctx(request)
    site = context.repo.validate(await body(request))
    session = await context.picker.create(site)
    return web.json_response(await session.view(), status=201)


async def picker_view(request: web.Request) -> web.Response:
    session = ctx(request).picker.get(request.match_info["id"])
    return web.json_response(await session.view())


async def picker_screenshot(request: web.Request) -> web.Response:
    session = ctx(request).picker.get(request.match_info["id"])
    if session.shot is None:
        raise web.HTTPNotFound()
    return web.Response(
        body=session.shot,
        content_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


async def picker_pick(request: web.Request) -> web.Response:
    session = ctx(request).picker.get(request.match_info["id"])
    data = await body(request)
    result = await session.pick(
        float(data.get("x", 0)), float(data.get("y", 0)), bool(data.get("broad"))
    )
    if result is None:
        return web.json_response({"error": "An dieser Stelle ist kein Element."}, status=404)
    return web.json_response(result)


async def picker_describe(request: web.Request) -> web.Response:
    session = ctx(request).picker.get(request.match_info["id"])
    data = await body(request)
    selector = str(data.get("selector") or "").strip()
    if not selector:
        return web.json_response({"selector": "", "matches": 0, "rects": []})
    return web.json_response(await session.describe(selector))


async def picker_scroll(request: web.Request) -> web.Response:
    session = ctx(request).picker.get(request.match_info["id"])
    data = await body(request)
    return web.json_response(await session.scroll_to(float(data.get("y", 0))))


async def picker_reload(request: web.Request) -> web.Response:
    session = ctx(request).picker.get(request.match_info["id"])
    return web.json_response(await session.reload())


async def picker_close(request: web.Request) -> web.Response:
    await ctx(request).picker.drop(request.match_info["id"])
    return web.json_response({"ok": True})


# -- Anwendung --------------------------------------------------------------


@web.middleware
async def error_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    """Fehler werden JSON, damit das Frontend sie anzeigen kann statt zu raten."""
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except ConfigError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except PickerError as exc:
        return web.json_response({"error": str(exc)}, status=409)
    except PlaywrightError as exc:
        message = str(exc).strip().splitlines()[0][:300]
        return web.json_response({"error": message}, status=502)
    except Exception as exc:  # noqa: BLE001 - nichts darf den Server umbringen
        log.exception("Web-UI: unbehandelter Fehler bei %s", request.rel_url)
        return web.json_response({"error": f"{exc.__class__.__name__}: {exc}"}, status=500)


async def index(request: web.Request) -> web.StreamResponse:  # noqa: ARG001
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


def build_app(
    config: Config, store: Store, repo: SiteRepository, runner: Runner
) -> web.Application:
    app = web.Application(middlewares=[error_middleware], client_max_size=2 * 1024 * 1024)
    app["ctx"] = Context(config, store, repo, runner)

    app.add_routes(
        [
            web.get("/", index),
            web.get("/api/state", get_state),
            web.get("/api/schema", get_schema),
            web.post("/api/sites", create_site),
            web.post("/api/sites/reorder", reorder_sites),
            web.post("/api/validate", validate_site),
            web.post("/api/preview", preview_site),
            web.post("/api/telegram/test", test_telegram),
            web.get("/api/sites/{key}/history", get_history),
            web.get("/api/sites/{key}/content", get_content),
            web.get("/api/sites/{key}/screenshot", get_screenshot),
            web.post("/api/sites/{key}/check", check_site),
            web.post("/api/sites/{key}/reset", reset_site),
            web.put("/api/sites/{key}", update_site),
            web.delete("/api/sites/{key}", delete_site),
            web.post("/api/picker", picker_create),
            web.get("/api/picker/{id}", picker_view),
            web.get("/api/picker/{id}/screenshot", picker_screenshot),
            web.post("/api/picker/{id}/pick", picker_pick),
            web.post("/api/picker/{id}/describe", picker_describe),
            web.post("/api/picker/{id}/scroll", picker_scroll),
            web.post("/api/picker/{id}/reload", picker_reload),
            web.delete("/api/picker/{id}", picker_close),
            web.static("/static", STATIC_DIR),
        ]
    )

    async def close_picker(app: web.Application) -> None:
        await app["ctx"].picker.close_all()

    app.on_cleanup.append(close_picker)
    return app


async def serve(
    config: Config, store: Store, repo: SiteRepository, runner: Runner
) -> web.AppRunner:
    """Startet den Server und gibt den Runner zurück - Aufräumen macht der Aufrufer."""
    app = build_app(config, store, repo, runner)
    app_runner = web.AppRunner(app, access_log=None)
    await app_runner.setup()
    site = web.TCPSite(app_runner, config.web.host, config.web.port)
    await site.start()
    log.info(
        "Web-UI auf http://%s:%d (ohne eigene Anmeldung - bitte hinter einen Proxy)",
        config.web.host,
        config.web.port,
    )
    return app_runner
