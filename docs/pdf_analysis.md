# Analisi PDF — Guida

Il bot può scaricare automaticamente i PDF trovati nella pagina monitorata e cercare un valore specifico all'interno delle loro tabelle. Quando il valore viene trovato, ricevi una notifica immediata con la posizione nella graduatoria.

---

## Come funziona

Ad ogni ciclo di monitoraggio, se l'analisi PDF è attiva, il bot:

1. Estrae tutti i link PDF dalla pagina (opzionalmente limitati alla sezione CSS configurata)
2. Ignora i PDF già analizzati in precedenza (tracciati in `pdf_results`)
3. Scarica i nuovi PDF
4. Per ogni pagina del PDF, cerca tabelle con una colonna il cui header contiene `MECCANOGRAFICO`
5. Se nella stessa riga c'è una colonna `POSIZIONE` **o `GRADUATORIA`**, la usa come valore di ranking
6. Se il valore cercato viene trovato, invia una notifica Telegram con posizione e link al documento

L'analisi è **indipendente** dal rilevamento di cambiamenti nella pagina: i PDF vengono analizzati ad ogni check, anche se la pagina non è cambiata. Ogni PDF URL viene processato **una sola volta** — non riceverai notifiche duplicate per lo stesso documento.

---

## Formato tabella supportato

Il bot riconosce automaticamente tabelle con questo schema:

| POSIZIONE | CODICE MECCANOGRAFICO | COGNOME | NOME | ... |
|-----------|----------------------|---------|------|-----|
| 1 | ABC123XYZ | Rossi | Mario | ... |
| 2 | DEF456UVW | Bianchi | Anna | ... |

**Regole di matching:**
- La colonna viene identificata cercando `MECCANOGRAFICO` nell'header (case-insensitive, ignora newline e spazi multipli)
- La colonna `POSIZIONE` viene identificata allo stesso modo
- Il confronto del valore cercato è **case-insensitive**
- Funziona su tabelle multi-pagina (ogni pagina del PDF viene esaminata)

---

## Configurazione via Telegram (`/addpdf`)

Il comando `/addpdf` avvia una conversazione guidata a 5 passi:

```
Passo 1/5 — URL da monitorare
  → https://www.esempio.gov.it/concorso/graduatoria

Passo 2/5 — CSS selector (opzionale)
  → #sezione-graduatoria   oppure   [Skip]

Passo 3/5 — Nome del sito (opzionale)
  → Concorso Esempio 2024   oppure   [Skip]

Passo 4/5 — Analisi PDF
  → [Sì, analizza PDF]   oppure   [No]

Passo 5/5 — Valore da cercare
  → ABC123XYZ
```

Al termine, il bot scarica la pagina per creare la baseline e avvia il monitoraggio.

---

## Configurazione da script (`setup_site.py`)

Per casi d'uso specifici lo script `setup_site.py` popola direttamente `monitored_sites.json` senza passare dal bot:

```bash
python setup_site.py https://www.esempio.gov.it/concorso --name "Concorso Esempio" --codice ABC123XYZ
```

Internamente costruisce un'entry come questa:

```python
entry = {
    "url": "https://www.esempio.gov.it/concorso",
    "name": "Concorso Esempio",
    "previous_hash": content_hash,
    "last_check": datetime.now().isoformat(),
    "css_selector": None,
    "paused": False,
    "error_count": 0,
    "error_notified": False,
    "use_browser": False,
    "threshold": None,
    "pdf_analysis": True,
    "pdf_search_value": "TUO_CODICE_QUI",
    "pdf_results": {},
}
```

---

## Notifica ricevuta

Quando il codice viene trovato, il bot invia:

```
🎯 Codice trovato in graduatoria!

📍 Concorso Esempio 2024
📄 Documento: graduatoria_definitiva.pdf

🔑 Codice: ABC123XYZ
🏆 Posizione: 42

🕐 12/05/2026 14:32:11
```

---

## Stato analisi PDF (`/list` → info sito)

Nel dettaglio di un sito si vedono:
- **Analisi PDF: attiva**
- **Cerco:** il valore configurato
- **PDF analizzati:** quanti documenti sono stati controllati
- **Trovati:** quanti documenti contenevano il valore cercato

---

## Edge case e limitazioni

| Scenario | Comportamento |
|----------|--------------|
| PDF non scaricabile | Marcato come `download_failed`, nessun retry automatico |
| PDF senza tabelle | Nessuna notifica, marcato come `not_found` |
| Colonna MECCANOGRAFICO assente | PDF ignorato (nessun match possibile) |
| Colonna POSIZIONE assente | Posizione riportata come `N/A` |
| PDF già analizzato | Saltato — tracciato in `pdf_results` |
| Valore non trovato | Marcato come `not_found`, nessuna notifica |
| pdfplumber non installato | Errore nel log, analisi disabilitata |

Per installare pdfplumber: `pip install pdfplumber`

---

## Aggiungere supporto per altri formati di tabella

Se la tabella del tuo PDF usa colonne con nomi diversi, puoi modificare `bot/pdf.py`:

```python
# Cerca header alternativi
codice_idx = next(
    (i for i, h in enumerate(headers) if "MECCANOGRAFICO" in h or "CODICE" in h),
    None,
)
pos_idx = next(
    (i for i, h in enumerate(headers) if "POSIZIONE" in h or "RANK" in h or "POS" in h),
    None,
)
```

La logica di normalizzazione degli header (`" ".join(str(h or "").split()).upper()`) gestisce automaticamente newline multipli e spazi extra comuni nei PDF estratti da tabelle complesse.

---

## Sorvegliare la graduatoria: nome omissato → pubblicato (`/expect`)

Caso tipico: sei in graduatoria a una posizione fissa, identificato solo dal **codice**, e il documento è pubblicato *omissato* (nomi nascosti). Vuoi sapere quando viene ripubblicato e quando compare il tuo nome.

Il bot **ancora la ricerca sul codice** (sempre presente, stabile), legge la riga corrispondente e ne ricava la posizione e — se presente — il nome.

```
/selector KEY div.card:-soup-contains("Funzionari amministrativi")   # limita alla sezione
/code KEY ABC123XYZ                                                  # codice da cercare
/expect KEY 3 Mario Rossi                                            # posizione e nome attesi
/expect KEY reset                                                    # rimuovi
```

Ad ogni ripubblicazione della graduatoria (nuovo file → nuovo URL) il bot invia:

- 📋 **Graduatoria aggiornata** — codice trovato, nome ancora *omissato* (non pubblicato).
- 🎉 **NOME PUBBLICATO** — alla tua riga ora compare un nome; con conferma se coincide con quello atteso, o `⚠️` se è diverso.
- ⚠️ **Codice non trovato** — il codice non è più in graduatoria (anomalia).

Il rilevamento del nome scarta i marcatori di omissione (`OMISSIS`/`OMISSATO`, anche spezzati su più righe), i punteggi e le date: considera "nome pubblicato" solo se resta testo alfabetico reale.

**Solo i file la cui denominazione contiene `GRADUATORIA`** attivano questa analisi: gli altri PDF della sezione (esiti, buste, elenchi) restano alla semplice ricerca del codice. Se la sezione è ristretta con un selettore CSS che smette di combaciare, l'analisi PDF **non** ripiega sull'intera pagina (nessun PDF analizzato) per non incrociare graduatorie di altre sezioni.
