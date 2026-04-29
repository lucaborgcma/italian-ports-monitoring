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
        # La risposta è testo puro (non HTML), get_text() su tutto
        raw = BeautifulSoup(r.text, "html.parser").get_text().strip()
        log.info(f"PSA raw preview: {raw[:120]!r}")
        cols_map = ["nave", "viaggio", "eta", "accettazione",
                    "fine_accettazione", "chiusura", "reefer", "imo"]
        data = []
        for row_str in [x for x in raw.split("^$#") if x.strip()]:
            parts = row_str.split("#$^")
            if len(parts) < 2:
                continue
            row = {
                cols_map[i]: (parts[i].strip() or None) if i < len(parts) else None
                for i in range(len(cols_map))
            }
            if not row.get("nave"):
                continue
            row["porto"] = "Genova PSA"
            data.append(row)
        log.info(f"PSA: {len(data)} navi estratte")
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_genova_psa: {e}")
        return {"error": True, "message": str(e), "data": []}

def scrape_spinelli():
    """Genova Spinelli — genoaterminal.com.
    L'API diretta è bloccata da WAF/CAPTCHA su IP cloud.
    Carichiamo prima la home (imposta sessione/cookie), poi navighiamo
    all'endpoint API nello stesso contesto browser — il CAPTCHA non scatta.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("playwright non installato")
        return _empty_table_error("playwright non installato")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(ignore_https_errors=True)
            # Step 1: carica la home per stabilire sessione/cookie
            page.goto("https://www.genoaterminal.com/", timeout=30000)
            page.wait_for_timeout(2000)
            # Step 2: naviga all'API nello stesso contesto — niente CAPTCHA
            api_response = page.goto(
                "https://www.genoaterminal.com/gptPublicService/getvesselsfull",
                timeout=20000
            )
            body_text = api_response.text() if api_response else ""
            browser.close()
    except Exception as e:
        log.error(f"scrape_spinelli browser: {e}")
        return {"error": True, "message": str(e), "data": []}

    try:
        body = json.loads(body_text)
    except Exception:
        log.error(f"Spinelli API non-JSON: {body_text[:200]!r}")
        return _empty_table_error(f"Spinelli: risposta API non JSON")

    v = body.get("IN_ACCETTAZIONE") or []
    data = []
    for x in v:
        nave = x.get("name") or x.get("vesselName") or x.get("vessel")
        if not nave:
            continue
        data.append({
            "nave":     nave,
            "viaggio":  x.get("exportVoyCode") or x.get("voyageCode"),
            "eta":      x.get("eta"),
            "etd":      x.get("etd"),
            "chiusura": x.get("customsDeadline"),
            "servizio": x.get("service") or x.get("lineService"),
            "porto":    "Genova Spinelli",
        })
    log.info(f"Spinelli: {len(data)} navi")
    return {"error": False, "data": data}

def scrape_livorno():
    try:
        r = requests.get("https://www.tdt.it/", timeout=15, verify=False)
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.select_one("table.navi-accettazione")
        if not table:
            return _empty_table_error("Livorno: tabella non trovata")
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
                "nave":              gc("Nome nave"),
                "viaggio":           gc("Viaggio uscita"),
                "eta":               gc("ETB"),
                "fine_accettazione": gc("Chiusura accettazione"),
                "chiusura":          gc("Chiusura doganale"),
                "porto":             "Livorno",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_livorno: {e}")
        return {"error": True, "message": str(e), "data": []}

def scrape_napoli():
    try:
        r = requests.get("https://tfg.bucci.it/@/TFGW_TP_ETA", timeout=15, verify=False)
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.select_one("table#dati")
        if not table:
            return _empty_table_error("Napoli: tabella non trovata")
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
            eta_a = gc("E.T.A.") or ""
            eta_b = gc("E.T.B.") or ""
            eta   = (f"{eta_a} {eta_b}").strip() or None
            data.append({
                "nave":         gc("E.T.S."),
                "viaggio":      gc("TERMINAL DI CONSEGNA"),
                "eta":          eta,
                "accettazione": gc("INT.RIF."),
                "porto":        "Napoli",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_napoli: {e}")
        return {"error": True, "message": str(e), "data": []}

def scrape_venezia():
    try:
        r = requests.get("https://www.vecon.it/tools/info-nave-partenze-arrivi/", timeout=15, verify=False)
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if not table:
            return _empty_table_error("Venezia: tabella non trovata")
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
                "nave":              gc("Nome Nave"),
                "viaggio":           gc("VOY. / RIF. VECON"),
                "eta":               gc("Eta"),
                "fine_accettazione": gc("Chiusura Gate"),
                "accettazione":      gc("Stato"),
                "porto":             "Venezia",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_venezia: {e}")
        return {"error": True, "message": str(e), "data": []}

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
    url = "https://www.terminalsangiorgio.it/"
    html = None
    # Tenta prima con wait_selector specifico, poi con networkidle, poi senza attesa
    for attempt, kwargs in [
        (1, {"wait_selector": "table.tab-elenco"}),
        (2, {"wait_selector": "table"}),
        (3, {}),
    ]:
        html = _fetch_html_browser(url, **kwargs)
        if html:
            log.info(f"San Giorgio: HTML ottenuto al tentativo {attempt}")
            break
    if not html:
        return _empty_table_error("Errore connessione Terminal San Giorgio")
    try:
        soup = BeautifulSoup(html, "lxml")
        # Log delle tabelle trovate per debug
        all_tables = soup.find_all("table")
        log.info(f"San Giorgio: {len(all_tables)} tabelle totali in pagina")
        for i, t in enumerate(all_tables):
            log.info(f"  tabella[{i}] class={t.get('class')} rows={len(t.find_all('tr'))}")

        tables = soup.find_all("table", class_="tab-elenco")
        data_tables = tables[1:] if len(tables) > 1 else tables
        if not data_tables:
            data_tables = [t for t in all_tables if len(t.find_all("tr")) > 1]
        if not data_tables:
            return _empty_table_error("Terminal San Giorgio: nessuna tabella trovata")

        data = []
        for table in data_tables:
            rows = table.find_all("tr")
            # Cerca intestazioni
            header_row = rows[0] if rows else None
            headers = [th.get_text(strip=True) for th in header_row.find_all(["th", "td"])] if header_row else []
            col = {h: i for i, h in enumerate(headers)}
            log.info(f"San Giorgio headers: {headers}")
            for tr in rows[1:]:
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(cells) < 2:
                    continue
                def gc(name, c=cells):
                    return (c[col[name]] or None) if name in col and col[name] < len(c) else None
                # Prova header-based, fallback a indici fissi
                nave = gc("NAME") or gc("Nave") or gc("Vessel") or (cells[0] if cells else None)
                if not nave or nave.upper() in ("NAME", "NAVE", "VESSEL"):
                    continue
                customs = gc("CUT OFF") or gc("Chiusura") or gc("CUSTOMS") or (cells[4] if len(cells) > 4 else None)
                if customs == "-":
                    customs = None
                data.append({
                    "nave":              nave,
                    "eta":               gc("ETA") or gc("ETB") or (cells[1] if len(cells) > 1 else None),
                    "viaggio":           gc("VOY") or gc("Viaggio") or (cells[2] if len(cells) > 2 else None),
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
