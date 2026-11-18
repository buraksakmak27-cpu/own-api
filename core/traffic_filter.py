"""Yakalanan trafikten işe yarar JSON API çağrılarını ve SSR HTML sayfalarını ayıklar.

Sıralama önceliği:
  0. Arama sonucu olamayacak yanıtlar EN SONA: çeviri/CMS/ayar dosyaları (arayüz metinlerinde
     "flight", "price" geçtiği için yanıltıcıdır) ve boş/çok küçük gövdeler (event/beacon).
  1. Yanıt içeriğinde anahtar kelime geçenler (recorder'ın entry["keywords"] alanı;
     eski kayıtlarda alan yoksa gövde burada taranır) — örn. flight, fare, price.
  2. URL'de anahtar kelime geçenler (search, product, ...).
  Eşitlikte orijinal sıra korunur; hiçbir JSON/HTML silinmez, sadece sıralanır.
  HTML yanıtlar (recorder 10.000 karakterden büyükleri kaydeder) SSR siteler
  (Sahibinden, Hepsiburada) için AI'a gönderilecekler listesine dahildir.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

# AI'a gönderilecek en fazla istek (compiler.MAX_ENTRIES bununla aynı tutulur).
MAX_ENTRIES = 50

# Analitik/reklam servisleri — URL'nin tamamında değil, SADECE alan adında aranır.
# (Önceden "segment" tüm URL'de aranıyordu ve /api/flight-segments gibi uçuş
#  endpoint'lerini de siliyordu.)
BLOCKED_HOSTS = (
    "google-analytics.com",
    "analytics.google.com",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.com",
    "facebook.net",
    "hotjar.com",
    "hotjar.io",
    "segment.io",
    "segment.com",
    "mixpanel.com",
    "sentry.io",
    "sentry-cdn.com",
)

# Yanıt içeriğinde aranan güçlü sinyaller (recorder.PRIORITY_KEYWORDS ile uyumlu).
CONTENT_KEYWORDS = (
    "flight", "fare", "segment", "price",
    "availability", "itinerary", "offer", "uçuş", "fiyat",
)
CONTENT_SCAN_CHARS = 300_000

# Arama sonucu olamayacak "statik içerik" URL'leri (çeviri, CMS, ayar, takip).
STATIC_URL_PATTERNS = (
    "translation", "i18n", "locale", "/cms/", "config", "/parameters", "/settings",
    "/events/", "/event?", "telemetry", "beacon", "/track", "/log?", "/logs",
    "redirect-url", "canonical", "notification", "banner", "loader", "/horizon/",
)
MIN_BODY_CHARS = 20  # bundan küçük gövdeler ("{}", "[]", "ok") sonuç olamaz

# URL'de aranan zayıf sinyaller.
URL_KEYWORDS = ("price", "product", "search", "result", "item", "data", "flight", "fare", "availability")


def _content_type(entry: dict) -> str:
    ct = entry.get("content_type")
    if ct:
        return ct.lower()
    headers = entry.get("headers") or {}
    for key, value in headers.items():
        if key.lower() == "content-type":
            return str(value).lower()
    return ""


def _is_json(entry: dict) -> bool:
    ct = _content_type(entry)
    return "application/json" in ct or "+json" in ct


def _is_html(entry: dict) -> bool:
    return entry.get("format") == "html" or "text/html" in _content_type(entry)


def _is_blocked(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS)


def _content_keywords(entry: dict) -> list[str]:
    """recorder'ın eklediği keywords; yoksa (eski kayıt) gövde/URL/post_data'yı tara."""
    kws = entry.get("keywords")
    if isinstance(kws, list):
        return kws
    body = entry.get("body")
    body_text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=str)
    haystack = " ".join(
        (entry.get("url") or "", entry.get("post_data") or "", body_text[:CONTENT_SCAN_CHARS])
    ).lower()
    return [kw for kw in CONTENT_KEYWORDS if kw in haystack]


def _is_static(url: str) -> bool:
    u = url.lower()
    return any(p in u for p in STATIC_URL_PATTERNS)


def _body_size(entry: dict) -> int:
    body = entry.get("body")
    if body is None:
        return 0
    return len(body) if isinstance(body, str) else len(json.dumps(body, ensure_ascii=False, default=str))


def _score(entry: dict) -> tuple[int, int, int, int]:
    url = entry.get("url", "").lower()
    real_data = not _is_static(url) and _body_size(entry) >= MIN_BODY_CHARS
    content = len(_content_keywords(entry))
    url_hits = sum(1 for kw in URL_KEYWORDS if kw in url)
    has_input = 1 if entry.get("post_data") else 0  # kullanıcı girdisi taşıyan POST
    # Önce gerçek veri, sonra içerik eşleşmesi (flight/fare/price...), sonra URL, sonra POST.
    return (int(real_data), content, url_hits, has_input)


def filter_useful(traffic: list[dict]) -> list[dict]:
    """Analitik/reklamı atar, JSON ve HTML yanıtları bırakır, anahtar kelimeye göre sıralar."""
    kept = [
        t for t in traffic
        if not _is_blocked(t.get("url", "")) and (_is_json(t) or _is_html(t))
    ]
    # Stabil sıralama: eşit skorlarda orijinal sıra korunur.
    return sorted(kept, key=_score, reverse=True)
