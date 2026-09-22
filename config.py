import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

LEVERAGE = int(os.getenv("LEVERAGE", "5"))
NOTIONAL_USD = float(os.getenv("NOTIONAL_USD", "10"))
MARGIN_USD = float(os.getenv("MARGIN_USD", "15"))

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
REPORT_INTERVAL_MINUTES = int(os.getenv("REPORT_INTERVAL_MINUTES", "60"))
TIMEFRAME = os.getenv("TIMEFRAME", "1m")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

