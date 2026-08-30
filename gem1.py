#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import time
import os
import random
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
# MEXC FUTURES API
# ============================================================
MEXC_BASE_URL = "https://contract.mexc.com/api/v1/contract/kline"

# MEXC Futures sembol formatı (Alt çizgi ile)
SYMBOLS = {
    "LTC_USDT": "LTC", "BTC_USDT": "BTC",
    "ETH_USDT": "ETH", "SOL_USDT": "SOL", "BNB_USDT": "BNB",
    "XRP_USDT": "XRP", "ADA_USDT": "ADA", "AVAX_USDT": "AVAX",
    "LINK_USDT": "LINK", "DOGE_USDT": "DOGE"
}

TIMEFRAME = "Min15"  # MEXC 15 dakikalık formatı
LIMIT = 100
LOOP_SECONDS = 60

STARTING_BALANCE_PER_COIN = 30.0
MARGIN_PER_TRADE = 25.0
LEVERAGE = 10.0
POSITION_SIZE = MARGIN_PER_TRADE * LEVERAGE
TAKE_PROFIT_PCT = 0.02
STOP_LOSS_PCT = 0.05
COMMISSION_RATE = 0.0004

STATE_FILE = "bot_state.json"
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
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0"
    }

    req = Request(url, data=payload, headers=headers, method="POST")

    for attempt in range(RETRY_COUNT):
        try:
            with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                return response.status == 200
        except HTTPError:
            time.sleep(1)
        except Exception:
            time.sleep(1)

    return False

def http_get_json(url, retries=2):
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for attempt in range(retries):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            if attempt < retries - 1:
                time.sleep(1) 
            else:
                return None
    return None

def get_klines(symbol):
    # MEXC Futures Klines Endpoint yapısı: /api/v1/contract/kline/{symbol}?interval=...
    url = f"{MEXC_BASE_URL}/{symbol}?interval={TIMEFRAME}"
    data = http_get_json(url)
    if data and data.get("success") and "data" in data:
        return data["data"]
    return None

def calc_ema(data, period):
    if len(data) < period:
        return []
    sma = sum(data[:period]) / period
    ema = [sma]
    k = 2 / (period + 1)
    for price in data[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema

def calc_atr(highs, lows, closes, period=20):
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1])
        )
        trs.append(tr)
    if len(trs) < period:
        return 0.0
    return sum(trs[-period:]) / period

def calc_rsi(closes, period=14):
    if len(closes) <= period:
        return 50.0
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

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def analyze(symbol):
    raw_data = get_klines(symbol)
    if not raw_data or "close" not in raw_data:
        return (symbol, None, None, None, None, None)

    closes = [float(x) for x in raw_data["close"]]
    highs = [float(x) for x in raw_data["high"]]
    lows = [float(x) for x in raw_data["low"]]

    if len(closes) < 50:
        return (symbol, None, None, None, None, None)

    # İşlem yapabilmek için son tamamlanmış mumu ve güncel mum verilerini ayırıyoruz
    closed_closes = closes[:-1]
    closed_highs = highs[:-1]
    closed_lows = lows[:-1]

    price = closes[-1]
    current_high = highs[-1]
    current_low = lows[-1]

    ema = calc_ema(closed_closes, 20)
    atr = calc_atr(closed_highs, closed_lows, closed_closes, 20)

    if not ema or atr == 0:
        return (symbol, None, None, None, current_high, current_low)

    kc_lower = ema[-1] - atr * 2
    kc_upper = ema[-1] + atr * 2
    rsi = calc_rsi(closed_closes, 14)

    signal = None
    if price < kc_lower and rsi <= 25:
        signal = "LONG"
    elif price > kc_upper and rsi >= 75:
        signal = "SHORT"

    return (symbol, signal, price, rsi, current_high, current_low)

# --- SUNUCU VE KEEP-ALIVE ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(b"<html><body><h1>MEXC Bot Aktif ve Calisiyor!</h1></body></html>")

    def log_message(self, format, *args):
        return

def run_health_check_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def self_ping():
    url = os.getenv("RENDER_EXTERNAL_URL", "https://buraya-render-linkini-yaz.onrender.com")
    while True:
        bekleme_suresi = random.randint(600, 720) 
        time.sleep(bekleme_suresi) 
        try:
            req = Request(url, headers={"User-Agent": "MEXCBot-KeepAlive"})
            with urlopen(req, timeout=10) as response:
                print(f"[{now_date_text()}] 🔄 Self-Ping atildi. (Durum: {response.status})")
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

    print("MEXC Bot başlatılıyor...")
    send_telegram_msg(f"🚀 <b>MEXC BOT BAŞLATILDI!</b>\nTarih: {now_date_text()}\nSistem Render üzerinde aktif.")
    time.sleep(3) 

    last_telegram_time = 0

    while True:
        try:
            trade_events = []
            total_unrealized_pnl = 0.0
            
            lines = []
            lines.append("🎯 <b>10X SANAL KELTNER RAPORU (MEXC)</b>")
            lines.append(f"🗓 <b>Tarih:</b> {now_date_text()}")
            lines.append(f"⚙️ <b>Kaldıraç:</b> {LEVERAGE:.0f}x | <b>Teminat:</b> {MARGIN_PER_TRADE:.0f} USDT\n")
            lines.append("<b>🪙 COIN DURUMLARI</b>")

            with ThreadPoolExecutor(max_workers=5) as executor:
                results = list(executor.map(analyze, SYMBOLS.keys()))

            analysis_dict = {r[0]: r[1:] for r in results}

            for symbol, name in SYMBOLS.items():
                signal, current_price, rsi, cur_high, cur_low = analysis_dict.get(symbol, (None, None, None, None, None))
                wallet = wallet_balances.get(symbol, STARTING_BALANCE_PER_COIN)

                if current_price is None:
                    lines.append(f"🔸 <b>{name}:</b> N/A")
                    lines.append(f"└ ⚪️ BOŞ | 💵 {wallet:.2f}$ | 📈 +0.00$")
                    continue

                pos = positions.get(symbol)
                unrealized_pnl = 0.0
                status_code = "BOŞ"

                # 1. Yeni pozisyon açma kontrolü
                if pos is None and signal in ("LONG", "SHORT") and wallet >= MARGIN_PER_TRADE:
                    trade_number += 1
                    if signal == "LONG":
                        tp = current_price * (1 + TAKE_PROFIT_PCT)
                        sl = current_price * (1 - STOP_LOSS_PCT)
                    else:
                        tp = current_price * (1 - TAKE_PROFIT_PCT)
                        sl = current_price * (1 + STOP_LOSS_PCT)

                    wallet_balances[symbol] = wallet - MARGIN_PER_TRADE
                    positions[symbol] = {
                        "id": trade_number, "side": signal, "entry": current_price,
                        "tp": tp, "sl": sl, "margin": MARGIN_PER_TRADE,
                        "leverage": LEVERAGE, "position_size": POSITION_SIZE
                    }
                    pos = positions[symbol]

                    trade_events.append(
                        f"🚨 <b>YENİ POZİSYON</b>\n"
                        f"Coin: {name} | Yön: {signal}\n"
                        f"Giriş: {current_price:.6f}\n"
                        f"TP: {tp:.6f} | SL: {sl:.6f}"
                    )

                # 2. Açık pozisyon yönetimi ve FİTİL KONTROLÜ
                if pos is not None:
                    side, entry = pos["side"], float(pos["entry"])
                    
                    # Fiyat fitillerini kontrol ederek TP veya SL'ye iğne atıp atmadığını yakalıyoruz
                    hit_tp = False
                    hit_sl = False
                    
                    if side == "LONG":
                        if cur_high >= pos["tp"]:
                            hit_tp = True
                        elif cur_low <= pos["sl"]:
                            hit_sl = True
                    else: # SHORT
                        if cur_low <= pos["tp"]:
                            hit_tp = True
                        elif cur_high >= pos["sl"]:
                            hit_sl = True

                    # Anlık PNL hesaplaması (görüntü için güncel fiyat baz alınır)
                    pct = (current_price - entry) / entry if side == "LONG" else (entry - current_price) / entry
                    gross_pnl = POSITION_SIZE * pct
                    commission = POSITION_SIZE * COMMISSION_RATE
                    unrealized_pnl = gross_pnl - commission
                    total_unrealized_pnl += unrealized_pnl

                    if hit_tp or hit_sl:
                        # Eğer hedefe iğne attıysa, tam hedef fiyatı üzerinden realize edip kapatıyoruz
                        exit_price = pos["tp"] if hit_tp else pos["sl"]
                        exit_pct = (exit_price - entry) / entry if side == "LONG" else (entry - exit_price) / entry
                        final_pnl = (POSITION_SIZE * exit_pct) - commission

                        wallet_balances[symbol] += MARGIN_PER_TRADE + final_pnl
                        realized_pnl[symbol] = realized_pnl.get(symbol, 0.0) + final_pnl
                        positions[symbol] = None
                        status_code = "KAPALI"
                        res_text = "TAKE PROFIT (Kâr Al)" if hit_tp else "STOP LOSS (Zarar Kes)"

                        trade_events.append(
                            f"✅ <b>POZİSYON KAPANDI</b>\n"
                            f"Coin: {name} | Sonuç: {res_text}\n"
                            f"P/L: {final_pnl:+.2f} USDT\n"
                            f"Yeni Cüzdan: {wallet_balances[symbol]:.2f} USDT"
                        )
                    else:
                        status_code = "LONG" if side == "LONG" else "SHORT"

                display_wallet = wallet_balances[symbol] + (MARGIN_PER_TRADE + unrealized_pnl if positions.get(symbol) else 0)
                
                if status_code == "BOŞ": status_emoji = "⚪️ BOŞ"
                elif status_code == "LONG": status_emoji = "🟢 LONG"
                elif status_code == "SHORT": status_emoji = "🔴 SHORT"
                elif status_code == "KAPALI": status_emoji = "✅ KAP"
                
                lines.append(f"🔸 <b>{name}:</b> {current_price}")
                lines.append(f"└ {status_emoji} | 💵 {display_wallet:.2f}$ | 📈 {unrealized_pnl:+.2f}$")

            total_cash = sum(wallet_balances.values())
            total_realized = sum(realized_pnl.values())
            total_equity = total_cash + sum(float(p["margin"]) for p in positions.values() if p) + total_unrealized_pnl
            pnl_pct = (total_unrealized_pnl / total_equity * 100) if total_equity > 0 else 0.0

            lines.append("\n<b>📊 GENEL ÖZET</b>")
            lines.append(f"💵 <b>Toplam Varlık:</b> {total_equity:.2f} USDT")
            lines.append(f"📈 <b>Açık K/Z:</b> {total_unrealized_pnl:+.2f} USDT (<b>%{pnl_pct:+.2f}</b>)")
            lines.append(f"💰 <b>Realize K/Z:</b> {total_realized:+.2f} USDT")

            output_text = "\n".join(lines)
            print("\n" + output_text.replace('<b>', '').replace('</b>', ''))

            for event in trade_events:
                send_telegram_msg(event)

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
