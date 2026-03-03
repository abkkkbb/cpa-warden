"""CPA Warden — web dashboard & health-check server."""

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA_DIR = Path(os.getenv("CPA_DATA_DIR", "/data"))
TRIGGER_DIR = Path("/tmp")

# ─── HTML dashboard (embedded) ────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CPA Warden</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e1e4e8;min-height:100vh;padding:1.5rem}
h1{font-size:1.4rem;color:#f0f3f6;margin-bottom:.3rem}
.subtitle{color:#484f58;font-size:.8rem;margin-bottom:1.5rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:1rem}
.card{background:#1c1f26;border:1px solid #2d333b;border-radius:8px;padding:1.2rem;transition:border-color .15s}
.card:hover{border-color:#3d444d}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:.8rem}
.inst-name{font-weight:600;font-size:1.05rem}
.badge{padding:2px 10px;border-radius:12px;font-size:.75rem;font-weight:600;text-transform:uppercase}
.badge-scan{background:#1f3a5f;color:#58a6ff}
.badge-maintain{background:#3b2e1a;color:#f0883e}
.row{display:flex;justify-content:space-between;padding:.35rem 0;border-bottom:1px solid #21262d;font-size:.85rem}
.row:last-of-type{border-bottom:none}
.lbl{color:#8b949e}
.val{text-align:right;max-width:60%;word-break:break-all}
.ok{color:#3fb950}.fail{color:#f85149}.running{color:#d29922}.idle{color:#6e7681}
.actions{display:flex;gap:.5rem;margin-top:.9rem}
.btn{flex:1;padding:.55rem;border:1px solid #2d333b;border-radius:6px;background:#21262d;color:#c9d1d9;font-size:.85rem;cursor:pointer;transition:background .12s}
.btn:hover{background:#30363d}
.btn:active{background:#3a414a}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-scan{border-color:#1f3a5f}.btn-scan:hover:not(:disabled){background:#1f3a5f}
.btn-maintain{border-color:#3b2e1a}.btn-maintain:hover:not(:disabled){background:#3b2e1a}
.footer{text-align:center;color:#484f58;font-size:.75rem;margin-top:1.8rem}
.loading{text-align:center;color:#6e7681;padding:3rem}
</style>
</head>
<body>
<h1>CPA Warden Dashboard</h1>
<p class="subtitle" id="clock"></p>
<div class="grid" id="grid"><div class="loading">Loading...</div></div>
<div class="footer">Auto-refresh 10s</div>
<script>
function fmtInterval(s){
  if(s>=3600)return (s/3600)+'h';
  if(s>=60)return (s/60)+'m';
  return s+'s';
}
function timeAgo(iso){
  if(!iso)return '-';
  const d=Date.now()-new Date(iso+'Z').getTime();
  if(d<0)return 'just now';
  const s=Math.floor(d/1000);
  if(s<60)return s+'s ago';
  if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';
  return Math.floor(s/86400)+'d ago';
}
function nextRun(iso,interval){
  if(!iso)return '-';
  const next=new Date(iso+'Z').getTime()+interval*1000;
  const diff=next-Date.now();
  if(diff<=0)return 'imminent';
  const s=Math.floor(diff/1000);
  if(s<60)return s+'s';
  if(s<3600)return Math.floor(s/60)+'m '+s%60+'s';
  return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';
}
function render(instances){
  const g=document.getElementById('grid');
  if(!instances.length){g.innerHTML='<div class="loading">No instances found</div>';return;}
  g.innerHTML=instances.map(i=>{
    const lr=i.last_run;
    let sc='idle',st='Waiting';
    if(i.running){sc='running';st='Running...';}
    else if(lr){
      if(lr.exit_code===0){sc='ok';st='Success';}
      else{sc='fail';st='Failed ('+lr.exit_code+')';}
    }
    const mc=i.mode==='maintain'?'badge-maintain':'badge-scan';
    const dis=i.running||i.trigger_pending?'disabled':'';
    const lrTime=lr?lr.timestamp:null;
    return `<div class="card">
      <div class="card-header">
        <span class="inst-name">Instance ${i.id}</span>
        <span class="badge ${mc}">${i.mode}</span>
      </div>
      <div class="row"><span class="lbl">Base URL</span><span class="val">${i.base_url}</span></div>
      <div class="row"><span class="lbl">Interval</span><span class="val">${fmtInterval(i.interval)}</span></div>
      <div class="row"><span class="lbl">Status</span><span class="val ${sc}">${st}</span></div>
      <div class="row"><span class="lbl">Last Run</span><span class="val">${lr?(lr.mode+' · '+timeAgo(lrTime)):'-'}</span></div>
      <div class="row"><span class="lbl">Next Run</span><span class="val">${i.running?'-':nextRun(lrTime,i.interval)}</span></div>
      <div class="actions">
        <button class="btn btn-scan" ${dis} onclick="trigger('${i.id}','scan')">Scan</button>
        <button class="btn btn-maintain" ${dis} onclick="trigger('${i.id}','maintain')">Maintain</button>
      </div>
    </div>`;
  }).join('');
}
async function refresh(){
  try{
    const r=await fetch('/api/instances');
    const d=await r.json();
    render(d.instances||[]);
  }catch(e){console.error(e);}
  document.getElementById('clock').textContent=new Date().toLocaleString('zh-CN');
}
async function trigger(id,mode){
  try{
    const r=await fetch('/api/trigger/'+id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:mode})});
    const d=await r.json();
    if(!d.ok)alert(d.error);
    setTimeout(refresh,500);
  }catch(e){alert(e);}
}
refresh();
setInterval(refresh,10000);
</script>
</body>
</html>"""


# ─── API helpers ──────────────────────────────────────────────

def _discover_instances() -> list[dict]:
    """Find all meta.json under DATA_DIR to discover instances."""
    instances: list[dict] = []
    seen: set[str] = set()

    # Single-instance: /data/meta.json
    # Multi-instance:  /data/instance_*/meta.json
    candidates = [DATA_DIR / "meta.json"]
    candidates.extend(sorted(DATA_DIR.glob("instance_*/meta.json")))

    for meta_path in candidates:
        if not meta_path.is_file():
            continue
        data_dir = meta_path.parent
        try:
            meta = json.loads(meta_path.read_text("utf-8"))
        except Exception:
            continue

        iid = str(meta.get("id", "0"))
        if iid in seen:
            continue
        seen.add(iid)

        # Read last_run.json
        last_run = None
        lr_path = data_dir / "last_run.json"
        if lr_path.is_file():
            try:
                last_run = json.loads(lr_path.read_text("utf-8"))
            except Exception:
                pass

        running = (data_dir / "running").is_file()
        trigger_pending = (TRIGGER_DIR / f"trigger_instance_{iid}").is_file()

        instances.append({
            "id": iid,
            "base_url": meta.get("base_url", ""),
            "mode": meta.get("mode", "scan"),
            "interval": meta.get("interval", 1800),
            "running": running,
            "trigger_pending": trigger_pending,
            "last_run": last_run,
        })

    return instances


def _trigger_instance(iid: str, mode: str) -> dict:
    if not iid.isalnum():
        return {"ok": False, "error": "invalid instance id"}
    if mode not in ("scan", "maintain"):
        return {"ok": False, "error": "mode must be 'scan' or 'maintain'"}

    # Locate data dir to check running state
    if iid == "0":
        data_dir = DATA_DIR
    else:
        data_dir = DATA_DIR / f"instance_{iid}"

    if (data_dir / "running").is_file():
        return {"ok": False, "error": "instance is currently running"}

    trigger_path = TRIGGER_DIR / f"trigger_instance_{iid}"
    if trigger_path.is_file():
        return {"ok": False, "error": "trigger already pending"}

    trigger_path.write_text(mode, encoding="utf-8")
    return {"ok": True, "message": f"Instance {iid} triggered ({mode})"}


# ─── HTTP handler ─────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._json(200, {"status": "ok"})
        elif self.path == "/":
            self._html(200, DASHBOARD_HTML)
        elif self.path == "/api/instances":
            self._json(200, {"instances": _discover_instances()})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.startswith("/api/trigger/"):
            iid = self.path.rsplit("/", 1)[-1]
            # Read request body
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(body)
            except Exception:
                payload = {}
            mode = payload.get("mode", "scan")
            result = _trigger_instance(iid, mode)
            self._json(200 if result["ok"] else 400, result)
        else:
            self._json(404, {"error": "not found"})

    # ── Response helpers ──────────────────────────────────────

    def _json(self, code: int, data: dict) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, code: int, html: str) -> None:
        raw = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, _fmt: str, *_args: object) -> None:
        return


# ─── Main ─────────────────────────────────────────────────────

def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"[dashboard] Listening on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
