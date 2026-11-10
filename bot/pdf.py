from __future__ import annotations

import io
import logging
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class PDFAnalyzer:
    """Download PDFs and search for a value in tabular data."""

    @staticmethod
    def search_table(pdf_bytes: bytes, search_value: str) -> Optional[dict]:
        """
        Search for search_value in a column whose header contains 'MECCANOGRAFICO'.
        Returns {'position': str, 'codice': str} if found, None otherwise.

        The column containing 'POSIZIONE' is used to read the rank.
        Both column lookup and value matching are case-insensitive.
        """
        try:
            import pdfplumber
        except ImportError:
            logger.error("pdfplumber non installato. Installa con: pip install pdfplumber")
            return None

        search_upper = search_value.strip().upper()
        try:
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for page in pdf.pages:
                    for table in (page.extract_tables() or []):
                        if not table or len(table) < 2:
                            continue
                        headers = [
                            " ".join(str(h or "").split()).upper() for h in table[0]
                        ]
                        codice_idx = next(
                            (i for i, h in enumerate(headers) if "MECCANOGRAFICO" in h),
                            None,
                        )
                        pos_idx = next(
                            (
                                i
                                for i, h in enumerate(headers)
                                # ACN usa "Graduatoria" come intestazione della posizione
                                if "POSIZIONE" in h or "GRADUATORIA" in h
                            ),
                            None,
                        )
                        if codice_idx is None:
                            continue
                        for row in table[1:]:
                            if not row or len(row) <= codice_idx:
                                continue
                            codice = " ".join(str(row[codice_idx] or "").split()).upper()
                            if codice == search_upper:
                                position = (
                                    str(row[pos_idx] or "").strip()
                                    if pos_idx is not None and len(row) > pos_idx
                                    else "N/A"
                                )
                                return {"position": position, "codice": search_value.strip()}
        except Exception as e:
            logger.error(f"Errore parsing PDF: {e}")
        return None

    @staticmethod
    def _extract_name(row: list, pos_idx: Optional[int], codice_idx: Optional[int]) -> str:
        """
        Best-effort candidate name from a ranking row, excluding the position and
        code columns, pure-number cells (scores/dates), and redaction markers
        (OMISSIS/OMISSATO, incl. line-broken variants). Empty string if the name
        is not shown (redacted row).
        """
        parts = []
        for i, cell in enumerate(row):
            if i in (pos_idx, codice_idx):
                continue
            txt = " ".join(str(cell or "").split())
            if not txt:
                continue
            letters = "".join(c for c in txt.upper() if c.isalpha())
            if len(letters) < 2:
                continue  # scores, dates, single chars
            if "OMISS" in letters or "MISSIS" in letters:
                continue  # redaction marker, not a name
            parts.append(txt)
        return " ".join(parts)

    @staticmethod
    def _walk_ranking_rows(pdf_bytes: bytes):
        """Yield (row, pos_idx, codice_idx) for every ranking-table row in the PDF."""
        try:
            import pdfplumber
        except ImportError:
            logger.error("pdfplumber non installato.")
            return
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                for table in (page.extract_tables() or []):
                    if not table or len(table) < 2:
                        continue
                    headers = [" ".join(str(h or "").split()).upper() for h in table[0]]
                    pos_idx = next(
                        (i for i, h in enumerate(headers) if "POSIZIONE" in h or "GRADUATORIA" in h),
                        None,
                    )
                    codice_idx = next(
                        (i for i, h in enumerate(headers) if "MECCANOGRAFICO" in h),
                        None,
                    )
                    for row in table[1:]:
                        if row:
                            yield row, pos_idx, codice_idx

    @staticmethod
    def find_code_row(pdf_bytes: bytes, code: str) -> Optional[dict]:
        """
        Locate the ranking row holding `code` (the reliable anchor: the code is
        always present and stable). Returns:
          {'position': str, 'codice': str, 'name': str, 'name_shown': bool, 'text': str}
        or None if the code is not in any ranking table.
        """
        want = code.strip().upper()
        try:
            for row, pos_idx, codice_idx in PDFAnalyzer._walk_ranking_rows(pdf_bytes):
                if codice_idx is None or len(row) <= codice_idx:
                    continue
                cell = " ".join(str(row[codice_idx] or "").split()).upper()
                if cell == want:
                    position = (
                        str(row[pos_idx] or "").strip()
                        if pos_idx is not None and len(row) > pos_idx
                        else ""
                    )
                    name = PDFAnalyzer._extract_name(row, pos_idx, codice_idx)
                    text = " | ".join(" ".join(str(c).split()) for c in row if c)
                    return {
                        "position": position,
                        "codice": code.strip(),
                        "name": name,
                        "name_shown": bool(name),
                        "text": text,
                    }
        except Exception as e:
            logger.error(f"Errore ricerca codice {code} nel PDF: {e}")
        return None

    @staticmethod
    def row_at_position(pdf_bytes: bytes, position: str) -> Optional[dict]:
        """Row at a given ranking position. Returns {'codice','name','name_shown','text'} or None."""
        target = str(position).strip()
        try:
            for row, pos_idx, codice_idx in PDFAnalyzer._walk_ranking_rows(pdf_bytes):
                if pos_idx is None or len(row) <= pos_idx:
                    continue
                if str(row[pos_idx] or "").strip() == target:
                    codice = ""
                    if codice_idx is not None and len(row) > codice_idx:
                        codice = " ".join(str(row[codice_idx] or "").split()).upper()
                    name = PDFAnalyzer._extract_name(row, pos_idx, codice_idx)
                    text = " | ".join(" ".join(str(c).split()) for c in row if c)
                    return {"codice": codice, "name": name, "name_shown": bool(name), "text": text}
        except Exception as e:
            logger.error(f"Errore lettura posizione {position} nel PDF: {e}")
        return None

    @staticmethod
    def doc_name(url: str) -> str:
        """Extract filename from URL (handles Liferay …/nome.pdf/<uuid> paths)."""
        segments = [s for s in urlparse(url).path.split("/") if s]
        for seg in reversed(segments):
            if seg.lower().endswith(".pdf"):
                return seg
        return segments[-1] if segments else url
