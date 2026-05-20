# ✅ Checklist Setup VPS Aruba

Usa questa checklist per non dimenticare nessun passaggio.

## 📋 Pre-Requisiti

- [ ] Account Aruba cloud.it
- [ ] Dominio lucaborg.it attivo su Aruba
- [ ] GitHub token revocato (già fatto ✅)
- [ ] File sensibili protetti da .gitignore (già fatto ✅)

## 🛒 Fase 1: Acquisto VPS

- [ ] Vai su [cloud.it/vps](https://www.cloud.it/vps/)
- [ ] Ordina **VPS O1I2 con IPv4** (4,49€/mese)
  - [ ] Sistema operativo: Ubuntu 22.04 LTS
- [ ] Annota **IP pubblico VPS**: `___________________`
- [ ] Annota **password root**: `___________________`

## 🔐 Fase 2: Primo Accesso e Sicurezza

- [ ] Connessione SSH: `ssh root@<IP_VPS>`
- [ ] Crea utente deploy:
  ```bash
  adduser deploy
  usermod -aG sudo deploy
  ```
- [ ] Testa login utente deploy:
  ```bash
  su - deploy
  ```

## 📦 Fase 3: Installazione Software

- [ ] Aggiorna sistema:
  ```bash
  sudo apt update && sudo apt upgrade -y
  ```
- [ ] Installa Python, Nginx, Certbot, Git:
  ```bash
  sudo apt install -y python3.11 python3.11-venv python3-pip \
    nginx certbot python3-certbot-nginx git
  ```
- [ ] Installa dipendenze Playwright:
  ```bash
  sudo apt install -y libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxkbcommon0 libxcomposite1 \
    libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2
  ```

## 🚀 Fase 4: Deploy Applicazione

- [ ] Clone repository:
  ```bash
  cd /home/deploy
  git clone https://github.com/lucaborgcma/italian-ports-monitoring.git app
  cd app
  ```
- [ ] Crea virtual environment:
  ```bash
  python3.11 -m venv venv
  source venv/bin/activate
  ```
- [ ] Installa dipendenze Python:
  ```bash
  pip install --upgrade pip
  pip install -r requirements.txt
  ```
- [ ] Installa browser Playwright:
  ```bash
  playwright install chromium
  ```

## 🔧 Fase 5: Configurazione Systemd

- [ ] Genera SECRET_KEY:
  ```bash
  openssl rand -hex 32
  ```
  Copia qui: `___________________________________`

- [ ] Scegli password admin:
  Password: `___________________`

- [ ] Copia file systemd:
  ```bash
  sudo cp /home/deploy/app/systemd/ports-monitor.service \
    /etc/systemd/system/ports-monitor.service
  ```

- [ ] Modifica file con i tuoi valori:
  ```bash
  sudo nano /etc/systemd/system/ports-monitor.service
  ```
  Cambia:
  - `SECRET_KEY=`
  - `ADMIN_PASSWORD=`

- [ ] Ricarica e avvia:
  ```bash
  sudo systemctl daemon-reload
  sudo systemctl enable ports-monitor
  sudo systemctl start ports-monitor
  sudo systemctl status ports-monitor
  ```

## 🌐 Fase 6: Configurazione Nginx

- [ ] Copia configurazione Nginx:
  ```bash
  sudo cp /home/deploy/app/nginx/ports-monitor.conf \
    /etc/nginx/sites-available/ports-monitor
  ```

- [ ] Abilita sito:
  ```bash
  sudo ln -s /etc/nginx/sites-available/ports-monitor \
    /etc/nginx/sites-enabled/
  sudo rm /etc/nginx/sites-enabled/default
  ```

- [ ] Test configurazione:
  ```bash
  sudo nginx -t
  ```

- [ ] Riavvia Nginx:
  ```bash
  sudo systemctl restart nginx
  ```

## 🔗 Fase 7: Configurazione DNS

- [ ] Vai su [aruba.it → Pannello Domini](https://www.aruba.it)
- [ ] Seleziona dominio `lucaborg.it`
- [ ] Vai in **Gestione DNS**
- [ ] Modifica record A:
  - Host: `@` o vuoto
  - Valore: `<IP_VPS>` (annotato sopra)
  - TTL: 3600
- [ ] Salva modifiche
- [ ] Attendi propagazione DNS (1-24 ore)
- [ ] Verifica propagazione:
  ```bash
  nslookup lucaborg.it
  dig lucaborg.it
  ```

## 🔒 Fase 8: Certificato SSL

- [ ] Attendi che DNS sia propagato (lucaborg.it punta al VPS)
- [ ] Ottieni certificato SSL:
  ```bash
  sudo certbot --nginx -d lucaborg.it -d www.lucaborg.it
  ```
- [ ] Inserisci email: `___________________`
- [ ] Accetta termini: Y
- [ ] Scegli redirect HTTPS: opzione 2
- [ ] Test rinnovo automatico:
  ```bash
  sudo certbot renew --dry-run
  ```

## 🛡️ Fase 9: Sicurezza

- [ ] Configura firewall:
  ```bash
  sudo ufw allow OpenSSH
  sudo ufw allow 'Nginx Full'
  sudo ufw enable
  sudo ufw status
  ```

- [ ] Installa Fail2Ban:
  ```bash
  sudo apt install -y fail2ban
  sudo systemctl enable fail2ban
  sudo systemctl start fail2ban
  ```

- [ ] Disabilita login root SSH:
  ```bash
  sudo nano /etc/ssh/sshd_config
  # Cambia: PermitRootLogin no
  sudo systemctl restart sshd
  ```
  ⚠️ Prima assicurati di poter entrare con utente deploy!

## ✅ Fase 10: Verifica Finale

- [ ] Apri browser
- [ ] Vai su `http://lucaborg.it`
- [ ] Verifica redirect automatico a `https://lucaborg.it`
- [ ] Verifica che l'app funzioni
- [ ] Testa login admin
- [ ] Verifica che i dati vengano mostrati

## 📊 Fase 11: Monitoraggio

- [ ] Installa htop:
  ```bash
  sudo apt install -y htop
  ```

- [ ] Bookmark comandi utili:
  ```bash
  # Logs live
  sudo journalctl -u ports-monitor -f
  
  # Stato servizio
  sudo systemctl status ports-monitor
  
  # Risorse
  htop
  free -h
  df -h
  ```

## 🔄 Fase 12: Backup

- [ ] Crea script backup:
  ```bash
  nano /home/deploy/backup.sh
  ```
  Copia contenuto dalla guida (Sezione 14)

- [ ] Rendi eseguibile:
  ```bash
  chmod +x /home/deploy/backup.sh
  ```

- [ ] Aggiungi a crontab:
  ```bash
  crontab -e
  # Aggiungi: 0 3 * * * /home/deploy/backup.sh
  ```

## 🎉 Completato!

Se tutti i checkbox sono spuntati, il tuo VPS è pronto!

- 🌐 **Sito:** https://lucaborg.it
- 💰 **Costo:** 4,49€/mese
- 🔒 **HTTPS:** Attivo con Let's Encrypt
- ♻️ **Auto-restart:** Abilitato
- 🔄 **Deploy futuro:** `./scripts/deploy-vps.sh`

---

## 📝 Note Importanti

- **Deploy aggiornamenti:** Da oggi in poi, per aggiornare l'app basta fare `git push` su GitHub, poi sul VPS eseguire `./scripts/deploy-vps.sh`
- **Certificato SSL:** Si rinnova automaticamente ogni 90 giorni
- **Backup:** Eseguito automaticamente ogni notte alle 3:00
- **Logs:** Salvati da systemd, visibili con `journalctl`

## 🆘 Se Qualcosa Va Male

1. **Servizio non si avvia:**
   ```bash
   sudo journalctl -u ports-monitor -n 50
   ```

2. **Nginx errori:**
   ```bash
   sudo nginx -t
   sudo tail -f /var/log/nginx/error.log
   ```

3. **DNS non propaga:**
   Attendi fino a 24 ore, poi contatta support Aruba

4. **SSL non funziona:**
   Verifica che DNS punti correttamente al VPS prima di eseguire certbot
