from __future__ import annotations

import hashlib
import logging
import re
from difflib import SequenceMatcher
from typing import Optional

from bot.utils import escape_html, truncate

logger = logging.getLogger(__name__)

# Righe di contorno che compaiono e scompaiono da sole (box di ricerca, separatori):
# senza filtro ogni oscillazione genera una notifica. Estendibile a runtime con /ignore.
# ponytail: regex applicate riga per riga, case-insensitive. Una stringa semplice
# funziona come regex, quindi l'utente può passare anche solo un pezzo di testo.
NOISE_DEFAULTS = [
    r"^\|$",
    r"^cerca nel sito$",
    r"/ricerca/?$",
]


def strip_noise(content: Optional[str], patterns: list[str]) -> Optional[str]:
    """Remove boilerplate lines so they never enter the hash/diff. None stays None."""
    if not content or not patterns:
        return content
    regexes = []
    for p in patterns:
        try:
            regexes.append(re.compile(p, re.I))
        except re.error as e:
            logger.warning(f"Pattern /ignore non valido, saltato: {p!r} ({e})")
    if not regexes:
        return content
    kept = [
        line
        for line in content.splitlines()
        if not any(r.search(line.strip()) for r in regexes)
    ]
    return "\n".join(kept)


class DiffAnalyzer:
    """Computes and summarises differences between page content versions."""

    # ponytail: full-rewrite heuristic — a CMS republish rewrites ~everything;
    # tune if legit large updates get misclassified.
    REWRITE_RATIO = 0.9

    @staticmethod
    def classify_change(old_content: str, new_content: str, ratio: float) -> str:
        """
        'wiped'     → content was emptied (page cleared, likely mid-republish)
        'rewritten' → content restored after a wipe, or ~fully replaced in one step
        'update'    → normal change
        """
        old_s, new_s = old_content.strip(), new_content.strip()
        if old_s and not new_s:
            return "wiped"
        if (new_s and not old_s) or ratio >= DiffAnalyzer.REWRITE_RATIO:
            return "rewritten"
        return "update"

    @staticmethod
    def compute_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def has_changed(old_content: Optional[str], new_content: str) -> bool:
        if old_content is None:
            return False
        return DiffAnalyzer.compute_hash(old_content) != DiffAnalyzer.compute_hash(new_content)

    @staticmethod
    def get_change_ratio(old_content: str, new_content: str) -> float:
        return 1.0 - SequenceMatcher(None, old_content, new_content).ratio()

    @staticmethod
    def get_similarity_ratio(old_content: str, new_content: str) -> float:
        return SequenceMatcher(None, old_content, new_content).ratio()

    @staticmethod
    def summarize_changes(
        old_content: str, new_content: str, max_length: int = 3000
    ) -> str:
        """Generate an HTML diff summary for Telegram."""
        old_lines = old_content.splitlines()
        new_lines = new_content.splitlines()
        old_set = set(old_lines)
        new_set = set(new_lines)

        added = [l for l in new_lines if l not in old_set and l.strip()]
        removed = [l for l in old_lines if l not in new_set and l.strip()]

        similarity = DiffAnalyzer.get_similarity_ratio(old_content, new_content)
        parts = [f"📊 Similarità: {similarity:.1%}"]

        if added:
            parts.append(f"\n✅ <b>Contenuto aggiunto ({len(added)} righe):</b>")
            for line in added[:10]:
                parts.append(f"  • {escape_html(truncate(line))}")
            if len(added) > 10:
                parts.append(f"  <i>… e altre {len(added) - 10} righe</i>")

        if removed:
            parts.append(f"\n❌ <b>Contenuto rimosso ({len(removed)} righe):</b>")
            for line in removed[:5]:
                parts.append(f"  • {escape_html(truncate(line))}")
            if len(removed) > 5:
                parts.append(f"  <i>… e altre {len(removed) - 5} righe</i>")

        if not added and not removed:
            parts.append("\n⚙️ <i>Cambiamento rilevato nella struttura della pagina</i>")

        summary = "\n".join(parts)
        if len(summary) > max_length:
            summary = summary[: max_length - 20] + "\n\n<i>[…troncato]</i>"
        return summary
