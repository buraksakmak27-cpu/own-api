"""Factory — bir siteyi uçtan uca ekler: kayıt → AI derleme → canlı test.

    own-api add trendyol https://www.trendyol.com --intent "ürün arama"
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import re
from datetime import date, datetime
from pathlib import Path
from types import ModuleType
from typing import Optional

from core.compiler import Compiler
from core.recorder import Recorder

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = PROJECT_ROOT / "registry"

TEST_ATTEMPTS = 3
TEST_RETRY_DELAY = 3  # sn


def _load_client(site_name: str) -> ModuleType:
    """Yeni üretilen client.py'yi her seferinde diskten taze yükler."""
    path = REGISTRY_DIR / site_name / "client.py"
    spec = importlib.util.spec_from_file_location(f"registry.{site_name}.client", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


async def _record(url: str, intent: str) -> list[dict]:
    recorder = Recorder()
    try:
        return await recorder.capture(url, intent)
    finally:
        # Test çağrısı aynı profili açacak; Chrome burada kapanmalı.
        await recorder.close()


def _param_names(client: ModuleType) -> list[str]:
    """search()'ün parametre adları (A5: search(**kwargs) + __signature__)."""
    sig = inspect.signature(client.search)
    return [
        n for n, p in sig.parameters.items()
        if p.kind not in (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)
    ]


# Tarih biçimleri: (regex, strptime biçimi). Gün-önce (TR/AB) biçimleri ay-önceden önce denenir.
_DATE_FORMATS = [
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})"), "%Y-%m-%d"),      # 2026-11-20 (+ ISO saat kısmı)
    (re.compile(r"^(\d{4})/(\d{2})/(\d{2})"), "%Y/%m/%d"),      # 2026/11/20
    (re.compile(r"^(\d{2})-(\d{2})-(\d{4})"), "%d-%m-%Y"),      # 20-11-2026 (THY)
    (re.compile(r"^(\d{2})\.(\d{2})\.(\d{4})"), "%d.%m.%Y"),  # 20.11.2026
    (re.compile(r"^(\d{2})/(\d{2})/(\d{4})"), "%d/%m/%Y"),      # 20/11/2026
    (re.compile(r"^(\d{2})/(\d{2})/(\d{4})"), "%m/%d/%Y"),      # 11/20/2026 (ABD)
]


def _refresh_date(value, today: Optional[date] = None):
    """Geçmişte kalmış bir tarih metnini, AYNI BİÇİMDE bugünün tarihiyle değiştirir.

    '02-03-2025' → '17-11-2026', '2025-01-05T10:00:00' → '2026-11-17T10:00:00'.
    Tarih değilse veya gelecekteyse değeri olduğu gibi döndürür.
    """
    if not isinstance(value, str):
        return value
    today = today or date.today()
    for rx, fmt in _DATE_FORMATS:
        m = rx.match(value)
        if not m:
            continue
        head = m.group(0)
        try:
            parsed = datetime.strptime(head, fmt).date()
        except ValueError:
            continue  # örn. 11/20 gün-önce ayrıştırılamaz → sonraki biçim
        if parsed >= today:
            return value
        return today.strftime(fmt) + value[len(head):]  # saat/kalan kısım korunur
    return value


def _test_kwargs(client: ModuleType, test_query: str, test_params: Optional[dict]) -> dict:
    """Test çağrısının argümanları.

    Tek "query" → {"query": test_query}. Çoklu parametre (örn. THY) → test_params,
    verilmeyenler için kayıt sırasında yakalanan gövdedeki gerçek değerler.
    """
    names = _param_names(client)
    if not names:  # eski/serbest **kwargs client
        return {"query": test_query}
    test_params = test_params or {}
    paths = getattr(client, "PARAMETERS", {}) or {}
    body = getattr(client, "BODY_TEMPLATE", None)
    kwargs, missing = {}, []
    for name in names:
        if name in test_params:
            kwargs[name] = test_params[name]
        elif paths.get(name) and body is not None:
            kwargs[name] = client._get_path(body, paths[name])  # kayıttaki değer
        elif name == "query":
            kwargs[name] = test_query
        else:
            missing.append(name)
    if missing:
        raise ValueError(f"Test için değer bulunamadı: {', '.join(missing)}")

    # Kayıttan gelen geçmiş tarihler (örn. THY departure_date) bugüne çekilir; aksi halde
    # sunucu "geçmiş tarih" diye boş döner. Elle verilen test_params'a dokunulmaz.
    for name, value in kwargs.items():
        if name in test_params:
            continue
        fresh = _refresh_date(value)
        if fresh != value:
            print(f"   📅 {name}: {value} geçmişte kaldı → {fresh} (bugün)")
            kwargs[name] = fresh
    return kwargs


async def _verify(
    site_name: str, test_query: str, test_params: Optional[dict] = None
) -> list[dict]:
    """search(**kwargs) en fazla TEST_ATTEMPTS kez denenir; ilk başarılı sonucu döner."""
    last_error = "bilinmeyen hata"
    for attempt in range(1, TEST_ATTEMPTS + 1):
        try:
            client = _load_client(site_name)
            kwargs = _test_kwargs(client, test_query, test_params)
        except Exception as e:  # client yüklenemiyorsa tekrar denemek anlamsız
            raise RuntimeError(f"Test hazırlanamadı: {type(e).__name__}: {e}") from e
        args = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
        print(f"🧪 Test {attempt}/{TEST_ATTEMPTS}: {site_name}.search({args})")
        try:
            results = await client.search(**kwargs)
            if not isinstance(results, list):
                last_error = f"search() liste döndürmedi: {type(results).__name__}"
            elif not results:
                last_error = "search() 0 sonuç döndürdü (items_path/endpoint yanlış olabilir)"
            else:
                return results
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
        print(f"   ⚠️  {last_error}")
        if attempt < TEST_ATTEMPTS:
            await asyncio.sleep(TEST_RETRY_DELAY)
    raise RuntimeError(f"{TEST_ATTEMPTS} denemede de başarısız: {last_error}")


async def add_site(
    site_name: str,
    url: str,
    intent: str = "ürün arama",
    test_query: str = "test",
    test_params: Optional[dict] = None,
) -> bool:
    """Siteyi kaydeder, derler ve test eder. Başarılıysa True döner.

    test_params: çoklu parametreli sitelerde test değerleri (örn. {"date": "2026-12-01"});
    verilmeyenler için kayıt sırasında yakalanan değerler kullanılır.
    """
    # 1. Kayıt (kullanıcı arama yapıp ENTER'a basar)
    print(f"📡 1/4 Kayıt: {url}")
    try:
        traffic = await _record(url, intent)
    except Exception as e:
        print(f"❌ Kayıt başarısız: {type(e).__name__}: {e}")
        return False
    print(f"   📦 {len(traffic)} JSON yanıt yakalandı.")
    if not traffic:
        print("❌ Hiç JSON yakalanmadı; sitede arama yaptığınızdan emin olun.")
        return False

    # 2–3. AI ile derleme → registry/{site}/client.py + schema.json
    print("🧠 2/4 AI ile derleniyor...")
    try:
        await Compiler().compile(site_name, intent)
    except Exception as e:
        print(f"❌ Derleme başarısız: {type(e).__name__}: {e}")
        return False

    site_dir = REGISTRY_DIR / site_name
    missing = [f for f in ("client.py", "schema.json") if not (site_dir / f).is_file()]
    if missing:
        print(f"❌ Dosyalar üretilemedi: {', '.join(missing)}")
        return False
    print(f"💾 3/4 Kaydedildi: registry/{site_name}/client.py, schema.json")

    # 4. Canlı test
    print("🔬 4/4 Canlı test...")
    try:
        results = await _verify(site_name, test_query, test_params)
    except Exception as e:
        print(f"❌ {site_name} eklendi ama test başarısız: {e}")
        print(f"   Dosyalar duruyor: registry/{site_name}/ — tekrar kayıt için: own-api add {site_name} {url}")
        return False

    print(f"   {len(results)} sonuç, örnek:")
    print("   " + json.dumps(results[0], ensure_ascii=False, default=str)[:300])
    print(f"✅ {site_name} başarıyla eklendi ve test edildi.")
    try:
        names = _param_names(_load_client(site_name)) or ["query"]
    except Exception:
        names = ["query"]
    print(f"   Deneyin: own-api call {site_name} " + " ".join(f'-p {n}="..."' for n in names))
    return True
