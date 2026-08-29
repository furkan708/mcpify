# Changelog

All notable changes to this project are documented in this file.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
