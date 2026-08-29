# mcpify — Architecture

A map of the codebase for contributors and reviewers.

## Module overview

```
mcpify/
├── spec.py        # OpenAPI loading + $ref resolution + operation walking
├── tools.py       # operation → MCP tool, argument → HTTP request, auth
├── http_client.py # execution (urllib) + result formatting
├── api_server.py  # MCP protocol server (stdio, JSON-RPC 2.0)
└── cli.py         # list / serve / doctor commands
```

Dependency direction is strictly one-way:

```
cli → api_server → tools → spec
                  → http_client
```

`spec` and `tools` are pure (no I/O except spec loading); `http_client` is
the only module that touches the network; `api_server` is the only module
that speaks MCP. This split is what makes the e2e tests fast and reliable
(they boot a real `http.server` and drive the full protocol).

## Request lifecycle

```
Agent (Claude/Cursor)
   │  tools/call {name: "get_pet", arguments: {petId: 9}}
   ▼
api_server.handle_message()            JSON-RPC → dispatch
   ▼
tools.build_request()                  arguments → {method, url, headers, body}
   │  - substitutes {petId} in the path template (URL-encoded)
   │  - collects query/header params (namespaced for headers)
   │  - validates body presence + rejects unknown arguments
   │  - injects auth from AuthConfig (env var → header/query)
   ▼
http_client.execute()                  urllib, 30s timeout
   │  - HTTP 4xx/5xx are RESULTS, not exceptions
   ▼
format_result()                        → (text, is_error)
   ▼
MCP tool response                      content[0].text, isError flag
```

## Design decisions

**1. HTTP errors are tool results, not protocol errors.**
An API returning 404 is information the agent can act on (retry, correct
arguments, tell the user). Protocol errors (`-32601` unknown tool) are
reserved for actual protocol violations.

**2. `noEmit`-style purity for `spec`/`tools`.**
`spec_to_tools()` is a pure function `dict → list[dict]`. The CLI's
`list` command and the server both consume it, so preview and serving can
never diverge.

**3. `Authorization` parameters are stripped from schemas.**
Even if a spec declares an `Authorization` header parameter, it is never
advertised to the agent — credentials can only come from `AuthConfig`
(environment). This removes the "agent leaks key into a tool argument"
failure class entirely.

**4. Minimal YAML.**
JSON parsing is stdlib; YAML requires PyYAML as an optional extra. The
core stays dependency-free while remaining spec-format pragmatic.

**5. `body` as a single object argument.**
Flattening body fields into top-level arguments collides with
path/query names and produces ambiguous schemas. One `body` object is
predictable, matches OpenAPI 1:1, and renders well in every MCP client.

## MCP surface

| Method | Behavior |
| ------ | -------- |
| `initialize` | Echoes requested protocol version; advertises `tools` capability |
| `notifications/*` | Ignored (never answered) |
| `ping` | Empty result |
| `tools/list` | Spec-derived descriptors (private `_meta` stripped) |
| `tools/call` | Argument validation → HTTP request → formatted result |
| anything else | `-32601 method not found` |

## Testing strategy

| Layer | File | Approach |
| ----- | ---- | -------- |
| `$ref` resolution, walking | `test_spec.py` | Pure fixtures |
| Operation → tool | `test_tools.py` | Pure fixtures, edge cases |
| Argument → request | `test_tools.py` | Assertions on URL/headers/body |
| Full protocol + real HTTP | `test_e2e.py` | Boots `http.server`, drives stdio loop |
| CLI | `test_cli.py` | `capsys`, temp specs, exit codes |

The e2e suite is the safety net for refactors: it proves that a change in
schema resolution still produces wire-correct HTTP requests.


## Design pattern

mcpify is a **Domain Adapter** (API-gateway shape): one upstream service, one
generated surface, no aggregation. It translates between two contracts — the
OpenAPI document and the MCP tool schema — at process startup, then stays a
stateless passthrough per call.

It deliberately is *not*:

- a **Proxy Aggregator** (it fronts exactly one spec; run one process per API
  and let the client connect to several),
- a **Stateful Session** server (no per-conversation memory beyond the spec
  loaded at boot),
- a **Tool Orchestrator** (it does not chain tools; the agent does).


## Structured results & the lazy surface

The tool pipeline is: `spec → tools.py (descriptor + annotations +
outputSchema) → api_server.run_tool → http_client.execute → payload`.
Three decisions shape v1.2.0:

1. **Promises are opt-in.** `outputSchema` is declared only when the spec
   documents a 2xx JSON body; `structuredContent` is delivered with the
   spec-mandated back-compat text block; error results stay text-only.
2. **Errors teach.** `http_client.remediation` turns status + API payload
   into corrective lines (validation, auth, retry-after, closest paths).
3. **Names are reserved.** Meta tools (`mcpify_*`) are claimed in
   `spec_to_tools` before operation IDs, so a colliding spec operation
   suffixes (`_2`) instead of shadowing the search/call surface.

## Scope statement

mcpify is intentionally focused: one job (OpenAPI → MCP), one interface
(a single CLI over stdio), zero runtime dependencies. Focused is a
scope decision, not a size claim — the operational layer (config
files, caching, retries, health, batch tolerance) exists to make that
one job production-ready, and everything outside the job (gateways,
GUIs, remote transports) is deliberately someone else's layer.
