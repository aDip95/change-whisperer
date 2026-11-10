#!/bin/bash
# ══════════════════════════════════════════════════════════════════════════════
# Setup Telegram Bot con Auto-Deploy su Raspberry Pi
# ══════════════════════════════════════════════════════════════════════════════

set -e

# Configurazione
GITHUB_REPO="${GITHUB_REPO:-https://github.com/aDip95/change-whisperer.git}"
CURRENT_USER="$(whoami)"
BOT_DIR="/home/${CURRENT_USER}/telegram-bot"
WEBHOOK_SECRET="$(openssl rand -hex 20)"

echo "══════════════════════════════════════════════════════════════════"
echo "  Setup Telegram Bot con Auto-Deploy"
echo "  Utente: $CURRENT_USER"
echo "  Directory: $BOT_DIR"
echo "══════════════════════════════════════════════════════════════════"

# 1. Installa dipendenze
echo "[1/6] Installazione dipendenze..."
sudo apt update
sudo apt install -y python3-pip python3-venv git webhook

# 2. Clona repository
echo "[2/6] Clonazione repository..."
if [ -d "$BOT_DIR" ]; then
    echo "Directory esistente, faccio backup..."
    mv "$BOT_DIR" "${BOT_DIR}_backup_$(date +%Y%m%d_%H%M%S)"
fi
git clone "$GITHUB_REPO" "$BOT_DIR"
cd "$BOT_DIR"

# 3. Setup Python virtual environment
echo "[3/6] Setup Python environment..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. Crea script di deploy
echo "[4/6] Creazione script di deploy..."
cat > "$BOT_DIR/deploy.sh" << DEPLOY_SCRIPT
#!/bin/bash
# Script eseguito ad ogni push su GitHub

BOT_DIR="$BOT_DIR"
LOG_FILE="/var/log/telegram-bot-deploy.log"

echo "\$(date): Deploy iniziato" >> "\$LOG_FILE"

cd "\$BOT_DIR"

# Pull ultime modifiche
git fetch origin main
git reset --hard origin/main

# Aggiorna dipendenze se requirements.txt è cambiato
source venv/bin/activate
pip install -r requirements.txt --quiet

# Riavvia il servizio
sudo systemctl restart telegram-bot

echo "\$(date): Deploy completato" >> "\$LOG_FILE"
DEPLOY_SCRIPT
chmod +x "$BOT_DIR/deploy.sh"

# Crea file di log con permessi corretti
sudo touch /var/log/telegram-bot-deploy.log
sudo chown "$CURRENT_USER:$CURRENT_USER" /var/log/telegram-bot-deploy.log

# 5. Crea configurazione webhook
echo "[5/6] Configurazione webhook..."
sudo mkdir -p /etc/webhook
cat > /tmp/hooks.json << HOOKS
[
  {
    "id": "telegram-bot-deploy",
    "execute-command": "$BOT_DIR/deploy.sh",
    "command-working-directory": "$BOT_DIR",
    "response-message": "Deploy avviato",
    "trigger-rule": {
      "and": [
        {
          "match": {
            "type": "payload-hmac-sha256",
            "secret": "$WEBHOOK_SECRET",
            "parameter": {
              "source": "header",
              "name": "X-Hub-Signature-256"
            }
          }
        },
        {
          "match": {
            "type": "value",
            "value": "refs/heads/main",
            "parameter": {
              "source": "payload",
              "name": "ref"
            }
          }
        }
      ]
    }
  }
]
HOOKS
sudo mv /tmp/hooks.json /etc/webhook/hooks.json

# 6. Crea servizi systemd
echo "[6/6] Creazione servizi systemd..."

# Servizio Bot
sudo tee /etc/systemd/system/telegram-bot.service > /dev/null << SERVICE_BOT
[Unit]
Description=Telegram Web Monitor Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/venv/bin/python monitor_bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SERVICE_BOT

# Servizio Webhook
sudo tee /etc/systemd/system/webhook.service > /dev/null << 'SERVICE_WEBHOOK'
[Unit]
Description=Webhook for GitHub Auto-Deploy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/webhook -hooks /etc/webhook/hooks.json -port 9000 -verbose
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SERVICE_WEBHOOK

# Permessi per deploy.sh di riavviare il servizio senza password
echo "$CURRENT_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart telegram-bot" | sudo tee /etc/sudoers.d/telegram-bot

# Abilita e avvia servizi
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot webhook
sudo systemctl start webhook

echo ""
echo "══════════════════════════════════════════════════════════════════"
echo "  SETUP COMPLETATO!"
echo "══════════════════════════════════════════════════════════════════"
echo ""
echo "  IMPORTANTE - Salva questo Webhook Secret:"
echo "  $WEBHOOK_SECRET"
echo ""
echo "  Prossimi passi:"
echo "  1. Crea il file .env:  nano $BOT_DIR/.env"
echo "  2. Avvia il bot:       sudo systemctl start telegram-bot"
echo "  3. Configura GitHub Webhook (vedi istruzioni sotto)"
echo ""
echo "  URL Webhook per GitHub:"
echo "  http://IP_DEL_TUO_PI:9000/hooks/telegram-bot-deploy"
echo ""
echo "══════════════════════════════════════════════════════════════════"