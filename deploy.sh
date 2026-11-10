#!/bin/bash
# Deploy sul Pi, da lanciare a mano dopo un push: ./deploy.sh
set -e

BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_FILE="/var/log/telegram-bot-deploy.log"
trap 'echo "$(date): Deploy FALLITO alla riga $LINENO — bot non riavviato" | tee -a "$LOG_FILE" >&2' ERR

echo "$(date): Deploy iniziato" >> "$LOG_FILE"

cd "$BOT_DIR"

# monitored_sites.json è in .gitignore: reset --hard non lo tocca.
# Pull ultime modifiche
git fetch origin main
git reset --hard origin/main

# Aggiorna dipendenze se requirements.txt è cambiato
source venv/bin/activate
pip install -r requirements.txt --quiet

# Riavvia il servizio
sudo systemctl restart telegram-bot

echo "$(date): Deploy completato" >> "$LOG_FILE"
