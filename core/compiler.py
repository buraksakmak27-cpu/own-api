"""AI Compiler — yakalanan trafikten site istemcisi (client.py + schema.json) üretir.

OpenAI API kullanır (OPENAI_API_KEY ortam değişkeni).
Model OPENAI_MODEL ile değiştirilebilir (varsayılan: gpt-4o).
"""

from __future__ import annotations

import json
import keyword
import os
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, unquote, urlparse

from openai import AsyncOpenAI

from core.traffic_filter import filter_useful

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = PROJECT_ROOT / "registry"

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

MAX_ENTRIES = 50          # AI'a gönderilecek en fazla istek (traffic_filter.MAX_ENTRIES)
MAX_BODY_CHARS = 2000     # her yanıt gövdesinden gönderilecek önizleme

PROMPT_TEMPLATE = """Aşağıda bir web sitesinden yakalanmış JSON API istekleri var.
Kullanıcının amacı: {intent}.
Bu istekler arasından arama sonuçlarını dönen doğru endpoint'i bul.

ÖNEMLİ: Sana verilen trafikte SADECE `application/json` içerik tipine sahip yanıtları seç
(her isteğin "content_type" alanına bak). `text/html` dönen istekleri asla seçme.
Eğer arama sonuçları JSON değil de HTML içinde geliyorsa, uygun bir JSON endpoint'i bulana
kadar diğer istekleri incele. Eğer hiç JSON yoksa (veya arama sonuçlarını dönen bir JSON
endpoint'i yoksa) hata döndür; bu durumda SADECE şunu dön:
{{"error": "neden uygun JSON endpoint bulunamadığının kısa açıklaması"}}

POST İSTEKLERİ: Her isteğin "post_data" alanına da bak. Eğer arama kelimesi URL'de değil de
POST isteğinin `post_data` (JSON body) içinde geçiyorsa, bu endpoint'i seç ve `method: "POST"` dön.

ÇOKLU PARAMETRE: Eğer bir POST isteği birden fazla kullanıcı girdisi (örn: kalkış, varış, tarih)
içeriyorsa, bunları tespit et. Bana `body_template` (orijinal POST gövdesi) ve `parameters`
(kullanıcının sağlaması gereken alanların bir sözlüğü, örn:
{{"origin": "departureAirport", "destination": "arrivalAirport", "date": "departureDate"}}) döndür.
- `parameters` anahtarları kısa, İngilizce, snake_case Python değişken adları olsun (örn: origin, return_date).
- Değerler, alanın `body_template` içindeki noktalı yoludur (iç içe: `search.origin.code`, liste: `legs.0.date`).
- Sadece kullanıcının gerçekten değiştireceği girdileri koy; oturum/kimlik/token/sabit ayar alanlarını koyma.
- Tek bir arama kelimesi varsa: {{"query": "<yol>"}}.
GET isteklerinde `body_template` ve `parameters` null olmalı.

Uygun endpoint bulursan bana sadece şu formatta bir JSON dön:
{{
  "endpoint_name": "search",
  "request_index": "seçtiğin isteğin index numarası (sayı)",
  "url": "tam_url",
  "method": "GET veya POST",
  "body_template": "POST ise orijinal post_data JSON gövdesi (obje), GET ise null",
  "parameters": {{"kullanıcı_girdisi_adı": "body_template içindeki noktalı yol"}},
  "query_param": "arama kelimesinin doğrudan gönderileceği query parametresinin adı (örn: q) veya null. Sayfa yolu/kategori taşıyan bir parametre (örn: source=sr?tag=...) OLMAMALI",
  "query_templates": {{"source": "endpoint sayfa yolunu bir parametrede taşıyorsa (örn: source=sr%3Ftag%3D...), o parametre için sitenin ARAMA sayfası yolunun şablonu; arama kelimesi yerine {{query}} yaz (örn: sr?q={{query}}). Böyle parametre yoksa boş obje {{}} dön"}},
  "drop_params": ["yakalanan sayfanın bağlamını taşıyan, aramada silinmesi gereken parametreler (kategori, etiket, sıralama, source vb.)"],
  "items_path": "json içindeki ürünlerin listesine giden yol (örn: data.products)",
  "fields": {{"name": "ürün adı yolu (örn: name)", "price": "fiyat yolu (örn: price.current)"}}
}}

İstekler:
{requests}
"""


def _truncate(text, limit: int = MAX_BODY_CHARS):
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[:limit] + "...(kesildi)"


def _path_exists(data, path: str) -> bool:
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return False
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return False
    return True


def _get_value(data, path: str):
    cur = data
    for part in path.split("."):
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    return cur


def _input_schema(parameters: dict, samples: dict) -> dict:
    """MCP/OpenAI tool şeması: her parametre, şablondaki örnek değerin tipiyle."""
    if list(parameters) == ["query"] and parameters["query"] is None:
        return {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Arama kelimesi"}},
            "required": ["query"],
        }
    props = {}
    for name, path in parameters.items():
        sample = samples.get(name)
        if isinstance(sample, bool):
            typ = "boolean"
        elif isinstance(sample, int):
            typ = "integer"
        elif isinstance(sample, float):
            typ = "number"
        else:
            typ = "string"
        desc = str(path)
        if sample not in (None, ""):
            desc += f" (örnek format: {str(sample)[:40]})"
        props[name] = {"type": typ, "description": desc}
    return {"type": "object", "properties": props, "required": list(parameters)}


def _find_entry(entries: list[dict], spec: dict) -> dict | None:
    """AI'ın seçtiği isteği bulur: önce request_index, sonra url+method eşleşmesi."""
    idx = spec.get("request_index")
    try:
        idx = int(idx)
    except (TypeError, ValueError):
        idx = None
    if idx is not None and 0 <= idx < len(entries):
        e = entries[idx]
        if (e.get("method") or "").upper() == spec["method"]:
            return e
    for e in entries:
        if e.get("url") == spec["url"] and (e.get("method") or "").upper() == spec["method"]:
            return e
    return None


def _summarize(entry: dict, index: int) -> dict:
    """AI'a gönderilecek kısa özet. Header/çerez GÖNDERİLMEZ (oturum gizliliği)."""
    body = entry.get("body")
    preview = json.dumps(body, ensure_ascii=False, default=str)
    if len(preview) > MAX_BODY_CHARS:
        preview = preview[:MAX_BODY_CHARS] + "...(kesildi)"
    return {
        "index": index,
        "url": entry.get("url"),
        "method": entry.get("method"),
        "status": entry.get("status"),
        "content_type": entry.get("content_type", ""),
        "post_data": _truncate(entry.get("post_data")),
        "body_preview": preview,
    }


def _strip_json_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _looks_like_page_context(value: str) -> bool:
    """Değer, encode edilmiş bir sayfa yolu/sorgusu mu? (örn: sr%253Ftag%253D...)"""
    v = value
    for _ in range(3):  # çift/üçlü encode edilmiş olabilir
        v = unquote(v)
    return v.startswith("/") or "?" in v or ("=" in v and "&" in v)


def _encode_depth(value: str) -> int:
    """Değer kaç kez URL-encode edilmiş? (parse_qsl'in çözdüğü katman hariç)"""
    depth = 0
    while depth < 3 and unquote(value) != value:
        value = unquote(value)
        depth += 1
    return depth


def _context_params(url: str) -> list[str]:
    return [
        k for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True)
        if _looks_like_page_context(v)
    ]


def _guess_query_param(url: str) -> str:
    params = parse_qs(urlparse(url).query)
    for cand in ("q", "query", "keyword", "search", "term", "text", "k", "s"):
        if cand in params:
            return cand
    return "q"


CLIENT_TEMPLATE = '''"""{site_name} istemcisi — core.compiler tarafından otomatik üretildi.

Amaç: {intent}
"""

from __future__ import annotations

import copy
import inspect
import sys
from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.healer import Healer  # noqa: E402
from core.session import SessionManager  # noqa: E402

SITE_NAME = {site_name!r}
ENDPOINT_NAME = {endpoint_name!r}
URL = {url!r}
METHOD = {method!r}
# Arama kelimesinin doğrudan gittiği parametre (yoksa None).
QUERY_PARAM = {query_param!r}
# Arama kelimesini bir sayfa yolu içinde taşıyan parametreler:
# {{param: [şablon, ekstra_encode_sayısı]}} — {{query}} yerine arama kelimesi yazılır.
QUERY_TEMPLATES = {query_templates!r}
# Yakalanan sayfanın bağlamını taşıyan parametreler (kategori, sıralama vb.) — silinir.
DROP_PARAMS = {drop_params!r}
# POST: yakalanan JSON gövde şablonu (GET'te None).
BODY_TEMPLATE = {body_template!r}
# search(**kwargs) parametreleri: {{ad: BODY_TEMPLATE içindeki yol}}.
# GET'te {{"query": None}} — arama kelimesi URL'ye (QUERY_PARAM / QUERY_TEMPLATES) gider.
PARAMETERS = {parameters!r}
ITEMS_PATH = {items_path!r}
FIELDS = {fields!r}


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
            f"'{{name}}' için geçersiz değer: {{value!r}} (beklenen: {{type(sample).__name__}})"
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
    drop = set(DROP_PARAMS) | set(QUERY_TEMPLATES) | {{QUERY_PARAM}}
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in drop]
    return urlunparse(parts._replace(query=urlencode(params)))


def _query_params(query: str) -> dict:
    params = {{}}
    for name, (template, depth) in QUERY_TEMPLATES.items():
        value = template.replace("{{query}}", quote_plus(query))
        for _ in range(depth):
            value = quote(value, safe="")
        params[name] = value
    if QUERY_PARAM:
        params[QUERY_PARAM] = query
    return params


async def search(**kwargs) -> list[dict]:
    """Parametreler: PARAMETERS anahtarları (örn. search(origin="IST", destination="AYT", date="2026-12-01"))."""
    missing = [p for p in PARAMETERS if kwargs.get(p) in (None, "")]
    unknown = [k for k in kwargs if k not in PARAMETERS]
    if missing or unknown:
        raise TypeError(
            f"search() parametreleri: {{', '.join(PARAMETERS)}}"
            + (f" — eksik: {{', '.join(missing)}}" if missing else "")
            + (f" — bilinmeyen: {{', '.join(unknown)}}" if unknown else "")
        )
    query = kwargs.get("query")
    session = SessionManager(SITE_NAME)
    healer = Healer(session)
    try:
        data = await healer.fetch(
            _base_url(),
            method=METHOD,
            params=_query_params(str(query)) if query is not None else {{}},
            json=_build_payload(kwargs),
        )
    finally:
        await healer.close()
    items = _get_path(data, ITEMS_PATH) or []
    if not isinstance(items, list):
        return []
    return [{{key: _get_path(item, path) for key, path in FIELDS.items()}} for item in items]


# search(**kwargs)'ın gerçek parametreleri dışarıya (CLI/MCP doğrulaması, inspect) görünsün.
search.__signature__ = inspect.Signature(
    [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY) for name in PARAMETERS],
    return_annotation="list[dict]",
)


if __name__ == "__main__":
    import asyncio
    import json

    # python client.py origin=IST destination=AYT   |   python client.py "saat" (tek query)
    args = sys.argv[1:]
    if len(args) == 1 and "=" not in args[0] and list(PARAMETERS) == ["query"]:
        kwargs = {{"query": args[0]}}
    else:
        kwargs = dict(a.split("=", 1) for a in args)
    results = asyncio.run(search(**kwargs))
    print(json.dumps(results[:5], ensure_ascii=False, indent=2))
'''


class Compiler:
    def __init__(self, model: str = DEFAULT_MODEL) -> None:
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY ortam değişkeni tanımlı değil.")
        self.model = model
        self.client = AsyncOpenAI(api_key=api_key)

    async def _ask_ai(self, intent: str, entries: list[dict]) -> dict:
        summaries = [_summarize(e, i) for i, e in enumerate(entries)]
        prompt = PROMPT_TEMPLATE.format(
            intent=intent,
            requests=json.dumps(summaries, ensure_ascii=False, indent=1),
        )
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or ""
        try:
            spec = json.loads(_strip_json_fence(content))
        except json.JSONDecodeError as e:
            raise ValueError(f"AI geçerli JSON döndürmedi: {content[:500]}") from e

        if spec.get("error"):
            raise ValueError(
                f"AI uygun bir JSON API bulamadı: {spec['error']} "
                "(site arama sonuçlarını HTML olarak dönüyor olabilir)."
            )

        missing = [k for k in ("url", "items_path", "fields") if not spec.get(k)]
        if missing:
            raise ValueError(f"AI cevabında eksik alan(lar): {missing} — {spec}")
        if not isinstance(spec["fields"], dict):
            raise ValueError(f"'fields' bir obje olmalı: {spec['fields']!r}")

        spec.setdefault("endpoint_name", "search")
        spec["method"] = (spec.get("method") or "GET").upper()

        # POST: gövde şablonu + kullanıcı parametreleri ({ad: yol}); GET: tek "query".
        ai_body = spec.get("body_template")
        ai_params = spec.get("parameters")
        legacy_path = spec.get("body_query_path")  # eski tek-parametre formatı
        spec["body_template"] = None
        spec["parameters"] = {"query": None}
        spec["param_samples"] = {}
        if spec["method"] == "POST":
            entry = _find_entry(entries, spec)
            body_template = None
            if entry is not None and entry.get("post_data"):
                # Yakalanan gerçek gövde esas alınır (AI'a giden kopya kesilmiş olabilir).
                try:
                    body_template = json.loads(entry["post_data"])
                except (TypeError, json.JSONDecodeError) as e:
                    raise ValueError("POST gövdesi JSON değil; şu an sadece JSON gövde destekleniyor.") from e
                spec["url"] = entry["url"]  # kesik/yanlış kopyalanmış URL'ye karşı
            elif isinstance(ai_body, (dict, list)):
                body_template = ai_body
            if body_template is None:
                raise ValueError(f"AI POST seçti ama gövde (post_data/body_template) bulunamadı: {spec['url']}")

            if not isinstance(ai_params, dict) or not ai_params:
                ai_params = {"query": legacy_path} if legacy_path else {}
            if not ai_params:
                raise ValueError("AI POST gövdesinde kullanıcı parametresi (parameters) bildirmedi.")

            parameters: dict[str, str] = {}
            for name, path in ai_params.items():
                name, path = str(name).strip(), str(path or "").strip()
                if not name.isidentifier() or keyword.iskeyword(name):
                    raise ValueError(f"Geçersiz parametre adı: {name!r} (Python değişken adı olmalı).")
                if not path or not _path_exists(body_template, path):
                    raise ValueError(f"'{name}' için yol POST gövdesinde bulunamadı: {path!r}")
                parameters[name] = path
            spec["body_template"] = body_template
            spec["parameters"] = parameters
            spec["param_samples"] = {n: _get_value(body_template, p) for n, p in parameters.items()}

        url_params = dict(parse_qsl(urlparse(spec["url"]).query, keep_blank_values=True))
        context = set(_context_params(spec["url"]))

        # Arama kelimesini sayfa yolu içinde taşıyan parametreler (örn: source=sr?q={query})
        raw_templates = spec.get("query_templates") or {}
        templates: dict[str, list] = {}
        if isinstance(raw_templates, dict):
            for name, tpl in raw_templates.items():
                if isinstance(tpl, str) and "{query}" in tpl and name in url_params:
                    templates[name] = [tpl, _encode_depth(url_params[name])]

        qp = spec.get("query_param") or None
        if spec["method"] == "POST" and "query" not in spec["parameters"]:
            qp, templates = None, {}  # URL'ye gidecek tek bir "query" yok
        if qp is None and not templates and spec["method"] != "POST":
            qp = _guess_query_param(spec["url"])
        if qp in context or qp in templates:
            # sayfa bağlamı parametresi doğrudan arama kelimesi olamaz
            qp = None if templates else "q"

        drop = spec.get("drop_params") or []
        if not isinstance(drop, list):
            drop = [drop]
        drop_set = ({str(d) for d in drop} | context) - set(templates) - {qp}

        spec["query_param"] = qp
        spec["query_templates"] = templates
        spec["drop_params"] = sorted(drop_set)
        return spec

    async def compile(
        self,
        site_name: str,
        intent: str,
        traffic_file: str = "captured_traffic.json",
    ) -> dict:
        path = Path(traffic_file)
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        traffic = json.loads(path.read_text(encoding="utf-8"))

        useful = filter_useful(traffic)[:MAX_ENTRIES]
        if not useful:
            raise ValueError("Filtreden sonra işe yarar JSON isteği kalmadı.")

        spec = await self._ask_ai(intent, useful)

        out_dir = REGISTRY_DIR / site_name
        out_dir.mkdir(parents=True, exist_ok=True)

        client_code = CLIENT_TEMPLATE.format(
            site_name=site_name,
            intent=intent,
            endpoint_name=spec["endpoint_name"],
            url=spec["url"],
            method=spec["method"],
            query_param=spec["query_param"],
            drop_params=spec["drop_params"],
            query_templates=spec["query_templates"],
            body_template=spec["body_template"],
            parameters=spec["parameters"],
            items_path=spec["items_path"],
            fields=spec["fields"],
        )
        (out_dir / "client.py").write_text(client_code, encoding="utf-8")

        schema = {
            "name": f"{site_name}_{spec['endpoint_name']}",
            "description": f"{site_name} üzerinde {intent}. Sonuç alanları: "
            + ", ".join(spec["fields"].keys()),
            "inputSchema": _input_schema(spec["parameters"], spec["param_samples"]),
            "endpoint": {
                "url": spec["url"],
                "method": spec["method"],
                "query_param": spec["query_param"],
                "drop_params": spec["drop_params"],
                "query_templates": spec["query_templates"],
                "parameters": spec["parameters"],
                "items_path": spec["items_path"],
                "fields": spec["fields"],
            },
        }
        (out_dir / "schema.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print(f"✅ registry/{site_name}/client.py ve schema.json üretildi.")
        print(json.dumps(spec, ensure_ascii=False, indent=2))
        return spec
