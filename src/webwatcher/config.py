"""YAML configuration loading, validation and defaults."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

WAIT_UNTIL_VALUES = {"load", "domcontentloaded", "networkidle", "commit"}
SCREENSHOT_VALUES = {"auto", "full_page", "viewport", "element", "none"}
MODE_VALUES = {"text", "html"}
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

_UNIT_SECONDS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}

# Site-level knobs and their fallbacks. Anything listed here can be set globally
# under `defaults:` and overridden per site.
SITE_DEFAULTS: dict[str, Any] = {
    "interval": "15m",
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
    "enabled": True,
}


class ConfigError(RuntimeError):
    """Raised for any malformed or missing configuration value."""


@dataclass(frozen=True)
class SiteConfig:
    key: str
    name: str
    url: str
    interval_seconds: float
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
    enabled: bool


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
    _by_key: dict[str, SiteConfig] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_key = {site.key: site for site in self.sites}

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

    url = str(raw.get("url") or "").strip()
    if not url:
        raise ConfigError(f"sites[{index}]: 'url' is required")
    if not url.startswith(("http://", "https://")):
        raise ConfigError(f"sites[{index}]: url must start with http:// or https://, got {url!r}")

    name = str(raw.get("name") or url).strip()
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

    return SiteConfig(
        key=key,
        name=name,
        url=url,
        interval_seconds=max(5.0, parse_duration(merged.get("interval"), f"{where}.interval")),
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
        enabled=_as_bool(merged.get("enabled"), f"{where}.enabled"),
    )


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
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{config_path}: invalid YAML: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: top level must be a mapping")

    raw = _expand_env(raw)

    defaults = dict(SITE_DEFAULTS)
    user_defaults = raw.get("defaults") or {}
    if not isinstance(user_defaults, dict):
        raise ConfigError("defaults: expected a mapping")
    unknown = set(user_defaults) - set(SITE_DEFAULTS)
    if unknown:
        raise ConfigError(f"defaults: unknown option(s): {sorted(unknown)}")
    defaults.update({k: v for k, v in user_defaults.items() if v is not None})

    # An empty `sites:` is the legitimate starting point - `webwatcher pick` fills it.
    # A missing key is almost always a typo, so that stays an error.
    if "sites" not in raw:
        raise ConfigError(
            "Die Konfiguration hat keinen 'sites:'-Block (Vorlage: config.example.yaml)"
        )
    sites_raw = raw.get("sites") or []
    if not isinstance(sites_raw, list):
        raise ConfigError("sites: expected a list")
    sites = [_build_site(item, defaults, i) for i, item in enumerate(sites_raw)]

    seen: dict[str, str] = {}
    for site in sites:
        if site.key in seen:
            raise ConfigError(
                f"duplicate site key {site.key!r} (used by {seen[site.key]!r} and {site.name!r}); "
                "give one of them an explicit 'key:'"
            )
        seen[site.key] = site.name

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

    storage = raw.get("storage") or {}
    if not isinstance(storage, dict):
        raise ConfigError("storage: expected a mapping")
    base = config_path.parent
    db_path = Path(str(storage.get("db_path") or "data/watcher.sqlite3")).expanduser()
    screenshot_dir = Path(str(storage.get("screenshot_dir") or "data/screenshots")).expanduser()

    return Config(
        sites=sites,
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
    )
