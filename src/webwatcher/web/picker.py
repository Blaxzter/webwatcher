"""Element-Picker für das Web-UI.

Der Picker der Kommandozeile öffnet ein sichtbares Chromium auf dem eigenen
Rechner. Auf dem Server geht das nicht - also andersherum: der Server hält eine
Seite headless offen, schickt Screenshots ins Browserfenster, und ein Klick
darauf kommt als Koordinate zurück. `document.elementFromPoint()` macht daraus
wieder ein Element, `__wwBuildSelector()` aus selector.js den Selektor. Es ist
dieselbe Selektor-Logik wie beim CLI-Picker, nur ferngesteuert.

Koordinaten sind immer CSS-Pixel im sichtbaren Ausschnitt - genau das, was
elementFromPoint erwartet. Das Umrechnen von der angezeigten Bildgröße erledigt
das Frontend.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any

from playwright.async_api import Error as PlaywrightError

from webwatcher.browser import Renderer
from webwatcher.config import SiteConfig
from webwatcher.picker import SELECTOR_JS, stability_preview

log = logging.getLogger(__name__)

# Eine vergessene Sitzung hält sonst dauerhaft einen Browser-Kontext offen.
SESSION_TTL = 900.0
MAX_SESSIONS = 4

# Liefert Selektor, Trefferzahl und die Rechtecke aller Treffer - daraus malt
# das Frontend die Markierungen über den Screenshot.
_PICK_JS = """
([x, y, broad]) => {
    const el = document.elementFromPoint(x, y);
    if (!el || el === document.documentElement || el === document.body) return null;
    let selector = null;
    if (broad) selector = window.__wwBroadSelector(el);
    if (!selector) selector = window.__wwBuildSelector(el);
    if (!selector) return null;
    return window.__wwDescribe(selector, el);
}
"""

_DESCRIBE_JS = """
(selector) => window.__wwDescribe(selector, null)
"""

# Gemeinsam genutzt von Klick und Selektor-Vorschau.
_HELPER_JS = """
(() => {
  window.__wwDescribe = (selector, el) => {
    let nodes = [];
    try {
      nodes = Array.from(document.querySelectorAll(selector));
    } catch (err) {
      return { selector, error: 'ungültiger Selektor', matches: 0, rects: [] };
    }
    const rects = nodes.slice(0, 60).map((node) => {
      const r = node.getBoundingClientRect();
      return { top: r.top, left: r.left, width: r.width, height: r.height };
    }).filter((r) => r.width > 0 || r.height > 0);
    const first = el || nodes[0] || null;
    const text = first ? (first.innerText || first.textContent || '').replace(/\\s+/g, ' ').trim() : '';
    return {
      selector,
      matches: nodes.length,
      rects,
      text: text.slice(0, 160),
      // Positionsbasierte Selektoren brechen, sobald sich die Seite umbaut.
      fragile: selector.includes(':nth-child'),
    };
  };
})();
"""


class PickerError(RuntimeError):
    """Die Sitzung existiert nicht (mehr) oder die Seite ist weggebrochen."""


class PickerSession:
    """Eine offene Seite, auf die geklickt werden kann."""

    def __init__(self, session_id: str, site: SiteConfig, renderer: Renderer) -> None:
        self.id = session_id
        self.site = site
        self.renderer = renderer
        self.touched = time.monotonic()
        self.shot_version = 0
        self.shot: bytes | None = None
        self._context: Any = None
        self._page: Any = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._context = await self.renderer.new_context(self.site, bypass_csp=True)
        script = SELECTOR_JS.read_text(encoding="utf-8") + "\n" + _HELPER_JS
        await self._context.add_init_script(script)
        self._page = await self._context.new_page()

        await self._page.goto(
            self.site.url, wait_until=self.site.wait_until, timeout=self.site.timeout_ms
        )
        # add_init_script greift erst ab der nächsten Navigation.
        if not await self._page.evaluate("() => Boolean(window.__wwSelectorLoaded)"):
            await self._page.evaluate(script)

        if self.site.wait_for:
            try:
                await self._page.wait_for_selector(self.site.wait_for, timeout=self.site.timeout_ms)
            except PlaywrightError:
                log.debug("%s: wait_for kam nicht - der Picker macht trotzdem weiter", self.id)
        if self.site.settle_ms:
            await self._page.wait_for_timeout(self.site.settle_ms)
        if self.site.js:
            try:
                await self._page.evaluate(self.site.js)
            except PlaywrightError as exc:
                log.warning("%s: eigenes JS im Picker fehlgeschlagen: %s", self.id, exc)

        await self._capture()

    async def close(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except PlaywrightError:
                pass
        self._context = None
        self._page = None

    # -- Aktionen -----------------------------------------------------------

    def _alive(self) -> Any:
        if self._page is None or self._page.is_closed():
            raise PickerError("Die Picker-Sitzung ist abgelaufen.")
        self.touched = time.monotonic()
        return self._page

    async def _capture(self) -> None:
        page = self._page
        if page is None:
            return
        self.shot = await page.screenshot(timeout=self.site.timeout_ms)
        self.shot_version += 1

    async def view(self, recapture: bool = False) -> dict[str, Any]:
        page = self._alive()
        async with self._lock:
            if recapture or self.shot is None:
                await self._capture()
            metrics = await page.evaluate(
                """() => ({
                    scrollY: Math.round(window.scrollY),
                    pageHeight: Math.round(document.documentElement.scrollHeight),
                    viewportHeight: Math.round(window.innerHeight),
                    viewportWidth: Math.round(window.innerWidth),
                    url: location.href,
                })"""
            )
        return {
            "session": self.id,
            "shot_version": self.shot_version,
            "scroll_y": metrics["scrollY"],
            "page_height": metrics["pageHeight"],
            "viewport": {
                "width": metrics["viewportWidth"],
                "height": metrics["viewportHeight"],
            },
            "url": metrics["url"],
        }

    async def pick(self, x: float, y: float, broad: bool = False) -> dict[str, Any] | None:
        page = self._alive()
        async with self._lock:
            return await page.evaluate(_PICK_JS, [x, y, broad])

    async def describe(self, selector: str) -> dict[str, Any]:
        """Was würde dieser Selektor gerade treffen? Für handgetippte Eingaben."""
        page = self._alive()
        async with self._lock:
            return await page.evaluate(_DESCRIBE_JS, selector)

    async def scroll_to(self, y: float) -> dict[str, Any]:
        page = self._alive()
        async with self._lock:
            await page.evaluate("(y) => window.scrollTo(0, y)", y)
            # Lazy-Loading braucht einen Moment, sonst ist der Screenshot leer.
            await page.wait_for_timeout(250)
            await self._capture()
        return await self.view()

    async def reload(self) -> dict[str, Any]:
        page = self._alive()
        async with self._lock:
            await page.reload(wait_until=self.site.wait_until, timeout=self.site.timeout_ms)
            if self.site.settle_ms:
                await page.wait_for_timeout(self.site.settle_ms)
            await self._capture()
        return await self.view()


class PickerRegistry:
    """Hält die offenen Sitzungen und räumt vergessene wieder weg."""

    def __init__(self, renderer: Renderer) -> None:
        self.renderer = renderer
        self._sessions: dict[str, PickerSession] = {}

    async def create(self, site: SiteConfig) -> PickerSession:
        await self.sweep()
        if len(self._sessions) >= MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda s: s.touched)
            await self.drop(oldest.id)

        session = PickerSession(secrets.token_urlsafe(9), site, self.renderer)
        try:
            await session.start()
        except PlaywrightError:
            await session.close()
            raise
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> PickerSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise PickerError("Die Picker-Sitzung ist abgelaufen.")
        return session

    async def drop(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.close()

    async def sweep(self) -> None:
        now = time.monotonic()
        for session_id, session in list(self._sessions.items()):
            if now - session.touched > SESSION_TTL:
                log.debug("picker session %s abgelaufen", session_id)
                await self.drop(session_id)

    async def close_all(self) -> None:
        for session_id in list(self._sessions):
            await self.drop(session_id)

    async def preview(self, site: SiteConfig) -> dict[str, Any]:
        """Zweimal headless rendern - dieselbe Prüfung wie im CLI-Picker."""
        return await stability_preview(self.renderer, site)
