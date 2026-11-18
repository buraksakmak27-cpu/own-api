"""thy istemcisi — core.compiler tarafından otomatik üretildi.

Amaç: uçuş arama
"""

from __future__ import annotations

import copy
import inspect
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.healer import Healer  # noqa: E402
from core.session import SessionManager  # noqa: E402

SITE_NAME = 'thy'
ENDPOINT_NAME = 'search'
URL = 'https://www.turkishairlines.com/api/v1/availability'
METHOD = 'POST'
# Arama kelimesinin doğrudan gittiği parametre (yoksa None).
QUERY_PARAM = None
# Arama kelimesini bir sayfa yolu içinde taşıyan parametreler:
# {param: [şablon, ekstra_encode_sayısı]} — {query} yerine arama kelimesi yazılır.
QUERY_TEMPLATES = {}
# Yakalanan sayfanın bağlamını taşıyan parametreler (kategori, sıralama vb.) — silinir.
DROP_PARAMS = []
# POST: yakalanan JSON gövde şablonu (GET'te None).
BODY_TEMPLATE = {'selectedBookerSearch': 'R', 'selectedCabinClass': 'ECONOMY', 'moduleType': 'TICKETING', 'passengerTypeList': [{'quantity': 1, 'code': 'ADULT'}, {'quantity': 1, 'code': 'CHILD'}], 'originDestinationInformationList': [{'originAirportCode': 'IST', 'originCountryCode': 'TR', 'originMultiPort': False, 'originDomestic': True, 'destinationAirportCode': 'ESB', 'destinationCountryCode': 'TR', 'destinationMultiPort': False, 'destinationDomestic': True, 'departureDate': '02-03-2027', 'originCityCode': 'IST', 'destinationCityCode': 'ANK', 'originCity': 'İstanbul', 'destinationCity': 'Ankara'}, {'originAirportCode': 'ESB', 'originCountryCode': 'TR', 'originMultiPort': False, 'originDomestic': True, 'destinationAirportCode': 'IST', 'destinationCountryCode': 'TR', 'destinationMultiPort': False, 'destinationDomestic': True, 'departureDate': '09-03-2027', 'originCityCode': 'ANK', 'destinationCityCode': 'IST', 'originCity': 'Ankara', 'destinationCity': 'İstanbul'}], 'savedDate': '2026-11-17T09:48:08.309Z', 'preselectedOptionDetails': []}
# search(**kwargs) parametreleri: {ad: BODY_TEMPLATE içindeki yol}.
# GET'te {"query": None} — arama kelimesi URL'ye (QUERY_PARAM / QUERY_TEMPLATES) gider.
PARAMETERS = {'origin': 'originDestinationInformationList.0.originAirportCode', 'destination': 'originDestinationInformationList.0.destinationAirportCode', 'departure_date': 'originDestinationInformationList.0.departureDate', 'return_date': 'originDestinationInformationList.1.departureDate'}
ITEMS_PATH = 'data.originDestinationInformationList.0.originDestinationOptionList'
FIELDS = {'name': 'segmentList.0.flightCode.flightNumber', 'airline': 'segmentList.0.flightCode.airlineCode', 'from': 'segmentList.0.departureAirportCode', 'to': 'segmentList.0.arrivalAirportCode', 'departure': 'segmentList.0.departureDateTime', 'arrival': 'segmentList.0.arrivalDateTime', 'price': 'startingPrice.amount', 'currency': 'startingPrice.currencyCode'}


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


async def search(**kwargs) -> list[dict]:
    """Parametreler: PARAMETERS anahtarları (örn. search(origin="IST", destination="AYT", date="2026-12-01")).

    return_date opsiyoneldir: boş/verilmemişse tek yön uçuş aranır.
    """
    optional = {"return_date"}  # tek yön uçuş için opsiyonel
    missing = [p for p in PARAMETERS if p not in optional and kwargs.get(p) in (None, "")]
    unknown = [k for k in kwargs if k not in PARAMETERS]
    if missing or unknown:
        raise TypeError(
            f"search() parametreleri: {', '.join(PARAMETERS)}"
            + (f" — eksik: {', '.join(missing)}" if missing else "")
            + (f" — bilinmeyen: {', '.join(unknown)}" if unknown else "")
        )
    # Opsiyonel parametreler payload kurulumunda KeyError vermesin.
    for _p in optional:
        kwargs.setdefault(_p, "")

    # Tarihleri THY'nin beklediği biçime (GG-AA-YYYY) çevir.
    from core.compiler import normalize_date  # noqa: E402
    if kwargs.get("departure_date"):
        kwargs["departure_date"] = normalize_date(kwargs["departure_date"])
    round_trip = bool(kwargs.get("return_date"))
    if round_trip:
        kwargs["return_date"] = normalize_date(kwargs["return_date"])

    query = kwargs.get("query")
    session = SessionManager(SITE_NAME)
    payload = _build_payload(kwargs)
    # Tek yön: dönüş bacağını kaldır VE arama tipini "O" (tek yön) yap.
    # THY'de selectedBookerSearch: "R"=gidiş-dönüş, "O"=tek yön. Sadece bacağı silmek
    # yetmiyor; tip "R" kalırsa THY ikinci bacak bekler ve boş döner.
    if not round_trip and isinstance(payload, dict):
        legs = payload.get("originDestinationInformationList")
        if isinstance(legs, list) and len(legs) > 1:
            del legs[1]
        if "selectedBookerSearch" in payload:
            payload["selectedBookerSearch"] = "O"
    if isinstance(payload, dict) and "savedDate" in payload:
        # Her aramada şimdiki zaman (UTC, kayıttaki gibi milisaniyeli): 2026-11-17T09:48:08.309Z
        payload["savedDate"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    healer = Healer(session, origin=f"https://www.turkishairlines.com/tr-tr/ucak-bileti/rezervasyon/ic-hat-musaitlik/?cId={uuid.uuid4()}")
    try:
        data = await healer.fetch(
            _base_url(),
            method=METHOD,
            params=_query_params(str(query)) if query is not None else {},
            json=payload,
        )
    finally:
        await healer.close()
    items = _get_path(data, ITEMS_PATH) or []
    if not isinstance(items, list):
        return []
    return [{key: _get_path(item, path) for key, path in FIELDS.items()} for item in items]


# search(**kwargs)'ın gerçek parametreleri dışarıya (CLI/MCP doğrulaması, inspect) görünsün.
# OPTIONAL_PARAMS varsayılan "" alır → CLI/API bind'i tek yön uçuşta (return_date'siz) reddetmesin.
OPTIONAL_PARAMS = {"return_date"}
search.__signature__ = inspect.Signature(
    [
        inspect.Parameter(
            name, inspect.Parameter.KEYWORD_ONLY,
            default="" if name in OPTIONAL_PARAMS else inspect.Parameter.empty,
        )
        for name in PARAMETERS
    ],
    return_annotation="list[dict]",
)


if __name__ == "__main__":
    import asyncio
    import json

    # python client.py origin=IST destination=AYT   |   python client.py "saat" (tek query)
    args = sys.argv[1:]
    if len(args) == 1 and "=" not in args[0] and list(PARAMETERS) == ["query"]:
        kwargs = {"query": args[0]}
    else:
        kwargs = dict(a.split("=", 1) for a in args)
    results = asyncio.run(search(**kwargs))
    print(json.dumps(results[:5], ensure_ascii=False, indent=2))
