from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.utils import truncate

if TYPE_CHECKING:
    from bot.models import MonitoredSite


def _site_id(key: str) -> str:
    """Short 8-char ID from site key for Telegram callback_data (max 64 bytes)."""
    return hashlib.md5(key.encode()).hexdigest()[:8]


def _resolve_site_id(sites: dict, short_id: str) -> str | None:
    """Resolve a short ID back to a full site key."""
    for key in sites:
        if _site_id(key) == short_id:
            return key
    return None


# Back-compat aliases (older code paths may import these names)
_url_id = _site_id
_resolve_url_id = _resolve_site_id


def site_action_keyboard(key: str, url: str | None = None) -> InlineKeyboardMarkup:
    sid = _site_id(key)
    open_url = url or key.split("#", 1)[0]
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Ricontrolla", callback_data=f"recheck|{sid}"),
            InlineKeyboardButton("⏸ Pausa", callback_data=f"pause|{sid}"),
        ],
        [
            InlineKeyboardButton("🗑 Rimuovi", callback_data=f"remove|{sid}"),
            InlineKeyboardButton("🌐 Apri sito", url=open_url),
        ],
    ])


# ponytail: cap fisso + riga indicatore; paginazione solo se si superano davvero 30 siti
MAX_LIST_BUTTONS = 30


def site_list_keyboard(sites: dict) -> InlineKeyboardMarkup:
    buttons = []
    for key, site in list(sites.items())[:MAX_LIST_BUTTONS]:
        status = "⏸" if site.paused else "✅"
        pdf_flag = "📄" if site.pdf_analysis else ""
        section_flag = f" ⟦{site.section_id}⟧" if site.section_id else ""
        buttons.append([
            InlineKeyboardButton(
                f"{status}{pdf_flag} {truncate(site.name, 22)}{section_flag}",
                callback_data=f"info|{_site_id(key)}",
            )
        ])
    hidden = len(sites) - MAX_LIST_BUTTONS
    if hidden > 0:
        buttons.append([
            InlineKeyboardButton(
                f"… e altri {hidden} siti (gestiscili con /pause /remove URL)",
                callback_data="noop",
            )
        ])
    if not buttons:
        buttons.append([InlineKeyboardButton("➕ Nessun sito — Usa /add", callback_data="noop")])
    return InlineKeyboardMarkup(buttons)


def confirm_remove_keyboard(key: str) -> InlineKeyboardMarkup:
    sid = _site_id(key)
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Sì, rimuovi", callback_data=f"confirm_remove|{sid}"),
            InlineKeyboardButton("❌ Annulla", callback_data="cancel"),
        ]
    ])


def site_info_keyboard(key: str, paused: bool, url: str | None = None) -> InlineKeyboardMarkup:
    sid = _site_id(key)
    open_url = url or key.split("#", 1)[0]
    pause_btn = (
        InlineKeyboardButton("▶️ Riprendi", callback_data=f"resume|{sid}")
        if paused
        else InlineKeyboardButton("⏸ Pausa", callback_data=f"pause|{sid}")
    )
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 Controlla ora", callback_data=f"recheck|{sid}"),
            pause_btn,
        ],
        [
            InlineKeyboardButton("🗑 Rimuovi", callback_data=f"remove|{sid}"),
            InlineKeyboardButton("🌐 Apri", url=open_url),
        ],
        [InlineKeyboardButton("✏️ Rinomina", callback_data=f"rename|{sid}")],
        [InlineKeyboardButton("« Torna alla lista", callback_data="back_list")],
    ])
