# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.12.1] - 2026-08-30

### Fixed — found by running mcpify against live APIs
- **Nested truncation for API envelopes:** an oversized envelope like
  weather.gov's `alerts_active` — small metadata keys + one giant
  `features` list — used to lose the payload entirely (metadata + an
  "omitted keys" marker only). Object values that alone exceed the
  remaining budget are now truncated IN PLACE: arrays keep their first
  items plus an explicit `mcpify_item_truncated`/`omitted` marker,
  nested objects keep fitting keys. The agent now receives the first
  ~190 real alerts in valid JSON instead of 356 chars of metadata.
  Fitters measure the `{key: value}` wrapper (nesting indentation
  included), so the 40k budget holds honestly.
- 2 new tests (412 total, twenty-five suites).

## [1.12.0] - 2026-08-30

### Added — the least-privilege release (answers to a reviewer's three sharp points)
- **Read/write credential split (`--write-auth-env`):** reads go out on the
  primary `--auth-env` credential, non-GET calls carry a dedicated write
  credential — the blast radius of a read call is the read key's, not the
  write key's. Style/name are inherited from `--auth-style`/`--auth-name`
  (overridable with `--write-auth-style`/`--write-auth-name`); works per API
  via `write-auth-env` in `[apis.NAME]`/`[serve]`. Static credentials only —
  combining with OAuth2 is refused with a clear message until a second token
  flow is designed. This turns `--read-only` from an MCP-layer suggestion
  into a credential-level property.
- **Structure-aware truncation:** oversized JSON responses are no longer cut
  mid-document (which handed models a fragment that looked complete but
  could not parse). Arrays keep their first items in a wrapper object;
  objects keep the top-level keys that fit; both carry explicit
  `truncated: true` / `mcpify_omitted_keys` markers and the serialized
  result is measured honestly against the 40k budget. Non-JSON bodies keep
  the character cut.
- **Doctor prompt-hygiene audit:** flags operations whose summary/description
  carries instruction-like text ("ignore previous instructions", "you must",
  "system prompt", ...) and descriptions over 1200 chars — spec authors
  become prompt authors, so tool text deserves a lint pass.
- **`[tool-text]` overrides in `.mcpify.toml`:** the operator (not the spec
  author) gets the last word on model-facing descriptions, per final tool
  name, without touching the spec. Unknown tool names warn; the validator
  rejects malformed sections; `mcpify config-schema` covers the section.
- **`mcpify list` now reads the config file** (like serve) — previews honor
  `[serve]` settings and show overrides; the `spec` positional is optional
  when the config provides it.
- 21 new tests (410 total, twenty-five suites).

## [1.11.0] - 2026-08-30

### Added — the operator & governance release (core stays zero-dependency)
- **`mcpify diff OLD NEW` — spec upgrades from the tool-surface view:**
  added/removed/changed operations with a per-change **breaking** verdict
  (operation removed, required parameter added or became required, request
  body became required). Deprecations and `operationId` renames are
  surfaced as non-breaking warnings. `migration guide` lines tell agent
  consumers exactly what to change; `--json` for machines and
  `--fail-on-breaking` as a CI gate. Exit contract: 0 clean, 1 breaking,
  2 usage error.
- **Audit trail (`--audit-log FILE`):** one JSON line per real API call —
  timestamp, tool, API label, HTTP status, ok/error outcome, latency and a
  12-hex **argument fingerprint** (sha256 of the sorted arguments — repeat
  calls correlate without storing end-user content). Fail-safe: an
  unwritable file warns once and never takes the serving path down.
- **Per-token tool RBAC (`--http-token-file FILE`):** a TOML file of
  `[tokens.NAME]` sections, each with its own bearer token plus `allow` /
  `deny` regex lists over tool names (deny wins; at least one `allow`
  required; duplicate tokens rejected). `tools/list` is filtered per
  caller and scoped-out `tools/call` requests are refused with a clear
  error. This is deliberately coarse, name-based access control — not
  SSO/identity (SELF-HOSTING.md says so).
- **ETag-aware caching:** stored responses keep their `ETag`; on expiry a
  conditional `If-None-Match` revalidation gets a `304` and serves the
  stored body (counted as a cache hit). New `mcpify_cache_invalidate`
  meta tool clears everything or a path pattern; `--cache-warm` pre-calls
  argument-free GET tools in background threads right after startup.
- **Plugin hooks (`--plugin FILE`, repeatable):** load a Python module
  that provides an `AUTH` object (replaces the spec-derived credential
  logic) and/or `on_request` / `on_result` hooks that see every upstream
  request and raw result (add headers, redact, ship events). A broken
  hook never breaks a request.
- **`.mcpify.toml` JSON Schema (`mcpify config-schema`):** the shipped,
  tested schema mirrors the exact accepted keys for `[serve]`, `[envs.*]`
  and `[apis.*]` — wire it into your editor.
- **External `$ref` bundling:** `$ref` targets in other files or URLs are
  inlined at load (nested refs resolve relative to their own file;
  component-only target files accepted). Cycles and unreadable targets
  are skipped, never fatal.
- **OpenTelemetry tracing (`--otel [ENDPOINT]`, optional extra):** one
  span per upstream API call (tool, api, status, latency) exported over
  OTLP/HTTP. Requires `pip install 'mcpify[otel]'`; without it you get an
  actionable error, and the core still has zero dependencies.
- **`mcpify ui` actually serves now:** v1.10.0 shipped the dashboard but
  the CLI dispatch branch was missing — `mcpify ui` parsed arguments and
  silently exited. Fixed and pinned by regression tests.
- 37 new tests (389 total, twenty-four suites).

## [1.10.0] - 2026-08-30

### Added — the operations release (all zero-dependency)
- **`mcpify ui` — the local operations dashboard:** one inline HTML
  page served by stdlib `http.server` — a live tool explorer (method,
  path, annotations, full JSON schema), masked dry-run request
  previews, per-API health probes with latency sparklines, a masked
  log tail, and a form that writes a **validated** `.mcpify.toml`
  (unknown keys are rejected with 400). Binds 127.0.0.1 by default;
  `--http-token` (or `?token=` for browsers) protects non-loopback
  use. Real execution stays in `mcpify try` — the dashboard is a
  dry-run cockpit.
- **Prometheus metrics (`--metrics [HOST:]PORT`):** `/metrics` in
  Prometheus text format — `mcpify_tool_calls_total{tool,api,outcome}`,
  `mcpify_tool_latency_seconds` histogram, `mcpify_cache_requests_total`
  hit/miss, `mcpify_api_health` per-API gauges, uptime/tool-count
  gauges. Recording is opt-in and a single boolean check when off.
  Example per-API alerting rules in USAGE.md.
- **`mcpify mock`:** a fake API generated from the spec — schema-shaped
  JSON (examples > example > default > const/enum > format > type
  heuristics, `$ref` resolved, required-only objects), exact + `{param}`
  template routing, 404 with the known-route list, optional
  `--delay-ms` for latency testing.
- **`--reload` (serve/ui):** watches the spec file(s) in a daemon
  thread and hot-swaps the tool surface in place — stdio loops and
  HTTP handler closures keep working. A broken half-saved spec keeps
  the previous surface and says so on stderr; URLs are skipped.
- 25 new tests (352 total, twenty-one suites) covering all four
  features end-to-end against real local servers.

## [1.9.1] - 2026-08-30

### Fixed — found by the strict-audit round
- **`mcpify init` showed no prompts** in an interactive terminal: the
  wizard read answers without ever printing its questions (a blank
  terminal). Prompts are now displayed; a regression test pins every
  question text.
- **Config root validation:** a `.mcpify.json` whose root is not a
  table (e.g. an array) now fails with a clear
  `root must be a table, got list` message instead of a distant
  AttributeError.
- **OAuth2 token header under `python -O`:** an internal `assert`
  guarding the fetched token is now an explicit error (asserts vanish
  under optimization flags).
- **XML hardening:** documents declaring a DTD/`<!ENTITY` are never
  parsed (billion-laughs class); the raw body is returned untouched
  and `--format xml` states why. Pinned by a regression test.
- **`AggregatedServer` copies the caller's entries list** — later
  caller mutations can no longer corrupt a running server.

### Changed — audit tooling at maximum strictness
- mypy moved to `strict = true` + `warn_unreachable` (127 new errors
  fixed: full generics, `Any`-returns, a hostile-input guard mypy
  mistook for dead code now typed honestly).
- ruff: `target-version` corrected py39→py310 and the rule set expanded
  (bugbear-bear, security `S`, `ARG`, `PERF`, `FURB`, `RUF`, …) — every
  finding fixed or justified inline (`noqa` with a reason).
- Test hardening from a hand-rolled mutation check (11 mutants, all
  killed): exact collision-suffix names, label-only lazy search, the
  health probe's documented concurrency, entries-aliasing protection.
- 3.10 fallback TOML parser parity-tested against `tomllib` behavior;
  wizard number-validation branches pinned; structured API `errors[]`
  messages now flow into remediation hints (tested).
- 10 new tests (327 total, twenty suites); fixture sockets closed
  deterministically; `tests/hostile_corpus.py` C2 check actually
  writes its spec now (it had been checking a missing file since
  v1.0.x).

## [1.9.0] - 2026-08-30

### Added — multi-API aggregation, the gateway feature, for free
- **`[apis.NAME]` config sections:** list multiple OpenAPI documents
  (local files or URLs) in `.mcpify.toml` and a single
  `mcpify serve` process fronts them all as one MCP tool surface —
  stdio, HTTP, or both readers of `mcpify try`. The composition layer
  hosted gateways bill $9–$229/month for, in a config file.
- **Per-API everything:** credentials (`auth-env`, OAuth2), policies
  (`read-only`, `include`/`exclude`/`tag`, `allow`/`deny`), caching,
  retries and timeouts are resolved per API with precedence
  CLI > `[apis.NAME]` > `[serve]` > defaults. One API's `read-only`
  never leaks into another's tools.
- **Collision-safe naming:** when two APIs expose the same tool name,
  both sides are renamed with their label (`catalog_list_pets`,
  `crm_list_pets`) — nothing silently wins; non-conflicting names stay
  untouched and built-in `mcpify_*` names are reserved.
- **Aggregated health:** `mcpify_health` probes every API concurrently
  (up to 8 at once) and returns one report with per-API status and
  latency; unreachable APIs are named in a remediation hint and the
  tool call surfaces an error. `mcpify status` prints one line per API
  and exits 2 if any is down; `--json` gives the machine-readable form.
- **`mcpify try` and `--env` work across the whole surface:** the REPL
  banner shows the API/tool counts, `--env staging` (or `default-env`)
  applies to every API from the `[serve]` layer, and passing a
  positional spec together with `[apis.*]` sections is rejected with a
  clear error instead of a silent precedence surprise.
- 23 new tests (317 total, nineteen suites): merge/rename rules,
  per-API auth & cache isolation, lazy search across APIs (including
  by API label), preview routing, health aggregation, status exit
  codes, and CLI wiring.

## [1.8.0] - 2026-08-30

### Added — free what others charge for
- **Auth auto-detection:** the spec's `security` declarations
  (components, operation-level, and Swagger 2.0 `securityDefinitions`)
  now configure `--auth-env` for you — `mcpify serve spec.json
  --auth-env VAR` alone picks bearer, HTTP basic, header or query style
  with the right name. Serving a secured spec without a credential
  prints the exact flags to run; `doctor` shows them too.
- **HTTP Basic auth:** `--auth-style basic` with the env variable
  holding `username:password` produces the `Authorization: Basic …`
  header (covers a whole class of internal APIs).
- **`--wait-on-429 SEC`:** opt-in rate-limit courtesy — on 429 for an
  idempotent call, honor `Retry-After` (or the retry delay) ONCE when
  the wait is within the cap; longer waits return the 429 untouched;
  POST/PATCH are never auto-waited.
- **Self-hosting pack:** `deploy/docker-compose.yml` (mcpify + Caddy,
  automatic HTTPS, double-layer bearer), `deploy/Caddyfile`,
  `deploy/mcpify.service` (hardened systemd unit) and
  `docs/SELF-HOSTING.md` — a free alternative to hosted-MCP plans that
  bill $9–$229/month.

### Tests
- 263 → **294 passing** (eighteen suites): auth detection & Basic (22),
  Retry-After handling (9).

## [1.7.0] - 2026-08-30

### Added — server selection (#13)
- `--server INDEX|NAME` on `serve`, `try` and `status`: pick among the
  spec's declared `servers[]` entries by 1-based index (`--server 2`) or
  name (`--server staging` — matched against each entry's description,
  exact or whole-word and case-insensitive, or its URL as a substring).
- Errors list every declared server with index, URL and description so
  the fix is obvious; `--base-url` still wins over `--server`, which
  wins over the `servers[0]` default; server variables without defaults
  keep failing fast with the existing actionable message.
- `mcpify doctor` now hints at `--server` when a spec declares more than
  one server; the `server` key is accepted in `.mcpify.toml`/yaml/json.
- Tests: 246 → **263 passing** (new suite: server selection, 17).

## [1.6.2] - 2026-08-30

### Fixed
- `mcpify output-server`: the generated script's docstring is now a raw
  string, so Windows paths (`C:\Users\...`) inside it can no longer
  produce a `\U` unicode-escape SyntaxError. Found by Windows CI; pinned
  by a regression test that generates from a backslash-carrying path on
  both platforms, plus a generation-time refusal for triple quotes.

## [1.6.1] - 2026-08-30

### Fixed
- Docker image build: `.dockerignore` excluded `examples/`, but the
  default CMD serves the bundled petstore spec — the v1.6.0 tag's image
  build failed with "…/examples/petstore.json: not found" (the new
  Dockerfile had not been tagged before, so the bug shipped untested).
  `examples/` is back in the build context; `docker run` without args
  starts the live demo server again.

## [1.6.0] - 2026-08-30

### Added — two transports, terminal REPL, OAuth2, shareable servers
- **HTTP transport:** `mcpify serve SPEC --http [HOST:]PORT` exposes the
  identical tool surface over MCP Streamable HTTP (stdlib `http.server`,
  stateless per the current spec). Optional bearer auth via
  `--http-token` / `MCPIFY_HTTP_TOKEN`, 405/411/413/415 transport-error
  ladder, JSON-RPC parse/batch rejections, 10 MB body cap, unauthenticated
  non-loopback bind warning.
- **`mcpify try`:** interactive terminal REPL to call the generated tools
  without an agent client — typed field-by-field prompts, `:raw`, `:info`,
  graceful EOF/Ctrl+C, identical execution path to `tools/call`.
- **OAuth2 client-credentials (RFC 6749 §4.4):** `--oauth2-token-url` +
  env-based client credentials; in-memory token cache with expiry margin,
  Basic or body client auth, public-client support, and a one-shot 401
  self-heal on the same call.
- **`mcpify output-server`:** bakes a serve command (and, for local
  specs, the spec itself) into a shareable standalone script; refuses
  silent overwrites, validates baked flags against the real serve parser,
  warns when `--http-token` would embed a secret.
- **Config & wizard:** OAuth2 and HTTP keys are accepted in
  `.mcpify.toml`/`yaml`/`json` (now actually applied when the CLI leaves
  a choice flag at its default), and `mcpify init` offers the OAuth2 flow
  as auth option 5.

### Tests
- 162 → **245 passing** (16 suites): new suites for the HTTP transport
  (19), OAuth2 flow with a fake clock (18), the REPL driven over piped
  stdin (26), `output-server` including a real subprocess E2E (10), and
  CLI connectivity glue (10).

## [1.5.3] - 2026-08-29

### Fixed
- `server.json` description shortened to fit the official MCP Registry's
  100-character limit (v1.5.2's longer positioning text had failed the
  Registry publish; PyPI keeps the full description).

## [1.5.2] - 2026-08-29

### Changed
- Positioning across README, metadata and docs: focused,
  production-ready, CLI-first — "Focused doesn't mean small".

## [1.5.1] - 2026-08-29

### Fixed
- Windows/TOML hardening: literal strings for Windows paths, single-quote
  support in the Python 3.10 subset parser, temp-file descriptor closed
  before reuse in `status` (WinError 32).

## [1.5.0] - 2026-08-29

### Added — operational layer
- Config files (`.mcpify.toml` / `.yaml` / `.json`) with per-environment
  `[envs.NAME]` sections and CLI > env > serve > defaults precedence;
  `mcpify init` wizard; verbose + log-file logging with masked
  credentials; GET response cache (`--cache-ttl`); safe idempotent-only
  retries (`--retry`, 502/503/504, hard cap 5); origin auto-discovery via
  well-known paths; `--strict` argument mode; XML→JSON conversion
  (`--format`); legacy batch tolerance with concurrent `tools/call`;
  `mcpify_health` tool and `mcpify status` command.

## [1.4.0] - 2026-08-29

### Added
- Stateless MCP compatibility: requests carrying
  `_meta["io.modelcontextprotocol/protocolVersion"]` (2026-07-28 spec)
  are accepted without a handshake, while the legacy 2025-06-18
  initialize flow keeps working unchanged; protocol-version compat test
  suite.

## [1.3.0] - 2026-08-29

### Added
- `mcpify list` marks deprecated OpenAPI operations and includes a
  `deprecated` flag in JSON output. **Thanks @kkkhs!** (#15)

### Planned
- See the Roadmap section in the README.

## [1.2.0] - 2026-08-29

### Added — agent-grade surface
- Tool annotations derived from HTTP semantics (readOnly/destructive/
  idempotent/openWorld + title from summary) for client approval UX.
- Structured output: tools with a documented 2xx JSON body declare
  `outputSchema` and return `structuredContent` + back-compat text.
- Remediation-grade tool errors: API validation details, auth hints,
  `Retry-After` on 429, closest-path suggestions on 404, upstream blame
  on 5xx.
- `--lazy` mode: search/get-schema/call meta tools replace the full
  listing (95.5% listing reduction measured on api.weather.gov, 69 tools).
- `--enable-preview`: `mcpify_preview_request` dry-run tool — exact
  request, masked credentials, nothing sent.
- Meta-tool names reserved during generation; spec collisions get the
  `_2` suffix instead of shadowing.

## [1.1.0] - 2026-08-29

### Added
- `doctor --json` output for CI-friendly spec checks with severity exit
  codes (0 clean / 1 warnings) — single JSON object on stdout, nothing on
  stderr. **First community contribution — thanks @mikemikimike!** (#14)

## [1.0.4] - 2026-08-29

### Added
- MCP lifecycle enforcement: `tools/*` before `initialize` returns -32002.
- `MCPIFY_DEBUG=1` stderr logging (INFO/WARNING/ERROR, query strings stripped).
- `doctor` warning for specs declaring securitySchemes without `--auth-env`.
- VS Code client example; architecture-pattern section; Windows CI matrix.
- Engineering audit checklist: docs/AUDIT-CHECKLIST.md.

### Fixed
- `serverInfo.version` now derives from package metadata (was hardcoded).

## [1.0.3] - 2026-08-29

### Fixed
- Real-world spec hardening (research-sourced hostile corpus):
  circular `$ref` bodies degrade instead of crashing; multipart bodies
  exposed as raw string args; `allOf` merged; server-variable substitution;
  relative server URLs fail fast; HEAD/OPTIONS/TRACE skipped; responses
  truncated at ~40k chars; doctor warnings (deprecated ops, large surface).

## [1.0.2] - 2026-08-29

### Fixed
- Parameter schemas referenced via `$ref` resolve against the full spec
  (found against the live api.weather.gov document); unresolvable
  parameters degrade to strings instead of crashing the listing.

### Added
- Published to the official MCP Registry (io.github.furkan708/mcpify).
- `--allow`/`--deny` policy layer over `--read-only`.

### Planned
- See the Roadmap section in the README.

## [0.1.0] - 2026-08-28

### Added
- Initial public release.
- Core functionality: Turn any OpenAPI REST API into an MCP server.
- Comprehensive test suite with CI on every push.
- HTML/terminal previews and developer tooling.
