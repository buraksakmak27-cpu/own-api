import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.compiler import Compiler  # noqa: E402

if __name__ == "__main__":
    compiler = Compiler()
    asyncio.run(compiler.compile("trendyol", "ürün arama"))
