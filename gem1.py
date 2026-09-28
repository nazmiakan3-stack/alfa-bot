#!/usr/bin/env python3
"""
Peak Reversal Futures Bot - TEK DOSYA VERSİYONU (gem1.py)
Sadece XAGUSDT taranır.
Contabo / Termius için optimize edilmiştir.

Güncel Mantık (2026-09-26):
LONG:
  - EMA5 en düşük noktada + dolmaya başlamış (EMA20 kesişimi YOK)
  - KDJ 0-20 | StochRSI 0-30 | RSI 0-30
  - MACD: DIF aşağıdan yukarı DEA kesiyor
  - Williams %R: -100 ile -70 arası
SHORT:
  - EMA5 en yüksek noktada + düşmeye başlamış
  - KDJ 80-100 | StochRSI 70-100 | RSI 70-100
  - MACD: DIF yukarıdan aşağı DEA kesiyor
  - Williams %R: -30 ile 0 arası
Çıkış: TP/SL veya EMA5/EMA20 ters kesişim | SL=1.5ATR | TP=2ATR
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

LEVERAGE = 10
NOTIONAL_USD = 100.0
MARGIN_USD = 200.0
VIRTUAL_BALANCE = 1000.0
SCAN_INTERVAL_SECONDS = 60
REPORT_INTERVAL_MINUTES = 60
CHART_INTERVAL_SECONDS = 300
TIMEFRAME = "15m"
LOG_LEVEL = "INFO"

EMA_FAST, EMA_MID, EMA_SLOW = 5, 20, 99
RSI_FAST, RSI_SLOW = 6, 14
ATR_PERIOD = 14
WILLIAMS_PERIOD = 14
SIGNAL_LOOKBACK = 4          # ±2 mum penceresi
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
        logger.info(f"SANAL AÇILDI | {side} {symbol} | 10x | 100$ | Marj:200$ | Bakiye:{self.balance:.2f}")
        return pos

    def update_positions(self, prices: Dict[str, float], df: Optional[pd.DataFrame] = None) -> List[Position]:
        """TP/SL + EMA5/EMA20 ters kesişim ile kapatma"""
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
            # 1) TP / SL
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

            # 2) EMA5 / EMA20 ters kesişim ile çıkış
            if not hit and df is not None and len(df) >= 2:
                curr = df.iloc[-1]
                prev = df.iloc[-2]
                if pos.side == "LONG":
                    # EMA5 üstten EMA20'yi aşağı kesti → LONG kapat
                    if prev["ema5"] >= prev["ema20"] and curr["ema5"] < curr["ema20"]:
                        pos.status, pos.close_price = "CLOSED_EMA_CROSS", price
                        pos.pnl = (price - pos.entry_price) * pos.quantity
                        hit = True
                else:
                    # EMA5 alttan EMA20'yi yukarı kesti → SHORT kapat
                    if prev["ema5"] <= prev["ema20"] and curr["ema5"] > curr["ema20"]:
                        pos.status, pos.close_price = "CLOSED_EMA_CROSS", price
                        pos.pnl = (pos.entry_price - price) * pos.quantity
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

def _ema5_at_bottom(df: pd.DataFrame, idx: int) -> bool:
    """EMA5 lokal dip + yukarı dönüş (canlı mumda gelecek bar aranmaz)"""
    if idx < 3:
        return False
    e = df["ema5"]
    start = max(0, idx - 8)
    at_low = e.iloc[idx] <= e.iloc[start:idx + 1].min() * 1.001
    rising = e.iloc[idx] >= e.iloc[idx - 1]
    # Pivot: önceki 2 mumdan düşük
    is_pivot = e.iloc[idx] <= e.iloc[idx - 1] and e.iloc[idx] <= e.iloc[idx - 2]
    if idx < len(df) - 1:
        is_pivot = is_pivot and e.iloc[idx] <= e.iloc[idx + 1]
    return (is_pivot and rising) or (at_low and rising)


def _ema5_at_top(df: pd.DataFrame, idx: int) -> bool:
    """EMA5 lokal tepe + aşağı dönüş (canlı mumda gelecek bar aranmaz)"""
    if idx < 3:
        return False
    e = df["ema5"]
    start = max(0, idx - 8)
    at_high = e.iloc[idx] >= e.iloc[start:idx + 1].max() * 0.999
    falling = e.iloc[idx] <= e.iloc[idx - 1]
    is_pivot = e.iloc[idx] >= e.iloc[idx - 1] and e.iloc[idx] >= e.iloc[idx - 2]
    if idx < len(df) - 1:
        is_pivot = is_pivot and e.iloc[idx] >= e.iloc[idx + 1]
    return (is_pivot and falling) or (at_high and falling)


def _kdj_long_ok(row) -> bool:
    return (0 <= row["kdj_k"] <= 25) or (0 <= row["kdj_j"] <= 25)


def _kdj_short_ok(row) -> bool:
    return (75 <= row["kdj_k"] <= 100) or (75 <= row["kdj_j"] <= 100)


def _stoch_long_ok(row) -> bool:
    return 0 <= row["stochrsi_k"] <= 35


def _stoch_short_ok(row) -> bool:
    return 65 <= row["stochrsi_k"] <= 100


def _rsi_long_ok(row) -> bool:
    return (0 <= row["rsi14"] <= 35) or (0 <= row["rsi6"] <= 35)


def _rsi_short_ok(row) -> bool:
    return (65 <= row["rsi14"] <= 100) or (65 <= row["rsi6"] <= 100)


def _will_long_ok(row) -> bool:
    return -100 <= row["williams_r"] <= -65


def _will_short_ok(row) -> bool:
    return -35 <= row["williams_r"] <= 0


def _macd_long_ok(df: pd.DataFrame, idx: int) -> bool:
    """DIF aşağıdan yukarı DEA kesiyor (bu mum veya 1-2 önceki)"""
    for j in range(max(1, idx - 2), idx + 1):
        if j < 1:
            continue
        c, p = df.iloc[j], df.iloc[j - 1]
        if p["macd_dif"] <= p["macd_dea"] and c["macd_dif"] > c["macd_dea"]:
            return True
        # hist negatiften pozitife / yükseliyor
        if c["macd_hist"] > p["macd_hist"] and p["macd_hist"] <= 0.02:
            return True
    return False


def _macd_short_ok(df: pd.DataFrame, idx: int) -> bool:
    for j in range(max(1, idx - 2), idx + 1):
        if j < 1:
            continue
        c, p = df.iloc[j], df.iloc[j - 1]
        if p["macd_dif"] >= p["macd_dea"] and c["macd_dif"] < c["macd_dea"]:
            return True
        if c["macd_hist"] < p["macd_hist"] and p["macd_hist"] >= -0.02:
            return True
    return False


def _any_in_window(df: pd.DataFrame, idx: int, check_fn, window: int = 2) -> bool:
    """±window mum içinde check_fn True ise OK"""
    for j in range(max(0, idx - window), min(len(df), idx + window + 1)):
        if check_fn(df.iloc[j]):
            return True
    return False


def check_long(df: pd.DataFrame, idx: int) -> bool:
    """
    LONG:
    1) EMA5 en dipte + dolmaya başlamış (ana tetik)
    2) KDJ 0-25, StochRSI 0-35, RSI 0-35, Williams -100/-65
       → bunlar ±2 mum içinde sağlanabilir
    3) MACD DIF↑DEA veya hist yukarı (±2 mum)
    """
    if idx < 3:
        return False

    if not _ema5_at_bottom(df, idx):
        return False

    kdj_ok = _any_in_window(df, idx, _kdj_long_ok, 2)
    stoch_ok = _any_in_window(df, idx, _stoch_long_ok, 2)
    rsi_ok = _any_in_window(df, idx, _rsi_long_ok, 2)
    will_ok = _any_in_window(df, idx, _will_long_ok, 2)
    macd_ok = _macd_long_ok(df, idx)

    # En az 3 yan şart + MACD (toplam esnek ama anlamlı)
    side_score = sum([kdj_ok, stoch_ok, rsi_ok, will_ok, macd_ok])
    return side_score >= 3


def check_short(df: pd.DataFrame, idx: int) -> bool:
    """
    SHORT:
    1) EMA5 en tepede + düşmeye başlamış (ana tetik)
    2) KDJ 75-100, StochRSI 65-100, RSI 65-100, Williams -35/0
       → ±2 mum içinde
    3) MACD DIF↓DEA veya hist aşağı (±2 mum)
    """
    if idx < 3:
        return False

    if not _ema5_at_top(df, idx):
        return False

    kdj_ok = _any_in_window(df, idx, _kdj_short_ok, 2)
    stoch_ok = _any_in_window(df, idx, _stoch_short_ok, 2)
    rsi_ok = _any_in_window(df, idx, _rsi_short_ok, 2)
    will_ok = _any_in_window(df, idx, _will_short_ok, 2)
    macd_ok = _macd_short_ok(df, idx)

    side_score = sum([kdj_ok, stoch_ok, rsi_ok, will_ok, macd_ok])
    return side_score >= 3


def detect_signal(df: pd.DataFrame) -> Optional[str]:
    if df is None or len(df) < 30:
        return None
    # Son 6 muma bak (±2 + biraz pay)
    start = max(len(df) - 1 - 6, 3)
    for idx in range(len(df) - 1, start - 1, -1):
        if check_long(df, idx):
            return "LONG"
        if check_short(df, idx):
            return "SHORT"
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
    """Grafikte EMA5 tepe/dip noktalarını işaretlemek için"""
    bottoms, tops = [], []
    start = max(3, len(df) - lookback)
    for idx in range(start, len(df)):
        if _ema5_at_bottom(df, idx):
            bottoms.append(idx - start)
        if _ema5_at_top(df, idx):
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


def _draw_ema_lines(ax, plot_df, extremes):
    ax.plot(plot_df["ema5"], color=EMA5_C, linewidth=1.6, label="EMA5", zorder=4)
    ax.plot(plot_df["ema20"], color=EMA20_C, linewidth=1.4, label="EMA20", zorder=4)
    ax.plot(plot_df["ema99"], color=EMA99_C, linewidth=1.3, label="EMA99", zorder=4, alpha=0.9)

    for x in extremes.get("bottoms", []):
        if 0 <= x < len(plot_df):
            ax.axvline(x, color=LONG_C, linestyle="--", linewidth=1.15, alpha=0.75, zorder=1)
            ax.scatter(x, plot_df["ema5"].iloc[x], marker="^", s=55, color=LONG_C,
                       edgecolors="white", linewidths=0.6, zorder=6)
    for x in extremes.get("tops", []):
        if 0 <= x < len(plot_df):
            ax.axvline(x, color=SHORT_C, linestyle="--", linewidth=1.15, alpha=0.75, zorder=1)
            ax.scatter(x, plot_df["ema5"].iloc[x], marker="v", s=55, color=SHORT_C,
                       edgecolors="white", linewidths=0.6, zorder=6)


def _draw_extreme_vlines(axes, extremes, n):
    for ax in axes:
        for x in extremes.get("bottoms", []):
            if 0 <= x < n:
                ax.axvline(x, color=LONG_C, linestyle="--", linewidth=1.0, alpha=0.55, zorder=1)
        for x in extremes.get("tops", []):
            if 0 <= x < n:
                ax.axvline(x, color=SHORT_C, linestyle="--", linewidth=1.0, alpha=0.55, zorder=1)


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
        extremes = find_ema_extremes(df, lookback=96)
        n = len(plot_df)

        fig = plt.figure(figsize=(15, 17), facecolor=BG)
        gs = fig.add_gridspec(6, 1, height_ratios=[3.4, 1.05, 1.05, 1.1, 1.05, 1.0], hspace=0.06)

        # --- Fiyat ---
        ax1 = fig.add_subplot(gs[0])
        _style_axes(ax1)
        _draw_candles(ax1, plot_df)
        _draw_ema_lines(ax1, plot_df, extremes)
        ax1.set_xlim(-1, n + 12)  # etiketler için sağda boşluk
        _draw_levels(ax1, pos.entry_price, pos.tp, pos.sl, pos.side)
        side_emoji = "LONG ▲" if pos.side == "LONG" else "SHORT ▼"
        ax1.set_title(
            f"{pos.symbol}  ·  {side_emoji}  ·  10x  ·  100$  ·  15m\n"
            f"Mavi kesik = EMA dip  |  Kırmızı kesik = EMA tepe  |  Giriş / TP / SL çizgileri",
            color=TEXT, fontsize=12, fontweight="bold", pad=10, loc="left"
        )
        _legend(ax1)

        # --- KDJ ---
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

        # --- StochRSI ---
        ax3 = fig.add_subplot(gs[2], sharex=ax1)
        _style_axes(ax3, "StochRSI")
        ax3.plot(plot_df["stochrsi_k"], color=EMA5_C, linewidth=1.15, label="K")
        ax3.plot(plot_df["stochrsi_d"], color=EMA20_C, linewidth=1.15, label="D")
        ax3.axhline(80, color=MUTED, linestyle=":", linewidth=0.8)
        ax3.axhline(20, color=MUTED, linestyle=":", linewidth=0.8)
        ax3.fill_between(range(n), 80, 100, color=SHORT_C, alpha=0.06)
        ax3.fill_between(range(n), 0, 20, color=LONG_C, alpha=0.06)
        _legend(ax3)

        # --- MACD ---
        ax4 = fig.add_subplot(gs[3], sharex=ax1)
        _style_axes(ax4, "MACD")
        ax4.plot(plot_df["macd_dif"], color=UP, linewidth=1.15, label="DIF")
        ax4.plot(plot_df["macd_dea"], color=DOWN, linewidth=1.15, label="DEA")
        hist_colors = [UP if v >= 0 else DOWN for v in plot_df["macd_hist"]]
        ax4.bar(range(n), plot_df["macd_hist"], color=hist_colors, width=0.65, alpha=0.65, zorder=2)
        ax4.axhline(0, color=MUTED, linewidth=0.7)
        _legend(ax4)

        # --- RSI ---
        ax5 = fig.add_subplot(gs[4], sharex=ax1)
        _style_axes(ax5, "RSI")
        ax5.plot(plot_df["rsi6"], color=EMA5_C, linewidth=1.15, label="RSI6")
        ax5.plot(plot_df["rsi14"], color=EMA20_C, linewidth=1.15, label="RSI14")
        ax5.axhline(70, color=MUTED, linestyle=":", linewidth=0.8)
        ax5.axhline(30, color=MUTED, linestyle=":", linewidth=0.8)
        ax5.fill_between(range(n), 70, 100, color=SHORT_C, alpha=0.06)
        ax5.fill_between(range(n), 0, 30, color=LONG_C, alpha=0.06)
        _legend(ax5)

        # --- Williams ---
        ax6 = fig.add_subplot(gs[5], sharex=ax1)
        _style_axes(ax6, "Wm %R")
        ax6.plot(plot_df["williams_r"], color=EMA5_C, linewidth=1.2, label="Williams %R")
        ax6.axhline(-20, color=MUTED, linestyle=":", linewidth=0.8)
        ax6.axhline(-80, color=MUTED, linestyle=":", linewidth=0.8)
        ax6.fill_between(range(n), -20, 0, color=SHORT_C, alpha=0.06)
        ax6.fill_between(range(n), -100, -80, color=LONG_C, alpha=0.06)
        _legend(ax6)

        _draw_extreme_vlines([ax2, ax3, ax4, ax5, ax6], extremes, n)

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
        extremes = find_ema_extremes(df, lookback=96)
        n = len(plot_df)

        fig = plt.figure(figsize=(15, 17), facecolor=BG)
        gs = fig.add_gridspec(6, 1, height_ratios=[3.4, 1.05, 1.05, 1.1, 1.05, 1.0], hspace=0.06)

        ax1 = fig.add_subplot(gs[0])
        _style_axes(ax1)
        _draw_candles(ax1, plot_df)
        _draw_ema_lines(ax1, plot_df, extremes)

        # Açık pozisyon varsa Giriş / TP / SL göster
        title_extra = "Mavi kesik = EMA dip  |  Kırmızı kesik = EMA tepe"
        if open_positions:
            ax1.set_xlim(-1, n + 12)
            for p in open_positions:
                if getattr(p, "symbol", "") == symbol or True:
                    _draw_levels(ax1, p.entry_price, p.tp, p.sl, p.side)
                    title_extra = f"Giriş {p.entry_price:.4f}  |  TP {p.tp:.4f}  |  SL {p.sl:.4f}"
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

        _draw_extreme_vlines([ax2, ax3, ax4, ax5, ax6], extremes, n)

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

    async def send_startup(self):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        await self.send(
            f"✅ <b>Peak Reversal Futures Bot aktif</b>\n"
            f"📁 Dosya: <code>gem1_final.py</code>\n"
            f"Sadece <b>XAGUSDT</b> | Sanal Bakiye: 1000 USDT\n"
            f"10x İzole | 100$ İşlem | 200$ Marj\n"
            f"Timeframe: <b>15m</b>\n"
            f"LONG: EMA5 dip+doluyor | KDJ0-20 | Stoch0-30 | RSI0-30 | MACD↑ | Wm-100/-70\n"
            f"SHORT: EMA5 tepe+düşüyor | KDJ80-100 | Stoch70-100 | RSI70-100 | MACD↓ | Wm-30/0\n"
            f"SL = 1.5×ATR | TP = 2×ATR | Ters EMA kesişimde kapanır\n"
            f"<code>{now}</code>"
        )

    async def send_new_position(self, pos: Position, chart_bytes: Optional[bytes] = None):
        emoji = "🟢" if pos.side == "LONG" else "🔴"
        caption = (
            f"{emoji} <b>Yeni Sanal İşlem</b>\n\n"
            f"Sembol: <code>{pos.symbol}</code>\nYön: <b>{pos.side}</b>\n"
            f"Giriş: <code>{pos.entry_price:.6f}</code>\n"
            f"TP: <code>{pos.tp:.6f}</code>\nSL: <code>{pos.sl:.6f}</code> (1.5×ATR)\n"
            f"ATR: <code>{pos.atr:.6f}</code>\n"
            f"İşlem: 100 USDT | Marj: 200 USDT | 10x | 15m"
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

    async def send_closed(self, pos: Position):
        emoji = "✅" if pos.pnl >= 0 else "❌"
        await self.send(
            f"{emoji} <b>Pozisyon Kapandı</b>\n\n"
            f"Sembol: <code>{pos.symbol}</code> | {pos.side}\n"
            f"Giriş → Çıkış: <code>{pos.entry_price:.6f}</code> → <code>{pos.close_price:.6f}</code>\n"
            f"PnL: <b>{pos.pnl:+.4f} USDT</b> | {pos.status}"
        )

    async def send_hourly(self, scanned: int, new_trades: int, open_pos: List[Position],
                          total_pnl: float, total_trades: int, balance: float = 1000.0):
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        lines = [
            f"📊 <b>Saatlik Rapor</b> – {now}",
            f"Sanal Bakiye: <b>{balance:.2f} USDT</b>",
            f"Bu saatte açılan: <b>{new_trades}</b>",
            f"Aktif Pozisyon: <b>{len(open_pos)}</b>",
            f"Toplam İşlem: <b>{total_trades}</b>",
            f"P&L (Sanal): <b>{total_pnl:+.4f} USDT</b>",
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
            f"📈 <b>15m İndikatör Grafiği (~1 gün)</b>\n"
            f"Sembol: <code>{symbol}</code> | Fiyat: <code>{price:.4f}</code>\n"
            f"Mavi kesik çizgi = EMA DİP | Kırmızı kesik çizgi = EMA TEPE"
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
        last_df = None
        for res in results:
            if isinstance(res, Exception):
                continue
            symbol, signal, entry, atr, df = res
            if entry > 0:
                prices[symbol] = entry
            if df is not None:
                last_df = df
                self.last_df = df
            if signal and atr > 0 and df is not None:
                pos = self.trader.open_position(symbol, signal, entry, atr)
                if pos:
                    signals += 1
                    self.hourly_new_trades += 1
                    chart = create_signal_chart(df, pos)
                    await self.notifier.send_new_position(pos, chart)

        closed = self.trader.update_positions(prices, last_df)
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
        await asyncio.sleep(10)
        while self.running:
            try:
                if not self.symbols:
                    await asyncio.sleep(CHART_INTERVAL_SECONDS)
                    continue
                symbol = self.symbols[0]
                _, _, entry, _, df = await self.scan_symbol(symbol)
                if df is not None and entry > 0:
                    open_pos = self.trader.get_open_positions()
                    chart = create_indicator_chart(df, symbol, open_positions=open_pos)
                    if chart:
                        await self.notifier.send_indicator_chart(chart, symbol, entry)
                        logger.info(f"15m grafik gönderildi | {symbol} @ {entry:.4f}")
            except Exception as e:
                logger.error(f"Grafik hatası: {e}")
            await asyncio.sleep(CHART_INTERVAL_SECONDS)

    async def start(self):
        logger.info("=" * 50)
        logger.info("Peak Reversal Futures Bot başlatılıyor... (gem1_final.py) | 15m | EMA5/20 cross")
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
