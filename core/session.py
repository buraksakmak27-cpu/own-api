"""SessionManager — sitenin kalıcı profilde oturum açık mı kontrol eder.

Giriş gerekiyorsa kullanıcıdan gerçek Chrome'da giriş yapmasını ister;
çerezler kalıcı profilde (~/.own-api/.browser_profile) saklanır.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from core.chrome_bridge import ChromeBridge

LOGIN_MARKERS = ("login", "signin", "sign-in", "giris")


def _needs_login(url: str) -> bool:
    low = url.lower()
    return any(m in low for m in LOGIN_MARKERS)


class SessionManager:
    def __init__(
        self,
        site_name: str,
        site_url: Optional[str] = None,
        bridge: Optional[ChromeBridge] = None,
    ) -> None:
        self.site_name = site_name
        self.site_url = site_url or f"https://www.{site_name}.com"
        self._bridge = bridge

    @property
    def bridge(self) -> ChromeBridge:
        # Tembel oluşturma: ensure_authenticated çağrılmazsa Chrome açılmaz.
        if self._bridge is None:
            self._bridge = ChromeBridge()
        return self._bridge

    async def ensure_authenticated(self) -> bool:
        """Siteye gider; giriş sayfasına yönlenirse kullanıcıyı bekler.

        True: oturum açık (veya giriş gerekmedi). False: hâlâ giriş sayfasında.
        """
        page = await self.bridge.navigate(self.site_url)
        if not _needs_login(page.url):
            return True

        print(f"🔐 {self.site_name} giriş istiyor ({page.url}).")
        print("👉 Chrome'da giriş yapın, sonra ENTER'a basın.")
        await asyncio.to_thread(input)

        if _needs_login(page.url):
            page = await self.bridge.navigate(self.site_url)
        if _needs_login(page.url):
            print("⚠️  Hâlâ giriş sayfasındasınız; oturum açılamadı.")
            return False
        print("✅ Oturum açık.")
        return True

    async def close(self) -> None:
        if self._bridge is not None:
            await self._bridge.close()
