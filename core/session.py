"""SessionManager — sitenin kalıcı profilde oturum açık mı kontrol eder.

Giriş gerekiyorsa kullanıcıdan gerçek Chrome'da giriş yapmasını ister;
çerezler kalıcı profilde (~/.own-api/.browser_profile) saklanır.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from core.chrome_bridge import ChromeBridge

LOGIN_MARKERS = ("login", "signin", "sign-in", "giris")

# OTP/2FA kodu girilen alanları yakalamak için seçiciler.
OTP_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[type="tel"]',
    'input[name*="otp" i]',
    'input[name*="code" i]',
    'input[name*="sms" i]',
    'input[id*="otp" i]',
    'input[id*="code" i]',
)
# Sayfa metninde OTP/2FA işareti.
OTP_TEXT_MARKERS = (
    "doğrulama", "dogrulama", "doğrulama kodu", "sms", "otp",
    "tek kullanımlık", "verification code", "one-time", "2fa", "iki adımlı",
)
OTP_MAX_ATTEMPTS = 3


def _needs_login(url: str) -> bool:
    low = url.lower()
    return any(m in low for m in LOGIN_MARKERS)


async def _find_otp_input(page):
    """Sayfadaki görünür OTP kod alanını döndürür; yoksa None."""
    for sel in OTP_SELECTORS:
        try:
            base = page.locator(sel)
            if await base.count() == 0:
                continue
            loc = base.first
            if await loc.is_visible():
                return loc
        except Exception:
            continue
    return None


async def _otp_state(page):
    """(input_locator | None, otp_var_mı) döndürür."""
    inp = await _find_otp_input(page)
    text_signal = False
    try:
        body = (await page.inner_text("body"))[:5000].lower()
        text_signal = any(m in body for m in OTP_TEXT_MARKERS)
    except Exception:
        pass
    return inp, (inp is not None or text_signal)


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

    async def _handle_otp(self, page) -> bool:
        """OTP/2FA formu varsa kullanıcıdan kodu alıp doldurur (en fazla 3 deneme).

        Dönüş: True = OTP işlendi (çözüldü ya da denendi), False = OTP yoktu.
        Kalıcı profil çerezleri Chrome tarafından otomatik saklanır.
        """
        handled = False
        for attempt in range(1, OTP_MAX_ATTEMPTS + 1):
            inp, present = await _otp_state(page)
            if not present:
                break
            handled = True
            code = (await asyncio.to_thread(
                input, "🔔 SMS/E-posta Doğrulama Kodu Gerekli! Lütfen kodu girin: "
            )).strip()
            if not code:
                print("   Kod girilmedi.")
                continue
            if inp is None:
                # Kod alanı otomatik bulunamadı → kullanıcı Chrome'da girsin.
                print("   Kod alanı bulunamadı; Chrome'da girin, sonra ENTER'a basın.")
                await asyncio.to_thread(input)
            else:
                try:
                    await inp.fill(code)
                    await inp.press("Enter")
                except Exception as e:
                    print(f"   Kod girilemedi: {e}")
                    continue
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    pass
            _, still = await _otp_state(page)
            if not still:
                print("✅ Doğrulama kodu kabul edildi.")
                return True
            if attempt < OTP_MAX_ATTEMPTS:
                print(f"   ⚠️ Kod kabul edilmedi, tekrar deneyin ({attempt}/{OTP_MAX_ATTEMPTS}).")
        if handled:
            print("   ⚠️ Doğrulama tamamlanamadı.")
        return handled

    async def ensure_authenticated(self) -> bool:
        """Siteye gider; giriş veya OTP/2FA gerekirse kullanıcıyı bekler.

        True: oturum açık. False: hâlâ giriş/doğrulama gerekli.
        """
        page = await self.bridge.navigate(self.site_url)

        # 1) Giriş sayfası: kullanıcı Chrome'da giriş yapsın.
        if _needs_login(page.url):
            print(f"🔐 {self.site_name} giriş istiyor ({page.url}).")
            print("👉 Chrome'da giriş yapın, sonra ENTER'a basın.")
            await asyncio.to_thread(input)
            if _needs_login(page.url):
                page = await self.bridge.navigate(self.site_url)

        # 2) Giriş sonrası (ya da doğrudan) OTP/2FA formu çıktıysa koddan geç.
        await self._handle_otp(page)

        _, otp_still = await _otp_state(page)
        if _needs_login(page.url) or otp_still:
            print("⚠️  Giriş/doğrulama tamamlanamadı.")
            return False
        print("✅ Oturum açık.")
        return True

    async def close(self) -> None:
        if self._bridge is not None:
            await self._bridge.close()
