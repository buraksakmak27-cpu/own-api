"""Recorder — kullanıcı sitede gezinirken JSON API ve SSR HTML trafiğini yakalar.

ChromeBridge'in kalıcı profilini kullanır (gerçek Chrome, gerçek oturum).

- Dinleme context seviyesinde: yönlendirmeler (örn. /booking/availability) ve
  yeni açılan sekmeler/popup'lar dahil TÜM sayfaların trafiği toplanır.
- ENTER'dan sonra ağ trafiği durana kadar (networkidle) beklenir; geç gelen
  uçuş/fiyat yanıtları kaçmaz.
- "flight", "fare", "segment", "price" gibi kelimeler geçen yanıtlar işaretlenir
  ve listenin başına alınır (hiçbir yanıt silinmez).
- JSON'a ek olarak, MIN_HTML_CHARS'tan büyük text/html yanıtları da kaydedilir
  (Sahibinden, Hepsiburada gibi veriyi sunucuda render eden siteler için).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from playwright.async_api import BrowserContext, CDPSession, Frame, Page, Request, Response

from core.chrome_bridge import ChromeBridge

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CAPTURE_FILE = PROJECT_ROOT / "captured_traffic.json"

# Önceliklendirilecek kelimeler (URL + POST gövdesi + yanıt gövdesinde aranır).
PRIORITY_KEYWORDS = (
    "flight", "fare", "segment", "price",
    "availability", "itinerary", "offer", "uçuş", "fiyat",
)
KEYWORD_SCAN_CHARS = 300_000  # büyük gövdelerde aranacak en fazla karakter

IDLE_QUIET_SECONDS = 1.5   # bu kadar süre yeni istek olmazsa trafik durmuş sayılır
IDLE_TIMEOUT_SECONDS = 20  # en fazla bu kadar beklenir (long-polling vb. için)

MIN_HTML_CHARS = 10_000  # bundan küçük HTML yanıtlar (iframe, hata, boş şablon) kaydedilmez


def _is_json(content_type: str) -> bool:
    ct = content_type.lower()
    return "application/json" in ct or "+json" in ct


def _is_html(content_type: str) -> bool:
    return "text/html" in content_type.lower()


def _keywords(entry: dict) -> list[str]:
    body = entry.get("body")
    body_text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)
    haystack = " ".join(
        (entry.get("url") or "", entry.get("post_data") or "", body_text[:KEYWORD_SCAN_CHARS])
    ).lower()
    return [kw for kw in PRIORITY_KEYWORDS if kw in haystack]


class Recorder:
    def __init__(self, bridge: Optional[ChromeBridge] = None) -> None:
        self.bridge = bridge or ChromeBridge()
        self._cdp: Optional[CDPSession] = None
        self._context: Optional[BrowserContext] = None
        self._captured: list[dict] = []
        self._pending: set[asyncio.Task] = set()
        self._inflight: set[Request] = set()
        self._last_activity = 0.0
        self._visited: list[str] = []  # gezilen sayfa URL'leri (yönlendirmeler dahil)

    # ------------------------------------------------------------------ #
    # olay dinleyicileri
    # ------------------------------------------------------------------ #
    def _touch(self) -> None:
        self._last_activity = asyncio.get_running_loop().time()

    def _on_request(self, request: Request) -> None:
        self._inflight.add(request)
        self._touch()

    def _on_request_done(self, request: Request) -> None:
        self._inflight.discard(request)
        self._touch()

    def _on_frame_navigated(self, frame: Frame) -> None:
        if frame.parent_frame is not None:  # iframe'ler değil, sadece ana sayfa
            return
        url = frame.url
        if url and url != "about:blank" and (not self._visited or self._visited[-1] != url):
            if self._visited:
                print(f"   ↪️  Sayfa değişti: {url}")
            self._visited.append(url)

    def _watch_page(self, page: Page) -> None:
        page.on("framenavigated", self._on_frame_navigated)

    def _on_new_page(self, page: Page) -> None:
        print(f"   🗂️  Yeni sekme açıldı: {page.url or '(yükleniyor)'}")
        self._watch_page(page)

    async def _handle_response(self, response: Response) -> None:
        content_type = response.headers.get("content-type", "")
        is_json = _is_json(content_type)
        is_html = not is_json and _is_html(content_type)
        if not (is_json or is_html):
            return
        request = response.request
        try:
            text = await response.text()
        except Exception:
            return  # gövde alınamadı (redirect, sayfa kapandı vb.)
        if is_html:
            if len(text) <= MIN_HTML_CHARS:
                return
            body = text  # HTML ham metin olarak saklanır
        else:
            try:
                body = json.loads(text)
            except (ValueError, TypeError):
                body = text
        try:
            headers = await request.all_headers()
        except Exception:
            headers = request.headers
        try:
            page_url = response.frame.page.url
        except Exception:
            page_url = None
        entry = {
            "url": response.url,
            "method": request.method,
            "headers": headers,
            "body": body,
            "status": response.status,
            "content_type": content_type,
            "post_data": request.post_data,
            "page_url": page_url,
            "resource_type": request.resource_type,
            "format": "html" if is_html else "json",
        }
        entry["keywords"] = _keywords(entry)
        self._captured.append(entry)

    def _on_response(self, response: Response) -> None:
        self._touch()
        task = asyncio.ensure_future(self._handle_response(response))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    # ------------------------------------------------------------------ #
    # bekleme
    # ------------------------------------------------------------------ #
    async def _wait_network_idle(self) -> None:
        """Tüm sekmelerde ağ trafiği durana kadar bekler (en fazla IDLE_TIMEOUT_SECONDS)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + IDLE_TIMEOUT_SECONDS

        # 1) Playwright'ın sayfa bazlı networkidle durumu (her sekme için)
        pages = [p for p in (self._context.pages if self._context else []) if not p.is_closed()]

        async def page_idle(p: Page) -> None:
            try:
                await p.wait_for_load_state("networkidle", timeout=IDLE_TIMEOUT_SECONDS * 1000)
            except Exception:
                pass  # zaman aşımı / kapanan sekme — aşağıdaki kontrol devam eder

        await asyncio.gather(*(page_idle(p) for p in pages))

        # 2) Context genelinde: uçuşta istek kalmasın ve IDLE_QUIET_SECONDS boyunca yeni istek gelmesin
        while loop.time() < deadline:
            quiet = loop.time() - self._last_activity
            if not self._inflight and quiet >= IDLE_QUIET_SECONDS:
                break
            await asyncio.sleep(0.2)
        else:
            print(f"   ⏱️  {IDLE_TIMEOUT_SECONDS} sn doldu; {len(self._inflight)} istek hâlâ açık, devam ediliyor.")

        # 3) Gövdesi okunmakta olan yanıtlar
        if self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    # ------------------------------------------------------------------ #
    # ana akış
    # ------------------------------------------------------------------ #
    async def capture(self, url: str, intent: str) -> list[dict]:
        """URL'yi açar, kullanıcı ENTER'a basıp trafik durana kadar JSON/HTML yanıtlarını toplar."""
        self._captured = []
        self._inflight = set()
        self._visited = []

        await self.bridge.start()
        page = await self.bridge.get_page()
        context = page.context
        self._context = context

        self._cdp = await context.new_cdp_session(page)
        await self._cdp.send("Network.enable")

        # Context seviyesinde dinle: tüm sekmeler, popup'lar ve yönlendirmeler.
        listeners = [
            ("response", self._on_response),
            ("request", self._on_request),
            ("requestfinished", self._on_request_done),
            ("requestfailed", self._on_request_done),
            ("page", self._on_new_page),
        ]
        for event, handler in listeners:
            context.on(event, handler)
        for p in context.pages:
            self._watch_page(p)
        self._touch()

        try:
            await page.goto(url, wait_until="domcontentloaded")
            print(f"🎯 Amaç: {intent}")
            print("👉 Sitede arama yapın (sonuç sayfası açılana kadar bekleyin), sonra ENTER'a basın.")
            await asyncio.to_thread(input)

            open_pages = [p for p in context.pages if not p.is_closed()]
            final_urls = [p.url for p in open_pages]
            if any(u.rstrip("/") != url.rstrip("/") for u in final_urls):
                print(f"   🔀 Açık sayfalar: {', '.join(final_urls)}")
            print("⏳ Ağ trafiğinin durması bekleniyor...")
            await self._wait_network_idle()
        finally:
            for event, handler in listeners:
                context.remove_listener(event, handler)
            for p in context.pages:
                try:
                    p.remove_listener("framenavigated", self._on_frame_navigated)
                except Exception:
                    pass

        # Anahtar kelime eşleşenler önce (çok eşleşen daha önce); sıra stabil.
        traffic = sorted(self._captured, key=lambda e: len(e["keywords"]), reverse=True)

        matched = [e for e in traffic if e["keywords"]]
        pages_seen = {e.get("page_url") for e in traffic if e.get("page_url")}
        html_count = sum(1 for e in traffic if e.get("format") == "html")
        print(
            f"📦 {len(traffic) - html_count} JSON + {html_count} HTML yanıt, {len(pages_seen)} sayfadan; "
            f"{len(matched)} tanesinde {'/'.join(PRIORITY_KEYWORDS[:4])}... geçiyor."
        )
        for e in matched[:5]:
            print(f"   ⭐ {e['method']} {e['url'][:110]}  [{', '.join(e['keywords'])}]")

        CAPTURE_FILE.write_text(
            json.dumps(traffic, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        return traffic

    async def close(self) -> None:
        if self._cdp is not None:
            try:
                await self._cdp.detach()
            except Exception:
                pass
            self._cdp = None
        self._context = None
        await self.bridge.close()
