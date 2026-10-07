"""Hetzner Cloud: ist ein Servertyp an einem Standort bestellbar?

Die Console zeigt das als "Preselected server type is not available". Dahinter
steht die öffentliche Cloud-API: `GET /v1/server_types?name=cx53` liefert pro
Standort `available`. Gebraucht wird dafür ein Projekt-Token (Lesen genügt) -
kein Login, keine 2FA, kein Browser. Der Token kommt aus HCLOUD_TOKEN und
landet damit weder in der Datenbank noch im Web-UI.

Das Ergebnis ist ein kurzer Text, eine Zeile je Standort. So läuft der Rest
(Hash, Diff, Verlauf, Fehlerzählung) genau wie bei einer Webseite.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import httpx

from webwatcher.browser import RenderError, RenderResult
from webwatcher.config import SiteConfig

API_URL = "https://api.hetzner.cloud/v1/server_types"
TOKEN_ENV = "HCLOUD_TOKEN"

LOCATION_NAMES = {
    "fsn1": "Falkenstein",
    "nbg1": "Nürnberg",
    "hel1": "Helsinki",
    "ash": "Ashburn",
    "hil": "Hillsboro",
    "sin": "Singapur",
}

AVAILABLE = "verfügbar"
SOLD_OUT = "ausverkauft"
NOT_OFFERED = "nicht angeboten"
DISCONTINUED = "eingestellt"

_SEPARATOR = ": "


def location_label(code: str) -> str:
    name = LOCATION_NAMES.get(code)
    return f"{name} ({code})" if name else code


def _status(entry: dict | None, now: datetime) -> str:
    if entry is None:
        return NOT_OFFERED
    deprecation = entry.get("deprecation") or {}
    unavailable_after = deprecation.get("unavailable_after")
    if unavailable_after:
        try:
            if datetime.fromisoformat(unavailable_after.replace("Z", "+00:00")) <= now:
                return DISCONTINUED
        except ValueError:
            pass
    return AVAILABLE if entry.get("available") else SOLD_OUT


def describe(server_type: dict, locations: list[str]) -> str:
    """Eine Zeile je Standort, z.B. 'CX53 Nürnberg (nbg1): ausverkauft'."""
    offered = {str(item.get("name")): item for item in server_type.get("locations") or []}
    wanted = locations or sorted(offered)
    title = str(server_type.get("description") or server_type.get("name") or "").upper()
    now = datetime.now(timezone.utc)
    return "\n".join(
        f"{title} {location_label(code)}{_SEPARATOR}{_status(offered.get(code), now)}"
        for code in wanted
    )


def parse(content: str | None) -> dict[str, str]:
    """Umkehrung von describe(): Zeile -> Status, Schlüssel ist die ganze Zeile vor ':'."""
    result: dict[str, str] = {}
    for line in (content or "").splitlines():
        head, sep, status = line.rpartition(_SEPARATOR)
        if sep:
            result[head] = status
    return result


def changes(old: str | None, new: str) -> tuple[list[str], list[str]]:
    """Was ist bestellbar geworden, was nicht mehr? Liefert die Zeilenköpfe."""
    before, after = parse(old), parse(new)
    opened, closed = [], []
    for head, status in after.items():
        was = before.get(head) == AVAILABLE
        if status == AVAILABLE and not was:
            opened.append(head)
        elif status != AVAILABLE and was:
            closed.append(head)
    return opened, closed


async def fetch(site: SiteConfig) -> RenderResult:
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise RenderError(
            f"{TOKEN_ENV} fehlt - API-Token in der Hetzner Console anlegen "
            "(Projekt > Sicherheit > API-Tokens, Lesen genügt) und in die .env eintragen"
        )

    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=site.timeout_ms / 1000) as client:
            response = await client.get(
                API_URL,
                params={"name": site.server_type},
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise RenderError(f"Hetzner-API nicht erreichbar: {exc}") from exc

    if response.status_code == 401:
        raise RenderError(f"Hetzner-API lehnt den Token ab (HTTP 401) - {TOKEN_ENV} prüfen")
    if response.status_code >= 400:
        raise RenderError(f"Hetzner-API: HTTP {response.status_code}: {response.text[:200]}")

    try:
        server_types = response.json()["server_types"]
    except (ValueError, KeyError, TypeError):
        raise RenderError("Hetzner-API: unerwartete Antwort") from None
    if not server_types:
        raise RenderError(f"Servertyp {site.server_type!r} gibt es bei Hetzner nicht")

    return RenderResult(
        text=describe(server_types[0], site.locations),
        screenshot=None,
        http_status=response.status_code,
        final_url=site.url,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
