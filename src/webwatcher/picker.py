"""Interactive element picker: build a site config against the real render pipeline.

Opens a visible Chromium with the same settings the watcher uses, lets you click
elements, and validates the result by rendering headless twice - so what you pick
is what the server actually sees.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from playwright.async_api import Error as PlaywrightError, Playwright, async_playwright

from webwatcher.browser import LAUNCH_ARGS, Renderer
from webwatcher.compare import make_diff, normalize
from webwatcher.config import ConfigError, SiteConfig, site_from_mapping

log = logging.getLogger(__name__)

PICKER_JS = Path(__file__).with_name("picker.js")

_DIGIT_RUN = re.compile(r"\d+")
_ESCAPED_SPACE = re.compile(r"\\(\s)")
_MAX_PATTERN_CHARS = 160


class _BlockDumper(yaml.SafeDumper):
    """Indents list items under their key, like config.example.yaml does."""

    def increase_indent(self, flow: bool = False, indentless: bool = False):  # noqa: ARG002
        return super().increase_indent(flow, False)


def suggest_pattern(line: str) -> str:
    """Turn an unstable line into a forgiving regex (numbers become \\d+)."""
    text = line.strip()[:_MAX_PATTERN_CHARS]
    escaped = re.escape(text)
    # re.escape also escapes spaces; harmless but unreadable in a config file.
    escaped = _ESCAPED_SPACE.sub(r"\1", escaped)
    return _DIGIT_RUN.sub(r"\\d+", escaped)


@dataclass
class PickState:
    url: str
    name: str
    interval: str = "15m"
    selector: str | None = None
    ignore_selectors: list[str] = field(default_factory=list)
    ignore_patterns: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "name": self.name,
            "interval": self.interval,
            "selector": self.selector,
            "ignore_selectors": list(self.ignore_selectors),
            "ignore_patterns": list(self.ignore_patterns),
        }

    def as_site_mapping(self) -> dict[str, Any]:
        """The YAML shape - omit everything that is still at its default."""
        mapping: dict[str, Any] = {"name": self.name, "url": self.url, "interval": self.interval}
        if self.selector:
            mapping["selector"] = self.selector
        if self.ignore_selectors:
            mapping["ignore_selectors"] = list(self.ignore_selectors)
        if self.ignore_patterns:
            mapping["ignore_patterns"] = list(self.ignore_patterns)
        return mapping

    def as_yaml_block(self, indent: int = 2) -> str:
        dumped = yaml.dump(
            [self.as_site_mapping()],
            Dumper=_BlockDumper,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
            width=100,
        )
        pad = " " * indent
        return "".join(f"{pad}{line}\n" for line in dumped.rstrip("\n").splitlines())


class PickerSession:
    """Drives the headed picker browser and a headless renderer for previews."""

    def __init__(
        self,
        state: PickState,
        *,
        viewport: dict[str, int],
        user_agent: str,
        locale: str,
        timezone: str,
        profile: Path | None = None,
        headless: bool = False,
    ) -> None:
        self.state = state
        self.viewport = viewport
        self.user_agent = user_agent
        self.locale = locale
        self.timezone = timezone
        self.profile = profile
        self.headless = headless

        self._playwright: Playwright | None = None
        self._browser = None
        self._context = None
        self._page = None
        self._preview_renderer: Renderer | None = None
        self._result: asyncio.Future[str] | None = None
        self._last_suggestions: list[str] = []

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._result = asyncio.get_running_loop().create_future()

        context_args: dict[str, Any] = {
            "viewport": self.viewport,
            "user_agent": self.user_agent,
            "locale": self.locale,
            "timezone_id": self.timezone,
            # Injection must work on sites with a strict Content-Security-Policy.
            "bypass_csp": True,
        }

        if self.profile:
            # Persistent profile: log in once, reuse the session later.
            self.profile.mkdir(parents=True, exist_ok=True)
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile), headless=self.headless, args=LAUNCH_ARGS, **context_args
            )
        else:
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless, args=LAUNCH_ARGS
            )
            self._context = await self._browser.new_context(**context_args)

        for name, handler in (
            ("__wwState", lambda: self.state.as_dict()),
            ("__wwPick", self._on_pick),
            ("__wwRemove", self._on_remove),
            ("__wwPreview", self._on_preview),
            ("__wwAcceptPatterns", self._on_accept_patterns),
            ("__wwFinish", lambda: self._finish("finish")),
            ("__wwCancel", lambda: self._finish("cancel")),
        ):
            await self._context.expose_function(name, handler)

        script = PICKER_JS.read_text(encoding="utf-8")
        await self._context.add_init_script(script)

        page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._page = page
        # Closing the window is a cancel.
        page.on("close", lambda _p: self._finish("cancel"))

        await page.goto(self.state.url, wait_until="load", timeout=60000)
        # add_init_script only covers future navigations; cover the current one too.
        if not await page.evaluate("() => Boolean(window.__wwPickerLoaded)"):
            await page.evaluate(script)

    async def wait(self) -> str:
        assert self._result is not None
        return await self._result

    async def close(self) -> None:
        # The preview renderer borrows our playwright instance, so it goes first.
        if self._preview_renderer is not None:
            await self._preview_renderer.close()
            self._preview_renderer = None
        for closable in (self._context, self._browser):
            if closable is not None:
                try:
                    await closable.close()
                except PlaywrightError:
                    pass
        self._context = None
        self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    def _finish(self, outcome: str) -> None:
        if self._result is not None and not self._result.done():
            self._result.set_result(outcome)

    # -- bindings -----------------------------------------------------------

    def _on_pick(self, kind: str, selector: str) -> dict[str, Any]:
        if not selector:
            return self.state.as_dict()
        if kind == "ignore":
            if selector not in self.state.ignore_selectors:
                self.state.ignore_selectors.append(selector)
        else:
            self.state.selector = selector
        log.info("picked %s: %s", kind, selector)
        return self.state.as_dict()

    def _on_remove(self, kind: str, value: str) -> dict[str, Any]:
        if kind == "selector":
            self.state.selector = None
        elif kind == "ignore" and value in self.state.ignore_selectors:
            self.state.ignore_selectors.remove(value)
        elif kind == "pattern" and value in self.state.ignore_patterns:
            self.state.ignore_patterns.remove(value)
        return self.state.as_dict()

    def _on_accept_patterns(self) -> dict[str, Any]:
        for pattern in self._last_suggestions:
            if pattern not in self.state.ignore_patterns:
                self.state.ignore_patterns.append(pattern)
        self._last_suggestions = []
        return self.state.as_dict()

    async def _on_preview(self) -> dict[str, Any]:
        """Render twice headless and report content plus anything that moved."""
        try:
            site = self._temp_site()
        except ConfigError as exc:
            return {"ok": False, "error": str(exc)}

        if self._preview_renderer is None:
            self._preview_renderer = Renderer(playwright=self._playwright)
            await self._preview_renderer.start()

        try:
            first = await self._preview_renderer.render(site)
            second = await self._preview_renderer.render(site)
        except Exception as exc:  # noqa: BLE001 - surfaced in the panel
            return {"ok": False, "error": str(exc).strip().splitlines()[0][:300]}

        lines = normalize(first.text, site.ignore_patterns)
        later = normalize(second.text, site.ignore_patterns)
        diff = make_diff(lines, later, 20)
        unstable = sorted({entry[2:] for entry in diff.text.splitlines() if entry[:1] in "+-"})
        self._last_suggestions = [suggest_pattern(entry) for entry in unstable[:12]]

        return {
            "ok": True,
            "count": len(lines),
            "chars": sum(len(entry) for entry in lines),
            "sample": lines[:25],
            "unstable": unstable[:12],
            "matches": first.match_count,
        }

    def _temp_site(self) -> SiteConfig:
        # The preview only compares text; skipping the screenshot keeps it snappy.
        return site_from_mapping({**self.state.as_site_mapping(), "screenshot": "none"})


# -- orchestration ----------------------------------------------------------


def find_sites_indent(text: str) -> int | None:
    """Indentation of the existing `sites:` list items, so appends line up."""
    in_sites = False
    for line in text.splitlines():
        if re.match(r"^sites\s*:", line):
            in_sites = True
            continue
        if in_sites:
            if re.match(r"^\S", line):  # next top level key -> list ended
                break
            match = re.match(r"^(\s*)-\s", line)
            if match:
                return len(match.group(1))
    return None if not in_sites else 2


def append_site(config_path: Path, block: str) -> tuple[bool, str]:
    """Append a site block, but only if the file still parses and gained the entry."""
    original = config_path.read_text(encoding="utf-8")
    candidate = original.rstrip("\n") + "\n" + block

    try:
        before = yaml.safe_load(original) or {}
        after = yaml.safe_load(candidate) or {}
    except yaml.YAMLError as exc:
        return False, f"Ergebnis wäre kein gültiges YAML: {exc}"

    old_sites = before.get("sites") or []
    new_sites = after.get("sites") or []
    if not isinstance(new_sites, list) or len(new_sites) != len(old_sites) + 1:
        return False, "Der Block hätte sich nicht sauber in die sites-Liste eingefügt."

    config_path.write_text(candidate, encoding="utf-8")
    return True, ""


async def run_picker(
    url: str,
    *,
    name: str | None,
    interval: str,
    config,
    profile: Path | None,
    write: bool,
    headless: bool = False,
) -> int:
    from webwatcher.config import SITE_DEFAULTS

    defaults = SITE_DEFAULTS
    state = PickState(url=url, name=name or url, interval=interval)
    session = PickerSession(
        state,
        viewport=defaults["viewport"],
        user_agent=defaults["user_agent"],
        locale=defaults["locale"],
        timezone=defaults["timezone"],
        profile=profile,
        headless=headless,
    )

    try:
        await session.start()
    except PlaywrightError as exc:
        print(f"Seite konnte nicht geladen werden: {str(exc).splitlines()[0]}")
        await session.close()
        return 1

    print(f"Picker läuft für {url} - Elemente anklicken, dann 'Fertig'.")
    outcome = await session.wait()
    await session.close()

    if outcome != "finish":
        print("Abgebrochen, nichts geschrieben.")
        return 1

    block = state.as_yaml_block(indent=2)
    print("\n" + block)

    target = getattr(config, "source_path", None) if config else None
    if not write or target is None:
        if target is None and write:
            print("Keine nutzbare config.yaml gefunden - Block bitte selbst einfügen.")
        return 0

    indent = find_sites_indent(target.read_text(encoding="utf-8"))
    if indent is None:
        print(f"In {target} fehlt ein 'sites:'-Block - bitte manuell einfügen.")
        return 0

    ok, error = append_site(target, state.as_yaml_block(indent=indent))
    if not ok:
        print(f"Nicht automatisch eingefügt ({error})\nBitte den Block oben selbst einfügen.")
        return 0

    print(f"An {target} angehängt.")
    return 0
