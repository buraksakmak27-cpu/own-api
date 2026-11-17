"""own-api HTTP API + Panel.

Çalıştırma (proje kökünde):
    venv\\Scripts\\python -m core.api
Panel: http://127.0.0.1:8000

Not: Windows'ta --reload KULLANMAYIN; uvicorn o modda SelectorEventLoop'a geçer
ve Playwright Chrome'u başlatamaz.
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import re
import sys
from pathlib import Path
from types import ModuleType

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = PROJECT_ROOT / "registry"
PANEL_FILE = PROJECT_ROOT / "panel" / "index.html"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import audit, passport, vault  # noqa: E402

SITE_RE = re.compile(r"^[a-z0-9_-]+$", re.IGNORECASE)

# Aynı kalıcı Chrome profili aynı anda iki kez açılamaz → aramalar sıralı.
_search_lock = asyncio.Lock()

app = FastAPI(title="own-api", version="0.1.0")


def _site_dir(site: str) -> Path:
    if not SITE_RE.match(site):
        raise HTTPException(400, "Geçersiz site adı.")
    path = REGISTRY_DIR / site
    if not (path / "client.py").is_file():
        raise HTTPException(404, f"'{site}' kayıtlı değil.")
    return path


def _load_client(site: str) -> ModuleType:
    path = _site_dir(site) / "client.py"
    spec = importlib.util.spec_from_file_location(f"registry.{site}.client", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@app.get("/", include_in_schema=False)
async def panel() -> FileResponse:
    if not PANEL_FILE.is_file():
        raise HTTPException(404, "panel/index.html bulunamadı.")
    return FileResponse(PANEL_FILE)


@app.get("/api/sites")
async def sites() -> list[dict]:
    out = []
    if REGISTRY_DIR.is_dir():
        for d in sorted(REGISTRY_DIR.iterdir()):
            if not (d / "client.py").is_file():
                continue
            desc, params = "", ["query"]
            schema = d / "schema.json"
            if schema.is_file():
                try:
                    data = json.loads(schema.read_text(encoding="utf-8"))
                    desc = data.get("description", "")
                    params = list((data.get("inputSchema") or {}).get("properties") or params)
                except (OSError, json.JSONDecodeError, AttributeError):
                    pass
            out.append({"site": d.name, "description": desc, "parameters": params})
    return out


@app.get("/api/vault/recent")
async def recent(limit: int = Query(20, ge=1, le=200)) -> list[dict]:
    return await vault.get_recent_searches(limit)


@app.get("/api/vault/history")
async def history(site: str, q: str = Query(..., min_length=1)) -> list[dict]:
    return await vault.get_price_history(site, q)


def _search_kwargs(func, request: Request) -> dict:
    """URL parametrelerini search(**kwargs)'a çevirir.

    Tek parametreli siteler: /api/trendyol/search?q=saat  (q → query)
    Çoklu parametreli siteler: /api/thy/search?origin=IST&destination=AYT&date=2026-12-01
    """
    kwargs = {k: v for k, v in request.query_params.items()}
    names = list(inspect.signature(func).parameters)
    if "q" in kwargs and "q" not in names and "query" not in kwargs:
        kwargs["query"] = kwargs.pop("q")
    for k, v in kwargs.items():
        if len(v) > 200:
            raise HTTPException(422, f"'{k}' çok uzun (en fazla 200 karakter).")
    try:
        inspect.signature(func).bind(**kwargs)
    except TypeError as e:
        raise HTTPException(
            422, f"Geçersiz parametreler: {e}. Beklenen: {', '.join(names) or 'query'}"
        ) from e
    if not any(str(v).strip() for v in kwargs.values()):
        raise HTTPException(422, "Arama parametresi boş.")
    return kwargs


def _query_label(kwargs: dict) -> str:
    """Vault/geçmiş için aramanın metin hali: 'saat' veya 'origin=IST destination=AYT'."""
    if list(kwargs) == ["query"]:
        return str(kwargs["query"])
    return " ".join(f"{k}={v}" for k, v in sorted(kwargs.items()))


@app.get("/api/{site}/search")
async def search(site: str, request: Request) -> dict:
    client = _load_client(site)
    func = getattr(client, "search", None)
    if func is None:
        raise HTTPException(500, f"registry/{site}/client.py içinde search() yok.")
    kwargs = _search_kwargs(func, request)

    if not passport.check_permission(site, "read"):
        audit.log_event("panel", site, "search", kwargs, "denied")
        raise HTTPException(403, f"'{site}' için okuma (read) izni yok (passport.json).")
    async with _search_lock:
        try:
            results = func(**kwargs)
            if inspect.isawaitable(results):
                results = await results
        except Exception as e:
            raise HTTPException(502, f"{site} araması başarısız: {type(e).__name__}: {e}") from e

    if not isinstance(results, list):
        results = []
    label = _query_label(kwargs)
    audit.log_event("panel", site, "search", kwargs, "ok", result_count=len(results))
    saved = await vault.save_results(site, label, results)
    return {
        "site": site, "query": label, "params": kwargs,
        "count": len(results), "saved": saved, "results": results,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
