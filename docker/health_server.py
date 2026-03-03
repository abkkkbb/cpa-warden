"""CPA Warden — web dashboard & health-check server."""

import json
import os
import sqlite3
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DATA_DIR = Path(os.getenv("CPA_DATA_DIR", "/data"))
TRIGGER_DIR = Path("/tmp")


# ─── SQLite helpers ───────────────────────────────────────────

def _read_stats(data_dir: Path) -> dict | None:
    """Read latest scan stats from the instance SQLite database."""
    db_path = data_dir / "cpa_warden_state.sqlite3"
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            # Latest scan run
            row = conn.execute(
                "SELECT * FROM scan_runs ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            latest_run = dict(row) if row else None

            # Account counts
            counts_row = conn.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(is_invalid_401)  AS invalid_401,
                    SUM(is_quota_limited) AS quota_limited,
                    SUM(is_recovered)    AS recovered,
                    SUM(CASE WHEN disabled = 1 THEN 1 ELSE 0 END) AS disabled,
                    SUM(CASE WHEN is_invalid_401 = 0 AND is_quota_limited = 0
                              AND (disabled IS NULL OR disabled = 0)
                         THEN 1 ELSE 0 END) AS healthy
                FROM auth_accounts
            """).fetchone()
            accounts = dict(counts_row) if counts_row else None

            # Recent actions (last 20 that had an action)
            action_rows = conn.execute("""
                SELECT name, last_action, last_action_status, updated_at
                FROM auth_accounts
                WHERE last_action IS NOT NULL AND last_action != ''
                ORDER BY updated_at DESC LIMIT 20
            """).fetchall()
            actions = [dict(r) for r in action_rows]

            return {
                "latest_run": latest_run,
                "accounts": accounts,
                "recent_actions": actions,
            }
        finally:
            conn.close()
    except Exception:
        return None


def _read_logs(data_dir: Path, lines: int = 80) -> str:
    """Read last N lines of the log file."""
    log_path = data_dir / "cpa_warden.log"
    if not log_path.is_file():
        return ""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            return "".join(deque(f, maxlen=lines))
    except Exception:
        return ""


def _resolve_data_dir(iid: str) -> Path:
    if iid == "0":
        return DATA_DIR
    return DATA_DIR / f"instance_{iid}"


# ─── Instance discovery ──────────────────────────────────────

def _discover_instances() -> list[dict]:
    instances: list[dict] = []
    seen: set[str] = set()

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

        last_run = None
        lr_path = data_dir / "last_run.json"
        if lr_path.is_file():
            try:
                last_run = json.loads(lr_path.read_text("utf-8"))
            except Exception:
                pass

        running = (data_dir / "running").is_file()
        trigger_pending = (TRIGGER_DIR / f"trigger_instance_{iid}").is_file()

        # Read stats from SQLite
        stats = _read_stats(data_dir)

        instances.append({
            "id": iid,
            "base_url": meta.get("base_url", ""),
            "mode": meta.get("mode", "scan"),
            "interval": meta.get("interval", 1800),
            "running": running,
            "trigger_pending": trigger_pending,
            "last_run": last_run,
            "stats": stats,
        })

    return instances


def _trigger_instance(iid: str, mode: str) -> dict:
    if not iid.isalnum():
        return {"ok": False, "error": "invalid instance id"}
    if mode not in ("scan", "maintain"):
        return {"ok": False, "error": "mode must be 'scan' or 'maintain'"}

    data_dir = _resolve_data_dir(iid)
    if (data_dir / "running").is_file():
        return {"ok": False, "error": "instance is currently running"}

    trigger_path = TRIGGER_DIR / f"trigger_instance_{iid}"
    if trigger_path.is_file():
        return {"ok": False, "error": "trigger already pending"}

    trigger_path.write_text(mode, encoding="utf-8")
    return {"ok": True, "message": f"Instance {iid} triggered ({mode})"}


# ─── HTML dashboard ──────────────────────────────────────────

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
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(400px,1fr));gap:1rem}
.card{background:#1c1f26;border:1px solid #2d333b;border-radius:8px;padding:1.2rem;transition:border-color .15s}
.card:hover{border-color:#3d444d}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:.8rem}
.inst-name{font-weight:600;font-size:1.05rem}
.badge{padding:2px 10px;border-radius:12px;font-size:.75rem;font-weight:600;text-transform:uppercase}
.badge-scan{background:#1f3a5f;color:#58a6ff}
.badge-maintain{background:#3b2e1a;color:#f0883e}
.section-title{font-size:.8rem;color:#58a6ff;font-weight:600;margin-top:.8rem;margin-bottom:.4rem;padding-top:.5rem;border-top:1px solid #21262d}
.row{display:flex;justify-content:space-between;padding:.3rem 0;font-size:.84rem}
.lbl{color:#8b949e}
.val{text-align:right;max-width:60%;word-break:break-all}
.ok{color:#3fb950}.fail{color:#f85149}.running{color:#d29922}.idle{color:#6e7681}.warn{color:#d29922}
.stat-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:.4rem;margin:.4rem 0}
.stat-box{background:#161b22;border:1px solid #21262d;border-radius:6px;padding:.5rem;text-align:center}
.stat-num{font-size:1.2rem;font-weight:700}
.stat-label{font-size:.7rem;color:#8b949e;margin-top:.1rem}
.actions{display:flex;gap:.5rem;margin-top:.9rem}
.btn{flex:1;padding:.55rem;border:1px solid #2d333b;border-radius:6px;background:#21262d;color:#c9d1d9;font-size:.85rem;cursor:pointer;transition:background .12s}
.btn:hover{background:#30363d}
.btn:active{background:#3a414a}
.btn:disabled{opacity:.4;cursor:not-allowed}
.btn-scan{border-color:#1f3a5f}.btn-scan:hover:not(:disabled){background:#1f3a5f}
.btn-maintain{border-color:#3b2e1a}.btn-maintain:hover:not(:disabled){background:#3b2e1a}
.btn-log{border-color:#2d333b;font-size:.8rem}
.log-panel{display:none;margin-top:.8rem;background:#0d1117;border:1px solid #21262d;border-radius:6px;max-height:300px;overflow-y:auto;padding:.6rem;font-family:'Cascadia Code','Fira Code',monospace;font-size:.75rem;line-height:1.5;color:#8b949e;white-space:pre-wrap;word-break:break-all}
.log-panel.open{display:block}
.action-list{margin:.3rem 0;max-height:120px;overflow-y:auto}
.action-item{display:flex;justify-content:space-between;font-size:.78rem;padding:.2rem 0;border-bottom:1px solid #161b22}
.action-name{color:#c9d1d9;max-width:40%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
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
function fmt(s){if(s>=3600)return(s/3600)+'h';if(s>=60)return(s/60)+'m';return s+'s';}
function ago(iso){
  if(!iso)return '-';
  const d=Date.now()-new Date(iso.endsWith('Z')?iso:iso+'Z').getTime();
  if(d<0)return 'just now';const s=Math.floor(d/1000);
  if(s<60)return s+'s ago';if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago';return Math.floor(s/86400)+'d ago';
}
function nxt(iso,iv){
  if(!iso)return '-';
  const n=new Date(iso.endsWith('Z')?iso:iso+'Z').getTime()+iv*1000,d=n-Date.now();
  if(d<=0)return 'imminent';const s=Math.floor(d/1000);
  if(s<60)return s+'s';if(s<3600)return Math.floor(s/60)+'m '+s%60+'s';
  return Math.floor(s/3600)+'h '+Math.floor((s%3600)/60)+'m';
}
function n(v){return v==null?0:v;}

function render(instances){
  const g=document.getElementById('grid');
  if(!instances.length){g.innerHTML='<div class="loading">No instances found</div>';return;}
  g.innerHTML=instances.map(i=>{
    const lr=i.last_run,s=i.stats,a=s?s.accounts:null,sr=s?s.latest_run:null;
    let sc='idle',st='Waiting';
    if(i.running){sc='running';st='Running...';}
    else if(lr){if(lr.exit_code===0){sc='ok';st='Success';}else{sc='fail';st='Failed ('+lr.exit_code+')';}}
    const mc=i.mode==='maintain'?'badge-maintain':'badge-scan';
    const dis=i.running||i.trigger_pending?'disabled':'';
    const lrt=lr?lr.timestamp:null;

    let statsHtml='';
    if(a){
      statsHtml=`
      <div class="section-title">Accounts</div>
      <div class="stat-grid">
        <div class="stat-box"><div class="stat-num">${n(a.total)}</div><div class="stat-label">Total</div></div>
        <div class="stat-box"><div class="stat-num ok">${n(a.healthy)}</div><div class="stat-label">Healthy</div></div>
        <div class="stat-box"><div class="stat-num fail">${n(a.invalid_401)}</div><div class="stat-label">Invalid</div></div>
        <div class="stat-box"><div class="stat-num warn">${n(a.quota_limited)}</div><div class="stat-label">Quota</div></div>
        <div class="stat-box"><div class="stat-num ok">${n(a.recovered)}</div><div class="stat-label">Recovered</div></div>
        <div class="stat-box"><div class="stat-num idle">${n(a.disabled)}</div><div class="stat-label">Disabled</div></div>
      </div>`;
    }

    let scanHtml='';
    if(sr){
      scanHtml=`
      <div class="section-title">Last Scan #${sr.run_id}</div>
      <div class="row"><span class="lbl">Scanned</span><span class="val">${sr.probed_files} / ${sr.filtered_files} files</span></div>
      <div class="row"><span class="lbl">Found 401</span><span class="val fail">${sr.invalid_401_count}</span></div>
      <div class="row"><span class="lbl">Found Quota</span><span class="val warn">${sr.quota_limited_count}</span></div>
      <div class="row"><span class="lbl">Found Recovered</span><span class="val ok">${sr.recovered_count}</span></div>
      <div class="row"><span class="lbl">Finished</span><span class="val">${ago(sr.finished_at)}</span></div>`;
    }

    let actionsHtml='';
    if(s&&s.recent_actions&&s.recent_actions.length){
      actionsHtml=`<div class="section-title">Recent Actions</div><div class="action-list">`+
        s.recent_actions.map(a=>{
          const asc=a.last_action_status==='ok'?'ok':'fail';
          return `<div class="action-item"><span class="action-name" title="${a.name}">${a.name}</span><span>${a.last_action}</span><span class="${asc}">${a.last_action_status}</span><span class="idle">${ago(a.updated_at)}</span></div>`;
        }).join('')+`</div>`;
    }

    return `<div class="card">
      <div class="card-header">
        <span class="inst-name">Instance ${i.id}</span>
        <span class="badge ${mc}">${i.mode}</span>
      </div>
      <div class="row"><span class="lbl">Base URL</span><span class="val">${i.base_url}</span></div>
      <div class="row"><span class="lbl">Interval</span><span class="val">${fmt(i.interval)}</span></div>
      <div class="row"><span class="lbl">Status</span><span class="val ${sc}">${st}</span></div>
      <div class="row"><span class="lbl">Last Run</span><span class="val">${lr?(lr.mode+' &middot; '+ago(lrt)):'-'}</span></div>
      <div class="row"><span class="lbl">Next Run</span><span class="val">${i.running?'-':nxt(lrt,i.interval)}</span></div>
      ${statsHtml}${scanHtml}${actionsHtml}
      <div class="actions">
        <button class="btn btn-scan" ${dis} onclick="trigger('${i.id}','scan')">Scan</button>
        <button class="btn btn-maintain" ${dis} onclick="trigger('${i.id}','maintain')">Maintain</button>
        <button class="btn btn-log" onclick="toggleLog('${i.id}')">Logs</button>
      </div>
      <div class="log-panel" id="log-${i.id}"></div>
    </div>`;
  }).join('');
}

async function refresh(){
  try{const r=await fetch('/api/instances');const d=await r.json();render(d.instances||[]);}catch(e){console.error(e);}
  document.getElementById('clock').textContent=new Date().toLocaleString('zh-CN');
}

async function trigger(id,mode){
  try{
    const r=await fetch('/api/trigger/'+id,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})});
    const d=await r.json();if(!d.ok)alert(d.error);setTimeout(refresh,500);
  }catch(e){alert(e);}
}

async function toggleLog(id){
  const el=document.getElementById('log-'+id);
  if(el.classList.contains('open')){el.classList.remove('open');return;}
  el.textContent='Loading...';el.classList.add('open');
  try{
    const r=await fetch('/api/logs/'+id);const d=await r.json();
    el.textContent=d.logs||'No logs yet';
    el.scrollTop=el.scrollHeight;
  }catch(e){el.textContent='Failed to load logs';}
}

refresh();setInterval(refresh,10000);
</script>
</body>
</html>"""


# ─── HTTP handler ─────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/healthz":
            self._json(200, {"status": "ok"})
        elif path == "/":
            self._html(200, DASHBOARD_HTML)
        elif path == "/api/instances":
            self._json(200, {"instances": _discover_instances()})
        elif path.startswith("/api/logs/"):
            iid = path.rsplit("/", 1)[-1]
            qs = parse_qs(parsed.query)
            lines = min(int(qs.get("lines", ["80"])[0]), 500)
            if not iid.isalnum():
                self._json(400, {"error": "invalid id"})
                return
            data_dir = _resolve_data_dir(iid)
            logs = _read_logs(data_dir, lines)
            self._json(200, {"logs": logs})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path.startswith("/api/trigger/"):
            iid = self.path.rsplit("/", 1)[-1]
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
