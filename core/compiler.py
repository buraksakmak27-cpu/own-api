"""AI Compiler — yakalanan trafikten site istemcisi (client.py + schema.json) üretir.

JSON API'ler (Trendyol, THY) ve SSR HTML sayfaları (Sahibinden, Hepsiburada) desteklenir.
HTML'de önce gömülü JSON (__NEXT_DATA__ vb.) aranır; yoksa CSS selector'larla ayrıştırılır
(üretilen client.py BeautifulSoup kullanır).

OpenAI API kullanır (OPENAI_API_KEY ortam değişkeni).
Model OPENAI_MODEL ile değiştirilebilir (varsayılan: gpt-4o).
"""

from __future__ import annotations

import json
import keyword
import os
import re
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, unquote, urlparse

from openai import AsyncOpenAI

from core.traffic_filter import filter_useful


def normalize_date(value, out_format: str = "%d-%m-%Y", refresh_past: bool = True, today=None):
    """Tarih metnini tek biçime (varsayılan THY'nin beklediği %d-%m-%Y) çevirir.

    - Ayraç/sıra farkını giderir: 02.03.2027 / 02/03/2027 / 2027-03-02 → 02-03-2027
    - Tek haneleri sıfırla doldurur: 2.3.2027 → 02-03-2027
    - ISO saat eki varsa atar: 2027-03-02T10:00 → 02-03-2027
    - refresh_past=True ise geçmişte kalan tarihi bugüne çeker (THY boş döndürmesin).
    - Tarih olarak çözülemezse değeri AYNEN döndürür (örn. 'IST').
    """
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return value
    head = re.split(r"[ T]", s, 1)[0]           # saat kısmını at
    parts = re.split(r"[-./]", head)
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return value
    a, b, c = parts
    if len(a) == 4:                              # YYYY-M-D
        y, m, d = a, b, c
    elif len(c) == 4:                            # D-M-YYYY (TR/AB)
        d, m, y = a, b, c
    else:
        return value                             # yıl belirsiz
    try:
        parsed = date(int(y), int(m), int(d))
    except ValueError:
        return value                             # 31-02 gibi geçersiz tarih
    if refresh_past:
        ref = today or date.today()
        if parsed < ref:
            parsed = ref
    return parsed.strftime(out_format)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = PROJECT_ROOT / "registry"

DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")

MAX_ENTRIES = 50          # AI'a gönderilecek en fazla istek (traffic_filter.MAX_ENTRIES)
MAX_BODY_CHARS = 2000     # her yanıt gövdesinden gönderilecek önizleme
MAX_HTML_CHARS = 50_000   # temizlenmiş HTML bundan uzunsa baştan+sondan kesilir, ortası atılır
MAX_HTML_FULL = 2         # tam (50K'ya kadar) gönderilecek HTML sayısı; diğerleri MAX_BODY_CHARS önizleme

PROMPT_TEMPLATE = """Aşağıda bir web sitesinden yakalanmış istekler var: JSON API yanıtları ve
sunucuda render edilmiş (SSR) HTML sayfaları. Her isteğin "format" alanı "json" veya "html"dir.
Kullanıcının amacı: {intent}.
Bu istekler arasından arama sonuçlarını dönen doğru isteği bul.

ÖNCELİK: Arama sonuçlarını içeren bir JSON API yanıtı varsa onu seç (`parser_type: "json"`).
Sonuçlar sadece HTML sayfasının içindeyse o HTML isteğini seç (`parser_type: "html"`).
Hiçbir istekte arama sonuçları yoksa SADECE şunu dön:
{{"error": "neden uygun istek bulunamadığının kısa açıklaması"}}

HTML SAYFALARI: Sana bir HTML sayfası gönderiyorum (script/style/svg temizlenmiş, çok uzunsa
ortası kesilmiş olabilir).

JSON-LD UYARISI: Eğer sayfada `application/ld+json` varsa, bunu SADECE tekil ürün detay sayfaları
için kullan. Arama sonuçları (liste) sayfalarında JSON-LD'ye ALDANMA: o etiket sitenin SEO
bilgilerini tutar, arama sonuçlarının listesini değil. Arama sonuçları her zaman HTML elementleridir.
İlanları/ürünleri seçmek için mutlaka bir CSS Selector (`items_selector`, örn: `.searchResultsItem`,
`.product-list-item`) bul. Her ilanın içindeki isim, fiyat ve link için de alt CSS Selector'lar üret:
- `embedded_json` ve `items_path` null olsun; `items_selector` ASLA boş olamaz.
- `items_selector`: her bir ürünü/ilanı (tek satır/kart) seçen CSS selector (örn: "tr.searchResultsItem").
- `fields`: öğenin İÇİNDE aranacak alt CSS selector'lar (örn: {{"name": "a.classifiedTitle", "price": "td.searchResultsPriceValue"}}).
  Varsayılan olarak etiketin metni alınır. Bir öznitelik gerekiyorsa `selector@öznitelik` yaz
  (örn: {{"link": "a.classifiedTitle@href"}}); öğenin kendi özniteliği için sadece "@href".
- Selector'lar sayfadaki gerçek, kalıcı class/id/öznitelikleri kullansın; rastgele üretilmiş
  (örn: "css-1x2y3z", "sc-abc123") class'lardan kaçın.
TEK İSTİSNA (gömülü veri JSON'u): Arama sonuçlarının TAM listesi `<script id="__NEXT_DATA__">` veya
`<script type="application/json">` içindeyse CSS yerine onu kullanabilirsin:
- `embedded_json`: o script etiketinin CSS selector'ı (örn: "script#__NEXT_DATA__").
- `items_path`: o JSON içindeki ürün/ilan listesinin noktalı yolu (örn: props.pageProps.products).
- `fields`: her alanın öğe içindeki noktalı JSON yolu.
`application/ld+json` etiketi ASLA `embedded_json` olarak kullanılamaz. Emin değilsen `items_selector` kullan.
Bana `parser_type: 'html'` formatında JSON dön. HTML isteklerinde `method` "GET" olmalı;
`query_param` / `query_templates` / `drop_params` kuralları GET ile aynıdır (arama kelimesi
sayfa URL'sinin hangi parametresinde?). `body_template` ve `parameters` null olmalı.

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

Uygun istek bulursan bana sadece şu formatta bir JSON dön:
{{
  "parser_type": "json veya html",
  "endpoint_name": "search",
  "request_index": "seçtiğin isteğin index numarası (sayı)",
  "url": "tam_url",
  "method": "GET veya POST",
  "body_template": "POST ise orijinal post_data JSON gövdesi (obje), GET ise null",
  "parameters": {{"kullanıcı_girdisi_adı": "body_template içindeki noktalı yol"}},
  "query_param": "arama kelimesinin doğrudan gönderileceği query parametresinin adı (örn: q) veya null. Sayfa yolu/kategori taşıyan bir parametre (örn: source=sr?tag=...) OLMAMALI",
  "query_templates": {{"source": "endpoint sayfa yolunu bir parametrede taşıyorsa (örn: source=sr%3Ftag%3D...), o parametre için sitenin ARAMA sayfası yolunun şablonu; arama kelimesi yerine {{query}} yaz (örn: sr?q={{query}}). Böyle parametre yoksa boş obje {{}} dön"}},
  "drop_params": ["yakalanan sayfanın bağlamını taşıyan, aramada silinmesi gereken parametreler (kategori, etiket, sıralama, source vb.)"],
  "items_path": "json (veya HTML'deki gömülü JSON) içindeki ürünlerin listesine giden yol (örn: data.products); HTML+CSS'te null",
  "embedded_json": "sadece html: gömülü JSON script'inin CSS selector'ı (örn: script#__NEXT_DATA__) veya null",
  "items_selector": "html'de ZORUNLU (sadece __NEXT_DATA__ istisnasında null): her ürünü/ilanı seçen CSS selector; json'da null",
  "fields": {{"name": "ürün adı yolu (örn: name) veya CSS selector", "price": "fiyat yolu (örn: price.current) veya CSS selector", "link": "html'de örn: a.title@href"}},
  "waf_detected": "true veya false (boolean) — aşağıdaki WAF kurallarına göre",
  "waf_origin_template": "WAF varsa ve page_url'de rastgele parametre (cId/token...) varsa, o değerin yerine {{uuid}} yazılmış page_url (örn: https://www.turkishairlines.com/tr-tr/ucak-bileti/rezervasyon/ic-hat-musaitlik/?cId={{uuid}}); aksi halde null",
  "waf_headers_to_capture": ["WAF varsa: yakalanacak x- başlık adları (örn: x-pgs-token, x-session-id); WAF yoksa null"],
  "origin_url": "isteğin yapıldığı sayfa ana siteden (www) farklı bir alt alan adındaysa onun origin'i (örn: https://web.site.com/), aksi halde null"
}}

WAF (Akamai/PerimeterX) TESPİTİ: Her isteğin "waf_headers" alanında, o istekte gönderilen `x-` ile
başlayan başlıkların SADECE ADLARI var (değerleri gizlilik için gönderilmez).
- Eğer yakalanan isteklerin herhangi birinde `x-bfp` veya benzeri (x- ile başlayan) gizli WAF
  başlıkları varsa, bu site bir WAF (Akamai/PerimeterX) kullanıyordur → `waf_detected: true`.
  (`x-requested-with`, `x-csrf-token`, `x-xsrf-token`, `x-client-timestamp` gibi standart başlıklar
  tek başına WAF işareti DEĞİLDİR.)
- WAF tespit edersen, o isteğin yapıldığı `page_url`'e bak. Eğer `page_url` içinde `cId=...` veya
  `token=...` gibi rastgele bir parametre varsa, `waf_origin_template` olarak o page_url'i dön ve
  rastgele değerin yerine `{{uuid}}` yaz (üretilen kod Healer'ı başlatırken
  `origin=f'...?cId={{uuid.uuid4()}}'` şeklinde dinamik bir URL kullanacak).
- Eğer `page_url`'de böyle bir parametre yoksa ama WAF varsa, yine de `waf_detected: true` dön ama
  `waf_origin_template` null olsun (Healer normal origin ile kullanılır).
- WAF yoksa `waf_detected: false`, `waf_origin_template: null`.
- Sana verilen 'waf_headers' dizisindeki (örn: `x-pgs-token`) başlık isimlerini
  `waf_headers_to_capture` olarak döndür (öncelikle seçtiğin isteğin waf_headers'ı; standart
  `x-requested-with` gibi başlıkları ekleme). Eğer WAF yoksa null dön.
- Eğer test isteği 406 (Not Acceptable) hatası alırsa, bu site eksik başlıklar istiyor demektir.
  Bu durumda `waf_detected: true` yap ve `waf_headers_to_capture` alanına sayfadaki tüm `x-`
  başlıklarını listele. (Yakalanan trafikte "status": 406 olan bir istek varsa da aynısını yap.)
  "Sayfadaki tüm x- başlıkları" = seçtiğin API isteğiyle AYNI host'a giden isteklerin x- başlıkları
  (örn. Pegasus: x-platform, x-version). Üçüncü taraf servislerin (google, firebase, analitik)
  başlıklarını ve `x-forwarded-for`, `x-real-ip`, `x-forwarded-host` gibi proxy başlıklarını ASLA koyma.

ORIGIN (CORS): Eğer API endpoint'i ana siteden farklı bir alt alan adındaysa (örn: web.site.com,
api.site.com), `origin_url` adında bir alan döndür ve buraya API'nin tam origin'ini yaz
(örn: https://web.site.com/). Ana site ile aynıysa null dön. Belirleyici olan, isteğin yapıldığı
SAYFADIR (`page_url`): istek web.site.com/booking sayfasından yapıldıysa origin_url
"https://web.site.com/" olur; istek www.site.com sayfasından api.site.com'a yapıldıysa (sayfa ana
sitedeyse) null dön.

ALAN EŞLEŞTİRME (fields): Uçuş sonuçlarında (`departureRouteList`), uçuş numarası genellikle
`flightNumber` veya `flightCode` içindedir. Fiyat ise `price`, `fare`, veya `amount` alanlarındadır.
Lütfen bu yapıyı çok dikkatli incele ve `fields` sözlüğünü (name ve price) doldur. `fields` ASLA
null veya boş olamaz; yollar `items_path` listesindeki TEK BİR öğeye göredir (örn: "flightNumber",
"fare.amount", "segments.0.flightCode").

İstekler:
{requests}
"""


_KEEP_SCRIPT_TYPES = ("application/json", "application/ld+json")


def _is_data_script(tag) -> bool:
    """Veri taşıyan script mi? (__NEXT_DATA__ veya type=application/json / ld+json)"""
    if tag.name != "script":
        return False
    if (tag.get("id") or "") == "__NEXT_DATA__":
        return True
    typ = (tag.get("type") or "").lower().split(";")[0].strip()
    return typ in _KEEP_SCRIPT_TYPES or typ.endswith("+json")


def _clean_html(html_text: str, limit: int = MAX_HTML_CHARS) -> str:
    """AI'a gönderilecek HTML'i küçültür.

    - <script>, <style>, <svg>, <noscript> etiketlerini içerikleriyle siler; veri taşıyan
      script'ler (__NEXT_DATA__, type="application/json") KORUNUR.
    - HTML yorumlarını siler, boşluk/yeni satırları sıkıştırır (minify).
    - Hâlâ `limit`ten uzunsa baştan ve sondan keser, ortasını atar.
    """
    from bs4 import BeautifulSoup, Comment  # lazy: sadece HTML işlenirken gerekli

    if not html_text:
        return ""
    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup.find_all(["script", "style", "svg", "noscript"]):
        if tag.decomposed or _is_data_script(tag):
            continue
        tag.decompose()
    for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
        comment.extract()

    text = str(soup)
    text = re.sub(r">\s+<", "><", text)   # etiketler arası boşluk
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > limit:
        cut = len(text) - limit
        head = limit // 2
        tail = limit - head
        text = f"{text[:head]} ...(ortası kesildi: {cut} karakter)... {text[-tail:]}"
    return text


def _is_html_entry(entry: dict) -> bool:
    return entry.get("format") == "html" or "text/html" in (entry.get("content_type") or "").lower()


def _is_ld_json(tag) -> bool:
    return "ld+json" in (tag.get("type") or "").lower()


class _SpecRetry(ValueError):
    """AI cevabı düzeltilebilir şekilde hatalı — hata mesajıyla AI'a tekrar sorulur."""


MAX_AI_ATTEMPTS = 3  # ilk soru + en fazla 2 düzeltme denemesi


def _extract_embedded_json(html_text: str, selector: str, items_path: str | None = None):
    """HTML'de selector'a uyan script'lerden ilk geçerli JSON'u döner (items_path varsa onu içereni)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup.select(selector):
        if _is_ld_json(tag):
            continue  # JSON-LD = SEO verisi, arama sonucu listesi değil
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (TypeError, ValueError):
            continue
        if not items_path or _path_exists(data, items_path):
            return data
    return None


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


def _waf_header_names(entry: dict) -> list[str]:
    """İstekteki x- başlıklarının SADECE adları (değerler — token/parmak izi — gönderilmez)."""
    headers = entry.get("headers") or {}
    return sorted({str(k).lower() for k in headers if str(k).lower().startswith("x-")})


def _summarize(entry: dict, index: int, full_html: bool = False) -> dict:
    """AI'a gönderilecek kısa özet. Header DEĞERLERİ/çerez GÖNDERİLMEZ (oturum gizliliği);
    WAF tespiti için sadece x- başlıklarının adları ve isteğin yapıldığı page_url gönderilir.

    HTML yanıtlar temizlenir (_clean_html); full_html=True ise MAX_HTML_CHARS'a kadar,
    değilse MAX_BODY_CHARS'lık önizleme gönderilir.
    """
    body = entry.get("body")
    is_html = _is_html_entry(entry)
    if is_html:
        preview = _clean_html(body if isinstance(body, str) else str(body or ""))
        if not full_html:
            preview = _truncate(preview)
    else:
        preview = json.dumps(body, ensure_ascii=False, default=str)
        if len(preview) > MAX_BODY_CHARS:
            preview = preview[:MAX_BODY_CHARS] + "...(kesildi)"
    return {
        "index": index,
        "format": "html" if is_html else "json",
        "url": entry.get("url"),
        "method": entry.get("method"),
        "status": entry.get("status"),
        "content_type": entry.get("content_type", ""),
        "post_data": _truncate(entry.get("post_data")),
        "page_url": entry.get("page_url"),
        "waf_headers": _waf_header_names(entry),
        "body_preview": preview,
    }


_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
# AI'ın {uuid} yerine yazabileceği varyantlar → {uuid}
_UUID_PLACEHOLDER_RE = re.compile(r"\{\s*(?:uuid(?:\.uuid4\(\))?|uuid4(?:\(\))?|UUID|cId|cid)\s*\}")


def _base_host(url: str) -> str:
    """Kayıt alanı (örn. www.turkishairlines.com, api.x.com.tr → x.com.tr) — aynı site kontrolü."""
    host = (urlparse(url).hostname or "").lower()
    labels = host.split(".")
    n = 3 if len(labels) >= 3 and labels[-2] in ("com", "net", "org", "gov", "edu", "co") and len(labels[-1]) == 2 else 2
    return ".".join(labels[-n:])


def _normalize_waf_origin(template, request_url: str) -> str | None:
    """waf_origin_template'i doğrular/normalize eder; geçersizse _SpecRetry fırlatır."""
    if template in (None, "", "null"):
        return None
    if not isinstance(template, str):
        raise _SpecRetry(f"waf_origin_template bir URL metni veya null olmalı: {template!r}")
    tpl = _UUID_PLACEHOLDER_RE.sub("{uuid}", template.strip())
    tpl = _UUID_RE.sub("{uuid}", tpl)  # AI gerçek UUID'yi kopyaladıysa yer tutucuya çevir
    leftover = re.findall(r"\{[^}]*\}", tpl.replace("{uuid}", ""))
    if leftover:
        raise _SpecRetry(
            f"waf_origin_template'te sadece {{uuid}} yer tutucusu olabilir, bulunan: {leftover}"
        )
    parts = urlparse(tpl.replace("{uuid}", "00000000-0000-4000-8000-000000000000"))
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise _SpecRetry(f"waf_origin_template geçerli bir http(s) URL'si değil: {template!r}")
    if _base_host(tpl) != _base_host(request_url):
        raise _SpecRetry(
            f"waf_origin_template ({parts.hostname}) seçilen isteğin sitesiyle "
            f"({urlparse(request_url).hostname}) aynı alan adında olmalı."
        )
    return tpl


def _normalize_waf_headers(raw, entries: list[dict], api_url: str) -> list[str] | None:
    """waf_headers_to_capture: küçük harfli, x- ile başlayan, API ile AYNI host'a giden isteklerde
    GERÇEKTEN görülmüş ve kopyalanması güvenli (healer._is_capturable) adlar."""
    from core.healer import _is_capturable

    if raw in (None, "", []):
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise _SpecRetry(f"waf_headers_to_capture bir başlık adı listesi veya null olmalı: {raw!r}")
    api_host = (urlparse(api_url).hostname or "").lower()
    seen = {
        h for e in entries
        if (urlparse(e.get("url") or "").hostname or "").lower() == api_host
        for h in _waf_header_names(e)
        if _is_capturable(h)
    }
    names, unknown = [], []
    for h in raw:
        name = str(h or "").strip().lower()
        if not name:
            continue
        if name in seen:
            if name not in names:
                names.append(name)
        else:
            unknown.append(name)
    if unknown and not names:
        raise _SpecRetry(
            f"waf_headers_to_capture içindeki başlıklar API host'una ({api_host}) giden isteklerde yok "
            f"veya kopyalanamaz: {unknown}. Sadece şunlardan seç: {sorted(seen) or '(hiç x- başlığı yok)'}"
        )
    if unknown:
        print(f"   ⚠️ API host'unda görülmeyen/kopyalanamaz başlıklar atlandı: {', '.join(unknown)}")
    return names or None


def _url_origin(url: str) -> str | None:
    parts = urlparse(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return f"{parts.scheme}://{parts.netloc}/"


def _resolve_origin_url(ai_value, entries: list[dict], spec: dict) -> str | None:
    """Healer'ın başlayacağı origin. None = healer._site_origin (sitenin www ana sayfası).

    Öncelik: isteğin GERÇEKTEN yapıldığı sayfanın (page_url) origin'i — tarayıcı isteğe o
    sayfadan izin verdiği için CORS orada kesin çalışır (Pegasus: web.flypgs.com/booking →
    https://web.flypgs.com/; Trendyol: www.trendyol.com → apigw'ye rağmen None).
    page_url yoksa AI'ın origin_url'i doğrulanarak kullanılır.
    """
    from core.healer import _site_origin

    default = _site_origin(spec["url"])
    entry = _find_entry(entries, spec)
    page_origin = _url_origin(entry.get("page_url")) if entry else None
    if page_origin and _base_host(page_origin) == _base_host(spec["url"]):
        chosen = page_origin
        ai_origin = _url_origin(ai_value) if isinstance(ai_value, str) else None
        if (ai_origin or default) != chosen:
            print(f"   ℹ️  origin_url isteğin yapıldığı sayfadan alındı: {chosen} (AI: {ai_value!r})")
    elif ai_value in (None, "", "null"):
        return None
    else:
        chosen = _url_origin(ai_value) if isinstance(ai_value, str) else None
        if not chosen:
            raise _SpecRetry(f"origin_url geçerli bir http(s) URL'si veya null olmalı: {ai_value!r}")
        if _base_host(chosen) != _base_host(spec["url"]):
            raise _SpecRetry(
                f"origin_url ({chosen}) API ile ({urlparse(spec['url']).hostname}) aynı alan adında olmalı."
            )
    return None if chosen == default else chosen


def _validate_json_fields(entries: list[dict], spec: dict, last_attempt: bool = False) -> None:
    """items_path ve fields yollarını yakalanan TAM JSON yanıt üzerinde dener."""
    entry = _find_entry(entries, spec)
    body = entry.get("body") if entry else None
    if not isinstance(body, (dict, list)):
        return  # yanıt yakalanmamış / JSON değil — doğrulanamaz
    items_path = str(spec["items_path"])
    items = _get_value(body, items_path) if _path_exists(body, items_path) else None
    if not isinstance(items, list) or not items:
        top = list(body)[:30] if isinstance(body, dict) else f"liste ({len(body)} öğe)"
        raise _SpecRetry(
            f"items_path {items_path!r} yakalanan yanıtta boş olmayan bir liste değil. "
            f"Yanıtın üst düzey anahtarları: {top}"
        )
    sample = [it for it in items[:20] if isinstance(it, (dict, list))]
    if not sample:
        return  # öğeler düz değer (string/sayı) — alt yol doğrulaması anlamsız
    missing = {
        key: path for key, path in spec["fields"].items()
        if not path or not any(_path_exists(it, str(path)) for it in sample)
    }
    if not missing:
        return
    if last_attempt and len(missing) < len(spec["fields"]):
        print(f"   ⚠️ Öğelerde bulunamayan alanlar çıkarıldı: {missing}")
        spec["fields"] = {k: v for k, v in spec["fields"].items() if k not in missing}
        return
    preview = json.dumps(sample[0], ensure_ascii=False, default=str)
    raise _SpecRetry(
        f"fields içindeki şu yollar '{items_path}' öğelerinde bulunamadı: {missing}. "
        f"Yollar tek bir öğeye göre olmalı. İlk öğenin yapısı: {_truncate(preview, 3000)}"
    )


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


# Üretilen client.py'deki Healer kurulumu (CLIENT_TEMPLATE'e {healer_factory} olarak girer;
# .format() ile işlenmez, süslü parantezler olduğu gibi kalır).
PLAIN_HEALER_FACTORY = '''def _new_healer(session) -> Healer:
    # ORIGIN_URL None ise Healer sitenin ana sayfasından (www) başlar.
    return Healer(session, origin=ORIGIN_URL, capture_headers=WAF_HEADERS_TO_CAPTURE)'''

WAF_HEALER_FACTORY = '''def _new_healer(session) -> Healer:
    """WAF: her çağrıda yeni cId/token ile, yakalanan sayfa üzerinden başlar."""
    return Healer(
        session,
        origin=WAF_ORIGIN_TEMPLATE.replace("{uuid}", str(uuid.uuid4())),
        capture_headers=WAF_HEADERS_TO_CAPTURE,
    )'''


CLIENT_TEMPLATE = '''"""{site_name} istemcisi — core.compiler tarafından otomatik üretildi.

Amaç: {intent}
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import sys
{uuid_import}from pathlib import Path
from urllib.parse import parse_qsl, quote, quote_plus, urlencode, urljoin, urlparse, urlunparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.healer import CAPTCHA_MARKERS, Healer, _with_params  # noqa: E402
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
# "json": API yanıtı | "html": SSR sayfa (BeautifulSoup ile ayrıştırılır).
PARSER_TYPE = {parser_type!r}
ITEMS_PATH = {items_path!r}
# html: gömülü JSON script'inin selector'ı (örn. script#__NEXT_DATA__) — varsa ITEMS_PATH/FIELDS JSON yoludur.
EMBEDDED_JSON = {embedded_json!r}
# html + gömülü JSON yok: her öğeyi seçen CSS selector; FIELDS alt selector'dır ("a.title@href" = öznitelik).
ITEMS_SELECTOR = {items_selector!r}
FIELDS = {fields!r}
HTML_MAX_ATTEMPTS = 3  # captcha/doğrulama sayfasında kullanıcıya tanınan deneme sayısı
# WAF (Akamai/PerimeterX): Healer, sitenin JS'inin ürettiği x- başlıklarını (x-bfp vb.) yakalar.
WAF_DETECTED = {waf_detected!r}
# Healer'ın açacağı sayfa; {{uuid}} her çağrıda yeni bir uuid4 ile değiştirilir (None = site ana sayfası).
WAF_ORIGIN_TEMPLATE = {waf_origin_template!r}
# Healer'ın sayfanın kendi isteklerinden yakalayıp bizim isteğe ekleyeceği başlıklar
# (None = varsayılan x-bfp modu).
WAF_HEADERS_TO_CAPTURE = {waf_headers_to_capture!r}
# API ana siteden (www) farklı bir alt alan adındaki sayfadan çağrılıyorsa (örn. web.flypgs.com)
# Healer o origin'den başlar; aksi halde CORS "Failed to fetch" verir. None = site ana sayfası.
ORIGIN_URL = {origin_url!r}


{healer_factory}


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
                return [{{key: _get_path(item, path) for key, path in FIELDS.items()}} for item in items]
        return None
    elements = soup.select(ITEMS_SELECTOR)
    if not elements:
        return None
    return [{{key: _select_value(el, sel, page_url) for key, sel in FIELDS.items()}} for el in elements]


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
                    raise RuntimeError(f"HTTP {{status}}: {{url}}")
                return []  # sayfa normal ama sonuç yok
            if attempt == HTML_MAX_ATTEMPTS:
                raise RuntimeError(f"Doğrulama {{HTML_MAX_ATTEMPTS}} denemede geçilemedi (HTTP {{status}}).")
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
            f"search() parametreleri: {{', '.join(PARAMETERS)}}"
            + (f" — eksik: {{', '.join(missing)}}" if missing else "")
            + (f" — bilinmeyen: {{', '.join(unknown)}}" if unknown else "")
        )
    query = kwargs.get("query")
    session = SessionManager(SITE_NAME)
    healer = _new_healer(session)
    params = _query_params(str(query)) if query is not None else {{}}
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
    return [{{key: _get_path(item, path) for key, path in FIELDS.items()}} for item in items]


# search(**kwargs)'ın gerçek parametreleri dışarıya (CLI/MCP doğrulaması, inspect) görünsün.
search.__signature__ = inspect.Signature(
    [inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY) for name in PARAMETERS],
    return_annotation="list[dict]",
)


if __name__ == "__main__":
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

    @staticmethod
    def _validate_html_spec(entries: list[dict], spec: dict) -> None:
        """AI'ın selector/yollarını yakalanan TAM HTML üzerinde dener (AI kesik kopyayı gördü)."""
        from bs4 import BeautifulSoup

        entry = _find_entry(entries, spec)
        if entry is None or not _is_html_entry(entry):
            raise ValueError(f"AI'ın seçtiği HTML isteği yakalanan trafikte bulunamadı: {spec['url']}")
        spec["url"] = entry["url"]
        html = entry.get("body") if isinstance(entry.get("body"), str) else ""

        if spec["embedded_json"]:
            data = _extract_embedded_json(html, spec["embedded_json"], spec["items_path"])
            items = _get_value(data, spec["items_path"]) if data is not None else None
            if not isinstance(items, list) or not items:
                raise _SpecRetry(
                    f"Gömülü JSON '{spec['embedded_json']}' içinde '{spec['items_path']}' yolunda "
                    "ilan/ürün listesi bulunamadı. items_selector boş olamaz, lütfen HTML elementlerini "
                    "seç (embedded_json ve items_path null olsun)."
                )
            print(f"   🧩 Gömülü JSON: {len(items)} öğe bulundu ({spec['embedded_json']}).")
            return

        soup = BeautifulSoup(html, "html.parser")
        try:
            elements = soup.select(spec["items_selector"])
        except Exception as e:
            raise _SpecRetry(f"Geçersiz CSS selector: {spec['items_selector']!r} ({e}).") from e
        if not elements:
            raise _SpecRetry(
                f"items_selector {spec['items_selector']!r} sayfada hiçbir öğe seçmedi. "
                "Lütfen ilanları/ürünleri gerçekten seçen bir CSS selector bul."
            )
        empty = []
        for key, sel in spec["fields"].items():
            css = str(sel or "").partition("@")[0].strip()
            try:
                if css and not any(el.select_one(css) for el in elements):
                    empty.append(key)
            except Exception as e:
                raise _SpecRetry(f"'{key}' için geçersiz CSS selector: {sel!r} ({e}).") from e
        print(f"   🧩 HTML: {len(elements)} öğe bulundu ({spec['items_selector']}).")
        if empty:
            print(f"   ⚠️ Şu alanlar hiçbir öğede bulunamadı: {', '.join(empty)}")

    async def _ask_ai(self, intent: str, entries: list[dict]) -> dict:
        html_idx = [i for i, e in enumerate(entries) if _is_html_entry(e)]
        html_full = set(html_idx[:MAX_HTML_FULL])  # sadece en öncelikli HTML'ler tam gider
        summaries = [_summarize(e, i, full_html=i in html_full) for i, e in enumerate(entries)]
        prompt = PROMPT_TEMPLATE.format(
            intent=intent,
            requests=json.dumps(summaries, ensure_ascii=False, indent=1),
        )
        messages = [{"role": "user", "content": prompt}]
        for attempt in range(1, MAX_AI_ATTEMPTS + 1):
            resp = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
            )
            content = resp.choices[0].message.content or ""
            try:
                spec = json.loads(_strip_json_fence(content))
            except json.JSONDecodeError as e:
                raise ValueError(f"AI geçerli JSON döndürmedi: {content[:500]}") from e
            try:
                return self._finalize_spec(entries, spec, last_attempt=attempt == MAX_AI_ATTEMPTS)
            except _SpecRetry as e:
                if attempt == MAX_AI_ATTEMPTS:
                    raise ValueError(f"AI {MAX_AI_ATTEMPTS} denemede geçerli bir cevap veremedi: {e}") from e
                print(f"   🔁 AI cevabı hatalı ({e}) — tekrar soruluyor ({attempt + 1}/{MAX_AI_ATTEMPTS})...")
                messages += [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": f"HATA: {e}\nLütfen düzeltip aynı JSON formatında tekrar dön."},
                ]
        raise RuntimeError("unreachable")

    def _finalize_spec(self, entries: list[dict], spec: dict, last_attempt: bool = False) -> dict:
        """AI cevabını doğrular/normalize eder. Düzeltilebilir hatalarda _SpecRetry fırlatır.

        last_attempt=True ise (son deneme) bazı alanları bulunamayan fields hata yerine
        uyarıyla ayıklanır; hiçbiri bulunamazsa yine hata verilir.
        """
        if spec.get("error"):
            raise ValueError(f"AI arama sonuçlarını dönen bir istek bulamadı: {spec['error']}")

        parser_type = str(spec.get("parser_type") or "json").strip().lower()
        if parser_type not in ("json", "html"):
            raise ValueError(f"Geçersiz parser_type: {parser_type!r}")
        spec["parser_type"] = parser_type
        spec["embedded_json"] = (spec.get("embedded_json") or None) if parser_type == "html" else None
        spec["items_selector"] = (spec.get("items_selector") or None) if parser_type == "html" else None
        if parser_type == "html" and spec["embedded_json"] and "ld+json" in str(spec["embedded_json"]).lower():
            raise _SpecRetry(
                "application/ld+json SEO verisidir, arama sonuçları listesi değildir. "
                "items_selector boş olamaz, lütfen HTML elementlerini seç (embedded_json ve items_path null olsun)."
            )
        if spec["embedded_json"]:
            spec["items_selector"] = None  # __NEXT_DATA__ / application/json varsa CSS'e gerek yok

        if parser_type == "html" and not spec["embedded_json"] and not spec["items_selector"]:
            # AI items_selector yerine items_path döndürdü (örn. JSON-LD'ye aldandı).
            raise _SpecRetry("items_selector boş olamaz, lütfen HTML elementlerini seç.")

        required = ["url"]  # fields aşağıda ayrıca (daha açıklayıcı mesajla) kontrol edilir
        if parser_type == "json" or spec["embedded_json"]:
            required.append("items_path")
        missing = [k for k in required if not spec.get(k)]
        if missing:
            raise _SpecRetry(f"Cevapta eksik alan(lar): {missing}. Lütfen bu alanları doldur.")
        if not isinstance(spec["fields"], dict) or not spec["fields"]:
            raise _SpecRetry(
                f"'fields' boş olmayan bir obje olmalı: {spec['fields']!r}. items_path listesindeki "
                "bir öğenin yapısını incele ve en az name ve price yollarını doldur."
            )
        if parser_type == "html" and not spec["embedded_json"]:
            spec["items_path"] = None

        spec.setdefault("endpoint_name", "search")
        spec["method"] = (spec.get("method") or "GET").upper()
        if parser_type == "html":
            if spec["method"] != "GET":
                raise ValueError("HTML (SSR) sayfalarında şu an sadece GET destekleniyor.")
            self._validate_html_spec(entries, spec)
        else:
            _validate_json_fields(entries, spec, last_attempt)

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

        # WAF: waf_detected bool olmalı; True ise waf_origin_template geçerli (aynı siteden) URL olmalı.
        waf = spec.get("waf_detected")
        if isinstance(waf, str):
            waf = waf.strip().lower() in ("true", "1", "yes", "evet")
        elif waf is None:
            waf = False
        elif not isinstance(waf, bool):
            raise _SpecRetry(f"waf_detected boolean (true/false) olmalı: {waf!r}")
        spec["waf_detected"] = waf
        spec["waf_origin_template"] = (
            _normalize_waf_origin(spec.get("waf_origin_template"), spec["url"]) if waf else None
        )
        spec["waf_headers_to_capture"] = (
            _normalize_waf_headers(spec.get("waf_headers_to_capture"), entries, spec["url"]) if waf else None
        )
        spec["origin_url"] = (
            None if parser_type == "html" else _resolve_origin_url(spec.get("origin_url"), entries, spec)
        )
        if spec["origin_url"]:
            print(f"   🌐 Origin (CORS): {spec['origin_url']}")
        if waf:
            print(f"   🛡️  WAF tespit edildi; origin: {spec['waf_origin_template'] or spec['origin_url'] or '(site ana sayfası)'}")
            print(f"   🔑 Yakalanacak başlıklar: {', '.join(spec['waf_headers_to_capture'] or ['(varsayılan: x-bfp)'])}")
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
            raise ValueError("Filtreden sonra işe yarar JSON/HTML isteği kalmadı.")

        spec = await self._ask_ai(intent, useful)

        out_dir = REGISTRY_DIR / site_name
        out_dir.mkdir(parents=True, exist_ok=True)

        # WAF + origin şablonu varsa uuid'li origin; aksi halde (WAF olsa da) normal Healer.
        use_waf_origin = bool(spec["waf_detected"] and spec["waf_origin_template"])
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
            parser_type=spec["parser_type"],
            items_path=spec["items_path"],
            embedded_json=spec["embedded_json"],
            items_selector=spec["items_selector"],
            fields=spec["fields"],
            waf_detected=spec["waf_detected"],
            waf_origin_template=spec["waf_origin_template"],
            waf_headers_to_capture=(
                tuple(spec["waf_headers_to_capture"]) if spec["waf_headers_to_capture"] else None
            ),
            origin_url=spec["origin_url"],
            uuid_import="import uuid\n" if use_waf_origin else "",
            healer_factory=WAF_HEALER_FACTORY if use_waf_origin else PLAIN_HEALER_FACTORY,
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
                "parser_type": spec["parser_type"],
                "items_path": spec["items_path"],
                "embedded_json": spec["embedded_json"],
                "items_selector": spec["items_selector"],
                "fields": spec["fields"],
                "waf_detected": spec["waf_detected"],
                "waf_origin_template": spec["waf_origin_template"],
                "waf_headers_to_capture": spec["waf_headers_to_capture"],
                "origin_url": spec["origin_url"],
            },
        }
        (out_dir / "schema.json").write_text(
            json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print(f"✅ registry/{site_name}/client.py ve schema.json üretildi.")
        print(json.dumps(spec, ensure_ascii=False, indent=2))
        return spec
