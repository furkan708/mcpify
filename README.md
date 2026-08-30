<!-- mcp-name: io.github.furkan708/mcpify -->

# mcpify

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
![License](https://img.shields.io/badge/license-MIT-green)

English | [Türkçe](README.tr.md)

<p align="center">
  <img src="docs/demo.gif" alt="mcpify in action — listing and serving OpenAPI endpoints as MCP tools" width="720">
</p>

[![Tests](https://img.shields.io/badge/tests-263%20passed-brightgreen)](https://github.com/furkan708/mcpify/actions/workflows/ci.yml)
[![CodeQL](https://github.com/furkan708/mcpify/actions/workflows/codeql.yml/badge.svg)](https://github.com/furkan708/mcpify/actions/workflows/codeql.yml)
[![Platforms](https://img.shields.io/badge/platform-Linux%20%7C%20Windows-lightgrey)](.github/workflows/ci.yml)
[![MCP Registry](https://img.shields.io/badge/MCP_Registry-listed-4A90D9)](server.json)
[![CI](https://github.com/furkan708/mcpify/actions/workflows/ci.yml/badge.svg)](https://github.com/furkan708/mcpify/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-261230.svg)](https://github.com/astral-sh/ruff)
[![Types: mypy](https://img.shields.io/badge/types-mypy-blue)](https://mypy-lang.org/)
[![PyPI](https://img.shields.io/pypi/v/mcpify-openapi)](https://pypi.org/project/mcpify-openapi/)
[![PyPI Downloads](https://img.shields.io/pypi/dm/mcpify-openapi)](https://pypi.org/project/mcpify-openapi/)
[![Run with uvx](https://img.shields.io/badge/run%20with-uvx-DE5FE9)](https://docs.astral.sh/uv/)
![Dependencies](https://img.shields.io/badge/dependencies-zero-success)

**Turn any OpenAPI REST API into an [MCP](https://modelcontextprotocol.io) server** — so Claude Code, Cursor, and every other MCP client can call your API directly.

mcpify is **focused, production-ready, and CLI-first**: one job (OpenAPI → MCP), zero runtime dependencies. Focused doesn't mean small — 263 tests across seventeen suites, two transports (stdio + HTTP), dual MCP-spec compatibility, OAuth2, a policy layer, caching, safe retries, and health probes back that one job.


Your company has a REST API. Your AI agent needs to call it. Until now that
meant hand-writing a custom MCP server for every API. With mcpify:

```bash
mcpify serve https://your-company.com/openapi.json
```

That's it — every endpoint just became a tool your AI agent can discover, understand, and call.

**Deep docs:** [Usage guide](docs/USAGE.md) — auth patterns, scoping, Docker, troubleshooting · [Architecture](docs/ARCHITECTURE.md) · [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md) · [Security](SECURITY.md)

**The launch story:** [How a live weather API broke this tool — and made it better](https://dev.to/furkan708/i-connected-a-real-weather-api-to-claude-in-3-commands-and-the-community-broke-my-tool-in-the-3jid)

## Why you'll like it

- **60 seconds to working** — point it at any OpenAPI 3.x spec (file or URL)
- **Credentials never touch the spec or the model** — pulled from your
  environment at call time (`--auth-env`), sent as `Authorization: Bearer`,
  a custom header, or a query parameter
- **Every operation becomes a first-class MCP tool** — input schemas are
  generated from `parameters` + `requestBody`, internal `$ref`s are resolved
- **Scope it down** — `--read-only` (GET only), `--tag payments`,
  `--include /v1/orders`, `--exclude /admin`, plus a policy layer for real-world
  APIs: `--deny REGEX` hides mutating GETs, `--allow REGEX` re-includes
  read-style POST endpoints. Deny always wins.
- **`mcpify doctor`** — tells you if your spec is agent-friendly before you ship
- **Multiple environments? Pick one.** Specs declaring
  prod/staging/dev `servers[]` get `--server 2` or `--server staging`
  (description or URL match) instead of a hand-typed `--base-url`
- **Two transports, one tool surface.** `serve` speaks stdio to local
  agents; `serve --http 8080` speaks MCP Streamable HTTP so a whole team
  (or a gateway) can share one server — optional bearer token with
  `--http-token`, stateless per the current MCP spec
- **OAuth2 client-credentials built in** — point it at your identity
  provider's token endpoint; tokens are fetched, cached, refreshed, and
  re-fetched automatically on a mid-flight 401 (RFC 6749, stdlib only)
- **`mcpify try`** — an interactive terminal REPL to call the generated
  tools without any agent client: pick a tool, fill the arguments, see the
  real response. Same execution path as MCP `tools/call`
- **`mcpify output-server`** — bake a serve command into a small shareable
  script: teammates run `python3 server.py` and get the identical MCP server
- **Operational, not just functional.** `mcpify init` wizard + `.mcpify.toml`
  configs with per-environment sections, GET response caching (`--cache-ttl`),
  safe retries (`--retry` — idempotent methods only, 502/503/504 only),
  verbose/log-file logging with masked credentials, XML→JSON conversion,
  strict argument mode, origin auto-discovery, legacy batch tolerance, and
  a health probe (`mcpify status` / `mcpify_health`)
- **Zero runtime dependencies** — the entire tree is auditable stdlib
  Python; YAML specs need an optional `pip install 'mcpify[yaml]'`
- **Agent-grade surface.** Tool annotations derived from HTTP semantics
  (clients auto-approve read-only tools), structured output via MCP
  `outputSchema`/`structuredContent`, remediation-grade errors that teach the
  next call, dry-run request previews, and a `--lazy` search-then-call mode
  that cut api.weather.gov's listing by **95.5%** (38,882 → 1,741 chars)
- **263 tests across seventeen suites** — including full MCP protocol runs
  over stdio *and* over HTTP against real local APIs and the **live
  api.weather.gov document** (69 tools, 16 enum'd parameters)

## Quick start

```bash
# run without installing (uvx — pulls from PyPI on demand)
uvx --from mcpify-openapi mcpify list ./openapi.json --read-only

# first time? the wizard writes a config for you
uvx --from mcpify-openapi mcpify init

# or install (installs the `mcpify` command)
pipx install mcpify-openapi

# ...as a container (GHCR, published on every release)
docker run -i ghcr.io/furkan708/mcpify:latest serve ./openapi.json --read-only

# ...or from source
git clone https://github.com/furkan708/mcpify.git
cd mcpify && pip install .

# 1. preview the tools that will be generated
mcpify list examples/petstore.json

# 2. validate the spec is agent-friendly
mcpify doctor examples/petstore.json

# 3. serve it over MCP
mcpify serve examples/petstore.json --base-url https://petstore.example.com/v1

# 4. no agent client at hand? try the tools in your terminal
mcpify try examples/petstore.json --base-url https://petstore.example.com/v1

# 5. or share it over HTTP with the whole team
mcpify serve examples/petstore.json --http 8080 --http-token $SHARED_TOKEN
```

### With authentication

```bash
# Bearer token read from the environment (never hardcoded)
export PETSTORE_KEY="sk-..."
mcpify serve petstore.json \
  --base-url https://petstore.example.com/v1 \
  --auth-env PETSTORE_KEY \
  --auth-style bearer \
  --read-only
```

| Flag | Meaning |
| ---- | ------- |
| `--auth-env VAR` | environment variable holding the credential |
| `--auth-style bearer\|header\|query` | how it is sent |
| `--auth-name NAME` | header / query name for non-bearer styles (e.g. `X-API-Key`) |

### With OAuth2 (client credentials)

For APIs behind an OAuth2 identity provider (RFC 6749 §4.4). Credentials
live in the environment; the access token is fetched, cached until its
`expires_in`, refreshed transparently, and re-fetched automatically once
if the API answers 401 mid-flight:

```bash
export OAUTH2_CLIENT_ID="..."
export OAUTH2_CLIENT_SECRET="..."
mcpify serve api.json \
  --oauth2-token-url https://idp.example.com/oauth2/token \
  --oauth2-client-id-env OAUTH2_CLIENT_ID \
  --oauth2-client-secret-env OAUTH2_CLIENT_SECRET \
  --oauth2-scope "read write"        # optional; --oauth2-client-auth body for token endpoints that reject Basic
```

## Plug it into your agent

**Claude Code:**

```bash
claude mcp add my-api -- mcpify serve openapi.json --read-only
```

**Claude Desktop / Cursor / any MCP client** (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "petstore": {
      "command": "mcpify",
      "args": ["serve", "~/specs/petstore.json", "--auth-env", "PETSTORE_KEY"]
    }
  }
}
```

**HTTP transport (team-shared server)** — run `mcpify serve api.json --http 0.0.0.0:8080 --http-token $TOKEN` once, then point HTTP-capable clients at it:

```json
{
  "mcpServers": {
    "petstore": {
      "type": "http",
      "url": "http://your-host:8080",
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

Now ask your agent: *"list the pets, then create one named Milo"* — it discovers `list_pets` and `create_pet`, fills the arguments, and performs real HTTP calls.

## How operations become tools

| OpenAPI | mcpify |
| ------- | ------ |
| `operationId` | tool name (sanitized; falls back to `method_path`) |
| `summary` / `description` | tool description the agent reads |
| `deprecated: true` | shown by `mcpify list` before you expose old endpoints |
| `parameters` (path/query/header) | individual typed arguments with enums |
| `requestBody` (JSON) | a `body` object argument |
| `$ref` pointers | resolved inline (components → real schemas) |
| `servers[0].url` | default base URL (override: `--base-url`) |

The agent only ever sees the tool list and your API's JSON responses —
mcpify adds no middleware, caches nothing, and sends credentials nowhere
except your API.

## Doctor

```bash
$ mcpify doctor my-api.json
openapi: 3.0.3
title:   Acme API
paths:   23
tools:   41 operations
servers: https://api.acme.com
warning: 12/41 operations have no operationId (names fall back to method_path)
warning: 30/41 operations have no summary (agents see no description)
```

## CLI reference

```
mcpify list <spec> [--tag T] [--include P] [--exclude P] [--read-only] [--json]
mcpify serve <spec> [--base-url URL] [--server INDEX|NAME] [--name N] [--auth-env VAR]
                    [--auth-style bearer|header|query] [--auth-name NAME]
                    [--oauth2-token-url URL --oauth2-client-id-env VAR
                     --oauth2-client-secret-env VAR] [--timeout S]
                    [--read-only] [--tag T] [--include P] [--exclude P]
                    [--http [HOST:]PORT] [--http-token TOKEN]
mcpify try <spec> [same serve flags]        # interactive REPL, no agent needed
mcpify output-server <spec> -o FILE [-- <any serve flags>]
mcpify doctor <spec>
```

### Notes & limitations

- JSON specs work out of the box; YAML specs need `pip install 'mcpify[yaml]'`
- Only local `$ref` pointers are resolved (bundle external docs first — most tools do anyway)
- Request bodies are exposed as a single `body` object argument — predictable over clever
- HTTP transport serves one JSON-RPC message per request (batching was removed from the MCP spec) and responds `application/json` — a stateless server has nothing to stream
- Spec versions: OpenAPI 3.x and Swagger 2.x roots are accepted; 3.x is the happy path

## Hardened against the real world

mcpify is audited on every release against a 10-category checklist of MCP
best practices and published production failure modes — not just our own
examples:

- **Hostile-spec corpus (12/12):** circular `$ref`s, multipart uploads,
  `allOf` schemas, server URL variables, relative base URLs, oversized
  responses — every scenario derived from a documented real-world failure,
  fixed, and locked in by a regression test. Sources include the arXiv
  study of REST→MCP generation across 18 real APIs.
- **Live integration:** the real api.weather.gov spec loads in CI — the
  case that found (and fixed) our last crash-class bug.
- **MCP lifecycle enforced:** tools are unreachable until the client
  completes the `initialize` handshake.
- **Blast-radius controls:** read-only mode, deny/allow policy layer,
  40k-char response truncation, `--timeout`, credentials never logged.

Full checklist with per-item status: **[docs/AUDIT-CHECKLIST.md](docs/AUDIT-CHECKLIST.md)**

## Tests

**263 passing**, plus one live-integration test that loads the real
api.weather.gov document (auto-skipped when offline). Every suite runs on
Python 3.10–3.12 across Linux and Windows; `ruff`, strict `mypy` and
CodeQL gate every push.

| Suite | Tests | What it pins down |
|---|---:|---|
| Spec parsing & resolution | 13 | OpenAPI 3.x + YAML loading, `$ref` chains, `allOf` merge, server variables, malformed input |
| Tool translation | 19 | operationId naming with collision suffixing, input schemas, enums, body handling, annotation & output-schema derivation |
| Agent surface | 31 | HTTP-derived annotations, structured output contract, remediation errors, `--lazy` search, dry-run previews |
| CLI | 15 | `list` / `doctor` / `serve` flags, `--json` output, deprecated badges |
| Hostile corpus | 11 | circular `$ref`s, multipart bodies, relative base URLs, 300 KB truncation, 500-op performance — each traced to a documented real-world failure |
| Lifecycle & hygiene | 8 | initialize handshake (`-32002`), byte-pure stdio, credentials never logged |
| Protocol end-to-end | 9 | real JSON-RPC over stdio against a live local HTTP API, wire-level assertions |
| Policy layer | 7 | `--read-only`, `--allow` / `--deny` precedence, mutating-GET protection |
| `$ref` parameters | 4 | parameter schemas resolved against the full spec — the weather.gov bug class (one test hits the live document) |
| Ops & configuration | 41 | config files + env precedence, init wizard, cache TTL & bounds, retry safety, XML conversion, discovery, batching, status/health |
| Protocol version compat | 5 | 2026-07-28 stateless `_meta` requests and the legacy 2025-06-18 handshake, on the same wire |
| HTTP transport | 19 | Streamable HTTP: lifecycle over POST, 405/411/413/415 error ladder, parse/batch rejections, bearer enforcement, bind-string parser |
| OAuth2 client-credentials | 18 | token fetch/cache/refresh with a fake clock, Basic vs body client auth, public clients, every failure mode, 401 self-heal end-to-end |
| `try` REPL | 26 | piped-stdin sessions: selection by number/name, typed prompts, re-prompt on bad input, `:raw`/`:info`, clean EOF/Ctrl+C exits, read-only surface |
| `output-server` | 10 | embedded spec integrity, guard rails (existing file, bad spec, unknown flags), secret warnings, and a real subprocess E2E handshake |
| Server selection | 17 | `--server INDEX|NAME` rules: index, description/URL name matching, error listings, `--base-url` precedence, server-variable defaults, CLI/status/config/doctor wiring |
| CLI connectivity glue | 10 | `--http` wiring, `MCPIFY_HTTP_TOKEN` fallback, OAuth2 flag rules, config-file keys, wizard option 5, `try` smoke test |

Policy on failures: every bug found in the wild becomes a pinned
regression test before the fix ships — the suite only grows.

Run it locally:

```bash
pip install pytest pyyaml
pytest -v
```

## Project Structure

```
mcpify/
├── mcpify/
│   ├── spec.py          # OpenAPI loading, $ref resolution, operation walking
│   ├── tools.py         # operation -> MCP tool, argument -> HTTP request
│   ├── http_client.py   # execution (urllib, HTTP errors become tool results), OAuth2 flow
│   ├── api_server.py    # the MCP server core (JSON-RPC 2.0, tools, policy)
│   ├── http_transport.py# Streamable HTTP transport (--http)
│   ├── repl.py          # `mcpify try` interactive terminal REPL
│   ├── standalone.py    # `mcpify output-server` script generator
│   └── cli.py           # list / serve / try / output-server / doctor / status / init
├── examples/petstore.json
└── tests/
```

## Roadmap

- [ ] SSE streaming responses for the HTTP transport (server-initiated messages)
- [ ] Multi-API aggregation: one `serve` process fronting several OpenAPI documents
- [x] ~~HTTP transport~~, ~~OAuth2 client-credentials~~, ~~`mcpify try` REPL~~, ~~`--output-server`~~ — shipped in v1.6.0

## License

MIT — see the [LICENSE](LICENSE) file for details.
