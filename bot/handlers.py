from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import Counter
from logging.handlers import RotatingFileHandler
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from bot.config import Config
from bot.diff import NOISE_DEFAULTS, DiffAnalyzer, strip_noise
from bot.keyboards import (
    _resolve_site_id,
    _site_id,
    confirm_remove_keyboard,
    site_action_keyboard,
    site_info_keyboard,
    site_list_keyboard,
)
from bot.models import MonitoredSite
from bot.pdf import PDFAnalyzer
from bot.scraper import WebScraper
from bot.utils import escape_html, is_valid_url, truncate

logger = logging.getLogger(__name__)


def _setup_change_log(config: Config) -> logging.Logger:
    """Local JSONL history of every detected change — storico per debug sul Raspberry."""
    log = logging.getLogger("bot.changes")
    log.propagate = False
    if log.handlers:
        return log
    try:
        path = Path(config.change_log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=config.log_max_bytes, backupCount=config.log_backups,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    except Exception as e:
        logger.warning(f"Storico cambiamenti disattivato ({e})")
    return log


# ConversationHandler states for /addpdf
(
    CONV_URL,
    CONV_SELECTOR,
    CONV_SECTION_ID,
    CONV_NAME,
    CONV_PDF_FLAG,
    CONV_CODICE,
    CONV_EXPECT,
) = range(7)


class WebMonitorBot:
    """Telegram bot that monitors web pages for changes."""

    BOT_COMMANDS = [
        BotCommand("start", "Avvia il bot e mostra i comandi"),
        BotCommand("add", "Aggiungi sito — /add URL [nome] [css] [--whole]"),
        BotCommand("addpdf", "Aggiungi sito con analisi PDF (guidato)"),
        BotCommand("remove", "Rimuovi sito — /remove URL"),
        BotCommand("list", "Mostra siti monitorati"),
        BotCommand("check", "Forza controllo immediato"),
        BotCommand("status", "Stato del bot"),
        BotCommand("test", "Invia notifica di test"),
        BotCommand("pause", "Pausa sito — /pause URL"),
        BotCommand("resume", "Riprendi sito — /resume URL"),
        BotCommand("rename", "Rinomina sito — /rename KEY Nuovo Nome"),
        BotCommand("threshold", "Soglia per-sito — /threshold URL 0.001"),
        BotCommand("browser", "Attiva/disattiva Playwright — /browser URL"),
        BotCommand("fullpage", "Monitora anche tutta la pagina — /fullpage URL"),
        BotCommand("selector", "Limita a una sezione — /selector KEY <css>"),
        BotCommand("code", "Codice da cercare nei PDF — /code KEY <codice>"),
        BotCommand("expect", "Posizione/nome attesi — /expect KEY 3 [nome]"),
        BotCommand("gif", "Attiva/disattiva GIF nelle notifiche"),
        BotCommand("ignore", "Righe da ignorare — /ignore testo"),
        BotCommand("unignore", "Rimuovi filtro — /unignore N"),
        BotCommand("report", "Report completo di tutti i siti"),
        BotCommand("help", "Guida dettagliata"),
    ]

    def __init__(self, config: Config):
        self.config = config
        self.scraper = WebScraper(config)
        self.sites: dict[str, MonitoredSite] = {}
        self.app: Optional[Application] = None
        self.gif_enabled: bool = config.gif_enabled
        self.noise_patterns: list[str] = list(NOISE_DEFAULTS)
        self._changelog = _setup_change_log(config)
        self._load_sites()

    # ──────────────────────────────────────────────────────────────────────
    # Persistence
    # ──────────────────────────────────────────────────────────────────────

    def _save_sites(self):
        try:
            data = {
                "_settings": {
                    "gif_enabled": self.gif_enabled,
                    "noise_patterns": self.noise_patterns,
                }
            }
            data.update({site.key: site.to_dict() for site in self.sites.values()})
            # Scrittura atomica: un blackout del Raspberry a metà non tronca lo stato.
            tmp = f"{self.config.sites_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.config.sites_file)
            logger.info(f"Salvati {len(data)} siti in {self.config.sites_file}")
        except Exception as e:
            logger.error(f"Errore salvataggio siti: {e}")

    def _load_sites(self):
        try:
            path = Path(self.config.sites_file)
            if not path.exists():
                logger.info("Nessun file siti trovato, inizio con lista vuota")
                return
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            settings = data.pop("_settings", {})
            self.gif_enabled = settings.get("gif_enabled", self.config.gif_enabled)
            self.noise_patterns = settings.get("noise_patterns", list(NOISE_DEFAULTS))
            for _stored_key, site_data in data.items():
                try:
                    site = MonitoredSite.from_dict(site_data)
                except Exception as e:
                    logger.error(f"Voce sito non valida ignorata ({_stored_key}): {e}")
                    continue
                self.sites[site.key] = site
            logger.info(f"Caricati {len(self.sites)} siti da {self.config.sites_file}")
        except Exception as e:
            logger.error(f"Errore caricamento siti: {e}")

    def _clean(self, content: Optional[str]) -> Optional[str]:
        """Strip flapping boilerplate (search box, separators) before hash/diff."""
        return strip_noise(content, self.noise_patterns)

    MAX_NOTIFIED_HASHES = 20

    def _recently_notified(self, site: MonitoredSite, content_hash: str) -> bool:
        """True se questo identico contenuto è già stato notificato di recente.

        Il caso reale: una pagina cambia, torna indietro e ricambia. Il contenuto
        è lo stesso di un'ora fa, l'avviso sarebbe una ripetizione.
        """
        window = self.config.dedup_window_hours
        if window <= 0:
            return False
        stamp = site.notified_hashes.get(content_hash)
        if not stamp:
            return False
        try:
            age_h = (datetime.now() - datetime.fromisoformat(stamp)).total_seconds() / 3600
        except (ValueError, TypeError):
            return False
        return age_h < window

    def _remember_notified(self, site: MonitoredSite, content_hash: str) -> None:
        site.notified_hashes[content_hash] = datetime.now().isoformat()
        if len(site.notified_hashes) > self.MAX_NOTIFIED_HASHES:
            oldest = sorted(site.notified_hashes.items(), key=lambda kv: kv[1])
            for h, _ in oldest[: -self.MAX_NOTIFIED_HASHES]:
                site.notified_hashes.pop(h, None)

    def _log_change(self, site: MonitoredSite, event: str, **fields):
        """Append one JSON line to the local change history."""
        try:
            self._changelog.info(json.dumps({
                "ts": datetime.now().isoformat(timespec="seconds"),
                "event": event,
                "site": site.name,
                "key": site.key,
                **fields,
            }, ensure_ascii=False))
        except Exception as e:
            logger.debug(f"Storico non scritto: {e}")

    @staticmethod
    def _parse_key(arg: str) -> tuple[str, Optional[str]]:
        """Split user-provided key 'url[#section_id]' into (url, section_id)."""
        if "#" in arg:
            url, section_id = arg.split("#", 1)
            return url, section_id or None
        return arg, None

    # ──────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────

    @staticmethod
    def _get_msg(update: Update):
        return (
            update.message
            or update.edited_message
            or update.channel_post
            or update.edited_channel_post
        )

    def _is_authorized(self, update: Update) -> bool:
        chat, user = update.effective_chat, update.effective_user
        return bool(
            (chat and chat.id == self.config.chat_id)
            or (user and user.id in self.config.allowed_user_ids)
        )

    async def guard_update(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Group -1: stops every update not from TELEGRAM_CHAT_ID / ALLOWED_USER_IDS."""
        chat, user = update.effective_chat, update.effective_user
        if self._is_authorized(update):
            msg = self._get_msg(update)
            what = (msg.text or "")[:40] if msg else ("callback" if update.callback_query else "altro")
            logger.info(f"Update da chat={chat.id if chat else None} ({chat.type if chat else '?'}): {what}")
            return
        logger.warning(
            f"Update non autorizzato ignorato: chat={chat.id if chat else None} "
            f"user={user.id if user else None}"
        )
        raise ApplicationHandlerStop

    async def _bot(self) -> Bot:
        return self.app.bot

    async def _ensure_job(self, context: ContextTypes.DEFAULT_TYPE):
        if context.job_queue:
            jobs = context.job_queue.get_jobs_by_name("check_sites")
            if not jobs:
                context.job_queue.run_repeating(
                    self._job_check_all,
                    interval=self.config.check_interval,
                    first=self.config.check_interval,
                    name="check_sites",
                )
                logger.info("Job di monitoraggio avviato")

    # ──────────────────────────────────────────────────────────────────────
    # PDF analysis
    # ──────────────────────────────────────────────────────────────────────

    async def _analyze_pdfs(self, site: MonitoredSite, html: str):
        """
        Download the PDFs of the monitored section and locate the configured code.

        For a graduatoria (file name containing GRADUATORIA) the anchor is the CODE
        — always present, always the user's row. On each republish (new URL) the bot
        reports whether the name at that row is published or still redacted (omissata).
        Other PDFs keep the plain "code found" behaviour.
        Each PDF URL is processed once (tracked in site.pdf_results).
        """
        if not site.pdf_analysis or not site.pdf_search_value:
            return

        pdf_urls = WebScraper.extract_pdf_links(html, site.url, site.css_selector)
        if not pdf_urls:
            logger.info(f"Nessun PDF trovato per {site.name}")
            return

        events: list[dict] = []
        expected_name = " ".join((site.pdf_expected_name or "").split()).upper()

        for pdf_url in pdf_urls:
            if pdf_url in site.pdf_results:
                continue

            logger.info(f"Analisi PDF: {pdf_url}")
            pdf_bytes = await self.scraper.download_bytes(pdf_url)
            if pdf_bytes is None:
                logger.warning(f"Download PDF fallito: {pdf_url}")
                site.pdf_results[pdf_url] = "download_failed"
                continue

            # Solo la graduatoria porta l'analisi nome/posizione; gli altri PDF
            # (esiti, buste, elenchi) restano alla semplice ricerca del codice.
            # ponytail: match sul nome file; se un concorso usa un altro termine, aggiungilo qui.
            is_graduatoria = "GRADUATORIA" in PDFAnalyzer.doc_name(pdf_url).upper()

            if is_graduatoria:
                row = PDFAnalyzer.find_code_row(pdf_bytes, site.pdf_search_value)
                if row is None:
                    site.pdf_results[pdf_url] = "code_absent"
                    logger.warning(f"Codice {site.pdf_search_value} assente in graduatoria {pdf_url}")
                    events.append({"url": pdf_url, "kind": "absent"})
                else:
                    state = "named" if row["name_shown"] else "omissata"
                    site.pdf_results[pdf_url] = f"found:{row['position']}:{state}"
                    logger.info(
                        f"Codice {site.pdf_search_value} in graduatoria pos {row['position']} "
                        f"({state}) — {pdf_url}"
                    )
                    events.append({"url": pdf_url, "kind": state, "row": row})
            else:
                result = PDFAnalyzer.search_table(pdf_bytes, site.pdf_search_value)
                if result:
                    site.pdf_results[pdf_url] = f"found:{result['position']}"
                    events.append({"url": pdf_url, "kind": "found", "row": result})
                    logger.info(
                        f"Codice {site.pdf_search_value} trovato pos {result['position']} in {pdf_url}"
                    )
                else:
                    site.pdf_results[pdf_url] = "not_found"
                    logger.info(f"Codice {site.pdf_search_value} non trovato in {pdf_url}")

        if events:
            bot = await self._bot()
            for ev in events:
                await bot.send_message(
                    chat_id=self.config.chat_id,
                    text=self._pdf_event_text(site, ev, expected_name),
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )

        self._save_sites()

    def _pdf_event_text(self, site: MonitoredSite, ev: dict, expected_name_upper: str) -> str:
        """Build the Telegram message for one PDF analysis event."""
        doc = escape_html(PDFAnalyzer.doc_name(ev["url"]))
        stamp = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        head = (
            f"📍 <b>{escape_html(site.name)}</b>\n"
            f"📄 Documento: <a href=\"{ev['url']}\">{doc}</a>\n"
            f"🔑 Codice: <code>{escape_html(site.pdf_search_value)}</code>\n"
        )
        kind = ev["kind"]
        row = ev.get("row")
        position_note = ""
        if (
            row
            and site.pdf_expected_position
            and str(row["position"]).strip() != str(site.pdf_expected_position).strip()
        ):
            position_note = (
                f"⚠️ <b>Posizione diversa da quella attesa</b> "
                f"({escape_html(site.pdf_expected_position)}).\n"
            )

        if kind == "omissata":
            return (
                f"📋 <b>Graduatoria aggiornata</b>\n\n{head}"
                f"🏆 Posizione: <b>{escape_html(row['position'])}</b>\n"
                f"{position_note}"
                f"🙈 Nome <b>non ancora pubblicato</b> (graduatoria omissata).\n\n"
                f"<i>Ti avviso di nuovo quando pubblicheranno una versione col nome.</i>\n\n"
                f"🕐 {stamp}"
            )
        if kind == "named":
            shown = escape_html(row["name"])
            if expected_name_upper and expected_name_upper not in row["name"].upper():
                match_note = (
                    f"⚠️ <b>Nome diverso da quello atteso</b> "
                    f"({escape_html(site.pdf_expected_name)}).\n"
                )
            else:
                match_note = "✅ Nome atteso confermato.\n" if expected_name_upper else ""
            return (
                f"🎉 <b>Graduatoria aggiornata — NOME PUBBLICATO!</b>\n\n{head}"
                f"🏆 Posizione: <b>{escape_html(row['position'])}</b>\n"
                f"{position_note}"
                f"👤 Nome in graduatoria: <b>{shown}</b>\n"
                f"{match_note}\n"
                f"🕐 {stamp}"
            )
        if kind == "absent":
            return (
                f"⚠️ <b>Graduatoria aggiornata — codice non trovato!</b>\n\n{head}\n"
                f"Il tuo codice <b>non compare</b> nella nuova graduatoria.\n"
                f"<i>Verifica subito il documento.</i>\n\n"
                f"🕐 {stamp}"
            )
        # kind == "found" (PDF generico non-graduatoria)
        return (
            f"🎯 <b>Codice trovato in un documento!</b>\n\n{head}"
            f"🏆 Posizione: <b>{escape_html(row['position'])}</b>\n\n"
            f"{position_note}"
            f"🕐 {stamp}"
        )

    # ──────────────────────────────────────────────────────────────────────
    # Site checking
    # ──────────────────────────────────────────────────────────────────────

    async def _check_site(self, site: MonitoredSite) -> Optional[str]:
        """Check a site for changes. Returns 'sent' if a notification was sent."""
        if site.paused:
            return None

        logger.info(f"Controllo {site.name}: {site.url}")
        html = await self.scraper.fetch_page(site.url, use_browser=site.use_browser)

        if html is None:
            site.error_count += 1
            logger.error(
                f"Impossibile scaricare {site.url} (errori consecutivi: {site.error_count})"
            )
            if site.error_count >= 3 and not site.error_notified:
                site.error_notified = True
                self._save_sites()
                bot = await self._bot()
                await bot.send_message(
                    chat_id=self.config.chat_id,
                    text=(
                        f"⚠️ <b>Sito non raggiungibile</b>\n\n"
                        f"📍 <b>{escape_html(site.name)}</b>\n"
                        f"🔗 {site.url}\n"
                        f"❌ {site.error_count} tentativi falliti consecutivi.\n"
                        f"Il sito potrebbe essere offline o in manutenzione.\n\n"
                        f"<i>Continuo a monitorare — ti avviserò quando tornerà online.</i>"
                    ),
                    parse_mode="HTML",
                    reply_markup=site_action_keyboard(site.key, site.url),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                return "sent"
            return None

        was_in_error = site.error_notified
        site.error_count = 0
        site.error_notified = False
        site.last_check = datetime.now().isoformat()

        if site.whole_site:
            return await self._check_whole_site(site, html)

        # Full-page check: runs when css_selector is active and monitor_full_page is enabled.
        # Reuses the already-fetched HTML — no extra HTTP request.
        if site.monitor_full_page and site.css_selector:
            full_content = self._clean(self.scraper.extract_content(html, None, base_url=site.url))
            site.previous_full_content = self._clean(site.previous_full_content)
            if site.previous_full_content is not None and DiffAnalyzer.has_changed(
                site.previous_full_content, full_content
            ):
                full_ratio = DiffAnalyzer.get_change_ratio(site.previous_full_content, full_content)
                effective_threshold = (
                    site.threshold if site.threshold is not None else self.config.change_threshold
                )
                self._log_change(
                    site, "fullpage_change", ratio=round(full_ratio, 4),
                    threshold=effective_threshold, notified=full_ratio >= effective_threshold,
                )
                if full_ratio >= effective_threshold:
                    full_summary = DiffAnalyzer.summarize_changes(
                        site.previous_full_content, full_content, self.config.max_diff_length
                    )
                    bot = await self._bot()
                    await bot.send_message(
                        chat_id=self.config.chat_id,
                        text=(
                            f"🌐 <b>Sito aggiornato!</b>\n\n"
                            f"📍 <b>{escape_html(site.name)}</b>\n"
                            f"🔗 {site.url}\n"
                            f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
                            f"{full_summary}"
                        ),
                        parse_mode="HTML",
                        reply_markup=site_action_keyboard(site.key, site.url),
                        disable_notification=full_ratio < 0.05,
                        link_preview_options=LinkPreviewOptions(is_disabled=True),
                    )
            site.previous_full_content = full_content
            site.previous_full_hash = DiffAnalyzer.compute_hash(full_content)

        current_content = self._clean(
            self.scraper.extract_content(html, site.css_selector, base_url=site.url)
        )
        # Legacy stored content may still carry noise lines: normalize both sides,
        # altrimenti il primo confronto dopo un /ignore genererebbe un falso allarme.
        site.previous_content = self._clean(site.previous_content)

        if site.pdf_analysis:
            await self._analyze_pdfs(site, html)

        # Fallback: previous_content absent (old JSON) but hash stored — use hash to detect changes
        if site.previous_content is None and site.previous_hash is not None:
            current_hash = DiffAnalyzer.compute_hash(current_content)
            changed = current_hash != site.previous_hash
            site.previous_content = current_content
            site.previous_hash = current_hash
            site.last_check = datetime.now().isoformat()
            self._save_sites()
            if changed:
                logger.info(f"Cambiamento rilevato (hash fallback) per {site.name}")
                self._log_change(site, "hash_fallback", notified=True)
                bot = await self._bot()
                await bot.send_message(
                    chat_id=self.config.chat_id,
                    text=(
                        f"🔔 <b>Aggiornamento rilevato!</b>\n\n"
                        f"📍 <b>{escape_html(site.name)}</b>\n"
                        f"🔗 {site.url}\n"
                        f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
                        f"⚠️ <i>Cambiamento avvenuto durante il riavvio del bot.\n"
                        f"Visita il sito per i dettagli.</i>"
                    ),
                    parse_mode="HTML",
                    reply_markup=site_action_keyboard(site.key, site.url),
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                return "sent"
            return None

        if not DiffAnalyzer.has_changed(site.previous_content, current_content):
            logger.info(f"Nessun cambiamento per {site.name}")
            site.previous_content = current_content
            if was_in_error:
                bot = await self._bot()
                await bot.send_message(
                    chat_id=self.config.chat_id,
                    text=(
                        f"✅ <b>Sito tornato online!</b>\n\n"
                        f"📍 <b>{escape_html(site.name)}</b>\n"
                        f"🔗 {site.url}\n"
                        f"📄 Contenuto invariato rispetto a prima del disservizio."
                    ),
                    parse_mode="HTML",
                    disable_notification=True,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                self._save_sites()
            return None

        if site.previous_content is not None:
            change_ratio = DiffAnalyzer.get_change_ratio(site.previous_content, current_content)
            effective_threshold = (
                site.threshold if site.threshold is not None else self.config.change_threshold
            )

            if change_ratio < effective_threshold:
                logger.info(
                    f"Cambiamento ignorato per {site.name} "
                    f"(change ratio {change_ratio:.2%} < soglia {effective_threshold:.2%})"
                )
                self._log_change(
                    site, "below_threshold", ratio=round(change_ratio, 4),
                    threshold=effective_threshold, notified=False,
                )
                site.previous_content = current_content
                site.previous_hash = DiffAnalyzer.compute_hash(current_content)
                return None

            current_hash = DiffAnalyzer.compute_hash(current_content)
            if self._recently_notified(site, current_hash):
                logger.info(
                    f"Contenuto già notificato nelle ultime "
                    f"{self.config.dedup_window_hours:g}h, silenzio per {site.name}"
                )
                self._log_change(
                    site, "duplicate_recent", ratio=round(change_ratio, 4), notified=False,
                )
                site.previous_content = current_content
                site.previous_hash = current_hash
                self._save_sites()
                return None

            summary = DiffAnalyzer.summarize_changes(
                site.previous_content, current_content, self.config.max_diff_length
            )
            documents = self.scraper.extract_documents(html, site.url)
            recovery_banner = "✅ <b>Sito tornato online!</b>\n\n" if was_in_error else ""

            section_line = (
                f"🧩 Sezione: <code>{escape_html(site.section_id)}</code>\n"
                if site.section_id
                else ""
            )
            change_kind = DiffAnalyzer.classify_change(
                site.previous_content, current_content, change_ratio
            )
            if change_kind == "wiped":
                header = "🗑 <b>Contenuto pagina svuotato!</b>"
                footer = (
                    "\n\n⚠️ <i>La pagina è stata svuotata — probabile riscrittura "
                    "in corso. Ti avviso appena il contenuto torna.</i>"
                )
            elif change_kind == "rewritten":
                header = "♻️ <b>Pagina interamente riscritta!</b>"
                footer = (
                    "\n\n⚠️ <i>L'intero contenuto è stato cancellato e ripubblicato: "
                    "controlla la pagina per le novità reali.</i>"
                )
            else:
                header = "🔔 <b>Aggiornamento rilevato!</b>"
                footer = ""
            message = (
                f"{recovery_banner}"
                f"{header}\n\n"
                f"📍 <b>{escape_html(site.name)}</b>\n"
                f"🔗 {site.url}\n"
                f"{section_line}"
                f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
                f"{summary}{footer}"
            )

            bot = await self._bot()
            is_minor = change_ratio < 0.05
            await bot.send_message(
                chat_id=self.config.chat_id,
                text=message,
                parse_mode="HTML",
                reply_markup=site_action_keyboard(site.key, site.url),
                disable_notification=is_minor,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

            # GIF only for a normal update: wipe/rewrite gets its own sober notification
            if change_kind == "update" and not is_minor and self.gif_enabled and self.config.gif_path:
                gif_path = Path(self.config.gif_path)
                if gif_path.is_file():
                    try:
                        with open(gif_path, "rb") as f:
                            await bot.send_animation(
                                chat_id=self.config.chat_id,
                                animation=f,
                                caption="🎉 Aggiornamento rilevato!",
                                disable_notification=True,
                            )
                    except Exception as e:
                        logger.warning(f"Errore invio GIF: {e}")

            if documents:
                docs_text = "\n".join(
                    f"📄 <a href=\"{d['url']}\">{escape_html(d['text'])}</a> ({d['type']})"
                    for d in documents[:10]
                )
                await bot.send_message(
                    chat_id=self.config.chat_id,
                    text=f"📎 <b>Documenti trovati:</b>\n\n{docs_text}",
                    parse_mode="HTML",
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                    disable_notification=True,
                )

            self._remember_notified(site, current_hash)
            self._log_change(
                site, "notified", kind=change_kind, ratio=round(change_ratio, 4),
                threshold=effective_threshold, notified=True,
                summary=summary[:800],
            )
            logger.info(f"Notifica inviata per {site.name}")

        site.previous_content = current_content
        site.previous_hash = DiffAnalyzer.compute_hash(current_content)
        self._save_sites()
        return "sent"

    async def _check_whole_site(self, site: MonitoredSite, root_html: str) -> Optional[str]:
        """/add --whole: crawl the pages under site.url, one message for new/changed/removed."""
        # ponytail: BFS sequenziale con tetto WHOLE_SITE_MAX_PAGES; siti enormi → sitemap.xml
        scope = site.url.split("#")[0].split("?")[0]
        old = {u: self._clean(c) for u, c in site.pages.items()}
        # Known pages first: under the cap the tracked set stays stable between crawls.
        queue = [site.url, *(u for u in old if u != site.url)]
        seen, linked, new = set(queue), {site.url}, {}
        for _ in range(self.config.whole_site_max_pages):
            if not queue:
                break
            url = queue.pop(0)
            if url == site.url:
                html = root_html
            else:
                await asyncio.sleep(1)  # cortesia verso il server
                html = await self.scraper.fetch_page(url, use_browser=site.use_browser)
            if html is None:
                continue
            new[url] = self._clean(self.scraper.extract_content(html, None, base_url=url))
            for link in self.scraper.extract_page_links(html, url, scope):
                linked.add(link)
                if link not in seen:
                    seen.add(link)
                    queue.append(link)
        # Not fetched (transient error, cap) but still linked somewhere: keep the old copy.
        for u in old.keys() - new.keys():
            if u in linked:
                new[u] = old[u]
        site.pages = new

        bot = await self._bot()
        if not old:
            self._save_sites()
            await bot.send_message(
                chat_id=self.config.chat_id,
                text=(
                    f"📚 <b>Scansione completata</b>\n\n"
                    f"📍 <b>{escape_html(site.name)}</b>\n"
                    f"🔗 {site.url}\n"
                    f"📄 {len(new)} pagine sotto controllo."
                ),
                parse_mode="HTML",
                disable_notification=True,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            return None

        threshold = site.threshold if site.threshold is not None else self.config.change_threshold
        added = [u for u in new if u not in old]
        removed = [u for u in old if u not in new]
        changed = [
            u for u in new
            if u in old
            and DiffAnalyzer.has_changed(old[u], new[u])
            and DiffAnalyzer.get_change_ratio(old[u], new[u]) >= threshold
            and not self._recently_notified(site, DiffAnalyzer.compute_hash(new[u]))
        ]
        if not (added or removed or changed):
            self._save_sites()
            return None

        parts = [
            f"🌐 <b>Sito aggiornato!</b>\n\n"
            f"📍 <b>{escape_html(site.name)}</b>\n"
            f"🔗 {site.url}\n"
            f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
        ]
        if changed:  # diff only for the first one: the rest as a list, stays under 4096 chars
            first = changed[0]
            parts.append(
                f"\n✏️ <b>{escape_html(truncate(first, 100))}</b>\n"
                + DiffAnalyzer.summarize_changes(old[first], new[first], 1500)
            )
        for label, urls in (
            ("✏️ Altre pagine modificate", changed[1:]),
            ("🆕 Nuove pagine", added),
            ("🗑 Pagine rimosse", removed),
        ):
            if urls:
                parts.append(f"\n<b>{label} ({len(urls)}):</b>")
                parts += [f"  • {escape_html(truncate(u, 100))}" for u in urls[:5]]
                if len(urls) > 5:
                    parts.append(f"  <i>… e altre {len(urls) - 5}</i>")
        await bot.send_message(
            chat_id=self.config.chat_id,
            text="\n".join(parts),
            parse_mode="HTML",
            reply_markup=site_action_keyboard(site.key, site.url),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        for u in changed:
            self._remember_notified(site, DiffAnalyzer.compute_hash(new[u]))
        self._log_change(
            site, "whole_site", added=added, changed=changed, removed=removed, notified=True,
        )
        self._save_sites()
        return "sent"

    # ──────────────────────────────────────────────────────────────────────
    # Periodic jobs
    # ──────────────────────────────────────────────────────────────────────

    async def _job_check_all(self, context: ContextTypes.DEFAULT_TYPE):
        for site in list(self.sites.values()):
            # Whole-site crawl is costly: redone only every WHOLE_SITE_INTERVAL (/check forces it)
            if site.whole_site and site.last_check_dt and (
                datetime.now() - site.last_check_dt
            ).total_seconds() < self.config.whole_site_interval:
                continue
            await self._check_site(site)
            await asyncio.sleep(2)

    async def _job_daily_report(self, context: ContextTypes.DEFAULT_TYPE):
        if not self.sites:
            return

        active = sum(1 for s in self.sites.values() if not s.paused)
        paused = sum(1 for s in self.sites.values() if s.paused)
        errors = sum(1 for s in self.sites.values() if s.error_count > 0)

        lines = [f"📊 <b>Report giornaliero</b> — {datetime.now().strftime('%d/%m/%Y')}\n"]
        lines.append(f"✅ Attivi: {active}  ⏸ In pausa: {paused}  ⚠️ Con errori: {errors}\n")

        for site in self.sites.values():
            status = "⏸" if site.paused else ("⚠️" if site.error_count > 0 else "✅")
            last = site.last_check_dt.strftime("%d/%m %H:%M") if site.last_check_dt else "Mai"
            pdf_note = " 📄" if site.pdf_analysis else ""
            lines.append(f"{status} <b>{escape_html(site.name)}</b>{pdf_note} — ultimo check: {last}")

        bot = await self._bot()
        await bot.send_message(
            chat_id=self.config.chat_id,
            text="\n".join(lines),
            parse_mode="HTML",
            disable_notification=True,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    # ──────────────────────────────────────────────────────────────────────
    # Standard commands
    # ──────────────────────────────────────────────────────────────────────

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        text = (
            "🤖 <b>Web Monitor Bot</b>\n\n"
            "Monitoro pagine web e ti avviso quando cambiano.\n\n"
            "<b>Comandi principali:</b>\n"
            "/add <code>URL [nome] [css]</code> — Aggiungi sito (anche <code>--name Nome Con Spazi</code>)\n"
            "/rename <code>KEY Nuovo Nome</code> — Rinomina sito\n"
            "/addpdf — Aggiungi sito con analisi PDF (guidato)\n"
            "/list — Mostra siti monitorati\n"
            "/check — Forza controllo immediato\n"
            "/help — Guida completa\n\n"
            "<i>Puoi anche incollare direttamente un URL!</i>"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📋 I miei siti", callback_data="back_list"),
                InlineKeyboardButton("📊 Report", callback_data="report"),
            ],
            [InlineKeyboardButton("ℹ️ Stato bot", callback_data="status")],
        ])
        await msg.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
        await self._ensure_job(context)

    async def cmd_add(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return

        if not context.args:
            await msg.reply_text(
                "❌ Specifica l'URL da monitorare.\n\n"
                "<b>Uso:</b>\n"
                "<code>/add URL</code>\n"
                "<code>/add URL NomeSito</code>\n"
                "<code>/add URL NomeSito .css-selector</code>\n"
                "<code>/add URL NomeSito .css-selector SectionID</code>\n"
                "<code>/add URL --name Nome Con Spazi</code>\n"
                "<code>/add URL --whole</code> — tutto il sito (pagine e sottopagine)\n\n"
                "Per più sezioni della stessa pagina specifica SectionID diversi.\n"
                "Per rinominare un sito già aggiunto usa /rename.\n"
                "Per aggiungere con analisi PDF usa /addpdf",
                parse_mode="HTML",
            )
            return

        # Telegram's smart punctuation turns "--whole" into "—whole" (or "–whole"): accept both.
        args = [re.sub(r"^[—–]", "--", a) for a in context.args]
        whole = "--whole" in args
        args = [a for a in args if a != "--whole"]
        flag_name = ""
        if "--name" in args:
            i = args.index("--name")
            tail = args[i + 1:]
            stop = next((j for j, t in enumerate(tail) if t.startswith("--")), len(tail))
            flag_name = " ".join(tail[:stop]).strip()
            args = args[:i] + tail[stop:]

        if not args:
            await msg.reply_text("❌ Specifica l'URL da monitorare.")
            return

        url = args[0]
        if not is_valid_url(url):
            await msg.reply_text(
                "❌ URL non valido. Assicurati che inizi con <code>http://</code> o <code>https://</code>",
                parse_mode="HTML",
            )
            return

        if flag_name:
            name = flag_name
            css_selector = args[1] if len(args) > 1 else None
            section_id = args[2] if len(args) > 2 else None
        else:
            name = args[1] if len(args) > 1 else ""
            css_selector = args[2] if len(args) > 2 else None
            section_id = args[3] if len(args) > 3 else None
        site = MonitoredSite(
            url=url, name=name, css_selector=css_selector, section_id=section_id,
            whole_site=whole,
        )
        key = site.key

        if key in self.sites:
            existing = self.sites[key]
            await msg.reply_text(
                f"⚠️ Già monitorato: <code>{escape_html(key)}</code>",
                parse_mode="HTML",
                reply_markup=site_info_keyboard(key, existing.paused, existing.url),
            )
            return

        self.sites[key] = site

        status_msg = await msg.reply_text(
            f"⏳ Verifico accesso a <code>{escape_html(url)}</code>...", parse_mode="HTML"
        )

        html = await self.scraper.fetch_page(url)
        if html is None:
            html = await self.scraper._fetch_playwright(url)
            if html:
                site.use_browser = True

        if html:
            if not name:
                # No explicit name: prefer the page title over the URL slug
                site.name = WebScraper.extract_title(html) or site.name
            site.previous_content = self.scraper.extract_content(html, css_selector, base_url=url)
            site.previous_hash = DiffAnalyzer.compute_hash(site.previous_content)
            # --whole: no last_check → the next job round runs the first crawl
            site.last_check = None if whole else datetime.now().isoformat()
            await self._ensure_job(context)
            self._save_sites()

            browser_note = (
                "\n🌐 Modalità: <b>Browser</b> (Playwright)" if site.use_browser else ""
            )
            section_note = (
                f"\n🧩 Sezione: <code>{escape_html(section_id)}</code>" if section_id else ""
            )
            if whole:
                section_note += (
                    f"\n📚 Intero sito: prima scansione entro {self.config.check_interval}s, "
                    f"poi ogni {self.config.whole_site_interval // 60} min "
                    f"(max {self.config.whole_site_max_pages} pagine)"
                )
            await status_msg.edit_text(
                f"✅ <b>Sito aggiunto!</b>\n\n"
                f"📍 <b>{escape_html(site.name)}</b>\n"
                f"🔗 {url}\n"
                f"🔍 CSS: <code>{css_selector or 'auto'}</code>"
                f"{section_note}\n"
                f"⏱️ Controllo ogni {self.config.check_interval}s"
                f"{browser_note}",
                parse_mode="HTML",
                reply_markup=site_info_keyboard(key, False, url),
            )
        else:
            del self.sites[key]
            await status_msg.edit_text(
                f"❌ Impossibile accedere a <code>{escape_html(url)}</code>\n"
                "Il sito potrebbe bloccare le richieste automatiche.\n"
                "<i>Assicurati che Playwright sia installato per siti protetti.</i>",
                parse_mode="HTML",
            )

    async def cmd_remove(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        if not context.args:
            if self.sites:
                await msg.reply_text(
                    "🗑 <b>Quale sito vuoi rimuovere?</b>\n\nSeleziona dalla lista:",
                    parse_mode="HTML",
                    reply_markup=site_list_keyboard(self.sites),
                )
            else:
                await msg.reply_text("📋 Nessun sito monitorato.")
            return
        key = context.args[0]
        if key in self.sites:
            site = self.sites[key]
            await msg.reply_text(
                f"🗑 Vuoi rimuovere <b>{escape_html(site.name)}</b>?",
                parse_mode="HTML",
                reply_markup=confirm_remove_keyboard(key),
            )
        else:
            await msg.reply_text(
                f"⚠️ Sito non trovato: <code>{escape_html(key)}</code>", parse_mode="HTML"
            )

    async def cmd_list(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        if not self.sites:
            await msg.reply_text(
                "📋 <b>Nessun sito monitorato.</b>\n\nUsa /add per aggiungerne uno!",
                parse_mode="HTML",
            )
            return
        lines = [f"📋 <b>Siti monitorati ({len(self.sites)}):</b>\n"]
        for site in self.sites.values():
            status = "⏸" if site.paused else ("⚠️" if site.error_count > 0 else "✅")
            last = site.last_check_dt.strftime("%H:%M:%S") if site.last_check_dt else "Mai"
            pdf_note = " 📄" if site.pdf_analysis else ""
            section_note = f" ⟦{escape_html(site.section_id)}⟧" if site.section_id else ""
            lines.append(
                f"{status} <b>{escape_html(site.name)}</b>{pdf_note}{section_note} — {last}"
            )
        await msg.reply_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=site_list_keyboard(self.sites),
        )

    async def cmd_check(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        if not self.sites:
            await msg.reply_text("⚠️ Nessun sito da controllare. Usa /add per aggiungerne uno!")
            return
        status_msg = await msg.reply_text("🔍 Controllo in corso...")
        changes = 0
        checked = 0
        for site in list(self.sites.values()):
            result = await self._check_site(site)
            if result is not None:
                changes += 1
            checked += 1
        emoji = "🔔" if changes > 0 else "✅"
        await status_msg.edit_text(
            f"{emoji} Controllo completato: {checked} siti verificati, {changes} cambiamenti rilevati."
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        job_status = "❌ Inattivo"
        if context.job_queue:
            jobs = context.job_queue.get_jobs_by_name("check_sites")
            job_status = "✅ Attivo" if jobs else "❌ Inattivo"
        active = sum(1 for s in self.sites.values() if not s.paused)
        paused = sum(1 for s in self.sites.values() if s.paused)
        errors = sum(1 for s in self.sites.values() if s.error_count > 0)
        pdf_sites = sum(1 for s in self.sites.values() if s.pdf_analysis)
        text = (
            f"🤖 <b>Stato Bot</b>\n\n"
            f"📡 Monitoraggio: {job_status}\n"
            f"📊 Siti: {len(self.sites)} totali\n"
            f"   ├ ✅ Attivi: {active}\n"
            f"   ├ ⏸ In pausa: {paused}\n"
            f"   ├ ⚠️ Con errori: {errors}\n"
            f"   └ 📄 Con analisi PDF: {pdf_sites}\n"
            f"⏱️ Intervallo: {self.config.check_interval}s\n"
            f"🎯 Soglia: {self.config.change_threshold:.0%}"
        )
        await msg.reply_text(text, parse_mode="HTML")

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        if not context.args:
            await msg.reply_text(
                "❌ Specifica la chiave: <code>/pause URL</code> o <code>/pause URL#section</code>",
                parse_mode="HTML",
            )
            return
        key = context.args[0]
        if key in self.sites:
            self.sites[key].paused = True
            self._save_sites()
            await msg.reply_text(
                f"⏸ <b>{escape_html(self.sites[key].name)}</b> in pausa.", parse_mode="HTML"
            )
        else:
            await msg.reply_text("⚠️ Sito non trovato.")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        if not context.args:
            await msg.reply_text(
                "❌ Specifica la chiave: <code>/resume URL</code> o <code>/resume URL#section</code>",
                parse_mode="HTML",
            )
            return
        key = context.args[0]
        if key in self.sites:
            self.sites[key].paused = False
            self.sites[key].error_count = 0
            self.sites[key].error_notified = False
            self._save_sites()
            await msg.reply_text(
                f"▶️ <b>{escape_html(self.sites[key].name)}</b> ripreso!", parse_mode="HTML"
            )
        else:
            await msg.reply_text("⚠️ Sito non trovato.")

    async def cmd_rename(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        if len(context.args) < 2:
            await msg.reply_text(
                "❌ Specifica chiave e nuovo nome.\n\n"
                "<b>Uso:</b>\n"
                "<code>/rename KEY Nuovo Nome</code>\n\n"
                "KEY è l'URL (o <code>URL#SectionID</code>) mostrato in /list. "
                "Il nome può contenere spazi.",
                parse_mode="HTML",
            )
            return
        key = context.args[0]
        if key not in self.sites:
            await msg.reply_text("⚠️ Sito non trovato.")
            return
        site = self.sites[key]
        old_name = site.name
        site.name = " ".join(context.args[1:]).strip()
        self._save_sites()
        await msg.reply_text(
            f"✏️ <b>{escape_html(old_name)}</b> → <b>{escape_html(site.name)}</b>",
            parse_mode="HTML",
        )

    async def cmd_threshold(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        if len(context.args) < 2:
            await msg.reply_text(
                "❌ Specifica URL e soglia.\n\n"
                "<b>Uso:</b>\n"
                "<code>/threshold URL 0.001</code> — imposta soglia allo 0.1%\n"
                "<code>/threshold URL reset</code> — torna alla soglia globale",
                parse_mode="HTML",
            )
            return
        key = context.args[0]
        if key not in self.sites:
            await msg.reply_text("⚠️ Sito non trovato.")
            return
        value = context.args[1]
        if value.lower() == "reset":
            self.sites[key].threshold = None
            self._save_sites()
            await msg.reply_text(
                f"🎯 Soglia di <b>{escape_html(self.sites[key].name)}</b> "
                f"ripristinata al valore globale ({self.config.change_threshold:.2%}).",
                parse_mode="HTML",
            )
        else:
            try:
                threshold = float(value)
                if not 0.0 <= threshold <= 1.0:
                    raise ValueError
                self.sites[key].threshold = threshold
                self._save_sites()
                await msg.reply_text(
                    f"🎯 Soglia di <b>{escape_html(self.sites[key].name)}</b> "
                    f"impostata a {threshold:.2%}.",
                    parse_mode="HTML",
                )
            except ValueError:
                await msg.reply_text("❌ Valore non valido. Usa un numero tra 0.0 e 1.0.")

    async def cmd_browser(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        if not context.args:
            await msg.reply_text(
                "❌ Specifica l'URL: <code>/browser URL</code>", parse_mode="HTML"
            )
            return
        key = context.args[0]
        if key not in self.sites:
            await msg.reply_text("⚠️ Sito non trovato.")
            return
        site = self.sites[key]
        site.use_browser = not site.use_browser
        self._save_sites()
        mode = "Browser (Playwright)" if site.use_browser else "HTTP (aiohttp)"
        await msg.reply_text(
            f"🌐 <b>{escape_html(site.name)}</b> ora usa: <b>{mode}</b>", parse_mode="HTML"
        )

    async def cmd_fullpage(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        if not context.args:
            await msg.reply_text(
                "❌ Specifica la chiave: <code>/fullpage URL</code> o <code>/fullpage URL#section</code>",
                parse_mode="HTML",
            )
            return
        key = context.args[0]
        if key not in self.sites:
            await msg.reply_text("⚠️ Sito non trovato.")
            return
        site = self.sites[key]
        if not site.css_selector:
            await msg.reply_text(
                "⚠️ Nessun selettore CSS impostato — questo sito monitora già tutta la pagina."
            )
            return
        site.monitor_full_page = not site.monitor_full_page
        if site.monitor_full_page:
            # Reset stored full-page state so first check sets the baseline
            site.previous_full_content = None
            site.previous_full_hash = None
        self._save_sites()
        state = "attivato ✅" if site.monitor_full_page else "disattivato 🚫"
        await msg.reply_text(
            f"🌐 <b>{escape_html(site.name)}</b>: monitoraggio pagina intera {state}.\n\n"
            + (
                "Da ora riceverai notifiche per qualsiasi cambiamento sul sito,\n"
                "oltre alla notifica specifica per la sezione monitorata."
                if site.monitor_full_page
                else "Solo la sezione configurata verrà monitorata."
            ),
            parse_mode="HTML",
        )

    async def cmd_gif(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        self.gif_enabled = not self.gif_enabled
        self._save_sites()
        state = "attivata ✅" if self.gif_enabled else "disattivata 🚫"
        await msg.reply_text(
            f"🎬 GIF nelle notifiche <b>{state}</b>.\nUsa /gif per cambiare di nuovo.",
            parse_mode="HTML",
        )

    async def cmd_ignore(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """List or add a noise pattern: lines matching it never trigger a notification."""
        msg = self._get_msg(update)
        if not msg:
            return
        if not context.args:
            if not self.noise_patterns:
                await msg.reply_text(
                    "🔕 Nessun filtro attivo.\n\n"
                    "<b>Uso:</b> <code>/ignore Cerca nel sito</code>",
                    parse_mode="HTML",
                )
                return
            lines = "\n".join(
                f"{i}. <code>{escape_html(p)}</code>"
                for i, p in enumerate(self.noise_patterns, 1)
            )
            await msg.reply_text(
                f"🔕 <b>Righe ignorate nei confronti:</b>\n\n{lines}\n\n"
                "Aggiungi: <code>/ignore testo o regex</code>\n"
                "Rimuovi: <code>/unignore N</code>",
                parse_mode="HTML",
            )
            return

        pattern = " ".join(context.args).strip()
        if pattern in self.noise_patterns:
            await msg.reply_text("ℹ️ Filtro già presente.")
            return
        try:
            re.compile(pattern, re.I)
        except re.error as e:
            await msg.reply_text(f"❌ Espressione non valida: {escape_html(str(e))}")
            return
        self.noise_patterns.append(pattern)
        # Rinormalizza subito lo stato salvato: le righe filtrate spariscono dal
        # confronto successivo invece di apparire come contenuto rimosso.
        for site in self.sites.values():
            site.previous_content = self._clean(site.previous_content)
            site.previous_full_content = self._clean(site.previous_full_content)
        self._save_sites()
        await msg.reply_text(
            f"🔕 Ora ignoro le righe che contengono <code>{escape_html(pattern)}</code>.",
            parse_mode="HTML",
        )

    async def cmd_unignore(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        if not context.args or not context.args[0].isdigit():
            await msg.reply_text(
                "❌ Indica il numero del filtro.\n\n"
                "<b>Uso:</b> <code>/unignore 2</code> (numeri da <code>/ignore</code>)",
                parse_mode="HTML",
            )
            return
        idx = int(context.args[0])
        if not 1 <= idx <= len(self.noise_patterns):
            await msg.reply_text(f"❌ Numero fuori range (1-{len(self.noise_patterns)}).")
            return
        removed = self.noise_patterns.pop(idx - 1)
        self._save_sites()
        await msg.reply_text(
            f"🔔 Filtro rimosso: <code>{escape_html(removed)}</code>",
            parse_mode="HTML",
        )

    async def cmd_expect(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        if len(context.args) < 2:
            await msg.reply_text(
                "❌ Specifica chiave e posizione.\n\n"
                "<b>Uso:</b>\n"
                "<code>/expect URL 3</code> — la tua posizione attesa in graduatoria\n"
                "<code>/expect URL 3 Mario Rossi</code> — anche il nome (per la conferma)\n"
                "<code>/expect URL reset</code> — rimuovi il controllo\n\n"
                "Richiede analisi PDF attiva (vedi /addpdf o /code). Ad ogni ripubblicazione "
                "della graduatoria ti avviso se il nome alla tua posizione è ancora omissato "
                "o finalmente pubblicato.",
                parse_mode="HTML",
            )
            return
        key = context.args[0]
        if key not in self.sites:
            await msg.reply_text("⚠️ Sito non trovato.")
            return
        site = self.sites[key]
        if context.args[1].lower() == "reset":
            site.pdf_expected_position = None
            site.pdf_expected_name = None
            self._save_sites()
            await msg.reply_text(
                f"🎯 Controllo posizione rimosso per <b>{escape_html(site.name)}</b>.",
                parse_mode="HTML",
            )
            return
        site.pdf_expected_position = context.args[1].strip()
        site.pdf_expected_name = " ".join(context.args[2:]).strip() or None
        # I PDF già analizzati vanno rivalutati con la nuova regola
        site.pdf_results = {}
        self._save_sites()
        name_note = (
            f"\n👤 Nome atteso: <b>{escape_html(site.pdf_expected_name)}</b>"
            if site.pdf_expected_name
            else ""
        )
        pdf_note = (
            ""
            if site.pdf_analysis and site.pdf_search_value
            else "\n\n⚠️ Analisi PDF non attiva su questo sito: il controllo scatterà "
            "solo dopo aver impostato un codice da cercare (usa /addpdf)."
        )
        await msg.reply_text(
            f"🎯 <b>{escape_html(site.name)}</b>\n"
            f"Attendo il codice <code>{escape_html(site.pdf_search_value or '—')}</code> "
            f"in posizione <b>{escape_html(site.pdf_expected_position)}</b> della graduatoria."
            f"{name_note}{pdf_note}",
            parse_mode="HTML",
        )

    async def cmd_selector(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        if len(context.args) < 2:
            await msg.reply_text(
                "❌ Specifica chiave e selettore CSS.\n\n"
                "<b>Uso:</b>\n"
                "<code>/selector KEY .mio-selettore</code>\n"
                "<code>/selector KEY reset</code> — torna a monitorare tutta la pagina\n\n"
                "Il selettore può contenere spazi (es. <code>div.card:-soup-contains(\"Funzionari amministrativi\")</code>): "
                "scrivi tutto dopo la chiave. Serve a limitare il monitoraggio (e i PDF analizzati) "
                "a una sola sezione della pagina.",
                parse_mode="HTML",
            )
            return
        key = context.args[0]
        if key not in self.sites:
            await msg.reply_text("⚠️ Sito non trovato.")
            return
        site = self.sites[key]
        new_selector = None if context.args[1].lower() == "reset" else " ".join(context.args[1:]).strip()
        if new_selector != site.css_selector:
            site.css_selector = new_selector
            # Cambio scope → azzero baseline e PDF già visti così il prossimo
            # check riparte pulito e rianalizza i PDF della nuova sezione.
            site.previous_content = None
            site.previous_hash = None
            site.pdf_results = {}
        self._save_sites()
        await msg.reply_text(
            f"🔍 <b>{escape_html(site.name)}</b>\n"
            f"Selettore CSS: <code>{escape_html(site.css_selector or 'auto (tutta la pagina)')}</code>\n\n"
            f"<i>Baseline riazzerata: nessuna falsa notifica al prossimo controllo.</i>",
            parse_mode="HTML",
        )

    async def cmd_code(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        if len(context.args) < 2:
            await msg.reply_text(
                "❌ Specifica chiave e codice.\n\n"
                "<b>Uso:</b>\n"
                "<code>/code KEY ABC123XYZ</code> — cerca questo codice nei PDF\n"
                "<code>/code KEY off</code> — disattiva l'analisi PDF",
                parse_mode="HTML",
            )
            return
        key = context.args[0]
        if key not in self.sites:
            await msg.reply_text("⚠️ Sito non trovato.")
            return
        site = self.sites[key]
        value = context.args[1].strip()
        if value.lower() == "off":
            site.pdf_analysis = False
            site.pdf_results = {}
            self._save_sites()
            await msg.reply_text(
                f"🚫 Analisi PDF disattivata per <b>{escape_html(site.name)}</b>.",
                parse_mode="HTML",
            )
            return
        site.pdf_analysis = True
        site.pdf_search_value = value
        site.pdf_results = {}
        self._save_sites()
        await msg.reply_text(
            f"📄 <b>{escape_html(site.name)}</b>\n"
            f"Analisi PDF attiva. Cerco il codice <code>{escape_html(value)}</code> nei PDF della sezione.\n"
            f"Imposta la posizione/nome attesi con <code>/expect {escape_html(key)} 3 Nome Cognome</code>.",
            parse_mode="HTML",
        )

    async def cmd_report(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        await self._job_daily_report(context)
        if not self.sites:
            await msg.reply_text("📋 Nessun sito monitorato.")

    async def cmd_test(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return

        test_url = "https://www.example.com/test"
        test_message = (
            "🔔 <b>TEST — Aggiornamento rilevato!</b>\n\n"
            "📍 <b>Sito di Test</b>\n"
            f"🔗 {test_url}\n"
            f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n\n"
            "📊 Similarità: 87.3%\n\n"
            "✅ <b>Contenuto aggiunto (3 righe):</b>\n"
            "  • Pubblicazione graduatoria finale\n"
            "  • Convocazione candidati idonei\n"
            "  • Aggiornamento calendario prove\n\n"
            "❌ <b>Contenuto rimosso (1 riga):</b>\n"
            "  • Avviso sospensione temporanea"
        )

        await msg.reply_text("🧪 Invio notifica di test...")
        bot = await self._bot()
        await bot.send_message(
            chat_id=self.config.chat_id,
            text=test_message,
            parse_mode="HTML",
            reply_markup=site_action_keyboard(test_url),
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

        gif_path = Path(self.config.gif_path)
        if self.gif_enabled and gif_path.is_file():
            try:
                with open(gif_path, "rb") as f:
                    await bot.send_animation(
                        chat_id=self.config.chat_id,
                        animation=f,
                        caption="🎉 Aggiornamento rilevato!",
                    )
            except Exception as e:
                logger.warning(f"Errore invio GIF di test: {e}")

        await bot.send_message(
            chat_id=self.config.chat_id,
            text=(
                "📎 <b>Documenti trovati:</b>\n\n"
                '📄 <a href="https://esempio.it/graduatoria.pdf">Graduatoria Finale</a> (PDF)\n'
                '📄 <a href="https://esempio.it/convocazione.pdf">Convocazione Candidati</a> (PDF)'
            ),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            disable_notification=True,
        )

        await bot.send_message(
            chat_id=self.config.chat_id,
            text=(
                "🎯 <b>TEST — Codice trovato nel documento!</b>\n\n"
                "📍 <b>Sito di Test</b>\n"
                '📄 Documento: <a href="https://esempio.it/graduatoria.pdf">graduatoria.pdf</a>\n\n'
                "🔑 Codice: <code>ABC123XYZ</code>\n"
                "🏆 Posizione: <b>1</b>\n\n"
                f"🕐 {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
            ),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            disable_notification=True,
        )

        await msg.reply_text("✅ Notifica di test inviata (incluso test analisi PDF)!")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg:
            return
        report_auto = (
            f"Report automatico ogni giorno alle {self.config.daily_report_hour}:00\n\n"
            if self.config.daily_report_enabled
            else "Report automatico disattivato (solo <code>/report</code> manuale).\n\n"
        )
        text = (
            "📖 <b>Guida Web Monitor Bot</b>\n\n"
            "<b>➕ Aggiungere un sito:</b>\n"
            "<code>/add URL</code>\n"
            "<code>/add URL NomeSito</code>\n"
            "<code>/add URL NomeSito .css-selector</code>\n"
            "<code>/add URL NomeSito .css-selector SectionID</code>\n"
            "<code>/add URL --name Nome Con Spazi</code>\n"
            "<code>/add URL --whole</code> — tutto il sito: pagine e sottopagine sotto l'URL, "
            f"ricontrollate ogni {self.config.whole_site_interval // 60} min "
            f"(max {self.config.whole_site_max_pages} pagine)\n\n"
            "<b>✏️ Rinominare un sito:</b>\n"
            "<code>/rename KEY Nuovo Nome</code>\n\n"
            "<b>🧩 SectionID:</b> etichetta per distinguere più sezioni dello stesso URL.\n"
            "Esempio: stessa pagina, sezione A e sezione B come due entry separate.\n"
            "Per gestirle usa <code>URL#SectionID</code> nei comandi pause/resume/remove.\n\n"
            "<b>📄 Aggiungere sito con analisi PDF:</b>\n"
            "<code>/addpdf</code> — procedura guidata interattiva\n"
            "Cerca un valore specifico nei PDF trovati nella sezione monitorata.\n\n"
            "<b>🎯 Selettore CSS:</b>\n"
            "Monitora solo una parte della pagina.\n"
            "Esempi: <code>.main-content</code>, <code>#results</code>, <code>article</code>\n\n"
            "<b>⏸ Pausa/Riprendi:</b>\n"
            "<code>/pause URL</code> — mette in pausa il monitoraggio\n"
            "<code>/resume URL</code> — riprende il monitoraggio\n\n"
            "<b>🎯 Soglia per-sito:</b>\n"
            "<code>/threshold URL 0.001</code> — soglia personalizzata (0.1%)\n"
            "<code>/threshold URL reset</code> — torna alla soglia globale\n\n"
            "<b>🌐 Modalità browser:</b>\n"
            "<code>/browser URL</code> — attiva/disattiva Playwright\n"
            "Utile per siti che bloccano richieste HTTP dirette.\n\n"
            "<b>📄 Sorvegliare una graduatoria (nome omissato → pubblicato):</b>\n"
            "<code>/selector KEY <css></code> — limita il monitoraggio a una sezione\n"
            "<code>/code KEY ABC123XYZ</code> — codice da cercare nei PDF\n"
            "<code>/expect KEY 3 Nome Cognome</code> — posizione e nome attesi\n"
            "Ad ogni ripubblicazione della graduatoria ti avviso se il nome alla tua "
            "posizione è <b>ancora omissato</b> oppure <b>finalmente pubblicato</b>.\n\n"
            "<b>🧩 Monitorare due sezioni diverse:</b>\n"
            "Ripeti <code>/addpdf</code> con un <b>SectionID</b> diverso per ciascuna sezione "
            "(stesso URL, selettore e codice propri). Le due sezioni restano entry separate.\n\n"
            "<b>🔁 Ripetizioni:</b>\n"
            f"Un contenuto identico già notificato nelle ultime "
            f"{self.config.dedup_window_hours:g}h non viene rimandato "
            "(pagina che cambia e torna indietro).\n"
            "Configurabile con <code>DEDUP_WINDOW_HOURS</code> nel .env\n\n"
            "<b>🔕 Rumore di pagina (link ricerca, separatori…):</b>\n"
            "<code>/ignore</code> — elenca le righe ignorate nei confronti\n"
            "<code>/ignore Cerca nel sito</code> — aggiunge un filtro (testo o regex)\n"
            "<code>/unignore 2</code> — rimuove il filtro numero 2\n"
            "Le righe filtrate non generano più notifiche quando compaiono e scompaiono.\n\n"
            "<b>🎬 GIF nelle notifiche (di default spenta):</b>\n"
            "<code>/gif</code> — attiva/disattiva la GIF celebrativa\n"
            "La GIF non viene mai inviata se la pagina è stata svuotata o riscritta per intero.\n\n"
            "<b>📊 Report:</b>\n"
            "<code>/report</code> — report manuale dello stato\n"
            + report_auto
            + "<b>🎯 Soglia cambiamento:</b>\n"
            f"Cambiamenti sotto il {self.config.change_threshold:.0%} vengono ignorati.\n"
            "Configurabile con <code>CHANGE_THRESHOLD</code> nel .env\n\n"
            "<b>💡 Tip:</b> Puoi anche incollare direttamente un URL senza /add!"
        )
        await msg.reply_text(text, parse_mode="HTML")

    # ──────────────────────────────────────────────────────────────────────
    # /addpdf guided conversation
    # ──────────────────────────────────────────────────────────────────────

    async def conv_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = self._get_msg(update)
        if not msg:
            return ConversationHandler.END
        context.user_data.clear()
        await msg.reply_text(
            "🔧 <b>Aggiungi sito con analisi PDF</b>\n\n"
            "Passo 1/6 — Inviami l'<b>URL</b> da monitorare:\n\n"
            "<i>Usa /cancel per annullare in qualsiasi momento.</i>",
            parse_mode="HTML",
        )
        return CONV_URL

    async def conv_got_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = self._get_msg(update)
        if not msg or not msg.text:
            return CONV_URL
        url = msg.text.strip()
        if not is_valid_url(url):
            await msg.reply_text(
                "❌ URL non valido. Deve iniziare con http:// o https://\n\nRiprova:"
            )
            return CONV_URL
        context.user_data["url"] = url
        same_url_count = sum(1 for s in self.sites.values() if s.url == url)
        hint = ""
        if same_url_count:
            hint = (
                f"\n\n⚠️ <b>{same_url_count}</b> sezione/i già monitorata/e per questo URL — "
                "specifica un <b>SectionID</b> diverso al passo 3 per evitare collisioni."
            )
        await msg.reply_text(
            "Passo 2/6 — <b>Selettore CSS</b> (opzionale)\n\n"
            "Inserisci un selettore per monitorare solo una <b>sezione</b> della pagina.\n"
            "Esempi: <code>#section-results</code>, <code>.accordion-content</code>\n\n"
            "Premi <b>Skip</b> per monitorare tutta la pagina."
            f"{hint}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Skip (tutta la pagina)", callback_data="conv_skip_sel"),
            ]]),
        )
        return CONV_SELECTOR

    async def conv_got_selector_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        msg = self._get_msg(update)
        if not msg or not msg.text:
            return CONV_SELECTOR
        context.user_data["css_selector"] = msg.text.strip()
        return await self._conv_ask_section(msg)

    async def conv_got_selector_skip(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        query = update.callback_query
        await query.answer()
        context.user_data["css_selector"] = None
        await query.edit_message_text("✅ Nessun selettore CSS — verrà monitorata tutta la pagina.")
        return await self._conv_ask_section(query.message)

    async def _conv_ask_section(self, msg) -> int:
        await msg.reply_text(
            "Passo 3/6 — <b>SectionID</b> (opzionale)\n\n"
            "Etichetta breve per distinguere più sezioni della stessa pagina "
            "(es. <code>J</code>, <code>B</code>).\n"
            "Necessario solo se monitori più sezioni dello stesso URL.\n\n"
            "Premi <b>Skip</b> per non impostarlo.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Skip", callback_data="conv_skip_section"),
            ]]),
        )
        return CONV_SECTION_ID

    async def conv_got_section_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        msg = self._get_msg(update)
        if not msg or not msg.text:
            return CONV_SECTION_ID
        section_id = msg.text.strip()
        if "#" in section_id or not section_id:
            await msg.reply_text("❌ SectionID non valido (no caratteri '#', non vuoto). Riprova:")
            return CONV_SECTION_ID
        url = context.user_data.get("url", "")
        candidate_key = f"{url}#{section_id}"
        if candidate_key in self.sites:
            await msg.reply_text(
                f"⚠️ Già monitorato: <code>{escape_html(candidate_key)}</code>\n"
                "Scegli un SectionID diverso o /cancel.",
                parse_mode="HTML",
            )
            return CONV_SECTION_ID
        context.user_data["section_id"] = section_id
        return await self._conv_ask_name(msg)

    async def conv_got_section_skip(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        query = update.callback_query
        await query.answer()
        context.user_data["section_id"] = None
        url = context.user_data.get("url", "")
        if url in self.sites:
            await query.edit_message_text(
                f"⚠️ URL già monitorato senza SectionID ({escape_html(self.sites[url].name)}). "
                "Imposta un SectionID per aggiungere un'altra sezione, o /cancel.",
                parse_mode="HTML",
            )
            return CONV_SECTION_ID
        await query.edit_message_text("✅ Nessun SectionID.")
        return await self._conv_ask_name(query.message)

    async def _conv_ask_name(self, msg) -> int:
        await msg.reply_text(
            "Passo 4/6 — <b>Nome</b> del sito (opzionale)\n\n"
            "Inserisci un nome descrittivo o premi <b>Skip</b>.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Skip (usa default)", callback_data="conv_skip_name"),
            ]]),
        )
        return CONV_NAME

    async def conv_got_name_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        msg = self._get_msg(update)
        if not msg or not msg.text:
            return CONV_NAME
        context.user_data["name"] = msg.text.strip()
        return await self._conv_ask_pdf(msg)

    async def conv_got_name_skip(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        query = update.callback_query
        await query.answer()
        context.user_data["name"] = ""
        await query.edit_message_text("✅ Nome: verrà usato il default dall'URL.")
        return await self._conv_ask_pdf(query.message)

    async def _conv_ask_pdf(self, msg) -> int:
        await msg.reply_text(
            "Passo 5/6 — <b>Analisi PDF</b>\n\n"
            "Vuoi che il bot scarichi i PDF trovati nella sezione\n"
            "e cerchi il tuo codice nelle tabelle?",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📄 Sì, analizza PDF", callback_data="conv_pdf_yes"),
                InlineKeyboardButton("🚫 No", callback_data="conv_pdf_no"),
            ]]),
        )
        return CONV_PDF_FLAG

    async def conv_pdf_yes(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        context.user_data["pdf_analysis"] = True
        await query.edit_message_text("✅ Analisi PDF attivata.")
        await query.message.reply_text(
            "Passo 6/6 — <b>Codice da cercare</b>\n\n"
            "Inserisci il valore da cercare nella colonna delle tabelle PDF.\n\n"
            "Esempio: <code>ABC123XYZ</code>",
            parse_mode="HTML",
        )
        return CONV_CODICE

    async def conv_pdf_no(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        context.user_data["pdf_analysis"] = False
        context.user_data["pdf_search_value"] = None
        await query.edit_message_text("🚫 Analisi PDF disattivata.")
        return await self._conv_finalize(query.message, context)

    async def conv_got_codice(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = self._get_msg(update)
        if not msg or not msg.text:
            return CONV_CODICE
        codice = msg.text.strip()
        if not codice:
            await msg.reply_text("❌ Codice non valido. Riprova:")
            return CONV_CODICE
        context.user_data["pdf_search_value"] = codice
        await msg.reply_text(
            "Passo 7/7 — <b>Posizione attesa</b> (opzionale)\n\n"
            "Invia la posizione in graduatoria, seguita facoltativamente dal nome atteso.\n"
            "Esempio: <code>3 Mario Rossi</code>\n\n"
            "Quando la graduatoria verrà ripubblicata ti avviserò se il tuo nome è mostrato "
            "oppure ancora omissato. Premi <b>Skip</b> per non impostarla.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏭ Skip", callback_data="conv_skip_expect"),
            ]]),
        )
        return CONV_EXPECT

    async def conv_got_expect_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = self._get_msg(update)
        if not msg or not msg.text:
            return CONV_EXPECT
        tokens = msg.text.split()
        context.user_data["pdf_expected_position"] = tokens[0].strip()
        context.user_data["pdf_expected_name"] = " ".join(tokens[1:]).strip() or None
        return await self._conv_finalize(msg, context)

    async def conv_got_expect_skip(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        query = update.callback_query
        await query.answer()
        await query.edit_message_text("✅ Nessuna posizione attesa impostata.")
        return await self._conv_finalize(query.message, context)

    async def _conv_finalize(self, msg, context: ContextTypes.DEFAULT_TYPE) -> int:
        data = context.user_data
        url = data["url"]
        name = data.get("name", "")
        css_selector = data.get("css_selector")
        section_id = data.get("section_id")
        pdf_analysis = data.get("pdf_analysis", False)
        pdf_search_value = data.get("pdf_search_value")

        site = MonitoredSite(
            url=url,
            name=name,
            section_id=section_id,
            css_selector=css_selector,
            pdf_analysis=pdf_analysis,
            pdf_search_value=pdf_search_value,
            pdf_expected_position=data.get("pdf_expected_position"),
            pdf_expected_name=data.get("pdf_expected_name"),
        )
        key = site.key
        self.sites[key] = site

        status_msg = await msg.reply_text(
            f"⏳ Verifico accesso a <code>{escape_html(url)}</code>...", parse_mode="HTML"
        )

        html = await self.scraper.fetch_page(url)
        if html is None:
            html = await self.scraper._fetch_playwright(url)
            if html:
                site.use_browser = True

        if html:
            if not name:
                site.name = WebScraper.extract_title(html) or site.name
            site.previous_content = self.scraper.extract_content(html, css_selector, base_url=url)
            site.previous_hash = DiffAnalyzer.compute_hash(site.previous_content)
            site.last_check = datetime.now().isoformat()
            self._save_sites()

            pdf_note = ""
            if pdf_analysis and pdf_search_value:
                pdf_note = (
                    f"\n📄 Analisi PDF: <b>attiva</b>\n"
                    f"🔑 Cerco: <code>{escape_html(pdf_search_value)}</code>"
                )
                if site.pdf_expected_position:
                    exp_name = (
                        f" ({escape_html(site.pdf_expected_name)})"
                        if site.pdf_expected_name
                        else ""
                    )
                    pdf_note += f"\n🎯 Posizione attesa: <b>{escape_html(site.pdf_expected_position)}</b>{exp_name}"
            section_note = (
                f"\n🧩 Sezione: <code>{escape_html(section_id)}</code>" if section_id else ""
            )

            await status_msg.edit_text(
                f"✅ <b>Sito aggiunto!</b>\n\n"
                f"📍 <b>{escape_html(site.name)}</b>\n"
                f"🔗 {url}\n"
                f"🔍 CSS: <code>{css_selector or 'auto'}</code>"
                f"{section_note}"
                f"{pdf_note}\n"
                f"⏱️ Controllo ogni {self.config.check_interval}s",
                parse_mode="HTML",
                reply_markup=site_info_keyboard(key, False, url),
            )
        else:
            del self.sites[key]
            await status_msg.edit_text(
                f"❌ Impossibile accedere a <code>{escape_html(url)}</code>\n"
                "Prova ad attivare la modalità browser con /browser dopo aver aggiunto il sito.",
                parse_mode="HTML",
            )

        context.user_data.clear()
        return ConversationHandler.END

    async def conv_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        msg = self._get_msg(update)
        if msg:
            await msg.reply_text("❌ Operazione annullata.")
        context.user_data.clear()
        return ConversationHandler.END

    # ──────────────────────────────────────────────────────────────────────
    # Direct URL handler
    # ──────────────────────────────────────────────────────────────────────

    async def handle_url(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = self._get_msg(update)
        if not msg or not msg.text:
            return
        text = msg.text.strip()
        key = context.user_data.pop("awaiting_rename", None)
        if key is not None:
            if key not in self.sites:
                await msg.reply_text("⚠️ Sito non più monitorato.")
                return
            site = self.sites[key]
            if text.lower() == "annulla" or not text:
                await msg.reply_text(
                    "❌ Rinomina annullata.",
                    reply_markup=site_info_keyboard(key, site.paused, site.url),
                )
                return
            old_name = site.name
            site.name = text
            self._save_sites()
            await msg.reply_text(
                f"✏️ <b>{escape_html(old_name)}</b> → <b>{escape_html(site.name)}</b>",
                parse_mode="HTML",
                reply_markup=site_info_keyboard(key, site.paused, site.url),
            )
            return
        if is_valid_url(text):
            context.args = [text]
            await self.cmd_add(update, context)

    # ──────────────────────────────────────────────────────────────────────
    # Inline keyboard callbacks
    # ──────────────────────────────────────────────────────────────────────

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data
        if not data:
            return

        if data == "noop":
            return
        if data == "cancel":
            await query.edit_message_text("❌ Operazione annullata.")
            return
        if data == "back_list":
            return await self._show_list(query)
        if data == "status":
            return await self._show_status(query)
        if data == "report":
            await self._job_daily_report(context)
            return

        if "|" not in data:
            return

        action, short_id = data.split("|", 1)
        key = _resolve_site_id(self.sites, short_id)

        if key is None:
            await query.edit_message_text("⚠️ Sito non più monitorato.")
            return

        site = self.sites[key]

        if action == "recheck":
            await query.edit_message_text(
                f"🔍 Ricontrollo <b>{escape_html(site.name)}</b>...",
                parse_mode="HTML",
            )
            result = await self._check_site(site)
            if result is None:
                await query.edit_message_text(
                    f"✅ Nessun cambiamento per <b>{escape_html(site.name)}</b>",
                    parse_mode="HTML",
                    reply_markup=site_info_keyboard(key, site.paused, site.url),
                )
        elif action == "pause":
            site.paused = True
            self._save_sites()
            await query.edit_message_text(
                f"⏸ <b>{escape_html(site.name)}</b> messo in pausa.",
                parse_mode="HTML",
                reply_markup=site_info_keyboard(key, True, site.url),
            )
        elif action == "resume":
            site.paused = False
            site.error_count = 0
            site.error_notified = False
            self._save_sites()
            await query.edit_message_text(
                f"▶️ <b>{escape_html(site.name)}</b> ripreso!",
                parse_mode="HTML",
                reply_markup=site_info_keyboard(key, False, site.url),
            )
        elif action == "remove":
            await query.edit_message_text(
                f"🗑 Vuoi rimuovere <b>{escape_html(site.name)}</b>?",
                parse_mode="HTML",
                reply_markup=confirm_remove_keyboard(key),
            )
        elif action == "confirm_remove":
            name = site.name
            del self.sites[key]
            self._save_sites()
            await query.edit_message_text(
                f"✅ <b>{escape_html(name)}</b> rimosso!", parse_mode="HTML"
            )
        elif action == "rename":
            context.user_data["awaiting_rename"] = key
            await query.edit_message_text(
                f"✏️ Invia il nuovo nome per <b>{escape_html(site.name)}</b>.\n\n"
                "Scrivi <code>annulla</code> per annullare.",
                parse_mode="HTML",
            )
        elif action == "info":
            return await self._show_site_info(query, key)

    async def _show_list(self, query):
        if not self.sites:
            await query.edit_message_text(
                "📋 <b>Nessun sito monitorato.</b>\nUsa /add per aggiungerne uno!",
                parse_mode="HTML",
            )
            return
        lines = [f"📋 <b>Siti monitorati ({len(self.sites)}):</b>\n"]
        for site in self.sites.values():
            status = "⏸" if site.paused else ("⚠️" if site.error_count > 0 else "✅")
            last = site.last_check_dt.strftime("%H:%M") if site.last_check_dt else "Mai"
            pdf_note = " 📄" if site.pdf_analysis else ""
            section_note = f" ⟦{escape_html(site.section_id)}⟧" if site.section_id else ""
            lines.append(
                f"{status} <b>{escape_html(site.name)}</b>{pdf_note}{section_note} — {last}"
            )
        await query.edit_message_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=site_list_keyboard(self.sites),
        )

    async def _show_status(self, query):
        active = sum(1 for s in self.sites.values() if not s.paused)
        paused = sum(1 for s in self.sites.values() if s.paused)
        text = (
            f"🤖 <b>Stato Bot</b>\n\n"
            f"📊 {len(self.sites)} siti — {active} attivi, {paused} in pausa\n"
            f"⏱️ Intervallo: {self.config.check_interval}s\n"
            f"🎯 Soglia: {self.config.change_threshold:.0%}"
        )
        await query.edit_message_text(text, parse_mode="HTML")

    async def _show_site_info(self, query, key: str):
        site = self.sites[key]
        status = "⏸ In pausa" if site.paused else ("⚠️ Errori" if site.error_count > 0 else "✅ Attivo")
        last = (
            site.last_check_dt.strftime("%d/%m/%Y %H:%M:%S") if site.last_check_dt else "Mai"
        )
        effective_threshold = (
            site.threshold if site.threshold is not None else self.config.change_threshold
        )
        section_line = (
            f"🧩 Sezione: <code>{escape_html(site.section_id)}</code>\n" if site.section_id else ""
        )
        text = (
            f"📍 <b>{escape_html(site.name)}</b>\n\n"
            f"🔗 {site.url}\n"
            f"{section_line}"
            f"🆔 Key: <code>{escape_html(key)}</code>\n"
            f"📡 Stato: {status}\n"
            f"🕐 Ultimo check: {last}\n"
            f"🔍 CSS: <code>{site.css_selector or 'auto'}</code>\n"
            f"🎯 Soglia: {effective_threshold:.2%}"
            f"{' (per-sito)' if site.threshold is not None else ' (globale)'}\n"
            f"🌐 Browser: {'Sì (Playwright)' if site.use_browser else 'No (aiohttp)'}\n"
        f"📡 Pagina intera: {'Sì' if site.monitor_full_page else 'No'}"
        f"{' (usa /fullpage per attivare)' if site.css_selector and not site.monitor_full_page else ''}\n"
        )
        if site.pdf_analysis:
            checked = len(site.pdf_results)
            found = sum(1 for v in site.pdf_results.values() if v.startswith("found:"))
            text += (
                f"📄 Analisi PDF: attiva\n"
                f"🔑 Cerco: <code>{escape_html(site.pdf_search_value or '')}</code>\n"
                f"📊 PDF analizzati: {checked}, trovati: {found}\n"
            )
            if site.pdf_expected_position:
                name_note = (
                    f" ({escape_html(site.pdf_expected_name)})"
                    if site.pdf_expected_name
                    else ""
                )
                text += f"🎯 Posizione attesa: <b>{escape_html(site.pdf_expected_position)}</b>{name_note}\n"
        if site.whole_site:
            text += f"📚 Intero sito: {len(site.pages)} pagine\n"
        if site.error_count > 0:
            text += f"⚠️ Errori consecutivi: {site.error_count}\n"
        await query.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=site_info_keyboard(key, site.paused, site.url),
        )

    # ──────────────────────────────────────────────────────────────────────
    # Startup / shutdown
    # ──────────────────────────────────────────────────────────────────────

    async def post_init(self, application: Application):
        await application.bot.set_my_commands(self.BOT_COMMANDS)
        logger.info("Menu comandi registrato in Telegram")

        for site in self.sites.values():
            if site.previous_content is None and not site.paused:
                html = await self.scraper.fetch_page(site.url, use_browser=site.use_browser)
                if html:
                    site.previous_content = self.scraper.extract_content(
                        html, site.css_selector, base_url=site.url
                    )
                    site.previous_hash = DiffAnalyzer.compute_hash(site.previous_content)

        await self._repair_duplicate_names()
        self._save_sites()

    async def _repair_duplicate_names(self):
        """
        One-time repair for sites stored with colliding auto-derived names
        (e.g. several 'dettaglio-bando-avviso' that differ only in query string):
        refetch the page and rename from its title; anything still colliding
        gets a short unique suffix. No-op once names are unique.
        """
        counts = Counter(s.name for s in self.sites.values())
        for site in self.sites.values():
            if counts[site.name] > 1:
                html = await self.scraper.fetch_page(site.url, use_browser=site.use_browser)
                title = WebScraper.extract_title(html) if html else ""
                if title:
                    logger.info(f"Rinomino sito '{site.name}' → '{title}'")
                    site.name = title
        counts = Counter(s.name for s in self.sites.values())
        for site in self.sites.values():
            if counts[site.name] > 1:
                site.name = f"{site.name[:23]}-{_site_id(site.key)[:6]}"

    async def post_shutdown(self, application: Application):
        self._save_sites()
        await self.scraper.close()
        logger.info("Bot spento, stato salvato.")

    def run(self):
        builder = Application.builder().token(self.config.bot_token)
        self.app = builder.post_init(self.post_init).post_shutdown(self.post_shutdown).build()

        self.app.add_handler(TypeHandler(Update, self.guard_update), group=-1)

        conv_handler = ConversationHandler(
            entry_points=[CommandHandler("addpdf", self.conv_start)],
            states={
                CONV_URL: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.conv_got_url),
                ],
                CONV_SELECTOR: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.conv_got_selector_text),
                    CallbackQueryHandler(self.conv_got_selector_skip, pattern="^conv_skip_sel$"),
                ],
                CONV_SECTION_ID: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.conv_got_section_text),
                    CallbackQueryHandler(self.conv_got_section_skip, pattern="^conv_skip_section$"),
                ],
                CONV_NAME: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.conv_got_name_text),
                    CallbackQueryHandler(self.conv_got_name_skip, pattern="^conv_skip_name$"),
                ],
                CONV_PDF_FLAG: [
                    CallbackQueryHandler(self.conv_pdf_yes, pattern="^conv_pdf_yes$"),
                    CallbackQueryHandler(self.conv_pdf_no, pattern="^conv_pdf_no$"),
                ],
                CONV_CODICE: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.conv_got_codice),
                ],
                CONV_EXPECT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, self.conv_got_expect_text),
                    CallbackQueryHandler(self.conv_got_expect_skip, pattern="^conv_skip_expect$"),
                ],
            },
            fallbacks=[CommandHandler("cancel", self.conv_cancel)],
            per_user=True,
            per_chat=True,
            per_message=False,
            allow_reentry=True,
        )
        self.app.add_handler(conv_handler, group=0)

        handlers = [
            CommandHandler("start", self.cmd_start),
            CommandHandler("add", self.cmd_add),
            CommandHandler("remove", self.cmd_remove),
            CommandHandler("list", self.cmd_list),
            CommandHandler("check", self.cmd_check),
            CommandHandler("status", self.cmd_status),
            CommandHandler("test", self.cmd_test),
            CommandHandler("pause", self.cmd_pause),
            CommandHandler("resume", self.cmd_resume),
            CommandHandler("rename", self.cmd_rename),
            CommandHandler("threshold", self.cmd_threshold),
            CommandHandler("browser", self.cmd_browser),
            CommandHandler("fullpage", self.cmd_fullpage),
            CommandHandler("selector", self.cmd_selector),
            CommandHandler("code", self.cmd_code),
            CommandHandler("expect", self.cmd_expect),
            CommandHandler("gif", self.cmd_gif),
            CommandHandler("ignore", self.cmd_ignore),
            CommandHandler("unignore", self.cmd_unignore),
            CommandHandler("report", self.cmd_report),
            CommandHandler("help", self.cmd_help),
        ]
        for h in handlers:
            self.app.add_handler(h, group=1)

        self.app.add_handler(CallbackQueryHandler(self.handle_callback), group=1)
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_url), group=1
        )
        self.app.add_handler(
            MessageHandler(
                filters.UpdateType.CHANNEL_POST & filters.TEXT & ~filters.COMMAND,
                self.handle_url,
            ),
            group=1,
        )

        if self.app.job_queue:
            if self.sites:
                self.app.job_queue.run_repeating(
                    self._job_check_all,
                    interval=self.config.check_interval,
                    first=10,
                    name="check_sites",
                )
                logger.info(f"Job monitoraggio avviato per {len(self.sites)} siti")
            if self.config.daily_report_enabled:
                self.app.job_queue.run_daily(
                    self._job_daily_report,
                    time=dt_time(
                        hour=self.config.daily_report_hour,
                        minute=0,
                        tzinfo=ZoneInfo(self.config.timezone),  # naive = UTC per PTB
                    ),
                    name="daily_report",
                )
                logger.info(f"Report giornaliero schedulato alle {self.config.daily_report_hour}:00")
            else:
                logger.info("Report giornaliero automatico disattivato (DAILY_REPORT_ENABLED=false)")

        logger.info("Bot avviato!")
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)
