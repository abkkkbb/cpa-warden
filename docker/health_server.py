"""CPA Warden — 中文 Web 控制面板 & 健康检查服务。"""

import json
import os
import secrets
import sqlite3
from collections import deque
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DATA_DIR = Path(os.getenv("CPA_DATA_DIR", "/data"))
TRIGGER_DIR = Path("/tmp")

# 密码保护：设置环境变量 DASHBOARD_PASSWORD 启用，不设则不需要登录
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

# 活跃会话令牌（内存中，重启即失效）
_sessions: set[str] = set()


# ─── 认证 ─────────────────────────────────────────────────────

def _auth_enabled() -> bool:
    return len(DASHBOARD_PASSWORD) > 0


def _check_session(cookie_header: str | None) -> bool:
    if not _auth_enabled():
        return True
    if not cookie_header:
        return False
    cookie = SimpleCookie()
    cookie.load(cookie_header)
    token_morsel = cookie.get("cpa_session")
    return token_morsel is not None and token_morsel.value in _sessions


def _create_session() -> str:
    token = secrets.token_hex(24)
    _sessions.add(token)
    return token


# ─── SQLite 读取 ──────────────────────────────────────────────

def _read_stats(data_dir: Path) -> dict | None:
    db_path = data_dir / "cpa_warden_state.sqlite3"
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM scan_runs ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            latest_run = dict(row) if row else None

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


# ─── 实例发现 ─────────────────────────────────────────────────

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
        return {"ok": False, "error": "无效的实例 ID"}
    if mode not in ("scan", "maintain"):
        return {"ok": False, "error": "模式必须是 scan 或 maintain"}

    data_dir = _resolve_data_dir(iid)
    if (data_dir / "running").is_file():
        return {"ok": False, "error": "实例正在运行中"}

    trigger_path = TRIGGER_DIR / f"trigger_instance_{iid}"
    if trigger_path.is_file():
        return {"ok": False, "error": "已有待执行的触发"}

    trigger_path.write_text(mode, encoding="utf-8")
    return {"ok": True, "message": f"实例 {iid} 已触发（{mode}）"}


# ─── HTML 页面 ────────────────────────────────────────────────

LOGIN_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CPA Warden - 登录</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e1e4e8;min-height:100vh;display:flex;align-items:center;justify-content:center}
.login-box{background:#1c1f26;border:1px solid #2d333b;border-radius:10px;padding:2.5rem;width:340px}
h2{font-size:1.2rem;text-align:center;margin-bottom:1.5rem;color:#f0f3f6}
input{width:100%;padding:.7rem;border:1px solid #2d333b;border-radius:6px;background:#0d1117;color:#e1e4e8;font-size:.9rem;margin-bottom:1rem;outline:none}
input:focus{border-color:#58a6ff}
button{width:100%;padding:.7rem;border:none;border-radius:6px;background:#238636;color:#fff;font-size:.9rem;cursor:pointer;font-weight:600}
button:hover{background:#2ea043}
.err{color:#f85149;font-size:.8rem;text-align:center;margin-bottom:.8rem;display:none}
</style>
</head>
<body>
<div class="login-box">
<h2>CPA Warden</h2>
<div class="err" id="err">密码错误</div>
<form onsubmit="return login(event)">
<input type="password" id="pwd" placeholder="请输入访问密码" autofocus>
<button type="submit">登 录</button>
</form>
</div>
<script>
async function login(e){
  e.preventDefault();
  const pwd=document.getElementById('pwd').value;
  const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pwd})});
  if(r.ok){location.href='/';}
  else{document.getElementById('err').style.display='block';document.getElementById('pwd').value='';}
  return false;
}
</script>
</body>
</html>"""

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
<h1>CPA Warden 控制面板</h1>
<p class="subtitle" id="clock"></p>
<div class="grid" id="grid"><div class="loading">加载中...</div></div>
<div class="footer">每 10 秒自动刷新</div>
<script>
function fmt(s){if(s>=3600)return(s/3600)+'h';if(s>=60)return(s/60)+'m';return s+'s';}
function ago(iso){
  if(!iso)return '-';
  const d=Date.now()-new Date(iso.endsWith('Z')?iso:iso+'Z').getTime();
  if(d<0)return '刚刚';const s=Math.floor(d/1000);
  if(s<60)return s+'秒前';if(s<3600)return Math.floor(s/60)+'分钟前';
  if(s<86400)return Math.floor(s/3600)+'小时前';return Math.floor(s/86400)+'天前';
}
function nxt(iso,iv){
  if(!iso)return '-';
  const n=new Date(iso.endsWith('Z')?iso:iso+'Z').getTime()+iv*1000,d=n-Date.now();
  if(d<=0)return '即将执行';const s=Math.floor(d/1000);
  if(s<60)return s+'秒';if(s<3600)return Math.floor(s/60)+'分'+s%60+'秒';
  return Math.floor(s/3600)+'时'+Math.floor((s%3600)/60)+'分';
}
function n(v){return v==null?0:v;}

function render(instances){
  const g=document.getElementById('grid');
  if(!instances.length){g.innerHTML='<div class="loading">未发现实例</div>';return;}
  g.innerHTML=instances.map(i=>{
    const lr=i.last_run,s=i.stats,a=s?s.accounts:null,sr=s?s.latest_run:null;
    let sc='idle',st='等待中';
    if(i.running){sc='running';st='运行中...';}
    else if(lr){if(lr.exit_code===0){sc='ok';st='成功';}else{sc='fail';st='失败 ('+lr.exit_code+')';}}
    const mc=i.mode==='maintain'?'badge-maintain':'badge-scan';
    const dis=i.running||i.trigger_pending?'disabled':'';
    const lrt=lr?lr.timestamp:null;

    let statsHtml='';
    if(a){
      statsHtml=`
      <div class="section-title">账号统计</div>
      <div class="stat-grid">
        <div class="stat-box"><div class="stat-num">${n(a.total)}</div><div class="stat-label">总计</div></div>
        <div class="stat-box"><div class="stat-num ok">${n(a.healthy)}</div><div class="stat-label">健康</div></div>
        <div class="stat-box"><div class="stat-num fail">${n(a.invalid_401)}</div><div class="stat-label">失效</div></div>
        <div class="stat-box"><div class="stat-num warn">${n(a.quota_limited)}</div><div class="stat-label">配额耗尽</div></div>
        <div class="stat-box"><div class="stat-num ok">${n(a.recovered)}</div><div class="stat-label">已恢复</div></div>
        <div class="stat-box"><div class="stat-num idle">${n(a.disabled)}</div><div class="stat-label">已禁用</div></div>
      </div>`;
    }

    let scanHtml='';
    if(sr){
      scanHtml=`
      <div class="section-title">最近扫描 #${sr.run_id}</div>
      <div class="row"><span class="lbl">已扫描</span><span class="val">${sr.probed_files} / ${sr.filtered_files} 个文件</span></div>
      <div class="row"><span class="lbl">发现失效</span><span class="val fail">${sr.invalid_401_count}</span></div>
      <div class="row"><span class="lbl">发现配额耗尽</span><span class="val warn">${sr.quota_limited_count}</span></div>
      <div class="row"><span class="lbl">发现已恢复</span><span class="val ok">${sr.recovered_count}</span></div>
      <div class="row"><span class="lbl">完成时间</span><span class="val">${ago(sr.finished_at)}</span></div>`;
    }

    let actionsHtml='';
    if(s&&s.recent_actions&&s.recent_actions.length){
      actionsHtml=`<div class="section-title">最近操作</div><div class="action-list">`+
        s.recent_actions.map(a=>{
          const asc=a.last_action_status==='ok'?'ok':'fail';
          return `<div class="action-item"><span class="action-name" title="${a.name}">${a.name}</span><span>${a.last_action}</span><span class="${asc}">${a.last_action_status}</span><span class="idle">${ago(a.updated_at)}</span></div>`;
        }).join('')+`</div>`;
    }

    return `<div class="card">
      <div class="card-header">
        <span class="inst-name">实例 ${i.id}</span>
        <span class="badge ${mc}">${i.mode==='maintain'?'维护':'扫描'}</span>
      </div>
      <div class="row"><span class="lbl">地址</span><span class="val">${i.base_url}</span></div>
      <div class="row"><span class="lbl">执行间隔</span><span class="val">${fmt(i.interval)}</span></div>
      <div class="row"><span class="lbl">状态</span><span class="val ${sc}">${st}</span></div>
      <div class="row"><span class="lbl">上次运行</span><span class="val">${lr?((lr.mode==='maintain'?'维护':'扫描')+' · '+ago(lrt)):'-'}</span></div>
      <div class="row"><span class="lbl">下次运行</span><span class="val">${i.running?'-':nxt(lrt,i.interval)}</span></div>
      ${statsHtml}${scanHtml}${actionsHtml}
      <div class="actions">
        <button class="btn btn-scan" ${dis} onclick="trigger('${i.id}','scan')">扫描</button>
        <button class="btn btn-maintain" ${dis} onclick="trigger('${i.id}','maintain')">维护</button>
        <button class="btn btn-log" onclick="toggleLog('${i.id}')">日志</button>
      </div>
      <div class="log-panel" id="log-${i.id}"></div>
    </div>`;
  }).join('');
}

async function refresh(){
  try{
    const r=await fetch('/api/instances');
    if(r.status===401){location.href='/login';return;}
    const d=await r.json();render(d.instances||[]);
  }catch(e){console.error(e);}
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
  el.textContent='加载中...';el.classList.add('open');
  try{
    const r=await fetch('/api/logs/'+id);const d=await r.json();
    el.textContent=d.logs||'暂无日志';
    el.scrollTop=el.scrollHeight;
  }catch(e){el.textContent='日志加载失败';}
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

        # 健康检查始终公开
        if path == "/healthz":
            self._json(200, {"status": "ok"})
            return

        # 登录页
        if path == "/login":
            if not _auth_enabled():
                self._redirect("/")
                return
            self._html(200, LOGIN_HTML)
            return

        # 需要认证的路由
        if not _check_session(self.headers.get("Cookie")):
            if path == "/":
                self._redirect("/login")
            else:
                self._json(401, {"error": "未登录"})
            return

        if path == "/":
            self._html(200, DASHBOARD_HTML)
        elif path == "/api/instances":
            self._json(200, {"instances": _discover_instances()})
        elif path.startswith("/api/logs/"):
            iid = path.rsplit("/", 1)[-1]
            qs = parse_qs(parsed.query)
            lines = min(int(qs.get("lines", ["80"])[0]), 500)
            if not iid.isalnum():
                self._json(400, {"error": "无效 ID"})
                return
            data_dir = _resolve_data_dir(iid)
            logs = _read_logs(data_dir, lines)
            self._json(200, {"logs": logs})
        else:
            self._json(404, {"error": "未找到"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        # 登录接口
        if path == "/login":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(body)
            except Exception:
                payload = {}
            password = payload.get("password", "")
            if password == DASHBOARD_PASSWORD:
                token = _create_session()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header(
                    "Set-Cookie",
                    f"cpa_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400",
                )
                raw = b'{"ok":true}'
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            else:
                self._json(403, {"ok": False, "error": "密码错误"})
            return

        # 需要认证
        if not _check_session(self.headers.get("Cookie")):
            self._json(401, {"error": "未登录"})
            return

        if path.startswith("/api/trigger/"):
            iid = path.rsplit("/", 1)[-1]
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
            self._json(404, {"error": "未找到"})

    # ── 响应工具方法 ──────────────────────────────────────────

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

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def log_message(self, _fmt: str, *_args: object) -> None:
        return


# ─── Main ─────────────────────────────────────────────────────

def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    if _auth_enabled():
        print(f"[dashboard] Password protection enabled")
    else:
        print(f"[dashboard] No password set (DASHBOARD_PASSWORD), open access")
    server = ThreadingHTTPServer(("0.0.0.0", port), _Handler)
    print(f"[dashboard] Listening on :{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
