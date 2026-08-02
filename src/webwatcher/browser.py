"""Playwright rendering: load a page, screenshot it, extract comparable content."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from playwright.async_api import (
    Browser,
    Error as PlaywrightError,
    Page,
    Playwright,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from webwatcher.config import SiteConfig

log = logging.getLogger(__name__)

LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--no-first-run",
    "--no-default-browser-check",
]

# Runs inside the page: strips ignored elements, then returns the comparable content.
_EXTRACT_JS = """
(opts) => {
    for (const selector of opts.remove) {
        try {
            document.querySelectorAll(selector).forEach((el) => el.remove());
        } catch (err) {
            /* invalid selector - skip it rather than failing the whole check */
        }
    }

    // A class selector usually matches several elements - compare all of them,
    // otherwise a list would be monitored by its first entry only.
    const roots = opts.selector
        ? Array.from(document.querySelectorAll(opts.selector))
        : [document.body];
    if (!roots.length) return null;

    // Some pages encode their state only in attributes - a calendar day that
    // turns from "daySoldOut" to "dayAvailable" keeps exactly the same text.
    // One line per element carrying a tracked attribute makes that visible
    // without dragging the whole markup into the diff.
    const annotate = (root, attrs) => {
        const lines = [];
        for (const el of root.querySelectorAll('*')) {
            const found = [];
            for (const attr of attrs) {
                const value = el.getAttribute(attr);
                if (value !== null && value !== '') found.push(attr + '=' + value.trim());
            }
            if (!found.length) continue;
            const ownText = Array.from(el.childNodes)
                .filter((node) => node.nodeType === 3)
                .map((node) => node.textContent)
                .join(' ')
                .replace(/\\s+/g, ' ')
                .trim()
                .slice(0, 80);
            lines.push(found.join(' ') + (ownText ? ' | ' + ownText : ''));
        }
        return lines;
    };

    const parts = [];
    for (const root of roots) {
        root.querySelectorAll('script, style, noscript, template, svg').forEach((el) => el.remove());
        parts.push(opts.mode === 'html' ? root.innerHTML : root.innerText || root.textContent || '');
        if (opts.attributes && opts.attributes.length) {
            parts.push(annotate(root, opts.attributes).join('\\n'));
        }
    }

    return { text: parts.join('\\n'), matches: roots.length };
}
"""


class RenderError(RuntimeError):
    """A page could not be rendered (network, timeout, missing selector, ...)."""


@dataclass
class RenderResult:
    text: str
    screenshot: bytes | None
    http_status: int | None
    final_url: str
    duration_ms: int
    match_count: int = 1
    """How many elements the selector matched (1 when watching the whole page)."""


class Renderer:
    """Owns one long-lived Chromium instance; one fresh context per check."""

    def __init__(self, playwright: Playwright | None = None) -> None:
        # A borrowed playwright instance (from the picker) must not be stopped here.
        self._playwright: Playwright | None = playwright
        self._owns_playwright = playwright is None
        self._browser: Browser | None = None

    async def start(self) -> None:
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        await self._ensure_browser()

    async def close(self) -> None:
        if self._browser is not None:
            try:
                await self._browser.close()
            except PlaywrightError:
                pass
            self._browser = None
        if self._playwright is not None and self._owns_playwright:
            await self._playwright.stop()
            self._playwright = None

    async def __aenter__(self) -> Renderer:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _ensure_browser(self) -> Browser:
        """Relaunch transparently if a previous crash killed the browser."""
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            log.debug("launching chromium")
            self._browser = await self._playwright.chromium.launch(headless=True, args=LAUNCH_ARGS)
        return self._browser

    async def render(self, site: SiteConfig) -> RenderResult:
        started = time.monotonic()
        browser = await self._ensure_browser()

        context = await browser.new_context(
            viewport=site.viewport,
            user_agent=site.user_agent,
            locale=site.locale,
            timezone_id=site.timezone,
            extra_http_headers=site.headers or None,
            ignore_https_errors=site.ignore_https_errors,
        )
        context.set_default_timeout(site.timeout_ms)

        try:
            if site.block_resources:
                blocked = set(site.block_resources)

                async def _route(route: Route) -> None:
                    if route.request.resource_type in blocked:
                        await route.abort()
                    else:
                        await route.continue_()

                await context.route("**/*", _route)

            page = await context.new_page()

            try:
                response = await page.goto(
                    site.url, wait_until=site.wait_until, timeout=site.timeout_ms
                )
            except PlaywrightTimeoutError:
                raise RenderError(
                    f"Timeout beim Laden nach {site.timeout_ms / 1000:.0f}s"
                ) from None
            except PlaywrightError as exc:
                raise RenderError(f"Navigation fehlgeschlagen: {_clean(exc)}") from exc

            status = response.status if response else None
            if status is not None and status >= 400:
                raise RenderError(f"HTTP {status}")

            if site.wait_for:
                try:
                    await page.wait_for_selector(site.wait_for, timeout=site.timeout_ms)
                except PlaywrightTimeoutError:
                    raise RenderError(f"Selektor {site.wait_for!r} ist nicht erschienen") from None

            if site.settle_ms:
                await page.wait_for_timeout(site.settle_ms)

            if site.js:
                try:
                    await page.evaluate(site.js)
                except PlaywrightError as exc:
                    raise RenderError(f"Eigenes JS ist fehlgeschlagen: {_clean(exc)}") from exc

            # Screenshot first: it should show the real page, before we strip nodes.
            screenshot = await self._screenshot(page, site)

            content = await page.evaluate(
                _EXTRACT_JS,
                {
                    "remove": site.ignore_selectors,
                    "selector": site.selector,
                    "mode": site.mode,
                    "attributes": site.track_attributes,
                },
            )
            if content is None:
                raise RenderError(f"Selektor {site.selector!r} wurde nicht gefunden")

            return RenderResult(
                text=str(content.get("text", "")),
                screenshot=screenshot,
                http_status=status,
                final_url=page.url,
                duration_ms=int((time.monotonic() - started) * 1000),
                match_count=int(content.get("matches", 1)),
            )
        finally:
            try:
                await context.close()
            except PlaywrightError:
                pass

    async def _resolve_auto(self, page: Page, site: SiteConfig) -> str:
        """'auto': show what is actually being watched.

        One matched element -> that element. Several (a list) -> the whole page,
        because a single card would hide the very change you are looking for.
        No selector -> the viewport.
        """
        if not site.selector:
            return "viewport"
        try:
            count = await page.evaluate(
                "sel => document.querySelectorAll(sel).length", site.selector
            )
        except PlaywrightError:
            return "viewport"
        return "element" if count == 1 else "full_page"

    async def _screenshot(self, page: Page, site: SiteConfig) -> bytes | None:
        """Never fail a check just because the screenshot did not work."""
        if site.screenshot == "none":
            return None
        mode = site.screenshot
        if mode == "auto":
            mode = await self._resolve_auto(page, site)
        try:
            if mode == "element":
                return await page.locator(site.selector or "body").first.screenshot(
                    timeout=site.timeout_ms
                )
            return await page.screenshot(full_page=mode == "full_page", timeout=site.timeout_ms)
        except PlaywrightError as exc:
            log.warning("screenshot failed for %s: %s", site.name, _clean(exc))
            # An element can be hidden or zero-sized; a page shot still helps.
            if mode == "element":
                try:
                    return await page.screenshot(timeout=site.timeout_ms)
                except PlaywrightError:
                    return None
            return None


def _clean(exc: BaseException) -> str:
    """Playwright errors carry a long call-log tail; keep the first line only."""
    return str(exc).strip().splitlines()[0][:300]
