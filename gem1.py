#!/usr/bin/env python3
"""
Peak Reversal Futures Bot - TEK DOSYA VERSİYONU (gem1.py)
Sadece XAGUSDT taranır.
Contabo / Termius için optimize edilmiştir.
"""

import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Literal

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd
from telegram import Bot
from telegram.constants import ParseMode

# ====================== AYARLAR ======================
TELEGRAM_BOT_TOKEN = "8680932537:AAHcV1npqk0H0MunNdvfchlurdEOfEaCgw4"
TELEGRAM_CHAT_ID = "1734551753"

LEVERAGE = 10
NOTIONAL_USD = 100.0
MARGIN_USD = 200.0
VIRTUAL_BALANCE = 1000.0
SCAN_INTERVAL_SECONDS = 60
REPORT_INTERVAL_MINUTES = 60
TIMEFRAME = "1m"
LOG_LEVEL = "INFO"

# İndikatör ayarları
EMA_FAST, EMA_MID, EMA_SLOW = 5, 20, 99
RSI_FAST, RSI_SLOW = 6, 14
ATR_PERIOD = 14
WILLIAMS_PERIOD = 14
SIGNAL_LOOKBACK = 1
# =====================================================

# Logging
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("PRFB")


# -------------------- Paper Position --------------------
@dataclass
class Position:
    symbol: str
    side: str
    entry_price: float
    quantity: float
    leverage: int
    notional: float
    margin: float
    tp: float
    sl: float
    atr: float
    open_time: datetime = field(default_factory=datetime.utcnow)
    status: str = "OPEN"
    close_price: Optional[float] = None
    pnl: float = 0.0


class PaperTrader:
    def __init__(self):
        self.positions: List[Position] = []
        self.closed_positions: List[Position] = []
        self.total_pnl: float = 0.0
        self.trade_count: int = 0
        self.balance: float = VIRTUAL_BALANCE  # Sanal 1000 USDT

    def open_position(self, symbol: str, side: str, entry_price: float, atr: float) -> Optional[Position]:
        if atr <= 0 or entry_price <= 0:
            return None
        if any(p.symbol == symbol and p.status == "OPEN" for p in self.positions):
            return None
        if self.balance < MARGIN_USD:
            logger.warning(f"Yetersiz sanal bakiye: {self.balance:.2f} USDT")
            return None

        quantity = NOTIONAL_USD / entry_price
        if side == "LONG":
            tp = entry_price + (2 * atr)
            sl = entry_price - (1 * atr)
        else:
            tp = entry_price - (2 * atr)
            sl = entry_price + (1 * atr)

        pos = Position(
            symbol=symbol, side=side, entry_price=entry_price,
            quantity=quantity, leverage=LEVERAGE, notional=NOTIONAL_USD,
            margin=MARGIN_USD, tp=tp, sl=sl, atr=atr
        )
        self.positions.append(pos)
        self.trade_count += 1
        self.balance -= MARGIN_USD  # Marjı bakiyeden düş
        logger.info(f"SANAL AÇILDI | {side} {symbol} | 10x | 100$ | Marj:200$ | Bakiye:{self.balance:.2f}")
        return pos

    def update_positions(self, prices: Dict[str, float]) -> List[Position]:
        closed_now = []
        still_open = []
        for pos in self.positions:
            if pos.status != "OPEN":
                continue
            price = prices.get(pos.symbol)
            if price is None:
                still_open.append(pos)
                continue

            hit = False
            if pos.side == "LONG":
                if price >= pos.tp:
                    pos.status, pos.close_price, pos.pnl = "CLOSED_TP", pos.tp, (pos.tp - pos.entry_price) * pos.quantity
                    hit = True
                elif price <= pos.sl:
                    pos.status, pos.close_price, pos.pnl = "CLOSED_SL", pos.sl, (pos.sl - pos.entry_price) * pos.quantity
                    hit = True
            else:
                if price <= pos.tp:
                    pos.status, pos.close_price, pos.pnl = "CLOSED_TP", pos.tp, (pos.entry_price - pos.tp) * pos.quantity
                    hit = True
                elif price >= pos.sl:
                    pos.status, pos.close_price, pos.pnl = "CLOSED_SL", pos.sl, (pos.entry_price - pos.sl) * pos.quantity
                    hit = True

            if hit:
                self.total_pnl += pos.pnl
                self.balance += MARGIN_USD + pos.pnl  # Marjı + kar/zararı bakiyeye iade
                self.closed_positions.append(pos)
                closed_now.append(pos)
                logger.info(f"SANAL KAPANDI | {pos.side} {pos.symbol} | PnL:{pos.pnl:+.4f} | Bakiye:{self.balance:.2f} | {pos.status}")
            else:
                still_open.append(pos)

        self.positions = still_open
        return closed_now

    def get_open_positions(self) -> List[Position]:
        return [p for p in self.positions if p.status == "OPEN"]

    def get_stats(self) -> Dict:
        return {
            "open_positions": len(self.get_open_positions()),
            "total_trades": self.trade_count,
            "total_pnl": round(self.total_pnl, 4),
            "balance": round(self.balance, 2),
        }


# -------------------- Indicators & Signals --------------------
def ohlcv_to_df(ohlcv: list) -> pd.DataFrame:
    df = pd.DataFrame(ohlcv, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")
    return df


def calculate_indicators(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    if df is None or len(df) < 120:
        return None
    try:
        df["ema5"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
        df["ema20"] = df["close"].ewm(span=EMA_MID, adjust=False).mean()
        df["ema99"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)

        avg_gain6 = gain.ewm(alpha=1/RSI_FAST, min_periods=RSI_FAST, adjust=False).mean()
        avg_loss6 = loss.ewm(alpha=1/RSI_FAST, min_periods=RSI_FAST, adjust=False).mean()
        df["rsi6"] = 100 - (100 / (1 + avg_gain6 / avg_loss6.replace(0, np.nan)))

        avg_gain14 = gain.ewm(alpha=1/RSI_SLOW, min_periods=RSI_SLOW, adjust=False).mean()
        avg_loss14 = loss.ewm(alpha=1/RSI_SLOW, min_periods=RSI_SLOW, adjust=False).mean()
        df["rsi14"] = 100 - (100 / (1 + avg_gain14 / avg_loss14.replace(0, np.nan)))

        ema12 = df["close"].ewm(span=12, adjust=False).mean()
        ema26 = df["close"].ewm(span=26, adjust=False).mean()
        df["macd_dif"] = ema12 - ema26
        df["macd_dea"] = df["macd_dif"].ewm(span=9, adjust=False).mean()
        df["macd_hist"] = df["macd_dif"] - df["macd_dea"]

        low_min = df["low"].rolling(9).min()
        high_max = df["high"].rolling(9).max()
        rsv = (df["close"] - low_min) / (high_max - low_min).replace(0, np.nan) * 100
        df["kdj_k"] = rsv.ewm(com=2, adjust=False).mean()
        df["kdj_d"] = df["kdj_k"].ewm(com=2, adjust=False).mean()
        df["kdj_j"] = 3 * df["kdj_k"] - 2 * df["kdj_d"]

        rsi = df["rsi14"]
        stochrsi = (rsi - rsi.rolling(14).min()) / (rsi.rolling(14).max() - rsi.rolling(14).min()).replace(0, np.nan)
        df["stochrsi_k"] = stochrsi.rolling(3).mean() * 100
        df["stochrsi_d"] = df["stochrsi_k"].rolling(3).mean()

        highest = df["high"].rolling(WILLIAMS_PERIOD).max()
        lowest = df["low"].rolling(WILLIAMS_PERIOD).min()
        df["williams_r"] = -100 * (highest - df["close"]) / (highest - lowest).replace(0, np.nan)

        tr = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"] - df["close"].shift()).abs()
        ], axis=1).max(axis=1)
        df["atr"] = tr.ewm(alpha=1/ATR_PERIOD, min_periods=ATR_PERIOD, adjust=False).mean()

        return df
    except Exception as e:
        logger.error(f"İndikatör hatası: {e}")
        return None


def check_long(df: pd.DataFrame, idx: int) -> bool:
    if idx < 2:
        return False
    curr, prev = df.iloc[idx], df.iloc[idx-1]
    cond1 = curr["close"] < curr["open"] and curr["close"] < curr["ema5"]
    kdj = (70 <= prev["kdj_j"] <= 110 or 70 <= prev["kdj_k"] <= 110) and \
          curr["kdj_j"] < prev["kdj_j"] and curr["kdj_k"] < prev["kdj_k"] and curr["kdj_d"] < prev["kdj_d"]
    stoch = prev["stochrsi_k"] >= 80 and curr["stochrsi_k"] < curr["stochrsi_d"] and prev["stochrsi_k"] >= prev["stochrsi_d"]
    macd = curr["macd_dif"] < prev["macd_dif"] and curr["macd_dea"] < prev["macd_dea"] and prev["macd_hist"] > 0
    rsi = prev["rsi6"] > prev["rsi14"] and curr["rsi6"] < curr["rsi14"] and 75 <= prev["rsi14"] <= 90
    will = -16 <= prev["williams_r"] <= 0 and curr["williams_r"] < prev["williams_r"]
    return all([cond1, kdj, stoch, macd, rsi, will])


def check_short(df: pd.DataFrame, idx: int) -> bool:
    if idx < 2:
        return False
    curr, prev = df.iloc[idx], df.iloc[idx-1]
    cond1 = curr["close"] > curr["open"] and curr["close"] > curr["ema5"]
    kdj = (0 <= prev["kdj_j"] <= 30 or 0 <= prev["kdj_k"] <= 30) and \
          curr["kdj_j"] > prev["kdj_j"] and curr["kdj_k"] > prev["kdj_k"] and curr["kdj_d"] > prev["kdj_d"]
    stoch = prev["stochrsi_k"] <= 20 and curr["stochrsi_k"] > curr["stochrsi_d"] and prev["stochrsi_k"] <= prev["stochrsi_d"]
    macd = curr["macd_dif"] > prev["macd_dif"] and curr["macd_dea"] > prev["macd_dea"] and prev["macd_hist"] < 0
    rsi = prev["rsi6"] < prev["rsi14"] and curr["rsi6"] > curr["rsi14"] and 10 <= prev["rsi14"] <= 25
    will = -100 <= prev["williams_r"] <= -84 and curr["williams_r"] > prev["williams_r"]
    return all([cond1, kdj, stoch, macd, rsi, will])


def detect_signal(df: pd.DataFrame) -> Optional[str]:
    if df is None or len(df) < 30:
        return None
    for idx in range(len(df)-1, max(len(df)-1-SIGNAL_LOOKBACK-1, 1), -1):
        if check_long(df, idx):
            return "LONG"
        if check_short(df, idx):
            return "SHORT"
    return None


# -------------------- Telegram --------------------
class TelegramNotifier:
    def __init__(self):
        self.enabled = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and "BURAYA" not in TELEGRAM_BOT_TOKEN)
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN) if self.enabled else None
        if self.enabled:
            logger.info("Telegram aktif")
        else:
            logger.warning("Telegram token/chat_id eksik!")

    async def send(self, text: str):
        if not self.enabled:
            return
        try:
            await self.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=text, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Telegram hata: {e}")

    async def send_startup(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.send(f"✅ <b>Peak Reversal Futures Bot aktif</b>\nSadece <b>XAGUSDT</b> | Sanal Bakiye: 1000 USDT\n10x İzole | 100$ İşlem | 200$ Marj\n<code>{now}</code>")

    async def send_new_position(self, pos: Position):
        emoji = "🟢" if pos.side == "LONG" else "🔴"
        await self.send(
            f"{emoji} <b>Yeni Sanal İşlem</b>\n\n"
            f"Sembol: <code>{pos.symbol}</code>\nYön: <b>{pos.side}</b>\n"
            f"Giriş: <code>{pos.entry_price:.6f}</code>\nTP: <code>{pos.tp:.6f}</code>\nSL: <code>{pos.sl:.6f}</code>\n"
            f"ATR: <code>{pos.atr:.6f}</code>\n"
            f"İşlem: 100 USDT | Marj: 200 USDT | Kaldıraç: 10x (İzole)"
        )

    async def send_closed(self, pos: Position):
        emoji = "✅" if pos.pnl >= 0 else "❌"
        await self.send(
            f"{emoji} <b>Pozisyon Kapandı</b>\n\n"
            f"Sembol: <code>{pos.symbol}</code> | {pos.side}\n"
            f"Giriş → Çıkış: <code>{pos.entry_price:.6f}</code> → <code>{pos.close_price:.6f}</code>\n"
            f"PnL: <b>{pos.pnl:+.4f} USDT</b> | {pos.status}"
        )

    async def send_hourly(self, scanned: int, new_trades: int, open_pos: List[Position], total_pnl: float, total_trades: int, balance: float = 1000.0):
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"📊 <b>Saatlik Rapor</b> – {now}",
            f"Sanal Bakiye: <b>{balance:.2f} USDT</b>",
            f"Toplam Tarama: <b>{scanned}</b> çift",
            f"Bu saatte açılan: <b>{new_trades}</b>",
            f"Aktif Pozisyon: <b>{len(open_pos)}</b>",
            f"Toplam İşlem: <b>{total_trades}</b>",
            f"Günlük P&L (Sanal): <b>{total_pnl:+.4f} USDT</b>",
            ""
        ]
        if open_pos:
            lines.append("<b>Açık İşlemler:</b>")
            for i, p in enumerate(open_pos[:12], 1):
                lines.append(f"{i}. <code>{p.symbol}</code> | {p.side} | {p.entry_price:.5f}")
        else:
            lines.append("Açık işlem yok.")
        await self.send("\n".join(lines))


# -------------------- Ana Bot --------------------
class PRFBBot:
    def __init__(self):
        self.exchange = ccxt.binanceusdm({
            "enableRateLimit": True,
            "options": {"defaultType": "future", "adjustForTimeDifference": True}
        })
        self.trader = PaperTrader()
        self.notifier = TelegramNotifier()
        self.symbols: List[str] = []
        self.running = False
        self.last_scan_count = 0
        self.hourly_new_trades = 0
        self.sem = asyncio.Semaphore(5)

    async def load_markets(self):
        # Sadece XAGUSDT taranacak
        await self.exchange.load_markets()
        candidates = ["XAG/USDT:USDT", "XAGUSDT", "XAG/USDT"]
        self.symbols = []
        for sym in candidates:
            if sym in self.exchange.markets:
                self.symbols = [sym]
                break
        if not self.symbols:
            self.symbols = ["XAG/USDT:USDT"]
        logger.info(f"Yüklenen sembol: {self.symbols}")

    async def scan_symbol(self, symbol: str):
        async with self.sem:
            try:
                ohlcv = await self.exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=150)
                if not ohlcv:
                    return symbol, None, 0.0, 0.0
                df = calculate_indicators(ohlcv_to_df(ohlcv))
                if df is None:
                    return symbol, None, 0.0, 0.0
                signal = detect_signal(df)
                entry = float(df["close"].iloc[-1])
                atr = float(df["atr"].iloc[-1]) if pd.notna(df["atr"].iloc[-1]) else 0.0
                return symbol, signal, entry, atr
            except Exception:
                return symbol, None, 0.0, 0.0

    async def run_scan(self):
        if not self.symbols:
            return
        logger.info(f"Tarama başlıyor... {len(self.symbols)} çift")
        tasks = [self.scan_symbol(s) for s in self.symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        signals = 0
        prices = {}
        for res in results:
            if isinstance(res, Exception):
                continue
            symbol, signal, entry, atr = res
            if entry > 0:
                prices[symbol] = entry
            if signal and atr > 0:
                pos = self.trader.open_position(symbol, signal, entry, atr)
                if pos:
                    signals += 1
                    self.hourly_new_trades += 1
                    await self.notifier.send_new_position(pos)

        closed = self.trader.update_positions(prices)
        for pos in closed:
            await self.notifier.send_closed(pos)

        self.last_scan_count = len(self.symbols)
        logger.info(f"Tarama bitti | Sinyal: {signals} | Açık: {len(self.trader.get_open_positions())}")

    async def scan_loop(self):
        while self.running:
            try:
                await self.run_scan()
            except Exception as e:
                logger.error(f"Tarama hatası: {e}")
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    async def report_loop(self):
        await asyncio.sleep(REPORT_INTERVAL_MINUTES * 60)
        while self.running:
            try:
                new = self.hourly_new_trades
                self.hourly_new_trades = 0
                stats = self.trader.get_stats()
                await self.notifier.send_hourly(
                    self.last_scan_count, new,
                    self.trader.get_open_positions(),
                    stats["total_pnl"], stats["total_trades"],
                    stats.get("balance", 1000.0)
                )
            except Exception as e:
                logger.error(f"Rapor hatası: {e}")
            await asyncio.sleep(REPORT_INTERVAL_MINUTES * 60)

    async def start(self):
        logger.info("=" * 50)
        logger.info("Peak Reversal Futures Bot başlatılıyor...")
        await self.load_markets()
        await self.notifier.send_startup()
        self.running = True
        await asyncio.gather(self.scan_loop(), self.report_loop())

    async def stop(self):
        self.running = False
        await self.exchange.close()
        logger.info("Bot kapatıldı.")


async def main():
    bot = PRFBBot()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.stop()))
        except NotImplementedError:
            pass
    try:
        await bot.start()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot durduruldu.")
