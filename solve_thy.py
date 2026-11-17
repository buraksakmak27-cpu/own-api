import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        # Senin kalıcı profilini kullan
        browser = await p.chromium.launch_persistent_context(
            user_data_dir="C:\\Users\\Ali\\.own-api\\.browser_profile",
            channel="chrome",
            headless=False
        )
        page = await browser.new_page()
        
        headers_captured = {}
        
        # Siteden giden istekleri dinle
        def handle_request(request):
            if "/api/" in request.url:
                h = request.headers
                if "x-bfp" in h or "x-conversationid" in h:
                    headers_captured.update(h)
        
        page.on("request", handle_request)
        
        print("1. THY ana sayfasına gidiliyor...")
        await page.goto("https://www.turkishairlines.com")
        print("2. Tarayıcının gizli şifreleri (token) üretmesi bekleniyor...")
        await page.wait_for_timeout(6000) # 6 saniye bekle
        
        print("\n--- YAKALANAN GİZLİ HEADER'LAR ---")
        if not headers_captured:
            print("Hiçbir x- header'ı yakalanamadı. Site daha yavaş yüklenmiş olabilir.")
        for k, v in headers_captured.items():
            if k.startswith("x-"):
                print(f"{k}: {v}")
        print("----------------------------------\n")
        
        await browser.close()

if __name__ == "__main__":
    asyncio.run(main())