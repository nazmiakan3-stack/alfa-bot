#!/usr/bin/env python3
"""
Peak Reversal Futures Bot - TEK DOSYA (gem1.py)
20 coin (XAU + XAG dahil) | 15m | Sanal paper trading
Contabo / Termius için optimize.

LONG : EMA5 dip + yükseliş | KDJ/Stoch/RSI/Williams aşırı satım | MACD↑
SHORT: EMA5 tepe + düşüş  | KDJ/Stoch/RSI/Williams aşırı alım  | MACD↓
Çıkış: sadece TP (2×ATR) / SL (1.5×ATR)
"""

import asyncio
import logging
import signal
import sys
import io
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import ccxt.async_support as ccxt
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from telegram import Bot, InputFile
from telegram.constants import ParseMode

# ====================== AYARLAR ======================
TELEGRAM_BOT_TOKEN = "8680932537:AAHcV1npqk0H0MunNdvfchlurdEOfEaCgw4"
TELEGRAM_CHAT_ID = "1734551753"
BOT_FILENAME = "gem1.py"   # Tüm Telegram mesajlarında görünür

LEVERAGE = 10
NOTIONAL_USD = 100.0
MARGIN_USD = 50.0              # 20 coin için marj düşürüldü
VIRTUAL_BALANCE = 2000.0
SCAN_INTERVAL_SECONDS = 90
REPORT_INTERVAL_MINUTES = 60
TIMEFRAME = "15m"
LOG_LEVEL = "INFO"

# 20 coin: Altın + Gümüş + majör vadeli
WATCH_SYMBOLS = [
    "XAU/USDT:USDT",   # Altın
    "XAG/USDT:USDT",   # Gümüş
    "BTC/USDT:USDT",
    "ETH/USDT:USDT",
    "BNB/USDT:USDT",
    "SOL/USDT:USDT",
    "XRP/USDT:USDT",
    "DOGE/USDT:USDT",
    "ADA/USDT:USDT",
    "AVAX/USDT:USDT",
    "LINK/USDT:USDT",
    "DOT/USDT:USDT",
    "LTC/USDT:USDT",
    "ATOM/USDT:USDT",
    "UNI/USDT:USDT",
    "APT/USDT:USDT",
    "ARB/USDT:USDT",
    "OP/USDT:USDT",
    "SUI/USDT:USDT",
    "NEAR/USDT:USDT",
]

EMA_FAST, EMA_MID, EMA_SLOW = 5, 20, 99
RSI_FAST, RSI_SLOW = 6, 14
ATR_PERIOD = 14
WILLIAMS_PERIOD = 14
SIGNAL_LOOKBACK = 4
# =====================================================

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
        # SL = 1.5 × ATR (kullanıcı isteği), TP = 2 × ATR
        if side == "LONG":
            tp = entry_price + (2.0 * atr)
            sl = entry_price - (1.5 * atr)
        else:
            tp = entry_price - (2.0 * atr)
            sl = entry_price + (1.5 * atr)

        pos = Position(
            symbol=symbol, side=side, entry_price=entry_price,
            quantity=quantity, leverage=LEVERAGE, notional=NOTIONAL_USD,
            margin=MARGIN_USD, tp=tp, sl=sl, atr=atr
        )
        self.positions.append(pos)
        self.trade_count += 1
        self.balance -= MARGIN_USD
        logger.info(f"SANAL AÇILDI | {side} {symbol} | 10x | 100$ | Marj:{MARGIN_USD:.0f}$ | Bakiye:{self.balance:.2f}")
        return pos

    def update_positions(self, prices: Dict[str, float], dfs: Optional[Dict[str, pd.DataFrame]] = None) -> List[Position]:
        """Sadece TP / SL ile kapatma (EMA kesişim çıkışı kaldırıldı — gürültülü erken kapanma olmasın)"""
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


# -------------------- Indicators --------------------
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

        # EMA99 eğimi (son 5 mum)
        df["ema99_slope"] = df["ema99"].diff(5)

        return df
    except Exception as e:
        logger.error(f"İndikatör hatası: {e}")
        return None


# -------------------- Signal Logic --------------------
# Ana tetik: EMA5 tepe/dip
# Diğer indikatörler ±2 mum içinde şartı sağlarsa pozisyon açılır

def _any_in_window(df: pd.DataFrame, idx: int, check_fn, window: int = 1) -> bool:
    """±window mum içinde check_fn True ise OK (varsayılan ±1)"""
    for j in range(max(0, idx - window), min(len(df), idx + window + 1)):
        if check_fn(df.iloc[j]):
            return True
    return False


# SATIŞ bölgesi = yüksek değerler → SHORT desteği
# ALIŞ  bölgesi = düşük değerler  → LONG desteği

def _kdj_sell_zone(row) -> bool:
    return (row["kdj_k"] >= 70) or (row["kdj_j"] >= 70)


def _kdj_buy_zone(row) -> bool:
    return (row["kdj_k"] <= 30) or (row["kdj_j"] <= 30)


def _stoch_sell_zone(row) -> bool:
    return row["stochrsi_k"] >= 65


def _stoch_buy_zone(row) -> bool:
    return row["stochrsi_k"] <= 35


def _rsi_sell_zone(row) -> bool:
    return (row["rsi14"] >= 60) or (row["rsi6"] >= 60)


def _rsi_buy_zone(row) -> bool:
    return (row["rsi14"] <= 40) or (row["rsi6"] <= 40)


def _will_sell_zone(row) -> bool:
    return -30 <= row["williams_r"] <= 0


def _will_buy_zone(row) -> bool:
    return -100 <= row["williams_r"] <= -70


def _macd_peak_pivot(df: pd.DataFrame, idx: int) -> Optional[int]:
    """
    SHORT ana tetik: DIF ve hist (yeşil hacim) en tepede.
    Tepe en fazla 1 mum önce olmalı.
    """
    if idx < 5:
        return None
    dif = df["macd_dif"]
    hist = df["macd_hist"]
    start = max(0, idx - 12)
    win_dif = dif.iloc[start: idx + 1]
    win_hist = hist.iloc[start: idx + 1]
    dif_peak = int(start + win_dif.values.argmax())
    hist_peak = int(start + win_hist.values.argmax())
    # İkisinden en az biri idx veya idx-1'de tepe
    peaks = [p for p in (dif_peak, hist_peak) if idx - p <= 1]
    if not peaks:
        return None
    pivot = max(peaks)  # en yakın tepe
    # Dönüş aşağı: hist veya DIF düşüyor
    c, p = df.iloc[idx], df.iloc[idx - 1]
    turning_down = (
        float(c["macd_hist"]) < float(p["macd_hist"])
        or float(c["macd_dif"]) < float(p["macd_dif"])
        or (p["macd_dif"] >= p["macd_dea"] and c["macd_dif"] < c["macd_dea"])
    )
    if not turning_down:
        return None
    return pivot


def _macd_bottom_pivot(df: pd.DataFrame, idx: int) -> Optional[int]:
    """
    LONG ana tetik: DIF ve hist en dipte.
    Dip en fazla 1 mum önce olmalı.
    """
    if idx < 5:
        return None
    dif = df["macd_dif"]
    hist = df["macd_hist"]
    start = max(0, idx - 12)
    win_dif = dif.iloc[start: idx + 1]
    win_hist = hist.iloc[start: idx + 1]
    dif_bot = int(start + win_dif.values.argmin())
    hist_bot = int(start + win_hist.values.argmin())
    bottoms = [b for b in (dif_bot, hist_bot) if idx - b <= 1]
    if not bottoms:
        return None
    pivot = max(bottoms)
    c, p = df.iloc[idx], df.iloc[idx - 1]
    turning_up = (
        float(c["macd_hist"]) > float(p["macd_hist"])
        or float(c["macd_dif"]) > float(p["macd_dif"])
        or (p["macd_dif"] <= p["macd_dea"] and c["macd_dif"] > c["macd_dea"])
    )
    if not turning_up:
        return None
    return pivot


def check_short(df: pd.DataFrame, idx: int) -> bool:
    """
    SHORT = SATIŞ:
    1) MACD DIF+hist EN TEPEDE (max 1 mum önce) + aşağı dönüş → ZORUNLU
    2) Destek: KDJ/Stoch/RSI/Williams SATIŞ bölgesinde (yüksek) ±1 mum
    3) Engel: göstergeler ALIŞ bölgesinde (düşük/ters) → SHORT YOK
    """
    if idx < 5:
        return False
    pivot = _macd_peak_pivot(df, idx)
    if pivot is None:
        return False

    support = sum([
        _any_in_window(df, pivot, _kdj_sell_zone, 1),
        _any_in_window(df, pivot, _stoch_sell_zone, 1),
        _any_in_window(df, pivot, _rsi_sell_zone, 1),
        _any_in_window(df, pivot, _will_sell_zone, 1),
    ])
    conflict = sum([
        _any_in_window(df, pivot, _kdj_buy_zone, 1),
        _any_in_window(df, pivot, _stoch_buy_zone, 1),
        _any_in_window(df, pivot, _rsi_buy_zone, 1),
        _any_in_window(df, pivot, _will_buy_zone, 1),
    ])
    if conflict >= 2:
        return False
    return support >= 2


def check_long(df: pd.DataFrame, idx: int) -> bool:
    """
    LONG = ALIŞ:
    1) MACD DIF+hist EN DİPTE (max 1 mum önce) + yukarı dönüş → ZORUNLU
    2) Destek: göstergeler ALIŞ bölgesinde (düşük) ±1 mum
    3) Engel: göstergeler SATIŞ bölgesinde (yüksek/ters) → LONG YOK
    """
    if idx < 5:
        return False
    pivot = _macd_bottom_pivot(df, idx)
    if pivot is None:
        return False

    support = sum([
        _any_in_window(df, pivot, _kdj_buy_zone, 1),
        _any_in_window(df, pivot, _stoch_buy_zone, 1),
        _any_in_window(df, pivot, _rsi_buy_zone, 1),
        _any_in_window(df, pivot, _will_buy_zone, 1),
    ])
    conflict = sum([
        _any_in_window(df, pivot, _kdj_sell_zone, 1),
        _any_in_window(df, pivot, _stoch_sell_zone, 1),
        _any_in_window(df, pivot, _rsi_sell_zone, 1),
        _any_in_window(df, pivot, _will_sell_zone, 1),
    ])
    if conflict >= 2:
        return False
    return support >= 2


def detect_signal(df: pd.DataFrame) -> Optional[str]:
    """Sadece son 1-2 mum — MACD tepesinden/dibinden 1 mumdan fazla uzaksa girme"""
    if df is None or len(df) < 30:
        return None
    for idx in range(len(df) - 1, max(len(df) - 3, 5) - 1, -1):
        if check_short(df, idx):
            return "SHORT"
        if check_long(df, idx):
            return "LONG"
    return None


def find_signal_indices(df: pd.DataFrame, lookback: int = 96) -> Dict[str, List[int]]:
    longs, shorts = [], []
    start = max(3, len(df) - lookback)
    for idx in range(start, len(df)):
        if check_long(df, idx):
            longs.append(idx - start)
        if check_short(df, idx):
            shorts.append(idx - start)
    return {"long": longs, "short": shorts}


def find_ema_extremes(df: pd.DataFrame, lookback: int = 96) -> Dict[str, List[int]]:
    """MACD tepe/dip (grafik için, opsiyonel)"""
    bottoms, tops = [], []
    start = max(5, len(df) - lookback)
    for idx in range(start, len(df)):
        if _macd_bottom_pivot(df, idx) is not None:
            bottoms.append(idx - start)
        if _macd_peak_pivot(df, idx) is not None:
            tops.append(idx - start)
    return {"bottoms": bottoms, "tops": tops}


# -------------------- Charts (Profesyonel stil) --------------------
BG = "#131722"
PANEL = "#1a1e2e"
GRID = "#2a2e39"
TEXT = "#d1d4dc"
MUTED = "#787b86"
UP = "#26a69a"
DOWN = "#ef5350"
EMA5_C = "#f0b90b"
EMA20_C = "#e040fb"
EMA99_C = "#2962ff"
LONG_C = "#2196f3"
SHORT_C = "#ff1744"
TP_C = "#00e676"
SL_C = "#ff6d00"
ENTRY_C = "#00bcd4"


def _style_axes(ax, ylabel: str = ""):
    ax.set_facecolor(PANEL)
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.yaxis.tick_right()
    ax.yaxis.set_label_position("right")
    ax.grid(True, alpha=0.25, color=GRID, linewidth=0.6, linestyle="-")
    for sp in ax.spines.values():
        sp.set_color("#2a2e39")
        sp.set_linewidth(0.8)
    if ylabel:
        ax.set_ylabel(ylabel, color=MUTED, fontsize=9, fontweight="bold", labelpad=6)


def _draw_candles(ax, plot_df):
    for i in range(len(plot_df)):
        o, h, l, c = plot_df["open"].iloc[i], plot_df["high"].iloc[i], plot_df["low"].iloc[i], plot_df["close"].iloc[i]
        color = UP if c >= o else DOWN
        ax.plot([i, i], [l, h], color=color, linewidth=1.0, solid_capstyle="round", zorder=2)
        body_bottom, body_top = min(o, c), max(o, c)
        height = max(body_top - body_bottom, (h - l) * 0.02 + 1e-8)
        ax.add_patch(plt.Rectangle(
            (i - 0.35, body_bottom), 0.7, height,
            facecolor=color, edgecolor=color, linewidth=0, zorder=3, alpha=0.95
        ))


def _draw_ema_lines(ax, plot_df):
    """Sadece EMA çizgileri — ekstra tepe/dip işaretleri yok"""
    ax.plot(plot_df["ema5"], color=EMA5_C, linewidth=1.6, label="EMA5", zorder=4)
    ax.plot(plot_df["ema20"], color=EMA20_C, linewidth=1.4, label="EMA20", zorder=4)
    ax.plot(plot_df["ema99"], color=EMA99_C, linewidth=1.3, label="EMA99", zorder=4, alpha=0.9)


def _draw_entry_marker(axes, plot_df, n: int, side: str, price_ax=None, entry_idx: Optional[int] = None):
    """Sadece BU işlemin giriş noktası — tek kesik çizgi (geçmiş sinyaller yok)."""
    x = entry_idx if entry_idx is not None else (n - 1)
    if x < 0 or x >= n:
        x = n - 1
    color = LONG_C if side == "LONG" else SHORT_C
    for ax in axes:
        ax.axvline(x, color=color, linestyle="--", linewidth=1.6, alpha=0.9, zorder=1)
    if price_ax is not None:
        marker = "^" if side == "LONG" else "v"
        price_ax.scatter(x, plot_df["close"].iloc[x], marker=marker, s=100, color=color,
                         edgecolors="white", linewidths=0.9, zorder=7)


def _legend(ax):
    leg = ax.legend(loc="upper left", fontsize=7.5, framealpha=0.92,
                    facecolor="#1e2330", edgecolor="#363a45", labelcolor=TEXT)
    leg.get_frame().set_linewidth(0.6)


def _draw_levels(ax, entry: float, tp: float, sl: float, side: str = ""):
    """Giriş / TP / SL yatay çizgi + sağda etiket"""
    n_right = ax.get_xlim()[1] if ax.get_xlim()[1] > 1 else 95
    # Çizgiler
    ax.axhline(entry, color=ENTRY_C, linestyle="-", linewidth=1.5, alpha=0.95, zorder=5)
    ax.axhline(tp, color=TP_C, linestyle="-", linewidth=1.6, alpha=0.95, zorder=5)
    ax.axhline(sl, color=SL_C, linestyle="-", linewidth=1.6, alpha=0.95, zorder=5)
    # Etiketler (sağ kenar)
    x_lab = n_right - 1
    bbox = dict(boxstyle="round,pad=0.25", facecolor="#1e2330", edgecolor="#363a45", alpha=0.92)
    ax.annotate(f"GİRİŞ  {entry:.4f}", xy=(x_lab, entry), xytext=(8, 0),
                textcoords="offset points", color=ENTRY_C, fontsize=8, fontweight="bold",
                va="center", ha="left", bbox=bbox, zorder=7)
    ax.annotate(f"TP  {tp:.4f}", xy=(x_lab, tp), xytext=(8, 0),
                textcoords="offset points", color=TP_C, fontsize=8, fontweight="bold",
                va="center", ha="left", bbox=bbox, zorder=7)
    ax.annotate(f"SL  {sl:.4f}", xy=(x_lab, sl), xytext=(8, 0),
                textcoords="offset points", color=SL_C, fontsize=8, fontweight="bold",
                va="center", ha="left", bbox=bbox, zorder=7)
    # Legend için de ekle
    ax.plot([], [], color=ENTRY_C, linewidth=1.5, label=f"Giriş {entry:.4f}")
    ax.plot([], [], color=TP_C, linewidth=1.5, label=f"TP {tp:.4f}")
    ax.plot([], [], color=SL_C, linewidth=1.5, label=f"SL {sl:.4f}")


def create_signal_chart(df: pd.DataFrame, pos: "Position") -> Optional[bytes]:
    try:
        plot_df = df.tail(96).copy().reset_index(drop=True)
        n = len(plot_df)

        fig = plt.figure(figsize=(15, 17), facecolor=BG)
        gs = fig.add_gridspec(6, 1, height_ratios=[3.4, 1.05, 1.05, 1.1, 1.05, 1.0], hspace=0.06)

        ax1 = fig.add_subplot(gs[0])
        _style_axes(ax1)
        _draw_candles(ax1, plot_df)
        _draw_ema_lines(ax1, plot_df)
        ax1.set_xlim(-1, n + 14)
        _draw_levels(ax1, pos.entry_price, pos.tp, pos.sl, pos.side)
        side_emoji = "LONG ▲" if pos.side == "LONG" else "SHORT ▼"
        ax1.set_title(
            f"{pos.symbol}  ·  {side_emoji}  ·  10x  ·  100$  ·  15m\n"
            f"Kesik çizgi = Giriş  |  Yeşil = TP  |  Turuncu = SL",
            color=TEXT, fontsize=12, fontweight="bold", pad=10, loc="left"
        )
        _legend(ax1)

        ax2 = fig.add_subplot(gs[1], sharex=ax1)
        _style_axes(ax2, "KDJ")
        ax2.plot(plot_df["kdj_k"], color=EMA5_C, linewidth=1.15, label="K")
        ax2.plot(plot_df["kdj_d"], color=EMA20_C, linewidth=1.15, label="D")
        ax2.plot(plot_df["kdj_j"], color=UP, linewidth=1.0, label="J", alpha=0.85)
        ax2.axhline(80, color=MUTED, linestyle=":", linewidth=0.8)
        ax2.axhline(20, color=MUTED, linestyle=":", linewidth=0.8)
        ax2.fill_between(range(n), 80, 100, color=SHORT_C, alpha=0.06)
        ax2.fill_between(range(n), 0, 20, color=LONG_C, alpha=0.06)
        _legend(ax2)

        ax3 = fig.add_subplot(gs[2], sharex=ax1)
        _style_axes(ax3, "StochRSI")
        ax3.plot(plot_df["stochrsi_k"], color=EMA5_C, linewidth=1.15, label="K")
        ax3.plot(plot_df["stochrsi_d"], color=EMA20_C, linewidth=1.15, label="D")
        ax3.axhline(80, color=MUTED, linestyle=":", linewidth=0.8)
        ax3.axhline(20, color=MUTED, linestyle=":", linewidth=0.8)
        ax3.fill_between(range(n), 80, 100, color=SHORT_C, alpha=0.06)
        ax3.fill_between(range(n), 0, 20, color=LONG_C, alpha=0.06)
        _legend(ax3)

        ax4 = fig.add_subplot(gs[3], sharex=ax1)
        _style_axes(ax4, "MACD")
        ax4.plot(plot_df["macd_dif"], color=UP, linewidth=1.15, label="DIF")
        ax4.plot(plot_df["macd_dea"], color=DOWN, linewidth=1.15, label="DEA")
        hist_colors = [UP if v >= 0 else DOWN for v in plot_df["macd_hist"]]
        ax4.bar(range(n), plot_df["macd_hist"], color=hist_colors, width=0.65, alpha=0.65, zorder=2)
        ax4.axhline(0, color=MUTED, linewidth=0.7)
        _legend(ax4)

        ax5 = fig.add_subplot(gs[4], sharex=ax1)
        _style_axes(ax5, "RSI")
        ax5.plot(plot_df["rsi6"], color=EMA5_C, linewidth=1.15, label="RSI6")
        ax5.plot(plot_df["rsi14"], color=EMA20_C, linewidth=1.15, label="RSI14")
        ax5.axhline(70, color=MUTED, linestyle=":", linewidth=0.8)
        ax5.axhline(30, color=MUTED, linestyle=":", linewidth=0.8)
        ax5.fill_between(range(n), 70, 100, color=SHORT_C, alpha=0.06)
        ax5.fill_between(range(n), 0, 30, color=LONG_C, alpha=0.06)
        _legend(ax5)

        ax6 = fig.add_subplot(gs[5], sharex=ax1)
        _style_axes(ax6, "Wm %R")
        ax6.plot(plot_df["williams_r"], color=EMA5_C, linewidth=1.2, label="Williams %R")
        ax6.axhline(-20, color=MUTED, linestyle=":", linewidth=0.8)
        ax6.axhline(-80, color=MUTED, linestyle=":", linewidth=0.8)
        ax6.fill_between(range(n), -20, 0, color=SHORT_C, alpha=0.06)
        ax6.fill_between(range(n), -100, -80, color=LONG_C, alpha=0.06)
        _legend(ax6)

        # Tek giriş çizgisi (bu pozisyon)
        all_axes = [ax1, ax2, ax3, ax4, ax5, ax6]
        _draw_entry_marker(all_axes, plot_df, n, pos.side, price_ax=ax1, entry_idx=n - 1)

        for ax in [ax1, ax2, ax3, ax4, ax5]:
            plt.setp(ax.get_xticklabels(), visible=False)
        ax6.tick_params(axis="x", labelsize=7)

        fig.tight_layout(pad=0.6)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor(), edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        logger.error(f"Sinyal grafik hatası: {e}")
        return None


def create_indicator_chart(df: pd.DataFrame, symbol: str, open_positions: Optional[List] = None) -> Optional[bytes]:
    try:
        plot_df = df.tail(96).copy().reset_index(drop=True)
        last_close = float(plot_df["close"].iloc[-1])
        last_atr = float(plot_df["atr"].iloc[-1]) if pd.notna(plot_df["atr"].iloc[-1]) else 0.0
        n = len(plot_df)

        fig = plt.figure(figsize=(15, 17), facecolor=BG)
        gs = fig.add_gridspec(6, 1, height_ratios=[3.4, 1.05, 1.05, 1.1, 1.05, 1.0], hspace=0.06)

        ax1 = fig.add_subplot(gs[0])
        _style_axes(ax1)
        _draw_candles(ax1, plot_df)
        _draw_ema_lines(ax1, plot_df)

        title_extra = "Temiz görünüm — sadece EMA + göstergeler"
        open_side = None
        if open_positions:
            ax1.set_xlim(-1, n + 14)
            for p in open_positions:
                _draw_levels(ax1, p.entry_price, p.tp, p.sl, p.side)
                title_extra = f"Giriş {p.entry_price:.4f}  |  TP {p.tp:.4f}  |  SL {p.sl:.4f}"
                open_side = p.side
                break

        ax1.set_title(
            f"{symbol}  ·  {last_close:.4f}  ·  ATR {last_atr:.4f}  ·  15m  ·  ~1 gün\n"
            f"{title_extra}",
            color=TEXT, fontsize=12, fontweight="bold", pad=10, loc="left"
        )
        _legend(ax1)

        ax2 = fig.add_subplot(gs[1], sharex=ax1)
        _style_axes(ax2, "KDJ")
        ax2.plot(plot_df["kdj_k"], color=EMA5_C, linewidth=1.15, label="K")
        ax2.plot(plot_df["kdj_d"], color=EMA20_C, linewidth=1.15, label="D")
        ax2.plot(plot_df["kdj_j"], color=UP, linewidth=1.0, label="J", alpha=0.85)
        ax2.axhline(80, color=MUTED, linestyle=":", linewidth=0.8)
        ax2.axhline(20, color=MUTED, linestyle=":", linewidth=0.8)
        ax2.fill_between(range(n), 80, 100, color=SHORT_C, alpha=0.06)
        ax2.fill_between(range(n), 0, 20, color=LONG_C, alpha=0.06)
        _legend(ax2)

        ax3 = fig.add_subplot(gs[2], sharex=ax1)
        _style_axes(ax3, "StochRSI")
        ax3.plot(plot_df["stochrsi_k"], color=EMA5_C, linewidth=1.15, label="K")
        ax3.plot(plot_df["stochrsi_d"], color=EMA20_C, linewidth=1.15, label="D")
        ax3.axhline(80, color=MUTED, linestyle=":", linewidth=0.8)
        ax3.axhline(20, color=MUTED, linestyle=":", linewidth=0.8)
        ax3.fill_between(range(n), 80, 100, color=SHORT_C, alpha=0.06)
        ax3.fill_between(range(n), 0, 20, color=LONG_C, alpha=0.06)
        _legend(ax3)

        ax4 = fig.add_subplot(gs[3], sharex=ax1)
        _style_axes(ax4, "MACD")
        ax4.plot(plot_df["macd_dif"], color=UP, linewidth=1.15, label="DIF")
        ax4.plot(plot_df["macd_dea"], color=DOWN, linewidth=1.15, label="DEA")
        hist_colors = [UP if v >= 0 else DOWN for v in plot_df["macd_hist"]]
        ax4.bar(range(n), plot_df["macd_hist"], color=hist_colors, width=0.65, alpha=0.65, zorder=2)
        ax4.axhline(0, color=MUTED, linewidth=0.7)
        _legend(ax4)

        ax5 = fig.add_subplot(gs[4], sharex=ax1)
        _style_axes(ax5, "RSI")
        ax5.plot(plot_df["rsi6"], color=EMA5_C, linewidth=1.15, label="RSI6")
        ax5.plot(plot_df["rsi14"], color=EMA20_C, linewidth=1.15, label="RSI14")
        ax5.axhline(70, color=MUTED, linestyle=":", linewidth=0.8)
        ax5.axhline(30, color=MUTED, linestyle=":", linewidth=0.8)
        ax5.fill_between(range(n), 70, 100, color=SHORT_C, alpha=0.06)
        ax5.fill_between(range(n), 0, 30, color=LONG_C, alpha=0.06)
        _legend(ax5)

        ax6 = fig.add_subplot(gs[5], sharex=ax1)
        _style_axes(ax6, "Wm %R")
        ax6.plot(plot_df["williams_r"], color=EMA5_C, linewidth=1.2, label="Williams %R")
        ax6.axhline(-20, color=MUTED, linestyle=":", linewidth=0.8)
        ax6.axhline(-80, color=MUTED, linestyle=":", linewidth=0.8)
        ax6.fill_between(range(n), -20, 0, color=SHORT_C, alpha=0.06)
        ax6.fill_between(range(n), -100, -80, color=LONG_C, alpha=0.06)
        _legend(ax6)

        # Açık pozisyon varsa sadece o giriş çizgisi
        if open_side:
            all_axes = [ax1, ax2, ax3, ax4, ax5, ax6]
            _draw_entry_marker(all_axes, plot_df, n, open_side, price_ax=ax1, entry_idx=n - 1)

        for ax in [ax1, ax2, ax3, ax4, ax5]:
            plt.setp(ax.get_xticklabels(), visible=False)
        ax6.tick_params(axis="x", labelsize=7)

        fig.tight_layout(pad=0.6)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor(), edgecolor="none")
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

    async def send_startup(self, n_symbols: int = 20, symbols: Optional[List[str]] = None):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sym_txt = ", ".join((symbols or [])[:8])
        if symbols and len(symbols) > 8:
            sym_txt += f" … +{len(symbols)-8}"
        await self.send(
            f"✅ <b>Bot başlatıldı</b>\n"
            f"📁 Dosya: <code>{BOT_FILENAME}</code>\n"
            f"Coin: <b>{n_symbols}</b> (Altın + Gümüş dahil)\n"
            f"<code>{sym_txt}</code>\n"
            f"Sanal cüzdan: <b>{VIRTUAL_BALANCE:.0f} USDT</b>\n"
            f"İşlem: 100$ | Marj: {MARGIN_USD:.0f}$ | 10x | TF: 15m\n"
            f"Pozisyon açılınca: grafik + Giriş/TP/SL\n"
            f"Saatlik rapor: kar/zarar + cüzdan\n"
            f"15 dk grafik <b>kapalı</b>\n"
            f"<code>{now}</code>"
        )

    async def send_new_position(self, pos: Position, chart_bytes: Optional[bytes] = None):
        emoji = "🟢" if pos.side == "LONG" else "🔴"
        caption = (
            f"{emoji} <b>Yeni Sanal İşlem</b>\n"
            f"📁 <code>{BOT_FILENAME}</code>\n\n"
            f"Sembol: <code>{pos.symbol}</code>\nYön: <b>{pos.side}</b>\n"
            f"Giriş: <code>{pos.entry_price:.6f}</code>\n"
            f"TP: <code>{pos.tp:.6f}</code>\nSL: <code>{pos.sl:.6f}</code> (1.5×ATR)\n"
            f"ATR: <code>{pos.atr:.6f}</code>\n"
            f"İşlem: 100 USDT | Marj: {MARGIN_USD:.0f} USDT | 10x | 15m"
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
            logger.error(f"Telegram grafik hatası: {e}")
            await self.send(caption)

    async def send_closed(self, pos: Position, balance: float = 0.0, total_pnl: float = 0.0):
        emoji = "✅" if pos.pnl >= 0 else "❌"
        await self.send(
            f"{emoji} <b>Pozisyon Kapandı</b>\n"
            f"📁 <code>{BOT_FILENAME}</code>\n\n"
            f"Sembol: <code>{pos.symbol}</code> | {pos.side}\n"
            f"Giriş → Çıkış: <code>{pos.entry_price:.6f}</code> → <code>{pos.close_price:.6f}</code>\n"
            f"Bu işlem PnL: <b>{pos.pnl:+.4f} USDT</b>\n"
            f"Sebep: <code>{pos.status}</code>\n"
            f"Süre: {pos.open_time.strftime('%H:%M')} → {datetime.utcnow().strftime('%H:%M')} UTC\n"
            f"────────────\n"
            f"💰 Cüzdan: <b>{balance:.2f} USDT</b>\n"
            f"📈 Toplam P&L: <b>{total_pnl:+.4f} USDT</b>"
        )

    async def send_hourly(self, scanned: int, new_trades: int, open_pos: List[Position],
                          total_pnl: float, total_trades: int, balance: float = 2000.0,
                          prices: Optional[Dict[str, float]] = None):
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        prices = prices or {}
        unrealized = 0.0
        detail_lines = []
        if open_pos:
            for i, p in enumerate(open_pos[:20], 1):
                px = prices.get(p.symbol)
                if px is not None and px > 0:
                    if p.side == "LONG":
                        upnl = (px - p.entry_price) * p.quantity
                    else:
                        upnl = (p.entry_price - px) * p.quantity
                    unrealized += upnl
                    emoji = "🟢" if upnl >= 0 else "🔴"
                    detail_lines.append(
                        f"{i}. {emoji} <code>{p.symbol}</code> {p.side}\n"
                        f"   Giriş {p.entry_price:.5f} → Fiyat {px:.5f}\n"
                        f"   K/Z: <b>{upnl:+.4f} USDT</b> | TP {p.tp:.5f} | SL {p.sl:.5f}"
                    )
                else:
                    detail_lines.append(
                        f"{i}. <code>{p.symbol}</code> {p.side}\n"
                        f"   Giriş {p.entry_price:.5f} | TP {p.tp:.5f} | SL {p.sl:.5f}"
                    )

        u_emoji = "🟢" if unrealized >= 0 else "🔴"
        lines = [
            f"📊 <b>Saatlik Cüzdan Raporu</b> – {now}",
            f"📁 Dosya: <code>{BOT_FILENAME}</code>",
            f"💰 Sanal cüzdan: <b>{balance:.2f} USDT</b>",
            f"📈 Gerçekleşen P&L: <b>{total_pnl:+.4f} USDT</b>",
            f"{u_emoji} Açık K/Z (anlık): <b>{unrealized:+.4f} USDT</b>",
            f"Bu saatte açılan: <b>{new_trades}</b> | Toplam işlem: <b>{total_trades}</b>",
            f"Aktif pozisyon: <b>{len(open_pos)}</b> / {scanned} coin",
            ""
        ]
        if detail_lines:
            lines.append("<b>Açık pozisyonlar:</b>")
            lines.extend(detail_lines)
        else:
            lines.append("Açık pozisyon yok.")
        await self.send("\n".join(lines))

    async def send_indicator_chart(self, chart_bytes: bytes, symbol: str, price: float):
        if not self.enabled or not chart_bytes:
            return
        caption = (
            f"📈 <b>15m İndikatör Grafiği (~1 gün)</b>\n"
            f"📁 <code>{BOT_FILENAME}</code>\n"
            f"Sembol: <code>{symbol}</code> | Fiyat: <code>{price:.4f}</code>\n"
            f"Kesik çizgi = Giriş | Yeşil = TP | Turuncu = SL"
        )
        try:
            await self.bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=InputFile(io.BytesIO(chart_bytes), filename="indicators.png"),
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Grafik gönderme hatası: {e}")


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
        self.last_df: Optional[pd.DataFrame] = None

    async def load_markets(self):
        await self.exchange.load_markets()
        self.symbols = []
        for sym in WATCH_SYMBOLS:
            if sym in self.exchange.markets:
                self.symbols.append(sym)
            else:
                # alternatif isimler dene
                alt = sym.replace("/USDT:USDT", "USDT").replace("/", "")
                found = False
                for m in self.exchange.markets:
                    if m.replace("/", "").replace(":USDT", "") == alt.replace(":USDT", ""):
                        self.symbols.append(m)
                        found = True
                        break
                if not found:
                    logger.warning(f"Sembol bulunamadı: {sym}")
        if not self.symbols:
            self.symbols = ["XAG/USDT:USDT", "XAU/USDT:USDT"]
        logger.info(f"Yüklenen {len(self.symbols)} sembol: {self.symbols}")

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
        dfs: Dict[str, pd.DataFrame] = {}
        for res in results:
            if isinstance(res, Exception):
                continue
            symbol, signal, entry, atr, df = res
            if entry > 0:
                prices[symbol] = entry
            if df is not None:
                dfs[symbol] = df
                self.last_df = df
            if signal and atr > 0 and df is not None:
                pos = self.trader.open_position(symbol, signal, entry, atr)
                if pos:
                    signals += 1
                    self.hourly_new_trades += 1
                    chart = create_signal_chart(df, pos)
                    await self.notifier.send_new_position(pos, chart)

        closed = self.trader.update_positions(prices, dfs)
        stats = self.trader.get_stats()
        for pos in closed:
            await self.notifier.send_closed(
                pos,
                balance=stats.get("balance", 0.0),
                total_pnl=stats.get("total_pnl", 0.0),
            )

        self.last_scan_count = len(self.symbols)
        logger.info(f"Tarama bitti | Sembol:{len(self.symbols)} | Sinyal:{signals} | Açık:{len(self.trader.get_open_positions())}")

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
                open_pos = self.trader.get_open_positions()
                # Güncel fiyatları çek (K/Z için)
                prices: Dict[str, float] = {}
                for p in open_pos:
                    try:
                        t = await self.exchange.fetch_ticker(p.symbol)
                        last = t.get("last") or t.get("close")
                        if last:
                            prices[p.symbol] = float(last)
                    except Exception as e:
                        logger.warning(f"Rapor fiyat hatası {p.symbol}: {e}")
                await self.notifier.send_hourly(
                    self.last_scan_count, new,
                    open_pos,
                    stats["total_pnl"], stats["total_trades"],
                    stats.get("balance", 1000.0),
                    prices=prices,
                )
            except Exception as e:
                logger.error(f"Rapor hatası: {e}")
            await asyncio.sleep(REPORT_INTERVAL_MINUTES * 60)

    async def start(self):
        logger.info("=" * 50)
        logger.info(f"Peak Reversal Futures Bot | 20 coin | 15m | {BOT_FILENAME}")
        await self.load_markets()
        await self.notifier.send_startup(len(self.symbols), self.symbols)
        self.running = True
        await asyncio.gather(
            self.scan_loop(),
            self.report_loop(),
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
