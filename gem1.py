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
# MEXC FUTURES API AYARLARI
# ============================================================
MEXC_BASE_URL = "https://contract.mexc.com/api/v1/contract/kline"

# MEXC Futures sembol formatı (Alt çizgi ile)
SYMBOLS = {
    "BTC_USDT": "BTC", "ETH_USDT": "ETH", "SOL_USDT": "SOL",
    "BNB_USDT": "BNB", "AVAX_USDT": "AVAX", "LINK_USDT": "LINK",
    "XRP_USDT": "XRP", "DOGE_USDT": "DOGE", "ADA_USDT": "ADA", "DOT_USDT": "DOT"
}

TIMEFRAME = "Min15"  # MEXC 15 dakikalık formatı
LIMIT = 250  # EMA 200 hesaplayabilmek için limit
LOOP_SECONDS = 60

STARTING_BALANCE_PER_COIN = 50.0
MARGIN_PER_TRADE = 30.0
LEVERAGE = 5.0
POSITION_SIZE = MARGIN_PER_TRADE * LEVERAGE

# İZLEYEN STOP AYARLARI
TAKE_PROFIT_PCT = 1.00   # %100 (Etkisiz eleman yapıyoruz ki İzleyen Stop çalışsın)
STOP_LOSS_PCT = 0.015    # %1.5 İlk Zarar Kes
TRAILING_STOP_PCT = 0.015 # Zirveden %1.5 düştüğünde kârı alıp işlemi kapat
COMMISSION_RATE = 0.0004

STATE_FILE = "mexc_alfa_state.json"
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
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    for attempt in range(retries):
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except:
            time.sleep(1)
    return None

def get_klines(symbol):
    # MEXC Futures kline endpoint yapısı: /api/v1/contract/kline/{symbol}?interval=Min15
    url = f"{MEXC_BASE_URL}/{symbol}?interval={TIMEFRAME}"
    data = http_get_json(url)
    if data and data.get("success") and "data" in data:
        return data["data"]
    return None

# --- TEKNİK GÖSTERGELER ---
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

def calc_vwma(closes, volumes, period):
    if len(closes) < period: return 0.0
    cv = sum(c * v for c, v in zip(closes[-period:], volumes[-period:]))
    v_sum = sum(volumes[-period:])
    return cv / v_sum if v_sum > 0 else 0.0

def calc_stoch_rsi(closes, period=14, stoch_period=14, k_period=3, d_period=3):
    if len(closes) <= period: 
        return None, None
    
    rsi_series = []
    gains = []
    losses = []
    
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(abs(min(change, 0)))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        rsi_series.append(100.0)
    else:
        rs = avg_gain / avg_loss
        rsi_series.append(100.0 - (100.0 / (1.0 + rs)))

    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0)
        loss = abs(min(change, 0))
        
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        
        if avg_loss == 0:
            rsi_series.append(100.0)
        else:
            rs = avg_gain / avg_loss
            rsi_series.append(100.0 - (100.0 / (1.0 + rs)))

    if len(rsi_series) < stoch_period:
        return None, None

    stoch_rsi = []
    for i in range(stoch_period - 1, len(rsi_series)):
        window = rsi_series[i - stoch_period + 1 : i + 1]
        low_rsi = min(window)
        high_rsi = max(window)
        if high_rsi == low_rsi:
            stoch_rsi.append(0.0)
        else:
            stoch_rsi.append(100 * (window[-1] - low_rsi) / (high_rsi - low_rsi))

    if len(stoch_rsi) < k_period: 
        return None, None
    
    k_series = []
    for i in range(k_period - 1, len(stoch_rsi)):
        k_val = sum(stoch_rsi[i - k_period + 1 : i + 1]) / k_period
        k_series.append(k_val)

    if len(k_series) < d_period: 
        return None, None

    d_series = []
    for i in range(d_period - 1, len(k_series)):
        d_val = sum(k_series[i - d_period + 1 : i + 1]) / d_period
        d_series.append(d_val)

    return k_series[-2:], d_series[-2:]

def analyze(symbol):
    raw_data = get_klines(symbol)
    if not raw_data or "close" not in raw_data or len(raw_data["close"]) < 205:
        return (symbol, None, None, None, None, None)

    closes_all = [float(x) for x in raw_data["close"]]
    highs_all = [float(x) for x in raw_data["high"]]
    lows_all = [float(x) for x in raw_data["low"]]
    volumes_all = [float(x) for x in raw_data["vol"]]

    closed_closes = closes_all[:-1]
    closed_highs = highs_all[:-1]
    closed_lows = lows_all[:-1]
    closed_volumes = volumes_all[:-1]

    price = closes_all[-1]
    current_high = highs_all[-1]
    current_low = lows_all[-1]
    
    ema200 = calc_ema(closed_closes, 200)
    ema20 = calc_ema(closed_closes, 20)
    atr = calc_atr(closed_highs, closed_lows, closed_closes, 20)
    vwma = calc_vwma(closed_closes, closed_volumes, 20)
    
    k_last2, d_last2 = calc_stoch_rsi(closed_closes, period=14, stoch_period=14, k_period=3, d_period=3)

    if not ema200 or not ema20 or atr == 0 or not k_last2 or not d_last2:
        return (symbol, None, price, None, current_high, current_low)

    kc_lower = ema20[-1] - (atr * 1.5)
    kc_upper = ema20[-1] + (atr * 1.5)
    trend_ema = ema200[-1]

    prev_k, curr_k = k_last2
    prev_d, curr_d = d_last2

    signal = None
    
    if price > trend_ema and price < kc_lower and price < vwma and (prev_k <= prev_d and curr_k > curr_d and curr_k < 20):
        signal = "LONG"
        
    elif price < trend_ema and price > kc_upper and price > vwma and (prev_k >= prev_d and curr_k < curr_d and curr_k > 80):
        signal = "SHORT"

    return (symbol, signal, price, curr_k, current_high, current_low)

# --- SUNUCU VE ANA DÖNGÜ ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"MEXC Alfa Bot Active")
    def log_message(self, format, *args): return

def run_health_check_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def main():
    threading.Thread(target=run_health_check_server, daemon=True).start()

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

    print("MEXC Alfa Çoklu Doğrulama Sistemi (İzleyen Stop'lu) başlatılıyor...")
    send_telegram_msg(f"👑 <b>MEXC ALFA BOT BAŞLATILDI!</b>\nTarih: {now_date_text()}\nBorsa: MEXC Futures\nStrateji: Trend+Konum+Hacim+StochRSI + İZLEYEN STOP")
    time.sleep(3) 

    last_telegram_time = 0

    while True:
        try:
            trade_events = []
            total_unrealized_pnl = 0.0
            
            lines = []
            lines.append("🛡 <b>MEXC ALFA SANAL TRADE RAPORU</b>")
            lines.append(f"🗓 <b>Tarih:</b> {now_date_text()}")
            lines.append(f"⚙️ <b>Kaldıraç:</b> {LEVERAGE:.0f}x | <b>Teminat:</b> {MARGIN_PER_TRADE:.0f} USDT\n")
            lines.append("<b>🪙 COIN DURUMLARI</b>")

            with ThreadPoolExecutor(max_workers=5) as executor:
                results = list(executor.map(analyze, SYMBOLS.keys()))

            analysis_dict = {r[0]: r[1:] for r in results}

            for symbol, name in SYMBOLS.items():
                signal, current_price, stoch_k, cur_high, cur_low = analysis_dict.get(symbol, (None, None, None, None, None))
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
                        "leverage": LEVERAGE, "position_size": POSITION_SIZE,
                        "highest_price": current_price, # İzleyen stop için eklendi
                        "lowest_price": current_price   # İzleyen stop için eklendi
                    }
                    pos = positions[symbol]

                    trade_events.append(
                        f"🚨 <b>MEXC KESKİN NİŞANCI GİRİŞİ!</b>\n"
                        f"Coin: {name} | Yön: {signal}\n"
                        f"Giriş: {current_price:.6f}\n"
                        f"İzleyen Stop: %{TRAILING_STOP_PCT*100}"
                    )

                if pos is not None:
                    side, entry = pos["side"], float(pos["entry"])
                    
                    # 1. Zirve ve Dip Fiyatları Güncelle (İzleyen Stop İçin)
                    if "highest_price" not in pos: pos["highest_price"] = entry # Eski veriler hata vermesin diye
                    if "lowest_price" not in pos: pos["lowest_price"] = entry

                    pos["highest_price"] = max(pos["highest_price"], cur_high)
                    pos["lowest_price"] = min(pos["lowest_price"], cur_low)

                    # 2. Dinamik SL (İzleyen Stop) Hesapla
                    if side == "LONG":
                        trailing_sl = pos["highest_price"] * (1 - TRAILING_STOP_PCT)
                        pos["sl"] = max(pos["sl"], trailing_sl) # SL'yi sadece yukarı taşı
                    else: # SHORT
                        trailing_sl = pos["lowest_price"] * (1 + TRAILING_STOP_PCT)
                        pos["sl"] = min(pos["sl"], trailing_sl) # SL'yi sadece aşağı taşı

                    # Anlık PNL hesaplama
                    pct = (current_price - entry) / entry if side == "LONG" else (entry - current_price) / entry
                    gross_pnl = POSITION_SIZE * pct
                    unrealized_pnl = gross_pnl - (POSITION_SIZE * COMMISSION_RATE)
                    total_unrealized_pnl += unrealized_pnl

                    # 3. FİTİL KONTROLÜ (Sadece SL tetiklenmesi beklenir)
                    hit_sl = False

                    if side == "LONG":
                        if cur_low <= pos["sl"]:
                            hit_sl = True
                    else:  # SHORT
                        if cur_high >= pos["sl"]:
                            hit_sl = True

                    if hit_sl:
                        exit_price = pos["sl"]
                        exit_pct = (exit_price - entry) / entry if side == "LONG" else (entry - exit_price) / entry
                        final_pnl = (POSITION_SIZE * exit_pct) - (POSITION_SIZE * COMMISSION_RATE)

                        wallet_balances[symbol] += MARGIN_PER_TRADE + final_pnl
                        realized_pnl[symbol] += final_pnl
                        positions[symbol] = None
                        
                        # Bildirim için sonucun Kâr mı Zarar mı olduğunu tespit et
                        if final_pnl > 0:
                            status_code = "KAPALI"
                            res_text = "🎯 İZLEYEN STOP (KÂR ALINDI)"
                        else:
                            status_code = "KAPALI"
                            res_text = "🛑 ZARAR KES (STOP LOSS)"

                        trade_events.append(
                            f"✅ <b>POZİSYON KAPANDI</b>\n"
                            f"Coin: {name} | Sonuç: {res_text}\n"
                            f"P/L: {final_pnl:+.2f} USDT\n"
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

            lines.append("\n<b>📊 MEXC ALFA GENEL ÖZET</b>")
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
