import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.chrome_bridge import DEFAULT_PROFILE_DIR, ChromeBridge  # noqa: E402


@pytest.mark.asyncio
async def test_open_navigate_close_and_reuse_profile():
    # 1. açılış
    bridge = ChromeBridge()
    await bridge.start()
    try:
        page = await bridge.navigate("https://example.com")
        assert "Example Domain" in await page.title()
    finally:
        await bridge.close()

    assert not bridge.is_running
    assert DEFAULT_PROFILE_DIR.is_dir()
    assert any(DEFAULT_PROFILE_DIR.iterdir()), "Profil klasörü boş"

    # 2. açılış — aynı kalıcı profil
    bridge2 = ChromeBridge()
    assert bridge2.user_data_dir == DEFAULT_PROFILE_DIR
    await bridge2.start()
    try:
        assert DEFAULT_PROFILE_DIR.is_dir()
        assert (DEFAULT_PROFILE_DIR / "Default").is_dir()
        page = await bridge2.navigate("https://example.com")
        assert "Example Domain" in await page.title()
    finally:
        await bridge2.close()
