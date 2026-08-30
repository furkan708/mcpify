<div align="center">

# mcpify

**OpenAPI dokümanı olan herhangi bir REST API'yi tek komutla MCP sunucusuna dönüştürün.**

[Türkçe](README.tr.md) | [English](README.md)

[![CI](https://github.com/furkan708/mcpify/actions/workflows/ci.yml/badge.svg)](https://github.com/furkan708/mcpify/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![mypy](https://img.shields.io/badge/mypy-strict-blue)](https://mypy-lang.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

</div>

---

## Bu araç ne yapar?

Claude Desktop, Claude Code veya Cursor'a **kendi REST API'nizi** bağlamak istediniz mi?
Her MCP eğitimi aynı cümlede bitiyor: *"şimdi özel bir MCP sunucusu yazın."*
40 uçlu (endpoint) bir API için bu; şemalar, doğrulama, hata yönetimi ve bakım yükü demek.

**mcpify bu adımı tamamen siliyor.** OpenAPI 3.x dokümanını gösterin — her endpoint,
tipatanmış parametrelerle bir MCP aracına dönüşür. Kod üretimi yok, şablon kod yok.

## 30 saniyede başlangıç

```bash
# 1. Kurun
pipx install mcpify-openapi
# ya da kaynaktan: pipx install git+https://github.com/furkan708/mcpify

# 2. Modeliniz ne görecek, önce önizleyin
mcpify list ./openapi.json --read-only

# 3. Sunucuyu başlatın — API'niz artık bir MCP sunucusu
mcpify serve ./openapi.json --read-only

# 4. Elinizin altında ajan istemcisi yok mu? Araçları terminalden deneyin
mcpify try ./openapi.json

# 5. Ya da tüm ekiple paylaşın: HTTP üzerinden MCP
mcpify serve ./openapi.json --http 8080 --http-token $PAYLASILAN_TOKEN
```

Claude Desktop yapılandırması (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "benim-api": {
      "command": "mcpify",
      "args": ["serve", "./openapi.json", "--read-only"]
    }
  }
}
```

Cursor için aynı JSON'u `.cursor/mcp.json` dosyasına koyun. Claude Code için:
`claude mcp add benim-api -- mcpify serve ./openapi.json`

## Öne çıkan özellikler

| Özellik | Komut | Ne işe yarar |
|---------|-------|--------------|
| Salt-okunur mod | `--read-only` | Yalnızca GET uçlarını açar; ajan (agent) okur, yazamaz |
| Kimlik doğrulama | `--auth-env API_TOKEN --auth-style bearer` | Anahtar ortam değişkeninden okunur; komut satırına veya config'e yazılmaz. **Stil çoğu zaman gerekmez:** spec'in security bildirimlerinden otomatik seçilir (bearer/Basic/header/query, doğru adla) |
| Uç daraltma | `--tag`, `--include`, `--exclude` | Modele 200 yerine 5 araç gösterir → token tasarrufu |
| Doktor | `mcpify doctor ./openapi.json` | Spec'i denetler: eksik operationId, ölü server adresleri vb. |
| Zaman aşımı | `--timeout 30` | API yavaşsa ajanı bekletmez |
| Agent yüzeyi | varsayılan + `--lazy`, `--enable-preview` | HTTP'den annotation'lar, structured output, öğreten hatalar; api.weather.gov'da ölçülmüş **%95,5** listeleme kazancı |
| Sunucu seçimi | `--server 2` / `--server staging` | Spec'te prod/staging gibi birden çok `servers[]` varsa indeks veya isimle seç; `--base-url` hâlâ nihai sözü söyler |
| İki transport | `serve` / `serve --http 8080` | stdio yerel ajanlar için; Streamable HTTP ekip/gateway paylaşımı için (`--http-token` ile bearer koruması) |
| OAuth2 client-credentials | `--oauth2-token-url ... --oauth2-client-id-env ...` | Token endpoint'ine bağlanır; token'ı çeker, cache'ler, yeniler, 401'de kendini düzeltir |
| HTTP Basic | `--auth-style basic --auth-env CREDS` | env `kullanici:sifre` tutar; `Authorization: Basic …` üretilir — iç API'lerin klasik akışı |
| 429 nezaketi | `--wait-on-429 30` | API "Retry-After" dediğinde idempotent çağrı için **bir kez** bekler (üst sınır aşılırsa 429 dürüstçe döner); POST/PATCH asla otomatik beklenmez |
| Kendi sunucun (bedava) | `deploy/docker-compose.yml` | Otomatik HTTPS'li Caddy + çift katman bearer: hosted-MCP planlarının ayda $9–229 ücretlendirdiğini $5 VPS'e taşır — [Self-hosting rehberi](docs/SELF-HOSTING.md) |
| Terminal REPL'i | `mcpify try ./openapi.json` | Ajan istemcisi olmadan araçları elle çağırın: seç, argüman gir, gerçek yanıtı gör |
| Paylaşılabilir sunucu | `mcpify output-server spec.json -o server.py` | serve komutunu küçük bir betiğe gömer; ekip `python3 server.py` der ve aynı sunucuyu alır |

## Neden önemli?

- **Odaklı ve üretim hazırı:** Tek iş (OpenAPI → MCP), tek arayüz (stdio
  üzerinde tek komut), sıfır runtime bağımlılığı. Odaklılık küçüklük demek değil:
  18 pakette 294 test, iki transport (stdio + HTTP), çift MCP-spec uyumu, OAuth2,
  politika katmanı, cache, güvenli retry ve health sorgusu bu tek işi destekliyor.
- **Token bütçesi:** Her araç tanımı modelin bağlam penceresini (context) tüketir.
  mcpify'ın filtreleriyle yalnızca ilgili uçları açarsınız; CLI tabanlı yaklaşımlardan
  belirgin biçimde daha az token harcarsınız.
- **Güvenlik varsayılanı:** Yazma uçlarını kapatmak tek bayrakla olur. Ajanlara
  "bakabilirler ama dokunamazlar" demek, üretimde standart olmalı.
- **Sıfır bakım:** Spec'iniz değişirse sunucu yeniden başlatıldığında araçlar otomatik güncellenir.

## Gerçek dünyaya karşı sağlamlaştırıldı

Her sürümde 10 kategorilik denetim listesi yeniden koşulur: döngüsel `$ref`,
multipart gövde, server URL değişkenleri, relative base URL, devasa yanıtlar —
12 senaryonun hepsi yayımlanmış hata çalışmalarından türetildi, düzeltildi ve
regresyon testiyle kilitlendi. MCP yaşam döngüsü zorunlu tutulur; 40k üzeri
yanıtlar kesilir; kimlik bilgileri asla loglanmaz.

Tam liste: **[docs/AUDIT-CHECKLIST.md](docs/AUDIT-CHECKLIST.md)**

## Test ve kalite

**294 test geçiyor**; bunlardan biri gerçek api.weather.gov dokümanını
yükleyen canlı entegrasyon testidir (çevrimdışında otomatik atlanır).
Tüm paketler Python 3.10–3.12 üzerinde Linux ve Windows'ta koşar; her
push'ta `ruff`, strict `mypy` ve CodeQL devreye girer
([badge](https://github.com/furkan708/mcpify/actions)).

| Paket | Test | Neleri kilitler |
|---|---:|---|
| Spec ayrıştırma & çözümleme | 13 | OpenAPI 3.x + YAML yükleme, `$ref` zincirleri, `allOf` birleştirme, server değişkenleri, bozuk girdi |
| Tool çevirisi | 19 | operationId isimlendirme ve çakışma son eki, input şemaları, enum'lar, gövde işleme, annotation & output şeması türetimi |
| Agent yüzeyi | 31 | HTTP'den türetilen annotation'lar, structured output sözleşmesi, remediation hataları, `--lazy` arama, dry-run önizleme |
| CLI | 15 | `list` / `doctor` / `serve` bayrakları, `--json` çıktı, deprecated etiketi |
| Düşmanca korpus | 11 | döngüsel `$ref`, multipart gövde, relative base URL, 300 KB kısaltma, 500-op performans — her biri belgelenmiş gerçek hata sınıfından |
| Yaşam döngüsü & hijyen | 8 | initialize el sıkışması (`-32002`), bayt-saf stdio, kimlik bilgileri asla loglanmaz |
| Protokol uçtan uca | 9 | gerçek yerel HTTP API'ye karşı stdio üzerinden JSON-RPC, tell-seviyesi doğrulama |
| Politika katmanı | 7 | `--read-only`, `--allow` / `--deny` önceliği, yazan-GET koruması |
| `$ref` parametreleri | 4 | parametre şemalarının tam spec'e karşı çözümlenmesi — weather.gov hata sınıfı (biri canlı dokümana vurur) |
| Protokol sürüm uyumu | 5 | 2026-07-28 stateless `_meta` istekleri ve eski 2025-06-18 el sıkışması aynı hatta |
| Operasyon & yapılandırma | 41 | config dosyaları + env önceliği, init sihirbazı, cache TTL, retry güvenliği, XML dönüşümü, keşif, batch, status/health |
| HTTP transport | 19 | Streamable HTTP: POST üzerinden yaşam döngüsü, 405/411/413/415 hata merdiveni, parse/batch reddi, bearer zorlaması, bind-string ayrıştırıcı |
| OAuth2 client-credentials | 18 | saatle token çekme/cache/yenileme, Basic vs body istemci kimliği, public client, her hata türü, 401 öz-düzeltme |
| `try` REPL | 26 | borulu stdin oturumları: numara/isimle seçim, tipli girdi, hatalı girdide yeniden sorma, `:raw`/`:info`, temiz EOF/Ctrl+C çıkışı |
| `output-server` | 10 | gömülü spec bütünlüğü, koruma rayları, sır uyarıları ve gerçek subprocess E2E el sıkışması |
| CLI bağlantı yapıştırıcısı | 10 | `--http` kablolaması, `MCPIFY_HTTP_TOKEN` yedeği, OAuth2 bayrak kuralları, config anahtarları, sihirbaz 5. seçenek, `try` duman testi |

İlke: sahada bulunan her hata, düzeltme gönderilmeden önce regresyon
testine çevrilir — paket yalnızca büyür.

```bash
pip install pytest pyyaml
pytest -v
```

## Dokümantasyon

- [Kullanım Kılavuzu (İngilizce)](docs/USAGE.md) — auth desenleri, Docker, sorun giderme, SSS
- [Mimari (İngilizce)](docs/ARCHITECTURE.md) — istek yaşam döngüsü, tasarım kararları

## Katkı

Katkılara açıktır! Bkz. [CONTRIBUTING.md](CONTRIBUTING.md) · Güvenlik: [SECURITY.md](SECURITY.md)

---

<div align="center">

**mcpify** — MIT lisansı · Python 3.10+ · Claude, Claude Code, Cursor ve tüm MCP istemcileriyle çalışır

</div>
