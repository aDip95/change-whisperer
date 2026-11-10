from __future__ import annotations

import asyncio
import logging
import re
import socket
from ipaddress import ip_address
from typing import Optional
from urllib.parse import urldefrag, urljoin, urlparse

import aiohttp
from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver
from bs4 import BeautifulSoup

from bot.config import Config
from bot.utils import is_valid_url

logger = logging.getLogger(__name__)

DOC_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".csv")
# Link che non sono pagine HTML: /add --whole non li scarica.
NON_PAGE_EXTENSIONS = DOC_EXTENSIONS + (
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".zip", ".rar", ".7z",
    ".mp3", ".mp4", ".xml", ".json", ".css", ".js", ".txt", ".p7m",
)


class PublicResolver(AbstractResolver):
    """Resolver used by aiohttp itself, so validated IPs are the connected IPs."""

    def __init__(self, delegate=None):
        self.delegate = delegate

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_UNSPEC):
        # Created lazily: aiohttp >= 3.14 needs a running loop to build it.
        self.delegate = self.delegate or DefaultResolver()
        records = await self.delegate.resolve(host, port, family)
        try:
            if not records or any(
                not ip_address(record["host"].split("%", 1)[0]).is_global
                for record in records
            ):
                raise OSError(f"Host non pubblico: {host}")
        except ValueError as exc:
            raise OSError(f"Indirizzo non valido per {host}") from exc
        return records

    async def close(self):
        if self.delegate:
            await self.delegate.close()


class WebScraper:
    """Async HTTP fetcher with Playwright fallback and page content extractor."""

    def __init__(self, config: Config):
        self.config = config
        self._session: Optional[aiohttp.ClientSession] = None
        self._resolver = PublicResolver()
        self._playwright = None
        self._browser = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={
                    "User-Agent": self.config.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
                },
                timeout=aiohttp.ClientTimeout(total=self.config.request_timeout),
                connector=aiohttp.TCPConnector(resolver=self._resolver),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def _validate_public_target(self, url: str):
        if not is_valid_url(url):
            raise ValueError(f"URL non pubblico o non valido: {url}")
        parsed = urlparse(url)
        try:
            await self._resolver.resolve(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
        except OSError as exc:
            raise ValueError(str(exc)) from exc

    async def _request_public(self, url: str, **kwargs) -> aiohttp.ClientResponse:
        """GET following redirects by hand, so every hop is validated."""
        session = await self._get_session()
        current = url
        for _ in range(6):
            if not is_valid_url(current):
                raise ValueError(f"URL non pubblico o non valido: {current}")
            response = await session.get(current, allow_redirects=False, **kwargs)
            if response.status not in (301, 302, 303, 307, 308):
                return response
            location = response.headers.get("Location")
            response.release()
            if not location:
                raise ValueError(f"Redirect senza Location: {current}")
            current = urljoin(current, location)
        raise ValueError(f"Troppi redirect: {url}")

    async def _fetch_aiohttp(self, url: str) -> Optional[str]:
        for attempt in range(self.config.max_retries):
            try:
                response = await self._request_public(url)
                async with response:
                    if response.status == 403:
                        logger.warning(f"403 Forbidden per {url} con aiohttp")
                        return None
                    response.raise_for_status()
                    return await response.text()
            except Exception as e:
                logger.warning(
                    f"Tentativo {attempt + 1}/{self.config.max_retries} fallito per {url}: {e}"
                )
                if attempt < self.config.max_retries - 1:
                    await asyncio.sleep(2**attempt)
        return None

    async def _fetch_playwright(self, url: str) -> Optional[str]:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            logger.error(
                "Playwright non installato. Installa con: "
                "pip install playwright && playwright install chromium"
            )
            return None

        try:
            await self._validate_public_target(url)
            # Launch may fail or Chromium may crash: retry instead of keeping a dead handle.
            if not self._browser or not self._browser.is_connected():
                if not self._playwright:
                    self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(headless=True)

            context = await self._browser.new_context(
                locale="it-IT",
                user_agent=self.config.user_agent,
                service_workers="block",
            )
            page = await context.new_page()
            try:
                # ponytail: best effort — Chromium risolve il DNS da sé (rebinding
                # possibile). Ok con allowlist; per multi-utente pubblico il branch
                # codex/public-release-hardening toglie del tutto il browser.
                async def guard(route, request):
                    if request.url.startswith(("http://", "https://")):
                        try:
                            await self._validate_public_target(request.url)
                        except Exception:
                            logger.warning(f"Richiesta browser privata bloccata: {request.url}")
                            await route.abort()
                            return
                    await route.continue_()

                await page.route("**/*", guard)

                async def block_websocket(websocket):
                    await websocket.close(code=1008, reason="WebSocket disabled")

                await page.route_web_socket("**/*", block_websocket)
                resp = await page.goto(url, wait_until="networkidle", timeout=60000)
                if resp and resp.status != 200:
                    logger.warning(f"Playwright status {resp.status} per {url}")
                    return None
                await page.wait_for_timeout(2000)
                return await page.content()
            finally:
                await context.close()
        except Exception as e:
            logger.error(f"Errore Playwright per {url}: {e}")
            return None

    async def fetch_page(self, url: str, use_browser: bool = False) -> Optional[str]:
        """Site's own mode only: browser and aiohttp HTML differ, mixing them fakes a change."""
        if use_browser:
            logger.info(f"Uso Playwright (modalità browser) per {url}")
            return await self._fetch_playwright(url)
        return await self._fetch_aiohttp(url)

    async def download_bytes(self, url: str) -> Optional[bytes]:
        """Download binary content (e.g. PDF)."""
        try:
            response = await self._request_public(
                url, timeout=aiohttp.ClientTimeout(total=60)
            )
            async with response as resp:
                if resp.status == 200:
                    return await resp.read()
                logger.warning(f"Download binario status {resp.status}: {url}")
        except Exception as e:
            logger.warning(f"Download binario fallito {url}: {e}")
        return None

    @staticmethod
    def extract_title(html: str) -> str:
        """Human-readable page name: first h1, else <title>. Empty string if none."""
        soup = BeautifulSoup(html, "html.parser")
        h1 = soup.find("h1")
        text = h1.get_text(strip=True) if h1 else ""
        if not text and soup.title:
            text = soup.title.get_text(strip=True)
        return " ".join(text.split())[:60]

    @staticmethod
    def extract_content(
        html: str, css_selector: Optional[str] = None, base_url: str = ""
    ) -> str:
        """
        Extract text from a page, optionally scoped to a CSS selector.
        Includes link hrefs in the fingerprint so new buttons/links are detected
        even when visible text changes minimally.
        """
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
            tag.decompose()

        if css_selector:
            element = soup.select_one(css_selector)
            if element:
                return WebScraper._build_composite(element, base_url)

        main_re = re.compile(r"content|main|body", re.I)
        candidates = [
            soup.find("main"),
            soup.find("article"),
            soup.select_one("[class*='web-content']"),
            *soup.find_all(id=main_re),
            *soup.find_all(class_=main_re),
            soup.select_one(".journal-content-article"),
        ]
        # ponytail: under 200 chars it's a nav wrapper, not content (Banca d'Italia:
        # div#main-nav-wrapper matched before div#main and hid every change).
        # Genuinely short content falls back to body: noisier, never blind.
        root = next(
            (c for c in candidates if c is not None and len(c.get_text(strip=True)) >= 200),
            soup.body or soup,
        )
        return WebScraper._build_composite(root, base_url)

    @staticmethod
    def _build_composite(element, base_url: str = "") -> str:
        """Build a composite fingerprint: text + sorted link hrefs."""
        text = element.get_text(separator="\n", strip=True)
        links = set()
        for a in element.find_all("a", href=True):
            href = a["href"].strip()
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                if base_url:
                    href = urljoin(base_url, href)
                links.add(href)
        if links:
            return f"{text}\n\n__LINKS__\n" + "\n".join(sorted(links))
        return text

    @staticmethod
    def _doc_extension(href: str, extensions: tuple[str, ...]) -> Optional[str]:
        """
        Extension of a document link, or None.
        Handles Liferay-style URLs where the file name is a middle path
        segment: /documents/…/nome.pdf/<uuid>?t=<ts>
        """
        path = href.lower().split("?")[0].split("#")[0]
        for ext in extensions:
            if path.endswith(ext) or f"{ext}/" in path:
                return ext
        return None

    @staticmethod
    def extract_documents(html: str, base_url: str) -> list[dict]:
        """Extract document links (PDF, DOC, etc.)."""
        soup = BeautifulSoup(html, "html.parser")
        docs = []
        for a in soup.find_all("a", href=True):
            ext = WebScraper._doc_extension(a["href"], DOC_EXTENSIONS)
            if ext:
                full_url = urljoin(base_url, a["href"])
                docs.append({
                    "url": full_url,
                    "text": a.get_text(strip=True) or "Documento",
                    "type": ext[1:].upper(),
                })

        seen: set[str] = set()
        unique = []
        for d in docs:
            if d["url"] not in seen:
                seen.add(d["url"])
                unique.append(d)
        return unique

    @staticmethod
    def extract_page_links(html: str, base_url: str, scope: str) -> list[str]:
        """Links to HTML pages on scope's host whose URL starts with scope (/add --whole)."""
        host = urlparse(scope).netloc
        links = []
        for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
            url = urldefrag(urljoin(base_url, a["href"].strip()))[0]
            if (
                urlparse(url).netloc == host
                and url.startswith(scope)
                and not WebScraper._doc_extension(url, NON_PAGE_EXTENSIONS)
            ):
                links.append(url)
        return list(dict.fromkeys(links))

    @staticmethod
    def extract_pdf_links(
        html: str, base_url: str, css_selector: Optional[str] = None
    ) -> list[str]:
        """
        Extract PDF links from a page, optionally scoped to a CSS selector.
        If a selector is given but matches nothing, returns [] (does NOT fall
        back to the whole page): a broken scope must not silently pull in PDFs
        from other sections and trigger cross-section false alarms.
        """
        soup = BeautifulSoup(html, "html.parser")
        root = soup
        if css_selector:
            element = soup.select_one(css_selector)
            if element is None:
                logger.warning(
                    f"Selettore '{css_selector}' non trova nulla: nessun PDF analizzato "
                    f"(scope non risolto, evito fallback all'intera pagina)"
                )
                return []
            root = element

        links: list[str] = []
        seen: set[str] = set()
        for a in root.find_all("a", href=True):
            href = a["href"].strip()
            if WebScraper._doc_extension(href, (".pdf",)):
                full_url = urljoin(base_url, href)
                if full_url not in seen:
                    seen.add(full_url)
                    links.append(full_url)
        return links
