# Self-hosting mcpify — the free alternative to hosted-MCP plans

Paid platforms charge monthly to put an API behind an MCP endpoint:
hosted generators bill per request or per token under management, hosted
MCP endpoints start around $9/month for a few thousand requests, and
integration gateways charge $29–$229/month for call quotas (public
pricing pages, as of August 2026). The underlying capability — *your
OpenAPI spec, exposed as MCP tools over HTTP, with auth* — is a single
stdin-optional process. You can run it yourself on anything, for free,
and this guide shows the two most common setups.

## What you already have in this repository

| Capability | Paid platforms | mcpify |
|---|---|---|
| OpenAPI → MCP tools | generator, often gated | `mcpify serve`, free, no account |
| Streamable HTTP endpoint | paid tier feature | `--http`, free (stdlib, stateless) |
| Bearer protection | paid tier / enterprise | `--http-token`, free |
| Auth style configuration | dashboard/paid flow | auto-detected from the spec (`--auth-env VAR` alone) |
| OAuth2 client-credentials | enterprise flow | `--oauth2-*`, free (stdlib) |
| Rate-limit courtesy (Retry-After) | gateway feature | `--wait-on-429`, free, opt-in |
| Logs | paid analytics | `--verbose` / `--log-file`, masked, free |

## Option 1 — Docker Compose + Caddy (recommended)

`deploy/docker-compose.yml` + `deploy/Caddyfile` give you a
bearer-protected, automatic-HTTPS endpoint in two commands:

```bash
cd deploy
cp openapi.json your real spec...     # mount your OpenAPI document
export MCPIFY_HTTP_TOKEN="$(openssl rand -hex 24)"
export DOMAIN="mcp.yourdomain.com"
docker compose up -d
```

Then point any HTTP-capable MCP client at `https://mcp.yourdomain.com`
with `Authorization: Bearer <token>`. Caddy terminates TLS (Let's
Encrypt, automatic) and re-checks the token at the edge; mcpify checks
it again at the process — defense in depth.

Point the compose file at a **read-only or deny-scoped** surface unless
your team explicitly needs writes:

```yaml
command: ["serve", "/spec/openapi.json", "--http", "0.0.0.0:8080",
          "--http-token", "${MCPIFY_HTTP_TOKEN}", "--read-only"]
```

## Option 2 — systemd on a plain VM

`deploy/mcpify.service` runs mcpify as a hardened service (non-root,
`ProtectSystem=strict`) on any Linux box:

```bash
pipx install mcpify-openapi
sudo cp deploy/mcpify.service /etc/systemd/system/
sudo systemctl edit mcpify      # set MCPIFY_HTTP_TOKEN (+ upstream API_TOKEN if needed)
sudo systemctl enable --now mcpify
```

Put nginx/Caddy/traefik in front for TLS, or keep it on a private
network / Tailscale and skip public exposure entirely.

## Choosing a host (if you don't have one)

The container is a ~1-core, ~64 MB-RAM workload for typical teams. Any
cheap VPS works. The 2026 cost floor others quote (Cloudflare Workers
Paid at $5/month, small VPSes at ~$4–6/month) is the *entire* budget
here — there is no per-request meter on top.

## Hardening checklist

- [ ] `--http-token` set (or an authenticating reverse proxy) — never expose unauthenticated
- [ ] Scope the surface: `--read-only`, `--tag`, `--include/--exclude`, `--deny`
- [ ] Credentials only via environment (`--auth-env` / `--oauth2-*-env`), never flags or config values
- [ ] `--timeout` low enough that a slow upstream can't pin the worker
- [ ] `--log-file` on a volume if you need an audit trail (credentials are masked)
- [ ] Upgrade is `docker compose pull && docker compose up -d` — the spec is re-read at start, tools are always current (no "publish/rollback" pipeline needed)

## When you *should* pay for a platform

Honest limits of self-hosting: you own availability, TLS renewal
monitoring, and incident response. If you need SSO/SCIM, per-agent RBAC,
PII blocking, SOC 2 attestations, or multi-tenant metering, managed
gateways sell exactly that — and mcpify deliberately does not pretend to
be it (see `docs/ARCHITECTURE.md` → Scope statement).
