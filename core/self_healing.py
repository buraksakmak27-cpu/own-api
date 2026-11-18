"""Self-Healing (FAZ C1) — kayıtlı sitelerin tariflerinin hâlâ çalışıp çalışmadığını kontrol eder.

Her site için registry/{site}/client.py ile gerçek bir test araması yapılır:
  - hata fırlatırsa veya 0 sonuç dönerse → bozuk (False)
  - en az 1 sonuç dönerse → sağlam (True)

    from core.self_healing import check_site, heal_all
    await check_site("trendyol")   # True / False
    await heal_all()               # {"trendyol": SiteReport(...), ...}

Not: Yeniden kayıt (recorder) kullanıcının sitede elle arama yapmasını gerektirdiği için
onarım şimdilik "tespit + bildirim" düzeyindedir; düzeltme `own-api add {site} ...` ile yapılır.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from core import audit, passport
from core.factory import _load_client, _test_kwargs

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = PROJECT_ROOT / "registry"

ACTOR = "self_healing"
CHECK_TIMEOUT_SECONDS = 180  # tek bir sitenin testi en fazla bu kadar sürebilir

# Site bazlı varsayılan test parametreleri. Burada olmayan parametreler kayıt sırasında
# yakalanan gövdeden alınır (geçmiş tarihler bugüne çekilir — factory._test_kwargs).
DEFAULT_TEST_PARAMS: dict[str, dict] = {
    "trendyol": {"query": "test"},
    "thy": {"origin": "IST", "destination": "ESB"},
    "sahibinden": {"query": "iphone"},
}
DEFAULT_TEST_QUERY = "test"  # listede olmayan, tek "query" parametreli siteler için


@dataclass
class SiteReport:
    site: str
    ok: bool
    result_count: int = 0
    error: Optional[str] = None
    params: dict = field(default_factory=dict)
    duration_s: float = 0.0
    skipped: bool = False  # passport izni yok → test edilmedi (bozuk sayılmaz)

    def to_dict(self) -> dict:
        return asdict(self)


def list_sites() -> list[str]:
    """registry/ altında client.py'si olan tüm siteler."""
    if not REGISTRY_DIR.is_dir():
        return []
    return sorted(d.name for d in REGISTRY_DIR.iterdir() if d.is_dir() and (d / "client.py").is_file())


async def check_site_report(site_name: str, test_params: Optional[dict] = None) -> SiteReport:
    """Siteyi test eder ve ayrıntılı rapor döner (heal_all ve CLI bunu kullanır)."""
    started = time.monotonic()
    report = SiteReport(site=site_name, ok=False)

    if not passport.check_permission(site_name, "read"):
        report.skipped = True
        report.error = "passport.json'da okuma (read) izni yok — test edilmedi"
        audit.log_event(ACTOR, site_name, "search", {}, "denied")
        return report

    try:
        client = _load_client(site_name)
        params = test_params if test_params is not None else DEFAULT_TEST_PARAMS.get(site_name, {})
        kwargs = _test_kwargs(client, params.get("query", DEFAULT_TEST_QUERY), params)
        report.params = kwargs
        results = await asyncio.wait_for(client.search(**kwargs), timeout=CHECK_TIMEOUT_SECONDS)
        if not isinstance(results, list):
            report.error = f"search() liste döndürmedi: {type(results).__name__}"
        elif not results:
            report.error = "search() 0 sonuç döndürdü (endpoint/selector/items_path bozulmuş olabilir)"
        else:
            report.ok = True
            report.result_count = len(results)
    except asyncio.TimeoutError:
        report.error = f"{CHECK_TIMEOUT_SECONDS} sn içinde cevap gelmedi (zaman aşımı)"
    except EOFError:
        # Gece (etkileşimsiz) çalışırken captcha/doğrulama istendi; input() okunamadı.
        report.error = "Doğrulama (captcha/OTP) istendi ama etkileşimsiz çalışılıyor"
    except Exception as e:  # noqa: BLE001 — her türlü hata "bozuk" demek
        report.error = f"{type(e).__name__}: {e}"
    finally:
        report.duration_s = round(time.monotonic() - started, 1)

    audit.log_event(
        ACTOR, site_name, "search", report.params, "ok" if report.ok else "error",
        result_count=report.result_count,
    )
    return report


async def check_site(site_name: str, test_params: Optional[dict] = None) -> bool:
    """Test araması başarılıysa True; hata fırlatırsa veya 0 sonuç dönerse False."""
    return (await check_site_report(site_name, test_params)).ok


async def heal_all(sites: Optional[list[str]] = None) -> dict[str, SiteReport]:
    """registry/ altındaki tüm siteleri SIRAYLA test eder (hepsi aynı Chrome profilini kullanır).

    Dönen sözlük: {site: SiteReport}. Bozuk olanlar: [s for s, r in sonuç.items() if not r.ok and not r.skipped]
    """
    reports: dict[str, SiteReport] = {}
    for site in sites if sites is not None else list_sites():
        print(f"🩺 {site} kontrol ediliyor...")
        report = await check_site_report(site)
        reports[site] = report
        if report.skipped:
            print(f"   ⏭️  {site}: atlandı ({report.error})")
        elif report.ok:
            print(f"   ✅ {site}: {report.result_count} sonuç ({report.duration_s} sn)")
        else:
            print(f"   ❌ {site}: {report.error}")
    return reports


def broken_sites(reports: dict[str, SiteReport]) -> list[str]:
    return [s for s, r in reports.items() if not r.ok and not r.skipped]
