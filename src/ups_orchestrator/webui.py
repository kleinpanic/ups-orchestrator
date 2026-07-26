"""Minimal stdlib web UI: live per-UPS status + draw history.

Zero dependencies (``http.server``), matching the rest of the project. Serves a
single dark dashboard plus JSON endpoints. **Binds to localhost by default and
has no authentication** — do not expose it publicly; put it behind a reverse
proxy with auth if you need remote access. It never serves secrets (the webhook
lives only in the environment).
"""

from __future__ import annotations

import html
import json
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ups_orchestrator.config import Config
from ups_orchestrator.nut import read_snapshot


def status_payload(cfg: Config, *, now: float | None = None) -> dict[str, object]:
    """Current per-UPS snapshot data for the ``/api/status`` endpoint."""
    now = time.time() if now is None else now
    upses = []
    for name, ups in cfg.upses.items():
        s = read_snapshot(name)
        upses.append(
            {
                "name": name,
                "label": ups.label,
                "status": s.status,
                "on_battery": s.on_battery,
                "low_battery": s.low_battery,
                "charge": s.charge,
                "runtime_seconds": s.runtime_seconds,
                "load": s.load,
                "load_level": s.load_level,
                "watts": s.estimated_load_watts,
                "nominal_watts": s.realpower_nominal,
                "headroom_watts": s.load_headroom_watts,
                "input_voltage": s.input_voltage,
                "output_voltage": s.output_voltage,
                "battery_voltage": s.battery_voltage,
                "battery_voltage_percent": s.battery_voltage_percent,
                "battery_type": s.battery_type,
                "alarm": s.alarm,
                "test_result": s.test_result,
            }
        )
    degraded = [
        {"severity": n.severity, "subject": n.subject, "message": n.message}
        for n in cfg.degraded
    ]
    return {"time": now, "upses": upses, "degraded": degraded}


def history_payload(
    cfg: Config, sample_path: Path, *, hours: int = 24, now: float | None = None
) -> dict[str, object]:
    """Recent per-UPS draw series (watts) for the ``/api/history`` endpoint."""
    now = time.time() if now is None else now
    from ups_orchestrator.dashboard import _read_series

    names = list(cfg.upses)
    series = _read_series(sample_path, names, now - hours * 3600)
    # Downsample to keep the response small (<= ~600 points per UPS).
    out: dict[str, list[list[float]]] = {}
    for name in names:
        pts = series.get(name, [])
        step = max(1, -(-len(pts) // 600))  # ceil, so the result is always <= 600 points
        out[name] = [[round(t, 1), w] for t, w in pts[::step]]
    return {"hours": hours, "series": out}


def make_handler(cfg: Config, sample_path: Path) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload: object, code: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 — stdlib naming
            route = urlparse(self.path)
            if route.path in ("/", "/index.html"):
                body = _render_page(cfg).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route.path == "/api/status":
                self._json(status_payload(cfg))
            elif route.path == "/api/history":
                hours = 24
                q = parse_qs(route.query)
                if "hours" in q:
                    with suppress(ValueError):
                        hours = max(1, min(720, int(q["hours"][0])))
                self._json(history_payload(cfg, sample_path, hours=hours))
            else:
                self._json({"error": "not found"}, code=404)

        def log_message(self, *_a: object) -> None:  # keep the console quiet
            pass

    return Handler


def serve(
    cfg: Config, sample_path: Path, *, host: str = "127.0.0.1", port: int = 8765
) -> None:  # pragma: no cover — the blocking server loop
    server = ThreadingHTTPServer((host, port), make_handler(cfg, sample_path))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _degraded_banner_html(cfg: Config) -> str:
    """Server-rendered degrade banner, empty when ``cfg.degraded`` is empty.

    ``Config`` is frozen and set once at ``serve()`` startup, so this is not a live
    view — it reflects the same load-time notices ``status.render`` shows, rendered
    once per page fetch so the operator sees it without opening a terminal.
    """
    if not cfg.degraded:
        return ""
    items = "".join(
        f'<div class="item"><span class="sev {html.escape(n.severity)}">{html.escape(n.severity)}'
        f'</span><span class="subj">{html.escape(n.subject)}</span>: {html.escape(n.message)}</div>'
        for n in cfg.degraded
    )
    return f'<div class="degraded" id="degraded">{items}</div>'


def _render_page(cfg: Config) -> str:
    """The served ``/`` page, with the degrade banner substituted for ``cfg``."""
    return _INDEX_HTML.replace("<!--DEGRADED_BANNER-->", _degraded_banner_html(cfg))


# All UPS-supplied strings (label/status/alarm/test_result) are HTML-escaped via
# esc() before insertion, since NUT variables cross a trust boundary.
_INDEX_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>UPS Power</title>
<style>
 :root{--bg:#0b1220;--panel:#0f1b2e;--grid:#1e293b;--fg:#e2e8f0;--muted:#94a3b8}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--fg);
   font:14px ui-monospace,Menlo,Consolas,monospace}
 header{padding:18px 24px;border-bottom:1px solid var(--grid)}
 h1{margin:0;font-size:20px} .sub{color:var(--muted);font-size:12px;margin-top:4px}
 .cards{display:flex;flex-wrap:wrap;gap:14px;padding:20px}
 .card{background:var(--panel);border:1px solid var(--grid);border-radius:12px;
   padding:16px;min-width:280px;flex:1}
 .card h2{margin:0 0 2px;font-size:15px} .model{color:var(--muted);font-size:12px}
 .badge{font-weight:700} .row{display:flex;justify-content:space-between;margin-top:10px;font-size:12px;color:var(--muted)}
 .val{color:var(--fg)} .gauge{height:8px;border-radius:5px;background:var(--grid);margin-top:4px;overflow:hidden}
 .gauge>span{display:block;height:100%} .watts{font-size:22px;font-weight:700;float:right}
 .alarm{color:#ef4444;font-weight:700}
 canvas{width:100%;height:260px;background:var(--panel);border:1px solid var(--grid);border-radius:12px}
 .chartwrap{padding:0 20px 24px}
 .degraded{margin:14px 20px 0;border:1px solid #ef4444;border-radius:10px;padding:10px 14px;
   background:#2a1015}
 .degraded .item{font-size:12px;margin-top:4px}
 .degraded .item:first-child{margin-top:0}
 .degraded .sev{font-weight:700;text-transform:uppercase;margin-right:6px}
 .degraded .sev.error{color:#ef4444} .degraded .sev.advisory{color:#fbbf24}
 .degraded .subj{color:var(--muted)}
</style></head><body>
<header><h1>UPS Power</h1><div class="sub" id="sub">loading…</div></header>
<!--DEGRADED_BANNER-->
<div class="cards" id="cards"></div>
<div class="chartwrap"><canvas id="chart" width="1200" height="260"></canvas></div>
<script>
const COLORS=["#38bdf8","#a78bfa","#f472b6","#34d399","#fbbf24"];
const ENT={"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"};
function esc(s){return String(s==null?"":s).replace(/[&<>"']/g,c=>ENT[c])}
function num(v){return v==null?"—":Number(v)}
function col(v,lo,hi){return v<=lo?"#ef4444":v<=hi?"#fbbf24":"#22c55e"}
function fmtRt(s){if(s==null)return"—";return s>=3600?`${(s/3600|0)}h ${(s%3600/60|0)}m`:`${(s/60|0)}m`}
async function refresh(){
 const st=await (await fetch("/api/status")).json();
 document.getElementById("sub").textContent=new Date(st.time*1000).toLocaleString()+" · live";
 const c=document.getElementById("cards"); c.innerHTML="";
 st.upses.forEach((u,i)=>{
  const nm=esc(u.label||u.name).split(" - ");
  const online=u.on_battery?"#fbbf24":u.low_battery?"#ef4444":"#22c55e";
  const bv=u.battery_voltage!=null?` · ${Number(u.battery_voltage).toFixed(1)} V`:"";
  const alarm=(u.alarm&&String(u.alarm).toLowerCase()!=="none")?`<span class="alarm">⚠ ${esc(u.alarm)}</span>`:"";
  c.innerHTML+=`<div class="card" style="border-top:3px solid ${COLORS[i%5]}">
   <span class="watts">${num(u.watts)} W</span>
   <h2>${nm[0]}</h2><div class="model">${nm[1]||""}</div>
   <div style="margin-top:8px"><span class="badge" style="color:${online}">${esc(u.status)||"?"}</span> ${alarm}</div>
   <div class="row"><span>battery ${num(u.charge)}%${bv}</span><span class="val">${fmtRt(u.runtime_seconds)}</span></div>
   <div class="gauge"><span style="width:${u.charge||0}%;background:${col(u.charge||0,20,50)}"></span></div>
   <div class="row"><span>load ${num(u.load)}% of ${num(u.nominal_watts)} W</span><span class="val">${u.headroom_watts!=null?num(u.headroom_watts)+" W free":""}</span></div>
   <div class="gauge"><span style="width:${u.load||0}%;background:${COLORS[i%5]}"></span></div>
   <div class="row"><span>in ${num(u.input_voltage)} V / out ${num(u.output_voltage)} V</span><span>${esc(u.test_result)}</span></div>
  </div>`;
 });
}
async function drawChart(){
 const h=await (await fetch("/api/history?hours=24")).json();
 const cv=document.getElementById("chart"),x=cv.getContext("2d"),W=cv.width,H=cv.height,P=36;
 x.clearRect(0,0,W,H);
 const names=Object.keys(h.series); let hi=1,t0=Infinity,t1=-Infinity;
 names.forEach(n=>h.series[n].forEach(([t,w])=>{hi=Math.max(hi,w);t0=Math.min(t0,t);t1=Math.max(t1,t)}));
 hi*=1.1; const sx=t=>P+(t-t0)/((t1-t0)||1)*(W-2*P), sy=w=>H-P-w/hi*(H-2*P);
 x.strokeStyle="#1e293b";x.beginPath();x.moveTo(P,H-P);x.lineTo(W-P,H-P);x.stroke();
 x.fillStyle="#94a3b8";x.font="11px monospace";x.fillText(Math.round(hi)+" W",4,sy(hi)+4);x.fillText("0",4,H-P+4);
 names.forEach((n,i)=>{const pts=h.series[n];if(pts.length<2)return;
  x.strokeStyle=COLORS[i%5];x.lineWidth=1.4;x.beginPath();
  pts.forEach(([t,w],j)=>{const px=sx(t),py=sy(w);j?x.lineTo(px,py):x.moveTo(px,py)});x.stroke();
  x.fillStyle=COLORS[i%5];x.fillText(n,P+10+i*120,18);});
}
refresh();drawChart();setInterval(refresh,5000);setInterval(drawChart,30000);
</script></body></html>
"""
