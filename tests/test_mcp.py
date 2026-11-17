import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from own_api.mcp_server import discover_tools  # noqa: E402

if __name__ == "__main__":
    tools = discover_tools()
    if not tools:
        print("registry/ içinde kayıtlı site yok.")
    for name, t in tools.items():
        print(f"{t['site']:<15} -> {name:<25} {t['description']}")
    print(f"Toplam: {len(tools)} tool")
