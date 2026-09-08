#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import logging
import threading
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen

# ============================================================
# 0. RENDER & UPTIMEROBOT İÇİN DAHİLİ HTTP SUNUCUSU
# ============================================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"MEXC Alpha Trading Bot is alive and running!")
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()
    def log_message(self, format, *args):
        return

def start_health_check_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

threading.Thread(target=start_health_check_server, daemon=True).start()

# ============================================================
# LOGGING VE KONFİGÜRASYON
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN", "BURAYA_BOT_TOKENINI_YAZ")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID", "BURAYA_CHAT_ID_YAZ")
SCORE_THRESHOLD = float(os.getenv("SCORE_THRESHOLD", "13.5"))
DB_FILE = os.getenv("DB_FILE", "trades_db.json")

# ============================================================
# 15 ADET SEÇİLEN COİN LİSTESİ (MEXC Formatı)
# ============================================================
SYMBOLS = {
    "BTC_USDT": "BTC",
    "ETH_USDT": "ETH",
    "SOL_USDT": "SOL",
    "BNB_USDT": "BNB",
    "XRP_USDT": "XRP",
    "ADA_USDT": "ADA",
    "DOGE_USDT": "DOGE",
    "AVAX_USDT": "AVAX",
    "LINK_USDT": "LINK",
    "DOT_USDT": "DOT",
    "NEAR_USDT": "NEAR",
    "MATIC_USDT": "MATIC",
    "ARB_USDT": "ARB",
    "SUI_USDT": "SUI",
    "FTM_USDT": "FTM",
}

TIMEFRAME = "60m"  # MEXC 1h için 60m kullanır
LIMIT = 150
LOOP_SECONDS = 60

STARTING_BALANCE_PER_COIN = 50.0
MARGIN_PER_TRADE = 30.0
LEVERAGE = 5.0
POSITION_SIZE = MARGIN_PER_TRADE * LEVERAGE

TAKE_PROFIT_PCT = 0.035
STOP_LOSS_PCT = 0.025
COMMISSION_RATE = 0.0004
TURKEY_TZ = timezone(timedelta(hours=3))

def now_date_text():
    return datetime.now(TURKEY_TZ).strftime("%d.%m.%Y %H:%M:%S")

def send_telegram_msg(message):
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN == "BURAYA_BOT_TOKENINI_YAZ":
        print(f"\n[TELEGRAM UYARI]:\n{message}\n")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
    try:
        req = Request(url, data=payload, headers=headers, method="POST")
        with urlopen(req, timeout=10) as res:
            return res.status == 200
    except Exception as e:
        print(f"Telegram Gönderim Hatası: {e}")
        return False

# ============================================================
# MEXC DOĞRUDAN VERİ ÇEKME (Render Engelini Aşar)
# ============================================================
def get_klines_df(symbol):
    try:
        url = f"https://api.mexc.com/api/v3/klines?symbol={symbol}&interval={TIMEFRAME}&limit={LIMIT}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            if data and isinstance(data, list) and len(data) > 0:
                # Pandas DataFrame dönüşümü
                import pandas as pd
                df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume'])
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = df[col].astype(float)
                return df
    except Exception as e:
        print(f"MEXC Veri çekme hatası ({symbol}): {e}")
    return None

# ============================================================
# SMC & SKORLAMA MOTORU
# ============================================================
def calculate_smc_analysis(df, symbol):
    import numpy as np
    import pandas as pd
    if df is None or len(df) < 50:
        return "BOŞ", 0.0, 50.0, 0.0

    closes = df["close"].values
    highs = df["high"].values
    lows = df["low"].values
    volumes = df["volume"].values
    price = closes[-1]
    
    ma50 = pd.Series(closes).rolling(50).mean().iloc[-1]
    ma100 = pd.Series(closes).rolling(min(100, len(closes))).mean().iloc[-1]
    ma200 = pd.Series(closes).rolling(min(200, len(closes))).mean().iloc[-1]
    
    delta = pd.Series(closes).diff()
    gain = (delta.where(delta > 0, 0)).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    current_rsi = rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 50.0

    info = {
        "sellside_sweep": lows[-1] <= np.min(lows[-10:-1]),
        "buyside_sweep": highs[-1] >= np.max(highs[-10:-1]),
        "bullish_ob": closes[-1] > closes[-2] and volumes[-1] > np.mean(volumes[-10:]),
        "bearish_ob": closes[-1] < closes[-2] and volumes[-1] > np.mean(volumes[-10:]),
        "bullish_bos": closes[-1] > np.max(highs[-15:-1]),
        "bearish_bos": closes[-1] < np.min(lows[-15:-1]),
        "bullish_fvg": (highs[-2] < lows[-1]),
        "bearish_fvg": (lows[-2] > highs[-1]),
        "above_ma50": price > ma50,
        "above_ma100": price > ma100,
        "above_ma200": price > ma200,
        "rsi": current_rsi
    }

    skor_l, skor_s = 0.0, 0.0
    if info.get("sellside_sweep"): skor_l += 1.57
    if info.get("buyside_sweep"): skor_s += 1.57
    if info.get("bullish_ob"): skor_l += 1.57
    if info.get("bearish_ob"): skor_s += 1.57
    if info.get("bullish_bos"): skor_l += 3.15
    if info.get("bearish_bos"): skor_s += 3.15
    if info.get("bullish_fvg"): skor_l += 2.70
    if info.get("bearish_fvg"): skor_s += 2.70

    if info.get("above_ma50"): skor_l += 2.02
    else: skor_s += 2.02
    if info.get("above_ma100"): skor_l += 0.90
    else: skor_s += 0.90
    if info.get("above_ma200"): skor_l += 1.35
    else: skor_s += 1.35
    if current_rsi > 50: skor_l += 1.12
    else: skor_s += 1.12

    if skor_l >= skor_s and skor_l >= SCORE_THRESHOLD:
        return "LONG", price, current_rsi, skor_l
    elif skor_s > skor_l and skor_s >= SCORE_THRESHOLD:
        return "SHORT", price, current_rsi, skor_s
    
    return "BOŞ", price, current_rsi, max(skor_l, skor_s)

def analyze(symbol_tuple):
    symbol, name = symbol_tuple
    df = get_klines_df(symbol)
    if df is None:
        return symbol, None, None, 0.0, 0.0
    signal, price, rsi, score = calculate_smc_analysis(df, symbol)
    return symbol, signal, price, rsi, score

# ============================================================
# STATE MANAGEMENT (DB)
# ============================================================
def save_state(positions, wallet_balances, realized_pnl, trade_number):
    state = {
        "positions": positions,
        "wallet_balances": wallet_balances,
        "realized_pnl": realized_pnl,
        "trade_number": trade_number,
        "last_save": now_date_text(),
    }
    try:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Durum kaydedilemedi: {e}")

def load_state():
    if not os.path.exists(DB_FILE):
        return None
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

# ============================================================
# ANA DÖNGÜ
# ============================================================
def main():
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

    print("MEXC Pro Alfa Trade Bot Başlatılıyor...")

    initial_lines = [
        "🛡 <b>MEXC PRO ALFA BAŞLANGIÇ RAPORU</b>",
        f"🗓 <b>Tarih:</b> {now_date_text()}",
        f"⚙️ <b>Kaldıraç:</b> {LEVERAGE:.0f}x | <b>Teminat:</b> {MARGIN_PER_TRADE:.0f} USDT\n",
        "🪙 <b>COIN DURUMLARI</b>"
    ]
    for symbol, name in SYMBOLS.items():
        initial_lines.append(f"🔸 <b>{name}:</b> Yükleniyor... | ⚪️ BOŞ | 💵 50.00$ | 📈 +0.00$")
    initial_lines.append("\n📊 <b>GENEL PORTFÖY ÖZETİ</b>")
    initial_lines.append(f"💵 <b>Toplam Varlık:</b> {len(SYMBOLS)*50.0:.2f} USDT")
    initial_lines.append("📈 <b>Açık K/Z:</b> +0.00 USDT (%+0.00)")
    initial_lines.append(f"💰 <b>Realize K/Z:</b> +0.00 USDT")

    send_telegram_msg("\n".join(initial_lines))

    while True:
        try:
            trade_events = []
            total_unrealized_pnl = 0.0
            position_activity_detected = False

            with ThreadPoolExecutor(max_workers=5) as executor:
                results = list(executor.map(analyze, SYMBOLS.items()))

            analysis_dict = {r[0]: r[1:] for r in results}

            lines = [
                "🛡 <b>MEXC PRO ALFA TRADE RAPORU</b>",
                f"🗓 <b>Tarih:</b> {now_date_text()}",
                f"⚙️ <b>Kaldıraç:</b> {LEVERAGE:.0f}x | <b>Teminat:</b> {MARGIN_PER_TRADE:.0f} USDT\n",
                "🪙 <b>COIN DURUMLARI</b>"
            ]

            for symbol, name in SYMBOLS.items():
                signal, current_price, rsi, score = analysis_dict.get(symbol, (None, None, None, 0.0))
                wallet = wallet_balances.get(symbol, STARTING_BALANCE_PER_COIN)

                if current_price is None:
                    lines.append(f"🔸 <b>{name}:</b> N/A\n└ ⚪️ BOŞ | 💵 {wallet:.2f}$ | 📈 +0.00$")
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
                    position_activity_detected = True

                    trade_events.append(
                        f"🚨 <b>SMC NİŞANCI GİRİŞİ ({name})!</b>\n"
                        f"Yön: {signal} | Skor: {score:.1f}/20\n"
                        f"Giriş Fiyatı: {current_price:.4f}\n"
                        f"Hedef (TP): {tp:.4f} | Stop (SL): {sl:.4f}"
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
                        exit_pnl = unrealized_pnl
                        wallet_balances[symbol] += MARGIN_PER_TRADE + exit_pnl
                        realized_pnl[symbol] = realized_pnl.get(symbol, 0.0) + exit_pnl
                        positions[symbol] = None
                        position_activity_detected = True

                        res_text = "🎯 TAKE PROFIT" if hit_tp else "🛑 STOP LOSS"
                        trade_events.append(
                            f"✅ <b>İŞLEM KAPANDI ({name})</b>\n"
                            f"Sonuç: {res_text}\n"
                            f"Net P/L: {exit_pnl:+.2f} USDT\n"
                            f"Güncel Kasa: {wallet_balances[symbol]:.2f} USDT"
                        )
                    else:
                        status_code = side

                display_wallet = wallet_balances[symbol] + (MARGIN_PER_TRADE + unrealized_pnl if positions.get(symbol) else 0)
                status_emoji = {"BOŞ": "⚪️ BOŞ", "LONG": "🟢 LONG", "SHORT": "🔴 SHORT"}[status_code]

                lines.append(f"🔸 <b>{name}:</b> {current_price}\n└ {status_emoji} | 💵 {display_wallet:.2f}$ | 📈 {unrealized_pnl:+.2f}$")

            total_cash = sum(wallet_balances.values())
            total_realized = sum(realized_pnl.values())
            total_equity = total_cash + sum(float(p["margin"]) for p in positions.values() if p) + total_unrealized_pnl
            pnl_pct = (total_unrealized_pnl / total_equity * 100) if total_equity > 0 else 0.0

            lines.append("\n📊 <b>GENEL PORTFÖY ÖZETİ</b>")
            lines.append(f"💵 <b>Toplam Varlık:</b> {total_equity:.2f} USDT")
            lines.append(f"📈 <b>Açık K/Z:</b> {total_unrealized_pnl:+.2f} USDT (<b>%{pnl_pct:+.2f}</b>)")
            lines.append(f"💰 <b>Realize K/Z:</b> {total_realized:+.2f} USDT")

            report_output = "\n".join(lines)

            if position_activity_detected:
                send_telegram_msg("🚨 <b>PORTFÖY HAREKETİ TESPİT EDİLDİ!</b>\n\n" + report_output)

            for event in trade_events:
                send_telegram_msg(event)

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
