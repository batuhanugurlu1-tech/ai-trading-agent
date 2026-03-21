"""
AI TRADING AGENT v2.1 — HYBRID + WEB DASHBOARD
Tek dosya: Mekanik sinyal + Claude filtre + Web Dashboard
"""
import time, json, re, os, traceback, requests, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timedelta
from urllib.parse import urlparse

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "900"))
PORT = int(os.environ.get("PORT", "8080"))

STARTING_CAPITAL = 10_000
MAX_POSITION_PCT = 0.10
MAX_POSITIONS = 5
DEFAULT_STOP_LOSS = 0.02
DEFAULT_TAKE_PROFIT = 0.05
MAX_CONSECUTIVE_LOSS = 3
COOLDOWN_SECONDS = 7200
MAX_DAILY_DRAWDOWN = 0.05
MAX_WEEKLY_DRAWDOWN = 0.10
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
MIN_SIGNAL_SCORE = 3

PORTFOLIO_FILE = "portfolio.json"
TRADES_FILE = "trades.json"
STATE_FILE = "agent_state.json"

STOCKS = {"AAPL": "Apple", "TSLA": "Tesla", "NVDA": "NVIDIA", "MSFT": "Microsoft", "GOOGL": "Google", "AMZN": "Amazon", "META": "Meta", "SPY": "S&P 500 ETF", "QQQ": "Nasdaq ETF", "AMD": "AMD", "GLD": "Gold ETF", "SLV": "Silver ETF", "USO": "Oil ETF"}
CRYPTO = {"bitcoin": {"symbol": "BTC", "name": "Bitcoin"}, "ethereum": {"symbol": "ETH", "name": "Ethereum"}, "solana": {"symbol": "SOL", "name": "Solana"}, "ripple": {"symbol": "XRP", "name": "Ripple"}, "dogecoin": {"symbol": "DOGE", "name": "Dogecoin"}}

latest_scan = {"assets": {}, "signals": [], "scan_number": 0, "last_scan_time": None}

def log(emoji, msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {emoji} {msg}")

def load_json(path, default):
    try:
        with open(path, "r") as f: return json.load(f)
    except: return default

def save_json(path, data):
    with open(path, "w") as f: json.dump(data, f, indent=2, ensure_ascii=False)

def telegram_send(text, chat_id=None):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID: return
    chat_id = chat_id or TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code != 200:
                payload["text"] = re.sub(r"<[^>]+>", "", chunk); del payload["parse_mode"]
                requests.post(url, json=payload, timeout=10)
        except: pass

def fetch_stock_data(ticker):
    try:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}", params={"interval": "1d", "range": "3mo"}, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        data = r.json(); result = data["chart"]["result"][0]; meta = result["meta"]; ohlcv = result["indicators"]["quote"][0]
        closes = [c for c in ohlcv["close"] if c is not None]; highs = [h for h in ohlcv["high"] if h is not None]
        lows = [l for l in ohlcv["low"] if l is not None]; volumes = [v for v in ohlcv["volume"] if v is not None]
        if len(closes) < 30: return None
        price = meta["regularMarketPrice"]; prev = meta.get("chartPreviousClose", price); change = ((price - prev) / prev) * 100
        ta = calc_technicals(closes, highs, lows, volumes)
        ta.update({"price": price, "change_pct": round(change, 2), "ticker": ticker, "name": STOCKS.get(ticker, ticker), "asset_type": "stock"})
        return ta
    except Exception as e: log("!", f"{ticker}: {e}"); return None

def fetch_crypto_data():
    results = {}
    try:
        r = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={','.join(CRYPTO.keys())}&vs_currencies=usd&include_24hr_change=true", timeout=15)
        prices = r.json()
        for cg_id, info in CRYPTO.items():
            try:
                r2 = requests.get(f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart?vs_currency=usd&days=90&interval=daily", timeout=15)
                if r2.status_code != 200: continue
                chart = r2.json(); closes = [p[1] for p in chart.get("prices", [])]; volumes = [v[1] for v in chart.get("total_volumes", [])]
                if len(closes) < 30: continue
                current = prices.get(cg_id, {}).get("usd", 0); change = prices.get(cg_id, {}).get("usd_24h_change", 0)
                ta = calc_technicals(closes, closes, closes, volumes)
                ta.update({"price": current, "change_pct": round(change, 2), "ticker": info["symbol"], "name": info["name"], "asset_type": "crypto"})
                results[info["symbol"]] = ta; time.sleep(0.5)
            except: continue
    except Exception as e: log("!", f"Crypto: {e}")
    return results

def ema(data, period):
    if len(data) < period: return None
    k = 2 / (period + 1); val = sum(data[:period]) / period
    for p in data[period:]: val = (p - val) * k + val
    return round(val, 4)

def calc_rsi(data, period=14):
    if len(data) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(data)):
        d = data[i] - data[i-1]; gains.append(max(d, 0)); losses.append(max(-d, 0))
    if len(gains) < period: return None
    ag = sum(gains[:period]) / period; al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period-1) + gains[i]) / period; al = (al * (period-1) + losses[i]) / period
    if al == 0: return 100
    return round(100 - 100 / (1 + ag/al), 1)

def calc_macd(data):
    e12, e26 = ema(data, 12), ema(data, 26)
    if e12 is None or e26 is None: return None, None, None
    macd_line = round(e12 - e26, 4); signal = None
    if len(data) >= 35:
        ms = []
        for i in range(26, len(data)):
            a, b = ema(data[:i+1], 12), ema(data[:i+1], 26)
            if a and b: ms.append(a - b)
        signal = ema(ms, 9) if len(ms) >= 9 else None
    hist = round(macd_line - signal, 4) if signal else None
    return macd_line, signal, hist

def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1: return None
    trs = []
    for i in range(1, len(closes)):
        h = highs[i] if i < len(highs) else closes[i]; l = lows[i] if i < len(lows) else closes[i]
        trs.append(max(h - l, abs(h - closes[i-1]), abs(l - closes[i-1])))
    return round(sum(trs[-period:]) / period, 4)

def calc_technicals(closes, highs, lows, volumes):
    n = len(closes); ema10 = ema(closes, 10); ema30 = ema(closes, 30); ema50 = ema(closes, 50)
    ema200 = ema(closes, 200) if n >= 200 else None; rsi = calc_rsi(closes)
    macd_line, macd_signal, macd_hist = calc_macd(closes); atr = calc_atr(highs, lows, closes)
    trend = "NEUTRAL"
    if ema10 and ema30:
        if ema10 > ema30: trend = "BULLISH"
        elif ema10 < ema30: trend = "BEARISH"
    cross = None
    if ema50 and ema200: cross = "DEATH_CROSS" if ema50 < ema200 else "GOLDEN_CROSS"
    recent = closes[-20:] if n >= 20 else closes
    momentum = round(((closes[-1] - closes[-10]) / closes[-10]) * 100, 2) if n >= 10 else None
    vol_trend = "NORMAL"
    if volumes and len(volumes) >= 10:
        avg_vol = sum(volumes[-10:]) / 10
        if volumes[-1] > avg_vol * 1.5: vol_trend = "HIGH"
        elif volumes[-1] < avg_vol * 0.5: vol_trend = "LOW"
    ema_cross_recent = False
    if n >= 32:
        e10p, e30p = ema(closes[:-1], 10), ema(closes[:-1], 30)
        if e10p and e30p and ema10 and ema30:
            if e10p <= e30p and ema10 > ema30: ema_cross_recent = "BULLISH_CROSS"
            elif e10p >= e30p and ema10 < ema30: ema_cross_recent = "BEARISH_CROSS"
    return {"ema10": ema10, "ema30": ema30, "ema50": ema50, "ema200": ema200, "rsi": rsi, "macd": macd_line, "macd_signal": macd_signal, "macd_hist": macd_hist, "atr": atr, "trend": trend, "cross": cross, "support": round(min(recent), 2), "resistance": round(max(recent), 2), "momentum": momentum, "vol_trend": vol_trend, "ema_cross_recent": ema_cross_recent}

def generate_signal(ta):
    if not ta or ta.get("rsi") is None: return None
    score = 0; reasons = []
    ema10, ema30, rsi, macd_hist = ta.get("ema10"), ta.get("ema30"), ta.get("rsi"), ta.get("macd_hist")
    trend, cross, vol, ema_cross = ta.get("trend"), ta.get("cross"), ta.get("vol_trend"), ta.get("ema_cross_recent")
    if ema10 and ema30:
        if ema10 > ema30: score += 1; reasons.append("EMA10>EMA30")
        else: score -= 1; reasons.append("EMA10<EMA30")
    if ema_cross == "BULLISH_CROSS": score += 1; reasons.append("Taze bullish cross")
    elif ema_cross == "BEARISH_CROSS": score -= 1; reasons.append("Taze bearish cross")
    if rsi is not None:
        if rsi < RSI_OVERSOLD: score += 1; reasons.append(f"RSI {rsi} oversold")
        elif rsi > RSI_OVERBOUGHT: score -= 1; reasons.append(f"RSI {rsi} overbought")
        elif rsi < 45 and trend == "BULLISH": score += 1; reasons.append(f"RSI {rsi} dusuk+uptrend")
        elif rsi > 55 and trend == "BEARISH": score -= 1; reasons.append(f"RSI {rsi} yuksek+downtrend")
    if macd_hist is not None:
        if macd_hist > 0: score += 1; reasons.append("MACD+")
        elif macd_hist < 0: score -= 1; reasons.append("MACD-")
    if cross == "GOLDEN_CROSS": score += 1; reasons.append("Golden Cross")
    elif cross == "DEATH_CROSS": score -= 1; reasons.append("Death Cross")
    if vol == "HIGH":
        if score > 0: score += 1; reasons.append("Yuksek hacim")
        elif score < 0: score -= 1; reasons.append("Yuksek hacim")
    price = ta.get("price", 0); atr = ta.get("atr") or (price * 0.02)
    sl_pct = min(max(atr / price, 0.015), 0.04); tp_pct = sl_pct * 2.5
    if abs(score) >= MIN_SIGNAL_SCORE:
        direction = "LONG" if score > 0 else "SHORT"
        return {"ticker": ta.get("ticker", "?"), "name": ta.get("name", "?"), "asset_type": ta.get("asset_type", "?"), "direction": direction, "score": score, "price": price, "rsi": rsi, "trend": trend, "stop_loss_pct": round(sl_pct, 4), "take_profit_pct": round(tp_pct, 4), "reasons": reasons, "summary": f"{direction} {ta.get('ticker','?')} @ ${price:,.2f} | Skor: {score} | RSI: {rsi}"}
    return None

def claude_filter(signals, portfolio, state):
    if not ANTHROPIC_API_KEY or not signals:
        for s in signals: s["confidence"] = 65; s["thesis"] = " | ".join(s["reasons"])
        return signals
    signals_str = ""
    for i, s in enumerate(signals, 1):
        signals_str += f"\n{i}. {s['summary']}\n   Sebepler: {' | '.join(s['reasons'])}\n   SL: %{s['stop_loss_pct']*100:.1f} | TP: %{s['take_profit_pct']*100:.1f}\n"
    pv = portfolio["cash"] + sum(p["size_usd"] for p in portfolio.get("positions", {}).values())
    pos_str = ", ".join([f"{t} {p['direction']}" for t, p in portfolio.get("positions", {}).items()]) or "YOK"
    prompt = f"""Risk yoneticisisin. Mekanik sinyal sistemi trade onerdi. ONAYLA veya REDDET.
PORTFOLIO: ${pv:,.0f} | Pozisyonlar: {pos_str} | Tarih: {datetime.now().strftime('%Y-%m-%d %H:%M')}
SİNYALLER:{signals_str}
JSON cevap: ```json\n[{{"signal_index": 1, "decision": "APPROVE" veya "REJECT", "confidence": 1-100, "thesis": "1-2 cumle"}}]\n```
Kurallar: Oversold'da SHORT veya overbought'ta LONG oneriyorsa REDDET. Cogu sinyali ONAYLA."""
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"}, json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1500, "messages": [{"role": "user", "content": prompt}]}, timeout=45)
        if r.status_code != 200:
            for s in signals: s["confidence"] = 65; s["thesis"] = " | ".join(s["reasons"])
            return signals
        text = r.json()["content"][0]["text"]; jm = re.search(r'\[[\s\S]*?\]', text)
        if not jm:
            for s in signals: s["confidence"] = 65; s["thesis"] = " | ".join(s["reasons"])
            return signals
        decisions = json.loads(jm.group()); approved = []
        for d in decisions:
            idx = d.get("signal_index", 0) - 1
            if 0 <= idx < len(signals):
                s = signals[idx]
                if d.get("decision", "").upper() == "APPROVE" and d.get("confidence", 0) >= 50:
                    s["confidence"] = d["confidence"]; s["thesis"] = d.get("thesis", " | ".join(s["reasons"]))
                    approved.append(s); log("v", f"Claude ONAYLADI: {s['ticker']} (%{d['confidence']})")
                else: log("x", f"Claude REDDETTI: {s['ticker']} -- {d.get('thesis', '?')}")
        return approved
    except Exception as e:
        log("!", f"Claude: {e}")
        for s in signals: s["confidence"] = 65; s["thesis"] = " | ".join(s["reasons"])
        return signals

def check_thesis(position, ta):
    if not ta: return True, "Veri yok"
    direction, rsi, ema_cross = position["direction"], ta.get("rsi"), ta.get("ema_cross_recent")
    if direction == "LONG" and ema_cross == "BEARISH_CROSS": return False, "Bearish EMA cross"
    if direction == "LONG" and rsi and rsi > 80: return False, f"RSI {rsi} asiri overbought"
    if direction == "SHORT" and ema_cross == "BULLISH_CROSS": return False, "Bullish EMA cross"
    if direction == "SHORT" and rsi and rsi < 20: return False, f"RSI {rsi} asiri oversold"
    state = load_json(STATE_FILE, {}); key = f"tc_{position['ticker']}"
    if time.time() - state.get(key, 0) < SCAN_INTERVAL * 3: return True, "Bekle"
    state[key] = time.time(); save_json(STATE_FILE, state)
    if not ANTHROPIC_API_KEY: return True, "API yok"
    try:
        prompt = f"{position['ticker']} {direction} @ ${position['entry_price']}\nTez: {position.get('thesis', '?')}\nGuncel: ${ta['price']:,.2f} RSI:{rsi} Trend:{ta['trend']} MACD:{ta['macd_hist']}\nSADECE: HOLD veya CLOSE: sebep"
        r = requests.post("https://api.anthropic.com/v1/messages", headers={"Content-Type": "application/json", "x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01"}, json={"model": "claude-haiku-4-5-20251001", "max_tokens": 80, "messages": [{"role": "user", "content": prompt}]}, timeout=20)
        if r.status_code == 200:
            t = r.json()["content"][0]["text"].strip()
            if "CLOSE" in t.upper(): return False, t.split(":", 1)[1].strip() if ":" in t else "Claude"
    except: pass
    return True, "OK"

def init_portfolio():
    if not os.path.exists(PORTFOLIO_FILE): save_json(PORTFOLIO_FILE, {"cash": STARTING_CAPITAL, "starting_capital": STARTING_CAPITAL, "positions": {}})
    if not os.path.exists(TRADES_FILE): save_json(TRADES_FILE, [])
    if not os.path.exists(STATE_FILE): save_json(STATE_FILE, {"paused": False, "consecutive_losses": 0, "cooldown_until": 0, "daily_pnl": 0, "daily_reset": datetime.now().strftime("%Y-%m-%d"), "weekly_pnl": 0, "weekly_reset": datetime.now().strftime("%Y-%m-%d"), "total_scans": 0})

def get_portfolio(): return load_json(PORTFOLIO_FILE, {"cash": STARTING_CAPITAL, "positions": {}})

def can_open_position(portfolio, state):
    if state.get("paused"): return False, "Durduruldu"
    if time.time() < state.get("cooldown_until", 0): return False, "Cooldown"
    if len(portfolio.get("positions", {})) >= MAX_POSITIONS: return False, "Max pozisyon"
    if state.get("daily_pnl", 0) / STARTING_CAPITAL < -MAX_DAILY_DRAWDOWN: return False, "Gunluk limit"
    if state.get("weekly_pnl", 0) / STARTING_CAPITAL < -MAX_WEEKLY_DRAWDOWN: return False, "Haftalik limit"
    return True, "OK"

def open_position(portfolio, state, signal):
    price, direction, ticker = signal["price"], signal["direction"], signal["ticker"]
    size = portfolio["cash"] * MAX_POSITION_PCT; shares = size / price
    sl_pct, tp_pct = signal["stop_loss_pct"], signal["take_profit_pct"]
    if direction == "SHORT": sl = round(price * (1 + sl_pct), 4); tp = round(price * (1 - tp_pct), 4)
    else: sl = round(price * (1 - sl_pct), 4); tp = round(price * (1 + tp_pct), 4)
    pos = {"ticker": ticker, "direction": direction, "entry_price": price, "shares": round(shares, 6), "size_usd": round(size, 2), "stop_loss": sl, "take_profit": tp, "thesis": signal.get("thesis", ""), "confidence": signal.get("confidence", 0), "signal_score": signal.get("score", 0), "reasons": signal.get("reasons", []), "opened_at": datetime.now().isoformat(), "opened_ts": time.time()}
    portfolio["positions"][ticker] = pos; portfolio["cash"] -= size; save_json(PORTFOLIO_FILE, portfolio)
    trades = load_json(TRADES_FILE, [])
    trade = {"id": len(trades)+1, "ticker": ticker, "direction": direction, "entry_price": price, "shares": pos["shares"], "size_usd": pos["size_usd"], "stop_loss": sl, "take_profit": tp, "thesis": pos["thesis"], "confidence": pos["confidence"], "signal_score": pos["signal_score"], "reasons": pos["reasons"], "status": "OPEN", "opened_at": pos["opened_at"]}
    trades.append(trade); save_json(TRADES_FILE, trades)
    return pos, trade

def close_position(portfolio, state, ticker, current_price, reason):
    if ticker not in portfolio.get("positions", {}): return None
    pos = portfolio["positions"][ticker]
    pnl = ((pos["entry_price"] - current_price) if pos["direction"] == "SHORT" else (current_price - pos["entry_price"])) * pos["shares"]
    pnl = round(pnl, 2); pnl_pct = round((pnl / pos["size_usd"]) * 100, 2)
    portfolio["cash"] += pos["size_usd"] + pnl; del portfolio["positions"][ticker]; save_json(PORTFOLIO_FILE, portfolio)
    if pnl < 0:
        state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
        if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSS: state["cooldown_until"] = time.time() + COOLDOWN_SECONDS
    else: state["consecutive_losses"] = 0
    state["daily_pnl"] = state.get("daily_pnl", 0) + pnl; state["weekly_pnl"] = state.get("weekly_pnl", 0) + pnl; save_json(STATE_FILE, state)
    trades = load_json(TRADES_FILE, [])
    for t in trades:
        if t["ticker"] == ticker and t["status"] == "OPEN":
            t.update({"status": "CLOSED", "exit_price": current_price, "pnl": pnl, "pnl_pct": pnl_pct, "close_reason": reason, "closed_at": datetime.now().isoformat()}); break
    save_json(TRADES_FILE, trades)
    return {"ticker": ticker, "pnl": pnl, "pnl_pct": pnl_pct, "reason": reason}

def check_exits(portfolio, state, prices):
    exits, to_close = [], []
    for t, pos in portfolio.get("positions", {}).items():
        p = prices.get(t)
        if not p: continue
        d = pos["direction"]
        if d == "LONG" and p <= pos["stop_loss"]: to_close.append((t, p, "STOP_LOSS"))
        elif d == "LONG" and p >= pos["take_profit"]: to_close.append((t, p, "TAKE_PROFIT"))
        elif d == "SHORT" and p >= pos["stop_loss"]: to_close.append((t, p, "STOP_LOSS"))
        elif d == "SHORT" and p <= pos["take_profit"]: to_close.append((t, p, "TAKE_PROFIT"))
        elif (time.time() - pos.get("opened_ts", time.time())) / 3600 > 72: to_close.append((t, p, "TIMEOUT"))
    for t, p, r in to_close:
        result = close_position(portfolio, state, t, p, r)
        if result: exits.append(result)
    return exits

DASHBOARD_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>AI Trading Agent</title><style>@import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;700&family=DM+Sans:wght@400;500;700&display=swap');*{margin:0;padding:0;box-sizing:border-box}body{background:#08080d;color:#d0d0d0;font-family:'DM Sans',sans-serif}
.hdr{background:#0c0c14;border-bottom:1px solid #1e1e30;padding:16px 24px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px}.hdr h1{font-size:1.2rem}.hdr h1 span{color:#00e676}.hdr .st{font-family:'JetBrains Mono',monospace;font-size:.75rem;color:#666}.live{color:#00e676;animation:pulse 2s infinite}@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.g{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;padding:16px 24px}@media(max-width:900px){.g{grid-template-columns:1fr}}.c{background:#0e0e18;border:1px solid #1e1e30;border-radius:10px;padding:16px}.c:hover{border-color:#00e676}.c h2{font-size:.75rem;color:#555;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}.big{font-family:'JetBrains Mono',monospace;font-size:1.8rem;font-weight:700}.sub{font-size:.8rem;color:#666;margin-top:4px}
.gr{color:#00e676}.rd{color:#ff3d5a}.yl{color:#ffa726}.fw{grid-column:1/-1}.tc{grid-column:span 2}@media(max-width:900px){.tc{grid-column:span 1}}table{width:100%;border-collapse:collapse;font-family:'JetBrains Mono',monospace;font-size:.75rem}th{text-align:left;color:#555;padding:6px 10px;border-bottom:1px solid #1e1e30}td{padding:6px 10px;border-bottom:1px solid #131320}tr:hover{background:#13131f}
.b{display:inline-block;padding:2px 6px;border-radius:3px;font-size:.65rem;font-weight:700}.b.lo{background:#00e67620;color:#00e676}.b.sh{background:#ff3d5a20;color:#ff3d5a}.b.bu{background:#00e67610;color:#00e676;border:1px solid #00e67630}.b.be{background:#ff3d5a10;color:#ff3d5a;border:1px solid #ff3d5a30}.b.ne{background:#55555510;color:#555;border:1px solid #55555530}
.mt{height:5px;background:#131320;border-radius:3px;margin-top:6px;overflow:hidden}.mt .f{height:100%;border-radius:3px}.btn{background:#00e67618;color:#00e676;border:1px solid #00e67630;padding:6px 14px;border-radius:5px;cursor:pointer;font-size:.75rem}.sc{background:#13131f;border:1px solid #1e1e30;border-radius:6px;padding:10px;margin-bottom:6px}</style></head><body>
<div class="hdr"><div><h1>AI Trading <span>Agent</span></h1><div class="st">v2.1 HYBRID | EMA + RSI + MACD + Claude</div></div><div style="text-align:right"><div class="live">CANLI</div><div class="st" id="ls">...</div><button class="btn" onclick="f()">Yenile</button></div></div>
<div class="g">
<div class="c"><h2>Portfoy</h2><div class="big" id="tv">$10,000</div><div class="sub" id="tp">+$0</div><div class="mt"><div class="f" id="pm" style="width:50%;background:#00e676"></div></div></div>
<div class="c"><h2>Nakit</h2><div class="big" id="ca">$10,000</div><div class="sub" id="pc">0 poz</div></div>
<div class="c"><h2>Win Rate</h2><div class="big" id="wr">--%</div><div class="sub" id="pf2">PF: --</div><div class="sub" id="tc2">0 trade</div></div>
<div class="c tc"><h2>Acik Pozisyonlar</h2><table><thead><tr><th>Asset</th><th>Yon</th><th>Giris</th><th>Boyut</th><th>SL</th><th>TP</th><th>Skor</th><th>Guven</th></tr></thead><tbody id="pt"><tr><td colspan="8" style="color:#444">Yok</td></tr></tbody></table></div>
<div class="c"><h2>Sinyaller</h2><div id="sl"><div class="sc" style="color:#444">Yok</div></div></div>
<div class="c fw"><h2>Market</h2><div style="overflow-x:auto"><table><thead><tr><th>Asset</th><th>Fiyat</th><th>Deg</th><th>RSI</th><th>Trend</th><th>Cross</th><th>MACD</th></tr></thead><tbody id="at"><tr><td colspan="7" style="color:#444">...</td></tr></tbody></table></div></div>
<div class="c fw"><h2>Trade Gecmisi</h2><div style="overflow-x:auto"><table><thead><tr><th>#</th><th>Asset</th><th>Yon</th><th>Giris</th><th>Cikis</th><th>P&L</th><th>%</th><th>Sebep</th><th>Tarih</th></tr></thead><tbody id="tt"><tr><td colspan="9" style="color:#444">Yok</td></tr></tbody></table></div></div>
</div>
<script>
async function f(){try{const[p,t,s]=await Promise.all([fetch('/api/portfolio').then(r=>r.json()),fetch('/api/trades').then(r=>r.json()),fetch('/api/scan').then(r=>r.json())]);const tot=p.cash+Object.values(p.positions||{}).reduce((a,b)=>a+b.size_usd,0);const pnl=tot-10000,pp=pnl/100;document.getElementById('tv').textContent='$'+tot.toLocaleString('en',{minimumFractionDigits:2,maximumFractionDigits:2});const tpe=document.getElementById('tp');tpe.className='sub '+(pnl>=0?'gr':'rd');tpe.textContent=(pnl>=0?'+':'')+' $'+pnl.toFixed(2)+' ('+pp.toFixed(1)+'%)';document.getElementById('ca').textContent='$'+p.cash.toLocaleString('en',{minimumFractionDigits:2});const ps=Object.keys(p.positions||{}).length;document.getElementById('pc').textContent=ps+' poz';const m=document.getElementById('pm');m.style.width=Math.min(Math.max((pp+10)/20*100,5),100)+'%';m.style.background=pnl>=0?'#00e676':'#ff3d5a';const pe=document.getElementById('pt');if(ps>0){pe.innerHTML=Object.entries(p.positions).map(([k,v])=>'<tr><td><b>'+k+'</b></td><td><span class="b '+(v.direction==='LONG'?'lo':'sh')+'">'+v.direction+'</span></td><td>$'+v.entry_price+'</td><td>$'+v.size_usd.toFixed(0)+'</td><td>$'+v.stop_loss+'</td><td>$'+v.take_profit+'</td><td>'+(v.signal_score||'?')+'</td><td>'+(v.confidence||'?')+'%</td></tr>').join('')}else{pe.innerHTML='<tr><td colspan="8" style="color:#444">Yok</td></tr>'}const cl=t.filter(x=>x.status==='CLOSED'),w=cl.filter(x=>(x.pnl||0)>0);const wr2=cl.length?(w.length/cl.length*100).toFixed(0):'--';const ws=w.reduce((a,b)=>a+b.pnl,0),ls2=Math.abs(cl.filter(x=>(x.pnl||0)<=0).reduce((a,b)=>a+(b.pnl||0),0));document.getElementById('wr').textContent=wr2+'%';document.getElementById('wr').className='big '+(parseFloat(wr2)>=55?'gr':parseFloat(wr2)>=45?'yl':'rd');document.getElementById('pf2').textContent='PF: '+(ls2>0?(ws/ls2).toFixed(2):'--');document.getElementById('tc2').textContent=cl.length+' trade ('+w.length+'W/'+(cl.length-w.length)+'L)';const te=document.getElementById('tt');if(cl.length>0){te.innerHTML=cl.slice(-12).reverse().map(x=>{const iw=(x.pnl||0)>0;return'<tr><td>#'+x.id+'</td><td><b>'+x.ticker+'</b></td><td><span class="b '+(x.direction==='LONG'?'lo':'sh')+'">'+x.direction+'</span></td><td>$'+x.entry_price+'</td><td>$'+(x.exit_price||'?')+'</td><td class="'+(iw?'gr':'rd')+'">$'+(x.pnl||0).toFixed(2)+'</td><td class="'+(iw?'gr':'rd')+'">'+(x.pnl_pct||0).toFixed(1)+'%</td><td>'+(x.close_reason||'?')+'</td><td>'+(x.closed_at||'').slice(0,16)+'</td></tr>'}).join('')}if(s.assets){const ae=document.getElementById('at');const en=Object.entries(s.assets);if(en.length>0){ae.innerHTML=en.map(([k,a])=>{const tc3=a.trend==='BULLISH'?'bu':a.trend==='BEARISH'?'be':'ne';const cc=(a.change_pct||0)>=0?'gr':'rd';return'<tr><td><b>'+k+'</b></td><td>$'+(a.price||0).toLocaleString('en',{maximumFractionDigits:2})+'</td><td class="'+cc+'">'+(a.change_pct>=0?'+':'')+a.change_pct+'%</td><td>'+(a.rsi||'-')+'</td><td><span class="b '+tc3+'">'+a.trend+'</span></td><td>'+(a.ema_cross_recent||a.cross||'-')+'</td><td>'+(a.macd_hist?(a.macd_hist>0?'up':'dn')+' '+a.macd_hist:'-')+'</td></tr>'}).join('')}}if(s.signals&&s.signals.length>0){document.getElementById('sl').innerHTML=s.signals.map(x=>'<div class="sc"><b class="'+(x.direction==='LONG'?'gr':'rd')+'">'+x.direction+' '+x.ticker+'</b> Skor:'+x.score+'<div style="color:#555;font-size:.7rem;margin-top:3px">'+(x.reasons||[]).join(' . ')+'</div></div>').join('')}else{document.getElementById('sl').innerHTML='<div class="sc" style="color:#444">Bu taramada sinyal yok</div>'}document.getElementById('ls').textContent='#'+(s.scan_number||0)+' | '+(s.last_scan_time||'?')}catch(e){console.error(e)}}f();setInterval(f,30000);</script></body></html>"""

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", ""): self._html(DASHBOARD_HTML)
        elif path == "/api/portfolio": self._json(load_json(PORTFOLIO_FILE, {"cash": STARTING_CAPITAL, "positions": {}}))
        elif path == "/api/trades": self._json(load_json(TRADES_FILE, []))
        elif path == "/api/state": self._json(load_json(STATE_FILE, {}))
        elif path == "/api/scan": self._json(latest_scan)
        else: self.send_response(404); self.end_headers()
    def _html(self, c): self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers(); self.wfile.write(c.encode())
    def _json(self, d): self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers(); self.wfile.write(json.dumps(d, default=str).encode())
    def log_message(self, *a): pass

def start_dashboard():
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", PORT), DashboardHandler).serve_forever(), daemon=True).start()
    log("W", f"Dashboard: port {PORT}")

def main():
    global latest_scan
    print("=" * 50); print("AI TRADING AGENT v2.1 -- HYBRID + WEB"); print(f"Scan: {SCAN_INTERVAL//60}dk | Port: {PORT}"); print("=" * 50)
    if not ANTHROPIC_API_KEY: print("! ANTHROPIC_API_KEY yok -- Claude devre disi")
    init_portfolio(); start_dashboard()
    telegram_send(f"AI Trading Agent v2.1 Aktif! | ${STARTING_CAPITAL:,} | {SCAN_INTERVAL//60}dk | Web Dashboard aktif")
    scan = 0
    while True:
        try:
            scan += 1; state = load_json(STATE_FILE, {}); state["total_scans"] = scan
            today = datetime.now().strftime("%Y-%m-%d")
            if state.get("daily_reset") != today: state["daily_pnl"] = 0; state["daily_reset"] = today
            if datetime.now().weekday() == 0 and state.get("weekly_reset") != today: state["weekly_pnl"] = 0; state["weekly_reset"] = today
            save_json(STATE_FILE, state)
            log("S", f"=== TARAMA #{scan} ===")
            all_data, current_prices = {}, {}
            for ticker in STOCKS:
                d = fetch_stock_data(ticker)
                if d: all_data[ticker] = d; current_prices[ticker] = d["price"]; log("$", f"{ticker}: ${d['price']:,.2f} ({d['change_pct']:+.1f}%) RSI:{d['rsi']} {d['trend']}")
                time.sleep(0.3)
            crypto = fetch_crypto_data()
            for sym, d in crypto.items(): all_data[sym] = d; current_prices[sym] = d["price"]; log("B", f"{sym}: ${d['price']:,.2f} ({d['change_pct']:+.1f}%) RSI:{d['rsi']} {d['trend']}")
            if not all_data: time.sleep(SCAN_INTERVAL); continue
            portfolio = get_portfolio()
            exits = check_exits(portfolio, state, current_prices)
            for ex in exits: telegram_send(f"{'W' if ex['pnl']>0 else 'L'} {ex['reason']} {ex['ticker']} | ${ex['pnl']:+.2f}")
            portfolio = get_portfolio()
            for ticker, pos in list(portfolio.get("positions", {}).items()):
                ta = all_data.get(ticker)
                if ta:
                    hold, reason = check_thesis(pos, ta)
                    if not hold:
                        result = close_position(portfolio, state, ticker, ta["price"], f"TEZ: {reason}")
                        if result: telegram_send(f"TEZ {ticker} ${result['pnl']:+.2f}"); portfolio = get_portfolio()
            raw_signals = []
            for ticker, ta in all_data.items():
                if ticker in portfolio.get("positions", {}): continue
                signal = generate_signal(ta); ta["signal_summary"] = signal["summary"] if signal else ""
                if signal: raw_signals.append(signal); log(">", f"SINYAL: {signal['summary']}")
            log(">", f"{len(raw_signals)} sinyal")
            can_trade, reason = can_open_position(portfolio, state); approved = []
            if can_trade and raw_signals:
                approved = claude_filter(raw_signals, portfolio, state)
                log("C", f"{len(approved)}/{len(raw_signals)} onayli")
                approved.sort(key=lambda s: s.get("confidence", 0), reverse=True)
                for signal in approved:
                    portfolio = get_portfolio(); ok, _ = can_open_position(portfolio, state)
                    if not ok: break
                    if signal["ticker"] in portfolio.get("positions", {}): continue
                    pos, trade = open_position(portfolio, state, signal)
                    telegram_send(f"YENI {signal['direction']} {signal['ticker']} ${signal['price']:,.2f} Skor:{signal['score']} Guven:%{signal['confidence']}\n{signal.get('thesis','')}")
                    time.sleep(1)
            latest_scan = {"assets": {k: {key: v.get(key) for key in ["price","change_pct","rsi","trend","cross","ema_cross_recent","macd_hist","name","signal_summary"]} for k, v in all_data.items()}, "signals": [{"ticker": s["ticker"], "direction": s["direction"], "score": s["score"], "rsi": s["rsi"], "reasons": s["reasons"]} for s in raw_signals], "scan_number": scan, "last_scan_time": datetime.now().strftime("%H:%M:%S")}
            portfolio = get_portfolio(); total = portfolio["cash"] + sum(p["size_usd"] for p in portfolio.get("positions", {}).values()); pnl = total - STARTING_CAPITAL
            log("P", f"${total:,.0f} ({pnl:+,.0f}) | {len(portfolio.get('positions', {}))} poz")
            log("Z", f"{SCAN_INTERVAL//60}dk bekleniyor")
        except KeyboardInterrupt: break
        except Exception as e: log("!", f"HATA: {e}"); traceback.print_exc()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
