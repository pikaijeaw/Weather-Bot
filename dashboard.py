#!/usr/bin/env python3
"""Weatherbot dashboard — http://<host>:5001  (Basic auth: user 'admin', DASHBOARD_PASSWORD in .env)"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

BOT_DIR = Path(__file__).resolve().parent
DATA = BOT_DIR / "data"
DATA.mkdir(exist_ok=True)
PIDF = DATA / "bot.pid"
LOG = DATA / "weatherbot.log"
POS = DATA / "positions.json"
PAPER_POS = DATA / "paper_positions.json"
PAPER_WALLET = DATA / "paper_wallet.json"
PY = BOT_DIR / "venv" / "bin" / "python"
PORT = 5001

load_dotenv(BOT_DIR / ".env")
PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
if not PASSWORD:
    sys.exit("Set DASHBOARD_PASSWORD in .env")


# ------------------------------------------------------------------ bot control
def bot_pid():
    try:
        pid = int(PIDF.read_text().strip())
        os.kill(pid, 0)
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore")
        return pid if "weatherbot.py" in cmd else None
    except Exception:
        return None


def start_bot():
    (DATA / ".manual_stop").unlink(missing_ok=True)
    if bot_pid():
        return "already running"
    p = subprocess.Popen([str(PY), "weatherbot.py", "run"], cwd=BOT_DIR,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    PIDF.write_text(str(p.pid))
    threading.Thread(target=p.wait, daemon=True).start()  # reap zombie
    return f"started (PID {p.pid})"


def stop_bot():
    (DATA / ".manual_stop").touch()  # watchdog won't restart a deliberately stopped bot
    pid = bot_pid()
    if not pid:
        PIDF.unlink(missing_ok=True)
        return "not running"
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        time.sleep(0.25)
        if not bot_pid():
            break
    else:
        os.kill(pid, signal.SIGKILL)
    PIDF.unlink(missing_ok=True)
    return "stopped"


def reset_bot():
    """Stop bot, archive local positions, paper wallet + log (paper restarts at paper_bankroll).
    Does NOT touch on-chain positions."""
    stop_bot()
    arch = DATA / "archive" / datetime.now().strftime("%Y%m%d-%H%M%S")
    moved = []
    for f in (POS, PAPER_POS, PAPER_WALLET, LOG):
        if f.exists():
            arch.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), arch / f.name)
            moved.append(f.name)
    return f"stopped; archived {', '.join(moved) or 'nothing'}" + (f" to data/archive/{arch.name}" if moved else "")


# ------------------------------------------------------------------ obsbot (separate paper bot)
OBS = BOT_DIR / "obsbot"
ODATA = OBS / "data"
OPIDF = ODATA / "bot.pid"


def obs_pid():
    try:
        pid = int(OPIDF.read_text().strip())
        os.kill(pid, 0)
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore")
        return pid if "obsbot.py" in cmd else None
    except Exception:
        return None


def obs_start():
    ODATA.mkdir(exist_ok=True)
    (ODATA / ".manual_stop").unlink(missing_ok=True)
    if obs_pid():
        return "obsbot already running"
    p = subprocess.Popen([str(PY), "obsbot/obsbot.py", "run"], cwd=BOT_DIR,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    OPIDF.write_text(str(p.pid))
    threading.Thread(target=p.wait, daemon=True).start()
    return f"obsbot started (PID {p.pid})"


def obs_stop():
    ODATA.mkdir(exist_ok=True)
    (ODATA / ".manual_stop").touch()
    pid = obs_pid()
    if not pid:
        OPIDF.unlink(missing_ok=True)
        return "obsbot not running"
    os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        time.sleep(0.25)
        if not obs_pid():
            break
    else:
        os.kill(pid, signal.SIGKILL)
    OPIDF.unlink(missing_ok=True)
    return "obsbot stopped"


def obs_reset():
    """Archive paper positions/wallet/log. Keeps learned.json + pending signals (the learning)."""
    obs_stop()
    arch = ODATA / "archive" / datetime.now().strftime("%Y%m%d-%H%M%S")
    moved = []
    for n in ("paper_positions.json", "paper_wallet.json", "obsbot.log"):
        f = ODATA / n
        if f.exists():
            arch.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), arch / n)
            moved.append(n)
    return f"obsbot stopped; archived {', '.join(moved) or 'nothing'} (learning kept)"


def _j(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def obs_state():
    cfg = _j(OBS / "config.json", {})
    pos = _j(ODATA / "paper_positions.json", {})
    w = _j(ODATA / "paper_wallet.json", {"balance": cfg.get("paper_bankroll", 100.0)})
    learned = _j(ODATA / "learned.json", {"stats": {}, "blocked": {}})
    lines = tail(ODATA / "obsbot.log")
    opn = [p for p in pos.values() if p["status"] == "open"]
    done = [p for p in pos.values() if p["status"] != "open"]
    start = max((i for i, l in enumerate(lines) if "=== scan |" in l), default=None)
    summary, gaps = None, []
    if start is not None:
        for l in lines[start:]:
            if "real-world signals" in l:
                summary = l[20:].replace("INFO ", "")
            m = re.search(r"(?:\[DRY\] would BUY|📝 PAPER BOUGHT|\[GAP\]) (.*)", l)
            if m:
                gaps.append(m.group(1))
    tiers = {}
    for p in done:
        t = tiers.setdefault(p["tier"], {"n": 0, "won": 0, "pnl": 0.0})
        t["n"] += 1
        t["won"] += p["status"] == "won"
        t["pnl"] = round(t["pnl"] + p["pnl"], 2)
    return {
        "running": obs_pid() is not None, "balance": w["balance"], "start": cfg.get("paper_bankroll"),
        "open": opn, "settled": done, "pnl": round(sum(p["pnl"] for p in done), 2),
        "wins": sum(p["status"] == "won" for p in done),
        "exposure": round(sum(p["cost"] for p in opn), 2), "tiers": tiers,
        "pending": len(_j(ODATA / "signals.json", {})),
        "learned": sorted(([k, v[0], v[1]] for k, v in learned["stats"].items()),
                          key=lambda x: -(x[1] + x[2]))[:40],
        "blocked": learned.get("blocked", {}), "summary": summary, "gaps": gaps[:40],
        "last_scan": lines[start][:19] if start is not None else None, "log": lines[-60:],
    }


# ------------------------------------------------------------------ data
_bal = {"t": 0, "v": None, "err": None}


def balance():
    if time.time() - _bal["t"] > 120:
        _bal["t"] = time.time()

        def work():
            try:
                out = subprocess.run([str(PY), "-c",
                                      "import weatherbot as w;print(w.pusd_balance())"],
                                     cwd=BOT_DIR, capture_output=True, text=True, timeout=60)
                _bal["v"] = float(out.stdout.strip().splitlines()[-1])
                _bal["err"] = None
            except Exception as e:
                _bal["err"] = str(e)[:100]
        threading.Thread(target=work, daemon=True).start()
    return _bal["v"]


def tail(path: Path, n=400):
    if not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 200_000))
        return f.read().decode(errors="ignore").splitlines()[-n:]


def state():
    cfg = json.loads((BOT_DIR / "config.json").read_text())
    paper = cfg.get("mode") != "live"
    pf = PAPER_POS if paper else POS
    pos = json.loads(pf.read_text()) if pf.exists() else {}
    paper_bal = None
    if paper:
        paper_bal = (json.loads(PAPER_WALLET.read_text())["balance"] if PAPER_WALLET.exists()
                     else cfg.get("paper_bankroll", 100.0))
    lines = tail(LOG)
    opn = [p for p in pos.values() if p["status"] == "open"]
    done = [p for p in pos.values() if p["status"] != "open"]
    # signals from the most recent completed scan
    last_scan_start = max((i for i, l in enumerate(lines) if "=== scan |" in l), default=None)
    signals, last_scan = [], None
    if last_scan_start is not None:
        last_scan = lines[last_scan_start][:19]
        for l in lines[last_scan_start:]:
            m = re.search(r"(?:\[DRY\] would BUY|✅ BOUGHT|📝 PAPER BOUGHT) (.*)", l)
            if m:
                signals.append(m.group(1))
    return {
        "running": bot_pid() is not None, "pid": bot_pid(), "mode": cfg.get("mode"),
        "balance": balance(), "balance_err": _bal["err"],
        "paper_balance": paper_bal, "paper_start": cfg.get("paper_bankroll"),
        "open": opn, "settled": done,
        "pnl": round(sum(p["pnl"] or 0 for p in done), 2),
        "wins": sum(1 for p in done if p["status"] == "won"),
        "exposure": round(sum(p["cost"] for p in opn), 2),
        "last_scan": last_scan, "signals": signals[:40],
        "log": lines[-80:],
        "obs": obs_state(),
        "cfg": {k: cfg[k] for k in ("min_edge", "max_edge", "max_bet_usd",
                                     "max_total_exposure_usd", "scan_interval_sec") if k in cfg},
    }


# ------------------------------------------------------------------ http
PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Weatherbot + Obsbot</title><style>
body{font:14px system-ui,sans-serif;background:#0f1419;color:#d8dee9;margin:0;padding:20px}
h1{font-size:20px;margin:0 0 16px}h2{font-size:15px;margin:22px 0 8px;color:#88c0d0}
.cards{display:flex;gap:12px;flex-wrap:wrap}.card{background:#1b222c;padding:12px 16px;border-radius:8px;min-width:130px}
.card b{display:block;font-size:20px;margin-top:4px}.k{color:#7b8794;font-size:12px}
button{font:inherit;padding:9px 20px;border:0;border-radius:6px;margin-right:8px;cursor:pointer;color:#fff}
#start,#ostart{background:#2e7d32}#stop,#ostop{background:#c62828}#reset,#oreset{background:#6d4c41}button:disabled{opacity:.4}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:5px 8px;border-bottom:1px solid #2a323d;text-align:left}
pre{background:#0a0d11;padding:10px;border-radius:6px;max-height:340px;overflow:auto;font-size:12px;white-space:pre-wrap}
.on{color:#66bb6a}.off{color:#ef5350}.live{color:#ffa726}#msg{margin-left:10px;color:#ffd54f}
</style></head><body>
<h1>🌦 Weatherbot</h1>
<div><button id=start onclick="act('start')">Start</button><button id=stop onclick="act('stop')">Stop</button>
<button id=reset onclick="if(confirm('Stop the bot and archive local positions + log? On-chain positions are not affected.'))act('reset')">Reset</button><span id=msg></span></div>
<h2>Status</h2><div class=cards id=cards></div>
<h2>Open positions</h2><table id=open></table>
<h2>Signals from last scan <span class=k id=ls></span></h2><table id=sig></table>
<h2>Settled</h2><table id=done></table>
<h2>Log</h2><pre id=log></pre>
<hr style="border-color:#2a323d;margin:30px 0">
<h1>🔭 Obsbot <span class=k>real-world vs Polymarket gaps · paper only · self-learning</span></h1>
<div><button id=ostart onclick="act('obs_start')">Start</button><button id=ostop onclick="act('obs_stop')">Stop</button>
<button id=oreset onclick="if(confirm('Stop obsbot and archive its paper wallet/positions/log? Learned stats are kept.'))act('obs_reset')">Reset</button></div>
<h2>Status</h2><div class=cards id=ocards></div>
<h2>Last scan <span class=k id=ols></span></h2><div class=k id=osum></div><table id=ogaps></table>
<h2>Open positions</h2><table id=oopen></table>
<h2>Results by tier</h2><table id=otier></table>
<h2>Learned (signal key → real outcome)</h2><table id=olearn></table><div id=oblock class=off></div>
<h2>Settled</h2><table id=odone></table>
<h2>Log</h2><pre id=olog></pre>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function act(a){$('msg').textContent='…';const r=await fetch('/api/'+a,{method:'POST',headers:{'X-Req':'1'}});
 $('msg').textContent=(await r.json()).result;load()}
async function load(){const s=await (await fetch('/api/state')).json();
 const card=(k,v,c='')=>`<div class=card><span class=k>${k}</span><b class="${c}">${v}</b></div>`;
 $('cards').innerHTML=card('Bot',s.running?'RUNNING':'STOPPED',s.running?'on':'off')+
  card('Mode',s.mode.toUpperCase(),s.mode=='live'?'live':'')+
  (s.paper_balance!=null?card('Paper $',`$${s.paper_balance.toFixed(2)} <span class=k>/ $${s.paper_start}</span>`):'')+
  card('pUSD',s.balance==null?(s.balance_err?'error':'…'):'$'+s.balance.toFixed(2))+
  card('Exposure','$'+s.exposure.toFixed(2))+card('Open',s.open.length)+
  card('Settled',`${s.wins}/${s.settled.length} won`)+card('PnL',(s.pnl>=0?'+$':'-$')+Math.abs(s.pnl).toFixed(2),s.pnl>=0?'on':'off');
 $('start').disabled=s.running;$('stop').disabled=!s.running;
 $('open').innerHTML='<tr><th>Market</th><th>Shares</th><th>Price</th><th>Cost</th><th>Model p</th></tr>'+
  (s.open.map(p=>`<tr><td>${esc(p.title)}</td><td>${p.shares.toFixed(2)}</td><td>${p.price.toFixed(3)}</td><td>$${p.cost.toFixed(2)}</td><td>${p.p}</td></tr>`).join('')||'<tr><td colspan=5 class=k>none</td></tr>');
 $('done').innerHTML='<tr><th>Market</th><th>Result</th><th>PnL</th></tr>'+
  (s.settled.slice(-30).reverse().map(p=>`<tr><td>${esc(p.title)}</td><td class=${p.status=='won'?'on':'off'}>${p.status}</td><td>${p.pnl.toFixed(2)}</td></tr>`).join('')||'<tr><td colspan=3 class=k>none</td></tr>');
 $('ls').textContent=s.last_scan?'('+s.last_scan+')':'';
 $('sig').innerHTML=s.signals.map(x=>`<tr><td>${esc(x)}</td></tr>`).join('')||'<tr><td class=k>none</td></tr>';
 const lg=$('log'),atBottom=lg.scrollTop+lg.clientHeight>=lg.scrollHeight-20;
 lg.textContent=s.log.join('\n');if(atBottom)lg.scrollTop=lg.scrollHeight;
const o=s.obs;
 $('ocards').innerHTML=card('Bot',o.running?'RUNNING':'STOPPED',o.running?'on':'off')+card('Mode','PAPER')+
  card('Paper $',`$${o.balance.toFixed(2)} <span class=k>/ $${o.start}</span>`)+card('Exposure','$'+o.exposure.toFixed(2))+
  card('Open',o.open.length)+card('Settled',`${o.wins}/${o.settled.length} won`)+
  card('PnL',(o.pnl>=0?'+$':'-$')+Math.abs(o.pnl).toFixed(2),o.pnl>=0?'on':'off')+card('Signals learning',o.pending);
 $('ostart').disabled=o.running;$('ostop').disabled=!o.running;
 $('ols').textContent=o.last_scan?'('+o.last_scan+')':'';$('osum').textContent=o.summary||'';
 $('ogaps').innerHTML=o.gaps.map(x=>`<tr><td>${esc(x)}</td></tr>`).join('')||'<tr><td class=k>no mispriced buckets</td></tr>';
 $('oopen').innerHTML='<tr><th>Market</th><th>Tier</th><th>Obs</th><th>Shares</th><th>Price</th><th>Cost</th><th>p≥</th></tr>'+
  (o.open.map(p=>`<tr><td>${esc(p.title)}</td><td>${p.tier}</td><td>${p.obs}</td><td>${p.shares.toFixed(2)}</td><td>${p.price.toFixed(3)}</td><td>$${p.cost.toFixed(2)}</td><td>${p.p_lb}</td></tr>`).join('')||'<tr><td colspan=7 class=k>none</td></tr>');
 $('otier').innerHTML='<tr><th>Tier</th><th>Trades</th><th>Won</th><th>PnL</th></tr>'+
  (Object.entries(o.tiers).map(([k,t])=>`<tr><td>${k}</td><td>${t.n}</td><td>${t.won}</td><td>${t.pnl.toFixed(2)}</td></tr>`).join('')||'<tr><td colspan=4 class=k>none yet</td></tr>');
 $('olearn').innerHTML='<tr><th>Key (tier|kind|side|margin|hour)</th><th>Right</th><th>Wrong</th></tr>'+
  (o.learned.map(x=>`<tr><td>${esc(x[0])}</td><td class=on>${x[1]}</td><td class=off>${x[2]}</td></tr>`).join('')||'<tr><td colspan=3 class=k>nothing resolved yet</td></tr>');
 $('oblock').textContent=Object.keys(o.blocked).length?'Blocked stations: '+Object.entries(o.blocked).map(([k,v])=>k+' ('+v+')').join('; '):'';
 $('odone').innerHTML='<tr><th>Market</th><th>Tier</th><th>Result</th><th>PnL</th></tr>'+
  (o.settled.slice(-30).reverse().map(p=>`<tr><td>${esc(p.title)}</td><td>${p.tier}</td><td class=${p.status=='won'?'on':'off'}>${p.status}</td><td>${p.pnl.toFixed(2)}</td></tr>`).join('')||'<tr><td colspan=4 class=k>none</td></tr>');
 const ol=$('olog'),ob=ol.scrollTop+ol.clientHeight>=ol.scrollHeight-20;ol.textContent=o.log.join('\n');if(ob)ol.scrollTop=ol.scrollHeight;}
load();setInterval(load,5000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _auth(self):
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode().partition(":")
                if user == "admin" and hmac.compare_digest(pw, PASSWORD):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="weatherbot"')
        self.end_headers()
        return False

    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self._auth():
            return
        if self.path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._send(200, json.dumps(state()))
        else:
            self._send(404, "{}")

    def do_POST(self):
        if not self._auth():
            return
        if self.headers.get("X-Req") != "1":  # blocks cross-site form posts
            return self._send(403, '{"result":"forbidden"}')
        fn = {"/api/start": start_bot, "/api/stop": stop_bot, "/api/reset": reset_bot,
              "/api/obs_start": obs_start, "/api/obs_stop": obs_stop,
              "/api/obs_reset": obs_reset}.get(self.path)
        if not fn:
            return self._send(404, "{}")
        self._send(200, json.dumps({"result": fn()}))


if __name__ == "__main__":
    print(f"dashboard on :{PORT}")
    #ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
