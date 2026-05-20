# Guida Setup VPS Aruba per italian-ports-monitoring

## 1. Acquisto VPS

1. Vai su **[cloud.it/vps](https://www.cloud.it/vps/)**
2. Scegli **VPS O1I2 con IPv4** (4,49€/mese):
   - 1 vCPU
   - 2 GB RAM
   - 40 GB SSD
   - 5 TB traffico
3. Sistema operativo: **Ubuntu 22.04 LTS**
4. Completa l'ordine e annota l'**IP pubblico** del VPS

## 2. Primo Accesso SSH

```bash
ssh root@<IP_VPS>
# Inserisci la password ricevuta via email
```

### Crea utente non-root

```bash
adduser deploy
usermod -aG sudo deploy
su - deploy
```

## 3. Installazione Software

```bash
# Aggiorna sistema
sudo apt update && sudo apt upgrade -y

# Installa Python 3.11+ e dipendenze
sudo apt install -y python3.11 python3.11-venv python3-pip nginx certbot python3-certbot-nginx git

# Installa Playwright dependencies
sudo apt install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
  libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 \
  libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2
```

## 4. Clone Repository

```bash
cd /home/deploy
git clone https://github.com/lucaborgcma/italian-ports-monitoring.git app
cd app

# Crea virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Installa dipendenze
pip install --upgrade pip
pip install -r requirements.txt

# Installa browser Playwright
playwright install chromium
```

## 5. Configurazione Variabili Ambiente

```bash
sudo nano /etc/systemd/system/ports-monitor.service
```

Incolla la configurazione dal file `systemd/ports-monitor.service` (vedi sotto).

## 6. Test Manuale App

```bash
cd /home/deploy/app
source venv/bin/activate
export SECRET_KEY="cambia-questo-valore-segreto-$(openssl rand -hex 16)"
export ADMIN_USER="admin"
export ADMIN_PASSWORD="cambia-questa-password"
export VIEWS_FILE="/home/deploy/app/port_views"
gunicorn --bind 127.0.0.1:8000 --timeout 120 app:app
```

Apri un nuovo terminale e testa:
```bash
curl http://127.0.0.1:8000
```

Se funziona, premi `Ctrl+C` e prosegui.

## 7. Configurazione Nginx

```bash
sudo nano /etc/nginx/sites-available/ports-monitor
```

Incolla la configurazione dal file `nginx/ports-monitor.conf` (vedi sotto).

```bash
# Abilita sito
sudo ln -s /etc/nginx/sites-available/ports-monitor /etc/nginx/sites-enabled/
sudo rm /etc/nginx/sites-enabled/default

# Test configurazione
sudo nginx -t

# Riavvia Nginx
sudo systemctl restart nginx
```

## 8. Configurazione DNS su Aruba

1. Vai su **aruba.it → Pannello di controllo → Domini → lucaborg.it → Gestione DNS**
2. Modifica il **record A**:
   - Host: `@` (oppure vuoto)
   - Valore: `<IP_VPS>`
   - TTL: 3600
3. Salva e attendi la propagazione (1-24 ore)

Verifica propagazione:
```bash
nslookup lucaborg.it
```

## 9. Certificato SSL (HTTPS)

```bash
sudo certbot --nginx -d lucaborg.it -d www.lucaborg.it
```

Segui le istruzioni:
- Inserisci email
- Accetta i termini
- Scegli se condividere email con EFF (opzionale)
- Scegli **2** (Redirect HTTP → HTTPS)

Test rinnovo automatico:
```bash
sudo certbot renew --dry-run
```

## 10. Avvio Servizio Systemd

```bash
# Ricarica configurazioni
sudo systemctl daemon-reload

# Abilita avvio automatico
sudo systemctl enable ports-monitor

# Avvia servizio
sudo systemctl start ports-monitor

# Verifica stato
sudo systemctl status ports-monitor
```

## 11. Verifica Finale

Apri browser e vai su:
- **http://lucaborg.it** → deve reindirizzare a HTTPS
- **https://lucaborg.it** → app funzionante

## 12. Gestione e Manutenzione

### Vedere logs
```bash
sudo journalctl -u ports-monitor -f
```

### Riavviare app
```bash
sudo systemctl restart ports-monitor
```

### Aggiornare codice
```bash
cd /home/deploy/app
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart ports-monitor
```

### Monitorare risorse
```bash
htop
df -h
```

## 13. Sicurezza Aggiuntiva

### Firewall
```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
```

### Fail2Ban (protezione brute-force SSH)
```bash
sudo apt install -y fail2ban
sudo systemctl enable fail2ban
sudo systemctl start fail2ban
```

### Disabilita login root SSH
```bash
sudo nano /etc/ssh/sshd_config
# Cambia: PermitRootLogin no
sudo systemctl restart sshd
```

## 14. Backup Automatico

```bash
# Crea script backup
sudo nano /home/deploy/backup.sh
```

```bash
#!/bin/bash
BACKUP_DIR="/home/deploy/backups"
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p $BACKUP_DIR

# Backup file views e stato
cp /home/deploy/app/port_views $BACKUP_DIR/port_views_$DATE
cp /home/deploy/app/last_state.json $BACKUP_DIR/last_state_$DATE.json

# Mantieni solo ultimi 7 giorni
find $BACKUP_DIR -type f -mtime +7 -delete
```

```bash
chmod +x /home/deploy/backup.sh

# Aggiungi a crontab (ogni giorno alle 3:00)
crontab -e
# 0 3 * * * /home/deploy/backup.sh
```

## Costi Totali

| Voce | Costo |
|------|-------|
| VPS O1I2 | 4,49 €/mese |
| Dominio lucaborg.it | Già posseduto |
| Certificato SSL | Gratis (Let's Encrypt) |
| **Totale** | **4,49 €/mese** |

## Troubleshooting

### App non si avvia
```bash
sudo journalctl -u ports-monitor -n 50
```

### Nginx errori
```bash
sudo nginx -t
sudo tail -f /var/log/nginx/error.log
```

### Certificato SSL non funziona
```bash
sudo certbot certificates
sudo certbot renew --force-renewal
```

### DNS non propaga
```bash
nslookup lucaborg.it
dig lucaborg.it
```

Attendi fino a 24 ore per la propagazione completa.
