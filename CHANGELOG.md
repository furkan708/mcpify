# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
