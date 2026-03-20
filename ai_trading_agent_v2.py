"""
AI TRADING AGENT v2.0 — HYBRID
================================
Mekanik sinyal üretimi + Claude AI filtre

SINYAL MOTORU (kural bazlı, backtestlenebilir):
  1. EMA10/EMA30 crossover → trend yönü
  2. RSI(14) filtre → aşırı alım/satım
  3. MACD histogram → momentum teyidi
  4. Hacim kontrolü → sinyal güvenilirliği

CLAUDE FİLTRE (AI, judgment):
  - Mekanik sinyali al → onayla veya reddet
  - Piyasa bağlamı değerlendir
  - Risk/reward analizi

Paper trading: $10,000 sanal sermaye
Hedef: 20+ trade → win rate + profit factor değerlendir
"""
from dashboard import start_dashboard, update_scan_data
import time
import json
import re
import os
import traceback
import requests
from datetime import datetime, timedelta

# =============================================
# AYARLAR
# =============================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

SCAN_INTERVAL = int(os.environ.get("SCAN_INTERVAL", "900"))  # 15 dk

# Portföy
STARTING_CAPITAL = 10_000
MAX_POSITION_PCT = 0.10
MAX_POSITIONS = 5
DEFAULT_STOP_LOSS = 0.02
DEFAULT_TAKE_PROFIT = 0.05

# Risk
MAX_CONSECUTIVE_LOSS = 3
COOLDOWN_SECONDS = 7200
MAX_DAILY_DRAWDOWN = 0.05
MAX_WEEKLY_DRAWDOWN = 0.10

# Sinyal eşikleri
RSI_OVERSOLD = 30
RSI_OVERBOUGHT = 70
MIN_SIGNAL_SCORE = 3  # -5 ile +5 arası, 3+ = güçlü sinyal

# Dosyalar
PORTFOLIO_FILE = "portfolio.json"
TRADES_FILE = "trades.json"
STATE_FILE = "agent_state.json"

# =============================================
# ASSET LİSTESİ
# =============================================
STOCKS = {
    "AAPL": "Apple", "TSLA": "Tesla", "NVDA": "NVIDIA",
    "MSFT": "Microsoft", "GOOGL": "Google", "AMZN": "Amazon",
    "META": "Meta", "SPY": "S&P 500 ETF", "QQQ": "Nasdaq ETF",
    "AMD": "AMD",
}

CRYPTO = {
    "bitcoin": {"symbol": "BTC", "name": "Bitcoin"},
    "ethereum": {"symbol": "ETH", "name": "Ethereum"},
    "solana": {"symbol": "SOL", "name": "Solana"},
    "ripple": {"symbol": "XRP", "name": "Ripple"},
    "dogecoin": {"symbol": "DOGE", "name": "Dogecoin"},
}


# =============================================
# YARDIMCI
# =============================================
def log(emoji, msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {emoji} {msg}")


def load_json(path, default):
    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def telegram_send(text, chat_id=None):
    if not TELEGRAM_TOKEN:
        return
    chat_id = chat_id or TELEGRAM_CHAT_ID
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        payload = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"}
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code != 200:
                payload["text"] = re.sub(r"<[^>]+>", "", chunk)
                del payload["parse_mode"]
                requests.post(url, json=payload, timeout=10)
        except Exception as e:
            log("❌", f"Telegram: {e}")


# =============================================
# VERİ KATMANI
# =============================================
def fetch_stock_data(ticker):
    """Yahoo Finance — 3 ay günlük veri"""
    try:
        r = requests.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
            params={"interval": "1d", "range": "3mo"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        data = r.json()
        result = data["chart"]["result"][0]
        meta = result["meta"]
        ohlcv = result["indicators"]["quote"][0]

        closes = [c for c in ohlcv["close"] if c is not None]
        highs = [h for h in ohlcv["high"] if h is not None]
        lows = [l for l in ohlcv["low"] if l is not None]
        volumes = [v for v in ohlcv["volume"] if v is not None]

        if len(closes) < 30:
            return None

        price = meta["regularMarketPrice"]
        prev = meta.get("chartPreviousClose", price)
        change = ((price - prev) / prev) * 100

        ta = calc_technicals(closes, highs, lows, volumes)
        ta["price"] = price
        ta["change_pct"] = round(change, 2)
        ta["ticker"] = ticker
        ta["name"] = STOCKS.get(ticker, ticker)
        ta["asset_type"] = "stock"
        return ta
    except Exception as e:
        log("⚠️", f"{ticker}: {e}")
        return None


def fetch_crypto_data():
    """CoinGecko — 90 gün günlük veri"""
    results = {}
    ids = ",".join(CRYPTO.keys())
    try:
        r = requests.get(
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={ids}&vs_currencies=usd&include_24hr_change=true",
            timeout=15,
        )
        prices = r.json()

        for cg_id, info in CRYPTO.items():
            try:
                r2 = requests.get(
                    f"https://api.coingecko.com/api/v3/coins/{cg_id}/market_chart"
                    f"?vs_currency=usd&days=90&interval=daily",
                    timeout=15,
                )
                if r2.status_code != 200:
                    continue
                chart = r2.json()
                closes = [p[1] for p in chart.get("prices", [])]
                volumes = [v[1] for v in chart.get("total_volumes", [])]

                if len(closes) < 30:
                    continue

                current = prices.get(cg_id, {}).get("usd", 0)
                change = prices.get(cg_id, {}).get("usd_24h_change", 0)

                ta = calc_technicals(closes, closes, closes, volumes)
                ta["price"] = current
                ta["change_pct"] = round(change, 2)
                ta["ticker"] = info["symbol"]
                ta["name"] = info["name"]
                ta["asset_type"] = "crypto"
                results[info["symbol"]] = ta
                time.sleep(0.5)
            except:
                continue
    except Exception as e:
        log("❌", f"Crypto: {e}")
    return results


# =============================================
# TEKNİK ANALİZ
# =============================================
def ema(data, period):
    if len(data) < period:
        return None
    k = 2 / (period + 1)
    val = sum(data[:period]) / period
    for p in data[period:]:
        val = (p - val) * k + val
    return round(val, 4)


def sma(data, period):
    if len(data) < period:
        return None
    return round(sum(data[-period:]) / period, 4)


def calc_rsi(data, period=14):
    if len(data) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(data)):
        d = data[i] - data[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    if len(gains) < period:
        return None
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period-1) + gains[i]) / period
        al = (al * (period-1) + losses[i]) / period
    if al == 0:
        return 100
    return round(100 - 100 / (1 + ag/al), 1)


def calc_macd(data):
    e12 = ema(data, 12)
    e26 = ema(data, 26)
    if e12 is None or e26 is None:
        return None, None, None
    macd_line = round(e12 - e26, 4)
    # Basit signal yaklaşımı
    if len(data) >= 35:
        macd_series = []
        for i in range(26, len(data)):
            e12_i = ema(data[:i+1], 12)
            e26_i = ema(data[:i+1], 26)
            if e12_i and e26_i:
                macd_series.append(e12_i - e26_i)
        signal = ema(macd_series, 9) if len(macd_series) >= 9 else None
    else:
        signal = None
    hist = round(macd_line - signal, 4) if signal else None
    return macd_line, signal, hist


def calc_atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        h = highs[i] if i < len(highs) else closes[i]
        l = lows[i] if i < len(lows) else closes[i]
        tr = max(h - l, abs(h - closes[i-1]), abs(l - closes[i-1]))
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 4)


def calc_technicals(closes, highs, lows, volumes):
    n = len(closes)
    ema10 = ema(closes, 10)
    ema30 = ema(closes, 30)
    ema50 = ema(closes, 50)
    ema200 = ema(closes, 200) if n >= 200 else None
    rsi = calc_rsi(closes)
    macd_line, macd_signal, macd_hist = calc_macd(closes)
    atr = calc_atr(highs, lows, closes)

    current = closes[-1]

    # Trend
    trend = "NEUTRAL"
    if ema10 and ema30:
        if ema10 > ema30:
            trend = "BULLISH"
        elif ema10 < ema30:
            trend = "BEARISH"

    # Cross
    cross = None
    if ema50 and ema200:
        if ema50 < ema200:
            cross = "DEATH_CROSS"
        elif ema50 > ema200:
            cross = "GOLDEN_CROSS"

    # Destek/Direnç
    recent = closes[-20:] if n >= 20 else closes
    support = round(min(recent), 2)
    resistance = round(max(recent), 2)

    # Momentum (10 gün)
    momentum = round(((closes[-1] - closes[-10]) / closes[-10]) * 100, 2) if n >= 10 else None

    # Hacim trend
    vol_trend = "NORMAL"
    if volumes and len(volumes) >= 10:
        avg_vol = sum(volumes[-10:]) / 10
        if volumes[-1] > avg_vol * 1.5:
            vol_trend = "HIGH"
        elif volumes[-1] < avg_vol * 0.5:
            vol_trend = "LOW"

    # EMA crossover son durumu — son 3 gün içinde cross oldu mu?
    ema_cross_recent = False
    if n >= 32:
        ema10_prev = ema(closes[:-1], 10)
        ema30_prev = ema(closes[:-1], 30)
        if ema10_prev and ema30_prev and ema10 and ema30:
            # Bullish cross: önceden ema10 < ema30, şimdi ema10 > ema30
            if ema10_prev <= ema30_prev and ema10 > ema30:
                ema_cross_recent = "BULLISH_CROSS"
            elif ema10_prev >= ema30_prev and ema10 < ema30:
                ema_cross_recent = "BEARISH_CROSS"

    return {
        "ema10": ema10, "ema30": ema30, "ema50": ema50, "ema200": ema200,
        "rsi": rsi, "macd": macd_line, "macd_signal": macd_signal, "macd_hist": macd_hist,
        "atr": atr, "trend": trend, "cross": cross,
        "support": support, "resistance": resistance,
        "momentum": momentum, "vol_trend": vol_trend,
        "ema_cross_recent": ema_cross_recent,
    }


# =============================================
# MEKANİK SİNYAL MOTORU
# =============================================
def generate_signal(ta):
    """
    Kural bazlı sinyal üretici. Skor -5 ile +5 arası.
    +3 ve üstü = LONG sinyali
    -3 ve altı = SHORT sinyali
    Arada = sinyal yok

    Kurallar:
      EMA10 > EMA30         → +1 (trend yukarı)
      EMA10 < EMA30         → -1 (trend aşağı)
      Taze EMA crossover    → +1/-1 ek (yeni sinyal daha güçlü)
      RSI < 30 (oversold)   → +1 (dip fırsatı)
      RSI > 70 (overbought) → -1 (tepe riski)
      RSI 30-45 + uptrend   → +1 (trend başlangıcı)
      RSI 55-70 + downtrend → -1
      MACD hist > 0         → +1 (momentum yukarı)
      MACD hist < 0         → -1 (momentum aşağı)
      MACD hist yön değişimi → +1/-1 ek
      Yüksek hacim           → ±1 (sinyal güvenilirliği)
      Golden Cross           → +1
      Death Cross            → -1
    """
    if not ta or ta.get("rsi") is None:
        return None

    score = 0
    reasons = []

    ema10 = ta.get("ema10")
    ema30 = ta.get("ema30")
    rsi = ta.get("rsi")
    macd_hist = ta.get("macd_hist")
    trend = ta.get("trend")
    cross = ta.get("cross")
    vol = ta.get("vol_trend")
    ema_cross = ta.get("ema_cross_recent")
    momentum = ta.get("momentum")

    # ── EMA TREND ──
    if ema10 and ema30:
        if ema10 > ema30:
            score += 1
            reasons.append("EMA10>EMA30 (uptrend)")
        else:
            score -= 1
            reasons.append("EMA10<EMA30 (downtrend)")

    # ── TAZE EMA CROSSOVER ──
    if ema_cross == "BULLISH_CROSS":
        score += 1
        reasons.append("Taze bullish EMA cross!")
    elif ema_cross == "BEARISH_CROSS":
        score -= 1
        reasons.append("Taze bearish EMA cross!")

    # ── RSI ──
    if rsi is not None:
        if rsi < RSI_OVERSOLD:
            score += 1
            reasons.append(f"RSI {rsi} oversold (dip fırsatı)")
        elif rsi > RSI_OVERBOUGHT:
            score -= 1
            reasons.append(f"RSI {rsi} overbought (tepe riski)")
        elif rsi < 45 and trend == "BULLISH":
            score += 1
            reasons.append(f"RSI {rsi} düşük + uptrend (erken giriş)")
        elif rsi > 55 and trend == "BEARISH":
            score -= 1
            reasons.append(f"RSI {rsi} yüksek + downtrend")

    # ── MACD HİSTOGRAM ──
    if macd_hist is not None:
        if macd_hist > 0:
            score += 1
            reasons.append(f"MACD histogram pozitif ({macd_hist:.4f})")
        elif macd_hist < 0:
            score -= 1
            reasons.append(f"MACD histogram negatif ({macd_hist:.4f})")

    # ── GOLDEN / DEATH CROSS ──
    if cross == "GOLDEN_CROSS":
        score += 1
        reasons.append("Golden Cross (EMA50>EMA200)")
    elif cross == "DEATH_CROSS":
        score -= 1
        reasons.append("Death Cross (EMA50<EMA200)")

    # ── HACİM ──
    if vol == "HIGH":
        # Yüksek hacim mevcut trendi güçlendirir
        if score > 0:
            score += 1
            reasons.append("Yüksek hacim (sinyal güçlü)")
        elif score < 0:
            score -= 1
            reasons.append("Yüksek hacim (satış baskısı güçlü)")

    # ── SONUÇ ──
    ticker = ta.get("ticker", "?")
    price = ta.get("price", 0)

    if score >= MIN_SIGNAL_SCORE:
        # ATR bazlı dinamik SL/TP
        atr = ta.get("atr") or (price * 0.02)
        sl_pct = min(max(atr / price, 0.015), 0.04)  # %1.5 - %4 arası
        tp_pct = sl_pct * 2.5  # Risk/reward 1:2.5

        return {
            "ticker": ticker,
            "name": ta.get("name", ticker),
            "asset_type": ta.get("asset_type", "?"),
            "direction": "LONG",
            "score": score,
            "price": price,
            "rsi": rsi,
            "trend": trend,
            "stop_loss_pct": round(sl_pct, 4),
            "take_profit_pct": round(tp_pct, 4),
            "reasons": reasons,
            "summary": f"LONG {ticker} @ ${price:,.2f} | Skor: {score} | RSI: {rsi} | {trend}",
        }

    elif score <= -MIN_SIGNAL_SCORE:
        atr = ta.get("atr") or (price * 0.02)
        sl_pct = min(max(atr / price, 0.015), 0.04)
        tp_pct = sl_pct * 2.5

        return {
            "ticker": ticker,
            "name": ta.get("name", ticker),
            "asset_type": ta.get("asset_type", "?"),
            "direction": "SHORT",
            "score": score,
            "price": price,
            "rsi": rsi,
            "trend": trend,
            "stop_loss_pct": round(sl_pct, 4),
            "take_profit_pct": round(tp_pct, 4),
            "reasons": reasons,
            "summary": f"SHORT {ticker} @ ${price:,.2f} | Skor: {score} | RSI: {rsi} | {trend}",
        }

    return None  # Sinyal yok


# =============================================
# CLAUDE FİLTRE
# =============================================
def claude_filter(signals, portfolio, state):
    """
    Mekanik sinyalleri Claude'a gönder.
    Claude ONAYLA veya REDDET der.
    """
    if not ANTHROPIC_API_KEY or not signals:
        return signals  # API yoksa mekanik sinyalleri direkt kullan

    signals_str = ""
    for i, s in enumerate(signals, 1):
        signals_str += (
            f"\n{i}. {s['summary']}\n"
            f"   Sebepler: {' | '.join(s['reasons'])}\n"
            f"   SL: %{s['stop_loss_pct']*100:.1f} | TP: %{s['take_profit_pct']*100:.1f}\n"
        )

    portfolio_value = portfolio["cash"]
    for pos in portfolio.get("positions", {}).values():
        portfolio_value += pos["size_usd"]

    positions_str = "YOK"
    if portfolio.get("positions"):
        positions_str = ", ".join([
            f"{t} {p['direction']} @ ${p['entry_price']}"
            for t, p in portfolio["positions"].items()
        ])

    prompt = f"""Sen risk yoneticisisin. Mekanik sinyal sistemi asagidaki trade'leri onerdi.
Her birini ONAYLA veya REDDET.

PORTFOLIO: ${portfolio_value:,.0f} | Pozisyonlar: {positions_str}
Ardisik kayip: {state.get('consecutive_losses', 0)}/{MAX_CONSECUTIVE_LOSS}
Tarih: {datetime.now().strftime('%Y-%m-%d %H:%M')}

MEKANİK SİNYALLER:
{signals_str}

Her sinyal icin SADECE bu JSON formatinda cevap ver:
```json
[
  {{
    "signal_index": 1,
    "decision": "APPROVE" veya "REJECT",
    "confidence": 1-100,
    "thesis": "1-2 cumle — neden onayladin veya reddeddin",
    "adjusted_sl": null,
    "adjusted_tp": null
  }}
]
```

KURALLAR:
- Mekanik sinyal zaten 3+ skor almis yani teknik olarak guclu
- Senin gorevin: piyasa baglami, haberler, mantik kontrolu
- Cogu sinyali ONAYLA — sadece ciddi sorun varsa REDDET
- Reddetme sebepleri: earnings yaklasiyorsa, buyuk makro olay, trend cok zayif, overexposure
- Confidence 50 altiysa REDDET
- SL/TP'yi ayarlayabilirsin (null birakirsan mekanik kalir)"""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 1500,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=45,
        )

        if r.status_code != 200:
            log("⚠️", f"Claude filtre hatasi: {r.status_code} — mekanik sinyaller kullanılıyor")
            # Fallback: mekanik sinyallere default confidence ekle
            for s in signals:
                s["confidence"] = 65
                s["thesis"] = " | ".join(s["reasons"])
            return signals

        text = r.json()["content"][0]["text"]
        json_match = re.search(r'\[[\s\S]*?\]', text)

        if not json_match:
            log("⚠️", "Claude JSON bulunamadı — mekanik sinyaller kullanılıyor")
            for s in signals:
                s["confidence"] = 65
                s["thesis"] = " | ".join(s["reasons"])
            return signals

        decisions = json.loads(json_match.group())
        approved = []

        for d in decisions:
            idx = d.get("signal_index", 0) - 1
            if idx < 0 or idx >= len(signals):
                continue

            signal = signals[idx]

            if d.get("decision", "").upper() == "APPROVE" and d.get("confidence", 0) >= 50:
                signal["confidence"] = d["confidence"]
                signal["thesis"] = d.get("thesis", " | ".join(signal["reasons"]))

                # Claude SL/TP ayarladıysa uygula
                if d.get("adjusted_sl") is not None:
                    signal["stop_loss_pct"] = d["adjusted_sl"]
                if d.get("adjusted_tp") is not None:
                    signal["take_profit_pct"] = d["adjusted_tp"]

                approved.append(signal)
                log("✅", f"Claude ONAYLADI: {signal['ticker']} {signal['direction']} (%{d['confidence']})")
            else:
                log("🚫", f"Claude REDDETTİ: {signal['ticker']} — {d.get('thesis', '?')}")

        return approved

    except Exception as e:
        log("⚠️", f"Claude filtre hatası: {e} — mekanik sinyaller kullanılıyor")
        for s in signals:
            s["confidence"] = 65
            s["thesis"] = " | ".join(s["reasons"])
        return signals


# =============================================
# TEZ KONTROLÜ (mevcut pozisyonlar)
# =============================================
def check_thesis(position, ta):
    """Mevcut pozisyonun tezini mekanik + Claude ile kontrol et"""
    if not ta:
        return True, "Veri yok"

    ticker = position["ticker"]
    direction = position["direction"]
    rsi = ta.get("rsi")
    trend = ta.get("trend")
    ema_cross = ta.get("ema_cross_recent")

    # Mekanik kontrol — tez kesin bozulmuşsa Claude'a sormaya gerek yok
    if direction == "LONG":
        if ema_cross == "BEARISH_CROSS":
            return False, "Bearish EMA crossover — trend döndü"
        if rsi and rsi > 80:
            return False, f"RSI {rsi} aşırı overbought"
    elif direction == "SHORT":
        if ema_cross == "BULLISH_CROSS":
            return False, "Bullish EMA crossover — trend döndü"
        if rsi and rsi < 20:
            return False, f"RSI {rsi} aşırı oversold"

    # Claude kontrolü (her 3 taramada bir — maliyet optimizasyonu)
    state = load_json(STATE_FILE, {})
    thesis_check_key = f"thesis_check_{ticker}"
    last_check = state.get(thesis_check_key, 0)
    if time.time() - last_check < SCAN_INTERVAL * 3:
        return True, "Henüz kontrol zamanı değil"

    state[thesis_check_key] = time.time()
    save_json(STATE_FILE, state)

    if not ANTHROPIC_API_KEY:
        return True, "API yok"

    prompt = f"""Pozisyon degerlendir — TUT mu KAPAT mi?

{ticker} | {direction} @ ${position['entry_price']}
Tez: {position.get('thesis', '?')}

Guncel: ${ta['price']:,.2f} ({ta['change_pct']:+.1f}%) | RSI: {rsi} | Trend: {trend}
EMA10: {ta['ema10']} | EMA30: {ta['ema30']} | MACD hist: {ta['macd_hist']}

Cevap SADECE: "HOLD" veya "CLOSE: [sebep]" """

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type": "application/json",
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 80,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        if r.status_code == 200:
            text = r.json()["content"][0]["text"].strip()
            if "CLOSE" in text.upper():
                reason = text.split(":", 1)[1].strip() if ":" in text else "Claude önerisi"
                return False, reason
    except:
        pass

    return True, "Tez geçerli"


# =============================================
# PORTFÖY YÖNETİMİ
# =============================================
def init_portfolio():
    if not os.path.exists(PORTFOLIO_FILE):
        save_json(PORTFOLIO_FILE, {
            "cash": STARTING_CAPITAL,
            "starting_capital": STARTING_CAPITAL,
            "positions": {},
        })
    if not os.path.exists(TRADES_FILE):
        save_json(TRADES_FILE, [])
    if not os.path.exists(STATE_FILE):
        save_json(STATE_FILE, {
            "paused": False, "consecutive_losses": 0,
            "cooldown_until": 0, "daily_pnl": 0,
            "daily_reset": datetime.now().strftime("%Y-%m-%d"),
            "weekly_pnl": 0, "weekly_reset": datetime.now().strftime("%Y-%m-%d"),
            "total_scans": 0,
        })


def get_portfolio():
    return load_json(PORTFOLIO_FILE, {"cash": STARTING_CAPITAL, "positions": {}})


def can_open_position(portfolio, state):
    if state.get("paused"):
        return False, "Duraklatılmış"
    if time.time() < state.get("cooldown_until", 0):
        return False, f"Cooldown: {int((state['cooldown_until']-time.time())//60)}dk"
    if len(portfolio.get("positions", {})) >= MAX_POSITIONS:
        return False, f"Max {MAX_POSITIONS} pozisyon"
    if state.get("daily_pnl", 0) / STARTING_CAPITAL < -MAX_DAILY_DRAWDOWN:
        return False, "Günlük drawdown limiti"
    if state.get("weekly_pnl", 0) / STARTING_CAPITAL < -MAX_WEEKLY_DRAWDOWN:
        return False, "Haftalık drawdown limiti"
    return True, "OK"


def open_position(portfolio, state, signal):
    price = signal["price"]
    direction = signal["direction"]
    ticker = signal["ticker"]
    sl_pct = signal["stop_loss_pct"]
    tp_pct = signal["take_profit_pct"]

    size = portfolio["cash"] * MAX_POSITION_PCT
    shares = size / price

    if direction == "SHORT":
        sl_price = round(price * (1 + sl_pct), 4)
        tp_price = round(price * (1 - tp_pct), 4)
    else:
        sl_price = round(price * (1 - sl_pct), 4)
        tp_price = round(price * (1 + tp_pct), 4)

    pos = {
        "ticker": ticker, "direction": direction,
        "entry_price": price, "shares": round(shares, 6),
        "size_usd": round(size, 2),
        "stop_loss": sl_price, "take_profit": tp_price,
        "thesis": signal.get("thesis", ""),
        "confidence": signal.get("confidence", 0),
        "signal_score": signal.get("score", 0),
        "signal_reasons": signal.get("reasons", []),
        "opened_at": datetime.now().isoformat(),
        "opened_ts": time.time(),
    }

    portfolio["positions"][ticker] = pos
    portfolio["cash"] -= size
    save_json(PORTFOLIO_FILE, portfolio)

    trades = load_json(TRADES_FILE, [])
    trade = {
        "id": len(trades) + 1, "ticker": ticker, "direction": direction,
        "entry_price": price, "shares": pos["shares"], "size_usd": pos["size_usd"],
        "stop_loss": sl_price, "take_profit": tp_price,
        "thesis": pos["thesis"], "confidence": pos["confidence"],
        "signal_score": pos["signal_score"],
        "status": "OPEN", "opened_at": pos["opened_at"],
    }
    trades.append(trade)
    save_json(TRADES_FILE, trades)
    return pos, trade


def close_position(portfolio, state, ticker, current_price, reason):
    if ticker not in portfolio.get("positions", {}):
        return None
    pos = portfolio["positions"][ticker]
    if pos["direction"] == "SHORT":
        pnl = (pos["entry_price"] - current_price) * pos["shares"]
    else:
        pnl = (current_price - pos["entry_price"]) * pos["shares"]
    pnl = round(pnl, 2)
    pnl_pct = round((pnl / pos["size_usd"]) * 100, 2)

    portfolio["cash"] += pos["size_usd"] + pnl
    del portfolio["positions"][ticker]
    save_json(PORTFOLIO_FILE, portfolio)

    if pnl < 0:
        state["consecutive_losses"] = state.get("consecutive_losses", 0) + 1
        if state["consecutive_losses"] >= MAX_CONSECUTIVE_LOSS:
            state["cooldown_until"] = time.time() + COOLDOWN_SECONDS
            log("⏸", f"{MAX_CONSECUTIVE_LOSS} ardışık kayıp → cooldown")
    else:
        state["consecutive_losses"] = 0
    state["daily_pnl"] = state.get("daily_pnl", 0) + pnl
    state["weekly_pnl"] = state.get("weekly_pnl", 0) + pnl
    save_json(STATE_FILE, state)

    trades = load_json(TRADES_FILE, [])
    for t in trades:
        if t["ticker"] == ticker and t["status"] == "OPEN":
            t.update({"status": "CLOSED", "exit_price": current_price,
                      "pnl": pnl, "pnl_pct": pnl_pct,
                      "close_reason": reason, "closed_at": datetime.now().isoformat()})
            break
    save_json(TRADES_FILE, trades)
    return {"ticker": ticker, "pnl": pnl, "pnl_pct": pnl_pct, "reason": reason}


def check_exits(portfolio, state, current_prices):
    exits = []
    to_close = []
    for ticker, pos in portfolio.get("positions", {}).items():
        price = current_prices.get(ticker)
        if not price:
            continue
        d = pos["direction"]
        if d == "LONG" and price <= pos["stop_loss"]:
            to_close.append((ticker, price, "STOP_LOSS"))
        elif d == "LONG" and price >= pos["take_profit"]:
            to_close.append((ticker, price, "TAKE_PROFIT"))
        elif d == "SHORT" and price >= pos["stop_loss"]:
            to_close.append((ticker, price, "STOP_LOSS"))
        elif d == "SHORT" and price <= pos["take_profit"]:
            to_close.append((ticker, price, "TAKE_PROFIT"))
        elif (time.time() - pos.get("opened_ts", time.time())) / 3600 > 72:
            to_close.append((ticker, price, "TIMEOUT_72H"))

    for ticker, price, reason in to_close:
        result = close_position(portfolio, state, ticker, price, reason)
        if result:
            exits.append(result)
    return exits


# =============================================
# TELEGRAM KOMUTLARI
# =============================================
def handle_command(text, chat_id):
    cmd = text.strip().lower().split()[0]

    if cmd in ("/start", "/help"):
        return (
            "🤖 <b>AI Trading Agent v2.0 — HYBRID</b>\n\n"
            "📐 Mekanik sinyal: EMA10/30 + RSI + MACD\n"
            "🧠 Claude filtre: Onayla/Reddet\n"
            f"💰 Sermaye: ${STARTING_CAPITAL:,}\n"
            f"⏱ Tarama: her {SCAN_INTERVAL//60}dk\n\n"
            "/portfolio — Portföy\n/trades — Trade geçmişi\n"
            "/stats — Performans\n/pause /resume"
        )

    elif cmd == "/portfolio":
        p = get_portfolio()
        total = p["cash"]
        msg = f"💼 <b>PORTFÖY</b>\n💵 Nakit: ${p['cash']:,.2f}\n\n"
        if p.get("positions"):
            for t, pos in p["positions"].items():
                total += pos["size_usd"]
                age = (time.time() - pos.get("opened_ts", time.time())) / 3600
                msg += (
                    f"{'📈' if pos['direction']=='LONG' else '📉'} <b>{t}</b> "
                    f"{pos['direction']} @ ${pos['entry_price']}\n"
                    f"  SL: ${pos['stop_loss']} TP: ${pos['take_profit']} | {age:.0f}h\n"
                    f"  Skor: {pos.get('signal_score','?')} | Güven: %{pos.get('confidence','?')}\n\n"
                )
        else:
            msg += "📭 Açık pozisyon yok\n"
        pnl = total - STARTING_CAPITAL
        msg += f"{'='*25}\n💰 ${total:,.2f} ({pnl:+,.2f})"
        return msg

    elif cmd == "/trades":
        trades = load_json(TRADES_FILE, [])
        closed = [t for t in trades if t["status"] == "CLOSED"][-10:]
        if not closed:
            return "📭 Kapanmış trade yok"
        msg = "📋 <b>SON TRADE'LER</b>\n"
        for t in reversed(closed):
            e = "✅" if t.get("pnl", 0) > 0 else "❌"
            msg += (
                f"\n{e} #{t['id']} {t['ticker']} {t['direction']}\n"
                f"  ${t['entry_price']} → ${t.get('exit_price','?')} | "
                f"${t.get('pnl',0):+.2f} ({t.get('pnl_pct',0):+.1f}%)\n"
                f"  {t.get('close_reason','?')}\n"
            )
        return msg

    elif cmd == "/stats":
        trades = load_json(TRADES_FILE, [])
        closed = [t for t in trades if t["status"] == "CLOSED"]
        s = load_json(STATE_FILE, {})
        if not closed:
            return f"📊 Veri yok | Tarama #{s.get('total_scans',0)}"
        wins = [t for t in closed if t.get("pnl", 0) > 0]
        losses = [t for t in closed if t.get("pnl", 0) <= 0]
        total_pnl = sum(t.get("pnl", 0) for t in closed)
        wr = len(wins) / len(closed) * 100
        avg_w = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_l = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 1
        pf = sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)) if losses and sum(t["pnl"] for t in losses) != 0 else 0

        msg = (
            f"📊 <b>PERFORMANS</b>\n\n"
            f"📈 {len(closed)} trade | ✅{len(wins)} ❌{len(losses)}\n"
            f"🎯 Win Rate: %{wr:.0f}\n"
            f"💰 PnL: ${total_pnl:+,.2f}\n"
            f"📊 Avg Win: ${avg_w:+.2f} | Avg Loss: ${avg_l:.2f}\n"
            f"⚡ Profit Factor: {pf:.2f}\n"
            f"🔄 Tarama #{s.get('total_scans',0)}\n"
        )
        if len(closed) >= 20:
            if wr >= 55 and pf >= 1.5:
                msg += "\n🟢 <b>Gerçek paraya geçiş değerlendir</b>"
            elif wr >= 50:
                msg += "\n🟡 <b>Daha fazla veri topla</b>"
            else:
                msg += "\n🔴 <b>Strateji gözden geçir</b>"
        return msg

    elif cmd == "/pause":
        s = load_json(STATE_FILE, {})
        s["paused"] = True
        save_json(STATE_FILE, s)
        return "⏸ Duraklatıldı"

    elif cmd == "/resume":
        s = load_json(STATE_FILE, {})
        s["paused"] = False
        s["cooldown_until"] = 0
        save_json(STATE_FILE, s)
        return "▶️ Devam"

    return "❓ /help"


def check_telegram():
    if not TELEGRAM_TOKEN:
        return
    try:
        s = load_json(STATE_FILE, {})
        offset = s.get("tg_offset", 0)
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"timeout": 0, "offset": offset + 1}, timeout=5,
        )
        for u in r.json().get("result", []):
            s["tg_offset"] = u["update_id"]
            msg = u.get("message", {})
            txt = msg.get("text", "")
            cid = str(msg.get("chat", {}).get("id", ""))
            if txt.startswith("/"):
                telegram_send(handle_command(txt, cid), cid)
        save_json(STATE_FILE, s)
    except:
        pass


# =============================================
# ANA DÖNGÜ
# =============================================
def main():
    print("=" * 50)
    print("🤖 AI TRADING AGENT v2.0 — HYBRID")
    print("📐 Mekanik: EMA10/30 crossover + RSI + MACD")
    print("🧠 Claude: Filtre (onayla/reddet)")
    print(f"📈 Stocks: {', '.join(STOCKS.keys())}")
    print(f"₿  Crypto: {', '.join(c['symbol'] for c in CRYPTO.values())}")
    print(f"⏱  Scan: {SCAN_INTERVAL//60}dk | SL: {DEFAULT_STOP_LOSS*100}% TP: {DEFAULT_TAKE_PROFIT*100}%")
    print("=" * 50)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ TELEGRAM gerekli!")
        return
    if not ANTHROPIC_API_KEY:
        print("⚠️ ANTHROPIC_API_KEY yok — Claude filtre devre dışı, sadece mekanik")

    init_portfolio()
    telegram_send(
        "🤖 <b>AI Trading Agent v2.0 — HYBRID Aktif!</b>\n\n"
        "📐 Mekanik sinyal: EMA10/30 + RSI + MACD\n"
        "🧠 Claude filtre: Onayla/Reddet\n"
        f"📈 {', '.join(STOCKS.keys())} + {', '.join(c['symbol'] for c in CRYPTO.values())}\n"
        f"💰 ${STARTING_CAPITAL:,} | ⏱ {SCAN_INTERVAL//60}dk\n\n"
        "/portfolio /trades /stats /help"
    )
start_dashboard()
    scan = 0
    while True:
        try:
            check_telegram()
            scan += 1
            state = load_json(STATE_FILE, {})
            state["total_scans"] = scan

            # Daily/weekly reset
            today = datetime.now().strftime("%Y-%m-%d")
            if state.get("daily_reset") != today:
                state["daily_pnl"] = 0
                state["daily_reset"] = today
            if datetime.now().weekday() == 0 and state.get("weekly_reset") != today:
                state["weekly_pnl"] = 0
                state["weekly_reset"] = today
            save_json(STATE_FILE, state)

            log("🔍", f"═══ TARAMA #{scan} ═══")

            # ── VERİ ÇEK ──
            all_data = {}
            current_prices = {}

            for ticker in STOCKS:
                d = fetch_stock_data(ticker)
                if d:
                    all_data[ticker] = d
                    current_prices[ticker] = d["price"]
                    log("📊", f"{ticker}: ${d['price']:,.2f} ({d['change_pct']:+.1f}%) RSI:{d['rsi']} {d['trend']} Skor→")
                time.sleep(0.3)

            crypto = fetch_crypto_data()
            for sym, d in crypto.items():
                all_data[sym] = d
                current_prices[sym] = d["price"]
                log("₿", f"{sym}: ${d['price']:,.2f} ({d['change_pct']:+.1f}%) RSI:{d['rsi']} {d['trend']}")

            if not all_data:
                log("⚠️", "Veri yok")
                time.sleep(SCAN_INTERVAL)
                continue

            portfolio = get_portfolio()

            # ── STOP LOSS / TAKE PROFIT ──
            exits = check_exits(portfolio, state, current_prices)
            for ex in exits:
                emoji = "💰" if ex["pnl"] > 0 else "💸"
                telegram_send(
                    f"{emoji} <b>{ex['reason']}</b>\n"
                    f"{ex['ticker']} | ${ex['pnl']:+.2f} ({ex['pnl_pct']:+.1f}%)"
                )

            # ── TEZ KONTROLÜ ──
            portfolio = get_portfolio()
            for ticker, pos in list(portfolio.get("positions", {}).items()):
                ta = all_data.get(ticker)
                if ta:
                    hold, reason = check_thesis(pos, ta)
                    if not hold:
                        result = close_position(portfolio, state, ticker, ta["price"], f"TEZ: {reason}")
                        if result:
                            emoji = "💰" if result["pnl"] > 0 else "💸"
                            telegram_send(
                                f"{emoji} <b>TEZ BOZULDU</b>\n"
                                f"{ticker} | {reason}\n${result['pnl']:+.2f} ({result['pnl_pct']:+.1f}%)"
                            )
                            portfolio = get_portfolio()

            # ── MEKANİK SİNYAL ÜRETİMİ ──
            raw_signals = []
            for ticker, ta in all_data.items():
                if ticker in portfolio.get("positions", {}):
                    continue  # Zaten pozisyonda
                signal = generate_signal(ta)
                if signal:
                    raw_signals.append(signal)
                    log("📡", f"SİNYAL: {signal['summary']}")

            log("📡", f"{len(raw_signals)} mekanik sinyal üretildi")

            # ── CLAUDE FİLTRE ──
            can_trade, block_reason = can_open_position(portfolio, state)
            if can_trade and raw_signals:
                approved = claude_filter(raw_signals, portfolio, state)
                log("🧠", f"{len(approved)}/{len(raw_signals)} onaylandı")

                # En yüksek confidence'dan başla
                approved.sort(key=lambda s: s.get("confidence", 0), reverse=True)

                for signal in approved:
                    portfolio = get_portfolio()
                    can_still, _ = can_open_position(portfolio, state)
                    if not can_still:
                        break
                    if signal["ticker"] in portfolio.get("positions", {}):
                        continue

                    pos, trade = open_position(portfolio, state, signal)
                    d_emoji = "📈" if signal["direction"] == "LONG" else "📉"

                    reasons_str = "\n".join([f"  • {r}" for r in signal.get("reasons", [])])
                    telegram_send(
                        f"{'🔴' if signal['confidence']>=80 else '🟡'} <b>YENİ POZİSYON</b> {d_emoji}\n"
                        f"#{trade['id']} | {signal['ticker']} {signal['direction']}\n\n"
                        f"💰 ${signal['price']:,.2f} | 💵 ${pos['size_usd']:,.0f}\n"
                        f"🛡 SL: ${pos['stop_loss']} | TP: ${pos['take_profit']}\n"
                        f"📐 Sinyal skoru: {signal['score']} | 🧠 Güven: %{signal['confidence']}\n\n"
                        f"<b>Sebepler:</b>\n{reasons_str}\n\n"
                        f"📝 {signal.get('thesis', '')}\n"
                        f"⏰ {datetime.now().strftime('%d.%m %H:%M')}"
                    )
                    time.sleep(1)
            elif not can_trade:
                log("⏸", f"Trade yok: {block_reason}")

            # ── ÖZET ──
            portfolio = get_portfolio()
            total = portfolio["cash"]
            for pos in portfolio.get("positions", {}).values():
                total += pos["size_usd"]
            pnl = total - STARTING_CAPITAL
            n_pos = len(portfolio.get("positions", {}))
          
            update_scan_data(
                {k: {key: v[key] for key in ["price","change_pct","rsi","trend","cross","ema_cross_recent","macd_hist","name","signal_summary"] if key in v} for k,v in all_data.items()},
                [{"ticker":s["ticker"],"direction":s["direction"],"score":s["score"],"rsi":s["rsi"],"reasons":s["reasons"]} for s in raw_signals],
                scan
            )
```

Sonra **Commit changes**.

## Adım 3: Railway'de PORT ekle

Railway → ai-trading-agent servisi → **Variables** → ekle:
```
PORT=8080
log("💼", f"${total:,.0f} ({pnl:+,.0f}) | {n_pos} poz | ${portfolio['cash']:,.0f} nakit")
            log("💤", f"{SCAN_INTERVAL//60}dk bekleniyor")

            # Günlük karne
            now = datetime.now()
            if now.hour == 23 and now.minute < SCAN_INTERVAL // 60 + 1:
                trades = load_json(TRADES_FILE, [])
                td = [t for t in trades if t["status"] == "CLOSED" and t.get("closed_at", "").startswith(today)]
                td_pnl = sum(t.get("pnl", 0) for t in td)
                telegram_send(
                    f"🌙 <b>KARNE</b>\n"
                    f"💼 ${total:,.0f} ({pnl:+,.0f})\n"
                    f"📊 Bugün: {len(td)} trade ${td_pnl:+,.2f}\n"
                    f"🔄 #{scan}"
                )

        except KeyboardInterrupt:
            telegram_send("⏹ Bot durdu.")
            break
        except Exception as e:
            log("❌", f"HATA: {e}")
            traceback.print_exc()
            try:
                telegram_send(f"⚠️ {str(e)[:100]}")
            except:
                pass

        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
