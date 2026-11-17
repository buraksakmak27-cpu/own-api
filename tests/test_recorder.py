import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.recorder import Recorder  # noqa: E402


@pytest.mark.asyncio
async def test_recorder_manual():
    pytest.skip("Manuel test: recorder için input() gerekiyor")


async def _main() -> list[dict]:
    recorder = Recorder()
    try:
        return await recorder.capture("https://www.trendyol.com", "ürün arama")
    finally:
        await recorder.close()


if __name__ == "__main__":
    traffic = asyncio.run(_main())
    print(f"Yakalanan: {len(traffic)} JSON")
    for t in traffic[:5]:
        print(t["url"], t["method"])
