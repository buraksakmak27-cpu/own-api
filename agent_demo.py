"""own-api mini ajanı — OpenAI function calling + registry/ araçları.

Çalıştırma:  venv\\Scripts\\python agent_demo.py
Çıkmak için: çık
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from openai import AsyncOpenAI  # noqa: E402

from own_api.cli import _load_schema, _run_search, _sites  # noqa: E402

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o")
MAX_ITEMS_TO_AI = 20      # AI'a gönderilecek en fazla sonuç
MAX_TOOL_ROUNDS = 5       # bir soruda en fazla kaç tur araç çağrısı
EXIT_WORDS = {"çık", "cik", "çıkış", "exit", "quit", "q"}

def build_system_prompt() -> str:
    today = date.today()
    tomorrow = today + timedelta(days=1)
    return (
        "Sen own-api asistanısın. Kullanıcının sorularını araçları kullanarak cevapla. "
        "Fiyat ve ürün bilgisi için mutlaka araç çağır; araç sonuçlarına dayan, uydurma. "
        "Türkçe, kısa ve düzenli (madde işaretli) cevap ver; fiyatları TL olarak yaz.\n"
        "\n"
        "ARAÇ PARAMETRELERİ: Bir araç birden fazla parametre istiyorsa (örn. uçuş araması için "
        "origin, destination, departure_date), bunların HEPSİNİ kullanıcının cümlesinden çıkar ve "
        "aracı çağırırken doğru alanlara yerleştir. Zorunlu bir bilgi cümlede yoksa uydurma; "
        "kullanıcıya sor.\n"
        "- Kullanıcı bir uçuş ararsa, kalkış ve varış şehirlerini ICAO/IATA kodlarına çevirmeye "
        "çalış (örn. İstanbul=IST, Ankara=ESB, İzmir=ADB, Antalya=AYT, Londra=LHR).\n"
        "- Tarih belirtilirse, formatını THY'nin beklediği 'GG-AA-YYYY' formatına uyarla "
        "(örn. 2 Mart 2027 → 02-03-2027).\n"
        f"- Bugünün tarihi: {today:%d-%m-%Y}. 'Bugün'={today:%d-%m-%Y}, 'yarın'={tomorrow:%d-%m-%Y}. "
        "Göreli tarihleri (yarın, haftaya, 3 gün sonra) buna göre hesapla.\n"
        "- Aracın parametre açıklamasında 'örnek format' verilmişse, değeri o formatta gönder."
    )


def build_tools() -> tuple[list[dict], dict[str, str]]:
    """registry/*/schema.json → OpenAI tools listesi ve {tool_adı: site}.

    schema.json'daki inputSchema tüm parametreleri (tek 'query' ya da çoklu origin/
    destination/date...) tipleri ve 'örnek format' açıklamalarıyla taşır; olduğu gibi
    AI'a verilir ki çoklu parametreli araçları da doğru doldurabilsin.
    """
    tools, tool_to_site = [], {}
    for site in _sites():
        schema = _load_schema(site)
        name = schema.get("name") or f"{site}_search"
        tool_to_site[name] = site

        input_schema = schema.get("inputSchema") or {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Arama kelimesi"}},
            "required": ["query"],
        }
        params = list((input_schema.get("properties") or {}).keys())
        description = schema.get("description") or f"{site} üzerinde arama"
        if params:
            description += f" — parametreler: {', '.join(params)}"

        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": input_schema,
                },
            }
        )
    return tools, tool_to_site


async def run_tool(name: str, arguments: str, tool_to_site: dict[str, str]) -> str:
    site = tool_to_site.get(name)
    if site is None:
        return json.dumps({"error": f"Bilinmeyen araç: {name}"}, ensure_ascii=False)
    try:
        args = json.loads(arguments or "{}")
        print(f"   🔎 {site} aranıyor: {args}")
        results = await _run_search(site, **args)
        print(f"   ✅ {len(results)} sonuç")
        return json.dumps(results[:MAX_ITEMS_TO_AI], ensure_ascii=False, default=str)
    except Exception as e:  # hatayı AI'a bildir, ajan çökmesin
        msg = getattr(e, "message", None) or str(e)
        print(f"   ❌ {type(e).__name__}: {msg}")
        return json.dumps({"error": f"{type(e).__name__}: {msg}"}, ensure_ascii=False)


async def answer(client: AsyncOpenAI, messages: list, tools: list, tool_to_site: dict) -> str:
    """Mesajı gönderir; AI araç isterse çalıştırıp sonucu geri verir, özet döndürür."""
    for _ in range(MAX_TOOL_ROUNDS):
        resp = await client.chat.completions.create(
            model=MODEL, messages=messages, tools=tools or None
        )
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            return msg.content or ""
        for call in msg.tool_calls:
            content = await run_tool(call.function.name, call.function.arguments, tool_to_site)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    # Tur sınırı aşıldı: araçsız son bir özet iste.
    resp = await client.chat.completions.create(model=MODEL, messages=messages)
    final = resp.choices[0].message.content or ""
    messages.append({"role": "assistant", "content": final})
    return final


async def main() -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("OPENAI_API_KEY ortam değişkeni tanımlı değil.")
        return

    tools, tool_to_site = build_tools()
    if not tools:
        print("registry/ içinde kayıtlı site yok. Önce: own-api add <site> <url>")
        return

    client = AsyncOpenAI(api_key=api_key)
    messages: list = [{"role": "system", "content": build_system_prompt()}]

    print(f"🤖 own-api ajanı ({MODEL}) — araçlar: {', '.join(tool_to_site)}")
    print("   Çıkmak için 'çık' yazın.\n")

    while True:
        try:
            user_input = (await asyncio.to_thread(input, "Siz: ")).strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not user_input:
            continue
        if user_input.lower() in EXIT_WORDS:
            break

        checkpoint = len(messages)
        messages.append({"role": "user", "content": user_input})
        try:
            reply = await answer(client, messages, tools, tool_to_site)
        except Exception as e:  # API hatası vb. — döngü devam etsin
            print(f"⚠️  Hata: {type(e).__name__}: {e}\n")
            # Yarım kalan tur (cevapsız tool_calls) sonraki istekleri bozmasın.
            del messages[checkpoint:]
            continue
        print(f"\nAjan: {reply}\n")

    print("👋 Görüşmek üzere!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Görüşmek üzere!")
