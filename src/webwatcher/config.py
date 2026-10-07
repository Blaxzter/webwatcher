"""YAML configuration loading, validation and defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from functools import cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

WAIT_UNTIL_VALUES = {"load", "domcontentloaded", "networkidle", "commit"}
SCREENSHOT_VALUES = {"auto", "full_page", "viewport", "element", "none"}
MODE_VALUES = {"text", "html"}
# page = Webseite im Browser rendern, hetzner_stock = Hetzner-Cloud-API fragen.
KIND_VALUES = {"page", "hetzner_stock"}
HETZNER_CONSOLE_URL = "https://console.hetzner.com/"
_SERVER_TYPE_RE = re.compile(r"^[a-z0-9-]+$")
_LOCATION_RE = re.compile(r"^[a-z]+[0-9]*$")
_CHAT_ID_RE = re.compile(r"^(-?\d+|@\w+)$")
RESOURCE_TYPES = {
    "document",
    "stylesheet",
    "image",
    "media",
    "font",
    "script",
    "texttrack",
    "xhr",
    "fetch",
    "eventsource",
    "websocket",
    "manifest",
    "other",
}

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)?\s*$", re.IGNORECASE)
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_CLOCK_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")

_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

MINUTES_PER_DAY = 1440
DAY_NAMES = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
_WEEKDAYS = {
    "mon": 0, "monday": 0, "mo": 0, "montag": 0,
    "tue": 1, "tuesday": 1, "di": 1, "dienstag": 1,
    "wed": 2, "wednesday": 2, "mi": 2, "mittwoch": 2,
    "thu": 3, "thursday": 3, "do": 3, "donnerstag": 3,
    "fri": 4, "friday": 4, "fr": 4, "freitag": 4,
    "sat": 5, "saturday": 5, "sa": 5, "samstag": 5,
    "sun": 6, "sunday": 6, "so": 6, "sonntag": 6,
}  # fmt: skip

# Site-level knobs and their fallbacks. Anything listed here can be set globally
# under `defaults:` and overridden per site.
SITE_DEFAULTS: dict[str, Any] = {
    "kind": "page",
    # Nur für kind: hetzner_stock - z.B. cx53 und [nbg1, fsn1] (leer = alle).
    "server_type": None,
    "locations": [],
    "interval": "15m",
    # Zeitfenster mit abweichendem (meist engerem) Takt, siehe _build_windows.
    "interval_windows": [],
    "mode": "text",
    "selector": None,
    "ignore_selectors": [],
    "ignore_patterns": [],
    # z.B. ["class"] wenn der Zustand nur in Attributen steckt, nicht im Text.
    "track_attributes": [],
    "wait_for": None,
    "wait_until": "networkidle",
    "timeout": "30s",
    "settle": "0s",
    "js": None,
    # 'auto' folgt dem selector, damit das Bild zeigt, was beobachtet wird.
    "screenshot": "auto",
    "viewport": {"width": 1440, "height": 900},
    "user_agent": DEFAULT_USER_AGENT,
    "locale": "de-DE",
    "timezone": "Europe/Berlin",
    "headers": {},
    "block_resources": [],
    "ignore_https_errors": False,
    "notify_on_error_after": 3,
    "min_changed_lines": 1,
    "max_diff_lines": 60,
    "notify_first_check": True,
    # Telegram-Chat-IDs nur für diese Seite. Leer = alle aus telegram.chat_id.
    "recipients": [],
    "enabled": True,
}


class ConfigError(RuntimeError):
    """Raised for any malformed or missing configuration value."""


@cache
def _zone(name: str) -> tzinfo:
    """Resolve a timezone name; unknown ones fall back to UTC (validated at load)."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def _clock(minute_of_day: int) -> str:
    hour, minute = divmod(minute_of_day, 60)
    return f"{hour:02d}:{minute:02d}"


@dataclass(frozen=True)
class IntervalWindow:
    """A recurring time-of-day window with its own check interval.

    Times are wall clock in the site's `timezone`, `end` is exclusive and a
    window may wrap around midnight (23:30 -> 00:30). With `days` set, the
    *start* day decides - a wrapping window keeps running into the next day.
    """

    start_minute: int
    end_minute: int
    interval_seconds: float
    days: frozenset[int] = frozenset()  # empty = every day

    @property
    def wraps_midnight(self) -> bool:
        return self.end_minute <= self.start_minute

    @property
    def label(self) -> str:
        days = ",".join(DAY_NAMES[day] for day in sorted(self.days))
        span = f"{_clock(self.start_minute)}-{_clock(self.end_minute)}"
        return f"{days} {span}" if days else span

    def describe(self) -> str:
        return f"{self.label} -> {format_duration(self.interval_seconds)}"

    def _day_matches(self, day: date) -> bool:
        return not self.days or day.weekday() in self.days

    def contains(self, local: datetime) -> bool:
        """`local` must already be in the site's timezone."""
        minute = local.hour * 60 + local.minute
        if not self.wraps_midnight:
            return self.start_minute <= minute < self.end_minute and self._day_matches(local.date())
        if minute >= self.start_minute:
            return self._day_matches(local.date())
        if minute < self.end_minute:
            return self._day_matches(local.date() - timedelta(days=1))
        return False

    def boundaries(self, local: datetime, zone: tzinfo) -> list[datetime]:
        """Every start/end around `local` (yesterday .. a week ahead).

        Built from wall clock dates rather than by adding a duration, so a DST
        switch inside a window does not shift its end by an hour.
        """
        start_time = time(*divmod(self.start_minute, 60))
        end_time = time(*divmod(self.end_minute, 60))
        result: list[datetime] = []
        for offset in range(-1, 8):
            day = local.date() + timedelta(days=offset)
            if not self._day_matches(day):
                continue
            end_day = day + timedelta(days=1) if self.wraps_midnight else day
            result.append(datetime.combine(day, start_time, tzinfo=zone))
            result.append(datetime.combine(end_day, end_time, tzinfo=zone))
        return result


@dataclass(frozen=True)
class SiteConfig:
    key: str
    name: str
    url: str
    kind: str
    server_type: str | None
    locations: list[str]
    interval_seconds: float
    interval_windows: tuple[IntervalWindow, ...]
    mode: str
    selector: str | None
    ignore_selectors: list[str]
    ignore_patterns: list[re.Pattern[str]]
    track_attributes: list[str]
    wait_for: str | None
    wait_until: str
    timeout_ms: int
    settle_ms: int
    js: str | None
    screenshot: str
    viewport: dict[str, int]
    user_agent: str
    locale: str
    timezone: str
    headers: dict[str, str]
    block_resources: list[str]
    ignore_https_errors: bool
    notify_on_error_after: int
    min_changed_lines: int
    max_diff_lines: int
    notify_first_check: bool
    recipients: tuple[str, ...]
    enabled: bool

    def active_window(self, moment: datetime) -> IntervalWindow | None:
        """The window in force at `moment` (UTC). Overlapping: the faster one wins."""
        if not self.interval_windows:
            return None
        local = moment.astimezone(_zone(self.timezone))
        matching = [w for w in self.interval_windows if w.contains(local)]
        return min(matching, key=lambda w: w.interval_seconds) if matching else None

    def interval_at(self, moment: datetime) -> float:
        window = self.active_window(moment)
        return window.interval_seconds if window else self.interval_seconds

    def next_window_change(self, moment: datetime) -> datetime | None:
        """When the effective interval changes next, i.e. a window starts or ends."""
        if not self.interval_windows:
            return None
        zone = _zone(self.timezone)
        local = moment.astimezone(zone)
        upcoming = [
            edge
            for window in self.interval_windows
            for edge in window.boundaries(local, zone)
            if edge > local
        ]
        return min(upcoming).astimezone(timezone.utc) if upcoming else None


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_ids: tuple[str, ...] = ()
    send_screenshot: bool = True
    silent: bool = False

    @property
    def has_token(self) -> bool:
        """Enough to talk to the API - getMe/getUpdates need no chat."""
        return bool(self.bot_token)

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_ids)


@dataclass(frozen=True)
class WebConfig:
    """Das Web-UI. Läuft im selben Prozess wie der Daemon.

    Es bringt bewusst keine eigene Authentifizierung mit: gedacht ist der
    Betrieb hinter einem Reverse Proxy (Traefik, Cloudflare Access, ...), der
    das übernimmt. `host` deshalb nur auf eine Adresse legen, die nicht offen
    im Netz steht - im Container ist 0.0.0.0 richtig, solange docker-compose
    keinen Port auf den Host veröffentlicht.
    """

    enabled: bool = False
    host: str = "0.0.0.0"  # noqa: S104 - siehe Docstring
    port: int = 8080
    # Woraus das UI ableitet, wer gerade etwas ändert (nur fürs Protokoll).
    user_header: str = "Cf-Access-Authenticated-User-Email"


@dataclass
class Config:
    sites: list[SiteConfig]
    telegram: TelegramConfig
    db_path: Path
    screenshot_dir: Path
    keep_screenshots: int = 20
    history_days: int = 30
    concurrency: int = 2
    jitter_seconds: float = 5.0
    source_path: Path | None = None
    web: WebConfig = field(default_factory=WebConfig)
    # Die zusammengeführten `defaults:` - das Repository braucht sie, um die
    # Mappings aus der Datenbank genauso aufzubauen wie die aus der YAML.
    site_defaults: dict[str, Any] = field(default_factory=lambda: dict(SITE_DEFAULTS))
    # Roher, ungeprüfter `sites:`-Block aus der YAML - nur für `import-config`.
    seed_sites: list[Any] = field(default_factory=list)
    _by_key: dict[str, SiteConfig] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_key = {site.key: site for site in self.sites}

    def replace_sites(self, sites: list[SiteConfig]) -> None:
        """Die Seiten kommen im Normalfall aus der Datenbank, nicht aus der YAML."""
        self.sites = sites
        self._by_key = {site.key: site for site in sites}

    def get_site(self, name_or_key: str) -> SiteConfig | None:
        needle = name_or_key.strip()
        if needle in self._by_key:
            return self._by_key[needle]
        lowered = needle.lower()
        for site in self.sites:
            if site.name.lower() == lowered or site.key == _slugify(lowered):
                return site
        return None


def _slugify(value: str) -> str:
    return _SLUG_RE.sub("-", value.lower()).strip("-") or "site"


def parse_duration(value: Any, where: str) -> float:
    """Accept 30, '30', '30s', '5m', '1h', '500ms' and return seconds."""
    if isinstance(value, bool):
        raise ConfigError(f"{where}: expected a duration, got a boolean")
    if isinstance(value, (int, float)):
        return float(value)
    match = _DURATION_RE.match(str(value))
    if not match:
        raise ConfigError(f"{where}: invalid duration {value!r} (use e.g. 30s, 5m, 2h)")
    amount = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    return amount * _UNIT_SECONDS[unit]


def format_duration(seconds: float) -> str:
    """Inverse of parse_duration for display: 90 -> '1.5m'."""
    for limit, unit, factor in ((60, "s", 1), (3600, "m", 60), (86400, "h", 3600)):
        if seconds < limit:
            return f"{seconds / factor:g}{unit}"
    return f"{seconds / 86400:g}d"


def parse_clock(value: Any, where: str) -> int:
    """'09:30' -> minutes since midnight. '24:00' means midnight, end of day."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # YAML 1.1 reads an unquoted 9:30 as the number 570 (sexagesimal).
        raise ConfigError(f'{where}: put the time in quotes, e.g. "09:30"')
    match = _CLOCK_RE.match(str(value))
    if not match:
        raise ConfigError(f'{where}: invalid time {value!r} (use "HH:MM", e.g. "08:45")')
    hour, minute = int(match.group(1)), int(match.group(2))
    if (hour, minute) == (24, 0):
        return 0
    if hour > 23 or minute > 59:
        raise ConfigError(f"{where}: invalid time {value!r} (00:00 - 23:59)")
    return hour * 60 + minute


def _parse_days(value: Any, where: str) -> frozenset[int]:
    days: set[int] = set()
    for item in _as_str_list(value, where):
        for part in item.split(","):
            name = part.strip().lower()
            if not name:
                continue
            if name not in _WEEKDAYS:
                raise ConfigError(
                    f"{where}: unknown weekday {name!r} (mon..sun, mo..so or montag..sonntag)"
                )
            days.add(_WEEKDAYS[name])
    return frozenset(days)


def _build_windows(raw: Any, where: str) -> tuple[IntervalWindow, ...]:
    """Parse `interval_windows: [{from, to, interval, days?}, ...]`."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{where}: expected a list of {{from, to, interval}} mappings")

    windows: list[IntervalWindow] = []
    for index, item in enumerate(raw):
        spot = f"{where}[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{spot}: expected a mapping with from/to/interval")
        unknown = set(item) - {"from", "to", "interval", "days"}
        if unknown:
            raise ConfigError(f"{spot}: unknown option(s): {sorted(unknown)}")
        for required in ("from", "to", "interval"):
            if item.get(required) is None:
                raise ConfigError(f"{spot}: {required!r} is required")

        start = parse_clock(item["from"], f"{spot}.from")
        end = parse_clock(item["to"], f"{spot}.to")
        if start == end:
            raise ConfigError(
                f"{spot}: 'from' and 'to' are identical ({item['from']!r}), the window is empty"
            )
        windows.append(
            IntervalWindow(
                start_minute=start,
                end_minute=end,
                interval_seconds=max(5.0, parse_duration(item["interval"], f"{spot}.interval")),
                days=_parse_days(item.get("days"), f"{spot}.days"),
            )
        )
    return tuple(windows)


def _expand_env(node: Any) -> Any:
    """Recursively replace ${VAR} / ${VAR:-fallback} in every string value."""
    if isinstance(node, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), node)
    if isinstance(node, list):
        return [_expand_env(item) for item in node]
    if isinstance(node, dict):
        return {key: _expand_env(value) for key, value in node.items()}
    return node


def _as_str_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    raise ConfigError(f"{where}: expected a string or a list of strings")


def _as_int(value: Any, where: str, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where}: expected an integer, got {value!r}") from None
    if minimum is not None and result < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}, got {result}")
    return result


def _as_bool(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in {"true", "false", "yes", "no", "1", "0"}:
        return value.lower() in {"true", "yes", "1"}
    raise ConfigError(f"{where}: expected true/false, got {value!r}")


def _one_of(value: Any, allowed: set[str], where: str) -> str:
    result = str(value)
    if result not in allowed:
        raise ConfigError(f"{where}: must be one of {sorted(allowed)}, got {result!r}")
    return result


def _build_site(raw: dict[str, Any], defaults: dict[str, Any], index: int) -> SiteConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"sites[{index}]: expected a mapping")

    kind = _one_of(
        raw.get("kind") or defaults.get("kind") or "page", KIND_VALUES, f"sites[{index}].kind"
    )
    server_type: str | None = None
    locations: list[str] = []
    if kind == "hetzner_stock":
        server_type = str(raw.get("server_type") or "").strip().lower() or None
        if not server_type:
            raise ConfigError(f"sites[{index}]: 'server_type' is required (e.g. cx53)")
        if not _SERVER_TYPE_RE.match(server_type):
            raise ConfigError(f"sites[{index}].server_type: invalid value {server_type!r}")
        for item in _as_str_list(raw.get("locations"), f"sites[{index}].locations"):
            for part in item.split(","):
                code = part.strip().lower()
                if code and not _LOCATION_RE.match(code):
                    raise ConfigError(f"sites[{index}].locations: invalid location {code!r}")
                if code and code not in locations:
                    locations.append(code)

    # Bei hetzner_stock ist die URL nur der Link in der Meldung.
    url = str(raw.get("url") or "").strip()
    if not url and kind == "hetzner_stock":
        url = HETZNER_CONSOLE_URL
    if not url:
        raise ConfigError(f"sites[{index}]: 'url' is required")
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"sites[{index}]: url must start with http:// or https://, got {url!r}")

    fallback_name = f"Hetzner {server_type.upper()}" if server_type else url
    name = str(raw.get("name") or fallback_name).strip()
    where = f"sites[{index}] ({name})"

    # `None` in a site block means "not set here" so the default still wins.
    merged = dict(defaults)
    merged.update({k: v for k, v in raw.items() if v is not None})

    patterns: list[re.Pattern[str]] = []
    for pattern in _as_str_list(merged.get("ignore_patterns"), f"{where}.ignore_patterns"):
        try:
            patterns.append(re.compile(pattern))
        except re.error as exc:
            raise ConfigError(
                f"{where}.ignore_patterns: invalid regex {pattern!r}: {exc}"
            ) from None

    viewport_raw = merged.get("viewport") or {}
    if not isinstance(viewport_raw, dict):
        raise ConfigError(f"{where}.viewport: expected a mapping with width/height")
    viewport = {
        "width": _as_int(viewport_raw.get("width", 1440), f"{where}.viewport.width", 100),
        "height": _as_int(viewport_raw.get("height", 900), f"{where}.viewport.height", 100),
    }

    headers_raw = merged.get("headers") or {}
    if not isinstance(headers_raw, dict):
        raise ConfigError(f"{where}.headers: expected a mapping")

    blocked = _as_str_list(merged.get("block_resources"), f"{where}.block_resources")
    for item in blocked:
        if item not in RESOURCE_TYPES:
            raise ConfigError(
                f"{where}.block_resources: unknown resource type {item!r}, "
                f"allowed: {sorted(RESOURCE_TYPES)}"
            )

    screenshot = _one_of(merged.get("screenshot"), SCREENSHOT_VALUES, f"{where}.screenshot")
    selector = merged.get("selector")
    selector = str(selector) if selector else None
    if screenshot == "element" and not selector:
        raise ConfigError(f"{where}: screenshot 'element' requires a 'selector'")

    key = str(raw.get("key") or _slugify(name))

    windows = _build_windows(merged.get("interval_windows"), f"{where}.interval_windows")
    timezone_name = str(merged.get("timezone"))
    if windows and _zone(timezone_name) is timezone.utc and timezone_name.upper() != "UTC":
        raise ConfigError(
            f"{where}.timezone: unknown timezone {timezone_name!r} - interval_windows are read "
            "in that timezone, so this has to resolve (on Windows: pip install tzdata)"
        )

    return SiteConfig(
        key=key,
        name=name,
        url=url,
        kind=kind,
        server_type=server_type,
        locations=locations,
        interval_seconds=max(5.0, parse_duration(merged.get("interval"), f"{where}.interval")),
        interval_windows=windows,
        mode=_one_of(merged.get("mode"), MODE_VALUES, f"{where}.mode"),
        selector=selector,
        ignore_selectors=_as_str_list(merged.get("ignore_selectors"), f"{where}.ignore_selectors"),
        ignore_patterns=patterns,
        track_attributes=_as_str_list(merged.get("track_attributes"), f"{where}.track_attributes"),
        wait_for=str(merged["wait_for"]) if merged.get("wait_for") else None,
        wait_until=_one_of(merged.get("wait_until"), WAIT_UNTIL_VALUES, f"{where}.wait_until"),
        timeout_ms=int(parse_duration(merged.get("timeout"), f"{where}.timeout") * 1000),
        settle_ms=int(parse_duration(merged.get("settle"), f"{where}.settle") * 1000),
        js=str(merged["js"]) if merged.get("js") else None,
        screenshot=screenshot,
        viewport=viewport,
        user_agent=str(merged.get("user_agent")),
        locale=str(merged.get("locale")),
        timezone=str(merged.get("timezone")),
        headers={str(k): str(v) for k, v in headers_raw.items()},
        block_resources=blocked,
        ignore_https_errors=_as_bool(
            merged.get("ignore_https_errors"), f"{where}.ignore_https_errors"
        ),
        notify_on_error_after=_as_int(
            merged.get("notify_on_error_after"), f"{where}.notify_on_error_after", 1
        ),
        min_changed_lines=_as_int(merged.get("min_changed_lines"), f"{where}.min_changed_lines", 1),
        max_diff_lines=_as_int(merged.get("max_diff_lines"), f"{where}.max_diff_lines", 1),
        notify_first_check=_as_bool(
            merged.get("notify_first_check"), f"{where}.notify_first_check"
        ),
        recipients=_build_recipients(merged.get("recipients"), f"{where}.recipients"),
        enabled=_as_bool(merged.get("enabled"), f"{where}.enabled"),
    )


def _build_recipients(value: Any, where: str) -> tuple[str, ...]:
    """Chat-IDs einer Seite: Liste oder komma-getrennt, wie bei telegram.chat_id."""
    if value is not None and not isinstance(value, str | int | list):
        raise ConfigError(f"{where}: expected a chat id or a list of chat ids")
    recipients = parse_chat_ids({"chat_ids": value})
    for chat_id in recipients:
        if not _CHAT_ID_RE.match(chat_id):
            raise ConfigError(
                f"{where}: invalid chat id {chat_id!r} (a number like 123456789, "
                "groups start with -, channels with @)"
            )
    return recipients


def parse_chat_ids(raw: dict[str, Any]) -> tuple[str, ...]:
    """Accept chat_id/chat_ids as a scalar, a comma separated string or a list.

    Comma separated matters because the value usually arrives through an
    environment variable: TELEGRAM_CHAT_ID=123456789,987654321
    """
    values: list[Any] = []
    for key in ("chat_id", "chat_ids"):
        value = raw.get(key)
        if value is None:
            continue
        values.extend(value if isinstance(value, list) else [value])

    result: list[str] = []
    for value in values:
        for part in str(value).split(","):
            part = part.strip()
            if part and part not in result:
                result.append(part)
    return tuple(result)


def site_from_mapping(raw: dict[str, Any], defaults: dict[str, Any] | None = None) -> SiteConfig:
    """Build a single SiteConfig outside of a config file (used by the picker)."""
    return _build_site(raw, {**SITE_DEFAULTS, **(defaults or {})}, 0)


def load_config(path: str | Path) -> Config:
    config_path = Path(path).expanduser()
    if config_path.is_dir():
        # Docker creates a directory for a bind-mounted file that does not exist yet.
        raise ConfigError(
            f"{config_path} ist ein Verzeichnis, keine Datei. Das passiert, wenn ein "
            "Container gestartet wird, bevor die config.yaml existiert. Auf dem Host: "
            "das Verzeichnis löschen (rmdir config.yaml), dann "
            "'cp config.example.yaml config.yaml' und erneut starten."
        )
    if not config_path.is_file():
        raise ConfigError(
            f"Konfigurationsdatei nicht gefunden: {config_path} "
            "(Vorlage kopieren: cp config.example.yaml config.yaml)"
        )

    try:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path}: invalid YAML: {exc}") from None
    if not isinstance(document, dict):
        raise ConfigError(f"{config_path}: top level must be a mapping")

    # Ein `sites:`-Block ist nur noch Saatgut für `import-config` - die Seiten
    # selbst leben in der Datenbank. Bewusst *vor* _expand_env abgegriffen:
    # so wandert `${TOKEN}` als `${TOKEN}` in die Datenbank und nicht der Token.
    seed_sites = document.get("sites") or []
    if not isinstance(seed_sites, list):
        raise ConfigError("sites: expected a list")

    raw = _expand_env(document)

    defaults = dict(SITE_DEFAULTS)
    user_defaults = raw.get("defaults") or {}
    if not isinstance(user_defaults, dict):
        raise ConfigError("defaults: expected a mapping")
    unknown = set(user_defaults) - set(SITE_DEFAULTS)
    if unknown:
        raise ConfigError(f"defaults: unknown option(s): {sorted(unknown)}")
    defaults.update({k: v for k, v in user_defaults.items() if v is not None})

    telegram_raw = raw.get("telegram") or {}
    if not isinstance(telegram_raw, dict):
        raise ConfigError("telegram: expected a mapping")
    telegram = TelegramConfig(
        bot_token=str(telegram_raw.get("bot_token") or "").strip(),
        chat_ids=parse_chat_ids(telegram_raw),
        send_screenshot=_as_bool(
            telegram_raw.get("send_screenshot", True), "telegram.send_screenshot"
        ),
        silent=_as_bool(telegram_raw.get("silent", False), "telegram.silent"),
    )

    web_raw = raw.get("web") or {}
    if not isinstance(web_raw, dict):
        raise ConfigError("web: expected a mapping")
    unknown_web = set(web_raw) - {"enabled", "host", "port", "user_header"}
    if unknown_web:
        raise ConfigError(f"web: unknown option(s): {sorted(unknown_web)}")
    web = WebConfig(
        enabled=_as_bool(web_raw.get("enabled", False), "web.enabled"),
        host=str(web_raw.get("host") or "0.0.0.0"),  # noqa: S104 - siehe WebConfig
        port=_as_int(web_raw.get("port", 8080), "web.port", 1),
        user_header=str(web_raw.get("user_header") or "Cf-Access-Authenticated-User-Email"),
    )

    storage = raw.get("storage") or {}
    if not isinstance(storage, dict):
        raise ConfigError("storage: expected a mapping")
    base = config_path.parent
    db_path = Path(str(storage.get("db_path") or "data/watcher.sqlite3")).expanduser()
    screenshot_dir = Path(str(storage.get("screenshot_dir") or "data/screenshots")).expanduser()

    return Config(
        sites=[],  # kommen aus der Datenbank, siehe cli.open_repo
        telegram=telegram,
        db_path=db_path if db_path.is_absolute() else base / db_path,
        screenshot_dir=screenshot_dir if screenshot_dir.is_absolute() else base / screenshot_dir,
        keep_screenshots=_as_int(
            storage.get("keep_screenshots", 20), "storage.keep_screenshots", 0
        ),
        history_days=_as_int(storage.get("history_days", 30), "storage.history_days", 1),
        concurrency=_as_int(raw.get("concurrency", 2), "concurrency", 1),
        jitter_seconds=parse_duration(raw.get("jitter", "5s"), "jitter"),
        source_path=config_path,
        web=web,
        site_defaults=defaults,
        seed_sites=seed_sites,
    )
