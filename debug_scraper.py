#!/usr/bin/env python3
"""
Page Scraping Diagnostic Tool
=============================
Mostra come aiohttp + BeautifulSoup "vedono" una pagina web e aiuta a capire
perché le notifiche di cambiamento potrebbero non scattare (403, contenuto
renderizzato lato client, soglia troppo alta, ecc.).

Usage:
    python debug_scraper.py URL
    python debug_scraper.py https://www.example.com

Requirements:
    pip install aiohttp beautifulsoup4 playwright
    playwright install chromium
"""

import asyncio
import hashlib
import re
import sys
from difflib import SequenceMatcher
from typing import Optional

# ─── CONFIG ──────────────────────────────────────────────────────────────────
CHANGE_THRESHOLD = 0.01  # 1%

AIOHTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
}

SEP = "=" * 70


# ─── BOT'S ORIGINAL EXTRACTION (reproduced) ─────────────────────────────────
def extract_content_original(html: str, css_selector: Optional[str] = None) -> str:
    """Exact replica of the bot's extract_content method."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    if css_selector:
        element = soup.select_one(css_selector)
        if element:
            return element.get_text(separator="\n", strip=True)
    main_content = (
        soup.find("main")
        or soup.find("article")
        or soup.find(class_=re.compile(r"content|main|body", re.I))
        or soup.find(id=re.compile(r"content|main|body", re.I))
        or soup.body
    )
    if main_content:
        return main_content.get_text(separator="\n", strip=True)
    return soup.get_text(separator="\n", strip=True)


# ─── IMPROVED EXTRACTION (recommended fix) ───────────────────────────────────
def extract_content_improved(html: str, base_url: str = "") -> dict:
    """
    Improved extraction that captures:
    - Text content
    - All href links (buttons, documents, anchors)
    - Data attributes and dynamic content markers
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "html.parser")

    # Remove noise
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    # Try to find the main content area (Liferay-aware selectors)
    content_el = (
        soup.select_one(".portlet-body")                       # Liferay portlet body
        or soup.select_one("[class*='journal-content']")       # Liferay journal content
        or soup.select_one("[class*='web-content']")           # Liferay web content
        or soup.find("main")
        or soup.find("article")
        or soup.find(class_=re.compile(r"content|main|body", re.I))
        or soup.find(id=re.compile(r"content|main|body", re.I))
        or soup.body
        or soup
    )

    # Extract text
    text = content_el.get_text(separator="\n", strip=True)

    # Extract ALL links with their text and href
    links = []
    for a in content_el.find_all("a", href=True):
        href = a["href"]
        if base_url:
            href = urljoin(base_url, href)
        link_text = a.get_text(strip=True)
        links.append(f"{link_text}|{href}")

    # Extract buttons (even non-<a> ones)
    buttons = []
    for btn in content_el.find_all(["button", "a"], class_=re.compile(r"btn|button", re.I)):
        btn_text = btn.get_text(strip=True)
        btn_href = btn.get("href", "")
        if base_url and btn_href:
            btn_href = urljoin(base_url, btn_href)
        buttons.append(f"{btn_text}|{btn_href}")

    # Build a composite fingerprint: text + sorted links
    link_block = "\n".join(sorted(set(links)))
    composite = f"{text}\n---LINKS---\n{link_block}"

    return {
        "text": text,
        "links": links,
        "buttons": buttons,
        "composite": composite,
        "composite_hash": hashlib.sha256(composite.encode()).hexdigest(),
    }


# ─── TEST 1: aiohttp fetch ──────────────────────────────────────────────────
async def test_aiohttp(url: str):
    import aiohttp

    print(f"\n{SEP}")
    print("[TEST 1] aiohttp fetch (your bot's method)")
    print(SEP)

    try:
        async with aiohttp.ClientSession(
            headers=AIOHTTP_HEADERS,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as session:
            async with session.get(url) as resp:
                print(f"  Status: {resp.status}")
                print(f"  Headers: Server={resp.headers.get('Server', '?')}, "
                      f"Content-Type={resp.headers.get('Content-Type', '?')}")
                if resp.status == 200:
                    html = await resp.text()
                    print(f"  HTML length: {len(html)} chars")
                    return html
                elif resp.status == 403:
                    body = await resp.text()
                    print(f"  ❌ 403 FORBIDDEN")
                    print(f"  Response body (first 300 chars):\n    {body[:300]}")
                    return None
                else:
                    print(f"  Unexpected status {resp.status}")
                    return None
    except Exception as e:
        print(f"  ❌ Connection error: {e}")
        return None


# ─── TEST 2: Playwright fetch (browser rendering) ───────────────────────────
async def test_playwright(url: str):
    print(f"\n{SEP}")
    print("[TEST 2] Playwright fetch (real browser rendering)")
    print(SEP)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  ⚠ Playwright not installed. Run:")
        print("    pip install playwright && playwright install chromium")
        return None

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                locale="it-IT",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            page = await context.new_page()
            resp = await page.goto(url, wait_until="networkidle", timeout=60000)
            print(f"  Status: {resp.status if resp else 'N/A'}")

            # Wait for dynamic content
            await page.wait_for_timeout(3000)

            html = await page.content()
            print(f"  HTML length: {len(html)} chars")

            # Take screenshot for visual comparison
            await page.screenshot(path="debug_screenshot.png", full_page=True)
            print("  📸 Screenshot saved: debug_screenshot.png")

            await browser.close()
            return html
    except Exception as e:
        print(f"  ❌ Playwright error: {e}")
        return None


# ─── TEST 3: Analyze HTML structure ─────────────────────────────────────────
def analyze_structure(html: str, source_label: str):
    from bs4 import BeautifulSoup

    print(f"\n{SEP}")
    print(f"[TEST 3] HTML Structure Analysis ({source_label})")
    print(SEP)

    soup = BeautifulSoup(html, "html.parser")

    # JS framework detection
    print("\n  [3a] JavaScript Framework Detection:")
    for fwid in ["root", "app", "__next", "__nuxt"]:
        el = soup.find(id=fwid)
        if el:
            inner_len = len(el.get_text(strip=True))
            print(f"    Found <div id='{fwid}'> — inner text: {inner_len} chars")
            if inner_len < 100:
                print("    ⚠ Possibly client-side rendered (minimal server content)")

    # Liferay detection
    liferay_classes = soup.find_all(class_=re.compile(r"portlet|liferay|journal|web-content", re.I))
    print(f"    Liferay markers: {len(liferay_classes)} elements")
    for el in liferay_classes[:5]:
        classes = " ".join(el.get("class", []))[:80]
        print(f"      <{el.name} class=\"{classes}\">")

    # What the bot matches for content
    print("\n  [3b] Bot's content-matching logic:")
    soup2 = BeautifulSoup(html, "html.parser")
    for tag in soup2(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()

    candidates = [
        ("soup.find('main')", soup2.find("main")),
        ("soup.find('article')", soup2.find("article")),
        ("class=content|main|body", soup2.find(class_=re.compile(r"content|main|body", re.I))),
        ("id=content|main|body", soup2.find(id=re.compile(r"content|main|body", re.I))),
        ("soup.body", soup2.body),
    ]
    for label, el in candidates:
        if el:
            txt_len = len(el.get_text(strip=True))
            tag_info = f"<{el.name}"
            if el.get("class"):
                tag_info += f" class=\"{' '.join(el['class'])[:60]}\""
            if el.get("id"):
                tag_info += f" id=\"{el['id']}\""
            tag_info += ">"
            print(f"    ✅ {label} → {tag_info} ({txt_len} chars)")
            break
        else:
            print(f"    ❌ {label} → not found")

    # Keyword content search (customise the keywords for your use case)
    print("\n  [3c] Keyword Content Search:")
    keywords = ["esit", "risultat", "graduatoria", "pubblicat", "aggiornament",
                "nuovo", "scadenza", "documento"]
    for kw in keywords:
        matches = soup.find_all(string=re.compile(kw, re.I))
        if matches:
            print(f"    '{kw}': {len(matches)} occurrence(s)")
            for m in matches[:2]:
                parent = m.parent
                print(f"      in <{parent.name}> → \"{str(m).strip()[:80]}\"")

    # Links and buttons analysis
    print("\n  [3d] Links & Buttons:")
    all_links = soup.find_all("a", href=True)
    print(f"    Total <a> tags: {len(all_links)}")

    pdf_links = [a for a in all_links if ".pdf" in a.get("href", "").lower()]
    print(f"    PDF links: {len(pdf_links)}")
    for a in pdf_links[:5]:
        print(f"      [{a.get_text(strip=True)[:50]}] → {a['href'][:80]}")

    btn_elements = soup.find_all(class_=re.compile(r"btn|button", re.I))
    print(f"    Button-styled elements: {len(btn_elements)}")
    for b in btn_elements[:10]:
        txt = b.get_text(strip=True)[:60]
        href = b.get("href", "N/A")[:60]
        print(f"      <{b.name}> '{txt}' href={href}")


# ─── TEST 4: Change Ratio Simulation ────────────────────────────────────────
def simulate_change_ratio(html: str, url: str):
    print(f"\n{SEP}")
    print("[TEST 4] Change Ratio Simulation")
    print(SEP)

    text = extract_content_original(html)
    print(f"  Extracted text length: {len(text)} chars, {len(text.split())} words")

    # Simulate various sizes of new content
    scenarios = [
        ("1 short label", "\nNuovo"),
        ("2 short labels", "\nNuovo documento A\nNuovo documento B"),
        ("4 lines + PDF links",
         "\nNuovo documento A\nNuovo documento B\n"
         "Scarica allegato A (PDF)\nScarica allegato B (PDF)"),
        ("10 new lines",
         "\n".join(f"\nNuova voce {i + 1} - dettagli aggiornati"
                   for i in range(10))),
    ]

    for label, addition in scenarios:
        new_text = text + addition
        ratio = 1.0 - SequenceMatcher(None, text, new_text).ratio()
        triggers = "✅ YES" if ratio >= CHANGE_THRESHOLD else "❌ NO"
        print(f"\n  Scenario: {label}")
        print(f"    Added chars: {len(addition)}")
        print(f"    Change ratio: {ratio:.4f} ({ratio:.2%})")
        print(f"    Exceeds {CHANGE_THRESHOLD:.0%} threshold? {triggers}")

    # Now test with composite (text + links)
    print(f"\n  --- With composite extraction (text + links) ---")
    improved = extract_content_improved(html, url)
    old_composite = improved["composite"]
    # Simulate adding a new PDF link
    from urllib.parse import urljoin
    new_link = f"\nNuovo allegato|{urljoin(url, '/documents/nuovo_allegato.pdf')}"
    new_composite = old_composite + new_link
    ratio = 1.0 - SequenceMatcher(None, old_composite, new_composite).ratio()
    print(f"    New link added to composite fingerprint")
    print(f"    Change ratio: {ratio:.4f} ({ratio:.2%})")
    print(f"    Exceeds {CHANGE_THRESHOLD:.0%} threshold? "
          f"{'✅ YES' if ratio >= CHANGE_THRESHOLD else '❌ NO'}")


# ─── MAIN ────────────────────────────────────────────────────────────────────
async def main():
    if len(sys.argv) < 2:
        print("Usage: python debug_scraper.py URL")
        print("Esempio: python debug_scraper.py https://www.example.com")
        sys.exit(1)
    url = sys.argv[1]

    print(f"\n{'#' * 70}")
    print(f"#  Page Scraping Diagnostic Tool")
    print(f"#  URL: {url}")
    print(f"{'#' * 70}")

    # Test 1: aiohttp
    aiohttp_html = await test_aiohttp(url)

    if aiohttp_html is None:
        print("\n" + "!" * 70)
        print("! aiohttp non ha restituito contenuto (es. 403 Forbidden).")
        print("! Alcuni siti bloccano i client HTTP non-browser: in quel caso")
        print("! il bot non vede mai il contenuto e nessun diff viene calcolato.")
        print("!" * 70)
        print("\n  → Soluzione: usa il fallback Playwright (/browser URL nel bot).\n")

    # Test 2: Playwright
    pw_html = await test_playwright(url)

    # Use whichever HTML we got for structural analysis
    html = aiohttp_html or pw_html
    if html is None:
        print("\n❌ Impossibile scaricare la pagina con nessun metodo.")
        print("   Se sei in locale, installa playwright:")
        print("     pip install playwright && playwright install chromium")
        return

    source = "aiohttp" if aiohttp_html else "Playwright"
    analyze_structure(html, source)
    simulate_change_ratio(html, url)

    # Summary
    print(f"\n{'=' * 70}")
    print("SUMMARY & RECOMMENDATIONS")
    print(f"{'=' * 70}")
    print("""
    1. 403 / blocco lato server:
       Alcuni siti (es. portali Liferay) restituiscono 403 ad aiohttp.
       → FIX: usa Playwright come fallback quando aiohttp fallisce
         (il bot lo fa automaticamente, o forzalo con /browser URL).

    2. Estrazione contenuto troppo larga:
       Il match su class regex (content|main|body) può catturare un wrapper
       troppo in alto nel DOM, diluendo il segnale.
       → FIX: usa un selettore CSS mirato (es. .portlet-body, #results,
         .journal-content-article) quando aggiungi l'URL al bot.

    3. Soglia vs. testo breve:
       Aggiungere poche etichette corte (<100 char) a una pagina con
       migliaia di char di boilerplate può restare sotto la soglia.
       → FIX: includi gli href nel fingerprint (composite hash),
         abbassa la soglia per quell'URL, o usa un selettore CSS.

    4. Comando bot consigliato:
       /add URL NomeSito .selettore-css
       (con backend Playwright attivo se il sito blocca aiohttp)
    """)


if __name__ == "__main__":
    asyncio.run(main())
