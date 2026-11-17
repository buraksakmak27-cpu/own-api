"""Healer — istekleri kullanıcının gerçek Chrome'u içinden fetch() ile yapar.

Kalıcı profil (ChromeBridge) sayesinde çerezler/oturum gerçek kullanıcınınkidir.
403 / captcha görülürse kullanıcıdan Chrome'da çözmesini ister ve tekrar dener.
"""

from __future__ import annotations

import asyncio
import json
import json as _json
import uuid
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from core.chrome_bridge import ChromeBridge

MAX_RETRIES = 3

# Sayfanın kendi isteklerinden (x-bfp içerenlerden) yakalanıp bizim isteğe eklenecek başlıklar.
# Beklenen başlıklar (bilgi/log için). Dinleyici, x-bfp taşıyan isteklerdeki
# "x-" ile başlayan TÜM başlıkları yakalar; bu liste sınırlamaz.
CAPTURE_HEADERS = (
    "x-bfp", "x-clientid", "x-platform", "x-country", "x-requestid", "x-conversationid",
)
HEADER_WAIT_SECONDS = 15  # x-bfp için en fazla bu kadar beklenir

CAPTCHA_MARKERS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "cf-challenge",
    "challenge-platform",
    "are you a robot",
    "robot olmadığınızı",
    "access denied",
)

# Sayfa içinde çalışan fetch. credentials: 'include' → çerezler gönderilir.
FETCH_JS = """
async ({url, method, body, referrer, extraHeaders}) => {
    const headers = {'Accept': 'application/json, text/plain, */*'};
    const init = {method, credentials: 'include', headers};
    if (body !== null && body !== undefined) {
        headers['Content-Type'] = 'application/json';
        init.body = body;
    }
    // Sitenin kendi JS'inin ÜRETTİĞİ (taklit edilmeyen) dinamik başlıklar: x-bfp,
    // x-conversationid, x-clientid vb. Kullanıcının canlı trafiğinden yakalanıp eklenir.
    if (extraHeaders) {
        for (const k in extraHeaders) { headers[k] = extraHeaders[k]; }
    }
    // POST: Referer = sitenin ana sayfası. 'Origin' ve 'Referer' tarayıcının korumalı
    // başlıklarıdır; headers'a yazılırsa sessizce yok sayılır. Referer fetch'in
    // 'referrer' seçeneğiyle verilir; Origin'i tarayıcı, fetch sitenin kendi
    // sayfasından (aynı origin) yapıldığı için otomatik olarak doğru gönderir.
    if (referrer) {
        init.referrer = referrer;
        init.referrerPolicy = 'strict-origin-when-cross-origin';
    }
    const resp = await fetch(url, init);
    return {
        status: resp.status,
        contentType: resp.headers.get('content-type') || '',
        text: await resp.text(),
    };
}
"""

# İki parçalı ülke uzantıları (örn. trendyol.com.tr)
_SECOND_LEVEL = {"com", "net", "org", "gov", "edu", "co", "ac"}


class VerificationRequired(Exception):
    pass


def _with_params(url: str, params: Optional[dict]) -> str:
    if not params:
        return url
    parts = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in params]
    query += [(k, str(v)) for k, v in params.items() if v is not None]
    return urlunparse(parts._replace(query=urlencode(query)))


def _site_origin(url: str) -> str:
    """API alt alanından (apigw.x.com) sitenin ana sayfasını (www.x.com) tahmin eder.

    fetch'i sitenin kendi origin'inden yapmak CORS + çerez açısından gerekli.
    """
    parts = urlparse(url)
    labels = parts.hostname.split(".") if parts.hostname else []
    n = 3 if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2 else 2
    base = ".".join(labels[-n:])
    return f"{parts.scheme or 'https'}://www.{base}/"


def _looks_like_captcha(status: int, content_type: str, text: str) -> bool:
    if status in (403, 429):
        return True
    if "json" in content_type.lower():
        return False
    low = text[:5000].lower()
    return any(m in low for m in CAPTCHA_MARKERS)


class Healer:
    def __init__(
        self,
        session: Any = None,
        bridge: Optional[ChromeBridge] = None,
        origin: Optional[str] = None,
    ) -> None:
        # session: core.session.SessionManager (client.py uyumluluğu için kabul edilir)
        self.session = session
        self.bridge = bridge or ChromeBridge()
        self.origin = origin

    async def _fetch_once(self, url: str, method: str, body: Optional[str] = None) -> Any:
        await self.bridge.start()
        context = self.bridge._context
        page = await context.new_page()

        # Sitenin kendi JS'inin, kullanıcının gerçek tarayıcısında ürettiği başlıklar.
        # Taklit edilmez; sayfanın kendi API isteklerinden okunur.
        captured_headers: dict[str, str] = {}

        def _on_request(request) -> None:
            try:
                headers = request.headers  # küçük harfli anahtarlar
            except Exception:
                return
            if "x-bfp" not in headers:
                return
            for name, value in headers.items():
                if name.startswith("x-") and value:
                    captured_headers[name] = value

        page.on("request", _on_request)  # sayfa yüklenmeden ÖNCE
        try:
            origin = self.origin or _site_origin(url)
            await page.goto(origin, wait_until="domcontentloaded")
            # Sayfanın JS'i başlıkları üretip x-bfp taşıyan bir API isteği atana kadar bekle.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + HEADER_WAIT_SECONDS
            while "x-bfp" not in captured_headers and loop.time() < deadline:
                await asyncio.sleep(0.2)
            print(f"   🔑 Yakalanan başlıklar: {', '.join(sorted(captured_headers)) or '(yok)'}")
            if captured_headers:
                # İstek takip kimlikleri (rastgele UUID) — sayfa üretmediyse biz üretiriz.
                # Sadece bu başlık sistemini kullanan sitelerde (x-bfp yakalandıysa) eklenir;
                # aksi halde örn. Trendyol'a gereksiz özel başlık gider ve CORS bozulabilir.
                if "x-conversationid" not in captured_headers:
                    captured_headers["x-conversationid"] = str(uuid.uuid4())
                if "x-requestid" not in captured_headers:
                    captured_headers["x-requestid"] = str(uuid.uuid4())
            else:
                print("   ⚠️ Header yakalanamadı, yine de deneniyor")
            # POST isteklerinde Referer: https://www.site.com/ (Origin tarayıcıdan otomatik gelir)
            referrer = origin if method == "POST" else None
            print(f"   🚀 Gönderilen Başlıklar: {captured_headers}")
            result = await page.evaluate(
                FETCH_JS,
                {
                    "url": url,
                    "method": method,
                    "body": body,
                    "referrer": referrer,
                    "extraHeaders": captured_headers or None,
                },
            )
        finally:
            try:
                page.remove_listener("request", _on_request)
            except Exception:
                pass
            await page.close()

        status = result["status"]
        content_type = result["contentType"]
        text = result["text"]

        if _looks_like_captcha(status, content_type, text):
            raise VerificationRequired(f"HTTP {status}")
        if status >= 400:
            raise RuntimeError(f"HTTP {status}: {text[:300]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"JSON olmayan yanıt ({content_type}): {text[:300]}") from e

    async def fetch_json(
        self,
        url: str,
        params: Optional[dict] = None,
        method: str = "GET",
        json: Any = None,
    ) -> Any:
        """json verilirse JSON gövde olarak gönderilir (POST/PUT vb.)."""
        full_url = _with_params(url, params)
        body = None if json is None else _json.dumps(json, ensure_ascii=False)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return await self._fetch_once(full_url, method.upper(), body)
            except VerificationRequired as e:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(
                        f"Doğrulama {MAX_RETRIES} denemede geçilemedi ({e})."
                    ) from e
                # Kullanıcının çözebilmesi için sitenin sayfasını açık bırak.
                page = await self.bridge.get_page()
                await page.goto(self.origin or _site_origin(full_url), wait_until="domcontentloaded")
                print(f"⚠️  {e} — Doğrulama gerekiyor, lütfen Chrome'da çözün. Bitince ENTER'a basın.")
                await asyncio.to_thread(input)
        raise RuntimeError("unreachable")

    async def fetch(
        self,
        url: str,
        method: str = "GET",
        params: Optional[dict] = None,
        json: Any = None,
    ) -> Any:
        """registry/*/client.py'nin kullandığı isim; fetch_json'a yönlendirir."""
        return await self.fetch_json(url, params=params, method=method, json=json)

    async def close(self) -> None:
        try:
            await self.bridge.close()
        finally:
            # Windows: Playwright alt sürecinin temiz kapanması için kısa bekleme.
            await asyncio.sleep(0.1)
