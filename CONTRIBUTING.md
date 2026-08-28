# Contributing to Mcpify

Thanks for taking the time to contribute! This project aims for
production-grade quality: typed code, full test coverage of new behavior,
and honest documentation.

## Development setup

```bash
git clone https://github.com/furkan708/mcpify.git
cd mcpify
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]' 2>/dev/null || pip install -e . pytest pytest-cov ruff mypy
```

## Workflow

1. Open an issue before non-trivial changes.
2. Create a feature branch (`feat/...`, `fix/...`).
3. Make your change. Every new behavior needs a test.
4. Run the full local gate before pushing:

```bash
pytest --cov -q          # tests must pass
ruff check .             # no lint errors
mypy mcpify/              # no type errors
```

5. Keep commits atomic and use [Conventional Commits](https://www.conventionalcommits.org/)
   (`feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:`).

## Pull requests

- CI must be green (tests, lint, type-check).
- Update `CHANGELOG.md` under **Unreleased**.
- Update README/docs when behavior or flags change.
- Backwards compatibility matters: breaking changes require a major version bump.

## Reporting bugs

Open an issue with: expected vs actual behavior, minimal reproduction,
version (`--version`) and environment details. For security issues see
[SECURITY.md](SECURITY.md) — please do not open public issues.
