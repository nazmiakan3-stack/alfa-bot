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

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================
# MEXC FUTURES API VE STRATEJİ AYARLARI
# ============================================================
MEXC_BASE_URL = "https://contract.mexc.com/api/v1/contract/kline"
SYMBOLS = {
    "BTC_USDT": "BTC",
    "ETH_USDT": "ETH",
    "SOL_USDT": "SOL",
    "BNB_USDT": "BNB",
    "AVAX_USDT": "AVAX",
    "LINK_USDT": "LINK",
    "XRP_USDT": "XRP",
    "DOGE_USDT": "DOGE",
    "ADA_USDT": "ADA",
    "DOT_USDT": "DOT",
}

TIMEFRAME = "Min15"
LIMIT = 250
LOOP_SECONDS = 60

STARTING_BALANCE_PER_COIN = 50.0
MARGIN_PER_TRADE = 30.0
LEVERAGE = 5.0
POSITION_SIZE = MARGIN_PER_TRADE * LEVERAGE

TAKE_PROFIT_PCT = 0.035
STOP_LOSS_PCT = 0.025
COMMISSION_RATE = 0.0004

STATE_FILE = "mexc_alfa_state.json"
REQUEST_TIMEOUT = 10
RETRY_COUNT = 3
TURKEY_TZ = timezone(timedelta(hours=3))

def now_date_text():
    return datetime.now(TURKEY_TZ).strftime("%d.%m.%Y %H:%M:%S")

def save_state(positions, wallet_balances, realized_pnl, trade_number):
    state = {
        "positions": positions,
        "wallet_balances": wallet_balances,
        "realized_pnl": realized_pnl,
        "trade_number": trade_number,
        "last_save": now_date_text(),
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
        print(f"[TELEGRAM UYARI] Token veya Chat ID eksik:\n{message}")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

    req = Request(url, data=payload, headers=headers, method="POST")
    for _ in range(RETRY_COUNT):
        try:
            with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
                return response.status == 200
        except Exception as e:
            print(f"Telegram gönderim hatası: {e}")
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
            time.sleep(1)
    return None

def get_klines(symbol):
    url = f"{MEXC_BASE_URL}/{symbol}?interval={TIMEFRAME}"
    data = http_get_json(url)
    if data and data.get("success") and "data" in data:
        return data["data"]
    return None

def analyze(symbol_tuple):
    symbol, name = symbol_tuple
    raw_data = get_klines(symbol)
    if not raw_data or len(raw_data) < 30:
        return symbol, None, 50.0, 0.0
    
    try:
        # MEXC Futures kline verisi genellikle sözlük veya liste formatındadır
        closes = []
        for item in raw_data:
            if isinstance(item, dict) and "close" in item:
                closes.append(float(item["close"]))
            elif isinstance(item, list) and len(item) > 2:
                closes.append(float(item[2])) # Format detayına göre kapanış
        
        if len(closes) < 20:
            return symbol, None, 50.0, 0.0
            
        current_price = closes[-1]
        # Basit bir RSI ve Skorlama Örneği
        return symbol, "BOŞ", current_price, 10.0
    except Exception as e:
        print(f"Analiz hatası ({symbol}): {e}")
        return symbol, None, 50.0, 0.0

def main():
    print("MEXC Alfa Trade Bot Başlatılıyor...")
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

    send_telegram_msg(f"🛡 <b>MEXC ALFA BAŞLANGIÇ RAPORU</b>\n🗓 Tarih: {now_date_text()}\n⚙️ Kaldıraç: {LEVERAGE}x")

    while True:
        try:
            with ThreadPoolExecutor(max_workers=5) as executor:
                results = list(executor.map(analyze, SYMBOLS.items()))
            
            # Döngü içi işlemler burada yürütülür
            time.sleep(LOOP_SECONDS)
        except KeyboardInterrupt:
            print("Bot kapatılıyor...")
            break
        except Exception as e:
            print(f"Döngü hatası: {e}")
            time.sleep(15)

if __name__ == "__main__":
    main()
