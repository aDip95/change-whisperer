#!/usr/bin/env python3
"""
Test & debug script per Web Monitor Bot.

Usage:
    python test_bot.py              # tutti i test
    python test_bot.py pdf          # solo parsing PDF
    python test_bot.py scrape       # solo scraping web
    python test_bot.py monitor      # logica di monitoraggio (dry run)
    python test_bot.py offline      # tutto tranne la rete (usato dalla CI)

Requisiti: .venv attivo, pdfplumber installato.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
from pathlib import Path

import logging
logging.disable(logging.CRITICAL)  # silencia tutto il logging durante i test

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ──────────────────────────────────────────────────────────────────────────────
# Helpers di output
# ──────────────────────────────────────────────────────────────────────────────

PASS = "✅ PASS"
FAIL = "❌ FAIL"
INFO = "ℹ️  INFO"
SEP  = "─" * 60

def ok(label: str, detail: str = ""):
    print(f"  {PASS}  {label}" + (f"  →  {detail}" if detail else ""))

def fail(label: str, detail: str = ""):
    print(f"  {FAIL}  {label}" + (f"  →  {detail}" if detail else ""))
    _failures.append(label)

def info(label: str):
    print(f"  {INFO}  {label}")

_failures: list[str] = []


# ──────────────────────────────────────────────────────────────────────────────
# TEST 1 — PDF parsing
# ──────────────────────────────────────────────────────────────────────────────

def test_pdf_parsing():
    print(f"\n{SEP}")
    print("TEST 1 — Parsing PDF con pdfplumber")
    print(SEP)

    try:
        import pdfplumber
        ok("pdfplumber importato")
    except ImportError:
        fail("pdfplumber non installato", "pip install pdfplumber")
        return

    example = Path("example/sample_graduatoria.pdf")
    if not example.exists():
        fail("PDF di esempio non trovato", str(example))
        return
    ok("PDF di esempio trovato")

    with open(example, "rb") as f:
        pdf_bytes = f.read()

    # Verifica estrazione tabelle
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        tables_found = []
        for i, page in enumerate(pdf.pages):
            for t in (page.extract_tables() or []):
                tables_found.append((i + 1, t))

    if tables_found:
        ok(f"Tabelle estratte: {len(tables_found)}")
    else:
        fail("Nessuna tabella trovata nel PDF")
        return

    page_num, table = tables_found[0]
    info(f"Prima tabella a pagina {page_num} — {len(table)} righe, {len(table[0])} colonne")

    headers = [" ".join(str(h or "").split()).upper() for h in table[0]]
    info(f"Header: {headers}")

    # Verifica colonne chiave
    has_codice = any("MECCANOGRAFICO" in h for h in headers)
    has_pos    = any("POSIZIONE" in h for h in headers)

    if has_codice:
        ok("Colonna CODICE MECCANOGRAFICO trovata")
    else:
        fail("Colonna CODICE MECCANOGRAFICO non trovata")

    if has_pos:
        ok("Colonna POSIZIONE trovata")
    else:
        fail("Colonna POSIZIONE non trovata")

    # Verifica dati
    codice_idx = next(i for i, h in enumerate(headers) if "MECCANOGRAFICO" in h)
    pos_idx    = next((i for i, h in enumerate(headers) if "POSIZIONE" in h), None)
    data_rows  = [r for r in table[1:] if r and str(r[codice_idx] or "").strip()]

    if data_rows:
        ok(f"Righe dati: {len(data_rows)}")
        info(f"Prima riga: pos={data_rows[0][pos_idx] if pos_idx else '?'}  codice={data_rows[0][codice_idx]}")
    else:
        fail("Nessuna riga dati con codice")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 2 — PDFAnalyzer.search_table
# ──────────────────────────────────────────────────────────────────────────────

def test_pdf_search():
    print(f"\n{SEP}")
    print("TEST 2 — PDFAnalyzer.search_table")
    print(SEP)

    from monitor_bot import PDFAnalyzer

    example = Path("example/sample_graduatoria.pdf")
    if not example.exists():
        fail("PDF di esempio non trovato", str(example))
        return

    with open(example, "rb") as f:
        pdf_bytes = f.read()

    # Codice noto — deve essere trovato in posizione 1
    result = PDFAnalyzer.search_table(pdf_bytes, "SAMPLE0001")
    if result:
        ok(f"Codice SAMPLE0001 trovato", f"posizione={result['position']}")
        if result["position"] == "1":
            ok("Posizione corretta: 1")
        else:
            fail("Posizione errata", f"attesa=1, ottenuta={result['position']}")
    else:
        fail("Codice SAMPLE0001 non trovato (atteso in posizione 1)")

    # Codice case-insensitive
    result_lower = PDFAnalyzer.search_table(pdf_bytes, "sample0001")
    if result_lower:
        ok("Ricerca case-insensitive funziona")
    else:
        fail("Ricerca case-insensitive fallita")

    # Codice esistente — posizione 2
    result2 = PDFAnalyzer.search_table(pdf_bytes, "SAMPLE0002")
    if result2 and result2["position"] == "2":
        ok("Codice SAMPLE0002 trovato in posizione 2")
    elif result2:
        fail("Posizione errata per SAMPLE0002", f"attesa=2, ottenuta={result2['position']}")
    else:
        fail("Codice SAMPLE0002 non trovato")

    # Codice inesistente — deve restituire None
    result_none = PDFAnalyzer.search_table(pdf_bytes, "XXXXXXXXXX")
    if result_none is None:
        ok("Codice inesistente restituisce None")
    else:
        fail("Codice inesistente ha restituito un risultato inatteso", str(result_none))

    # Bytes vuoti — non deve crashare
    try:
        result_empty = PDFAnalyzer.search_table(b"", "SAMPLE0001")
        ok("Bytes vuoti gestiti senza crash", f"risultato={result_empty}")
    except Exception as e:
        fail("Bytes vuoti hanno causato un'eccezione", str(e))

    # Tutti i codici presenti nel PDF
    all_codes = [(f"SAMPLE{i:04d}", str(i)) for i in range(1, 11)]
    ok(f"Verifica tutti i {len(all_codes)} codici del PDF...")
    errors = 0
    for codice, attesa in all_codes:
        r = PDFAnalyzer.search_table(pdf_bytes, codice)
        if not r or r["position"] != attesa:
            fail(f"  Codice {codice}", f"attesa posizione {attesa}, ottenuta {r}")
            errors += 1
    if errors == 0:
        ok(f"Tutti i {len(all_codes)} codici trovati nelle posizioni corrette")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 3 — WebScraper.extract_pdf_links
# ──────────────────────────────────────────────────────────────────────────────

def test_pdf_link_extraction():
    print(f"\n{SEP}")
    print("TEST 3 — Estrazione link PDF dall'HTML")
    print(SEP)

    from monitor_bot import WebScraper

    html_with_pdfs = """
    <html><body>
      <div id="section-1">
        <h3>Sezione 1</h3>
        <a href="/docs/graduatoria_1.pdf">Graduatoria sezione 1</a>
        <a href="/docs/determina_1.pdf">Determina 1</a>
      </div>
      <div id="section-2">
        <a href="/docs/graduatoria_2.pdf">Graduatoria sezione 2</a>
      </div>
    </body></html>
    """
    base = "https://www.example.com"

    # Senza selettore — tutti i PDF
    links_all = WebScraper.extract_pdf_links(html_with_pdfs, base)
    if len(links_all) == 3:
        ok(f"Estrazione senza selettore: {len(links_all)} PDF trovati")
    else:
        fail("Estrazione senza selettore", f"attesi 3, trovati {len(links_all)}")

    # Con selettore CSS — solo dalla sezione 1
    links_j = WebScraper.extract_pdf_links(html_with_pdfs, base, css_selector="#section-1")
    if len(links_j) == 2:
        ok(f"Estrazione con #section-1: {len(links_j)} PDF trovati")
    else:
        fail("Estrazione con selettore #section-1", f"attesi 2, trovati {len(links_j)}")

    # URL assoluti
    if all(l.startswith("https://") for l in links_all):
        ok("Tutti gli URL sono assoluti")
    else:
        fail("Alcuni URL non sono assoluti", str(links_all))

    # Dedup
    html_dup = """
    <html><body>
      <a href="/docs/test.pdf">Link 1</a>
      <a href="/docs/test.pdf">Link 2 (stesso file)</a>
    </body></html>
    """
    links_dup = WebScraper.extract_pdf_links(html_dup, base)
    if len(links_dup) == 1:
        ok("Deduplicazione URL funziona")
    else:
        fail("Deduplicazione fallita", f"trovati {len(links_dup)} invece di 1")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 4 — MonitoredSite serializzazione con nuovi campi
# ──────────────────────────────────────────────────────────────────────────────

def test_serialization():
    print(f"\n{SEP}")
    print("TEST 4 — Serializzazione MonitoredSite (nuovi campi PDF)")
    print(SEP)

    from monitor_bot import MonitoredSite

    site = MonitoredSite(
        url="https://www.example.com/graduatoria",
        name="Sito Test",
        css_selector="#section-1",
        pdf_analysis=True,
        pdf_search_value="SAMPLE0001",
        pdf_results={"https://example.com/doc.pdf": "found:1"},
    )

    d = site.to_dict()
    required_keys = ["pdf_analysis", "pdf_search_value", "pdf_results"]
    for k in required_keys:
        if k in d:
            ok(f"Chiave '{k}' presente in to_dict")
        else:
            fail(f"Chiave '{k}' mancante in to_dict")

    # Round-trip
    site2 = MonitoredSite.from_dict(d)
    if site2.pdf_analysis == site.pdf_analysis:
        ok("pdf_analysis sopravvive a round-trip")
    else:
        fail("pdf_analysis perso nel round-trip")

    if site2.pdf_search_value == site.pdf_search_value:
        ok("pdf_search_value sopravvive a round-trip")
    else:
        fail("pdf_search_value perso nel round-trip")

    if site2.pdf_results == site.pdf_results:
        ok("pdf_results sopravvive a round-trip")
    else:
        fail("pdf_results perso nel round-trip")

    # Compatibilità con dati vecchi (senza campi PDF)
    old_data = {
        "url": "https://www.example.com",
        "name": "Old Site",
        "previous_hash": None,
        "last_check": None,
        "css_selector": None,
        "paused": False,
        "error_count": 0,
        "error_notified": False,
        "use_browser": False,
        "threshold": None,
    }
    try:
        old_site = MonitoredSite.from_dict(old_data)
        if not old_site.pdf_analysis and old_site.pdf_search_value is None:
            ok("Compatibilità con dati vecchi (senza campi PDF)")
        else:
            fail("Valori default errati per dati vecchi")
    except Exception as e:
        fail("Eccezione su dati vecchi", str(e))

    # Serializzazione JSON
    try:
        json_str = json.dumps(d, ensure_ascii=False)
        d2 = json.loads(json_str)
        if d2["pdf_results"] == d["pdf_results"]:
            ok("Serializzazione JSON completa funziona")
        else:
            fail("Discrepanza dopo JSON round-trip")
    except Exception as e:
        fail("Errore JSON", str(e))


# ──────────────────────────────────────────────────────────────────────────────
# TEST 5 — Scraping web (rete reale)
# ──────────────────────────────────────────────────────────────────────────────

async def test_scrape_web():
    print(f"\n{SEP}")
    print("TEST 5 — Scraping pagina web (richiede connessione)")
    print(SEP)

    from monitor_bot import Config, WebScraper

    # URL neutro per il test (override con TEST_SCRAPE_URL nel .env se serve)
    URL = os.getenv("TEST_SCRAPE_URL", "https://www.example.com")

    config = Config()
    scraper = WebScraper(config)

    try:
        info("Tentativo con aiohttp...")
        html = await scraper._fetch_aiohttp(URL)
        if html:
            ok(f"aiohttp riuscito ({len(html)} chars)")
        else:
            info("aiohttp fallito — provo Playwright...")
            html = await scraper._fetch_playwright(URL)
            if html:
                ok(f"Playwright riuscito ({len(html)} chars)")
            else:
                fail("Entrambi i metodi falliti — verifica connessione o Playwright")
                return

        # Estrai contenuto
        content = WebScraper.extract_content(html, base_url=URL)
        if content:
            ok(f"Contenuto estratto ({len(content)} chars)")
        else:
            fail("Contenuto estratto vuoto")

        # Cerca PDF nella pagina
        pdf_links = WebScraper.extract_pdf_links(html, URL)
        info(f"PDF trovati nella pagina (senza selettore): {len(pdf_links)}")
        for link in pdf_links[:5]:
            info(f"  {link}")

        # Cerca documenti generici
        docs = WebScraper.extract_documents(html, URL)
        info(f"Documenti totali trovati: {len(docs)}")

    finally:
        await scraper.close()


# ──────────────────────────────────────────────────────────────────────────────
# TEST 6 — Logica monitoraggio (dry run, senza bot Telegram)
# ──────────────────────────────────────────────────────────────────────────────

async def test_monitoring_logic():
    print(f"\n{SEP}")
    print("TEST 6 — Logica di monitoraggio (dry run)")
    print(SEP)

    from monitor_bot import (
        Config, MonitoredSite, WebScraper, DiffAnalyzer, PDFAnalyzer, WebMonitorBot
    )

    # Simula un sito con analisi PDF abilitata
    site = MonitoredSite(
        url="https://www.example.com/graduatoria",
        name="Sito Test",
        css_selector=None,
        pdf_analysis=True,
        pdf_search_value="SAMPLE0001",
    )

    # Simula HTML con un link PDF fittizio (puntiamo al file locale di esempio)
    example_pdf = Path("example/sample_graduatoria.pdf")
    if not example_pdf.exists():
        fail("PDF di esempio non trovato per dry run")
        return

    # Test diretto PDFAnalyzer su file locale
    with open(example_pdf, "rb") as f:
        pdf_bytes = f.read()

    result = PDFAnalyzer.search_table(pdf_bytes, site.pdf_search_value)
    if result:
        ok(f"Dry run: codice {site.pdf_search_value} trovato in posizione {result['position']}")
    else:
        fail(f"Dry run: codice {site.pdf_search_value} non trovato nel PDF di esempio")

    # Test hashing e diff
    content_v1 = "Graduatoria in corso\nCandidati ammessi: 150"
    content_v2 = "Graduatoria in corso\nCandidati ammessi: 150\nNuova sezione pubblicata"

    hash1 = DiffAnalyzer.compute_hash(content_v1)
    hash2 = DiffAnalyzer.compute_hash(content_v2)

    if hash1 != hash2:
        ok("Hash diversi per contenuto diverso")
    else:
        fail("Hash identici per contenuto diverso")

    if DiffAnalyzer.has_changed(content_v1, content_v2):
        ok("has_changed rileva differenza")
    else:
        fail("has_changed non rileva differenza")

    ratio = DiffAnalyzer.get_change_ratio(content_v1, content_v2)
    info(f"Change ratio v1→v2: {ratio:.2%}")

    if not DiffAnalyzer.has_changed(None, content_v1):
        ok("has_changed con old=None restituisce False (primo caricamento)")
    else:
        fail("has_changed con old=None dovrebbe restituire False")

    # Test soglia
    small_change = content_v1 + " " * 5
    small_ratio = DiffAnalyzer.get_change_ratio(content_v1, small_change)
    info(f"Change ratio per micro-cambio: {small_ratio:.4%}")

    if small_ratio < 0.01:
        ok("Micro-cambiamento sotto soglia 1% — verrebbe ignorato")
    else:
        info("Micro-cambiamento sopra soglia — verrebbe notificato")

    # Verifica pdf_results tracking
    site.pdf_results["https://example.com/test.pdf"] = "found:1"
    d = site.to_dict()
    site2 = MonitoredSite.from_dict(d)
    if "https://example.com/test.pdf" in site2.pdf_results:
        ok("pdf_results persiste correttamente")
    else:
        fail("pdf_results non persistito")


# ──────────────────────────────────────────────────────────────────────────────
# TEST 7 — Simulazione download PDF (con file locale)
# ──────────────────────────────────────────────────────────────────────────────

async def test_pdf_download_simulation():
    print(f"\n{SEP}")
    print("TEST 7 — Simulazione download e analisi PDF completa")
    print(SEP)

    from monitor_bot import PDFAnalyzer, WebScraper, Config

    example_pdf = Path("example/sample_graduatoria.pdf")
    if not example_pdf.exists():
        fail("PDF di esempio non trovato")
        return

    # Crea un server HTTP locale temporaneo con http.server? Troppo complesso.
    # Testiamo il flusso direttamente sui bytes.
    with open(example_pdf, "rb") as f:
        pdf_bytes = f.read()

    ok(f"PDF caricato localmente ({len(pdf_bytes)} bytes)")

    # Testa tutti i codici attesi
    test_cases = [
        ("SAMPLE0001", "1"),
        ("SAMPLE0002", "2"),
        ("SAMPLE0009", "9"),
        ("SAMPLE0010", "10"),
        ("NOTEXIST00", None),
    ]

    all_ok = True
    for codice, expected_pos in test_cases:
        result = PDFAnalyzer.search_table(pdf_bytes, codice)
        if expected_pos is None:
            if result is None:
                ok(f"'{codice}' → non trovato (atteso)")
            else:
                fail(f"'{codice}' → trovato inaspettatamente", str(result))
                all_ok = False
        else:
            if result and result["position"] == expected_pos:
                ok(f"'{codice}' → posizione {result['position']}")
            else:
                fail(f"'{codice}'", f"atteso pos={expected_pos}, ottenuto={result}")
                all_ok = False

    if all_ok:
        ok("Tutti i casi di test superati")


# ──────────────────────────────────────────────────────────────────────────────
# RUNNER
# ──────────────────────────────────────────────────────────────────────────────

def test_names_and_classify():
    print(f"\n{SEP}")
    print("TEST 5 — Nomi unici per slug uguali + classificazione wipe/rewrite")
    print(SEP)

    from bot.models import MonitoredSite
    from bot.scraper import WebScraper
    from bot.diff import DiffAnalyzer

    # Stesso slug, query diverse → nomi default diversi
    a = MonitoredSite(url="https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id=aaa111")
    b = MonitoredSite(url="https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id=bbb222")
    if a.name != b.name:
        ok("Nomi default unici con query diverse", f"{a.name!r} vs {b.name!r}")
    else:
        fail("Nomi default identici", a.name)

    # Titolo pagina preferito allo slug
    title = WebScraper.extract_title("<html><head><title>x</title></head><body><h1>Concorso 90 posti</h1></body></html>")
    if title == "Concorso 90 posti":
        ok("extract_title usa h1")
    else:
        fail("extract_title errato", repr(title))
    if WebScraper.extract_title("<html><head><title>Solo Title</title></head><body></body></html>") == "Solo Title":
        ok("extract_title fallback su <title>")
    else:
        fail("extract_title fallback rotto")

    # Classificazione cambiamenti
    cases = [
        ("contenuto pieno", "", "wiped"),
        ("", "contenuto nuovo", "rewritten"),
        ("vecchio contenuto della pagina", "0123456789" * 5, "rewritten"),  # nessun overlap → ratio ~1.0
        ("riga1\nriga2\nriga3", "riga1\nriga2\nriga3 extra", "update"),
    ]
    for old, new, expected in cases:
        ratio = DiffAnalyzer.get_change_ratio(old, new) if (old or new) else 0.0
        got = DiffAnalyzer.classify_change(old, new, ratio)
        if got == expected:
            ok(f"classify_change({expected})")
        else:
            fail(f"classify_change atteso {expected}", f"ottenuto {got} (ratio {ratio:.2f})")

    # Lista lunga: cap + riga indicatore invece di troncamento silenzioso
    from bot.keyboards import site_list_keyboard, MAX_LIST_BUTTONS

    many = {
        f"https://example.com/p{i}": MonitoredSite(url=f"https://example.com/p{i}")
        for i in range(MAX_LIST_BUTTONS + 5)
    }
    kb = site_list_keyboard(many)
    rows = kb.inline_keyboard
    if len(rows) == MAX_LIST_BUTTONS + 1 and "altri 5" in rows[-1][0].text:
        ok("site_list_keyboard mostra indicatore siti nascosti")
    else:
        fail("site_list_keyboard indicatore mancante", f"{len(rows)} righe, ultima: {rows[-1][0].text!r}")


def test_noise_filter():
    print(f"\n{SEP}")
    print("TEST 7 — Filtro rumore (link ricerca, separatori)")
    print(SEP)

    from bot.diff import DiffAnalyzer, NOISE_DEFAULTS, strip_noise

    base = "Bando 50 ICT\nScadenza 30/09\n\n__LINKS__\nhttps://www.bancaditalia.it/bando.pdf"
    noisy = (
        "Bando 50 ICT\nCerca nel sito\n|\nScadenza 30/09\n\n__LINKS__\n"
        "https://www.bancaditalia.it/bando.pdf\nhttps://www.bancaditalia.it/ricerca/"
    )

    clean_base = strip_noise(base, NOISE_DEFAULTS)
    clean_noisy = strip_noise(noisy, NOISE_DEFAULTS)
    if clean_base == clean_noisy:
        ok("Comparsa/sparizione del box ricerca non conta come cambiamento")
    else:
        fail("Rumore non filtrato", f"{clean_base!r} != {clean_noisy!r}")

    if DiffAnalyzer.has_changed(clean_base, clean_noisy) is False:
        ok("has_changed() = False sul contenuto filtrato")
    else:
        fail("has_changed() ancora True dopo il filtro")

    # Un cambiamento vero passa comunque
    real = strip_noise(noisy.replace("Scadenza 30/09", "Scadenza 15/10"), NOISE_DEFAULTS)
    if DiffAnalyzer.has_changed(clean_base, real):
        ok("Cambiamento reale rilevato anche col filtro attivo")
    else:
        fail("Il filtro nasconde un cambiamento reale")

    # Pattern rotto: ignorato, non esplode
    if strip_noise("riga", ["(("]) == "riga":
        ok("Regex non valida ignorata senza crash")
    else:
        fail("Regex non valida gestita male")

    if strip_noise(None, NOISE_DEFAULTS) is None:
        ok("None resta None (primo controllo del sito)")
    else:
        fail("strip_noise(None) non restituisce None")


def test_extract_skips_nav_wrapper():
    print(f"\n{SEP}")
    print("TEST 7b — Estrazione: wrapper menu 'main-nav' non scambiato per contenuto")
    print(SEP)

    from bot.scraper import WebScraper

    # Struttura reale bancaditalia.it: div#main-nav-wrapper (menu) prima di div#main
    def page(scadenza):
        return (
            '<html><body><div id="main-nav-wrapper"><a href="/home">Home page</a> Cerca</div>'
            f'<div id="main"><h1>Bando 50 ICT</h1><p>{"Requisiti e modalità. " * 15}</p>'
            f'<p>Scadenza {scadenza}</p></div></body></html>'
        )

    a = WebScraper.extract_content(page("30/09"))
    b = WebScraper.extract_content(page("15/10"))
    if "Bando 50 ICT" in a and a != b:
        ok("Contenuto di div#main estratto, cambio scadenza rilevato")
    else:
        fail("Estratto solo il menu, cambiamento invisibile", a[:120])


def test_dedup_recent():
    print(f"\n{SEP}")
    print("TEST 8 — Deduplica temporale (contenuto già notificato)")
    print(SEP)

    import tempfile
    from datetime import datetime, timedelta
    from bot.config import Config
    from bot.handlers import WebMonitorBot
    from bot.models import MonitoredSite

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SITES_FILE"] = f"{tmp}/sites.json"
        os.environ["CHANGE_LOG_FILE"] = f"{tmp}/changes.jsonl"
        bot = WebMonitorBot(Config())
        site = MonitoredSite(url="https://example.com/bando")

        if not bot._recently_notified(site, "h1"):
            ok("Hash mai visto → non è un duplicato")
        else:
            fail("Hash sconosciuto scambiato per duplicato")

        bot._remember_notified(site, "h1")
        if bot._recently_notified(site, "h1"):
            ok("Contenuto identico entro la finestra → silenzio")
        else:
            fail("Duplicato recente non riconosciuto")

        # fuori finestra: stesso contenuto, ma vecchio → si notifica di nuovo
        site.notified_hashes["h1"] = (
            datetime.now() - timedelta(hours=bot.config.dedup_window_hours + 1)
        ).isoformat()
        if not bot._recently_notified(site, "h1"):
            ok("Oltre la finestra → torna a notificare")
        else:
            fail("Finestra temporale ignorata")

        # timestamp corrotto: non deve zittire nulla
        site.notified_hashes["h2"] = "non-una-data"
        if not bot._recently_notified(site, "h2"):
            ok("Timestamp corrotto ignorato senza crash")
        else:
            fail("Timestamp corrotto trattato come duplicato")

        for i in range(40):
            bot._remember_notified(site, f"x{i}")
        if len(site.notified_hashes) <= bot.MAX_NOTIFIED_HASHES + 2:
            ok(f"Memoria hash limitata ({len(site.notified_hashes)} voci)")
        else:
            fail("Memoria hash cresce senza limite", str(len(site.notified_hashes)))

        # round-trip: la memoria sopravvive al salvataggio
        d = MonitoredSite.from_dict(site.to_dict())
        if d.notified_hashes == site.notified_hashes:
            ok("notified_hashes serializzato e riletto")
        else:
            fail("notified_hashes perso nel round-trip")


def test_startup_outside_loop():
    print(f"\n{SEP}")
    print("TEST 11 — Avvio: il bot si costruisce senza event loop, come in monitor_bot.py")
    print(SEP)

    import threading
    from bot.config import Config
    from bot.handlers import WebMonitorBot

    errors = []
    with tempfile.TemporaryDirectory() as tmp:
        def build():
            try:
                WebMonitorBot(Config(sites_file=f"{tmp}/s.json", change_log_file=f"{tmp}/c.jsonl"))
            except Exception as e:
                errors.append(e)

        # run_all gira dentro asyncio.run: un thread nuovo non ha loop, come all'avvio reale
        t = threading.Thread(target=build)
        t.start()
        t.join()
    if not errors:
        ok("WebMonitorBot costruito fuori dall'event loop")
    else:
        fail("Il bot crasha all'avvio", repr(errors[0]))


async def test_browser_relaunch():
    print(f"\n{SEP}")
    print("TEST 10 — Playwright: rilancio dopo launch fallito o browser morto")
    print(SEP)

    from unittest.mock import patch
    import playwright.async_api
    from bot.config import Config
    from bot.scraper import WebScraper

    class Page:
        async def route(self, *a, **kw): pass
        async def route_web_socket(self, *a, **kw): pass
        async def goto(self, url, **kw): return None
        async def wait_for_timeout(self, ms): pass
        async def content(self): return "<html>ok</html>"

    class Context:
        async def new_page(self): return Page()
        async def close(self): pass

    class Browser:
        alive = True
        def is_connected(self): return self.alive
        async def new_context(self, **kw): return Context()
        async def close(self): pass

    class Chromium:
        launches = 0
        async def launch(self, **kw):
            self.launches += 1
            if self.launches == 1:
                raise RuntimeError("Host system is missing dependencies")
            return Browser()

    class Playwright:
        chromium = Chromium()
        async def stop(self): pass

    pw = Playwright()

    class Starter:
        async def start(self): return pw

    scraper = WebScraper(Config())
    async def public(url): pass  # offline: niente DNS reale
    scraper._validate_public_target = public
    with patch.object(playwright.async_api, "async_playwright", Starter):
        if await scraper._fetch_playwright("https://example.com") is None:
            ok("Primo launch fallito → None, nessun crash")
        else:
            fail("Launch fallito non gestito")

        html = await scraper._fetch_playwright("https://example.com")
        if html == "<html>ok</html>" and pw.chromium.launches == 2:
            ok("Dopo un launch fallito il browser viene rilanciato")
        else:
            fail("Nessun rilancio dopo launch fallito", f"html={html!r} launch={pw.chromium.launches}")

        scraper._browser.alive = False
        html = await scraper._fetch_playwright("https://example.com")
        if html == "<html>ok</html>" and pw.chromium.launches == 3:
            ok("Browser disconnesso (crash Chromium) → rilanciato")
        else:
            fail("Browser morto riusato", f"launch={pw.chromium.launches}")

        await scraper._fetch_playwright("https://example.com")
        if pw.chromium.launches == 3:
            ok("Browser vivo riusato, nessun launch superfluo")
        else:
            fail("Launch ripetuto con browser vivo", f"launch={pw.chromium.launches}")
    await scraper.close()

    calls = []
    async def aiohttp_down(url): return None
    async def browser(url):
        calls.append(url)
        return "<html>browser</html>"
    scraper._fetch_aiohttp, scraper._fetch_playwright = aiohttp_down, browser
    if await scraper.fetch_page("https://example.com") is None and not calls:
        ok("aiohttp giù → nessun fallback al browser (niente falsi cambiamenti)")
    else:
        fail("fetch_page mescola aiohttp e browser", f"browser chiamato {len(calls)}x")
    if await scraper.fetch_page("https://example.com", use_browser=True) == "<html>browser</html>":
        ok("use_browser=True → browser")
    else:
        fail("Modalità browser ignorata")


def test_graduatoria_position():
    print(f"\n{SEP}")
    print("TEST 6 — Posizione graduatoria, doc_name Liferay, estensioni")
    print(SEP)

    from bot.pdf import PDFAnalyzer
    from bot.scraper import WebScraper

    fixture = Path(__file__).parent / "example" / "sample_graduatoria.pdf"
    if not fixture.is_file():
        info("Fixture sample_graduatoria.pdf assente — skip")
    else:
        b = fixture.read_bytes()
        row = PDFAnalyzer.row_at_position(b, "3")
        if row and row["codice"] == "SAMPLE0003":
            ok("row_at_position(3) → codice corretto")
        else:
            fail("row_at_position(3)", repr(row))
        if PDFAnalyzer.row_at_position(b, "999") is None:
            ok("row_at_position posizione inesistente → None")
        else:
            fail("row_at_position 999 dovrebbe essere None")

    # doc_name gestisce URL Liferay (…/nome.pdf/<uuid>?t=…)
    liferay = "https://x/documents/1/2/DETERMINA+GRADUATORIA.pdf/abc-uuid?t=123"
    if PDFAnalyzer.doc_name(liferay) == "DETERMINA+GRADUATORIA.pdf":
        ok("doc_name estrae il file da URL Liferay")
    else:
        fail("doc_name Liferay", PDFAnalyzer.doc_name(liferay))

    # _doc_extension riconosce il .pdf come segmento intermedio
    cases = [
        ("/documents/1/2/x.pdf/uuid?t=1", ".pdf"),
        ("/files/y.PDF", ".pdf"),
        ("/page.html", None),
    ]
    for href, expected in cases:
        got = WebScraper._doc_extension(href, (".pdf",))
        if got == expected:
            ok(f"_doc_extension({href}) → {expected}")
        else:
            fail(f"_doc_extension({href})", f"atteso {expected}, ottenuto {got}")

    # Header 'Graduatoria' (ACN) riconosciuto come colonna posizione
    from bot.diff import DiffAnalyzer  # noqa: F401 (coerenza import stile)
    if fixture.is_file():
        b = fixture.read_bytes()
        r = PDFAnalyzer.search_table(b, "SAMPLE0005")
        if r and r["position"] == "5":
            ok("search_table restituisce la posizione corretta")
        else:
            fail("search_table posizione", repr(r))

        # find_code_row + rilevamento nome mostrato/omissato
        row = PDFAnalyzer.find_code_row(b, "SAMPLE0004")
        if row and row["position"] == "4" and row["name_shown"] and "Nome04" in row["name"]:
            ok("find_code_row: codice con nome → name_shown=True")
        else:
            fail("find_code_row named", repr(row))
        if PDFAnalyzer.find_code_row(b, "NONESISTE") is None:
            ok("find_code_row: codice assente → None")
        else:
            fail("find_code_row assente dovrebbe essere None")

    # _extract_name: scarta redazioni OMISSIS/MISSIS e numeri, tiene i nomi
    from bot.pdf import PDFAnalyzer as P
    # row = [pos, codice, cognome, nome, data, punteggio]; pos_idx=0, codice_idx=1
    assert P._extract_name(["1", "ABC", "Rossi", "Mario", "OMISSIS", "108"], 0, 1) == "Rossi Mario"
    assert P._extract_name(["11", "ABC", "", None, None, "86,8"], 0, 1) == ""
    assert P._extract_name(["10", "ABC", "MISSIS\nO", "", "", "99"], 0, 1) == ""
    ok("_extract_name: nomi sì, redazioni/numeri no")

    # Due sezioni dello stesso URL → chiavi distinte (monitoraggio separato)
    from bot.models import MonitoredSite
    j = MonitoredSite(url="https://acn.example/concorso", section_id="J")
    b2 = MonitoredSite(url="https://acn.example/concorso", section_id="B")
    if j.key != b2.key and j.key.endswith("#J") and b2.key.endswith("#B"):
        ok("Due sezioni stesso URL → chiavi distinte")
    else:
        fail("Chiavi sezioni non distinte", f"{j.key} / {b2.key}")


async def test_hardening():
    print(f"\n{SEP}")
    print("TEST 9 — Accesso, URL pubblici, salvataggio atomico, posizione attesa")
    print(SEP)

    from types import SimpleNamespace as NS
    from telegram.ext import ApplicationHandlerStop
    from bot.config import Config
    from bot.handlers import WebMonitorBot
    from bot.models import MonitoredSite
    from bot.scraper import PublicResolver
    from bot.utils import is_valid_url

    good = ["https://example.com/x", "http://8.8.8.8/"]
    bad = [
        "ftp://example.com", "http://127.0.0.1/", "http://192.168.1.1/", "http://[::1]/",
        "http://169.254.169.254/", "https://user:pw@example.com/", " https://example.com",
    ]
    wrong = [u for u in good if not is_valid_url(u)] + [u for u in bad if is_valid_url(u)]
    if not wrong:
        ok("is_valid_url: pubblici sì, privati/credenziali/schema no")
    else:
        fail("is_valid_url sbagliato su", str(wrong))

    class FakeDNS:
        def __init__(self, ip):
            self.ip = ip

        async def resolve(self, host, port, family):
            return [{"host": self.ip}]

    async def resolves(ip: str) -> bool:
        try:
            await PublicResolver(FakeDNS(ip)).resolve("x.test", 443)
            return True
        except OSError:
            return False

    if await resolves("93.184.216.34") and not await resolves("10.0.0.5"):
        ok("PublicResolver: blocca host che risolvono su IP privati")
    else:
        fail("PublicResolver non filtra gli IP privati")

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(
            chat_id=-100123,
            allowed_user_ids=frozenset({42}),
            sites_file=f"{tmp}/sites.json",
            change_log_file=f"{tmp}/changes.jsonl",
        )
        bot = WebMonitorBot(cfg)

        def upd(chat, user):
            return NS(
                effective_chat=NS(id=chat) if chat else None,
                effective_user=NS(id=user) if user else None,
            )

        cases = [(upd(-100123, None), True), (upd(42, 42), True), (upd(7, 7), False), (upd(None, None), False)]
        if all(bot._is_authorized(u) == expected for u, expected in cases):
            ok("Accesso: solo TELEGRAM_CHAT_ID e ALLOWED_USER_IDS")
        else:
            fail("Controllo accesso sbagliato")
        try:
            await bot.guard_update(upd(7, 7), None)
            fail("guard_update lascia passare uno sconosciuto")
        except ApplicationHandlerStop:
            ok("guard_update ferma gli sconosciuti")

        site = MonitoredSite(
            url="https://example.com/g", name="G",
            pdf_search_value="C1", pdf_expected_position="11",
        )
        bot.sites[site.key] = site
        bot._save_sites()
        saved = json.loads(Path(cfg.sites_file).read_text(encoding="utf-8"))
        if site.key in saved and not Path(f"{cfg.sites_file}.tmp").exists():
            ok("Salvataggio atomico: stato scritto, nessun .tmp residuo")
        else:
            fail("Salvataggio atomico", str(list(saved)))

        ev = {"url": "https://example.com/graduatoria.pdf", "kind": "omissata",
              "row": {"position": "12", "name": "", "name_shown": False}}
        mismatch = "Posizione diversa" in bot._pdf_event_text(site, ev, "")
        ev["row"]["position"] = "11"
        match = "Posizione diversa" not in bot._pdf_event_text(site, ev, "")
        if mismatch and match:
            ok("Posizione attesa: avviso solo se diversa")
        else:
            fail("Nota posizione attesa", f"mismatch={mismatch} match={match}")


async def test_whole_site():
    print(f"\n{SEP}")
    print("TEST 12 — /add --whole: scansione di tutte le pagine sotto l'URL")
    print(SEP)

    from bot.config import Config
    from bot.handlers import WebMonitorBot
    from bot.models import MonitoredSite

    R = "https://example.com/sito/"
    long = lambda w: "<main>" + (w + " ") * 200 + "</main>"
    web = {
        R: '<a href="a">A</a> <a href="/altro/">fuori</a> <a href="https://evil.com/sito/x">host</a>'
           '<a href="doc.pdf">pdf</a> <a href="a#top">frag</a> <a href="mailto:x@y">m</a>' + long("root"),
        R + "a": '<a href="a/b">B</a>' + long("pagina a"),
        R + "a/b": long("pagina b"),
    }
    fetched: list[str] = []
    sent: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        bot = WebMonitorBot(Config(sites_file=f"{tmp}/s.json", change_log_file=f"{tmp}/c.jsonl"))

        async def fetch_page(url, use_browser=False):
            fetched.append(url)
            return web.get(url)

        async def send_message(**kw):
            sent.append(kw["text"])

        async def fake_bot():
            return type("B", (), {"send_message": staticmethod(send_message)})

        async def no_sleep(_):
            pass

        bot.scraper.fetch_page = fetch_page
        bot._bot = fake_bot
        real_sleep = asyncio.sleep
        asyncio.sleep = no_sleep
        try:
            site = MonitoredSite(url=R, name="S", whole_site=True)
            await bot._check_site(site)
            if sorted(site.pages) == [R, R + "a", R + "a/b"] and "3 pagine" in sent[-1]:
                ok("Prima scansione: sottopagine trovate, fuori scope/pdf/altri host esclusi")
            else:
                fail("Prima scansione", f"{sorted(site.pages)} {sent}")

            web[R] = web[R].replace("root", "home") + '<a href="c">C</a>'
            web[R + "c"] = long("pagina c")
            web[R + "a"] = None  # errore temporaneo: resta, niente falso "rimossa"
            res = await bot._check_site(site)
            msg = sent[-1]
            if res == "sent" and R + "c" in msg and "Nuove pagine" in msg and "rimosse" not in msg \
                    and R + "a" in site.pages and "home" in msg:
                ok("Nuova pagina + pagina modificata in un messaggio, errore temporaneo ignorato")
            else:
                fail("Secondo giro", msg)

            web[R] = web[R].replace('<a href="c">C</a>', "")
            web[R + "c"] = None
            await bot._check_site(site)
            if "Pagine rimosse (1)" in sent[-1] and R + "c" not in site.pages:
                ok("Pagina sparita e non più linkata → rimossa")
            else:
                fail("Rimozione", sent[-1])

            n = len(sent)
            await bot._check_site(site)
            if len(sent) == n:
                ok("Nessun cambiamento → silenzio")
            else:
                fail("Notifica senza cambiamenti", sent[-1])

            if MonitoredSite.from_dict(site.to_dict()).pages == site.pages:
                ok("pages serializzato e riletto")
            else:
                fail("pages perso nel round-trip")
        finally:
            asyncio.sleep = real_sleep


async def run_all(mode: str = "all"):
    tests_sync = {
        "pdf":      [test_pdf_parsing, test_pdf_search, test_pdf_link_extraction, test_serialization],
        "serial":   [test_serialization],
        "names":    [test_names_and_classify, test_graduatoria_position],
        "noise":    [test_noise_filter, test_dedup_recent],
        "startup":  [test_startup_outside_loop],
        "all":      [test_pdf_parsing, test_pdf_search, test_pdf_link_extraction, test_serialization, test_names_and_classify, test_graduatoria_position, test_noise_filter, test_extract_skips_nav_wrapper, test_dedup_recent, test_startup_outside_loop],
    }
    tests_sync["offline"] = tests_sync["all"]
    tests_async = {
        "scrape":   [test_scrape_web],
        "monitor":  [test_monitoring_logic, test_pdf_download_simulation],
        "browser":  [test_browser_relaunch],
        "hardening": [test_hardening],
        "whole":    [test_whole_site],
        "offline":  [test_monitoring_logic, test_pdf_download_simulation, test_browser_relaunch, test_hardening, test_whole_site],
        "all":      [test_monitoring_logic, test_pdf_download_simulation, test_browser_relaunch, test_hardening, test_whole_site, test_scrape_web],
    }

    # Modalità presente in una sola tabella → l'altra non aggiunge nulla.
    sync_suite  = tests_sync.get(mode, [] if mode in tests_async else tests_sync["all"])
    async_suite = tests_async.get(mode, [] if mode in tests_sync else tests_async["all"])

    for t in sync_suite:
        try:
            t()
        except Exception as e:
            print(f"\n  💥 ECCEZIONE in {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            _failures.append(t.__name__)

    for t in async_suite:
        try:
            await t()
        except Exception as e:
            print(f"\n  💥 ECCEZIONE in {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            _failures.append(t.__name__)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"

    print("=" * 60)
    print(f"  Web Monitor Bot — Test Suite  (mode: {mode})")
    print("=" * 60)

    # Cambia directory nella root del progetto
    project_root = Path(__file__).parent
    os.chdir(project_root)

    asyncio.run(run_all(mode))

    print(f"\n{'=' * 60}")
    if _failures:
        print(f"  RISULTATO: {len(_failures)} test FALLITI")
        for f in _failures:
            print(f"    ❌ {f}")
        sys.exit(1)
    else:
        print("  RISULTATO: tutti i test SUPERATI ✅")
    print("=" * 60)


if __name__ == "__main__":
    main()
