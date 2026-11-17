"""Passport — own-api'nin kimliği (Ed25519) ve izinleri.

passport.json (proje kökü, .gitignore'da) ilk kullanımda otomatik üretilir:
    {
      "key": {"algorithm": "Ed25519", "key_id": ..., "private_key": ..., "public_key": ...},
      "permissions": {"default": ["read"], "sites": {"trendyol": ["read", "write"]}}
    }

Varsayılan: her site için sadece "read" (arama). "write" ancak açıkça verilirse.
DİKKAT: passport.json özel anahtar içerir; paylaşmayın, commit etmeyin.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PASSPORT_PATH = PROJECT_ROOT / "passport.json"

READ, WRITE = "read", "write"
# Okuma sayılan aksiyonlar; listede olmayan her aksiyon "write" kabul edilir.
READ_ACTIONS = {READ, "search", "get", "list", "fetch", "query"}
DEFAULT_PERMISSIONS = [READ]

_lock = threading.Lock()
_cache: Optional[dict] = None


class PermissionDenied(PermissionError):
    pass


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(obj: Any) -> bytes:
    """İmzalanacak deterministik JSON (anahtar sırası/boşluk sabit)."""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def _write(data: dict) -> None:
    tmp = PASSPORT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, PASSPORT_PATH)


def _generate() -> dict:
    private = Ed25519PrivateKey.generate()
    pub_raw = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    priv_raw = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    return {
        "version": 1,
        "created_at": _now(),
        "key": {
            "algorithm": "Ed25519",
            "key_id": hashlib.sha256(pub_raw).hexdigest()[:16],
            "public_key": _b64(pub_raw),
            "private_key": _b64(priv_raw),
        },
        "permissions": {"default": list(DEFAULT_PERMISSIONS), "sites": {}},
    }


def load_passport() -> dict:
    """passport.json'u yükler; yoksa anahtar çifti üretip kaydeder."""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        if PASSPORT_PATH.is_file():
            try:
                data = json.loads(PASSPORT_PATH.read_text(encoding="utf-8"))
                data["key"]["private_key"]  # noqa: B018 — yapı kontrolü
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as e:
                # Bozuk dosyanın üzerine yazma: anahtar kaybolur, eski imzalar doğrulanamaz.
                raise RuntimeError(f"passport.json okunamadı/bozuk: {e}. Dosyayı kontrol edin.") from e
        else:
            data = _generate()
            _write(data)
        data.setdefault("permissions", {"default": list(DEFAULT_PERMISSIONS), "sites": {}})
        _cache = data
        return data


def _private_key() -> Ed25519PrivateKey:
    raw = base64.b64decode(load_passport()["key"]["private_key"])
    return Ed25519PrivateKey.from_private_bytes(raw)


def public_key() -> str:
    return load_passport()["key"]["public_key"]


# --------------------------------------------------------------------------- #
# imza
# --------------------------------------------------------------------------- #
def sign_call(site: str, action: str, params: Optional[dict] = None) -> dict:
    """Çağrıyı imzalar. Dönen dict audit log'a yazılabilir ve verify_call ile doğrulanır."""
    payload = {
        "site": site,
        "action": action,
        "params": params or {},
        "ts": _now(),
        "nonce": secrets.token_hex(8),
    }
    signature = _private_key().sign(_canonical(payload))
    return {
        "payload": payload,
        "signature": _b64(signature),
        "key_id": load_passport()["key"]["key_id"],
    }


def verify_call(signed: dict, public_key_b64: Optional[str] = None) -> bool:
    """sign_call çıktısının imzasını doğrular."""
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64 or public_key()))
        pub.verify(base64.b64decode(signed["signature"]), _canonical(signed["payload"]))
        return True
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# izinler
# --------------------------------------------------------------------------- #
def action_kind(action: str) -> str:
    """'search' → 'read', 'read' → 'read', diğer her şey → 'write'."""
    return READ if action.lower() in READ_ACTIONS else WRITE


def _site_permissions(site: str) -> list[str]:
    perms = load_passport()["permissions"]
    return perms.get("sites", {}).get(site, perms.get("default", DEFAULT_PERMISSIONS))


def check_permission(site: str, action: str) -> bool:
    """site için action'a izin var mı? (action: 'read'/'write' veya 'search' gibi bir aksiyon adı)"""
    return action_kind(action) in _site_permissions(site)


def require_permission(site: str, action: str) -> None:
    if not check_permission(site, action):
        raise PermissionDenied(
            f"'{site}' için '{action_kind(action)}' izni yok "
            f"(izinler: {_site_permissions(site)}). passport.json'dan verilebilir."
        )


def grant_permission(site: str, kind: str) -> None:
    """Örn. grant_permission('trendyol', 'write')"""
    if kind not in (READ, WRITE):
        raise ValueError("kind 'read' veya 'write' olmalı")
    data = load_passport()
    with _lock:
        sites = data["permissions"].setdefault("sites", {})
        perms = sites.setdefault(site, list(data["permissions"].get("default", DEFAULT_PERMISSIONS)))
        if kind not in perms:
            perms.append(kind)
        _write(data)


def revoke_permission(site: str, kind: str) -> None:
    data = load_passport()
    with _lock:
        sites = data["permissions"].setdefault("sites", {})
        perms = sites.setdefault(site, list(data["permissions"].get("default", DEFAULT_PERMISSIONS)))
        if kind in perms:
            perms.remove(kind)
        _write(data)
