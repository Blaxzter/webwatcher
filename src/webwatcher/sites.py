"""Die Seiten leben in der Datenbank, nicht in der config.yaml.

Gespeichert wird das rohe Mapping (genau die Form, die auch unter `sites:` in
der YAML stünde) als JSON. Gebaut wird daraus mit `site_from_mapping()` - der
gleichen Funktion, die der YAML-Loader und der Picker benutzen. Damit gibt es
weiterhin genau eine Stelle, die eine Seitenkonfiguration validiert, und das
Web-UI kann nichts speichern, was der Daemon nicht auch lesen könnte.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from webwatcher.config import (
    SITE_DEFAULTS,
    ConfigError,
    SiteConfig,
    _expand_env,
    _slugify,
    site_from_mapping,
)
from webwatcher.store import Store

log = logging.getLogger(__name__)

# Was in einem Mapping stehen darf. Alles andere ist ein Tippfehler und wird
# abgelehnt, statt still zu verpuffen.
ALLOWED_KEYS = set(SITE_DEFAULTS) | {"name", "url", "key"}


def build_site(mapping: dict[str, Any], defaults: dict[str, Any]) -> SiteConfig:
    """Mapping -> SiteConfig, mit aufgelösten ${VARIABLEN}.

    Aufgelöst wird erst hier, nicht beim Speichern: in der Datenbank steht
    weiter `${TOKEN}` und nicht der Token. Sonst würde ein Header mit einem
    Geheimnis beim ersten Speichern über das Web-UI im Klartext landen.
    """
    return site_from_mapping(_expand_env(mapping), defaults)


@dataclass
class SiteEntry:
    """Eine Zeile aus site_configs - gültig oder nicht.

    Ungültige Einträge verschwinden nicht: der Daemon überspringt sie, das UI
    zeigt sie mit ihrem Fehler an, damit man sie reparieren kann.
    """

    key: str
    mapping: dict[str, Any]
    site: SiteConfig | None
    error: str | None
    position: int
    updated_at: str | None = None
    updated_by: str | None = None

    @property
    def valid(self) -> bool:
        return self.site is not None

    @property
    def name(self) -> str:
        if self.site:
            return self.site.name
        return str(self.mapping.get("name") or self.mapping.get("url") or self.key)


def clean_mapping(raw: dict[str, Any]) -> dict[str, Any]:
    """Unbekannte Schlüssel ablehnen, leere Angaben als 'nicht gesetzt' werten.

    Leere Listen bleiben erhalten: `ignore_patterns: []` ist die einzige
    Möglichkeit, einen globalen `defaults:`-Wert wieder abzuräumen.
    """
    if not isinstance(raw, dict):
        raise ConfigError("Eine Seite muss ein Mapping sein.")
    unknown = set(raw) - ALLOWED_KEYS
    if unknown:
        raise ConfigError(f"unbekannte Option(en): {sorted(unknown)}")
    return {
        key: value
        for key, value in raw.items()
        if value is not None and not (isinstance(value, str) and not value.strip())
    }


class SiteRepository:
    """Liest und schreibt Seitenkonfigurationen; cached anhand der Revision."""

    def __init__(
        self,
        store: Store,
        defaults: dict[str, Any] | None = None,
        screenshot_dir: Path | None = None,
    ) -> None:
        self.store = store
        self.defaults = dict(defaults or SITE_DEFAULTS)
        self.screenshot_dir = screenshot_dir
        self._revision = -1
        self._entries: list[SiteEntry] = []

    # -- lesen --------------------------------------------------------------

    def entries(self) -> list[SiteEntry]:
        """Alle Einträge, gültige wie kaputte, in ihrer Reihenfolge."""
        revision = self.store.sites_revision()
        if revision == self._revision:
            return self._entries

        entries: list[SiteEntry] = []
        for row in self.store.site_configs():
            entries.append(self._build_entry(row))
        self._entries = entries
        self._revision = revision
        return entries

    def _build_entry(self, row: Any) -> SiteEntry:
        key = str(row["key"])
        try:
            mapping = json.loads(row["data"])
            if not isinstance(mapping, dict):
                raise ValueError("kein Mapping")
        except (ValueError, TypeError) as exc:
            return SiteEntry(key, {}, None, f"beschädigter Datensatz: {exc}", row["position"])

        # Die Spalte gewinnt, damit Zeilenschlüssel und Mapping nie auseinanderlaufen.
        mapping["key"] = key
        try:
            site = build_site(mapping, self.defaults)
            error = None
        except ConfigError as exc:
            site, error = None, str(exc)
            log.warning("%s: ungültige Konfiguration, wird übersprungen (%s)", key, exc)
        return SiteEntry(
            key,
            mapping,
            site,
            error,
            int(row["position"]),
            row["updated_at"],
            row["updated_by"],
        )

    def sites(self) -> list[SiteConfig]:
        """Nur die gültigen - das ist die Liste, mit der der Runner arbeitet."""
        return [entry.site for entry in self.entries() if entry.site is not None]

    def enabled_sites(self) -> list[SiteConfig]:
        return [site for site in self.sites() if site.enabled]

    def get(self, key: str) -> SiteEntry | None:
        for entry in self.entries():
            if entry.key == key:
                return entry
        return None

    def revision(self) -> int:
        return self.store.sites_revision()

    # -- schreiben ----------------------------------------------------------

    def validate(self, mapping: dict[str, Any]) -> SiteConfig:
        """Baut die Seite testweise - wirft ConfigError mit der genauen Stelle."""
        return build_site(clean_mapping(mapping), self.defaults)

    def _unique_key(self, base: str, ignore: str | None = None) -> str:
        taken = {entry.key for entry in self.entries()} - ({ignore} if ignore else set())
        if base not in taken:
            return base
        for suffix in range(2, 1000):
            candidate = f"{base}-{suffix}"
            if candidate not in taken:
                return candidate
        raise ConfigError(f"kein freier Key für {base!r} zu finden")

    def save(
        self,
        raw: dict[str, Any],
        *,
        original_key: str | None = None,
        actor: str | None = None,
    ) -> SiteEntry:
        """Anlegen oder ändern. Validiert zuerst, schreibt nur bei Erfolg."""
        mapping = clean_mapping(raw)
        name = str(mapping.get("name") or mapping.get("url") or "").strip()
        explicit = str(mapping.get("key") or "").strip()

        if explicit:
            key = _slugify(explicit)
        elif original_key:
            # Umbenennen einer bestehenden Seite zieht den Key nicht mit: sonst
            # verlöre eine Tippkorrektur am Namen die Baseline.
            key = original_key
        else:
            key = self._unique_key(_slugify(name))

        if key != original_key and self.store.get_site_config(key) is not None:
            raise ConfigError(f"Es gibt bereits eine Seite mit dem Key {key!r}.")

        mapping["key"] = key
        site = build_site(mapping, self.defaults)  # wirft bei Fehlern

        if original_key and original_key != key:
            self.store.rename_site(original_key, key)
            self._move_screenshots(original_key, key)

        self.store.put_site_config(
            key,
            json.dumps(mapping, ensure_ascii=False, sort_keys=True),
            actor=actor,
        )
        self._revision = -1
        return SiteEntry(key, mapping, site, None, 0, updated_by=actor)

    def delete(self, key: str) -> bool:
        if self.store.get_site_config(key) is None:
            return False
        self.store.delete_site_config(key)
        self._drop_screenshots(key)
        self._revision = -1
        return True

    def reorder(self, keys: list[str]) -> None:
        known = {entry.key for entry in self.entries()}
        unknown = [key for key in keys if key not in known]
        if unknown:
            raise ConfigError(f"unbekannte Seite(n): {unknown}")
        self.store.set_positions(keys)
        self._revision = -1

    def import_mappings(
        self, mappings: list[dict[str, Any]], actor: str | None = None
    ) -> tuple[list[str], list[tuple[str, str]]]:
        """Seed aus der config.yaml. Vorhandene Keys bleiben unangetastet."""
        added: list[str] = []
        skipped: list[tuple[str, str]] = []
        for index, raw in enumerate(mappings):
            label = str((raw or {}).get("name") or (raw or {}).get("url") or f"sites[{index}]")
            try:
                mapping = clean_mapping(raw)
                key = _slugify(
                    str(mapping.get("key") or mapping.get("name") or mapping.get("url") or "")
                )
                if self.store.get_site_config(key) is not None:
                    skipped.append((label, f"Key {key!r} existiert bereits"))
                    continue
                entry = self.save(mapping, actor=actor)
                added.append(entry.key)
            except ConfigError as exc:
                skipped.append((label, str(exc)))
        return added, skipped

    # -- Screenshots folgen dem Key ----------------------------------------

    def _move_screenshots(self, old_key: str, new_key: str) -> None:
        if not self.screenshot_dir:
            return
        source = self.screenshot_dir / old_key
        target = self.screenshot_dir / new_key
        if not source.is_dir() or target.exists():
            return
        try:
            source.rename(target)
        except OSError as exc:
            log.warning("Screenshots von %s nach %s: %s", old_key, new_key, exc)

    def _drop_screenshots(self, key: str) -> None:
        if not self.screenshot_dir:
            return
        directory = self.screenshot_dir / key
        if directory.is_dir():
            shutil.rmtree(directory, ignore_errors=True)
