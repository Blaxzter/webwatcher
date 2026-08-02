"""Telegram Bot API client - text messages plus screenshots."""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime

import httpx

from webwatcher.compare import Diff
from webwatcher.config import SiteConfig, TelegramConfig

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"

# Telegram hard limits (https://core.telegram.org/bots/api)
MAX_MESSAGE_CHARS = 4096
MAX_CAPTION_CHARS = 1024
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTO_DIMENSION_SUM = 10000
MAX_PHOTO_RATIO = 20


class TelegramError(RuntimeError):
    pass


def png_size(data: bytes) -> tuple[int, int] | None:
    """Read width/height straight from the PNG IHDR chunk (no image library needed)."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def fits_as_photo(data: bytes) -> bool:
    """Telegram rejects photos that are too big, too tall or too skewed."""
    if len(data) > MAX_PHOTO_BYTES:
        return False
    size = png_size(data)
    if size is None:
        return True
    width, height = size
    if width + height > MAX_PHOTO_DIMENSION_SUM:
        return False
    longer, shorter = max(width, height), max(1, min(width, height))
    return longer / shorter <= MAX_PHOTO_RATIO


def _file_id_from(result: dict, as_photo: bool) -> str | None:
    """Pull the uploaded file's id out of a sendPhoto/sendDocument response."""
    try:
        if as_photo:
            return str(result["photo"][-1]["file_id"])
        return str(result["document"]["file_id"])
    except (KeyError, IndexError, TypeError):
        return None


def _chunks(text: str, limit: int) -> list[str]:
    """Split on line boundaries so code blocks stay readable."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return parts


class TelegramNotifier:
    def __init__(self, config: TelegramConfig, timeout: float = 30.0) -> None:
        self.config = config
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> TelegramNotifier:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # -- low level ----------------------------------------------------------

    async def _call(
        self,
        method: str,
        data: dict[str, object],
        files: dict[str, tuple[str, bytes, str]] | None = None,
        retries: int = 3,
    ) -> dict:
        if not self.config.has_token:
            raise TelegramError("bot_token fehlt in der Konfiguration")

        url = f"{API_BASE}/bot{self.config.bot_token}/{method}"
        payload = {k: v for k, v in data.items() if v is not None}
        # getMe/getUpdates carry no chat_id; everything that sends something does.
        if "chat_id" in payload and not payload["chat_id"]:
            raise TelegramError("chat_id fehlt - ermitteln mit: webwatcher chat-id")
        last_error = ""

        for attempt in range(1, retries + 1):
            try:
                response = await self._client.post(url, data=payload, files=files)
            except httpx.HTTPError as exc:
                last_error = f"Netzwerkfehler: {exc}"
                if attempt < retries:
                    await asyncio.sleep(2 * attempt)
                    continue
                break

            try:
                body = response.json()
            except ValueError:
                body = {}

            if response.status_code == 200 and body.get("ok"):
                return body.get("result", {})

            description = body.get("description") or response.text[:200]
            last_error = f"HTTP {response.status_code}: {description}"

            # 429 tells us exactly how long to back off; 5xx is worth a retry.
            if response.status_code == 429:
                wait = float(body.get("parameters", {}).get("retry_after", 5))
                log.warning("telegram rate limit, waiting %.0fs", wait)
                await asyncio.sleep(min(wait, 60))
                continue
            if response.status_code >= 500 and attempt < retries:
                await asyncio.sleep(2 * attempt)
                continue
            break

        raise TelegramError(f"{method} fehlgeschlagen - {last_error}")

    def _payload_for(self, chat_id: str) -> dict[str, object]:
        return {
            "chat_id": chat_id,
            "parse_mode": "HTML",
            "disable_notification": self.config.silent or None,
        }

    def _report(self, errors: list[str]) -> None:
        """One unreachable recipient must not silence the others."""
        if not errors:
            return
        if len(errors) == len(self.config.chat_ids):
            raise TelegramError("; ".join(errors))
        for entry in errors:
            log.error("telegram: %s", entry)

    async def send_message(self, text: str) -> None:
        chunks = _chunks(text, MAX_MESSAGE_CHARS)
        errors: list[str] = []
        for chat_id in self.config.chat_ids:
            try:
                for chunk in chunks:
                    await self._call(
                        "sendMessage",
                        {
                            **self._payload_for(chat_id),
                            "text": chunk,
                            "link_preview_options": '{"is_disabled": true}',
                        },
                    )
            except TelegramError as exc:
                errors.append(f"Chat {chat_id}: {exc}")
        self._report(errors)

    async def send_image(self, image: bytes, caption: str, filename: str) -> None:
        """Send as photo when Telegram accepts it, otherwise as a file."""
        caption = caption[:MAX_CAPTION_CHARS]
        # Full-page screenshots are often far too tall for the photo endpoint.
        as_photo = fits_as_photo(image)
        method = "sendPhoto" if as_photo else "sendDocument"
        field = "photo" if as_photo else "document"

        file_id: str | None = None
        errors: list[str] = []
        for chat_id in self.config.chat_ids:
            payload = {**self._payload_for(chat_id), "caption": caption}
            try:
                if file_id is not None:
                    # Telegram keeps the file after the first upload; sending the
                    # id spares us re-uploading the screenshot per recipient.
                    await self._call(method, {**payload, field: file_id})
                else:
                    result = await self._call(
                        method, payload, files={field: (filename, image, "image/png")}
                    )
                    file_id = _file_id_from(result, as_photo)
            except TelegramError as exc:
                errors.append(f"Chat {chat_id}: {exc}")
        self._report(errors)

    async def get_updates(self) -> list[dict]:
        result = await self._call("getUpdates", {"timeout": 0, "limit": 20}, retries=1)
        return result if isinstance(result, list) else []

    async def get_me(self) -> dict:
        return await self._call("getMe", {}, retries=1)

    # -- messages -----------------------------------------------------------

    async def _deliver(self, header: str, body: str, image: bytes | None, key: str) -> None:
        """Screenshot carries the header as caption; the body follows as text."""
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if image and self.config.send_screenshot:
            await self.send_image(image, header, f"{key}-{stamp}.png")
            if body:
                await self.send_message(body)
        else:
            await self.send_message(header + (f"\n\n{body}" if body else ""))

    async def send_change(
        self, site: SiteConfig, diff: Diff, image: bytes | None, final_url: str
    ) -> None:
        header = (
            f"🔔 <b>{html.escape(site.name)}</b> hat sich geändert\n"
            f"{html.escape(diff.summary())}\n"
            f'<a href="{html.escape(final_url or site.url, quote=True)}">Seite öffnen</a>'
        )
        body = ""
        if diff.text:
            body = f"<pre>{html.escape(diff.text)}</pre>"
        await self._deliver(header, body, image, site.key)

    async def send_baseline(self, site: SiteConfig, image: bytes | None, final_url: str) -> None:
        header = (
            f"👀 <b>{html.escape(site.name)}</b> wird jetzt beobachtet\n"
            f'<a href="{html.escape(final_url or site.url, quote=True)}">Seite öffnen</a>'
        )
        await self._deliver(header, "", image, site.key)

    async def send_error(self, site: SiteConfig, error: str, failures: int) -> None:
        await self.send_message(
            f"⚠️ <b>{html.escape(site.name)}</b> ist {failures}x hintereinander fehlgeschlagen\n"
            f"{html.escape(site.url)}\n"
            f"<pre>{html.escape(error[:800])}</pre>"
        )

    async def send_recovery(self, site: SiteConfig) -> None:
        await self.send_message(
            f"✅ <b>{html.escape(site.name)}</b> ist wieder erreichbar\n{html.escape(site.url)}"
        )
