# mcpify — Deep Usage Guide

Everything beyond the README quick start: authentication patterns, scoping,
deployment, troubleshooting, and frequently asked questions.

## Table of contents

1. [Choosing a spec source](#1-choosing-a-spec-source)
2. [Authentication patterns](#2-authentication-patterns)
3. [Scoping which operations are exposed](#3-scoping-which-operations-are-exposed)
4. [Connecting different MCP clients](#4-connecting-different-mcp-clients)
5. [Running behind Docker / production](#5-running-behind-docker--production)
6. [How tool naming works](#6-how-tool-naming-works)
7. [Troubleshooting](#7-troubleshooting)
8. [FAQ](#8-faq)

---

## 1. Choosing a spec source

`mcpify list` and `mcpify serve` accept both a local file and an https URL:

```bash
mcpify list ./openapi.json
mcpify list https://api.example.com/openapi.json
```

- **JSON specs** work out of the box.
- **YAML specs** need the optional extra: `pip install 'mcpify[yaml]'`.
- **Swagger 2.x** documents are accepted (the `swagger` root field is
  recognized), but OpenAPI 3.x is the well-tested happy path.
- Specs with **external `$ref` documents** are not resolved over the
  network. Bundle the spec first (most generators have a "bundle" option).

## 2. Authentication patterns

Credentials are **never** read from the spec, a CLI flag, or the model's
context — only from an environment variable at call time.

### Bearer token (most common)

```bash
export ACME_TOKEN="ey..."
mcpify serve acme.json --base-url https://api.acme.com \
  --auth-env ACME_TOKEN --auth-style bearer
```

Every outgoing request carries `Authorization: Bearer ey...`.

### Custom header (API keys)

```bash
mcpify serve acme.json --auth-env ACME_KEY --auth-style header --auth-name X-API-Key
# -> X-API-Key: <value>
```

### Query parameter

```bash
mcpify serve acme.json --auth-env ACME_KEY --auth-style query --auth-name api_key
# -> https://api.acme.com/v1/pets?api_key=<value>
```

### No auth / public APIs

Omit all auth flags. Nothing is injected.

### What happens when the variable is missing

The tool call fails **before** any network request with:
`environment variable 'ACME_TOKEN' is not set (required for API
authentication)`. The agent sees this as a tool error and can tell you —
it never sends an unauthenticated request blindly.

> **Rotation tip:** because the variable is read on every call, rotating
> the secret requires no server restart — export the new value in the
> environment where the server process runs.

## 3. Scoping which operations are exposed

Giving an agent *everything* is rarely what you want. Four filters combine:

| Flag | Effect |
| ---- | ------ |
| `--read-only` | Only `GET` operations |
| `--tag payments` | Only operations with that OpenAPI tag |
| `--include /v1/orders` | Only this path prefix (repeatable) |
| `--exclude /admin` | Everything except this prefix (repeatable) |

`--include` and `--exclude` combine; excludes win.

```bash
# A safe "reporting" server: only reads, no admin paths
mcpify serve acme.json --read-only --exclude /admin --exclude /internal
```

Preview the result first with the same filters:

```bash
mcpify list acme.json --read-only --exclude /admin
```

The `Authorization` header parameter is always stripped from advertised
schemas — clients cannot inject their own credentials through tool arguments.

## 4. Connecting different MCP clients

### Claude Code (CLI)

```bash
claude mcp add acme -- mcpify serve acme.json --auth-env ACME_TOKEN --read-only
```

### Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "acme": {
      "command": "mcpify",
      "args": ["serve", "/absolute/path/acme.json", "--auth-env", "ACME_TOKEN"],
      "env": { "ACME_TOKEN": "..." }
    }
  }
}
```

### Cursor / Windsurf / any stdio client

Same shape — `command` + `args` pointing at `mcpify serve ...`. The server
speaks the standard MCP stdio transport (newline-delimited JSON-RPC 2.0).

### One API, many scopes

Register the same spec twice with different flags:

```json
{
  "mcpServers": {
    "acme-readonly": { "command": "mcpify", "args": ["serve", "acme.json", "--read-only"] },
    "acme-payments": { "command": "mcpify", "args": ["serve", "acme.json", "--tag", "payments"] }
  }
}
```

## 5. Running behind Docker / production

```dockerfile
FROM python:3.12-slim
RUN pip install git+https://github.com/furkan708/mcpify.git
ENTRYPOINT ["mcpify"]
```

```bash
docker run -i --rm \
  -e ACME_TOKEN="$ACME_TOKEN" \
  -v "$PWD/specs:/specs:ro" \
  my-mcpify serve /specs/acme.json --auth-env ACME_TOKEN
```

Notes for production:

- stdio MCP servers are meant to be **spawned by the client**, not exposed
  over the network. Do not wrap them in a TCP listener without adding auth.
- `--timeout` (seconds, default 30) bounds each API call; tune for slow APIs.
- The server keeps zero state between calls — restarts are free.

## 6. How tool naming works

- `operationId` is preferred, slugified (`getPetById` → `get_pet_by_id`).
- Without an `operationId`, the name falls back to `method_path`
  (`GET /pets/{petId}` → `get_pets_petid`).
- Duplicates get numeric suffixes (`same`, `same_2`).
- Run `mcpify doctor` to see how many operations lack ids/summaries —
  fixing those makes agent interaction dramatically better.

## 7. Troubleshooting

| Symptom | Cause / fix |
| ------- | ----------- |
| `not an OpenAPI document` | The URL returned HTML (a login page?). Check the URL. |
| `no base URL` | Spec has no `servers` — pass `--base-url https://...`. |
| `unresolvable reference` | External `$ref` — bundle the spec first. |
| `YAML specs need PyYAML` | `pip install 'mcpify[yaml]'`. |
| Agent calls never arrive | Check the client config command resolves (`which mcpify`). |
| `HTTP 401` from calls | Env var set but wrong/rotated; or the API expects a different auth style. |
| `HTTP 404` on every call | Base URL path duplication — spec paths usually already include a version segment. |

## 8. FAQ

**Does mcpify cache or store anything?**
No. It is a stateless translation layer between MCP and your API.

**Can the agent see my API key?**
No. Keys live in the server process environment and are injected into
outgoing requests only. They never appear in tool descriptions or results.

**POST bodies?**
Exposed as a single `body` object argument whose shape comes from the
spec's `requestBody` schema (with `$ref`s resolved).

**Does it support SSE/streaming MCP transports?**
Currently stdio only — which is what Claude Code, Cursor, and Claude
Desktop spawn. If you need HTTP transport, put a transport shim in front.

**Multiple servers, one spec?**
Run one `mcpify serve` per scope (see §4). Processes are cheap and isolated.
