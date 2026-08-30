"""`mcpify ui` — the local, zero-dependency operations dashboard.

One stdlib HTTP process next to your (or a would-be) MCP server:

- live tool explorer: every tool, its method/path/annotations and a
  dry-run preview of the exact request (masked; nothing executes —
  real calls stay in `mcpify try`)
- health monitor: on-demand probe per API with a latency history ring
- masked log viewer: the same lines --verbose/--log-file would write,
  never containing credentials
- form-based config editor: writes .mcpify.toml through the same
  serializer `mcpify init` uses, validated before hitting disk
- metrics snapshot of the Prometheus series (when recording is on)

Binds 127.0.0.1 by default. `--http-token` (or a ?token= query
parameter, so browsers can open it directly) protects non-loopback
use. No CDNs, no external fonts, one HTML string — auditable like the
rest of the tree.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from . import __version__, metrics
from .config import build_config_document

MAX_LOG_LINES = 200
MAX_HEALTH_POINTS = 60

_LOG_RING: deque[str] = deque(maxlen=MAX_LOG_LINES)
_LOG_SEEN = threading.Lock()
_HEALTH_HISTORY: dict[str, deque[float]] = {}
_LAST_HEALTH: dict[str, Any] | None = None


def _ring_sink(line: str) -> None:
    with _LOG_SEEN:
        _LOG_RING.append(line)


def _install_sink() -> None:
    from .http_client import register_log_sink

    register_log_sink(_ring_sink)


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mcpify dashboard</title>
<style>
:root{--bg:#0d1117;--card:#161b22;--line:#21262d;--txt:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--bad:#f85149;--warn:#d29922;--acc:#58a6ff;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
header{display:flex;align-items:baseline;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line)}
header h1{font-size:16px;margin:0}header .v{color:var(--dim);font-size:12px}
main{display:grid;grid-template-columns:1fr 380px;gap:14px;padding:14px 20px}
@media(max-width:980px){main{grid-template-columns:1fr}}
section{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:14px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);margin:0 0 10px}
input,select,button{background:#0d1117;color:var(--txt);border:1px solid var(--line);
border-radius:6px;padding:6px 8px;font:inherit}
button{cursor:pointer}button:hover{border-color:var(--acc)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--dim);text-align:left;font-weight:500;border-bottom:1px solid var(--line);padding:4px 8px}
td{border-bottom:1px solid var(--line);padding:5px 8px;vertical-align:top}
tr:hover td{background:#1c2129}
.badge{display:inline-block;border-radius:10px;padding:0 8px;font-size:11px;font-family:var(--mono)}
.GET{background:#123521;color:var(--ok)}.POST{background:#0c2d57;color:var(--acc)}
.PUT,.PATCH{background:#3d2e00;color:var(--warn)}.DELETE{background:#43111a;color:var(--bad)}
.ro{background:#123521;color:var(--ok)}.dep{background:#43111a;color:var(--bad)}
.api{background:#21262d;color:var(--dim)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}
.ok{background:var(--ok)}.bad{background:var(--bad)}.unk{background:var(--dim)}
#detail pre,#logs{background:#0a0d12;border:1px solid var(--line);border-radius:6px;
padding:10px;font-family:var(--mono);font-size:12px;overflow:auto;white-space:pre-wrap}
#logs{max-height:230px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
label{display:block;font-size:12px;color:var(--dim);margin:8px 0 3px}
#msg{margin-left:8px;font-size:12px}
.kv{color:var(--dim)}canvas{width:100%;height:56px;background:#0a0d12;border:1px solid var(--line);border-radius:6px}
details summary{cursor:pointer;color:var(--acc);font-size:12px}
</style></head><body>
<header><h1>mcpify</h1><span class="v" id="ver"></span><span class="v" id="uptime"></span>
<span style="flex:1"></span><span class="v" id="mode"></span></header>
<main><div>
<section><h2>Tools <span class="kv" id="tcount"></span></h2>
<input id="q" placeholder="filter tools…" style="width:260px">
<table><thead><tr><th>Name</th><th>API</th><th>Method</th><th>Path</th><th>Summary</th><th></th></tr></thead>
<tbody id="tools"></tbody></table>
<div id="detail" style="display:none;margin-top:10px"><b id="dname"></b>
<pre id="dschema"></pre>
<button id="runpreview">Preview request</button> <span class="kv">dry-run only — nothing executes; real calls live in <code>mcpify try</code></span>
<pre id="dpreview" style="display:none"></pre></div></section>
<section><h2>Metrics snapshot</h2><div id="metrics" class="kv" style="font-family:var(--mono);font-size:12px"></div></section>
</div><div>
<section><h2>API health</h2><div id="health">no probe yet — <button id="probe">run health check</button></div>
<div id="charts"></div></section>
<section><h2>Log tail <span class="kv">(masked)</span></h2><div id="logs">—</div></section>
<section><h2>Config editor <span class="kv" id="cfgpath"></span></h2>
<div class="grid">
<label>spec<input id="c-spec" style="width:100%"></label>
<label>base-url<input id="c-base-url" style="width:100%"></label>
<label>auth-env<input id="c-auth-env" style="width:100%"></label>
<label>format<select id="c-format"><option>auto</option><option>json</option><option>xml</option></select></label>
<label>cache-ttl<input id="c-cache-ttl" type="number" min="0" style="width:100%"></label>
<label>retry<input id="c-retry" type="number" min="0" style="width:100%"></label>
<label>timeout<input id="c-timeout" type="number" min="1" style="width:100%"></label>
<label>wait-on-429<input id="c-wait-on-429" type="number" min="0" style="width:100%"></label>
</div>
<label><input type="checkbox" id="c-read-only" style="width:auto"> read-only</label>
<label><input type="checkbox" id="c-lazy" style="width:auto"> lazy mode</label>
<button id="save">Save .mcpify.toml</button><span id="msg"></span></section>
</div></main>
<script>
const TOKEN=new URLSearchParams(location.search).get("token")||"";
const $=id=>document.getElementById(id);let STATE=null;
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function authed(u){return u+(TOKEN?u.includes("?")?"&":"?":"")+"token="+encodeURIComponent(TOKEN)}
async function api(u,opts){const r=await fetch(authed(u),opts);if(!r.ok)throw new Error(await r.text());return r.json()}
function fmtU(s){const m=Math.floor(s/60);return m?m+"m "+Math.round(s%60)+"s":Math.round(s)+"s"}
function spark(id,pts){const c=$(id);if(!c)return;const g=c.getContext("2d");
g.clearRect(0,0,c.width,c.height);if(pts.length<2)return;
const max=Math.max(...pts,0.001),w=c.width/ (pts.length-1);
g.strokeStyle="#58a6ff";g.beginPath();
pts.forEach((p,i)=>{const x=i*w,y=c.height-4-(p/max)*(c.height-10);i?g.lineTo(x,y):g.moveTo(x,y)});g.stroke()}
function render(){if(!STATE)return;
$("ver").textContent="v"+STATE.server.version;$("mode").textContent=STATE.server.name+" · ui";
$("uptime").textContent="up "+fmtU(STATE.server.uptime_seconds);
$("tcount").textContent="("+STATE.tools.length+")";
const q=$("q").value.toLowerCase();
$("tools").innerHTML=STATE.tools.filter(t=>!q||JSON.stringify(t).toLowerCase().includes(q))
.map(t=>`<tr data-n="${esc(t.name)}"><td><code>${esc(t.name)}</code></td>
<td><span class="badge api">${esc(t.api||"default")}</span></td>
<td><span class="badge ${esc(t.method)}">${esc(t.method)}</span></td>
<td><code>${esc(t.path)}</code></td><td>${esc(t.summary||"")}
${t.deprecated?' <span class="badge dep">deprecated</span>':""}${t.readOnly?' <span class="badge ro">RO</span>':""}</td>
<td>${t.hasSchema?'<button class="see">schema</button>':""}</td></tr>`).join("");
document.querySelectorAll("tr[data-n]").forEach(tr=>tr.querySelector(".see")?.addEventListener("click",()=>{
const t=STATE.tools.find(x=>x.name===tr.dataset.n);$("detail").style.display="block";
$("dname").textContent=t.name;$("dschema").textContent=JSON.stringify(t.schema,null,2);
$("dpreview").style.display="none";window.sel=t}));
$("metrics").innerHTML=STATE.metrics.counters.map(c=>`${esc(c.name)}{${Object.entries(c.labels).map(([k,v])=>esc(k)+'="'+esc(v)+'"').join(",")}} <b>${c.value}</b>`).join("<br>")||"—";
const logs=STATE.logs.slice(-40).join("\\n");$("logs").textContent=logs||"(no activity yet)";
const h=STATE.last_health;if(h&&h.apis){
$("health").innerHTML=h.apis.map(a=>`<div><span class="dot ${a.api_reachable?"ok":"bad"}"></span>
<b>${esc(a.api)}</b> <span class="kv">${a.api_reachable?"reachable":"DOWN"} · ${a.latency_seconds}s · ${a.base_url}</span></div>`).join("");
$("charts").innerHTML=h.apis.map((a,i)=>`<div style="margin-top:8px"><span class="kv">${esc(a.api)} latency</span>
<canvas id="sp${i}" width="330" height="56"></canvas></div>`).join("");
h.apis.forEach((a,i)=>spark("sp"+i,(STATE.health_history[a.api]||[]).slice(-40)));}
const c=STATE.config_defaults||{};["spec","base-url","auth-env","format","cache-ttl","retry","timeout","wait-on-429"]
.forEach(k=>{const el=$("c-"+k);if(el&&document.activeElement!==el)el.value=c[k]??""});
$("c-read-only").checked=!!(c["read-only"]);$("c-lazy").checked=!!c.lazy;
$("cfgpath").textContent=STATE.config_path?("· "+STATE.config_path):"";}
$("q").addEventListener("input",render);
$("runpreview").addEventListener("click",async()=>{if(!window.sel)return;
try{const r=await api("/api/preview",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify({name:window.sel.name,arguments:{}})});
$("dpreview").style.display="block";$("dpreview").textContent=r.content?.[0]?.text??"(no preview)";}
catch(e){$("dpreview").style.display="block";$("dpreview").textContent=String(e)}});
$("probe").addEventListener("click",async()=>{try{await api("/api/health",{method:"POST"})}catch(e){}
refresh()});
$("save").addEventListener("click",async()=>{
const body={spec:$("c-spec").value,"base-url":$("c-base-url").value,"auth-env":$("c-auth-env").value,
format:$("c-format").value,"cache-ttl":+$("c-cache-ttl").value||0,retry:+$("c-retry").value||0,
timeout:+$("c-timeout").value||0,"wait-on-429":+$("c-wait-on-429").value||0,
"read-only":$("c-read-only").checked,lazy:$("c-lazy").checked};
try{const r=await api("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},
body:JSON.stringify(body)});$("msg").textContent="saved → "+r.path;$("msg").style.color="var(--ok)";}
catch(e){$("msg").textContent=String(e).slice(0,160);$("msg").style.color="var(--bad)"}});
async function refresh(){try{STATE=await api("/api/state");render()}catch(e){/* transient */}}
refresh();setInterval(refresh,5000);
</script></body></html>
"""


def _public_tool_rows(server: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    lookup = {**server.by_name, **server.meta_tools}
    for name in server.listed_names:
        tool = lookup[name]
        meta = tool.get("_meta") or {}
        ann = tool.get("annotations") or {}
        rows.append(
            {
                "name": tool["name"],
                "api": tool.get("api", "default"),
                "method": meta.get("method", ""),
                "path": meta.get("path", ""),
                "summary": (tool.get("description") or "").split("\n")[0][:110],
                "deprecated": bool(meta.get("deprecated")),
                "readOnly": bool(ann.get("readOnlyHint")),
                "destructive": bool(ann.get("destructiveHint")),
                "tags": meta.get("tags", []),
                "hasSchema": bool(tool.get("inputSchema")),
                "schema": tool.get("inputSchema") or {},
            }
        )
    return rows


def build_state(server: Any, config_path: str | None, started: float) -> dict[str, Any]:
    """Everything the dashboard renders in one JSON document."""
    return {
        "server": {
            "name": server.server_name,
            "version": __version__,
            "uptime_seconds": round(time.monotonic() - started, 1),
        },
        "tools": _public_tool_rows(server),
        "metrics": metrics.snapshot(),
        "logs": list(_LOG_RING),
        "last_health": _LAST_HEALTH,
        "health_history": {api: list(points) for api, points in _HEALTH_HISTORY.items()},
        "config_path": config_path,
        "config_defaults": getattr(server, "ui_config_defaults", None),
    }


def make_ui_handler(
    server: Any,
    token: str | None,
    config_path: str | None,
    started: float,
) -> type[BaseHTTPRequestHandler]:
    """Bind a server object into the dashboard handler class."""

    class UIHandler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def _authorized(self) -> bool:
            if not token:
                return True
            header = self.headers.get("Authorization", "")
            if header == f"Bearer {token}":
                return True
            query = self.path.split("?", 1)
            if len(query) == 2:
                from urllib.parse import parse_qs

                values = parse_qs(query[1]).get("token", [])
                return bool(values) and values[0] == token
            return False

        def _deny(self) -> None:
            body = b"unauthorized"
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="mcpify-ui"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0 or length > 200_000:
                raise ValueError("request body missing or too large")
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("JSON body must be an object")
            return data

        def do_GET(self) -> None:
            if not self._authorized():
                self._deny()
                return
            path = self.path.split("?", 1)[0]
            if path == "/":
                body = _PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/state":
                self._json(build_state(server, config_path, started))
            else:
                self._json({"error": f"unknown path {path!r}"}, 404)

        def do_POST(self) -> None:
            if not self._authorized():
                self._deny()
                return
            global _LAST_HEALTH
            path = self.path.split("?", 1)[0]
            try:
                if path == "/api/preview":
                    data = self._body()
                    result = server.preview_request(str(data.get("name", "")), dict(data.get("arguments") or {}))
                    self._json(result)
                elif path == "/api/health":
                    result = server.run_health_check()
                    try:
                        report = json.loads(str(result["content"][0]["text"]))
                    except (KeyError, IndexError, ValueError):
                        report = {"error": "health probe returned no report"}
                    _LAST_HEALTH = report
                    for item in report.get("apis", [report]):
                        api_name = str(item.get("api", server.server_name))
                        points = _HEALTH_HISTORY.setdefault(api_name, deque(maxlen=MAX_HEALTH_POINTS))
                        points.append(float(item.get("latency_seconds", 0.0)))
                    self._json(report)
                elif path == "/api/config":
                    data = self._body()
                    written = write_config_from_form(data, config_path)
                    self._json({"ok": True, "path": written})
                else:
                    self._json({"error": f"unknown path {path!r}"}, 404)
            except (ValueError, KeyError) as err:
                self._json({"error": str(err)}, 400)

    return UIHandler


_CONFIG_FORM_KEYS = (
    "spec", "base-url", "auth-env", "auth-style", "auth-name", "format",
    "cache-ttl", "retry", "retry-delay", "timeout", "wait-on-429",
)


def write_config_from_form(form: dict[str, Any], config_path: str | None) -> str:
    """Validate the dashboard form and write .mcpify.toml through the
    same serializer `mcpify init` uses. Unknown keys are rejected, so a
    typo can never silently produce a dead config."""
    settings: dict[str, Any] = {}
    for key, value in form.items():
        if key not in _CONFIG_FORM_KEYS and key not in ("read-only", "lazy"):
            raise ValueError(f"unknown config key: {key}")
        if key in ("read-only", "lazy"):
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be a boolean")
            if value:
                settings[key] = True
            continue
        if value in (None, ""):
            continue
        if key in ("cache-ttl", "retry", "retry-delay", "timeout", "wait-on-429"):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{key} must be a non-negative number")
            settings[key] = int(value) if key in ("cache-ttl", "retry") else float(value)
        elif key == "format":
            if value not in ("auto", "json", "xml"):
                raise ValueError(f"invalid format: {value}")
            if value != "auto":
                settings[key] = value
        else:
            settings[key] = str(value)
    document = build_config_document(settings)
    target = config_path or ".mcpify.toml"
    Path(target).write_text(document, encoding="utf-8")
    return target


def build_ui_server(
    server: Any,
    host: str,
    port: int,
    token: str | None,
    config_path: str | None,
) -> tuple[HTTPServer, float]:
    """Bind the dashboard; returns (httpd, started-monotonic)."""
    _install_sink()
    metrics.enable()
    handler = make_ui_handler(server, token, config_path, time.monotonic())
    httpd = HTTPServer((host, port), handler)
    return httpd, time.monotonic()


def serve_ui(
    server: Any,
    host: str,
    port: int,
    token: str | None,
    config_path: str | None,
    reload_cb: Callable[[], None] | None = None,
) -> None:
    """Run the dashboard until Ctrl+C. `reload_cb` runs on a background
    watcher so `--reload` keeps working while the UI is open."""
    # bind choice belongs to the operator; the warning below is the guard
    if host not in ("127.0.0.1", "localhost", "::1") and token is None:
        print(
            "WARNING: dashboard bound to a non-loopback address WITHOUT a token — "
            "pass --http-token (tool schemas and masked logs are exposed).",
            file=sys.stderr,
            flush=True,
        )
    httpd, _ = build_ui_server(server, host, port, token, config_path)
    bound = httpd.server_address
    shown_host = "127.0.0.1" if str(bound[0]) == "0.0.0.0" else str(bound[0])  # noqa: S104 -- display only
    suffix = f"?token={token}" if token else ""
    print(
        f"mcpify ui: dashboard at http://{shown_host}:{bound[1]}/{suffix} (Ctrl+C to stop)",
        file=sys.stderr,
        flush=True,
    )
    if reload_cb is not None:
        watcher = threading.Thread(target=reload_cb, daemon=True)
        watcher.start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
