"""Config file support: .mcpify.toml / .mcpify.yaml / .mcpify.json.

A config file captures one API's serve settings; the optional [envs.NAME]
sections hold per-environment overrides (dev/staging/prod). Precedence:

    CLI flags  >  [envs.NAME]  >  [serve]  >  built-in defaults

Zero dependencies: TOML is parsed with tomllib on Python 3.11+, with a
small built-in subset parser as the 3.10 fallback (the subset covers
everything `mcpify init` writes: tables, strings, integers, booleans and
string arrays). YAML configs need the optional `mcpify[yaml]` extra.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

CONFIG_NAMES = (".mcpify.toml", ".mcpify.yaml", ".mcpify.json")

# Keys allowed inside [serve] and [envs.NAME]. Everything the serve path
# consumes; unknown keys are reported (typos should not vanish silently).
KNOWN_KEYS = frozenset({
    "spec", "base-url", "server", "name", "auth-env", "auth-style", "auth-name",
    "timeout", "tag", "include", "exclude", "read-only", "allow", "deny",
    "lazy", "enable-preview", "cache-ttl", "retry", "retry-delay",
    "strict", "format", "verbose", "log-file", "default-env",
    "oauth2-token-url", "oauth2-client-id-env", "oauth2-client-secret-env",
    "oauth2-scope", "oauth2-client-auth", "http", "http-token", "wait-on-429",
})

_ENV_KEYS = KNOWN_KEYS - {"default-env"}

# Per-API sections ([apis.NAME]): everything that can differ per upstream.
# Surface-level switches (lazy, preview, verbose, http transport, server
# name) are process-wide and therefore belong to [serve]/CLI only.
_API_KEYS = frozenset({
    "spec", "base-url", "server", "auth-env", "auth-style", "auth-name",
    "oauth2-token-url", "oauth2-client-id-env", "oauth2-client-secret-env",
    "oauth2-scope", "oauth2-client-auth", "timeout", "tag", "include",
    "exclude", "read-only", "allow", "deny", "cache-ttl", "retry",
    "retry-delay", "strict", "format", "wait-on-429",
})


_BASIC_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "r": "\r"}


def _unescape_basic(body: str, lineno: int) -> str:
    """Resolve TOML basic-string escapes (subset: \\ \" \n \t \r)."""
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(body) or body[i + 1] not in _BASIC_ESCAPES:
            raise ValueError(
                f".mcpify.toml line {lineno}: unsupported escape \\{body[i + 1:i + 2]} "
                "(write Windows paths with single quotes: 'C:\\path')"
            )
        out.append(_BASIC_ESCAPES[body[i + 1]])
        i += 2
    return "".join(out)


def _parse_mini_toml(text: str) -> dict:
    """Parse the TOML subset mcpify writes: tables, strings, ints,
    booleans, string arrays, # comments. Raises ValueError on anything
    else so users get a clear message instead of silent misconfig."""
    data: dict = {}
    table = data
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            table = data
            for part in name.split("."):
                table = table.setdefault(part, {})
            continue
        match = re.match(r"^([A-Za-z0-9_-]+)\s*=\s*(.+)$", line)
        if not match:
            raise ValueError(f".mcpify.toml line {lineno}: cannot parse {raw!r}")
        key, value = match.group(1), match.group(2).strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            items = [s.strip() for s in inner.split(",")] if inner else []
            parsed = []
            for item in items:
                if item.startswith('"') and item.endswith('"'):
                    parsed.append(_unescape_basic(item[1:-1], lineno))
                elif item.startswith("'") and item.endswith("'"):
                    parsed.append(item[1:-1])
                else:
                    raise ValueError(f".mcpify.toml line {lineno}: arrays of strings only")
            table[key] = parsed
        elif value.startswith('"') and value.endswith('"'):
            table[key] = _unescape_basic(value[1:-1], lineno)
        elif value.startswith("'") and value.endswith("'"):
            table[key] = value[1:-1]  # TOML literal string: no escapes
        elif value in ("true", "false"):
            table[key] = value == "true"
        else:
            try:
                table[key] = int(value)
            except ValueError:
                raise ValueError(
                    f".mcpify.toml line {lineno}: unsupported value {value!r} "
                    "(use strings, integers, booleans or string arrays)"
                ) from None
    return data


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib  # Python 3.11+; the 3.10 path is the subset parser below
    except ImportError:
        try:
            return _parse_mini_toml(path.read_text(encoding="utf-8"))
        except ValueError as err:
            raise ValueError(str(err)) from None
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as err:  # pragma: no cover - exercised via extra
        raise ValueError(
            f"{path.name} is YAML; install the optional extra: pip install 'mcpify[yaml]'"
        ) from err
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def find_config(start_dir: str | None = None) -> Path | None:
    """Return the first config file in start_dir (default: cwd)."""
    directory = Path(start_dir) if start_dir else Path.cwd()
    for name in CONFIG_NAMES:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def load_config(path: str | None = None, start_dir: str | None = None) -> tuple[Path | None, dict[str, Any]]:
    """Load explicit or auto-discovered config. Returns (path, data)."""
    candidate: Path | None
    if path:
        candidate = Path(path)
        if not candidate.is_file():
            raise ValueError(f"config not found: {path}")
    else:
        candidate = find_config(start_dir)
        if candidate is None:
            return None, {}
    if candidate.name.endswith(".toml"):
        data = _load_toml(candidate)
    elif candidate.name.endswith((".yaml", ".yml")):
        data = _load_yaml(candidate)
    else:
        data = _load_json(candidate)
    if not isinstance(data, dict):
        raise ValueError(f"{candidate.name}: top level must be a table/mapping")
    return candidate, data


def validate(data: dict) -> list[str]:
    """Unknown-key report so a typo never silently does nothing."""
    problems = []
    for key, value in data.items():
        if key == "envs":
            if not isinstance(value, dict):
                problems.append("envs: must be a table of tables")
            for env, section in value.items():
                if not isinstance(section, dict):
                    problems.append(f"envs.{env}: must be a table")
                    continue
                problems.extend(f"envs.{env}.{k}: unknown key" for k in section if k not in _ENV_KEYS)
        elif key == "serve":
            if not isinstance(value, dict):
                problems.append("serve: must be a table")
                continue
            problems.extend(f"serve.{k}: unknown key" for k in value if k not in KNOWN_KEYS)
        elif key == "apis":
            if not isinstance(value, dict) or not value:
                problems.append("apis: must be a table of at least one API ([apis.NAME])")
                continue
            for api_name, section in value.items():
                if not isinstance(section, dict):
                    problems.append(f"apis.{api_name}: must be a table")
                    continue
                if not section.get("spec"):
                    problems.append(f"apis.{api_name}: missing required 'spec'")
                problems.extend(
                    f"apis.{api_name}.{k}: unknown key"
                    for k in section
                    if k not in _API_KEYS
                )
        elif key not in KNOWN_KEYS:
            problems.append(f"{key}: unknown key (put serve settings under [serve])")
    return problems


def api_sections(data: dict) -> dict[str, dict[str, Any]]:
    """The [apis.NAME] sections of a config, or an empty dict."""
    sections = data.get("apis")
    return sections if isinstance(sections, dict) else {}


def resolve(data: dict, env: str | None = None) -> dict[str, Any]:
    """Flatten config to serve settings: [serve] then [envs.NAME] on top.

    The chosen env is (in order) the --env flag, `default-env` in the
    file, then none. Keys use the CLI flag spelling (kebab-case).
    """
    serve = dict(data.get("serve") or {})
    default_env = serve.pop("default-env", None)
    chosen = env or default_env
    if chosen:
        envs = data.get("envs") or {}
        if chosen not in envs:
            raise ValueError(
                f"env '{chosen}' not in config (available: {', '.join(sorted(envs)) or 'none'})"
            )
        serve.update(envs[chosen])
    serve["_env"] = chosen
    return serve


def apply_to_namespace(settings: dict[str, Any], args: Any) -> list[str]:
    """Fill unset argparse attributes from config. CLI flags always win.

    A value counts as "unset" when it still equals the parser's built-in
    default — including choice flags whose default is a string ("bearer",
    "basic", "auto", "mcpify"). Returns the list of keys the config
    actually provided (for the stderr banner)."""
    applied: list[str] = []
    mapping = {
        "base-url": "base_url", "auth-env": "auth_env", "auth-style": "auth_style",
        "auth-name": "auth_name", "read-only": "read_only", "enable-preview": "enable_preview",
        "cache-ttl": "cache_ttl", "retry-delay": "retry_delay", "log-file": "log_file",
        "wait-on-429": "wait_on_429",
    }
    choice_defaults = {
        # --auth-style's default is now None (spec auto-detection), which the
        # `current is None` branch already treats as unset
        "oauth2_client_auth": "basic", "name": "mcpify", "format": "auto",
    }
    for key, value in settings.items():
        if key.startswith("_"):
            continue
        attr = mapping.get(key, key.replace("-", "_"))
        current = getattr(args, attr, None)
        is_default = current is None or current is False or (
            attr in ("timeout", "cache_ttl", "retry_delay", "wait_on_429") and current == 0
        ) or (attr == "timeout" and current == 30.0) or (
            attr in choice_defaults and current == choice_defaults[attr]
        )
        if key in ("read-only", "lazy", "enable-preview", "strict", "verbose"):
            # boolean flags: config may turn them ON; an explicit CLI True stays
            if current is False and value:
                setattr(args, attr, value)
                applied.append(key)
            continue
        if is_default and value is not None:
            setattr(args, attr, value)
            applied.append(key)
    return applied


# ---------------------------------------------------------------------------
# init wizard (logic separated from I/O for testability)
# ---------------------------------------------------------------------------

def _toml_string(value: str) -> str:
    """TOML string for a value: literal quotes when safe (keeps Windows
    paths readable and un-escaped), basic quotes otherwise."""
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


_CONFIG_KEY_ORDER = [
    "spec", "base-url", "server", "name", "auth-env", "auth-style", "auth-name",
    "oauth2-token-url", "oauth2-client-id-env", "oauth2-client-secret-env",
    "oauth2-scope", "oauth2-client-auth", "http", "wait-on-429",
    "read-only", "timeout", "cache-ttl", "retry", "retry-delay",
    "strict", "lazy", "enable-preview", "format", "log-file",
    "tag", "include", "exclude", "allow", "deny", "verbose",
]


def _fmt_toml(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_string(str(v)) for v in value) + "]"
    return _toml_string(str(value))


def build_config_document(settings: dict) -> str:
    """Serialize serve settings as readable TOML (subset-safe)."""
    lines = ["# Generated by `mcpify init`. Edit freely;",
             "# CLI flags override everything in here.", "", "[serve]"]
    for key in _CONFIG_KEY_ORDER:
        if key in settings and settings[key] not in (None, False, "", 0, []):
            lines.append(f"{key} = {_fmt_toml(settings[key])}")
    return "\n".join(lines) + "\n"




def run_wizard(
    answers: Iterator[str],
    spec_loader: Callable[[str], tuple[dict, str]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Interactive questionnaire. `answers` is an iterator of raw input
    strings (tests inject them; the CLI passes stdin lines). Returns
    (settings, warnings). spec_loader(spec_arg) -> (spec, base_default)
    when the spec can be read; used to prefill the base URL."""
    def ask(prompt: str, default: str = "") -> str:
        raw = next(answers).strip()
        return raw or default

    def ask_bool(prompt: str, default: bool = False) -> bool:
        raw = next(answers).strip().lower()
        if not raw:
            return default
        return raw in ("y", "yes", "ev", "true", "1")

    def ask_int(prompt: str, default: int) -> int:
        raw = next(answers).strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(f"'{raw}' is not a number") from None
        if value < 0:
            raise ValueError("must be >= 0")
        return value

    settings: dict[str, Any] = {}
    warnings: list[str] = []

    spec_arg = ask("Spec path or URL")
    if not spec_arg:
        raise ValueError("a spec path or URL is required")
    if spec_arg.startswith(("http://", "https://")):
        from urllib.parse import urlparse

        if urlparse(spec_arg).path in ("", "/"):
            from .spec import discover_spec

            spec_arg, hint = discover_spec(spec_arg)
            if hint:
                warnings.append(hint)
    settings["spec"] = spec_arg

    base_default = ""
    if spec_loader is not None:
        try:
            spec, base_default = spec_loader(spec_arg)
        except Exception:  # noqa: BLE001 — wizard degrades gracefully
            base_default = ""
    settings["base-url"] = ask(f"Base URL{' [' + base_default + ']' if base_default else ''}", base_default)
    if not settings["base-url"]:
        raise ValueError("a base URL is required")

    auth = ask("Auth [1=none 2=bearer 3=header 4=query 5=oauth2-cc]", "1")
    if auth == "2":
        settings["auth-env"] = ask("Env variable holding the token", "API_TOKEN")
        settings["auth-style"] = "bearer"
    elif auth == "3":
        settings["auth-env"] = ask("Env variable holding the key", "API_KEY")
        settings["auth-style"] = "header"
        settings["auth-name"] = ask("Header name", "X-API-Key")
    elif auth == "4":
        settings["auth-env"] = ask("Env variable holding the key", "API_KEY")
        settings["auth-style"] = "query"
        settings["auth-name"] = ask("Query parameter name", "api_key")
    elif auth == "5":
        settings["oauth2-token-url"] = ask("Token endpoint URL")
        settings["oauth2-client-id-env"] = ask("Env variable holding the client id", "OAUTH2_CLIENT_ID")
        secret_env = ask("Env variable holding the client secret (empty = public client)")
        if secret_env:
            settings["oauth2-client-secret-env"] = secret_env
        scope = ask("Scope(s), space-separated (empty = none)")
        if scope:
            settings["oauth2-scope"] = scope

    if ask_bool("Read-only mode"):
        settings["read-only"] = True
    ttl = ask_int("Cache TTL seconds [0 = off]", 0)
    if ttl:
        settings["cache-ttl"] = ttl
    retry = ask_int("Retry attempts for 502/503/504 [0 = off]", 0)
    if retry:
        settings["retry"] = retry
        settings["retry-delay"] = ask_int("Retry delay seconds", 1)
    if ask_bool("Lazy mode (search/schema/call for large APIs)"):
        settings["lazy"] = True
    fmt = ask("Response format [auto/json/xml]", "auto")
    if fmt != "auto":
        settings["format"] = fmt

    if settings.get("auth-env") and not os.environ.get(settings["auth-env"]):
        warnings.append(
            f"environment variable '{settings['auth-env']}' is not set right now — "
            "set it before serving"
        )
    for env_key in ("oauth2-client-id-env", "oauth2-client-secret-env"):
        if settings.get(env_key) and not os.environ.get(settings[env_key]):
            warnings.append(
                f"environment variable '{settings[env_key]}' is not set right now — "
                "set it before serving"
            )
    return settings, warnings
