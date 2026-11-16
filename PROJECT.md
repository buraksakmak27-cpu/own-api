# own-api — Proje Özeti

## Ne yapıyor?
Kullanıcının kendi Chrome'u üzerinden, API'si olmayan sitelerden
otomatik API üreten, MCP ile Claude'a bağlayan sistem.

## Mimari (4 Katman)
1. Keşif: recorder.py (CDP trafik) + compiler.py (AI) + registry/
2. Kalıcı Tarayıcı: launch_persistent_context (.browser_profile)
3. Self-Healing: healer.py (fetch tüneli) + session.py (oturum)
4. Ajan Köprüsü: mcp_server.py + passport.py + audit.py

## Altın Kural
- Fingerprint taklidi YOK
- Proxy rotasyonu YOK
- TLS spoofing YOK
- Sadece kullanıcının gerçek Chrome'u, gerçek oturumu, gerçek IP'si

## Şu An (FAZ A0)
Sadece Chrome Bridge yazıyoruz.

## Klasör Yapısı
core/       → chrome_bridge, recorder, compiler, healer, session, shield
registry/   → site tarifleri (client.py + schema.json)
own_api/    → cli, mcp_server, sdk
tests/      → pytest
panel/      → HTML panel

## Bağımlılıklar
playwright, fastapi, uvicorn, anthropic, pydantic, mcp,
sqlalchemy, pytest, pytest-asyncio, cryptography, click

## Kalıcı Profil
~/.own-api/.browser_profile — çerezler, oturum burada

## CLI Komutu
`own-api list` / `own-api call ...` / `own-api add ...` / `own-api ask "..."`