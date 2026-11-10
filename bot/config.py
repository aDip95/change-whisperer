from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class Config:
    """Bot configuration from environment variables."""

    bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id: int = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
    # Oltre a TELEGRAM_CHAT_ID, utenti che possono comandare il bot (es. in privato).
    # ponytail: allowlist piatta, isolamento per utente in codex/public-release-hardening
    allowed_user_ids: frozenset = frozenset(
        int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()
    )
    timezone: str = os.getenv("TIMEZONE", "Europe/Rome")
    check_interval: int = int(os.getenv("CHECK_INTERVAL", "60"))
    gif_path: str = os.getenv("GIF_PATH", "./data/giphy.gif")
    # ponytail: off di default — si riattiva a runtime con /gif (stato in _settings)
    gif_enabled: bool = os.getenv("GIF_ENABLED", "false").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    sites_file: str = os.getenv("SITES_FILE", "./monitored_sites.json")
    # Contenuto identico già notificato entro questa finestra -> niente secondo
    # avviso. Risposta esatta a "ripete roba già inviata" (vedi docs/esperimento-poorjev).
    dedup_window_hours: float = float(os.getenv("DEDUP_WINDOW_HOURS", "6"))
    change_threshold: float = float(os.getenv("CHANGE_THRESHOLD", "0.01"))
    # /add URL --whole: pagine per scansione e ogni quanto rifarla (gli altri siti restano a CHECK_INTERVAL)
    whole_site_max_pages: int = int(os.getenv("WHOLE_SITE_MAX_PAGES", "100"))
    whole_site_interval: int = int(os.getenv("WHOLE_SITE_INTERVAL", "900"))
    daily_report_enabled: bool = os.getenv("DAILY_REPORT_ENABLED", "true").lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    daily_report_hour: int = int(os.getenv("DAILY_REPORT_HOUR", "8"))
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
    log_file: str = os.getenv("LOG_FILE", "./data/bot.log")
    change_log_file: str = os.getenv("CHANGE_LOG_FILE", "./data/changes.jsonl")
    log_max_bytes: int = int(os.getenv("LOG_MAX_BYTES", "2000000"))
    log_backups: int = int(os.getenv("LOG_BACKUPS", "3"))
    request_timeout: int = 30
    max_retries: int = 3
    max_diff_length: int = 3000
