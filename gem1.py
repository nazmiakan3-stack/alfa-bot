#!/usr/bin/env python3
"""
Peak Reversal Futures Bot (PRFB)
Ana giriş noktası

Contabo / VPS / Termius uyumlu
GitHub'a yüklenebilir temiz yapı
"""
import asyncio
import signal
import sys
from datetime import datetime
from config import SCAN_INTERVAL_SECONDS, REPORT_INTERVAL_MINUTES
from bot.exchange import ExchangeClient
from bot.paper_trader import PaperTrader
from bot.telegram_bot import TelegramNotifier
from bot.scanner import MarketScanner
from utils.logger import setup_logger

logger = setup_logger("Main")


class PRFBBot:
    def __init__(self):
        self.exchange = ExchangeClient()
        self.trader = PaperTrader()
        self.notifier = TelegramNotifier()
        self.scanner = MarketScanner(self.exchange, self.trader, self.notifier)
        self.running = False
        self._scan_task = None
        self._report_task = None

    async def start(self):
        logger.info("=" * 50)
        logger.info("Peak Reversal Futures Bot başlatılıyor...")
        logger.info("=" * 50)

        await self.exchange.load_markets()
        await self.notifier.send_startup()

        self.running = True

        # İki paralel görev: tarama + saatlik rapor
        self._scan_task = asyncio.create_task(self._scan_loop())
        self._report_task = asyncio.create_task(self._report_loop())

        logger.info("Bot çalışıyor. Durdurmak için Ctrl+C")

        try:
            await asyncio.gather(self._scan_task, self._report_task)
        except asyncio.CancelledError:
            logger.info("Görevler iptal edildi.")

    async def _scan_loop(self):
        while self.running:
            try:
                await self.scanner.run_scan()
            except Exception as e:
                logger.error(f"Tarama döngüsü hatası: {e}", exc_info=True)

            await asyncio.sleep(SCAN_INTERVAL_SECONDS)

    async def _report_loop(self):
        # İlk raporu 1 saat sonra gönder
        await asyncio.sleep(REPORT_INTERVAL_MINUTES * 60)

        while self.running:
            try:
                new_trades = self.scanner.reset_hourly_counter()
                stats = self.trader.get_stats()
                open_pos = self.trader.get_open_positions()

                await self.notifier.send_hourly_report(
                    scanned=self.scanner.last_scan_count,
                    new_trades=new_trades,
                    open_positions=open_pos,
                    total_pnl=stats["total_pnl"],
                    total_trades=stats["total_trades"]
                )
            except Exception as e:
                logger.error(f"Rapor hatası: {e}", exc_info=True)

            await asyncio.sleep(REPORT_INTERVAL_MINUTES * 60)

    async def stop(self):
        logger.info("Bot durduruluyor...")
        self.running = False
        if self._scan_task:
            self._scan_task.cancel()
        if self._report_task:
            self._report_task.cancel()
        await self.exchange.close()
        logger.info("Bot kapatıldı.")


async def main():
    bot = PRFBBot()

    loop = asyncio.get_running_loop()

    def shutdown():
        logger.info("Kapatma sinyali alındı...")
        asyncio.create_task(bot.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown)
        except NotImplementedError:
            # Windows uyumluluğu
            pass

    try:
        await bot.start()
    except KeyboardInterrupt:
        await bot.stop()
    finally:
        await bot.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot kullanıcı tarafından durduruldu.")
        sys.exit(0)
