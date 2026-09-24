#!/usr/bin/env python3
"""
Peak Reversal Futures Bot - TEK DOSYA VERSİYONU (gem1.py)
Sadece XAGUSDT taranır.
Contabo / Termius için optimize edilmiştir.

Değişiklikler (2026-09-25):
- Sinyal koşulları ±2 mum penceresinde (lookback=4) yeterli kabul edilir.
- Peak (aşırı alım) → SHORT, Trough (aşırı satım) → LONG mantığı düzeltildi.
- Her 5 dakikada bir tüm indikatörleri içeren grafik Telegram'a gönderilir.
- Startup mesajında dosya adı yer alır.
- TIMEFRAME 15m yapıldı (grafik ve sinyaller 15 dakikalık mum).
- MACD: LONG için DIF>DEA kesişimi histogramın en yüksek noktasının ÜSTÜNDE,
  SHORT için DIF<DEA kesişimi kırmızı (negatif) histogram çubuklarının en tepesinde olmalı.
- EMA: SHORT'ta fiyat EMA5'i EN TEPEDE aşağı kesmeli,
  LONG'ta fiyat EMA5'i EN DİPTE yukarı kesmeli.
"""

import asyncio
import logging
import os
import signal
import sys
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Literal

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from telegram import Bot, InputFile
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
CHART_INTERVAL_SECONDS = 300          # Her 5 dakikada indikatör grafiği (15m data)
TIMEFRAME = "15m"                     # 15 dakikalık mum
LOG_LEVEL = "INFO"

# İndikatör ayarları
EMA_FAST, EMA_MID, EMA_SLOW = 5, 20, 99
RSI_FAST, RSI_SLOW = 6, 14
ATR_PERIOD = 14
WILLIAMS_PERIOD = 14
SIGNAL_LOOKBACK = 4                   # ±2 mum penceresi
MACD_HIST_LOOKBACK = 20               # MACD hist ekstrem için bakılacak mum sayısı
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
        self.balance: float = VIRTUAL_BALANCE

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
        self.balance -= MARGIN_USD
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
                self.balance += MARGIN_USD + pos.pnl
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
        df = df.copy()
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


def check_peak_reversal(df: pd.DataFrame, idx: int) -> bool:
    """Aşırı alım (peak) dönüşü → SHORT sinyali
    - EMA: Fiyat EMA5'i en tepede (son yükseklerin zirvesinde) aşağı kesmeli
    - MACD: DIF, DEA'yı aşağı kesmeli ve bu kesişim kırmızı histogram çubuklarının en tepesinde olmalı
    """
    if idx < 2:
        return False
    curr, prev = df.iloc[idx], df.iloc[idx - 1]

    # EMA SHORT: kırmızı mum + close EMA5 altında + kesişim son high'ların en tepesinde
    ema_cross_down = prev["close"] >= prev["ema5"] and curr["close"] < curr["ema5"]
    start_p = max(0, idx - 20)
    recent_high = df["high"].iloc[start_p:idx + 1].max()
    at_peak = curr["high"] >= recent_high * 0.998   # en tepeye çok yakın
    cond1 = curr["close"] < curr["open"] and curr["close"] < curr["ema5"] and (ema_cross_down or at_peak)

    kdj = (70 <= prev["kdj_j"] <= 110 or 70 <= prev["kdj_k"] <= 110) and \
          curr["kdj_j"] < prev["kdj_j"] and curr["kdj_k"] < prev["kdj_k"] and curr["kdj_d"] < prev["kdj_d"]
    stoch = prev["stochrsi_k"] >= 80 and curr["stochrsi_k"] < curr["stochrsi_d"] and prev["stochrsi_k"] >= prev["stochrsi_d"]
    rsi = prev["rsi6"] > prev["rsi14"] and curr["rsi6"] < curr["rsi14"] and 75 <= prev["rsi14"] <= 90
    will = -16 <= prev["williams_r"] <= 0 and curr["williams_r"] < prev["williams_r"]

    # MACD SHORT: DIF DEA'yı aşağı kesiyor + kesişim kırmızı hist en tepesinde
    cross_down = prev["macd_dif"] >= prev["macd_dea"] and curr["macd_dif"] < curr["macd_dea"]
    start_h = max(0, idx - MACD_HIST_LOOKBACK)
    recent_hist = df["macd_hist"].iloc[start_h:idx + 1]
    if len(recent_hist) < 3:
        return False
    neg_hist = recent_hist[recent_hist < 0]
    if len(neg_hist) == 0:
        hist_at_peak = False
    else:
        hist_peak = neg_hist.max()
        hist_at_peak = curr["macd_hist"] <= 0 and abs(curr["macd_hist"] - hist_peak) < abs(hist_peak) * 0.35 + 1e-8

    macd = cross_down and hist_at_peak and prev["macd_hist"] > 0

    return all([cond1, kdj, stoch, macd, rsi, will])


def check_trough_reversal(df: pd.DataFrame, idx: int) -> bool:
    """Aşırı satım (trough) dönüşü → LONG sinyali
    - EMA: Fiyat EMA5'i en dipte (son low'ların dibinde) yukarı kesmeli
    - MACD: DIF, DEA'yı yukarı kesmeli ve bu kesişim histogramın en yüksek noktasının ÜSTÜNDE olmalı
    """
    if idx < 2:
        return False
    curr, prev = df.iloc[idx], df.iloc[idx - 1]

    # EMA LONG: yeşil mum + close EMA5 üstünde + kesişim son low'ların en dibinde
    ema_cross_up = prev["close"] <= prev["ema5"] and curr["close"] > curr["ema5"]
    start_p = max(0, idx - 20)
    recent_low = df["low"].iloc[start_p:idx + 1].min()
    at_bottom = curr["low"] <= recent_low * 1.002   # en dibe çok yakın
    cond1 = curr["close"] > curr["open"] and curr["close"] > curr["ema5"] and (ema_cross_up or at_bottom)

    kdj = (0 <= prev["kdj_j"] <= 30 or 0 <= prev["kdj_k"] <= 30) and \
          curr["kdj_j"] > prev["kdj_j"] and curr["kdj_k"] > prev["kdj_k"] and curr["kdj_d"] > prev["kdj_d"]
    stoch = prev["stochrsi_k"] <= 20 and curr["stochrsi_k"] > curr["stochrsi_d"] and prev["stochrsi_k"] <= prev["stochrsi_d"]
    rsi = prev["rsi6"] < prev["rsi14"] and curr["rsi6"] > curr["rsi14"] and 10 <= prev["rsi14"] <= 25
    will = -100 <= prev["williams_r"] <= -84 and curr["williams_r"] > prev["williams_r"]

    # MACD LONG: DIF DEA'yı yukarı kesiyor + kesişim en yüksek hist noktasının ÜSTÜNDE
    cross_up = prev["macd_dif"] <= prev["macd_dea"] and curr["macd_dif"] > curr["macd_dea"]
    start_h = max(0, idx - MACD_HIST_LOOKBACK)
    recent_hist = df["macd_hist"].iloc[start_h:idx + 1]
    if len(recent_hist) < 3:
        return False
    hist_max = recent_hist.max()
    above_highest = curr["macd_dif"] > hist_max

    macd = cross_up and above_highest and prev["macd_hist"] < 0

    return all([cond1, kdj, stoch, macd, rsi, will])


def detect_signal(df: pd.DataFrame) -> Optional[str]:
    """
    Son SIGNAL_LOOKBACK+1 mum içinde (yaklaşık ±2 mum penceresi)
    full koşul seti sağlanırsa sinyal üretir.
    """
    if df is None or len(df) < 30:
        return None
    start = max(len(df) - 1 - SIGNAL_LOOKBACK, 2)
    for idx in range(len(df) - 1, start - 1, -1):
        if check_peak_reversal(df, idx):
            return "SHORT"
        if check_trough_reversal(df, idx):
            return "LONG"
    return None


def _style_axes(ax):
    ax.set_facecolor("#0e1117")
    ax.tick_params(colors="#aaa")
    ax.grid(True, alpha=0.15, color="#555")
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#333")
    ax.spines["bottom"].set_color("#333")


def create_signal_chart(df: pd.DataFrame, pos: "Position") -> Optional[bytes]:
    """İşlem açıldığında tüm indikatörleri + TP/SL içeren grafik (15m)"""
    try:
        plot_df = df.tail(80).copy().reset_index(drop=True)

        fig = plt.figure(figsize=(12, 14), facecolor="#0e1117")
        gs = fig.add_gridspec(6, 1, height_ratios=[3, 1, 1, 1, 1, 1], hspace=0.08)

        # 1. Fiyat + EMA + Entry/TP/SL
        ax1 = fig.add_subplot(gs[0])
        _style_axes(ax1)
        for i in range(len(plot_df)):
            color = "#26a69a" if plot_df["close"].iloc[i] >= plot_df["open"].iloc[i] else "#ef5350"
            ax1.plot([i, i], [plot_df["low"].iloc[i], plot_df["high"].iloc[i]], color=color, linewidth=0.8)
            ax1.plot([i, i], [plot_df["open"].iloc[i], plot_df["close"].iloc[i]], color=color, linewidth=2.2)

        ax1.plot(plot_df["ema5"], color="#f0b90b", linewidth=1.2, label="EMA5")
        ax1.plot(plot_df["ema20"], color="#e040fb", linewidth=1.2, label="EMA20")
        ax1.plot(plot_df["ema99"], color="#7c4dff", linewidth=1.2, label="EMA99")
        ax1.axhline(pos.entry_price, color="#2196f3", linestyle="--", linewidth=1.3, label=f"Giriş {pos.entry_price:.4f}")
        ax1.axhline(pos.tp, color="#00e676", linestyle="-", linewidth=1.5, label=f"TP {pos.tp:.4f}")
        ax1.axhline(pos.sl, color="#ff1744", linestyle="-", linewidth=1.5, label=f"SL {pos.sl:.4f}")
        ax1.set_title(f"{pos.symbol}  |  {pos.side}  |  10x İzole  |  100$ İşlem  |  15m", color="white", fontsize=13, pad=8)
        ax1.legend(loc="upper left", fontsize=8, facecolor="#1e222d", edgecolor="none", labelcolor="white")

        # 2. KDJ
        ax2 = fig.add_subplot(gs[1], sharex=ax1)
        _style_axes(ax2)
        ax2.plot(plot_df["kdj_k"], color="#f0b90b", linewidth=1, label="K")
        ax2.plot(plot_df["kdj_d"], color="#e040fb", linewidth=1, label="D")
        ax2.plot(plot_df["kdj_j"], color="#26a69a", linewidth=1, label="J")
        ax2.axhline(80, color="#555", linestyle="--", linewidth=0.7)
        ax2.axhline(20, color="#555", linestyle="--", linewidth=0.7)
        ax2.set_ylabel("KDJ", color="#aaa", fontsize=9)
        ax2.legend(loc="upper left", fontsize=7, facecolor="#1e222d", edgecolor="none", labelcolor="white")

        # 3. StochRSI
        ax3 = fig.add_subplot(gs[2], sharex=ax1)
        _style_axes(ax3)
        ax3.plot(plot_df["stochrsi_k"], color="#f0b90b", linewidth=1, label="StochRSI")
        ax3.plot(plot_df["stochrsi_d"], color="#e040fb", linewidth=1, label="MA")
        ax3.axhline(80, color="#555", linestyle="--", linewidth=0.7)
        ax3.axhline(20, color="#555", linestyle="--", linewidth=0.7)
        ax3.set_ylabel("StochRSI", color="#aaa", fontsize=9)
        ax3.legend(loc="upper left", fontsize=7, facecolor="#1e222d", edgecolor="none", labelcolor="white")

        # 4. MACD
        ax4 = fig.add_subplot(gs[3], sharex=ax1)
        _style_axes(ax4)
        ax4.plot(plot_df["macd_dif"], color="#26a69a", linewidth=1, label="DIF")
        ax4.plot(plot_df["macd_dea"], color="#ef5350", linewidth=1, label="DEA")
        colors = ["#26a69a" if v >= 0 else "#ef5350" for v in plot_df["macd_hist"]]
        ax4.bar(range(len(plot_df)), plot_df["macd_hist"], color=colors, width=0.7, alpha=0.7)
        ax4.axhline(0, color="#555", linewidth=0.7)
        ax4.set_ylabel("MACD", color="#aaa", fontsize=9)
        ax4.legend(loc="upper left", fontsize=7, facecolor="#1e222d", edgecolor="none", labelcolor="white")

        # 5. RSI
        ax5 = fig.add_subplot(gs[4], sharex=ax1)
        _style_axes(ax5)
        ax5.plot(plot_df["rsi6"], color="#f0b90b", linewidth=1, label="RSI6")
        ax5.plot(plot_df["rsi14"], color="#e040fb", linewidth=1, label="RSI14")
        ax5.axhline(70, color="#555", linestyle="--", linewidth=0.7)
        ax5.axhline(30, color="#555", linestyle="--", linewidth=0.7)
        ax5.set_ylabel("RSI", color="#aaa", fontsize=9)
        ax5.legend(loc="upper left", fontsize=7, facecolor="#1e222d", edgecolor="none", labelcolor="white")

        # 6. Williams %R
        ax6 = fig.add_subplot(gs[5], sharex=ax1)
        _style_axes(ax6)
        ax6.plot(plot_df["williams_r"], color="#f0b90b", linewidth=1, label="Williams %R")
        ax6.axhline(-20, color="#555", linestyle="--", linewidth=0.7)
        ax6.axhline(-80, color="#555", linestyle="--", linewidth=0.7)
        ax6.set_ylabel("Wm %R", color="#aaa", fontsize=9)
        ax6.legend(loc="upper left", fontsize=7, facecolor="#1e222d", edgecolor="none", labelcolor="white")

        for ax in [ax1, ax2, ax3, ax4, ax5]:
            plt.setp(ax.get_xticklabels(), visible=False)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        logger.error(f"Sinyal grafik hatası: {e}")
        return None


def create_indicator_chart(df: pd.DataFrame, symbol: str) -> Optional[bytes]:
    """Her 5 dakikada gönderilecek, 15m tüm indikatörleri içeren genel grafik"""
    try:
        plot_df = df.tail(80).copy().reset_index(drop=True)
        last_close = float(plot_df["close"].iloc[-1])
        last_atr = float(plot_df["atr"].iloc[-1]) if pd.notna(plot_df["atr"].iloc[-1]) else 0.0

        fig = plt.figure(figsize=(12, 14), facecolor="#0e1117")
        gs = fig.add_gridspec(6, 1, height_ratios=[3, 1, 1, 1, 1, 1], hspace=0.08)

        # 1. Fiyat + EMA
        ax1 = fig.add_subplot(gs[0])
        _style_axes(ax1)
        for i in range(len(plot_df)):
            color = "#26a69a" if plot_df["close"].iloc[i] >= plot_df["open"].iloc[i] else "#ef5350"
            ax1.plot([i, i], [plot_df["low"].iloc[i], plot_df["high"].iloc[i]], color=color, linewidth=0.8)
            ax1.plot([i, i], [plot_df["open"].iloc[i], plot_df["close"].iloc[i]], color=color, linewidth=2.2)

        ax1.plot(plot_df["ema5"], color="#f0b90b", linewidth=1.2, label="EMA5")
        ax1.plot(plot_df["ema20"], color="#e040fb", linewidth=1.2, label="EMA20")
        ax1.plot(plot_df["ema99"], color="#7c4dff", linewidth=1.2, label="EMA99")
        ax1.set_title(
            f"{symbol}  |  Fiyat: {last_close:.4f}  |  ATR: {last_atr:.4f}  |  15dk İndikatör",
            color="white", fontsize=13, pad=8
        )
        ax1.legend(loc="upper left", fontsize=8, facecolor="#1e222d", edgecolor="none", labelcolor="white")

        # 2. KDJ
        ax2 = fig.add_subplot(gs[1], sharex=ax1)
        _style_axes(ax2)
        ax2.plot(plot_df["kdj_k"], color="#f0b90b", linewidth=1, label="K")
        ax2.plot(plot_df["kdj_d"], color="#e040fb", linewidth=1, label="D")
        ax2.plot(plot_df["kdj_j"], color="#26a69a", linewidth=1, label="J")
        ax2.axhline(80, color="#555", linestyle="--", linewidth=0.7)
        ax2.axhline(20, color="#555", linestyle="--", linewidth=0.7)
        ax2.set_ylabel("KDJ", color="#aaa", fontsize=9)
        ax2.legend(loc="upper left", fontsize=7, facecolor="#1e222d", edgecolor="none", labelcolor="white")

        # 3. StochRSI
        ax3 = fig.add_subplot(gs[2], sharex=ax1)
        _style_axes(ax3)
        ax3.plot(plot_df["stochrsi_k"], color="#f0b90b", linewidth=1, label="StochRSI")
        ax3.plot(plot_df["stochrsi_d"], color="#e040fb", linewidth=1, label="MA")
        ax3.axhline(80, color="#555", linestyle="--", linewidth=0.7)
        ax3.axhline(20, color="#555", linestyle="--", linewidth=0.7)
        ax3.set_ylabel("StochRSI", color="#aaa", fontsize=9)
        ax3.legend(loc="upper left", fontsize=7, facecolor="#1e222d", edgecolor="none", labelcolor="white")

        # 4. MACD
        ax4 = fig.add_subplot(gs[3], sharex=ax1)
        _style_axes(ax4)
        ax4.plot(plot_df["macd_dif"], color="#26a69a", linewidth=1, label="DIF")
        ax4.plot(plot_df["macd_dea"], color="#ef5350", linewidth=1, label="DEA")
        colors = ["#26a69a" if v >= 0 else "#ef5350" for v in plot_df["macd_hist"]]
        ax4.bar(range(len(plot_df)), plot_df["macd_hist"], color=colors, width=0.7, alpha=0.7)
        ax4.axhline(0, color="#555", linewidth=0.7)
        ax4.set_ylabel("MACD", color="#aaa", fontsize=9)
        ax4.legend(loc="upper left", fontsize=7, facecolor="#1e222d", edgecolor="none", labelcolor="white")

        # 5. RSI
        ax5 = fig.add_subplot(gs[4], sharex=ax1)
        _style_axes(ax5)
        ax5.plot(plot_df["rsi6"], color="#f0b90b", linewidth=1, label="RSI6")
        ax5.plot(plot_df["rsi14"], color="#e040fb", linewidth=1, label="RSI14")
        ax5.axhline(70, color="#555", linestyle="--", linewidth=0.7)
        ax5.axhline(30, color="#555", linestyle="--", linewidth=0.7)
        ax5.set_ylabel("RSI", color="#aaa", fontsize=9)
        ax5.legend(loc="upper left", fontsize=7, facecolor="#1e222d", edgecolor="none", labelcolor="white")

        # 6. Williams %R
        ax6 = fig.add_subplot(gs[5], sharex=ax1)
        _style_axes(ax6)
        ax6.plot(plot_df["williams_r"], color="#f0b90b", linewidth=1, label="Williams %R")
        ax6.axhline(-20, color="#555", linestyle="--", linewidth=0.7)
        ax6.axhline(-80, color="#555", linestyle="--", linewidth=0.7)
        ax6.set_ylabel("Wm %R", color="#aaa", fontsize=9)
        ax6.legend(loc="upper left", fontsize=7, facecolor="#1e222d", edgecolor="none", labelcolor="white")

        for ax in [ax1, ax2, ax3, ax4, ax5]:
            plt.setp(ax.get_xticklabels(), visible=False)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        logger.error(f"İndikatör grafik hatası: {e}")
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
        await self.send(
            f"✅ <b>Peak Reversal Futures Bot aktif</b>\n"
            f"📁 Dosya: <code>gem1.py</code>\n"
            f"Sadece <b>XAGUSDT</b> | Sanal Bakiye: 1000 USDT\n"
            f"10x İzole | 100$ İşlem | 200$ Marj\n"
            f"Timeframe: <b>15m</b> | Sinyal penceresi: ±2 mum | 5dk grafik aktif\n"
            f"<code>{now}</code>"
        )

    async def send_new_position(self, pos: Position, chart_bytes: Optional[bytes] = None):
        emoji = "🟢" if pos.side == "LONG" else "🔴"
        caption = (
            f"{emoji} <b>Yeni Sanal İşlem</b>\n\n"
            f"Sembol: <code>{pos.symbol}</code>\nYön: <b>{pos.side}</b>\n"
            f"Giriş: <code>{pos.entry_price:.6f}</code>\nTP: <code>{pos.tp:.6f}</code>\nSL: <code>{pos.sl:.6f}</code>\n"
            f"ATR: <code>{pos.atr:.6f}</code>\n"
            f"İşlem: 100 USDT | Marj: 200 USDT | Kaldıraç: 10x (İzole) | 15m"
        )
        if not self.enabled:
            return
        try:
            if chart_bytes:
                await self.bot.send_photo(
                    chat_id=TELEGRAM_CHAT_ID,
                    photo=InputFile(io.BytesIO(chart_bytes), filename="signal.png"),
                    caption=caption,
                    parse_mode=ParseMode.HTML
                )
            else:
                await self.send(caption)
        except Exception as e:
            logger.error(f"Telegram grafik gönderme hatası: {e}")
            await self.send(caption)

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

    async def send_indicator_chart(self, chart_bytes: bytes, symbol: str, price: float):
        if not self.enabled or not chart_bytes:
            return
        caption = (
            f"📈 <b>15 Dakikalık İndikatör Grafiği</b>\n"
            f"Sembol: <code>{symbol}</code>\n"
            f"Fiyat: <code>{price:.4f}</code>\n"
            f"EMA5 / EMA20 / EMA99 | KDJ | StochRSI | MACD | RSI | Williams %R"
        )
        try:
            await self.bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=InputFile(io.BytesIO(chart_bytes), filename="indicators.png"),
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"15dk grafik gönderme hatası: {e}")


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
                    return symbol, None, 0.0, 0.0, None
                df = calculate_indicators(ohlcv_to_df(ohlcv))
                if df is None:
                    return symbol, None, 0.0, 0.0, None
                signal = detect_signal(df)
                entry = float(df["close"].iloc[-1])
                atr = float(df["atr"].iloc[-1]) if pd.notna(df["atr"].iloc[-1]) else 0.0
                return symbol, signal, entry, atr, df
            except Exception as e:
                logger.error(f"scan_symbol hata ({symbol}): {e}")
                return symbol, None, 0.0, 0.0, None

    async def run_scan(self):
        if not self.symbols:
            return
        logger.info(f"Tarama başlıyor... {len(self.symbols)} çift (15m)")
        tasks = [self.scan_symbol(s) for s in self.symbols]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        signals = 0
        prices = {}
        for res in results:
            if isinstance(res, Exception):
                continue
            symbol, signal, entry, atr, df = res
            if entry > 0:
                prices[symbol] = entry
            if signal and atr > 0 and df is not None:
                pos = self.trader.open_position(symbol, signal, entry, atr)
                if pos:
                    signals += 1
                    self.hourly_new_trades += 1
                    chart = create_signal_chart(df, pos)
                    await self.notifier.send_new_position(pos, chart)

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

    async def chart_loop(self):
        """Her 5 dakikada bir XAGUSDT 15m indikatör grafiği gönderir"""
        await asyncio.sleep(10)
        while self.running:
            try:
                if not self.symbols:
                    await asyncio.sleep(CHART_INTERVAL_SECONDS)
                    continue
                symbol = self.symbols[0]
                _, _, entry, _, df = await self.scan_symbol(symbol)
                if df is not None and entry > 0:
                    chart = create_indicator_chart(df, symbol)
                    if chart:
                        await self.notifier.send_indicator_chart(chart, symbol, entry)
                        logger.info(f"15dk indikatör grafiği gönderildi | {symbol} @ {entry:.4f}")
            except Exception as e:
                logger.error(f"15dk grafik hatası: {e}")
            await asyncio.sleep(CHART_INTERVAL_SECONDS)

    async def start(self):
        logger.info("=" * 50)
        logger.info("Peak Reversal Futures Bot başlatılıyor... (gem1.py) | TIMEFRAME=15m")
        await self.load_markets()
        await self.notifier.send_startup()
        self.running = True
        await asyncio.gather(
            self.scan_loop(),
            self.report_loop(),
            self.chart_loop()
        )

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
