"""Check orchestration: render, compare, notify, schedule."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from webwatcher.browser import Renderer
from webwatcher.compare import Diff, content_hash, make_diff, normalize
from webwatcher.config import Config, SiteConfig
from webwatcher.notify.telegram import TelegramError, TelegramNotifier
from webwatcher.sites import SiteRepository
from webwatcher.store import Store, from_iso, utcnow

log = logging.getLogger(__name__)

STATUS_BASELINE = "baseline"
STATUS_UNCHANGED = "unchanged"
STATUS_CHANGED = "changed"
STATUS_MINOR = "minor"
STATUS_ERROR = "error"


@dataclass
class Outcome:
    site: SiteConfig
    status: str
    diff: Diff | None = None
    error: str | None = None
    duration_ms: int = 0
    notified: bool = False

    def describe(self) -> str:
        if self.status == STATUS_CHANGED and self.diff:
            return f"geändert ({self.diff.summary()})"
        if self.status == STATUS_MINOR and self.diff:
            return f"minimale Änderung ignoriert ({self.diff.summary()})"
        return {
            STATUS_BASELINE: "Baseline gespeichert",
            STATUS_UNCHANGED: "unverändert",
            STATUS_ERROR: f"Fehler: {self.error}",
        }.get(self.status, self.status)


class Runner:
    def __init__(
        self,
        config: Config,
        store: Store,
        renderer: Renderer,
        notifier: TelegramNotifier | None,
        repo: SiteRepository | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.renderer = renderer
        self.notifier = notifier
        self.repo = repo or SiteRepository(store, config.site_defaults, config.screenshot_dir)
        self._stop = asyncio.Event()
        # Gesetzt, wenn sich von aussen etwas geändert hat (Web-UI): weckt die
        # Schleife sofort, statt bis zu einer Minute zu warten.
        self._wake = asyncio.Event()
        self._semaphore = asyncio.Semaphore(config.concurrency)
        # Eine Seite nie zweimal gleichzeitig prüfen: seit das Web-UI Checks
        # auslösen kann, kann ein Klick mit dem fälligen Lauf zusammenfallen.
        # Beide läsen dann denselben (noch leeren) Zustand und legten je eine
        # Baseline an - also zwei Meldungen für dieselbe Seite.
        self._site_locks: dict[str, asyncio.Lock] = {}

    def request_stop(self) -> None:
        self._stop.set()

    def wake(self) -> None:
        """Neue oder geänderte Seite - Schleife soll jetzt nachsehen."""
        self._wake.set()

    # -- single check -------------------------------------------------------

    async def check_site(self, site: SiteConfig, notify: bool = True) -> Outcome:
        started = time.monotonic()
        self.store.ensure_site(site.key, site.name, site.url)
        state = self.store.get_state(site.key)

        try:
            result = await self.renderer.render(site)
        except Exception as exc:  # noqa: BLE001 - any render failure is just a failed check
            # CancelledError derives from BaseException, so shutdown still propagates.
            message = str(exc).strip().splitlines()[0][:500] or exc.__class__.__name__
            elapsed = int((time.monotonic() - started) * 1000)
            return await self._handle_failure(site, message, elapsed, notify)

        lines = normalize(result.text, site.ignore_patterns)
        new_hash = content_hash(lines)
        content = "\n".join(lines)
        screenshot_path = self._save_screenshot(site, result.screenshot)
        path_str = str(screenshot_path) if screenshot_path else None

        # Recovering from a run of failures that we already alerted about.
        if notify and state and state["error_notified"]:
            await self._safe_notify(self.notifier and self.notifier.send_recovery(site))

        previous_hash = state["content_hash"] if state else None
        previous_content = state["content"] if state else None

        if not previous_hash or previous_content is None:
            self.store.save_snapshot(site.key, content, new_hash, path_str, changed=True)
            self.store.record_check(
                site.key,
                STATUS_BASELINE,
                content_hash=new_hash,
                duration_ms=result.duration_ms,
                http_status=result.http_status,
                screenshot_path=path_str,
            )
            notified = False
            if notify and site.notify_first_check and self.notifier:
                notified = await self._safe_notify(
                    self.notifier.send_baseline(site, result.screenshot, result.final_url)
                )
            return Outcome(site, STATUS_BASELINE, duration_ms=result.duration_ms, notified=notified)

        if new_hash == previous_hash:
            self.store.touch_checked(site.key, path_str)
            self.store.record_check(
                site.key,
                STATUS_UNCHANGED,
                content_hash=new_hash,
                duration_ms=result.duration_ms,
                http_status=result.http_status,
            )
            return Outcome(site, STATUS_UNCHANGED, duration_ms=result.duration_ms)

        diff = make_diff(previous_content.split("\n"), lines, site.max_diff_lines)

        # Below the threshold: adopt the new content silently so it does not
        # re-trigger on every single run.
        if diff.total < site.min_changed_lines:
            self.store.save_snapshot(site.key, content, new_hash, path_str, changed=False)
            self.store.record_check(
                site.key,
                STATUS_MINOR,
                content_hash=new_hash,
                added_lines=diff.added,
                removed_lines=diff.removed,
                duration_ms=result.duration_ms,
                http_status=result.http_status,
            )
            return Outcome(site, STATUS_MINOR, diff=diff, duration_ms=result.duration_ms)

        self.store.save_snapshot(site.key, content, new_hash, path_str, changed=True)
        self.store.record_check(
            site.key,
            STATUS_CHANGED,
            content_hash=new_hash,
            added_lines=diff.added,
            removed_lines=diff.removed,
            duration_ms=result.duration_ms,
            http_status=result.http_status,
            screenshot_path=path_str,
        )

        notified = False
        if notify and self.notifier:
            notified = await self._safe_notify(
                self.notifier.send_change(site, diff, result.screenshot, result.final_url)
            )
        return Outcome(
            site, STATUS_CHANGED, diff=diff, duration_ms=result.duration_ms, notified=notified
        )

    async def _handle_failure(
        self, site: SiteConfig, message: str, elapsed_ms: int, notify: bool
    ) -> Outcome:
        failures = self.store.record_failure(site.key, message)
        self.store.record_check(site.key, STATUS_ERROR, duration_ms=elapsed_ms, error=message)
        log.warning("%s: check failed (%dx): %s", site.name, failures, message)

        state = self.store.get_state(site.key)
        already_notified = bool(state["error_notified"]) if state else False
        notified = False
        if (
            notify
            and self.notifier
            and failures >= site.notify_on_error_after
            and not already_notified
        ):
            notified = await self._safe_notify(self.notifier.send_error(site, message, failures))
            if notified:
                self.store.mark_error_notified(site.key)

        return Outcome(site, STATUS_ERROR, error=message, duration_ms=elapsed_ms, notified=notified)

    async def _safe_notify(self, coro) -> bool:
        """A broken notification must never abort or crash a check."""
        if coro is None:
            return False
        try:
            await coro
            return True
        except TelegramError as exc:
            log.error("telegram: %s", exc)
        except Exception as exc:  # noqa: BLE001 - defensive, notifications are best effort
            log.error("notification failed: %s", exc)
        return False

    # -- screenshots --------------------------------------------------------

    def _save_screenshot(self, site: SiteConfig, data: bytes | None) -> Path | None:
        if not data or self.config.keep_screenshots == 0:
            return None
        directory = self.config.screenshot_dir / site.key
        directory.mkdir(parents=True, exist_ok=True)
        # Milliseconds: two checks within the same second would otherwise
        # silently overwrite each other and keep_screenshots would keep fewer.
        path = directory / f"{utcnow().strftime('%Y%m%d-%H%M%S-%f')[:-3]}.png"
        path.write_bytes(data)
        self._prune_screenshots(directory)
        return path

    def _prune_screenshots(self, directory: Path) -> None:
        keep = self.config.keep_screenshots
        files = sorted(directory.glob("*.png"))
        for stale in files[:-keep] if keep > 0 else []:
            try:
                stale.unlink()
            except OSError:
                pass

    # -- scheduling ---------------------------------------------------------

    def _next_due(self, site: SiteConfig) -> datetime:
        now = utcnow()
        interval = site.interval_at(now)
        # Der Jitter streut die Seiten gegeneinander, darf ein 1-Minuten-Fenster
        # aber nicht verwässern - daher an das Intervall gekoppelt.
        spread = min(self.config.jitter_seconds, interval / 4)
        due = now + timedelta(seconds=interval + (random.uniform(0, spread) if spread > 0 else 0))

        # Faengt ein Fenster vorher an (oder hoert auf), dort aufwachen: sonst
        # verschluckt der laufende 15m-Takt den Anfang des Fensters.
        boundary = site.next_window_change(now)
        return min(due, boundary) if boundary else due

    def _site_lock(self, key: str) -> asyncio.Lock:
        lock = self._site_locks.get(key)
        if lock is None:
            lock = self._site_locks[key] = asyncio.Lock()
        return lock

    async def check_once(self, sites: list[SiteConfig], notify: bool = True) -> list[Outcome]:
        async def guarded(site: SiteConfig) -> Outcome:
            # Erst die Seite belegen, dann einen Platz nehmen - andersherum
            # würde ein wartender Doppellauf einen Slot blockieren.
            async with self._site_lock(site.key), self._semaphore:
                window = site.active_window(utcnow())
                outcome = await self.check_site(site, notify=notify)
                self.store.set_next_due(site.key, self._next_due(site))
                log.info(
                    "%s: %s (%dms)%s",
                    site.name,
                    outcome.describe(),
                    outcome.duration_ms,
                    f" [Fenster {window.describe()}]" if window else "",
                )
                return outcome

        return list(await asyncio.gather(*(guarded(site) for site in sites)))

    def _announce(self, sites: list[SiteConfig]) -> None:
        """Nach jeder Änderung an der Seitenliste einmal den Stand loggen."""
        if not sites:
            log.warning("keine aktiven Seiten - der Watcher wartet auf Konfiguration")
            return
        log.info(
            "watching %d site(s), concurrency=%d, db=%s",
            len(sites),
            self.config.concurrency,
            self.config.db_path,
        )
        for site in sites:
            self.store.ensure_site(site.key, site.name, site.url)
            if site.interval_windows:
                log.info(
                    "%s: Zeitfenster (%s): %s",
                    site.name,
                    site.timezone,
                    "; ".join(window.describe() for window in site.interval_windows),
                )

    async def run_forever(self) -> None:
        last_prune = utcnow()
        known_revision = -1
        sites: list[SiteConfig] = []

        while not self._stop.is_set():
            # Die Seiten kommen aus der Datenbank, nicht aus einer Momentaufnahme
            # beim Start: was das Web-UI ändert, gilt ab dem nächsten Durchlauf.
            revision = self.repo.revision()
            if revision != known_revision:
                sites = self.repo.enabled_sites()
                known_revision = revision
                self._announce(sites)

            now = utcnow()
            due: list[SiteConfig] = []
            for site in sites:
                state = self.store.get_state(site.key)
                next_due = from_iso(state["next_due_at"]) if state else None
                if next_due is None or next_due <= now:
                    due.append(site)

            if due:
                await self.check_once(due, notify=True)

            if (utcnow() - last_prune) > timedelta(hours=12):
                removed = self.store.prune_history(self.config.history_days)
                log.debug("pruned %d history rows", removed)
                last_prune = utcnow()

            await self._sleep_until_next(sites)

        log.info("watcher stopped")

    async def _sleep_until_next(self, sites: list[SiteConfig]) -> None:
        """Sleep until the earliest due date, but stay responsive to shutdown."""
        now = utcnow()
        waits = []
        for site in sites:
            state = self.store.get_state(site.key)
            next_due = from_iso(state["next_due_at"]) if state else None
            waits.append((next_due - now).total_seconds() if next_due else 0.0)

        delay = min(waits) if waits else 60.0
        delay = max(1.0, min(delay, 60.0))

        self._wake.clear()
        waiters = [
            asyncio.ensure_future(self._stop.wait()),
            asyncio.ensure_future(self._wake.wait()),
        ]
        try:
            await asyncio.wait(waiters, timeout=delay, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for waiter in waiters:
                waiter.cancel()
