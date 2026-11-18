"""Gece çalışan Self-Healing kontrolü — tüm siteleri test eder, sonucu logs/heal_report.log'a yazar.

Çalıştırma (proje kökünden, venv içindeki python ile):
    python scripts/nightly.py

Zamanlama örnekleri (her gece 03:00):
    Linux/macOS (crontab -e):
        0 3 * * * cd /yol/own-api && venv/bin/python scripts/nightly.py >> logs/nightly.out 2>&1
    Windows (Görev Zamanlayıcı):
        schtasks /Create /SC DAILY /ST 03:00 /TN "own-api nightly" ^
            /TR "C:\\yol\\own-api\\venv\\Scripts\\python.exe C:\\yol\\own-api\\scripts\\nightly.py"

Notlar:
- Etkileşimsiz çalışır: bir site captcha/doğrulama isterse beklenmez, o site bozuk raporlanır.
- Testler gerçek Chrome profilini kullanır; o sırada aynı profille açık bir Chrome olmamalı.
- Çıkış kodu: 0 = hepsi sağlam, 1 = en az bir site bozuk, 2 = kontrol çalıştırılamadı.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

LOG_DIR = PROJECT_ROOT / "logs"
REPORT_FILE = LOG_DIR / "heal_report.log"

for _stream in (sys.stdout, sys.stderr):  # Windows konsolu emoji yazamazsa çökmesin
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")


def _write_report(lines: list[str]) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with REPORT_FILE.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")


def main() -> int:
    # Gece kimse ENTER'a basamaz: input() hemen EOFError versin (captcha beklemesinde asılı kalmasın).
    sys.stdin = open(os.devnull, encoding="utf-8")

    started = datetime.now()
    header = f"===== Self-Healing raporu — {started:%Y-%m-%d %H:%M:%S} ====="
    try:
        from core.self_healing import broken_sites, heal_all

        reports = asyncio.run(heal_all())
    except Exception:  # noqa: BLE001
        _write_report([header, "💥 Kontrol çalıştırılamadı:", traceback.format_exc().rstrip()])
        return 2

    broken = broken_sites(reports)
    healthy = [s for s, r in reports.items() if r.ok]
    skipped = [s for s, r in reports.items() if r.skipped]
    elapsed = (datetime.now() - started).total_seconds()

    lines = [header]
    if not reports:
        lines.append("Kayıtlı site yok.")
    for site, r in reports.items():
        if r.skipped:
            lines.append(f"⏭️  {site:<15} ATLANDI  {r.error}")
        elif r.ok:
            lines.append(f"✅ {site:<15} SAĞLAM   {r.result_count} sonuç, {r.duration_s} sn")
        else:
            lines.append(f"❌ {site:<15} BOZUK    {r.error} ({r.duration_s} sn)")
        if r.params:
            lines.append(f"     parametreler: {json.dumps(r.params, ensure_ascii=False, default=str)}")
    lines.append(
        f"Özet: {len(healthy)} sağlam, {len(broken)} bozuk, {len(skipped)} atlandı — {elapsed:.0f} sn"
    )
    for site in broken:
        lines.append(f"⚠️ {site} bozulmuş! Onarım için: own-api add {site} <url> --intent \"...\"")
    _write_report(lines)
    print("\n".join(lines))
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
