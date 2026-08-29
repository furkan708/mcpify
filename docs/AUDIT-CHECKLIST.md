# Engineering Audit Checklist — mcpify

This project is audited against a 10-category checklist of MCP best
practices and common production failure modes. Every release re-runs the
full checklist. Status below reflects **v1.2.0**.

Legend: ✅ verified · 🔧 fixed during audit · ⛔ deliberate limitation
(documented in [USAGE.md](USAGE.md) FAQ)

## 1. Server setup & dependencies
- ✅ Zero runtime dependencies; dev floors pinned with sane minimums
- ✅ Version surfaced from package metadata (no drift)
- ✅ Config file support: `.mcpify.toml`/`.yaml`/`.json`, per-environment
  sections, CLI-flags-wins precedence, unknown-key warnings
- ✅ Installs via `pipx install mcpify-openapi` (PATH-ready command)
- ✅ Cross-platform CI: **Linux + Windows** matrices
- ✅ No dependency conflicts possible (stdlib only)

## 2. Transport & connection
- ✅ stdio, newline-delimited JSON-RPC 2.0 (MCP stdio transport)
- ✅ Lifecycle, both spec generations: legacy handshake enforced
  (`tools/*` before `initialize` → `-32002`); stateless requests carrying
  `_meta` protocolVersion (2026-07-28 spec) accepted without a handshake
- ✅ `ping` supported; protocol version echoed from client
- ⛔ SSE/streaming transports (roadmap; stdio covers Claude Code/Desktop, Cursor)

## 3. Tool definitions & schemas
- ✅ Explicit JSON Schema per tool (types, descriptions, enums)
- ✅ No opaque wrapper objects — parameters are plain fields
- ✅ operationId-based names with collision suffixing (`_2`, `_3` …)
- ✅ Meta-tool names reserved server-side; spec collisions suffix, never shadow
- ✅ Tool annotations (readOnly/destructive/idempotent/openWorld + title)
  derived honestly from HTTP methods — client approval UX rides on them
- ✅ Enum'd parameters from real specs surface as proper enums
- ✅ `$ref`-based parameter/body schemas resolve against the full spec
- ✅ Unresolvable schema → string fallback with a note (never a crash)

## 4. Response management & performance
- ✅ Responses truncated at ~40k chars (context-blast guard)
- ✅ Structured output: outputSchema only when the spec documents 2xx JSON;
  structuredContent delivered with back-compat text; non-JSON body → tool error
- ✅ Opt-in GET caching (`--cache-ttl`, bounded, GET+200), safe retries
  (idempotent-only, 502/503/504, capped), XML→JSON conversion, strict
  argument mode, legacy JSON-RPC batch tolerance
- ✅ Remediation-grade errors: validation details, auth hints, Retry-After,
  closest-path suggestions on 404
- ✅ Lazy mode (`--lazy`): 95.5% listing reduction measured on api.weather.gov
- ✅ Dry-run preview (`--enable-preview`): exact request, masked credentials,
  zero network
- ✅ HTTP errors returned as tool errors — agent-visible, in plain English
- ✅ 500-operation specs convert in milliseconds (benchmarked in tests)
- ⛔ Response streaming/continuation (transport-level; see §2)

## 5. Logging & stdio hygiene *(critical for stdio servers)*
- ✅ Product code writes **nothing** to stdout — the JSON-RPC stream is
  reserved; two regression tests pin this
- ✅ Optional `MCPIFY_DEBUG=1` logging to **stderr**, uppercase levels,
  query strings stripped (query-auth credentials never logged)

## 6. Security & authorization
- ✅ Credentials only from environment variables (`--auth-env`)
- ✅ `Authorization` header stripped from advertised tool schemas
- ✅ `doctor` warns when the spec declares security schemes but the serve
  command passes no auth
- ✅ Policy layer: `--read-only` + `--allow`/`--deny` (deny wins)
- ✅ CodeQL static analysis on every push; Dependabot weekly
- ✅ Secrets never appear in logs (test-pinned)

## 7. Documentation & configuration
- ✅ README (EN + TR), USAGE guide (auth patterns, scoping, troubleshooting,
  FAQ), ARCHITECTURE, DEPLOYMENT-grade config notes, CHANGELOG, CONTRIBUTING,
  SECURITY policy
- ✅ Client config examples: Claude Desktop, Claude Code, Cursor, VS Code
- ✅ Landing page with animated demo

## 8. Architecture pattern
- ✅ Documented as a **Domain Adapter** (single-upstream API gateway shape);
  explicitly *not* a Proxy Aggregator / Stateful Session / Orchestrator

## 9. Error handling & resilience
- ✅ HTTP ≥400 returned as structured tool errors (never exceptions to the client)
- ✅ Unknown tool → `-32601`; unknown arguments → clear tool error
- ✅ Startup validation: bad specs, relative URLs, default-less server
  variables fail fast with actionable messages
- ⛔ Retry / rate-limit / circuit-breaker: deliberate omissions —
  non-idempotent retries belong at the gateway (rationale in USAGE.md)

## 10. Test & validation
- ✅ **162 passing tests across eleven suites** (163 collected; one
  live-integration test auto-skips offline) — full matrix in the README
- ✅ Quality gates on every push: ruff, mypy `strict`, CodeQL,
  Python 3.10–3.12 × Linux + Windows
- ✅ Hostile corpus (11 scenarios) derived from published failure studies
  (arXiv 2507.16044; TrueFoundry & DigitalAPI conversion guides)
- ✅ Live integration: the real api.weather.gov document loads in CI
  (69 tools, 16 enum'd parameters) — the scenario that found our
  crash-class bug
- ✅ Regression policy: every field bug becomes a pinned test before the
  fix ships
- ✅ Multi-client by protocol: standard stdio MCP, verified with a real
  JSON-RPC handshake in tests (Claude Desktop/Code, Cursor compatible)

---
*Audited 2026-08-29 against v1.3.0. Re-run on every release.*
