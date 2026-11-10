from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from urllib.parse import urlparse


@dataclass
class MonitoredSite:
    """Represents a monitored web page."""

    url: str
    name: str = ""
    section_id: Optional[str] = None
    previous_content: Optional[str] = None
    previous_hash: Optional[str] = None
    last_check: Optional[str] = None
    css_selector: Optional[str] = None
    paused: bool = False
    error_count: int = 0
    error_notified: bool = False
    use_browser: bool = False
    threshold: Optional[float] = None
    # PDF analysis
    pdf_analysis: bool = False
    pdf_search_value: Optional[str] = None
    pdf_results: dict = field(default_factory=dict)
    # Expected ranking slot: alert if the PDF updates and this position
    # is no longer held by pdf_search_value / pdf_expected_name
    pdf_expected_position: Optional[str] = None
    pdf_expected_name: Optional[str] = None
    # content hash -> ISO timestamp of the notification that carried it
    notified_hashes: dict = field(default_factory=dict)
    # Full-page monitoring alongside a css_selector
    monitor_full_page: bool = False
    previous_full_content: Optional[str] = None
    previous_full_hash: Optional[str] = None
    # /add URL --whole: every page under url, page url -> cleaned content
    whole_site: bool = False
    pages: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.name:
            parsed = urlparse(self.url)
            path_part = (
                parsed.path.rstrip("/").split("/")[-1]
                if parsed.path.rstrip("/")
                else parsed.netloc
            )
            name = path_part[:30] or parsed.netloc[:30] or "Pagina"
            # Generic slugs (es. /dettaglio-bando-avviso/?concorso_id=…) differ only
            # in the query string: append a short digest so names stay unique.
            if parsed.query:
                name = f"{name[:23]}-{hashlib.md5(parsed.query.encode()).hexdigest()[:6]}"
            self.name = name

    @property
    def key(self) -> str:
        """Composite identifier: url plus optional section_id for multi-section monitoring."""
        return f"{self.url}#{self.section_id}" if self.section_id else self.url

    @property
    def last_check_dt(self) -> Optional[datetime]:
        if self.last_check:
            try:
                return datetime.fromisoformat(self.last_check)
            except (ValueError, TypeError):
                return None
        return None

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "name": self.name,
            "section_id": self.section_id,
            "previous_content": self.previous_content,
            "previous_hash": self.previous_hash,
            "last_check": self.last_check,
            "css_selector": self.css_selector,
            "paused": self.paused,
            "error_count": self.error_count,
            "error_notified": self.error_notified,
            "use_browser": self.use_browser,
            "threshold": self.threshold,
            "pdf_analysis": self.pdf_analysis,
            "pdf_search_value": self.pdf_search_value,
            "pdf_results": self.pdf_results,
            "pdf_expected_position": self.pdf_expected_position,
            "pdf_expected_name": self.pdf_expected_name,
            "notified_hashes": self.notified_hashes,
            "monitor_full_page": self.monitor_full_page,
            "previous_full_content": self.previous_full_content,
            "previous_full_hash": self.previous_full_hash,
            "whole_site": self.whole_site,
            "pages": self.pages,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MonitoredSite":
        return cls(
            url=data["url"],
            name=data.get("name", ""),
            section_id=data.get("section_id"),
            previous_content=data.get("previous_content"),
            previous_hash=data.get("previous_hash"),
            last_check=data.get("last_check"),
            css_selector=data.get("css_selector"),
            paused=data.get("paused", False),
            error_count=data.get("error_count", 0),
            error_notified=data.get("error_notified", False),
            use_browser=data.get("use_browser", False),
            threshold=data.get("threshold"),
            pdf_analysis=data.get("pdf_analysis", False),
            pdf_search_value=data.get("pdf_search_value"),
            pdf_results=data.get("pdf_results", {}),
            pdf_expected_position=data.get("pdf_expected_position"),
            pdf_expected_name=data.get("pdf_expected_name"),
            notified_hashes=data.get("notified_hashes", {}),
            monitor_full_page=data.get("monitor_full_page", False),
            previous_full_content=data.get("previous_full_content"),
            previous_full_hash=data.get("previous_full_hash"),
            whole_site=data.get("whole_site", False),
            pages=data.get("pages", {}),
        )
