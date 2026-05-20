#!/bin/bash
set -e

echo "🚀 Deploy Script per VPS Aruba"
echo "================================"

# Colori
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

APP_DIR="/home/deploy/app"
SERVICE_NAME="ports-monitor"

# Verifica di essere sul VPS
if [ ! -d "$APP_DIR" ]; then
    echo -e "${RED}❌ Errore: directory $APP_DIR non trovata${NC}"
    echo "Questo script deve essere eseguito sul VPS, non in locale."
    exit 1
fi

cd $APP_DIR

# Backup dello stato attuale
echo -e "${YELLOW}📦 Backup stato attuale...${NC}"
if [ -f "last_state.json" ]; then
    cp last_state.json "last_state.backup.$(date +%Y%m%d_%H%M%S).json"
fi
if [ -f "port_views" ]; then
    cp port_views "port_views.backup.$(date +%Y%m%d_%H%M%S)"
fi

# Pull ultimo codice
echo -e "${YELLOW}📥 Aggiornamento codice da GitHub...${NC}"
git fetch origin
git reset --hard origin/main

# Attiva virtual environment
echo -e "${YELLOW}🐍 Attivazione virtual environment...${NC}"
source venv/bin/activate

# Aggiorna dipendenze
echo -e "${YELLOW}📚 Aggiornamento dipendenze...${NC}"
pip install --upgrade pip
pip install -r requirements.txt

# Verifica Playwright
if ! playwright show | grep chromium > /dev/null 2>&1; then
    echo -e "${YELLOW}🎭 Installazione browser Playwright...${NC}"
    playwright install chromium
fi

# Riavvia servizio
echo -e "${YELLOW}♻️  Riavvio servizio...${NC}"
sudo systemctl restart $SERVICE_NAME

# Attendi e verifica
sleep 3
if sudo systemctl is-active --quiet $SERVICE_NAME; then
    echo -e "${GREEN}✅ Deploy completato con successo!${NC}"
    echo ""
    echo "📊 Stato servizio:"
    sudo systemctl status $SERVICE_NAME --no-pager -l
    echo ""
    echo "📝 Ultimi log:"
    sudo journalctl -u $SERVICE_NAME -n 10 --no-pager
else
    echo -e "${RED}❌ Errore: il servizio non si è avviato${NC}"
    echo ""
    echo "📝 Log errori:"
    sudo journalctl -u $SERVICE_NAME -n 30 --no-pager
    exit 1
fi

echo ""
echo -e "${GREEN}🎉 Deploy completato alle $(date)${NC}"
echo "🌐 Sito: https://lucaborg.it"
