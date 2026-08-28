# Security Policy

## Supported versions

| Version | Supported |
| ------- | --------- |
| latest release | ✅ |
| older releases | ❌ |

## Reporting a vulnerability

**Do not open a public issue.**

Email: **furkangoktan2@icloud.com** with the subject line
`[SECURITY] mcpify`.

Please include: a description, reproduction steps, affected versions,
and your assessment of impact/severity. You will get an initial response
within 72 hours. We will credit reporters in the release notes unless
anonymity is requested.

## Design notes

- Mcpify never sends credentials anywhere except the API you configure.
- Secrets are read from environment variables at call time, never from
  files, flags on the process list, or the specification itself.
