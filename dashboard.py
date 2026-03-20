"""
WEB DASHBOARD — ai_trading_agent_v2.py için eklenti
=====================================================
Bu dosyayı aynı repoya ekle ve ai_trading_agent_v2.py'deki
main() fonksiyonunun başına şu satırı ekle:

    from dashboard import start_dashboard
    start_dashboard()

Railway otomatik PORT verecek, public URL'den erişebilirsin.
"""

import json
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

PORT = int(os.environ.get("PORT", "8080"))

PORTFOLIO_FILE = "portfolio.json"
TRADES_FILE = "trades.json"
STATE_FILE = "agent_state.json"

# Global — ana bot tarafından güncellenir
scan_data = {"assets": {}, "signals": [], "scan_number": 0, "last_scan_time": None}


def load_json(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return default


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AI Trading Agent</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;700&family=DM+Sans:wght@400;500;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
body{background:#08080d;color:#d0d0d0;font-family:'DM Sans',sans-serif;min-height:100vh}
.hdr{background:#0c0c14;border-bottom:1px solid #1e1e30;padding:16px 24px;display:flex;justify-content:space-between;align-items:center}
.hdr h1{font-size:1.2rem;font-weight:700}.hdr h1 span{color:#00e676}
.hdr .st{font-family:'JetBrains Mono',monospace;font-size:.75rem;color:#666}
.live{color:#00e676;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.g{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;padding:16px 24px}
@media(max-width:900px){.g{grid-template-columns:1fr}}
.c{background:#0e0e18;border:1px solid #1e1e30;border-radius:10px;padding:16px}
.c:hover{border-color:#00e676}
.c h2{font-size:.75rem;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
.big{font-family:'JetBrains Mono',monospace;font-size:1.8rem;font-weight:700}
.sub{font-size:.8rem;color:#666;margin-top:4px}
.gr{color:#00e676}.rd{color:#ff3d5a}.yl{color:#ffa726}
.fw{grid-column:1/-1}.tc{grid-column:span 2}
@media(max-width:900px){.tc{grid-column:span 1}}
table{width:100%;border-collapse:collapse;font-family:'JetBrains Mono',monospace;font-size:.75rem}
th{text-align:left;color:#555;font-weight:500;padding:6px 10px;border-bottom:1px solid #1e1e30}
td{padding:6px 10px;border-bottom:1px solid #131320}
tr:hover{background:#13131f}
.b{display:inline-block;padding:2px 6px;border-radius:3px;font-size:.65rem;font-weight:700}
.b.lo{background:#00e67620;color:#00e676}.b.sh{background:#ff3d5a20;color:#ff3d5a}
.b.bu{background:#00e67610;color:#00e676;border:1px solid #00e67630}
.b.be{background:#ff3d5a10;color:#ff3d5a;border:1px solid #ff3d5a30}
.b.ne{background:#55555510;color:#555;border:1px solid #55555530}
.mt{height:5px;background:#131320;border-radius:3px;margin-top:6px;overflow:hidden}
.mt .f{height:100%;border-radius:3px;transition:width .5s}
.btn{background:#00e67618;color:#00e676;border:1px solid #00e67630;padding:6px 14px;border-radius:5px;cursor:pointer;font-family:inherit;font-size:.75rem}
.btn:hover{background:#00e67630}
.sc{background:#13131f;border:1px solid #1e1e30;border-radius:6px;padding:10px;margin-bottom:6px}
</style>
</head>
<body>
<div class="hdr">
<div><h1>🤖 AI Trading <span>Agent</span></h1><div class="st">v2.1 HYBRID — Mekanik + Claude Filtre</div></div>
<div style="text-align:right"><div class="live">● CANLI</div><div class="st" id="ls">...</div><button class="btn" onclick="f()">↻</button></div>
</div>
<div class="g">
<div class="c"><h2>Portföy</h2><div class="big" id="tv">$10,000</div><div class="sub" id="tp">+$0</div><div class="mt"><div class="f" id="pm" style="width:50%;background:#00e676"></div></div></div>
<div class="c"><h2>Nakit</h2><div class="big" id="ca">$10,000</div><div class="sub" id="pc">0 pozisyon</div></div>
<div class="c"><h2>Win Rate</h2><div class="big" id="wr">--%</div><div class="sub" id="pf">PF: --</div><div class="sub" id="tc">0 trade</div></div>
<div class="c tc"><h2>Açık Pozisyonlar</h2><table><thead><tr><th>Asset</th><th>Yön</th><th>Giriş</th><th>Boyut</th><th>SL</th><th>TP</th><th>Skor</th><th>Güven</th></tr></thead><tbody id="pt"><tr><td colspan="8" style="color:#444">Pozisyon yok</td></tr></tbody></table></div>
<div class="c"><h2>Son Sinyaller</h2><div id="sl"><div class="sc" style="color:#444">Henüz yok</div></div></div>
<div class="c fw"><h2>Market Durumu</h2><table><thead><tr><th>Asset</th><th>Fiyat</th><th>Değişim</th><th>RSI</th><th>Trend</th><th>Cross</th><th>MACD</th><th>Sinyal</th></tr></thead><tbody id="at"><tr><td colspan="8" style="color:#444">Yükleniyor...</td></tr></tbody></table></div>
<div class="c fw"><h2>Trade Geçmişi</h2><table><thead><tr><th>#</th><th>Asset</th><th>Yön</th><th>Giriş</th><th>Çıkış</th><th>P&L</th><th>%</th><th>Sebep</th><th>Tarih</th></tr></thead><tbody id="tt"><tr><td colspan="9" style="color:#444">Henüz trade yok</td></tr></tbody></table></div>
</div>
<script>
async function f(){
try{
const[p,t,s]=await Promise.all([fetch('/api/portfolio').then(r=>r.json()),fetch('/api/trades').then(r=>r.json()),fetch('/api/scan').then(r=>r.json())]);
const tot=p.cash+Object.values(p.positions||{}).reduce((a,b)=>a+b.size_usd,0);
const pnl=tot-10000,pp=pnl/100;
document.getElementById('tv').textContent='$'+tot.toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2});
const tpe=document.getElementById('tp');tpe.className='sub '+(pnl>=0?'gr':'rd');tpe.textContent=(pnl>=0?'+':'')+' $'+pnl.toFixed(2)+' ('+pp.toFixed(1)+'%)';
document.getElementById('ca').textContent='$'+p.cash.toLocaleString('en',{minimumFractionDigits:2});
const ps=Object.keys(p.positions||{}).length;
document.getElementById('pc').textContent=ps+' pozisyon';
const m=document.getElementById('pm');m.style.width=Math.min(Math.max((pp+10)/20*100,5),100)+'%';m.style.background=pnl>=0?'#00e676':'#ff3d5a';
// positions
const pe=document.getElementById('pt');
if(ps>0){pe.innerHTML=Object.entries(p.positions).map(([k,v])=>'<tr><td><b>'+k+'</b></td><td><span class="b '+(v.direction==='LONG'?'lo':'sh')+'">'+v.direction+'</span></td><td>$'+v.entry_price+'</td><td>$'+v.size_usd.toFixed(0)+'</td><td>$'+v.stop_loss+'</td><td>$'+v.take_profit+'</td><td>'+(v.signal_score||'?')+'</td><td>'+(v.confidence||'?')+'%</td></tr>').join('')}
else{pe.innerHTML='<tr><td colspan="8" style="color:#444">Pozisyon yok</td></tr>'}
// stats
const cl=t.filter(x=>x.status==='CLOSED'),w=cl.filter(x=>(x.pnl||0)>0);
const wr=cl.length?(w.length/cl.length*100).toFixed(0):'--';
const ws=w.reduce((a,b)=>a+b.pnl,0),ls=Math.abs(cl.filter(x=>(x.pnl||0)<=0).reduce((a,b)=>a+(b.pnl||0),0));
document.getElementById('wr').textContent=wr+'%';document.getElementById('wr').className='big '+(parseFloat(wr)>=55?'gr':parseFloat(wr)>=45?'yl':'rd');
document.getElementById('pf').textContent='PF: '+(ls>0?(ws/ls).toFixed(2):'--');
document.getElementById('tc').textContent=cl.length+' trade ('+w.length+'W/'+(cl.length-w.length)+'L)';
// trades
const te=document.getElementById('tt');
if(cl.length>0){te.innerHTML=cl.slice(-12).reverse().map(x=>{const iw=(x.pnl||0)>0;return'<tr><td>#'+x.id+'</td><td><b>'+x.ticker+'</b></td><td><span class="b '+(x.direction==='LONG'?'lo':'sh')+'">'+x.direction+'</span></td><td>$'+x.entry_price+'</td><td>$'+(x.exit_price||'?')+'</td><td class="'+(iw?'gr':'rd')+'">$'+(x.pnl||0).toFixed(2)+'</td><td class="'+(iw?'gr':'rd')+'">'+(x.pnl_pct||0).toFixed(1)+'%</td><td>'+(x.close_reason||'?')+'</td><td>'+(x.closed_at||'').slice(0,16)+'</td></tr>'}).join('')}
// assets
if(s.assets){const ae=document.getElementById('at');const en=Object.entries(s.assets);if(en.length>0){ae.innerHTML=en.map(([k,a])=>{const tc=a.trend==='BULLISH'?'bu':a.trend==='BEARISH'?'be':'ne';const cc=(a.change_pct||0)>=0?'gr':'rd';return'<tr><td><b>'+k+'</b></td><td>$'+(a.price||0).toLocaleString('en',{maximumFractionDigits:2})+'</td><td class="'+cc+'">'+(a.change_pct>=0?'+':'')+a.change_pct+'%</td><td>'+(a.rsi||'-')+'</td><td><span class="b '+tc+'">'+a.trend+'</span></td><td>'+(a.ema_cross_recent||a.cross||'-')+'</td><td>'+(a.macd_hist?(a.macd_hist>0?'▲':'▼')+' '+a.macd_hist:'-')+'</td><td>'+(a.signal_summary||'-')+'</td></tr>'}).join('')}}
// signals
if(s.signals&&s.signals.length>0){document.getElementById('sl').innerHTML=s.signals.map(x=>'<div class="sc"><b class="'+(x.direction==='LONG'?'gr':'rd')+'">'+x.direction+' '+x.ticker+'</b> Skor:'+x.score+' RSI:'+x.rsi+'<div style="color:#555;font-size:.7rem;margin-top:3px">'+(x.reasons||[]).join(' · ')+'</div></div>').join('')}
document.getElementById('ls').textContent='Tarama #'+(s.scan_number||0)+' | '+(s.last_scan_time||'?');
}catch(e){console.error(e)}}
f();setInterval(f,30000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "":
            self._html(DASHBOARD_HTML)
        elif path == "/api/portfolio":
            self._json(load_json(PORTFOLIO_FILE, {"cash": 10000, "positions": {}}))
        elif path == "/api/trades":
            self._json(load_json(TRADES_FILE, []))
        elif path == "/api/state":
            self._json(load_json(STATE_FILE, {}))
        elif path == "/api/scan":
            self._json(scan_data)
        else:
            self.send_response(404); self.end_headers()

    def _html(self, content):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(content.encode())

    def _json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, default=str).encode())

    def log_message(self, *args):
        pass


def start_dashboard():
    """Ana bot'tan çağır — arka planda web server başlatır"""
    t = threading.Thread(target=lambda: HTTPServer(("0.0.0.0", PORT), Handler).serve_forever(), daemon=True)
    t.start()
    print(f"[DASHBOARD] 🌐 http://0.0.0.0:{PORT}")


def update_scan_data(assets, signals, scan_number):
    """Ana bot her taramada bunu çağırır"""
    global scan_data
    from datetime import datetime
    scan_data = {
        "assets": assets,
        "signals": signals,
        "scan_number": scan_number,
        "last_scan_time": datetime.now().strftime("%H:%M:%S"),
    }
