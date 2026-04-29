from __future__ import annotations
import json, time, os, logging, threading, requests, urllib3
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Playwright cerca Chromium in questa cartella (impostata anche in render-build.sh)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH",
                      "/opt/render/project/src/.playwright-browsers")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ROME_TZ = ZoneInfo("Europe/Rome")
HTTP_TIMEOUT = 25
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
}

SECRET_KEY = os.environ.get("SECRET_KEY", "cma-cgm-italy-secret-2026")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = generate_password_hash("cma2026")

# ... (Configurazione PORT_GROUPS e PORTS identica a prima) ...
PORT_GROUPS = [
    {"label": "Liguria", "keys": ["GENOVA_PSA", "SPINELLI", "GENOVA_SECH", "SAN_GIORGIO", "LA_SPEZIA"]},
    {"label": "Toscana", "keys": ["LIVORNO"]},
    {"label": "Adriatico", "keys": ["VENEZIA", "TRIESTE"]},
    {"label": "Campania", "keys": ["NAPOLI", "SALERNO"]},
]
PORTS = [
    {"key": "GENOVA_PSA", "name": "Genova PSA", "code": "ITGOA"},
    {"key": "SPINELLI", "name": "Genova Spinelli", "code": "ITGOA"},
    {"key": "GENOVA_SECH", "name": "Genova SECH", "code": "ITGOA"},
    {"key": "SAN_GIORGIO", "name": "Genova San Giorgio", "code": "ITGOA"},
    {"key": "LA_SPEZIA", "name": "La Spezia", "code": "ITSPE"},
    {"key": "LIVORNO", "name": "Livorno", "code": "ITLGH"},
    {"key": "VENEZIA", "name": "Venezia", "code": "ITVCE"},
    {"key": "TRIESTE", "name": "Trieste", "code": "ITTRS"},
    {"key": "NAPOLI", "name": "Napoli", "code": "ITNAP"},
    {"key": "SALERNO", "name": "Salerno", "code": "ITSAL"},
]

app = Flask(__name__)
app.secret_key = SECRET_KEY
login_manager = LoginManager(app)
login_manager.login_view = 'login'

class User(UserMixin):
    def __init__(self, id): self.id = id
@login_manager.user_loader
def load_user(user_id): return User(user_id) if user_id == ADMIN_USER else None

def _fetch_html(url):
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT, verify=False)
        return r.text
    except: return None

def _empty_table_error(msg="Sito richiede rendering JS (dati non disponibili)"):
    return {"error": True, "message": msg, "data": []}

def _fetch_html_browser(url, *, wait_selector=None):
    """Fetch HTML con Chromium headless (Playwright) per pagine JS-rendered."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("playwright non installato")
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(ignore_https_errors=True)
            page.goto(url, timeout=30000)
            if wait_selector:
                page.wait_for_selector(wait_selector, timeout=15000)
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        log.error(f"Browser error {url}: {e}")
        return None

# --- SCRAPERS ---

def scrape_lsz():
    """La Spezia — contshipitalia.com (JS-rendered, usa Chromium headless)."""
    url  = "https://services.contshipitalia.com/it/reports/vessel-acceptance-report.html?terminal=LSCT"
    html = _fetch_html_browser(url, wait_selector="table#open-vessel-voyages")
    if not html:
        return _empty_table_error("Errore connessione La Spezia")
    try:
        soup  = BeautifulSoup(html, "lxml")
        table = soup.select_one("table#open-vessel-voyages")
        if not table:
            return _empty_table_error("La Spezia: tabella non trovata")
        rows = table.select("tbody tr")
        if not rows:
            return _empty_table_error()
        data = []
        for tr in rows:
            cols = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cols) < 3:
                continue
            data.append({
                "nave":         cols[1] if len(cols) > 1 else None,
                "viaggio":      cols[2] if len(cols) > 2 else None,
                "accettazione": cols[4] if len(cols) > 4 else None,
                "status":       cols[8] if len(cols) > 8 else None,
                "eta":          None,
                "porto":        "La Spezia",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_lsz: {e}")
        return {"error": True, "message": str(e), "data": []}

def scrape_genova_psa():
    ts = int(time.time() * 1000)
    url = f"https://online.psagp.it/report_get_data/146?queryArgs=0&clientCodeArgs=0&dhxr{ts}=1"
    try:
        r = requests.get(url, timeout=15, verify=False)
        raw = BeautifulSoup(r.text, "html.parser").get_text().strip()
        data = []
        for row_str in [x for x in raw.split("^$#") if x.strip()]:
            p = row_str.split("#$^")
            if len(p) > 2: data.append({"nave": p[0], "viaggio": p[1], "eta": p[2], "porto": "Genova PSA"})
        return {"error": False, "data": data}
    except: return {"error": True, "data": []}

def scrape_spinelli():
    try:
        r = requests.get("https://www.genoaterminal.com/gptPublicService/getvesselsfull", timeout=15, verify=False)
        body = r.json()
        v = body.get("IN_ACCETTAZIONE") or []
        data = []
        for x in v:
            nave = x.get("name") or x.get("vesselName") or x.get("vessel")
            if not nave:
                log.warning(f"Spinelli: riga senza nome nave: {x}")
                continue
            data.append({
                "nave":    nave,
                "viaggio": x.get("exportVoyCode") or x.get("voyageCode"),
                "eta":     x.get("eta"),
                "etd":     x.get("etd"),
                "chiusura": x.get("customsDeadline"),
                "porto":   "Genova Spinelli",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_spinelli: {e}")
        return {"error": True, "message": str(e), "data": []}

def scrape_livorno():
    try:
        r = requests.get("https://www.tdt.it/", timeout=15, verify=False)
        soup = BeautifulSoup(r.text, "html.parser")
        tr = soup.find("table", class_="navi-accettazione").find_all("tr")[1:]
        return {"error": False, "data": [{"nave": td[0].text.strip(), "eta": td[2].text.strip(), "porto": "Livorno"} for r in tr for td in [r.find_all("td")] if len(td) > 2]}
    except: return {"error": True, "data": []}

def scrape_napoli():
    try:
        r = requests.get("https://tfg.bucci.it/@/TFGW_TP_ETA", timeout=15, verify=False)
        soup = BeautifulSoup(r.text, "html.parser")
        tr = soup.find("table", id="dati").find_all("tr")[1:]
        return {"error": False, "data": [{"nave": td[1].text.strip(), "eta": td[2].text.strip(), "porto": "Napoli"} for r in tr for td in [r.find_all("td")] if len(td) > 2]}
    except: return {"error": True, "data": []}

def scrape_venezia():
    try:
        r = requests.get("https://www.vecon.it/tools/info-nave-partenze-arrivi/", timeout=15, verify=False)
        soup = BeautifulSoup(r.text, "html.parser")
        tr = soup.find("table").find_all("tr")[1:]
        return {"error": False, "data": [{"nave": td[0].text.strip(), "eta": td[2].text.strip(), "porto": "Venezia"} for r in tr for td in [r.find_all("td")] if len(td) > 2]}
    except: return {"error": True, "data": []}

def scrape_sech():
    """Genova SECH — sech.it (Angular Material, JS-rendered)."""
    url  = "https://www.sech.it/"
    html = _fetch_html_browser(url, wait_selector="table.vessels")
    if not html:
        return _empty_table_error("Errore connessione Genova SECH")
    try:
        soup  = BeautifulSoup(html, "lxml")
        table = soup.find("table", class_="vessels")
        if not table:
            return _empty_table_error("Genova SECH: tabella non trovata")
        rows = table.find_all("tr")
        if len(rows) < 2:
            return _empty_table_error()
        headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        col = {h: i for i, h in enumerate(headers)}
        data = []
        for tr in rows[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 2:
                continue
            def gc(name, c=cells):
                return (c[col[name]] or None) if name in col and col[name] < len(c) else None
            nave = gc("Nave")
            if not nave:
                continue
            data.append({
                "nave":    nave,
                "eta":     gc("ETA"),
                "viaggio": gc("Voy In Agenzia"),
                "chiusura": gc("Chiusura Doganale"),
                "porto":   "Genova SECH",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_sech: {e}")
        return {"error": True, "message": str(e), "data": []}

def scrape_trieste():
    """Trieste — trieste-marine-terminal.com (JS-rendered)."""
    url  = "https://www.trieste-marine-terminal.com/it/content/navi-banchina-arrivi-e-partenze"
    html = _fetch_html_browser(url, wait_selector="table.table-hover")
    if not html:
        return _empty_table_error("Errore connessione Trieste")
    try:
        soup  = BeautifulSoup(html, "lxml")
        table = soup.select_one("table.table-hover") or soup.select_one("table.table")
        if not table:
            return _empty_table_error("Trieste: tabella non trovata")
        rows = table.find_all("tr")
        if len(rows) < 2:
            return _empty_table_error()
        headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        col = {h: i for i, h in enumerate(headers)}
        data = []
        for tr in rows[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if not cells:
                continue
            def gc(name, c=cells):
                return (c[col[name]] or None) if name in col and col[name] < len(c) else None
            data.append({
                "nave":    gc("Vessel"),
                "viaggio": gc("Viaggio"),
                "eta":     gc("ETB"),
                "etd":     gc("ETD"),
                "porto":   "Trieste",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_trieste: {e}")
        return {"error": True, "message": str(e), "data": []}

def scrape_salerno():
    """Salerno — salernocontainerterminal.com (JS-rendered, 403 senza browser)."""
    url  = "https://www.salernocontainerterminal.com/ca/an/vessel_schedule.php"
    html = _fetch_html_browser(url, wait_selector="table#tbanavi")
    if not html:
        return _empty_table_error("Errore connessione Salerno")
    try:
        soup  = BeautifulSoup(html, "lxml")
        table = soup.select_one("table#tbanavi")
        if not table:
            return _empty_table_error("Salerno: tabella non trovata")
        rows = table.find_all("tr")
        if len(rows) < 2:
            return _empty_table_error()
        headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        col = {h: i for i, h in enumerate(headers)}
        data = []
        for tr in rows[1:]:
            tds = tr.find_all("td")
            if len(tds) == 1:
                continue
            cells = [td.get_text(strip=True) for td in tds]
            if not cells:
                continue
            def gc(name, c=cells):
                return (c[col[name]] or None) if name in col and col[name] < len(c) else None
            nave = gc("VESSEL")
            if not nave:
                continue
            # Salta righe reefer-only
            if (gc("ACCEPTANCE REEF") or "").strip():
                continue
            data.append({
                "nave":              nave,
                "viaggio":           gc("VOYAGE"),
                "eta":               gc("E.T.A."),
                "fine_accettazione": gc("CLOSING TIME"),
                "porto":             "Salerno",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_salerno: {e}")
        return {"error": True, "message": str(e), "data": []}

def scrape_san_giorgio():
    """Terminal San Giorgio — terminalsangiorgio.it (JS-rendered)."""
    url  = "https://www.terminalsangiorgio.it/"
    # Prova prima con wait_selector, poi senza (fallback se struttura cambiata)
    html = _fetch_html_browser(url, wait_selector="table.tab-elenco")
    if not html:
        html = _fetch_html_browser(url)
    if not html:
        return _empty_table_error("Errore connessione Terminal San Giorgio")
    try:
        soup   = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table", class_="tab-elenco")
        log.info(f"San Giorgio: trovate {len(tables)} tabelle tab-elenco")
        # Prima tabella = solo intestazioni → skip; se c'è solo 1, usala
        data_tables = tables[1:] if len(tables) > 1 else tables
        if not data_tables:
            # Fallback: qualsiasi tabella nella pagina
            data_tables = soup.find_all("table")
            log.info(f"San Giorgio fallback: {len(data_tables)} tabelle generiche")
        if not data_tables:
            return _empty_table_error("Terminal San Giorgio: nessuna tabella trovata")
        data = []
        for table in data_tables:
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(cells) < 2:
                    continue
                nave = cells[0]
                if not nave or nave.upper() == "NAME":
                    continue
                customs = cells[4] if len(cells) > 4 else None
                if customs == "-":
                    customs = None
                data.append({
                    "nave":              nave,
                    "eta":               cells[1] if len(cells) > 1 else None,
                    "viaggio":           cells[2] if len(cells) > 2 else None,
                    "fine_accettazione": customs,
                    "porto":             "Genova San Giorgio",
                })
        if not data:
            return _empty_table_error("Terminal San Giorgio: nessun dato estratto")
        log.info(f"San Giorgio: {len(data)} navi estratte")
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_san_giorgio: {e}")
        return {"error": True, "message": str(e), "data": []}

SCRAPERS = {
    "GENOVA_PSA": scrape_genova_psa, "SPINELLI": scrape_spinelli, "LIVORNO": scrape_livorno,
    "NAPOLI": scrape_napoli, "VENEZIA": scrape_venezia, "TRIESTE": scrape_trieste,
    "LA_SPEZIA": scrape_lsz, "SALERNO": scrape_salerno, "GENOVA_SECH": scrape_sech,
    "SAN_GIORGIO": scrape_san_giorgio,
}

# --- ROUTES ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('username') == ADMIN_USER and request.form.get('password') == 'cma2026':
            login_user(User(ADMIN_USER)); return redirect(url_for('index'))
    return '<form method="post"><input name="username"><input type="password" name="password"><button>Login</button></form>'

@app.route('/refresh/<key>')
@login_required
def refresh_port(key):
    if key not in SCRAPERS: return jsonify({"error": True}), 404
    return jsonify({"key": key, "result": SCRAPERS[key]()})

@app.route('/')
@login_required
def index():
    ports_json = json.dumps([{"key": p["key"], "name": p["name"], "code": p["code"]} for p in PORTS])
    groups = [{**g, "ports": [{p["key"]: p for p in PORTS}[k] for k in g["keys"] if k in {p["key"]: p for p in PORTS}]} for g in PORT_GROUPS]
    return render_template("index.html", port_groups=groups, ports_json=ports_json)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
