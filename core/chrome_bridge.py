"""Chrome Bridge — kullanıcının gerçek Chrome'unu kalıcı profille açar.

Altın kural: fingerprint taklidi, proxy, TLS spoofing YOK.
Sadece gerçek Chrome + kalıcı profil (~/.own-api/.browser_profile).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)


def _silence_windows_closed_pipe() -> None:
    """Windows'ta 'ValueError: I/O operation on closed pipe' gürültüsünü susturur.

    asyncio.run() döngüyü kapattıktan sonra çöp toplayıcı Playwright alt sürecinin
    pipe transport'larını __del__ ile kapatmaya çalışır; döngü kapalı olduğu için
    zararsız bir ValueError fırlar. Sadece bu hatayı yutar.
    """
    if sys.platform != "win32":
        return
    from asyncio.proactor_events import _ProactorBasePipeTransport

    original_del = _ProactorBasePipeTransport.__del__
    if getattr(original_del, "_own_api_patched", False):
        return

    def __del__(self, _warn=None):  # type: ignore[no-untyped-def]
        try:
            original_del(self)
        except ValueError as e:
            if "closed pipe" not in str(e):
                raise

    __del__._own_api_patched = True  # type: ignore[attr-defined]
    _ProactorBasePipeTransport.__del__ = __del__  # type: ignore[assignment]


_silence_windows_closed_pipe()

DEFAULT_PROFILE_DIR = Path.home() / ".own-api" / ".browser_profile"

CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]


class ChromeBridge:
    def __init__(self, user_data_dir: Path = DEFAULT_PROFILE_DIR) -> None:
        self.user_data_dir = Path(user_data_dir)
        self._playwright: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    @property
    def is_running(self) -> bool:
        return self._context is not None

    async def start(self) -> BrowserContext:
        """Gerçek Chrome'u kalıcı profille başlatır."""
        if self._context is not None:
            return self._context

        self.user_data_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()
        try:
            self._context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.user_data_dir),
                channel="chrome",
                headless=False,
                args=CHROME_ARGS,
                locale="tr-TR",
                timezone_id="Europe/Istanbul",
            )
        except Exception:
            await self._playwright.stop()
            self._playwright = None
            raise

        # Kalıcı context genelde bir boş sekmeyle açılır; onu kullan.
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()
        return self._context

    async def get_page(self) -> Page:
        """Aktif sayfayı döndürür; kapanmışsa yenisini açar."""
        if self._context is None:
            await self.start()
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
        return self._page

    async def navigate(self, url: str) -> Page:
        """Aktif sayfada URL'ye gider ve sayfayı döndürür."""
        page = await self.get_page()
        await page.goto(url, wait_until="domcontentloaded")
        return page

    async def get_cookies(self, domain: Optional[str] = None) -> list[dict]:
        """Profildeki çerezleri döndürür; domain verilirse filtreler."""
        if self._context is None:
            raise RuntimeError("ChromeBridge başlatılmadı; önce start() çağırın.")
        cookies = await self._context.cookies()
        if not domain:
            return cookies
        target = domain.lstrip(".").lower()
        return [
            c
            for c in cookies
            if (cd := c.get("domain", "").lstrip(".").lower()) == target
            or target.endswith("." + cd)
            or cd.endswith("." + target)
        ]

    async def close(self) -> None:
        """Tarayıcıyı ve Playwright'ı kapatır (profil diskte kalır)."""
        if self._context is not None:
            try:
                await self._context.close()
            finally:
                self._context = None
                self._page = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            finally:
                self._playwright = None
                # Windows: alt sürecin pipe'larının döngü kapanmadan temizlenmesine izin ver.
                await asyncio.sleep(0.1)

    async def __aenter__(self) -> "ChromeBridge":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()
