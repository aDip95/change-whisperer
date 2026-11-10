#!/usr/bin/env python3
"""
Web Monitor Bot — entry point.

Run with:  python monitor_bot.py
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_log_file = Path(os.getenv("LOG_FILE", "./data/bot.log"))
_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _log_file.parent.mkdir(parents=True, exist_ok=True)
    _handlers.append(
        RotatingFileHandler(
            _log_file,
            maxBytes=int(os.getenv("LOG_MAX_BYTES", "2000000")),
            backupCount=int(os.getenv("LOG_BACKUPS", "3")),
            encoding="utf-8",
        )
    )
except OSError as e:  # disco pieno / permessi: si continua con la sola console
    print(f"Log su file disattivato ({e})")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO"),
    handlers=_handlers,
)
# httpx logga a INFO l'URL completo delle richieste, token del bot compreso:
# su console passa, su file resterebbe scritto su disco. Silenziato.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Re-export public symbols so that existing tooling / tests can still do:
#   from monitor_bot import Config, WebScraper, PDFAnalyzer ...
from bot import (  # noqa: F401  (re-exports)
    CONV_CODICE,
    CONV_NAME,
    CONV_PDF_FLAG,
    CONV_SECTION_ID,
    CONV_SELECTOR,
    CONV_URL,
    Config,
    DiffAnalyzer,
    MonitoredSite,
    PDFAnalyzer,
    WebMonitorBot,
    WebScraper,
    _resolve_url_id,
    _url_id,
    confirm_remove_keyboard,
    escape_html,
    is_valid_url,
    site_action_keyboard,
    site_info_keyboard,
    site_list_keyboard,
    truncate,
)


def main():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token or token == "YOUR_BOT_TOKEN_HERE":
        logger.error("❌ TELEGRAM_BOT_TOKEN non configurato!")
        logger.error("   Crea un file .env basandoti su env.example")
        return

    chat_id = os.getenv("TELEGRAM_CHAT_ID", "0")
    if chat_id == "0":
        logger.error("❌ TELEGRAM_CHAT_ID non configurato!")
        logger.error("   Vedi env.example per le istruzioni")
        return

    config = Config()
    logger.info(f"Bot Token: {config.bot_token[:10]}...")
    logger.info(f"Chat ID: {config.chat_id}")
    logger.info(f"Intervallo: {config.check_interval}s")
    logger.info(f"Soglia: {config.change_threshold:.0%}")
    logger.info(f"GIF: {config.gif_path} ({'on' if config.gif_enabled else 'off'} di default)")
    logger.info(f"Log: {config.log_file} — storico cambiamenti: {config.change_log_file}")
    if config.daily_report_enabled:
        logger.info(f"Report giornaliero: {config.daily_report_hour}:00")
    else:
        logger.info("Report giornaliero: disattivato")

    bot = WebMonitorBot(config)
    bot.run()


if __name__ == "__main__":
    main()
