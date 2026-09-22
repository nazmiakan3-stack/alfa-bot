#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
PRFB - Peak Reversal Futures Bot
TEK DOSYA CONTABO/VPS SÜRÜMÜ

ZIP içindeki ana modüller tek dosyada birleştirilmiştir:
- config
- logger
- exchange
- indicators
- signals
- paper_trader
- scanner
- telegram_bot
- main

Çalışma modu: PAPER TRADING.
Gerçek emir göndermez.
"""

import os
import sys
import time
import asyncio
import signal
import logging
import subprocess
import importlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Literal


# ============================================================
# OTOMATİK BAĞIMLILIK KURULUMU
# ============================================================

REQUIRED_PACKAGES = {
    "ccxt": "ccxt>=4.2.0",
    "pandas": "pandas>=2.1.0",
    "numpy": "numpy>=1.26.0",
    "dotenv": "python-dotenv>=1.0.0",
    "telegram": "python-telegram-bot>=21.0",
    "aiohttp": "aiohttp>=3.9.0",
}


def ensure_packages():
    missing = []

    for module, package in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)

    if missing:
        print(
            "Eksik paketler kuruluyor:",
            ", ".join(missing),
            flush=True
        )

        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            *missing
        ])

        print(
            "Paketler kuruldu. Program yeniden başlatılıyor...",
            flush=True
        )

        os.execv(
            sys.executable,
            [sys.executable] + sys.argv
        )


ensure_packages()


# ============================================================
# IMPORT
# ============================================================

import ccxt.async_support as ccxt
import pandas as pd
import numpy as np

from dotenv import load_dotenv

from telegram import Bot
from telegram.constants import ParseMode


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    ""
).strip()

BINANCE_API_KEY = os.getenv(
    "BINANCE_API_KEY",
    ""
).strip()

BINANCE_API_SECRET = os.getenv(
    "BINANCE_API_SECRET",
    ""
).strip()


LEVERAGE = int(
    os.getenv("LEVERAGE", "5")
)

NOTIONAL_USD = float(
    os.getenv("NOTIONAL_USD", "10")
)

MARGIN_USD = float(
    os.getenv("MARGIN_USD", "15")
)

SCAN_INTERVAL_SECONDS = int(
    os.getenv("SCAN_INTERVAL_SECONDS", "60")
)

REPORT_INTERVAL_MINUTES = int(
    os.getenv("REPORT_INTERVAL_MINUTES", "60")
)

TIMEFRAME = os.getenv(
    "TIMEFRAME",
    "1m"
)

LOG_LEVEL = os.getenv(
    "LOG_LEVEL",
    "INFO"
).upper()

MAX_CONCURRENT = int(
    os.getenv("MAX_CONCURRENT", "15")
)


# ============================================================
# İNDİKATÖR AYARLARI
# ============================================================

EMA_FAST = 5
EMA_MID = 20
EMA_SLOW = 99

RSI_FAST = 6
RSI_SLOW = 14

ATR_PERIOD = 14

WILLIAMS_PERIOD = 14

SIGNAL_LOOKBACK = 1

QUOTE_ASSET = "USDT"


# ============================================================
# LOGGER
# ============================================================

logging.basicConfig(
    level=getattr(
        logging,
        LOG_LEVEL,
        logging.INFO
    ),

    format=(
        "%(asctime)s | "
        "%(levelname)-8s | "
        "%(name)s | "
        "%(message)s"
    ),

    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("PRFB")


# ============================================================
# BINANCE EXCHANGE
# ============================================================

class ExchangeClient:

    def __init__(self):

        self.exchange = ccxt.binanceusdm({

            "apiKey": (
                BINANCE_API_KEY
                or None
            ),

            "secret": (
                BINANCE_API_SECRET
                or None
            ),

            "enableRateLimit": True,

            "options": {
                "defaultType": "future",
                "adjustForTimeDifference": True,
            },
        })

        self.markets = {}

        self.symbols: List[str] = []


    async def load_markets(self):

        self.markets = (
            await self.exchange.load_markets()
        )

        self.symbols = [

            symbol

            for symbol, market
            in self.markets.items()

            if market.get("quote") == QUOTE_ASSET

            and market.get("swap") is True

            and market.get("active") is True

            and market.get(
                "info",
                {}
            ).get(
                "contractType"
            ) == "PERPETUAL"

        ]

        logger.info(
            "Yüklenen USDT perpetual sembol sayısı: %d",
            len(self.symbols)
        )


    async def fetch_ohlcv(
        self,
        symbol: str,
        limit: int = 150
    ):

        try:

            return await self.exchange.fetch_ohlcv(

                symbol,

                timeframe=TIMEFRAME,

                limit=limit

            )

        except Exception as e:

            logger.debug(
                "%s OHLCV alınamadı: %s",
                symbol,
                e
            )

            return None


    async def close(self):

        try:

            await self.exchange.close()

        except Exception:

            pass


# ============================================================
# DATAFRAME
# ============================================================

def ohlcv_to_df(
    ohlcv: list
) -> pd.DataFrame:

    df = pd.DataFrame(
        ohlcv,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    )

    df["timestamp"] = pd.to_datetime(
        df["timestamp"],
        unit="ms"
    )

    return df


# ============================================================
# İNDİKATÖRLER
# ============================================================

def calculate_indicators(
    df: pd.DataFrame
) -> Optional[pd.DataFrame]:

    if df is None or len(df) < 120:
        return None

    try:

        # ----------------------------------------------------
        # EMA
        # ----------------------------------------------------

        df["ema5"] = (
            df["close"]
            .ewm(
                span=EMA_FAST,
                adjust=False
            )
            .mean()
        )

        df["ema20"] = (
            df["close"]
            .ewm(
                span=EMA_MID,
                adjust=False
            )
            .mean()
        )

        df["ema99"] = (
            df["close"]
            .ewm(
                span=EMA_SLOW,
                adjust=False
            )
            .mean()
        )


        # ----------------------------------------------------
        # RSI 6 / 14
        # ----------------------------------------------------

        delta = df["close"].diff()

        gain = delta.where(
            delta > 0,
            0.0
        )

        loss = -delta.where(
            delta < 0,
            0.0
        )


        avg_gain6 = (
            gain.ewm(
                alpha=1 / RSI_FAST,
                min_periods=RSI_FAST,
                adjust=False
            )
            .mean()
        )

        avg_loss6 = (
            loss.ewm(
                alpha=1 / RSI_FAST,
                min_periods=RSI_FAST,
                adjust=False
            )
            .mean()
        )

        rs6 = (
            avg_gain6
            /
            avg_loss6.replace(
                0,
                np.nan
            )
        )

        df["rsi6"] = (
            100 -
            (
                100 /
                (1 + rs6)
            )
        )


        avg_gain14 = (
            gain.ewm(
                alpha=1 / RSI_SLOW,
                min_periods=RSI_SLOW,
                adjust=False
            )
            .mean()
        )

        avg_loss14 = (
            loss.ewm(
                alpha=1 / RSI_SLOW,
                min_periods=RSI_SLOW,
                adjust=False
            )
            .mean()
        )

        rs14 = (
            avg_gain14
            /
            avg_loss14.replace(
                0,
                np.nan
            )
        )

        df["rsi14"] = (
            100 -
            (
                100 /
                (1 + rs14)
            )
        )


        # ----------------------------------------------------
        # MACD 12 / 26 / 9
        # ----------------------------------------------------

        ema12 = (
            df["close"]
            .ewm(
                span=12,
                adjust=False
            )
            .mean()
        )

        ema26 = (
            df["close"]
            .ewm(
                span=26,
                adjust=False
            )
            .mean()
        )

        df["macd_dif"] = (
            ema12 - ema26
        )

        df["macd_dea"] = (
            df["macd_dif"]
            .ewm(
                span=9,
                adjust=False
            )
            .mean()
        )

        df["macd_hist"] = (
            df["macd_dif"]
            -
            df["macd_dea"]
        )


        # ----------------------------------------------------
        # KDJ 9 / 3 / 3
        # ----------------------------------------------------

        low_min = (
            df["low"]
            .rolling(9)
            .min()
        )

        high_max = (
            df["high"]
            .rolling(9)
            .max()
        )

        rsv = (
            (
                df["close"]
                -
                low_min
            )
            /
            (
                high_max
                -
                low_min
            ).replace(
                0,
                np.nan
            )
            *
            100
        )

        df["kdj_k"] = (
            rsv
            .ewm(
                com=2,
                adjust=False
            )
            .mean()
        )

        df["kdj_d"] = (
            df["kdj_k"]
            .ewm(
                com=2,
                adjust=False
            )
            .mean()
        )

        df["kdj_j"] = (
            3 * df["kdj_k"]
            -
            2 * df["kdj_d"]
        )


        # ----------------------------------------------------
        # STOCH RSI
        # ----------------------------------------------------

        rsi = df["rsi14"]

        rsi_min = (
            rsi
            .rolling(14)
            .min()
        )

        rsi_max = (
            rsi
            .rolling(14)
            .max()
        )

        stochrsi = (
            (
                rsi -
                rsi_min
            )
            /
            (
                rsi_max -
                rsi_min
            ).replace(
                0,
                np.nan
            )
        )

        df["stochrsi_k"] = (
            stochrsi
            .rolling(3)
            .mean()
            *
            100
        )

        df["stochrsi_d"] = (
            df["stochrsi_k"]
            .rolling(3)
            .mean()
        )


        # ----------------------------------------------------
        # WILLIAMS %R
        # ----------------------------------------------------

        highest = (
            df["high"]
            .rolling(
                WILLIAMS_PERIOD
            )
            .max()
        )

        lowest = (
            df["low"]
            .rolling(
                WILLIAMS_PERIOD
            )
            .min()
        )

        df["williams_r"] = (
            -100
            *
            (
                highest -
                df["close"]
            )
            /
            (
                highest -
                lowest
            ).replace(
                0,
                np.nan
            )
        )


        # ----------------------------------------------------
        # ATR
        # ----------------------------------------------------

        high_low = (
            df["high"] -
            df["low"]
        )

        high_close = (
            df["high"] -
            df["close"].shift()
        ).abs()

        low_close = (
            df["low"] -
            df["close"].shift()
        ).abs()

        tr = pd.concat(
            [
                high_low,
                high_close,
                low_close
            ],
            axis=1
        ).max(
            axis=1
        )

        df["atr"] = (
            tr
            .ewm(
                alpha=1 / ATR_PERIOD,
                min_periods=ATR_PERIOD,
                adjust=False
            )
            .mean()
        )

        return df

    except Exception as e:

        logger.error(
            "İndikatör hesaplama hatası: %s",
            e
        )

        return None


# ============================================================
# SIGNALS
# ============================================================

SignalType = Literal[
    "LONG",
    "SHORT",
    None
]


def _is_red(row):

    return (
        row["close"] <
        row["open"]
    )


def _is_green(row):

    return (
        row["close"] >
        row["open"]
    )


def check_long_conditions(
    df: pd.DataFrame,
    idx: int
) -> bool:

    if (
        idx < 2
        or idx >= len(df)
    ):
        return False

    curr = df.iloc[idx]

    prev = df.iloc[idx - 1]

    return all([

        # EMA5
        (
            _is_red(curr)
            and
            curr["close"]
            <
            curr["ema5"]
        ),

        # KDJ
        (
            (
                70 <= prev["kdj_j"] <= 110
                or
                70 <= prev["kdj_k"] <= 110
            )

            and
            curr["kdj_j"]
            <
            prev["kdj_j"]

            and
            curr["kdj_k"]
            <
            prev["kdj_k"]

            and
            curr["kdj_d"]
            <
            prev["kdj_d"]
        ),

        # STOCH RSI
        (
            prev["stochrsi_k"]
            >= 80

            and
            curr["stochrsi_k"]
            <
            curr["stochrsi_d"]

            and
            prev["stochrsi_k"]
            >=
            prev["stochrsi_d"]
        ),

        # MACD
        (
            curr["macd_dif"]
            <
            prev["macd_dif"]

            and
            curr["macd_dea"]
            <
            prev["macd_dea"]

            and
            prev["macd_hist"]
            > 0
        ),

        # RSI
        (
            prev["rsi6"]
            >
            prev["rsi14"]

            and
            curr["rsi6"]
            <
            curr["rsi14"]

            and
            75 <=
            prev["rsi14"]
            <= 90
        ),

        # WILLIAMS
        (
            -16 <=
            prev["williams_r"]
            <= 0

            and
            curr["williams_r"]
            <
            prev["williams_r"]
        ),
    ])


def check_short_conditions(
    df: pd.DataFrame,
    idx: int
) -> bool:

    if (
        idx < 2
        or idx >= len(df)
    ):
        return False

    curr = df.iloc[idx]

    prev = df.iloc[idx - 1]

    return all([

        # EMA5
        (
            _is_green(curr)
            and
            curr["close"]
            >
            curr["ema5"]
        ),

        # KDJ
        (
            (
                0 <= prev["kdj_j"] <= 30
                or
                0 <= prev["kdj_k"] <= 30
            )

            and
            curr["kdj_j"]
            >
            prev["kdj_j"]

            and
            curr["kdj_k"]
            >
            prev["kdj_k"]

            and
            curr["kdj_d"]
            >
            prev["kdj_d"]
        ),

        # STOCH RSI
        (
            prev["stochrsi_k"]
            <= 20

            and
            curr["stochrsi_k"]
            >
            curr["stochrsi_d"]

            and
            prev["stochrsi_k"]
            <=
            prev["stochrsi_d"]
        ),

        # MACD
        (
            curr["macd_dif"]
            >
            prev["macd_dif"]

            and
            curr["macd_dea"]
            >
            prev["macd_dea"]

            and
            prev["macd_hist"]
            < 0
        ),

        # RSI
        (
            prev["rsi6"]
            <
            prev["rsi14"]

            and
            curr["rsi6"]
            >
            curr["rsi14"]

            and
            10 <=
            prev["rsi14"]
            <= 25
        ),

        # WILLIAMS
        (
            -100 <=
            prev["williams_r"]
            <= -84

            and
            curr["williams_r"]
            >
            prev["williams_r"]
        ),
    ])


def detect_signal(
    df: pd.DataFrame
) -> SignalType:

    if (
        df is None
        or
        len(df) < 30
    ):
        return None

    indices = list(
        range(
            len(df) - 1,
            max(
                len(df)
                - 1
                - SIGNAL_LOOKBACK
                - 1,
                1
            ),
            -1
        )
    )

    for idx in indices:

        if check_long_conditions(
            df,
            idx
        ):
            return "LONG"

        if check_short_conditions(
            df,
            idx
        ):
            return "SHORT"

    return None


def get_atr(df):

    if (
        df is None
        or
        "atr" not in df.columns
    ):
        return 0.0

    val = df["atr"].iloc[-1]

    return (
        float(val)
        if pd.notna(val)
        else 0.0
    )


def get_entry_price(df):

    return float(
        df["close"].iloc[-1]
    )


# ============================================================
# PAPER TRADER
# ============================================================

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

    open_time: datetime = field(
        default_factory=datetime.utcnow
    )

    status: str = "OPEN"

    close_price: Optional[float] = None

    pnl: float = 0.0


class PaperTrader:

    def __init__(self):

        self.positions = []

        self.closed_positions = []

        self.total_pnl = 0.0

        self.trade_count = 0


    def open_position(
        self,
        symbol,
        side,
        entry_price,
        atr
    ):

        if (
            atr <= 0
            or
            entry_price <= 0
        ):
            return None

        if any(
            p.symbol == symbol
            and
            p.status == "OPEN"
            for p in self.positions
        ):
            return None

        quantity = (
            NOTIONAL_USD /
            entry_price
        )

        if side == "LONG":

            tp = (
                entry_price
                +
                (2 * atr)
            )

            sl = (
                entry_price
                -
                atr
            )

        else:

            tp = (
                entry_price
                -
                (2 * atr)
            )

            sl = (
                entry_price
                +
                atr
            )

        pos = Position(
            symbol,
            side,
            entry_price,
            quantity,
            LEVERAGE,
            NOTIONAL_USD,
            MARGIN_USD,
            tp,
            sl,
            atr
        )

        self.positions.append(pos)

        self.trade_count += 1

        logger.info(
            "SANAL AÇILDI | %s %s | "
            "Giriş %.6f | TP %.6f | SL %.6f",
            side,
            symbol,
            entry_price,
            tp,
            sl
        )

        return pos


    def update_positions(
        self,
        prices: Dict[str, float]
    ):

        closed_now = []

        still_open = []

        for pos in self.positions:

            price = prices.get(
                pos.symbol
            )

            if price is None:

                still_open.append(pos)

                continue

            hit = False


            if pos.side == "LONG":

                if price >= pos.tp:

                    pos.status = (
                        "CLOSED_TP"
                    )

                    pos.close_price = (
                        pos.tp
                    )

                    pos.pnl = (
                        pos.tp
                        -
                        pos.entry_price
                    ) * pos.quantity

                    hit = True

                elif price <= pos.sl:

                    pos.status = (
                        "CLOSED_SL"
                    )

                    pos.close_price = (
                        pos.sl
                    )

                    pos.pnl = (
                        pos.sl
                        -
                        pos.entry_price
                    ) * pos.quantity

                    hit = True


            else:

                if price <= pos.tp:

                    pos.status = (
                        "CLOSED_TP"
                    )

                    pos.close_price = (
                        pos.tp
                    )

                    pos.pnl = (
                        pos.entry_price
                        -
                        pos.tp
                    ) * pos.quantity

                    hit = True

                elif price >= pos.sl:

                    pos.status = (
                        "CLOSED_SL"
                    )

                    pos.close_price = (
                        pos.sl
                    )

                    pos.pnl = (
                        pos.entry_price
                        -
                        pos.sl
                    ) * pos.quantity

                    hit = True


            if hit:

                self.total_pnl += pos.pnl

                self.closed_positions.append(
                    pos
                )

                closed_now.append(pos)

            else:

                still_open.append(pos)


        self.positions = still_open

        return closed_now


    def get_open_positions(self):

        return [
            p
            for p in self.positions
            if p.status == "OPEN"
        ]


    def get_stats(self):

        return {

            "open_positions":
                len(
                    self.get_open_positions()
                ),

            "total_trades":
                self.trade_count,

            "total_pnl":
                round(
                    self.total_pnl,
                    4
                ),

            "closed_count":
                len(
                    self.closed_positions
                ),
        }


# ============================================================
# TELEGRAM
# ============================================================

class TelegramNotifier:

    def __init__(self):

        self.enabled = bool(
            TELEGRAM_BOT_TOKEN
            and
            TELEGRAM_CHAT_ID
        )

        self.bot = (
            Bot(
                token=TELEGRAM_BOT_TOKEN
            )
            if self.enabled
            else None
        )

        logger.info(
            "Telegram: %s",
            (
                "AKTİF"
                if self.enabled
                else "KAPALI"
            )
        )


    async def send(
        self,
        text: str
    ):

        if (
            not self.enabled
            or
            not self.bot
        ):
            return

        try:

            await self.bot.send_message(

                chat_id=
                    TELEGRAM_CHAT_ID,

                text=text,

                parse_mode=
                    ParseMode.HTML,

                disable_web_page_preview=True,
            )

        except Exception as e:

            logger.error(
                "Telegram gönderim hatası: %s",
                e
            )


    async def send_startup(self):

        await self.send(

            "✅ <b>PRFB aktif</b>\n"

            f"Tarama: "
            f"{TIMEFRAME} / "
            f"{SCAN_INTERVAL_SECONDS} sn\n"

            f"Sanal işlem: "
            f"{NOTIONAL_USD} USDT | "
            f"{LEVERAGE}x\n"

            "🟡 PAPER MODE — "
            "gerçek emir gönderilmez."
        )


    async def send_new_position(
        self,
        pos: Position
    ):

        emoji = (
            "🟢"
            if pos.side == "LONG"
            else
            "🔴"
        )

        await self.send(

            f"{emoji} "
            f"<b>Yeni Sanal İşlem</b>\n\n"

            f"Sembol: "
            f"<code>{pos.symbol}</code>\n"

            f"Yön: "
            f"<b>{pos.side}</b>\n"

            f"Giriş: "
            f"<code>{pos.entry_price:.6f}</code>\n"

            f"TP: "
            f"<code>{pos.tp:.6f}</code>\n"

            f"SL: "
            f"<code>{pos.sl:.6f}</code>\n"

            f"ATR: "
            f"<code>{pos.atr:.6f}</code>\n"

            f"Nominal: "
            f"{pos.notional} USDT | "
            f"Kaldıraç: "
            f"{pos.leverage}x"
        )


    async def send_closed_position(
        self,
        pos: Position
    ):

        emoji = (
            "✅"
            if pos.pnl >= 0
            else
            "❌"
        )

        await self.send(

            f"{emoji} "
            f"<b>Pozisyon Kapandı</b>\n\n"

            f"Sembol: "
            f"<code>{pos.symbol}</code>\n"

            f"Yön: "
            f"{pos.side}\n"

            f"Giriş → Çıkış: "
            f"<code>"
            f"{pos.entry_price:.6f}"
            f"</code> → "
            f"<code>"
            f"{pos.close_price:.6f}"
            f"</code>\n"

            f"PnL: "
            f"<b>{pos.pnl:+.4f} USDT</b>\n"

            f"Durum: "
            f"{pos.status}"
        )


    async def send_hourly_report(
        self,
        scanned,
        new_trades,
        open_positions,
        total_pnl,
        total_trades
    ):

        lines = [

            f"📊 <b>Saatlik Rapor</b> – "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M')}",

            "",

            f"Toplam Tarama: "
            f"<b>{scanned}</b> çift",

            f"Bu saatte açılan: "
            f"<b>{new_trades}</b>",

            f"Aktif Pozisyon: "
            f"<b>{len(open_positions)}</b>",

            f"Toplam İşlem: "
            f"<b>{total_trades}</b>",

            f"Toplam P&amp;L (sanal): "
            f"<b>{total_pnl:+.4f} USDT</b>",

            "",
        ]


        if open_positions:

            lines.append(
                "<b>Açık İşlemler:</b>"
            )

            for i, p in enumerate(
                open_positions[:15],
                1
            ):

                lines.append(

                    f"{i}. "
                    f"<code>{p.symbol}</code> | "
                    f"{p.side} | "
                    f"Giriş "
                    f"{p.entry_price:.5f} | "
                    f"TP "
                    f"{p.tp:.5f} | "
                    f"SL "
                    f"{p.sl:.5f}"
                )


            if len(open_positions) > 15:

                lines.append(
                    f"... ve "
                    f"{len(open_positions)-15} "
                    f"tane daha"
                )

        else:

            lines.append(
                "Açık işlem yok."
            )


        await self.send(
            "\n".join(lines)
        )


# ============================================================
# MARKET SCANNER
# ============================================================

class MarketScanner:

    def __init__(
        self,
        exchange,
        trader,
        notifier,
        max_concurrent=MAX_CONCURRENT
    ):

        self.exchange = exchange

        self.trader = trader

        self.notifier = notifier

        self.semaphore = (
            asyncio.Semaphore(
                max_concurrent
            )
        )

        self.last_scan_count = 0

        self.hourly_new_trades = 0


    async def _scan_symbol(
        self,
        symbol
    ):

        async with self.semaphore:

            ohlcv = (
                await
                self.exchange.fetch_ohlcv(
                    symbol,
                    150
                )
            )

            if not ohlcv:

                return (
                    symbol,
                    None,
                    0.0,
                    0.0
                )


            df = calculate_indicators(
                ohlcv_to_df(ohlcv)
            )


            if df is None:

                return (
                    symbol,
                    None,
                    0.0,
                    0.0
                )


            # Fiyat her taramada döndürülüyor.
            # Böylece açık TP/SL pozisyonları
            # sinyal olmasa bile takip ediliyor.

            entry = get_entry_price(df)

            atr = get_atr(df)


            return (
                symbol,
                detect_signal(df),
                entry,
                atr
            )


    async def run_scan(self):

        symbols = self.exchange.symbols


        if not symbols:

            return {
                "scanned": 0,
                "signals": 0,
                "open":
                    len(
                        self.trader
                        .get_open_positions()
                    )
            }


        logger.info(
            "Tarama başlıyor... %d çift",
            len(symbols)
        )


        results = await asyncio.gather(

            *(
                self._scan_symbol(s)
                for s in symbols
            ),

            return_exceptions=True
        )


        signals_found = 0

        prices = {}


        for res in results:

            if isinstance(
                res,
                Exception
            ):
                continue


            (
                symbol,
                signal_type,
                entry,
                atr
            ) = res


            if entry > 0:

                prices[
                    symbol
                ] = entry


            if (
                signal_type
                and
                atr > 0
            ):

                pos = (
                    self.trader
                    .open_position(
                        symbol,
                        signal_type,
                        entry,
                        atr
                    )
                )


                if pos:

                    signals_found += 1

                    self.hourly_new_trades += 1

                    await (
                        self.notifier
                        .send_new_position(
                            pos
                        )
                    )


        closed = (
            self.trader
            .update_positions(
                prices
            )
        )


        for pos in closed:

            await (
                self.notifier
                .send_closed_position(
                    pos
                )
            )


        self.last_scan_count = (
            len(symbols)
        )


        logger.info(

            "Tarama bitti | "
            "Taranan: %d | "
            "Yeni sinyal: %d | "
            "Açık: %d",

            len(symbols),

            signals_found,

            len(
                self.trader
                .get_open_positions()
            )
        )


        return {

            "scanned":
                len(symbols),

            "signals":
                signals_found,

            "open":
                len(
                    self.trader
                    .get_open_positions()
                )
        }


    def reset_hourly_counter(self):

        n = (
            self.hourly_new_trades
        )

        self.hourly_new_trades = 0

        return n


# ============================================================
# MAIN BOT
# ============================================================

class PRFBBot:

    def __init__(self):

        self.exchange = (
            ExchangeClient()
        )

        self.trader = (
            PaperTrader()
        )

        self.notifier = (
            TelegramNotifier()
        )

        self.scanner = (
            MarketScanner(
                self.exchange,
                self.trader,
                self.notifier
            )
        )

        self.running = False

        self.scan_task = None

        self.report_task = None


    async def start(self):

        logger.info(
            "=" * 60
        )

        logger.info(
            "Peak Reversal Futures Bot başlatılıyor..."
        )

        logger.info(
            "PAPER MODE - "
            "Gerçek emir gönderilmez."
        )

        logger.info(
            "=" * 60
        )


        await (
            self.exchange
            .load_markets()
        )


        await (
            self.notifier
            .send_startup()
        )


        self.running = True


        self.scan_task = (
            asyncio.create_task(
                self.scan_loop()
            )
        )


        self.report_task = (
            asyncio.create_task(
                self.report_loop()
            )
        )


        await asyncio.gather(
            self.scan_task,
            self.report_task
        )


    async def scan_loop(self):

        while self.running:

            started = (
                time.monotonic()
            )


            try:

                await (
                    self.scanner
                    .run_scan()
                )


            except asyncio.CancelledError:

                raise


            except Exception as e:

                logger.exception(
                    "Tarama döngüsü hatası: %s",
                    e
                )


            elapsed = (
                time.monotonic()
                -
                started
            )


            await asyncio.sleep(
                max(
                    1,
                    SCAN_INTERVAL_SECONDS
                    -
                    elapsed
                )
            )


    async def report_loop(self):

        while self.running:

            await asyncio.sleep(
                REPORT_INTERVAL_MINUTES
                * 60
            )


            if not self.running:
                break


            try:

                stats = (
                    self.trader
                    .get_stats()
                )


                await (
                    self.notifier
                    .send_hourly_report(

                        scanned=
                            self.scanner
                            .last_scan_count,

                        new_trades=
                            self.scanner
                            .reset_hourly_counter(),

                        open_positions=
                            self.trader
                            .get_open_positions(),

                        total_pnl=
                            stats[
                                "total_pnl"
                            ],

                        total_trades=
                            stats[
                                "total_trades"
                            ],
                    )
                )


            except asyncio.CancelledError:

                raise


            except Exception as e:

                logger.exception(
                    "Rapor döngüsü hatası: %s",
                    e
                )


    async def stop(self):

        if (
            not self.running
            and
            self.scan_task is None
            and
            self.report_task is None
        ):

            await (
                self.exchange.close()
            )

            return


        self.running = False


        for task in (
            self.scan_task,
            self.report_task
        ):

            if (
                task
                and
                not task.done()
            ):

                task.cancel()


        await (
            self.exchange.close()
        )


        logger.info(
            "Bot kapatıldı."
        )


# ============================================================
# PROGRAM BAŞLANGICI
# ============================================================

async def main():

    bot = PRFBBot()

    loop = (
        asyncio.get_running_loop()
    )

    stopped = False


    def shutdown():

        nonlocal stopped

        if not stopped:

            stopped = True

            asyncio.create_task(
                bot.stop()
            )


    for sig in (
        signal.SIGINT,
        signal.SIGTERM
    ):

        try:

            loop.add_signal_handler(
                sig,
                shutdown
            )

        except (
            NotImplementedError,
            RuntimeError
        ):

            pass


    try:

        await bot.start()

    finally:

        await bot.stop()


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    try:

        asyncio.run(
            main()
        )

    except KeyboardInterrupt:

        print(
            "Bot durduruldu."
        )
