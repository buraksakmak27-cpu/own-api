"""sahibinden istemcisi — core.compiler tarafından otomatik üretildi.

Amaç: ilan arama
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import sys
from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urljoin, urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.healer import CAPTCHA_MARKERS, Healer, _with_params  # noqa: E402
from core.session import SessionManager  # noqa: E402

SITE_NAME = 'sahibinden'
ENDPOINT_NAME = 'search'
URL = 'https://www.sahibinden.com/cep-telefonu-aksesuar?query_textUnmodified=iphone&query_text=iphone&query_text_mf=iphone'
METHOD = 'GET'
# Arama kelimesinin doğrudan gittiği parametre (yoksa None).
QUERY_PARAM = 'query_text'
# Arama kelimesini bir sayfa yolu içinde taşıyan parametreler:
# {param: [şablon, ekstra_encode_sayısı]} — {query} yerine arama kelimesi yazılır.
QUERY_TEMPLATES = {}
# Yakalanan sayfanın bağlamını taşıyan parametreler (kategori, sıralama vb.) — silinir.
DROP_PARAMS = ['query_textUnmodified', 'query_text_mf']
# POST: yakalanan JSON gövde şablonu (GET'te None).
BODY_TEMPLATE = None
# search(**kwargs) parametreleri: {ad: BODY_TEMPLATE içindeki yol}.
# GET'te {"query": None} — arama kelimesi URL'ye (QUERY_PARAM / QUERY_TEMPLATES) gider.
PARAMETERS = {'query': None}
# "json": API yanıtı | "html": SSR sayfa (BeautifulSoup ile ayrıştırılır).
PARSER_TYPE = 'html'
ITEMS_PATH = None
# html: gömülü JSON script'inin selector'ı (örn. script#__NEXT_DATA__) — varsa ITEMS_PATH/FIELDS JSON yoludur.
EMBEDDED_JSON = None
# html + gömülü JSON yok: her öğeyi seçen CSS selector; FIELDS alt selector'dır ("a.title@href" = öznitelik).
ITEMS_SELECTOR = '.searchResultsItem'
FIELDS = {'name': '.classifiedTitle', 'price': '.searchResultsPriceValue', 'link': '.classifiedTitle@href'}
HTML_MAX_ATTEMPTS = 3  # captcha/doğrulama sayfasında kullanıcıya tanınan deneme sayısı


def _get_path(data, path: str):
    """'data.products' / 'price.current' / 'items.0.name' gibi yolları çözer."""
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _set_path(data, path: str, value) -> None:
    """'filters.searchTerm' / 'items.0.text' yolundaki değeri değiştirir."""
    parts = path.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def _coerce(name: str, value, sample):
    """CLI'dan gelen metni şablondaki örnek değerin tipine çevirir ("2" → 2, "true" → True)."""
    if value is None or isinstance(sample, str) or sample is None:
        return value
    try:
        if isinstance(sample, bool):
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "evet", "yes", "on")
        if isinstance(sample, int):
            return int(value)
        if isinstance(sample, float):
            return float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"'{name}' için geçersiz değer: {value!r} (beklenen: {type(sample).__name__})"
        ) from e
    return value


def _build_payload(values: dict):
    """POST ise şablonun kopyasına kullanıcı değerlerini yerleştirir; GET ise None."""
    if METHOD != "POST" or BODY_TEMPLATE is None:
        return None
    payload = copy.deepcopy(BODY_TEMPLATE)
    for name, path in PARAMETERS.items():
        if path:
            _set_path(payload, path, _coerce(name, values[name], _get_path(BODY_TEMPLATE, path)))
    return payload


def _base_url() -> str:
    """URL'den sayfa bağlamı parametrelerini ve eski arama kelimesini temizler."""
    parts = urlparse(URL)
    drop = set(DROP_PARAMS) | set(QUERY_TEMPLATES) | {QUERY_PARAM}
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in drop]
    return urlunparse(parts._replace(query=urlencode(params)))


def _query_params(query: str) -> dict:
    params = {}
    for name, (template, depth) in QUERY_TEMPLATES.items():
        value = template.replace("{query}", quote_plus(query))
        for _ in range(depth):
            value = quote(value, safe="")
        params[name] = value
    if QUERY_PARAM:
        params[QUERY_PARAM] = query
    return params


def _select_value(el, spec: str, page_url: str):
    """'a.title' → metin, 'a.title@href' → öznitelik, '@href' → öğenin kendi özniteliği."""
    sel, _, attr = (spec or "").partition("@")
    sel, attr = sel.strip(), attr.strip()
    target = el.select_one(sel) if sel else el
    if target is None:
        return None
    if attr:
        value = target.get(attr)
        if isinstance(value, list):
            value = " ".join(value)
        if value and attr in ("href", "src", "data-src"):
            value = urljoin(page_url, value)
        return value
    return target.get_text(" ", strip=True) or None


def _parse_html(html: str, page_url: str) -> list[dict] | None:
    """HTML'den sonuçları çıkarır; ayrıştırılacak yapı bulunamazsa None döner."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    if EMBEDDED_JSON:
        for tag in soup.select(EMBEDDED_JSON):
            try:
                data = json.loads(tag.string or tag.get_text() or "")
            except (TypeError, ValueError):
                continue
            items = _get_path(data, ITEMS_PATH)
            if isinstance(items, list):
                return [{key: _get_path(item, path) for key, path in FIELDS.items()} for item in items]
        return None
    elements = soup.select(ITEMS_SELECTOR)
    if not elements:
        return None
    return [{key: _select_value(el, sel, page_url) for key, sel in FIELDS.items()} for el in elements]


def _looks_blocked(status: int, html: str) -> bool:
    low = html[:20000].lower()
    return status in (403, 429) or any(m in low for m in CAPTCHA_MARKERS)


async def _fetch_html_results(healer: Healer, url: str) -> list[dict]:
    """Sayfayı kullanıcının Chrome profilinde açar (gerçek oturum/çerez), HTML'i ayrıştırır.

    Captcha kontrolü sadece sonuç bulunamadığında yapılır: normal SSR sayfalarında da
    "captcha" kelimesi (örn. recaptcha script'i) geçebildiği için.
    """
    bridge = healer.bridge
    await bridge.start()
    page = await bridge._context.new_page()
    try:
        for attempt in range(1, HTML_MAX_ATTEMPTS + 1):
            response = await page.goto(url, wait_until="domcontentloaded")
            status = response.status if response else 200
            html = await page.content()
            results = _parse_html(html, page.url)
            if results is not None:
                return results
            if not _looks_blocked(status, html):
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}: {url}")
                return []  # sayfa normal ama sonuç yok
            if attempt == HTML_MAX_ATTEMPTS:
                raise RuntimeError(f"Doğrulama {HTML_MAX_ATTEMPTS} denemede geçilemedi (HTTP {status}).")
            print(
                "🔔 Doğrulama gerekiyor (captcha / robot kontrolü). "
                "Lütfen açılan Chrome sekmesinde tamamlayın, sonra ENTER'a basın."
            )
            await asyncio.to_thread(input)
        return []
    finally:
        await page.close()


async def search(**kwargs) -> list[dict]:
    """Parametreler: PARAMETERS anahtarları (örn. search(origin="IST", destination="AYT", date="2026-12-01"))."""
    missing = [p for p in PARAMETERS if kwargs.get(p) in (None, "")]
    unknown = [k for k in kwargs if k not in PARAMETERS]
    if missing or unknown:
        raise TypeError(
            f"search() parametreleri: {', '.join(PARAMETERS)}"
            + (f" — eksik: {', '.join(missing)}" if missing else "")
            + (f" — bilinmeyen: {', '.join(unknown)}" if unknown else "")
        )
    query = kwargs.get("query")
    session = SessionManager(SITE_NAME)
    healer = Healer(session)
    params = _query_params(str(query)) if query is not None else {}
    if PARSER_TYPE == "html":
        try:
            return await _fetch_html_results(healer, _with_params(_base_url(), params))
        finally:
            await healer.close()
    try:
        data = await healer.fetch(
            _base_url(),
            method=METHOD,
            params=params,
            json=_build_payload(kwargs),
        )
    finally:
        await healer.close()
    items = _get_path(data, ITEMS_PATH) or []
    if not isinstance(items, list):
        return []
    return [{key: _get_path(item, path) for key, path in FIELDS.items()} for item in items]


# search(**kwargs)'ın gerçek parametreleri dışarıya (CLI/MCP doğrulaması, inspect) görünsün.
search.__signature__ = inspect.Signature(
    [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY) for name in PARAMETERS],
    return_annotation="list[dict]",
)


if __name__ == "__main__":
    # python client.py origin=IST destination=AYT   |   python client.py "saat" (tek query)
    args = sys.argv[1:]
    if len(args) == 1 and "=" not in args[0] and list(PARAMETERS) == ["query"]:
        kwargs = {"query": args[0]}
    else:
        kwargs = dict(a.split("=", 1) for a in args)
    results = asyncio.run(search(**kwargs))
    print(json.dumps(results[:5], ensure_ascii=False, indent=2))
