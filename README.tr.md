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
| Kimlik doğrulama | `--auth-env API_TOKEN --auth-style bearer` | Anahtar ortam değişkeninden okunur; komut satırına veya config'e yazılmaz |
| Uç daraltma | `--tag`, `--include`, `--exclude` | Modele 200 yerine 5 araç gösterir → token tasarrufu |
| Doktor | `mcpify doctor ./openapi.json` | Spec'i denetler: eksik operationId, ölü server adresleri vb. |
| Zaman aşımı | `--timeout 30` | API yavaşsa ajanı bekletmez |
| Agent yüzeyi | varsayılan + `--lazy`, `--enable-preview` | HTTP'den annotation'lar, structured output, öğreten hatalar; api.weather.gov'da ölçülmüş **%95,5** listeleme kazancı |

## Neden önemli?

- **Odaklı ve üretim hazırı:** Tek iş (OpenAPI → MCP), tek arayüz (stdio
  üzerinde tek komut), sıfır runtime bağımlılığı. Odaklılık küçüklük demek değil:
  11 pakette 162 test, çift MCP-spec uyumu, politika katmanı, cache, güvenli
  retry ve health sorgusu bu tek işi destekliyor.
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

**162 test geçiyor**; bunlardan biri gerçek api.weather.gov dokümanını
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
