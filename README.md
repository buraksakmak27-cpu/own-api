# 🚀 own-api — The Local-First API Factory for AI Agents

**own-api**, halka açık API'si olmayan web sitelerini, kullanıcının kendi Chrome tarayıcısını ve oturumunu kullanarak **Yapay Zeka Ajanları (MCP & OpenAI Function Calling)** için hazır araçlara dönüştüren açık kaynaklı, "Local-First" (Yerel Öncelikli) bir API fabrikasıdır.

> *"Headless tarayıcı yok, parmak izi taklidi yok, proxy yok. Sadece senin gerçek Chrome'un, gerçek oturumun ve gerçek şifrelerin."*

## 🧠 Nasıl Çalışır?

Geleneksel scraper'lar her istek için görünmez (headless) bir tarayıcı açar (15-30 saniye) ve güvenlik duvarlarına (WAF) takılır. **own-api** ise **"Bir Kez Kaydet, Hızlı Çalıştır"** prensibini kullanır:

1. **Recorder (Kayıt):** Gerçek Chrome'unuzda siteye girersiniz. CDP ağ dinleyicisi, sitenin arka planda çalışan gizli JSON API trafiğini yakalar.
2. **AI Compiler (Derleyici):** GPT-4o, yakalanan trafiği analiz eder, doğru endpoint'i bulur, parametreleri (karmaşık POST gövdeleri dahil) çıkarır ve size temiz bir Python `client.py` dosyası üretir.
3. **Healer (İyileştirici):** Sonraki istekler, sizin gerçek tarayıcınızın içinden `fetch()` ile atılır. Sitenin kendi JavaScript'inin ürettiği dinamik şifreler (`x-bfp`, `x-conversationid`) yasal olarak yakalanıp kullanılır.
4. **Agent Ready (Ajana Hazır):** Üretilen her istemci, otomatik olarak Claude Desktop için bir MCP aracı ve OpenAI Function şeması olarak dışa açılır.

## ✨ Özellikler

- **JSON tabanlı siteler için Sıfır Konfigürasyon:** Tek komutla site ekleyin.
- **Local-First Güvenlik:** Çerezler, oturumlar ve veriler bilgisayarınızdan asla çıkmaz (KVKK & GDPR uyumlu).
- **Kurumsal Düzeyde Denetim:** Her çağrı için Ed25519 imzalı loglar.
- **Çoklu Parametre Desteği:** Karmaşık POST isteklerini (örn: uçuş aramaları) yönetir.
- **Modern WAF Kalkanlarını Aşar:** **THY (Akamai + PerimeterX)** üzerinde parmak izi taklidi yapmadan başarıyla test edilmiştir.
- **Web Paneli:** Arama yapmak ve geçmişi görmek için şık bir yerel arayüz.
- **Veri Kasası:** Zaman yolculuğu sorguları için SQLite depolama.

## 🎯 Kanıtlanmış Sonuçlar

| Site | Koruma | Durum |
|---|---|---|
| **Trendyol** | Cloudflare | ✅ 14 gerçek ürün |
| **THY (Türk Hava Yolları)** | Akamai + PerimeterX | ✅ 14 gerçek uçuş |
| **Kitapyurdu** | Standart | ✅ Çalışıyor |
| **N11 / Hepsiburada** | Sunucu Taraflı Render (SSR) | ❌ (JSON tabanlı değil) |

## 🛡️ "Parmak İzi Yok" Kuralı

**own-api** TLS parmak izlerini taklit etmez, veri merkezi proxy'leri kullanmaz veya captcha'ları otomatik aşmaz. Sadece sitenin kendi JavaScript'inin sizin gerçek tarayıcınızda ürettiği **gerçek şifreleri** yakalar ve kullanır. Eğer bir site sizi engellerse (örn: PerimeterX "basılı tut"), kullanıcı bunu manuel olarak bir kez çözer ve çerez kalıcı profile kaydedilir.

## 🚀 Hızlı Başlangıç

```bash
git clone https://github.com/KULLANICI_ADIN/own-api.git
cd own-api
python -m venv venv
venv\Scripts\activate  # (Windows)
pip install -e .
playwright install chrome

# Yeni site ekle
own-api add trendyol "https://www.trendyol.com" --intent "ürün arama"

# API'yi çağır
own-api call trendyol -p query="saat"

# Karmaşık çoklu parametreli API (THY)
own-api call thy -p origin="IST" -p destination="ESB" -p departure_date="02-03-2027"

# Web Panelini Başlat
python -m core.api
# Tarayıcıda aç: http://127.0.0.1:8000




🗺️ Yol Haritası

    ☑

    Faz A: Çekirdek Motor (Recorder, Compiler, Healer, Session)
    ☑

    Faz A: CLI, Panel, Vault, Passport, Audit
    ☑

    Faz A: MCP Server + Ajan
    ☑

    Faz A: Çoklu Parametreli POST + WAF Yönetimi (THY)
    □

    Faz B: Geliştirici API Anahtarları & Etkileşimli OTP Girişi
    □

    Faz C: Bulut Tarif Borsası (Kullanıcılar arası paylaşım)

📄 Lisans

MIT