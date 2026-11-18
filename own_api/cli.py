"""own-api komut satırı.

    own-api list
    own-api call trendyol -p query="saat" --limit 5
    own-api add trendyol https://www.trendyol.com --intent "ürün arama"
    own-api ask "trendyol'da 1000 TL altı duvar saati bul"
    own-api heal                 (tüm siteler)   |   own-api heal trendyol thy
"""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path
from types import ModuleType

import click

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = PROJECT_ROOT / "registry"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Windows konsolu (cp1254) bazı karakterleri (örn. "₂") yazamazsa çökmesin.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

MAX_ITEMS_TO_AI = 20

from core import audit, passport  # noqa: E402


# --------------------------------------------------------------------------- #
# registry yardımcıları
# --------------------------------------------------------------------------- #
def _sites() -> list[str]:
    if not REGISTRY_DIR.is_dir():
        return []
    return sorted(
        d.name for d in REGISTRY_DIR.iterdir() if d.is_dir() and (d / "client.py").is_file()
    )


def _load_schema(site: str) -> dict:
    path = REGISTRY_DIR / site / "schema.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_client(site: str) -> ModuleType:
    path = REGISTRY_DIR / site / "client.py"
    if not path.is_file():
        raise click.ClickException(
            f"'{site}' bulunamadı. Kayıtlı siteler: {', '.join(_sites()) or '(yok)'}"
        )
    spec = importlib.util.spec_from_file_location(f"registry.{site}.client", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


async def _run_search(site: str, *, _actor: str = "cli", **params) -> list[dict]:
    client = _load_client(site)
    if not hasattr(client, "search"):
        raise click.ClickException(f"registry/{site}/client.py içinde search() yok.")
    try:
        inspect.signature(client.search).bind(**params)
    except TypeError as e:
        raise click.ClickException(
            f"{site}.search{inspect.signature(client.search)} için geçersiz parametre: {e}"
        ) from e
    if not passport.check_permission(site, "read"):
        audit.log_event(_actor, site, "search", params, "denied")
        raise click.ClickException(f"'{site}' için okuma (read) izni yok (passport.json).")
    results = await client.search(**params)
    audit.log_event(_actor, site, "search", params, "ok", result_count=len(results))
    return results


def _parse_params(pairs: tuple[str, ...]) -> dict[str, str]:
    """('query=saat', 'sort=asc') -> {'query': 'saat', 'sort': 'asc'}"""
    params: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key = key.strip()
        if not sep or not key:
            raise click.BadParameter(
                f"'{pair}' geçersiz; anahtar=değer formatında olmalı (örn: -p query=saat).",
                param_hint="-p/--param",
            )
        params[key] = value
    return params


def _echo_json(data) -> None:
    click.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


# --------------------------------------------------------------------------- #
# komutlar
# --------------------------------------------------------------------------- #
@click.group()
def cli() -> None:
    """own-api — kendi Chrome'unuzla API'si olmayan sitelere API."""


@cli.command("list")
def list_sites() -> None:
    """Kayıtlı siteleri listeler."""
    sites = _sites()
    if not sites:
        click.echo("Kayıtlı site yok. Eklemek için: own-api add <site> <url>")
        return
    for site in sites:
        schema = _load_schema(site)
        name = schema.get("name", f"{site}_search")
        desc = schema.get("description", "")
        click.echo(f"• {site:<15} {name:<25} {desc}")


@cli.command()
@click.argument("site")
@click.option(
    "--param", "-p", "params", multiple=True, metavar="ANAHTAR=DEĞER",
    help="Client fonksiyonuna gidecek parametre; birden fazla verilebilir.",
)
@click.option("--limit", "-n", default=10, show_default=True, help="Gösterilecek sonuç sayısı.")
def call(site: str, params: tuple[str, ...], limit: int) -> None:
    """SITE istemcisini çağırır.  Örn: own-api call trendyol -p query="saat" """
    kwargs = _parse_params(params)
    results = asyncio.run(_run_search(site, **kwargs))
    click.echo(f"{len(results)} sonuç (ilk {min(limit, len(results))} gösteriliyor)", err=True)
    _echo_json(results[:limit])


@cli.command()
@click.argument("site_name")
@click.argument("url")
@click.option("--intent", "-i", default="ürün arama", show_default=True,
              help="Sitenin ne için kullanılacağı.")
@click.option("--test-query", default="test", show_default=True,
              help="Eklendikten sonra canlı testte aranacak kelime.")
def add(site_name: str, url: str, intent: str, test_query: str) -> None:
    """Siteyi kaydeder, AI ile derler ve canlı test eder.

    Örn: own-api add trendyol https://www.trendyol.com --intent "ürün arama"
    """
    from core.factory import add_site

    ok = asyncio.run(add_site(site_name, url, intent=intent, test_query=test_query))
    if not ok:
        sys.exit(1)


@cli.command()
@click.argument("question")
def ask(question: str) -> None:
    """Doğal dilde sorar; AI uygun siteyi seçip arar ve cevaplar."""
    from openai import AsyncOpenAI

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise click.ClickException("OPENAI_API_KEY ortam değişkeni tanımlı değil.")

    tools, tool_to_site = [], {}
    for site in _sites():
        schema = _load_schema(site)
        name = schema.get("name", f"{site}_search")
        tool_to_site[name] = site
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": schema.get("description", f"{site} üzerinde arama"),
                    "parameters": schema.get(
                        "inputSchema",
                        {"type": "object", "properties": {"query": {"type": "string"}},
                         "required": ["query"]},
                    ),
                },
            }
        )
    if not tools:
        raise click.ClickException("Kayıtlı site yok. Önce: own-api add <site> <url>")

    model = os.environ.get("OPENAI_MODEL", "gpt-4o")

    async def _ask() -> str:
        client = AsyncOpenAI(api_key=api_key)
        messages: list[dict] = [
            {
                "role": "system",
                "content": "Kullanıcının sorusunu araçları kullanarak cevapla. "
                "Araç sonuçlarına dayan, uydurma. Türkçe ve kısa cevap ver.",
            },
            {"role": "user", "content": question},
        ]
        resp = await client.chat.completions.create(model=model, messages=messages, tools=tools)
        msg = resp.choices[0].message
        if not msg.tool_calls:
            return msg.content or ""

        messages.append(msg.model_dump(exclude_none=True))
        for call_ in msg.tool_calls:
            site = tool_to_site.get(call_.function.name)
            try:
                args = json.loads(call_.function.arguments or "{}")
                click.echo(f"🔎 {site}: {args}", err=True)
                results = await _run_search(site, _actor="agent", **args)
                content = json.dumps(results[:MAX_ITEMS_TO_AI], ensure_ascii=False, default=str)
            except Exception as e:  # araç hatasını modele bildir
                content = json.dumps({"error": str(e)}, ensure_ascii=False)
            messages.append({"role": "tool", "tool_call_id": call_.id, "content": content})

        final = await client.chat.completions.create(model=model, messages=messages)
        return final.choices[0].message.content or ""

    click.echo(asyncio.run(_ask()))


@cli.command()
@click.argument("sites", nargs=-1)
def heal(sites: tuple[str, ...]) -> None:
    """Kayıtlı siteleri test eder, bozulanları bildirir (Self-Healing).

    Örn: own-api heal   |   own-api heal trendyol
    """
    from core.self_healing import broken_sites, heal_all

    for site in sites:
        if site not in _sites():
            raise click.ClickException(
                f"'{site}' bulunamadı. Kayıtlı siteler: {', '.join(_sites()) or '(yok)'}"
            )
    reports = asyncio.run(heal_all(list(sites) or None))
    if not reports:
        click.echo("Kayıtlı site yok. Eklemek için: own-api add <site> <url>")
        return

    broken = broken_sites(reports)
    healthy = [s for s, r in reports.items() if r.ok]
    click.echo(f"\n🩺 {len(healthy)}/{len(reports)} site sağlam.")
    for site in broken:
        click.echo(
            f"⚠️ {site} bozulmuş! Otomatik onarım için 'own-api add {site} ...' komutunu "
            "çalıştırın veya gece cron job'unu bekleyin."
        )
    if broken:
        sys.exit(1)


if __name__ == "__main__":
    cli()
