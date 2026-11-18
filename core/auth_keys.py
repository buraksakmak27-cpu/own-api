"""API anahtarları — `sk-own-...` biçiminde geliştirici anahtarları.

Vault ile aynı SQLite dosyasında (vault.db) `api_keys` tablosunda saklanır.
Tam anahtar yalnızca üretildiği anda döner; listelemede maskelenir.

Not: Bu yerel geliştirici aracının API'si 127.0.0.1'e bağlıdır. Anahtarlar düz metin
saklanır; paylaşmayın, vault.db zaten .gitignore'dadır.
"""

from __future__ import annotations

import asyncio
import secrets
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from core import vault  # DB_PATH'i runtime'da okuyarak testlerin patch'ini destekler

KEY_PREFIX = "sk-own-"

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key_string  TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(vault.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def generate_key() -> str:
    """Tahmin edilemez yeni anahtar: sk-own-<43 karakter>."""
    return KEY_PREFIX + secrets.token_urlsafe(32)


def mask_key(key: str) -> str:
    """Listeleme için: sk-own-…XXXX (son 4 karakter)."""
    tail = key[-4:] if len(key) >= 4 else key
    return f"{KEY_PREFIX}…{tail}"


# --------------------------------------------------------------------------- #
# senkron çekirdek (asyncio.to_thread ile çağrılır)
# --------------------------------------------------------------------------- #
def _create(name: str) -> dict:
    key = generate_key()
    now = _now()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO api_keys (key_string, name, created_at) VALUES (?, ?, ?)",
            (key, name, now),
        )
        key_id = cur.lastrowid
    # Tam anahtar SADECE burada döner.
    return {"id": key_id, "key": key, "name": name, "created_at": now}


def _list() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, key_string, name, created_at FROM api_keys ORDER BY id DESC"
        ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "created_at": r["created_at"],
            "key_masked": mask_key(r["key_string"]),
        }
        for r in rows
    ]


def _verify(key: str) -> bool:
    if not key or not key.startswith(KEY_PREFIX):
        return False
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM api_keys WHERE key_string = ?", (key,)
        ).fetchone()
    return row is not None


def _ensure_named(name: str) -> dict:
    """Verilen ada sahip bir anahtar varsa onu (maskeli) döndürür; yoksa üretir (tam anahtarla)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, key_string, name, created_at FROM api_keys WHERE name = ? ORDER BY id LIMIT 1",
            (name,),
        ).fetchone()
    if row is not None:
        return {
            "id": row["id"], "name": row["name"], "created_at": row["created_at"],
            "key_masked": mask_key(row["key_string"]), "existed": True,
        }
    created = _create(name)
    created["existed"] = False
    return created


# --------------------------------------------------------------------------- #
# async API
# --------------------------------------------------------------------------- #
async def create_key(name: str = "api") -> dict:
    """Yeni anahtar üretir. Dönen dict'te tam 'key' alanı YALNIZCA burada bulunur."""
    return await asyncio.to_thread(_create, (name or "api").strip() or "api")


async def list_keys() -> list[dict]:
    """Anahtarları maskeli olarak listeler (tam anahtar dönmez)."""
    return await asyncio.to_thread(_list)


async def verify_key(key: str) -> bool:
    return await asyncio.to_thread(_verify, key)


async def ensure_key(name: str) -> dict:
    """Panelin kendi varsayılan anahtarı için: ada göre getir ya da üret."""
    return await asyncio.to_thread(_ensure_named, (name or "api").strip() or "api")
