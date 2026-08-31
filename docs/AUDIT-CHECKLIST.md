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
- ✅ `--redact` masks secret-named fields at every response level (success and error bodies); `--fields` projects; `--rate-limit` caps requests/second upstream
- ✅ Pre-flight: `doctor --probe` (credential-aware, `--fail-on-http-error` CI gate), `init --probe`, and `diff --probe` (adopt-time live check, exit 2); policy visible in `status` output and Prometheus counters
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
- ✅ Retries (502/503/504, idempotent methods only), opt-in 429
  Retry-After courtesy, per-API in aggregation
- ⛔ Circuit-breaker / adaptive throttling: deliberate omission —
  belongs above this layer (rationale in USAGE.md)

## 10. Test & validation
- ✅ **516 passing tests across twenty-eight suites** (518 collected; one
  live-integration test auto-skips offline, the OTel positive test skips
  without the optional extra) — full matrix in the README
- ✅ Quality gates on every push: ruff, mypy `strict`, CodeQL,
  Python 3.10–3.12 × Linux + Windows
- ✅ Hostile corpus (11 scenarios) derived from published failure studies
  (arXiv 2507.16044; TrueFoundry & DigitalAPI conversion guides)
- ✅ Live integration: the real api.weather.gov document loads in CI
  (69 tools, 16 enum'd parameters) — the scenario that found our
  crash-class bug
- ✅ Regression policy: every field bug becomes a pinned test before the
  fix ships (latest: the `.dockerignore`/Dockerfile conflict and the
  Windows `\U` docstring escape, both found at release time in v1.6.x)
- ✅ Multi-client by protocol: stdio **and** Streamable HTTP transports,
  each verified with real JSON-RPC handshakes in tests (Claude
  Desktop/Code, Cursor compatible; HTTP suites cover the 401/405/411/413/415
  error ladder and bearer enforcement)
- ✅ OAuth2 client-credentials exercised end-to-end against real local
  token/API servers: fetch, cache, expiry (fake clock), public clients,
  every failure mode, and the one-shot 401 self-heal
- ✅ Multi-API aggregation pinned end-to-end against real local APIs:
  merge/rename rules, per-API auth & cache isolation, concurrent health
  probes (dead API named in the hint), lazy search across APIs, status
  exit codes, and the reject-both-inputs guard
- ✅ v1.9.1 audit round: mypy `strict = true` + `warn_unreachable` clean;
  ruff expanded to 20+ rule families (S/ARG/PERF/FURB/RUF…) with every
  finding fixed or justified inline; hand-rolled mutation check on the
  aggregation layer — 11 mutants, all killed (suffix counter, owner
  routing, health flags, search-by-label, entries aliasing, probe
  concurrency); coverage **91.3%** overall, `aggregate.py` 100%;
  bandit findings triaged (all deliberate, justified inline); sdist +
  wheel `twine check` PASSED; 3.10 fallback TOML parser parity-tested

---
*Audited 2026-08-31 against v1.14.0. Re-run on every release.*
