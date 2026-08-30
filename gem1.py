#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import time
import os
from datetime import datetime, timezone, timedelta
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

# ============================================================
# TELEGRAM BİLGİLERİ (Render Environment Variables)
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================
# ALFA BOT AYARLARI
# ============================================================
BASE_URLS = [
    "https://fapi.binance.com/fapi/v1/klines",
    "https://fapi1.binance.com/fapi/v1/klines",
    "https://fapi2.binance.com/fapi/v1/klines"
]

SYMBOLS = {
    "BTCUSDT": "BTC", "ETHUSDT": "ETH", "SOLUSDT": "SOL",
    "BNBUSDT": "BNB", "AVAXUSDT": "AVAX", "LINKUSDT": "LINK",
    "XRPUSDT": "XRP", "DOGEUSDT": "DOGE", "ADAUSDT": "ADA", "DOTUSDT": "DOT"
}

TIMEFRAME = "15m"
LIMIT = 250
LOOP_SECONDS = 60

STARTING_BALANCE_PER_COIN = 50.0
MARGIN_PER_TRADE = 30.0
LEVERAGE = 5.0
POSITION_SIZE = MARGIN_PER_TRADE * LEVERAGE
TAKE_PROFIT_PCT = 0.025
STOP_LOSS_PCT = 0.015
COMMISSION_RATE = 0.0004

STATE_FILE = "alfa_state.json"
REQUEST_TIMEOUT = 10
RETRY_COUNT = 3
TELEGRAM_NOTIFY_INTERVAL = 15 * 60
TURKEY_TZ = timezone(timedelta(hours=3))

def now_date_text():
    return datetime.now(TURKEY_TZ).strftime("%d.%m.%Y %H:%M:%S")

def save_state(positions, wallet_balances, realized_pnl, trade_number):
    state = {
        "positions": positions,
        "wallet_balances": wallet_balances,
        "realized_pnl": realized_pnl,
        "trade_number": trade_number,
        "last_save": now_date_text()
    }
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Durum kaydedilemedi: {e}")

def load_state():
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def send_telegram_msg(message, parse_mode="HTML"):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": parse_mode}).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

    req = Request(url, data=payload, headers=headers, method="POST")
    for _ in range(RETRY_COUNT):
        try:
            with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                return response.status == 200
        except:
            time.sleep(1)
    return False

def http_get_json(url, retries=2):
    headers = {"User-Agent": "Mozilla/5.0"}
    for attempt in range(retries):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except:
            time.sleep(1)
    return None

def get_all_prices():
    """Coingecko API üzerinden IP engeline takılmadan toplu fiyat çeker."""
    url = "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,ethereum,solana,binancecoin,avalanche-2,chainlink,ripple,dogecoin,cardano,polkadot&vs_currencies=usd"
    data = http_get_json(url)
    
    if data and isinstance(data, dict):
        mapping = {
            "bitcoin": "BTCUSDT",
            "ethereum": "ETHUSDT",
            "solana": "SOLUSDT",
            "binancecoin": "BNBUSDT",
            "avalanche-2": "AVAXUSDT",
            "chainlink": "LINKUSDT",
            "ripple": "XRPUSDT",
            "dogecoin": "DOGEUSDT",
            "cardano": "ADAUSDT",
            "polkadot": "DOTUSDT"
        }
        
        prices = {}
        for cg_id, symbol in mapping.items():
            if cg_id in data and "usd" in data[cg_id]:
                prices[symbol] = float(data[cg_id]["usd"])
        
        if prices:
            return prices

    return {}

def get_klines(symbol):
    for base_url in BASE_URLS:
        url = f"{base_url}?symbol={symbol}&interval={TIMEFRAME}&limit={LIMIT}"
        data = http_get_json(url)
        if data and isinstance(data, list) and len(data) > 0:
            return data
    return None

def calc_ema(data, period):
    if len(data) < period: return []
    sma = sum(data[:period]) / period
    ema = [sma]
    k = 2 / (period + 1)
    for price in data[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema

def calc_atr(highs, lows, closes, period=20):
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1, len(closes))]
    if len(trs) < period: return 0.0
    return sum(trs[-period:]) / period

def calc_rsi(closes, period=14):
    if len(closes) <= period: return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0: return 100.0
    return 100 - (100 / (1 + (avg_gain / avg_loss)))

def calc_vwma(closes, volumes, period):
    if len(closes) < period: return 0.0
    cv = sum(c * v for c, v in zip(closes[-period:], volumes[-period:]))
    v_sum = sum(volumes[-period:])
    return cv / v_sum if v_sum > 0 else 0.0

def analyze(symbol, all_prices):
    data = get_klines(symbol)
    current_price = all_prices.get(symbol)

    if not data or len(data) < 205 or current_price is None:
        return (symbol, None, current_price, None)

    closed = data[:-1]
    closes = [float(row[4]) for row in closed]
    highs = [float(row[2]) for row in closed]
    lows = [float(row[3]) for row in closed]
    volumes = [float(row[5]) for row in closed]
    
    ema200 = calc_ema(closes, 200)
    ema20 = calc_ema(closes, 20)
    atr = calc_atr(highs, lows, closes, 20)
    rsi = calc_rsi(closes, 14)
    vwma = calc_vwma(closes, volumes, 20)

    if not ema200 or not ema20 or atr == 0:
        return (symbol, None, current_price, None)

    kc_lower = ema20[-1] - (atr * 1.5)
    kc_upper = ema20[-1] + (atr * 1.5)
    trend_ema = ema200[-1]

    signal = None
    if current_price > trend_ema and current_price < kc_lower and rsi <= 30 and current_price < vwma:
        signal = "LONG"
    elif current_price < trend_ema and current_price > kc_upper and rsi >= 70 and current_price > vwma:
        signal = "SHORT"

    return (symbol, signal, current_price, rsi)

# --- SUNUCU, ANA DÖNGÜ VE KENDİ KENDİNİ UYANDIRMA SİSTEMİ ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body><h1>Alfa Bot Aktif ve Calisiyor!</h1></body></html>")
        
    def log_message(self, format, *args): 
        return

def run_health_check_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def self_ping():
    url = "https://alfa-bot-pj2b.onrender.com"
    while True:
        time.sleep(600)  
        try:
            req = Request(url, headers={"User-Agent": "AlfaBot-KeepAlive"})
            with urlopen(req, timeout=10) as response:
                print(f"[{now_date_text()}] 🔄 Self-Ping: Bot uyanik tutuluyor. (Durum: {response.status})")
        except Exception as e:
            print(f"[{now_date_text()}] ⚠️ Self-Ping hatasi: {e}")

def main():
    threading.Thread(target=run_health_check_server, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()

    state = load_state()
    if state:
        positions = state.get("positions", {s: None for s in SYMBOLS})
        wallet_balances = state.get("wallet_balances", {s: STARTING_BALANCE_PER_COIN for s in SYMBOLS})
        realized_pnl = state.get("realized_pnl", {s: 0.0 for s in SYMBOLS})
        trade_number = state.get("trade_number", 0)
    else:
        positions = {s: None for s in SYMBOLS}
        wallet_balances = {s: STARTING_BALANCE_PER_COIN for s in SYMBOLS}
        realized_pnl = {s: 0.0 for s in SYMBOLS}
        trade_number = 0

    print("Alfa Çoklu Doğrulama Sistemi başlatılıyor (CoinGecko API)...")
    send_telegram_msg(f"👑 <b>ALFA BOT BAŞLATILDI!</b>\nTarih: {now_date_text()}\nStrateji: Trend+Konum+Hacim+Momentum")
    time.sleep(3) 

    last_telegram_time = 0

    while True:
        try:
            trade_events = []
            total_unrealized_pnl = 0.0
            
            # 1. Tüm fiyatları CoinGecko üzerinden IP engeli olmadan çekiyoruz
            all_prices = get_all_prices()

            lines = []
            lines.append("🛡 <b>ALFA SANAL TRADE RAPORU</b>")
            lines.append(f"🗓 <b>Tarih:</b> {now_date_text()}")
            lines.append(f"⚙️ <b>Kaldıraç:</b> {LEVERAGE:.0f}x | <b>Teminat:</b> {MARGIN_PER_TRADE:.0f} USDT\n")
            lines.append("<b>🪙 COIN DURUMLARI</b>")

            with ThreadPoolExecutor(max_workers=5) as executor:
                results = list(executor.map(lambda s: analyze(s, all_prices), SYMBOLS.keys()))

            analysis_dict = {r[0]: r[1:] for r in results}

            for symbol, name in SYMBOLS.items():
                signal, current_price, rsi = analysis_dict.get(symbol, (None, None, None))
                wallet = wallet_balances.get(symbol, STARTING_BALANCE_PER_COIN)

                if current_price is None:
                    lines.append(f"🔸 <b>{name}:</b> N/A")
                    lines.append(f"└ ⚪️ YÜKLENİYOR | 💵 {wallet:.2f}$ | 📈 +0.00$")
                    continue

                pos = positions.get(symbol)
                unrealized_pnl = 0.0
                status_code = "BOŞ"

                if pos is None and signal in ("LONG", "SHORT") and wallet >= MARGIN_PER_TRADE:
                    trade_number += 1
                    tp = current_price * (1 + TAKE_PROFIT_PCT) if signal == "LONG" else current_price * (1 - TAKE_PROFIT_PCT)
                    sl = current_price * (1 - STOP_LOSS_PCT) if signal == "LONG" else current_price * (1 + STOP_LOSS_PCT)

                    wallet_balances[symbol] -= MARGIN_PER_TRADE
                    positions[symbol] = {
                        "id": trade_number, "side": signal, "entry": current_price,
                        "tp": tp, "sl": sl, "margin": MARGIN_PER_TRADE,
                        "leverage": LEVERAGE, "position_size": POSITION_SIZE
                    }
                    pos = positions[symbol]

                    trade_events.append(
                        f"🚨 <b>KESKİN NİŞANCI GİRİŞİ!</b>\n"
                        f"Coin: {name} | Yön: {signal}\n"
                        f"Giriş: {current_price:.6f}\n"
                        f"TP: {tp:.6f} | SL: {sl:.6f}"
                    )

                if pos is not None:
                    side, entry = pos["side"], float(pos["entry"])
                    pct = (current_price - entry) / entry if side == "LONG" else (entry - current_price) / entry
                    gross_pnl = POSITION_SIZE * pct
                    unrealized_pnl = gross_pnl - (POSITION_SIZE * COMMISSION_RATE)
                    total_unrealized_pnl += unrealized_pnl

                    hit_tp = (side == "LONG" and current_price >= pos["tp"]) or (side == "SHORT" and current_price <= pos["tp"])
                    hit_sl = (side == "LONG" and current_price <= pos["sl"]) or (side == "SHORT" and current_price >= pos["sl"])

                    if hit_tp or hit_sl:
                        wallet_balances[symbol] += MARGIN_PER_TRADE + unrealized_pnl
                        realized_pnl[symbol] += unrealized_pnl
                        positions[symbol] = None
                        status_code = "KAPALI"
                        res_text = "🎯 TAKE PROFIT" if hit_tp else "🛑 STOP LOSS"

                        trade_events.append(
                            f"✅ <b>POZİSYON KAPANDI</b>\n"
                            f"Coin: {name} | Sonuç: {res_text}\n"
                            f"P/L: {unrealized_pnl:+.2f} USDT\n"
                            f"Kasa: {wallet_balances[symbol]:.2f} USDT"
                        )
                    else:
                        status_code = side

                display_wallet = wallet_balances[symbol] + (MARGIN_PER_TRADE + unrealized_pnl if positions.get(symbol) else 0)
                status_emoji = {"BOŞ": "⚪️ BOŞ", "LONG": "🟢 LONG", "SHORT": "🔴 SHORT", "KAPALI": "✅ KAP"}[status_code]
                
                lines.append(f"🔸 <b>{name}:</b> {current_price}")
                lines.append(f"└ {status_emoji} | 💵 {display_wallet:.2f}$ | 📈 {unrealized_pnl:+.2f}$")

            total_cash = sum(wallet_balances.values())
            total_realized = sum(realized_pnl.values())
            total_equity = total_cash + sum(float(p["margin"]) for p in positions.values() if p) + total_unrealized_pnl
            pnl_pct = (total_unrealized_pnl / total_equity * 100) if total_equity > 0 else 0.0

            lines.append("\n<b>📊 ALFA GENEL ÖZET</b>")
            lines.append(f"💵 <b>Toplam Varlık:</b> {total_equity:.2f} USDT")
            lines.append(f"📈 <b>Açık K/Z:</b> {total_unrealized_pnl:+.2f} USDT (<b>%{pnl_pct:+.2f}</b>)")
            lines.append(f"💰 <b>Realize K/Z:</b> {total_realized:+.2f} USDT")

            output_text = "\n".join(lines)
            print("\n" + output_text.replace('<b>', '').replace('</b>', ''))

            for event in trade_events: send_telegram_msg(event)

            now_ts = time.time()
            if now_ts - last_telegram_time >= TELEGRAM_NOTIFY_INTERVAL:
                send_telegram_msg(output_text) 
                last_telegram_time = now_ts

            save_state(positions, wallet_balances, realized_pnl, trade_number)
            time.sleep(LOOP_SECONDS)

        except KeyboardInterrupt:
            print("\nBot kapatılıyor...")
            save_state(positions, wallet_balances, realized_pnl, trade_number)
            break
        except Exception as e:
            print(f"Hata oluştu: {e}")
            time.sleep(15)

if __name__ == "__main__":
    main()
