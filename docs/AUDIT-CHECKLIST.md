# Engineering Audit Checklist — mcpify

This project is audited against a 10-category checklist of MCP best
practices and common production failure modes. Every release re-runs the
full checklist. Status below reflects **v1.0.4**.

Legend: ✅ verified · 🔧 fixed during audit · ⛔ deliberate limitation
(documented in [USAGE.md](USAGE.md) FAQ)

## 1. Server setup & dependencies
- ✅ Zero runtime dependencies; dev floors pinned with sane minimums
- ✅ Version surfaced from package metadata (no drift)
- ✅ Installs via `pipx install mcpify-openapi` (PATH-ready command)
- ✅ Cross-platform CI: **Linux + Windows** matrices
- ✅ No dependency conflicts possible (stdlib only)

## 2. Transport & connection
- ✅ stdio, newline-delimited JSON-RPC 2.0 (MCP stdio transport)
- ✅ MCP lifecycle enforced: `tools/*` before `initialize` → error `-32002`
- ✅ `ping` supported; protocol version echoed from client
- ⛔ SSE/streaming transports (roadmap; stdio covers Claude Code/Desktop, Cursor)

## 3. Tool definitions & schemas
- ✅ Explicit JSON Schema per tool (types, descriptions, enums)
- ✅ No opaque wrapper objects — parameters are plain fields
- ✅ operationId-based names with collision suffixing (`_2`, `_3` …)
- ✅ Enum'd parameters from real specs surface as proper enums
- ✅ `$ref`-based parameter/body schemas resolve against the full spec
- ✅ Unresolvable schema → string fallback with a note (never a crash)

## 4. Response management & performance
- ✅ Responses truncated at ~40k chars (context-blast guard)
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
- ✅ **82 tests**: unit, hostile-spec corpus, MCP protocol end-to-end over
  stdio against a real local HTTP API
- ✅ **Live integration**: api.weather.gov OpenAPI document loaded in CI
  (69 tools, 16 enum'd parameters) — the scenario that found our last
  crash-class bug
- ✅ Hostile corpus (12 scenarios) derived from published failure studies
  (arXiv 2507.16044; TrueFoundry & DigitalAPI conversion guides)
- ✅ Multi-client by protocol: speaks standard stdio MCP — verified with
  real JSON-RPC handshake in tests (Claude Desktop/Code, Cursor compatible)

---
*Audited 2026-08-29 against v1.0.4. Re-run on every release.*
