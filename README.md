# Web Monitor Bot

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
[![Telegram Bot API](https://img.shields.io/badge/Telegram%20Bot%20API-v21+-blue?logo=telegram)](https://core.telegram.org/bots/api)

Bot Telegram che monitora pagine web e invia notifiche dettagliate quando rileva cambiamenti. Progettato per tracciare bandi, concorsi, graduatorie e qualsiasi pagina che cambia nel tempo.

---

## Funzionalità

- **Monitoraggio multiplo** — Traccia quante pagine vuoi contemporaneamente
- **Diff intelligente** — Mostra esattamente cosa è stato aggiunto/rimosso, con percentuale di similarità
- **Selettore CSS** — Monitora solo una sezione specifica della pagina
- **Monitoraggio doppio** — Con `/fullpage` puoi monitorare contemporaneamente la sezione configurata (CSS selector) E l'intera pagina: se cambia qualcosa ovunque ricevi una notifica, se cambia la sezione specifica ricevi notifica separata + analisi PDF
- **SectionID** — Monitora più sezioni distinte della stessa pagina URL come entry indipendenti
- **Estrazione documenti** — Rileva automaticamente nuovi PDF, DOC, XLSX e li linka
- **Analisi PDF** — Scarica i PDF trovati nella pagina e cerca un valore in una colonna di tabella (es. codice identificativo in una graduatoria) — vedi [docs/pdf_analysis.md](docs/pdf_analysis.md)
- **Setup guidato `/addpdf`** — Conversazione interattiva a 6 passi per configurare l'analisi PDF da Telegram
- **Inline Keyboard** — Pulsanti interattivi sotto ogni notifica per azioni rapide
- **Pausa/Riprendi** — Metti in pausa singoli siti senza rimuoverli
- **Soglia configurabile** — Ignora micro-cambiamenti sotto una soglia percentuale (globale o per-sito)
- **Report giornaliero (opzionale)** — Report automatico ogni mattina con lo stato di tutti i siti; attivabile/disattivabile via `DAILY_REPORT_ENABLED`
- **Notifiche silenziose** — Cambiamenti minori (<5%) non fanno suonare il telefono
- **Persistenza completa** — Siti e contenuti precedenti salvati su JSON, sopravvivono ai riavvii senza perdere cambiamenti
- **Graceful shutdown** — Salvataggio automatico dello stato alla chiusura
- **Asincrono** — HTTP non-bloccante con `aiohttp` per performance ottimali
- **Anti-blocking** — Fallback automatico a Playwright (headless Chromium) per siti che bloccano aiohttp
- **Supporto canali** — Funziona in chat privata, gruppi e canali Telegram
- **Deploy Raspberry Pi** — Script di setup completo con systemd e auto-deploy

---

## Quick Start

```bash
# 1. Clona
git clone https://github.com/aDip95/change-whisperer.git
cd telegramBot

# 2. Virtual environment
python3 -m venv venv
source venv/bin/activate

# 3. Installa dipendenze
pip install -r requirements.txt

# 4. Configura
cp env.example .env
nano .env  # Inserisci BOT_TOKEN e CHAT_ID

# 5. Avvia
python monitor_bot.py
```

---

## Installazione

### Prerequisiti

- Python 3.10+
- Un **Bot Telegram** (creato tramite [@BotFather](https://t.me/BotFather))
- Un **Chat ID** dove ricevere le notifiche

### Passo 1 — Crea il Bot Telegram

1. Apri Telegram e cerca **@BotFather**
2. Invia `/newbot`
3. Scegli un nome e un username
4. Copia il token (es. `123456789:ABCdefGHIjklMNOpqrsTUVwxyz`)

### Passo 2 — Ottieni il Chat ID

| Dove | Come | Formato |
|------|------|---------|
| Chat privata | Scrivi al bot → visita `https://api.telegram.org/bot<TOKEN>/getUpdates` | Positivo (es. `123456789`) |
| Gruppo | Aggiungi il bot → usa [@getidsbot](https://t.me/getidsbot) | Negativo (es. `-1001234567890`) |
| Canale | Aggiungi il bot come admin → usa [@getidsbot](https://t.me/getidsbot) | Negativo (es. `-1001234567890`) |

> Nei canali il bot deve essere amministratore con permesso di inviare messaggi.

### Passo 3 — Configura `.env`

```bash
cp env.example .env
```

| Variabile | Obbligatoria | Descrizione | Default |
|-----------|:---:|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token da BotFather | — |
| `TELEGRAM_CHAT_ID` | ✅ | ID chat/gruppo/canale | — |
| `CHECK_INTERVAL` | ❌ | Secondi tra i controlli | `60` |
| `CHANGE_THRESHOLD` | ❌ | Soglia minima cambiamento (0.0–1.0) | `0.01` |
| `DAILY_REPORT_ENABLED` | ❌ | Attiva il report giornaliero automatico | `true` |
| `DAILY_REPORT_HOUR` | ❌ | Ora del report giornaliero (0-23) | `8` |
| `GIF_PATH` | ❌ | Percorso alla GIF celebrativa | `./data/giphy.gif` |
| `SITES_FILE` | ❌ | Percorso al file JSON dei siti | `./monitored_sites.json` |
| `LOG_LEVEL` | ❌ | Livello di logging | `INFO` |

---

## Comandi Telegram

| Comando | Descrizione |
|---------|-------------|
| `/start` | Avvia il bot con pulsanti interattivi |
| `/add URL [nome] [css] [sectionID]` | Aggiungi sito da monitorare |
| `/add URL --whole` | Monitora tutto il sito: pagine e sottopagine sotto l'URL |
| `/addpdf` | Aggiungi sito con analisi PDF (conversazione guidata) |
| `/remove URL` | Rimuovi sito (con conferma) |
| `/list` | Lista siti con pulsanti per dettagli |
| `/check` | Forza controllo immediato di tutti i siti |
| `/status` | Stato dettagliato del bot |
| `/pause URL[#sectionID]` | Metti in pausa un sito |
| `/resume URL[#sectionID]` | Riprendi monitoraggio di un sito |
| `/threshold URL[#sectionID] 0.001` | Soglia per-sito personalizzata |
| `/browser URL[#sectionID]` | Attiva/disattiva modalità Playwright |
| `/fullpage URL[#sectionID]` | Attiva/disattiva monitoraggio pagina intera (in aggiunta alla sezione CSS) |
| `/selector KEY <css>` | Limita il monitoraggio a una sezione (il selettore può contenere spazi) |
| `/code KEY <codice>` | Imposta il codice da cercare nei PDF (`off` per disattivare) |
| `/expect KEY 3 [Nome]` | Posizione (e nome) attesi in graduatoria |
| `/gif` | Attiva/disattiva la GIF celebrativa nelle notifiche |
| `/report` | Report manuale dello stato |
| `/test` | Invia notifica di test completa |
| `/help` | Guida dettagliata |

> Puoi anche incollare direttamente un URL senza il comando `/add`.

---

## Aggiungere un sito

### Comando `/add`

```
/add URL
/add URL NomeSito
/add URL NomeSito .css-selector
/add URL NomeSito .css-selector SectionID
```

**Selettore CSS** — Limita il monitoraggio a una porzione della pagina.
Esempi: `.main-content`, `#results`, `article`, `.journal-content-article`

Senza selettore, il bot monitora il contenuto principale della pagina (rimuove automaticamente navigazione, header, footer e script).

**SectionID** — Etichetta per monitorare più sezioni distinte della **stessa URL** come entry indipendenti. Utile quando una pagina ha più blocchi (es. Sezione A e Sezione B di un concorso) e vuoi notifiche separate.

```
# Due sezioni della stessa pagina
/add https://esempio.it/pagina .sezione-a SezA
/add https://esempio.it/pagina .sezione-b SezB

# Per gestirle nei comandi, usa URL#SectionID
/pause https://esempio.it/pagina#SezA
/resume https://esempio.it/pagina#SezB
```

### Comando `/addpdf` (guidato)

Il comando avvia una conversazione a 6 passi:

1. **URL** — Incolla l'URL da monitorare
2. **CSS selector** — Sezione specifica della pagina (o Skip per tutta la pagina)
3. **SectionID** — Etichetta per distinguere più sezioni dello stesso URL (o Skip)
4. **Nome** — Nome descrittivo (o Skip)
5. **Analisi PDF** — `[Sì]` / `[No]`
6. **Codice da cercare** — Il valore da trovare nelle tabelle dei PDF (solo se passo 5 = Sì)

Quando il bot scarica un PDF e trova il tuo valore nella colonna configurata, invia una notifica con la posizione nella graduatoria e il link al documento.

Per la documentazione completa sull'analisi PDF vedi [docs/pdf_analysis.md](docs/pdf_analysis.md).

---

## Sorvegliare una graduatoria (nome omissato → pubblicato)

Caso d'uso: sei in graduatoria a una certa posizione, identificato solo dal **codice**. La graduatoria è pubblicata *omissata* (nomi nascosti) e vuoi sapere **quando viene ripubblicata** e soprattutto **quando compare il tuo nome**.

Il bot ancora la ricerca sul **codice** (sempre presente, sempre alla tua posizione). Ad ogni ripubblicazione del PDF di graduatoria invia una di queste notifiche:

- 📋 **Graduatoria aggiornata** — codice trovato, ma il nome è ancora *omissato*.
- 🎉 **NOME PUBBLICATO** — alla tua posizione ora compare un nome (con conferma se coincide con quello atteso).
- ⚠️ **Codice non trovato** — il codice non è più nella graduatoria (anomalia da verificare).

Configurazione (una tantum, via Telegram):

```
# 1. Limita alla sezione giusta (il selettore può avere spazi)
/selector <KEY> div.card:-soup-contains("Funzionari amministrativi")

# 2. Codice da cercare nei PDF della sezione
/code <KEY> ABC123XYZ

# 3. Posizione e nome attesi
/expect <KEY> 3 Mario Rossi
```

Solo i file con **`GRADUATORIA`** nel nome attivano l'analisi nome/posizione: gli altri PDF della sezione (esiti, buste, elenchi) non generano falsi allarmi.

### Monitorare due (o più) sezioni della stessa pagina

Ogni sezione è una entry separata, distinta da un **SectionID**. Ripeti `/addpdf` per ciascuna sezione, indicando lo stesso URL ma **SectionID e selettore diversi**:

```
/addpdf
# URL:        https://www.esempio.gov.it/concorsi
# Selettore:  div.card:-soup-contains("Funzionari amministrativi")   (sezione A)
# SectionID:  A
# ... codice, posizione, nome

/addpdf
# stesso URL
# Selettore:  div.card:-soup-contains("<testo unico della sezione 2>")
# SectionID:  B
# ... codice, posizione, nome della sezione 2
```

Le due sezioni vengono monitorate in modo indipendente (chiavi `URL#A` e `URL#B`). Per trovare il testo che identifica una sezione, apri la pagina: ogni sezione è una card con un titolo tipo *"A - 5 Funzionari amministrativi…"* — scegli una parte di testo che compare **solo** in quella card.

---

## Monitoraggio pagina intera + sezione specifica

Se hai configurato un selettore CSS (es. una sezione specifica di una pagina), normalmente ricevi notifiche solo quando cambia quella sezione. Con `/fullpage` puoi aggiungere anche il monitoraggio dell'intera pagina, senza entry extra e senza HTTP aggiuntivi (l'HTML è già fetchato):

```
# 1. Vedi la chiave esatta del sito con /list
/list

# 2. Attiva il monitoraggio pagina intera
/fullpage https://esempio.it/pagina#sezione-a
```

**Comportamento risultante:**
- Pagina cambia fuori dalla sezione configurata → notifica `🌐 Sito aggiornato!`
- La sezione configurata cambia → notifica `🔔 Aggiornamento rilevato!` + analisi PDF (se configurata)
- Entrambe cambiano → due notifiche separate nello stesso ciclo di check

Per disattivare, riinvia lo stesso comando (toggle):
```
/fullpage https://esempio.it/pagina#sezione-a
```

---

## Architettura

```
telegram-bot/
├── monitor_bot.py          # Entry point (avvia il bot)
├── bot/                    # Package principale
│   ├── __init__.py         # Esporta tutti i simboli pubblici
│   ├── config.py           # Config (dataclass da env variables)
│   ├── models.py           # MonitoredSite (dataclass + serializzazione JSON)
│   ├── utils.py            # escape_html, is_valid_url, truncate
│   ├── scraper.py          # WebScraper (aiohttp + Playwright fallback)
│   ├── diff.py             # DiffAnalyzer (hash SHA-256, similarità, diff)
│   ├── pdf.py              # PDFAnalyzer (pdfplumber, ricerca in tabelle)
│   ├── keyboards.py        # Inline keyboard factories
│   └── handlers.py         # WebMonitorBot + ConversationHandler /addpdf
├── setup_site.py           # Setup di un sito da CLI (esempio d'uso avanzato)
├── debug_scraper.py        # Tool diagnostico di scraping per un URL
├── test_bot.py             # Suite di test e debug
├── requirements.txt
├── env.example
├── raspberry_setup.sh      # Script deploy Raspberry Pi con systemd
├── docs/
│   └── pdf_analysis.md     # Guida all'analisi PDF
├── example/                # Fixture di test
│   ├── sample_graduatoria.pdf  # PDF di esempio (dati fittizi)
│   └── make_fixture.py     # Rigenera la fixture
└── data/
    └── giphy.gif           # GIF celebrativa (opzionale, non inclusa)
```

### Componenti

| Modulo | Responsabilità |
|--------|---------------|
| `bot/config.py` | Configurazione da environment variables |
| `bot/models.py` | `MonitoredSite` con serializzazione JSON — persiste `previous_content` per rilevare cambiamenti anche dopo riavvio |
| `bot/scraper.py` | Fetch HTTP asincrono, estrazione contenuto, link PDF e documenti |
| `bot/diff.py` | Hashing SHA-256, similarità SequenceMatcher, sommario diff HTML |
| `bot/pdf.py` | Download PDF, parsing tabelle con pdfplumber, ricerca valore |
| `bot/keyboards.py` | Factory functions per inline keyboard Telegram |
| `bot/handlers.py` | `WebMonitorBot`, comandi, ConversationHandler, jobs, notifiche |

### Flusso di monitoraggio

```
[Job periodico ogni N secondi]
        │
        ▼
  WebScraper.fetch_page()          ← aiohttp → Playwright (fallback automatico)
        │
        ▼
  WebScraper.extract_content()     ← filtro CSS opzionale + fingerprint link
        │
        ├──► PDFAnalyzer (se pdf_analysis=True)
        │         │
        │    extract_pdf_links()   ← solo PDF nuovi (non già in pdf_results)
        │         │
        │    download_bytes() + search_table()
        │         │
        │    se trovato → notifica con posizione + link PDF
        │
        ▼
  previous_content disponibile?
        │
    ┌───┴───┐
    │ No    │ Sì
    │       ▼
    │  DiffAnalyzer.has_changed()  ← confronto hash SHA-256
    │       │
    │   ┌───┴───┐
    │   │ No    │ Sì
    │   │       ▼
    │   │  change_ratio > soglia?
    │   │       │
    │   │   ┌───┴───┐
    │   │   │ No    │ Sì
    │   │   │       ▼
    │   │   │  summarize_changes()
    │   │   │  extract_documents()
    │   │   │       │
    │   │   │       ▼
    │   │   │  send_notification()
    │   │   │  ├─ Messaggio con inline keyboard
    │   │   │  ├─ GIF (se >5% cambiamento)
    │   │   │  └─ Lista documenti
    │   │   │
    └───┴───┴──────────────────────────────┐
                                           ▼
                             Salva previous_content + hash su JSON
```

> **Nota sulla persistenza:** `previous_content` viene salvato nel JSON ad ogni cambiamento rilevato. Al riavvio del bot, se la pagina è cambiata durante il downtime, il confronto rileva correttamente la differenza e invia notifica. Se il `previous_content` non fosse disponibile (JSON precedente alla v2), il bot usa `previous_hash` come fallback.

---

## Deploy su Raspberry Pi

### Setup automatico

```bash
chmod +x raspberry_setup.sh
./raspberry_setup.sh
```

Lo script configura: virtual environment, servizio `systemd` con auto-restart e `network-online.target`, webhook per auto-deploy da GitHub.

Dopo il setup:
```bash
# 1. Crea .env
nano ~/telegram-bot/.env

# 2. Avvia il bot
sudo systemctl start telegram-bot

# 3. Verifica
sudo systemctl status telegram-bot
journalctl -u telegram-bot -f
```

### Setup manuale

```bash
# 1. Clona il repo
git clone https://github.com/aDip95/change-whisperer.git ~/telegram-bot
cd ~/telegram-bot

# 2. Installa dipendenze sistema
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip

# 3. Venv e dipendenze Python
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 4. (Opzionale) Playwright per siti che bloccano le richieste HTTP
playwright install chromium

# 5. Configura
cp env.example .env
nano .env   # BOT_TOKEN e CHAT_ID obbligatori

# 6. Crea servizio systemd
sudo tee /etc/systemd/system/telegram-bot.service > /dev/null << 'EOF'
[Unit]
Description=Telegram Web Monitor Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<tuo-utente>
WorkingDirectory=/home/<tuo-utente>/telegram-bot
ExecStart=/home/<tuo-utente>/telegram-bot/venv/bin/python monitor_bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot
```

> **Nota:** Sostituisci `<tuo-utente>` con il tuo utente di sistema e adatta i percorsi alla posizione reale del progetto.

> **Nota:** Il bot usa `python-dotenv` per caricare `.env` dalla `WorkingDirectory`. Non usare `EnvironmentFile=` nel service — la sintassi di systemd è incompatibile con il formato `.env` standard.

> **Nota su `network-online.target`:** Su alcune distribuzioni potrebbe richiedere l'attivazione del servizio corrispondente:
> ```bash
> # Con NetworkManager (Raspberry Pi OS recente):
> sudo systemctl enable NetworkManager-wait-online.service
> # Con systemd-networkd:
> sudo systemctl enable systemd-networkd-wait-online.service
> ```
> In alternativa, `Restart=always` con `RestartSec=10` assicura che il bot si riavvii automaticamente se avviato prima che la rete sia pronta.

### Gestione servizio

```bash
sudo systemctl status telegram-bot     # Stato
sudo systemctl restart telegram-bot    # Riavvio (salva stato automaticamente)
journalctl -u telegram-bot -f          # Log in tempo reale
journalctl -u telegram-bot --since "1h ago"  # Log ultima ora
```

> **Attenzione al riavvio:** Prima di `systemctl restart`, il bot salva lo stato in `monitored_sites.json` tramite `post_shutdown`. Il nuovo processo carica esattamente da quel file. Se vuoi modificare manualmente il JSON, usa `stop` → modifica → `start` invece di `restart`, per evitare che `post_shutdown` sovrascriva le tue modifiche.

### Aggiornamento

```bash
cd ~/telegram-bot
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart telegram-bot
```

### Auto-Deploy via GitHub Webhook

1. **GitHub → Settings → Webhooks → Add webhook**
2. **URL:** `http://IP_PI:9000/hooks/telegram-bot-deploy`
3. **Content type:** `application/json`
4. **Secret:** Il webhook secret mostrato dallo script di setup
5. **Events:** Solo `push`

---

## Test e Debug

```bash
# Tutti i test (include scraping reale)
python test_bot.py

# Solo parsing PDF (no rete)
python test_bot.py pdf

# Solo scraping pagina web
python test_bot.py scrape

# Solo logica di monitoraggio (dry run)
python test_bot.py monitor
```

I test PDF usano i file in `example/` come fixture. I test sono autonomi e non richiedono un bot Telegram attivo.

### Diagnostica di scraping

Per capire come il bot "vede" una pagina (status HTTP, contenuto estratto, soglia di cambiamento, fallback Playwright):

```bash
python debug_scraper.py https://www.example.com
```

---

## Contribuire

1. Fork il repository
2. Crea un branch (`git checkout -b feature/nome-feature`)
3. Committa (`git commit -m 'Descrizione'`)
4. Push (`git push origin feature/nome-feature`)
5. Apri una Pull Request

---

## Licenza

Copyright (C) 2025-2026 Deep (@aDip95)

GNU Affero General Public License v3.0 — vedi [LICENSE](LICENSE).
Uso, modifica e redistribuzione liberi; chi fa girare una versione modificata
(anche solo come bot) deve renderne disponibile il codice sotto la stessa licenza.

## Autore

**Deep** — [@aDip95](https://github.com/aDip95)
