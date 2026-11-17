"""own-api MCP sunucusu — registry/ içindeki siteleri Claude'a tool olarak açar.

Çalıştırma:  python -m own_api.mcp_server   (stdio)

Not: kurulu mcp 2.x'te @server.list_tools()/@server.call_tool() dekoratörleri yok;
handler'lar Server(on_list_tools=..., on_call_tool=...) ile verilir.
"""

from __future__ import annotations

import asyncio
import builtins
import importlib.util
import inspect
import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

import mcp.types as types
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = PROJECT_ROOT / "registry"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core import audit, passport  # noqa: E402

# Windows'ta stderr/stdout cp1254 olabilir; emoji/özel karakter log'u tool'u çökertmesin.
# (mcp 2.x stdout fd'sini stderr'e yönlendirir; protokol kendi kopyasından yazar.)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# Captcha/403'te Healer input() ile ENTER bekler; MCP'de stdin protokole ait.
# Bunun yerine kullanıcıya Chrome'da çözmesi için bu kadar süre tanınır.
VERIFICATION_WAIT_SECONDS = int(os.environ.get("OWN_API_VERIFY_WAIT", "60"))

# Aynı kalıcı Chrome profili aynı anda iki kez açılamaz → çağrılar sıralı.
_call_lock = asyncio.Lock()


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
def discover_tools() -> dict[str, dict[str, Any]]:
    """{tool_adı: {"site", "action", "description", "input_schema"}}"""
    tools: dict[str, dict[str, Any]] = {}
    if not REGISTRY_DIR.is_dir():
        return tools
    for site_dir in sorted(REGISTRY_DIR.iterdir()):
        schema_path = site_dir / "schema.json"
        if not (site_dir / "client.py").is_file() or not schema_path.is_file():
            continue
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _log(f"⚠️  {schema_path} okunamadı: {e}")
            continue
        site = site_dir.name
        name = schema.get("name") or f"{site}_search"
        prefix = f"{site}_"
        action = name[len(prefix):] if name.startswith(prefix) else "search"
        tools[name] = {
            "site": site,
            "action": action or "search",
            "description": schema.get("description") or f"{site} üzerinde arama",
            "input_schema": schema.get("inputSchema")
            or {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        }
    return tools


def _load_client(site: str) -> ModuleType:
    path = REGISTRY_DIR / site / "client.py"
    spec = importlib.util.spec_from_file_location(f"registry.{site}.client", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


@contextmanager
def _timed_input():
    """Healer'ın input() çağrısını, stdin yerine süreli beklemeye çevirir."""

    def fake_input(prompt: str = "") -> str:
        if prompt:
            _log(prompt)
        _log(f"⏳ Chrome'da doğrulamayı çözmek için {VERIFICATION_WAIT_SECONDS} sn bekleniyor...")
        time.sleep(VERIFICATION_WAIT_SECONDS)
        return ""

    original = builtins.input
    builtins.input = fake_input
    try:
        yield
    finally:
        builtins.input = original


def _text(text: str, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=text)], is_error=is_error
    )


# --------------------------------------------------------------------------- #
# handler'lar
# --------------------------------------------------------------------------- #
async def list_tools(
    ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(name=name, description=t["description"], input_schema=t["input_schema"])
            for name, t in discover_tools().items()
        ]
    )


async def call_tool(
    ctx: ServerRequestContext, params: types.CallToolRequestParams
) -> types.CallToolResult:
    tools = discover_tools()
    tool = tools.get(params.name)
    if tool is None:
        return _text(f"Bilinmeyen tool: {params.name}. Mevcut: {', '.join(tools)}", is_error=True)

    site, action = tool["site"], tool["action"]
    args = dict(params.arguments or {})
    try:
        client = _load_client(site)
        func = getattr(client, action, None)
        if func is None:
            return _text(f"registry/{site}/client.py içinde {action}() yok.", is_error=True)
        inspect.signature(func).bind(**args)

        if not passport.check_permission(site, action):
            audit.log_event("mcp", site, action, args, "denied")
            return _text(f"'{site}' için '{passport.action_kind(action)}' izni yok (passport.json).", is_error=True)
        _log(f"🔎 {site}.{action}({args})")
        async with _call_lock:
            with _timed_input():
                result = func(**args)
                if inspect.isawaitable(result):
                    result = await result
    except Exception as e:  # hata Claude'a mesaj olarak döner, sunucu çökmez
        _log(f"❌ {site}.{action}: {type(e).__name__}: {e}")
        return _text(f"{type(e).__name__}: {e}", is_error=True)

    audit.log_event("mcp", site, action, args, "ok",
                    result_count=len(result) if isinstance(result, list) else None)
    return _text(json.dumps(result, ensure_ascii=False, default=str))


server = Server(
    "own-api",
    version="0.1.0",
    instructions="Kullanıcının kendi Chrome oturumu üzerinden site araması yapan araçlar.",
    on_list_tools=list_tools,
    on_call_tool=call_tool,
)


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
