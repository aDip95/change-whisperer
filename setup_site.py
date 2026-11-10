#!/usr/bin/env python3
"""
Setup rapido di un sito monitorato da riga di comando.

Aggiunge direttamente un'entry in monitored_sites.json senza passare dal bot,
usando la stessa logica di estrazione di WebScraper per una baseline coerente.
Utile per script di provisioning o casi d'uso avanzati.

Usage:
    python setup_site.py URL
    python setup_site.py URL --name "Nome sito"
    python setup_site.py URL --css ".portlet-body" --browser
    python setup_site.py URL --codice ABC123XYZ        # abilita analisi PDF
    python setup_site.py URL --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

SITES_FILE = os.getenv("SITES_FILE", "./monitored_sites.json")


async def fetch_initial_content(
    url: str, css_selector: str | None, force_browser: bool
) -> tuple[str | None, str | None, bool]:
    """Fetch page and return (content_text, content_hash, use_browser) using the same
    extraction logic as WebScraper so the stored hash is always consistent."""
    from bot.config import Config
    from bot.scraper import WebScraper

    config = Config()
    scraper = WebScraper(config)
    html = None
    use_browser = force_browser

    if not force_browser:
        print("  Tentativo con aiohttp...", end=" ", flush=True)
        html = await scraper._fetch_aiohttp(url)
        if html:
            print(f"OK ({len(html)} chars)")
        else:
            print("fallito")

    if not html:
        print("  Tentativo con Playwright...", end=" ", flush=True)
        html = await scraper._fetch_playwright(url)
        if html:
            use_browser = True
            print(f"OK ({len(html)} chars)")
        else:
            print("fallito")
            await scraper.close()
            return None, None, use_browser

    content = scraper.extract_content(html, css_selector=css_selector, base_url=url)
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    await scraper.close()
    return content, content_hash, use_browser


def load_sites(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def save_sites(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


async def main():
    parser = argparse.ArgumentParser(description="Setup rapido di un sito monitorato")
    parser.add_argument("url", help="URL della pagina da monitorare")
    parser.add_argument("--name", help="Nome descrittivo del sito")
    parser.add_argument("--css", help="Selettore CSS per limitare il monitoraggio")
    parser.add_argument("--section-id", help="Etichetta per distinguere sezioni dello stesso URL")
    parser.add_argument("--codice", help="Valore da cercare nei PDF (abilita l'analisi PDF)")
    parser.add_argument("--browser", action="store_true", help="Forza l'uso di Playwright")
    parser.add_argument("--dry-run", action="store_true", help="Non modifica il file JSON")
    args = parser.parse_args()

    url = args.url
    name = args.name or url
    codice = args.codice.strip().upper() if args.codice else None

    print("=" * 60)
    print("  Setup Monitoraggio Sito")
    print("=" * 60)
    print(f"\nURL:  {url}")
    print(f"Nome: {name}")
    print(f"File: {SITES_FILE}")
    if codice:
        print(f"Analisi PDF attiva — codice cercato: {codice}")

    sites = load_sites(SITES_FILE)

    if url in sites:
        existing = sites[url]
        print(f"\n⚠️  Il sito è già monitorato (nome: {existing.get('name', '?')})")
        print(f"   pdf_analysis: {existing.get('pdf_analysis', False)}")
        print(f"   pdf_search_value: {existing.get('pdf_search_value', 'N/A')}")
        if not args.dry_run:
            resp = input("\nVuoi sovrascrivere la configurazione? [s/N] ").strip().lower()
            if resp not in ("s", "y", "si", "yes"):
                print("Operazione annullata.")
                sys.exit(0)
        else:
            print("  [dry-run: sovrascrittura simulata]")

    print("\nScaricare contenuto iniziale della pagina (per baseline)...")
    content_text, content_hash, use_browser = await fetch_initial_content(
        url, args.css, args.browser
    )
    if content_hash is None:
        print("⚠️  Impossibile scaricare la pagina.")
        resp = input("Continuare comunque senza baseline? [s/N] ").strip().lower()
        if resp not in ("s", "y", "si", "yes"):
            sys.exit(1)

    entry = {
        "url": url,
        "name": name,
        "section_id": args.section_id,
        "previous_content": content_text,
        "previous_hash": content_hash,
        "last_check": datetime.now().isoformat() if content_hash else None,
        "css_selector": args.css,
        "paused": False,
        "error_count": 0,
        "error_notified": False,
        "use_browser": use_browser,
        "threshold": None,
        "pdf_analysis": codice is not None,
        "pdf_search_value": codice,
        "pdf_results": {},
        "monitor_full_page": False,
        "previous_full_content": None,
        "previous_full_hash": None,
    }

    print(f"\nConfigurazione finale:")
    print(f"  Nome:            {entry['name']}")
    print(f"  CSS selector:    {entry['css_selector'] or 'nessuno (tutta la pagina)'}")
    print(f"  Browser mode:    {'Playwright' if entry['use_browser'] else 'aiohttp'}")
    print(f"  Analisi PDF:     {entry['pdf_analysis']}")
    if entry["pdf_analysis"]:
        print(f"  Codice cercato:  {entry['pdf_search_value']}")
    print(f"  Baseline hash:   {entry['previous_hash'][:16] if entry['previous_hash'] else 'N/A'}...")
    print(f"  Baseline content:{' ' + str(len(entry['previous_content'])) + ' chars' if entry['previous_content'] else ' N/A'}")

    if args.dry_run:
        print("\n[DRY RUN] Nessuna modifica al file JSON.")
        print("\nEntry che verrebbe scritta:")
        print(json.dumps(entry, indent=2, ensure_ascii=False))
        return

    sites[url] = entry
    save_sites(SITES_FILE, sites)
    print(f"\n✅ Configurazione salvata in {SITES_FILE}")
    print("\nAvvia il bot con: python monitor_bot.py")
    print("Il bot controllerà la pagina ogni CHECK_INTERVAL secondi.")
    if entry["pdf_analysis"]:
        print("Quando un PDF con il tuo codice viene pubblicato, riceverai una notifica.")


if __name__ == "__main__":
    asyncio.run(main())
