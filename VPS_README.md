# Configurazione VPS per italian-ports-monitoring

Questa directory contiene i file necessari per il deployment su **VPS Aruba O1I2**.

## 📁 Struttura File

```
.
├── docs/
│   └── VPS_SETUP_GUIDE.md          # Guida completa setup VPS
├── nginx/
│   └── ports-monitor.conf           # Configurazione Nginx
├── systemd/
│   └── ports-monitor.service        # Systemd service file
├── scripts/
│   └── deploy-vps.sh               # Script deploy automatico
└── .env.vps.template               # Template variabili ambiente
```

## 🚀 Quick Start

### 1. Prima Volta (Setup Completo)

Segui la guida completa: **[docs/VPS_SETUP_GUIDE.md](docs/VPS_SETUP_GUIDE.md)**

### 2. Deploy Aggiornamenti

Una volta configurato il VPS, per deployare nuove versioni:

```bash
# Sul VPS, connesso via SSH
cd /home/deploy/app
./scripts/deploy-vps.sh
```

## 🔒 Sicurezza

**IMPORTANTE:** Prima del primo deploy:

1. ✅ Genera un `SECRET_KEY` sicuro:
   ```bash
   openssl rand -hex 32
   ```

2. ✅ Cambia la password admin nel file systemd service

3. ✅ Configura firewall:
   ```bash
   sudo ufw allow OpenSSH
   sudo ufw allow 'Nginx Full'
   sudo ufw enable
   ```

4. ✅ Installa Fail2Ban:
   ```bash
   sudo apt install -y fail2ban
   sudo systemctl enable fail2ban
   ```

## 📊 Monitoraggio

### Vedere logs in tempo reale
```bash
sudo journalctl -u ports-monitor -f
```

### Stato servizio
```bash
sudo systemctl status ports-monitor
```

### Risorse server
```bash
htop
df -h
free -h
```

## 🔄 Comandi Utili

| Comando | Descrizione |
|---------|-------------|
| `sudo systemctl restart ports-monitor` | Riavvia app |
| `sudo systemctl stop ports-monitor` | Ferma app |
| `sudo systemctl start ports-monitor` | Avvia app |
| `sudo nginx -t` | Test configurazione Nginx |
| `sudo systemctl reload nginx` | Ricarica Nginx |
| `sudo certbot renew` | Rinnova certificato SSL |

## 🆘 Troubleshooting

### App non risponde
```bash
# Verifica che il servizio sia attivo
sudo systemctl status ports-monitor

# Se non è attivo, guarda gli errori
sudo journalctl -u ports-monitor -n 50
```

### Errori Nginx
```bash
# Test configurazione
sudo nginx -t

# Vedi log errori
sudo tail -f /var/log/nginx/error.log
```

### Out of Memory
```bash
# Controlla RAM
free -h

# Se necessario, aumenta workers in systemd service:
# --workers 1 invece di --workers 2
```

## 💰 Costi

| Servizio | Costo |
|----------|-------|
| VPS Aruba O1I2 | 4,49 €/mese |
| Dominio lucaborg.it | Già posseduto |
| SSL Certificate | Gratis (Let's Encrypt) |
| **TOTALE** | **4,49 €/mese** |

## 📞 Support

- **VPS Aruba:** support.aruba.it
- **Let's Encrypt:** letsencrypt.org/docs
- **Repository:** github.com/lucaborgcma/italian-ports-monitoring
