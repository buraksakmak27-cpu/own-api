"""trendyol istemcisi — core.compiler tarafından otomatik üretildi.

Amaç: ürün arama
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.healer import Healer  # noqa: E402
from core.session import SessionManager  # noqa: E402

SITE_NAME = 'trendyol'
ENDPOINT_NAME = 'search'
URL = 'https://apigw.trendyol.com/discovery-sfint-browsing-service/api/search-feed/products?source=sr%253Ftag%253Dkategori_encoksatanlar%2526sst%253DBEST_SELLER%2526tag%253Dkategori_encoksatanlar%2526tag%253Dkategori_encoksatanlar%2526tag%253Dkategori_encoksatanlar&channelId=1'
METHOD = 'GET'
# Arama kelimesinin doğrudan gittiği parametre (yoksa None).
QUERY_PARAM = None
# Arama kelimesini bir sayfa yolu içinde taşıyan parametreler:
# {param: [şablon, ekstra_encode_sayısı]} — {query} yerine arama kelimesi yazılır.
QUERY_TEMPLATES = {'source': ['sr?q={query}', 1]}
# Yakalanan sayfanın bağlamını taşıyan parametreler (kategori, sıralama vb.) — silinir.
DROP_PARAMS = []
ITEMS_PATH = 'contents'
FIELDS = {'name': 'name', 'price': 'sanitizedPrice.finalPrice.value'}


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


async def search(query: str) -> list[dict]:
    session = SessionManager(SITE_NAME)
    healer = Healer(session)
    try:
        data = await healer.fetch(_base_url(), method=METHOD, params=_query_params(query))
    finally:
        await healer.close()
    items = _get_path(data, ITEMS_PATH) or []
    if not isinstance(items, list):
        return []
    return [{key: _get_path(item, path) for key, path in FIELDS.items()} for item in items]


if __name__ == "__main__":
    import asyncio
    import json

    results = asyncio.run(search(sys.argv[1] if len(sys.argv) > 1 else "telefon"))
    print(json.dumps(results[:5], ensure_ascii=False, indent=2))
