<div align="center">

# ⚡ mcpify

**OpenAPI dokümanı olan herhangi bir REST API'yi tek komutla MCP sunucusuna dönüştürün.**

🌐 [Türkçe](README.tr.md) | [English](README.md)

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
| 🔒 Salt-okunur mod | `--read-only` | Yalnızca GET uçlarını açar; ajan (agent) okur, yazamaz |
| 🔑 Kimlik doğrulama | `--auth-env API_TOKEN --auth-style bearer` | Anahtar ortam değişkeninden okunur; komut satırına veya config'e yazılmaz |
| 🎯 Uç daraltma | `--tag`, `--include`, `--exclude` | Modele 200 yerine 5 araç gösterir → token tasarrufu |
| 🩺 Doktor | `mcpify doctor ./openapi.json` | Spec'i denetler: eksik operationId, ölü server adresleri vb. |
| ⏱️ Zaman aşımı | `--timeout 30` | API yavaşsa ajanı bekletmez |

## Neden önemli?

- **Token bütçesi:** Her araç tanımı modelin bağlam penceresini (context) tüketir.
  mcpify'ın filtreleriyle yalnızca ilgili uçları açarsınız; CLI tabanlı yaklaşımlardan
  belirgin biçimde daha az token harcarsınız.
- **Güvenlik varsayılanı:** Yazma uçlarını kapatmak tek bayrakla olur. Ajanlara
  "bakabilirler ama dokunamazlar" demek, üretimde standart olmalı.
- **Sıfır bakım:** Spec'iniz değişirse sunucu yeniden başlatıldığında araçlar otomatik güncellenir.

## Test ve kalite

- ✅ **60 test** (birim + uçtan uca MCP protokol testleri)
- ✅ **mypy strict** tip denetimi, **ruff** lint
- ✅ CI her push'ta çalışır: [badge'e bakın](https://github.com/furkan708/mcpify/actions)

## Dokümantasyon

- 📖 [Kullanım Kılavuzu (İngilizce)](docs/USAGE.md) — auth desenleri, Docker, sorun giderme, SSS
- 🏗️ [Mimari (İngilizce)](docs/ARCHITECTURE.md) — istek yaşam döngüsü, tasarım kararları

## Katkı

Katkılara açıktır! Bkz. [CONTRIBUTING.md](CONTRIBUTING.md) · Güvenlik: [SECURITY.md](SECURITY.md)

---

<div align="center">

**mcpify** — MIT lisansı · Python 3.10+ · Claude, Claude Code, Cursor ve tüm MCP istemcileriyle çalışır

</div>
