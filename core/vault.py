"""Vault — arama sonuçlarını ve fiyat geçmişini SQLite'ta (vault.db) saklar."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "vault.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site          TEXT NOT NULL,
    product_name  TEXT NOT NULL,
    price         REAL,
    search_query  TEXT NOT NULL,
    timestamp     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prices_site_query ON prices (site, search_query, timestamp);
CREATE INDEX IF NOT EXISTS idx_prices_timestamp ON prices (timestamp);
"""

NAME_KEYS = ("name", "product_name", "title", "productName")
PRICE_KEYS = ("price", "finalPrice", "current", "amount")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _normalize_query(query: str) -> str:
    return " ".join(query.strip().lower().split())


def parse_price(value: Any) -> Optional[float]:
    """855 / 125.45 / "1.299,90 TL" / "1,299.90" → float; anlaşılmazsa None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):  # örn. {"value": 855, "currency": "TRY"}
        for key in ("value", *PRICE_KEYS):
            if key in value:
                return parse_price(value[key])
        return None
    text = re.sub(r"[^\d.,]", "", str(value))
    if not text:
        return None
    if "," in text and "." in text:
        # Son görülen ayraç ondalıktır: 1.299,90 (TR) veya 1,299.90 (EN)
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif re.fullmatch(r"\d{1,3}([.,]\d{3})+", text):
        # Tek tür ayraç + 3'lü gruplar → binlik ayracı: "12.499", "1,299", "1.250.000"
        text = re.sub(r"[.,]", "", text)
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _pick(item: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if item.get(key) not in (None, ""):
            return item[key]
    return None


# --------------------------------------------------------------------------- #
# senkron çekirdek (asyncio.to_thread ile çağrılır)
# --------------------------------------------------------------------------- #
def _save(site: str, query: str, results: list[dict]) -> int:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    q = _normalize_query(query)
    rows = []
    for item in results:
        if not isinstance(item, dict):
            continue
        name = _pick(item, NAME_KEYS)
        if not name:
            continue
        rows.append((site, str(name), parse_price(_pick(item, PRICE_KEYS)), q, now))
    if not rows:
        return 0
    with _connect() as conn:
        conn.executemany(
            "INSERT INTO prices (site, product_name, price, search_query, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def _recent(limit: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT site, search_query AS query, timestamp,
                   COUNT(*) AS result_count,
                   MIN(price) AS min_price, MAX(price) AS max_price, AVG(price) AS avg_price
            FROM prices
            GROUP BY site, search_query, timestamp
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def _history(site: str, query: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT timestamp, COUNT(*) AS result_count,
                   MIN(price) AS min_price, MAX(price) AS max_price, AVG(price) AS avg_price
            FROM prices
            WHERE site = ? AND search_query = ?
            GROUP BY timestamp
            ORDER BY timestamp ASC
            """,
            (site, _normalize_query(query)),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# async API
# --------------------------------------------------------------------------- #
async def save_results(site: str, query: str, results: list[dict]) -> int:
    """Sonuçları kaydeder; kaydedilen satır sayısını döndürür."""
    return await asyncio.to_thread(_save, site, query, results)


async def get_recent_searches(limit: int = 20) -> list[dict]:
    """Son aramalar (her arama: site, query, timestamp, sonuç sayısı, min/max/ort. fiyat)."""
    return await asyncio.to_thread(_recent, limit)


async def get_price_history(site: str, query: str) -> list[dict]:
    """Bir aramanın zaman içindeki fiyat özeti (eskiden yeniye)."""
    return await asyncio.to_thread(_history, site, query)
