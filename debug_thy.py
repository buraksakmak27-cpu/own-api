import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.healer import Healer
import registry.thy.client as thy

# Healer'ın veri çekme fonksiyonunu geçici olarak ele geçirip (monkeypatch) ham veriyi ekrana basıyoruz
original_fetch = Healer.fetch
async def debug_fetch(self, *args, **kwargs):
    data = await original_fetch(self, *args, **kwargs)
    print("\n--- DEBUG: GELEN HAM CEVAP ---")
    print(str(data)[:1000])
    print("------------------------------\n")
    return data

Healer.fetch = debug_fetch

async def main():
    try:
        # THY aramasını tetikle
        res = await thy.search(origin="IST", destination="ESB", departure_date="02-03-2027", return_date="09-03-2027")
        print("Filtrelenmiş Sonuç:", res)
    except Exception as e:
        print("Hata:", e)

if __name__ == "__main__":
    asyncio.run(main())