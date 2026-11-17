import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Üretilen client.py'de sınıf yok; modül seviyesinde async search(query) var.
from registry.trendyol import client  # noqa: E402


async def main() -> None:
    results = await client.search(query="saat")
    results = results[:3]  # client'ta max_results parametresi yok
    print(f"Toplam (ilk 3 gösteriliyor): {len(results)}")
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
