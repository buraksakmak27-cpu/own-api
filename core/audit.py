"""Audit — tüm dış çağrıları logs/audit.log'a JSONL olarak yazar.

Her satır Passport ile imzalanır; kayıt sonradan değiştirilirse verify_entry() False döner.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

from core import passport

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = PROJECT_ROOT / "logs" / "audit.log"

_lock = threading.Lock()


def log_event(
    actor: str,
    site: str,
    action: str,
    params: Optional[dict] = None,
    status: str = "ok",
    error: Optional[str] = None,
    **extra: Any,
) -> dict:
    """Bir olayı kaydeder. actor: 'panel' | 'cli' | 'mcp' | 'agent' ...
    status: 'ok' | 'denied' | 'error'. Log yazılamazsa çağrıyı bozmaz (stderr'e uyarır).
    """
    params = params or {}
    entry: dict[str, Any] = {
        "actor": actor,
        "site": site,
        "action": action,
        "kind": passport.action_kind(action),
        "params": params,
        "status": status,
    }
    if error:
        entry["error"] = error[:500]
    entry.update(extra)

    try:
        # İmza; aktör/durum dahil tüm kaydı kapsar.
        signed = passport.sign_call(site, action, {k: v for k, v in entry.items() if k not in ("site", "action")})
        entry = {
            "ts": signed["payload"]["ts"],
            **entry,
            "nonce": signed["payload"]["nonce"],
            "key_id": signed["key_id"],
            "signature": signed["signature"],
        }
    except Exception as e:  # imza yoksa da log tutulsun
        entry["signature_error"] = f"{type(e).__name__}: {e}"

    try:
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with _lock:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except OSError as e:
        print(f"⚠️  audit log yazılamadı: {e}", file=sys.stderr)
    return entry


def read_events(limit: Optional[int] = None) -> Iterator[dict]:
    """Log kayıtlarını okur (limit verilirse son N kayıt)."""
    if not LOG_PATH.is_file():
        return iter(())
    lines = LOG_PATH.read_text(encoding="utf-8").splitlines()
    if limit is not None:
        lines = lines[-limit:]
    return (json.loads(line) for line in lines if line.strip())


def verify_entry(entry: dict) -> bool:
    """Kaydın imzasını doğrular (sonradan değiştirilmiş mi?)."""
    if "signature" not in entry:
        return False
    signed_fields = {
        k: v for k, v in entry.items()
        if k not in ("ts", "site", "action", "nonce", "key_id", "signature")
    }
    return passport.verify_call(
        {
            "payload": {
                "site": entry["site"],
                "action": entry["action"],
                "params": signed_fields,
                "ts": entry["ts"],
                "nonce": entry["nonce"],
            },
            "signature": entry["signature"],
        }
    )
