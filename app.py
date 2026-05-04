from __future__ import annotations
import json, re, time, os, logging, threading, requests, urllib3
from pathlib import Path
from datetime import datetime, timezone
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
AISHUB_USERNAME = os.environ.get("AISHUB_USERNAME", "")

# ... (Configurazione PORT_GROUPS e PORTS identica a prima) ...
PORT_GROUPS = [
    {"label": "Liguria", "keys": ["GENOVA_PSA", "SPINELLI", "GENOVA_SECH", "SAN_GIORGIO", "LA_SPEZIA"]},
    {"label": "Toscana", "keys": ["LIVORNO"]},
    {"label": "Adriatico", "keys": ["VENEZIA", "TRIESTE"]},
    {"label": "Campania", "keys": ["NAPOLI", "CONATECO", "SALERNO"]},
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
    {"key": "CONATECO", "name": "Napoli Conateco", "code": "ITNAP"},
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

_VIEWS_FILE = Path(os.environ.get("VIEWS_FILE",
                   "/opt/render/project/.port_views"))

def _load_views():
    try:
        return int(_VIEWS_FILE.read_text().strip())
    except:
        return 0

def _save_views(n):
    try:
        _VIEWS_FILE.write_text(str(n))
    except:
        pass

_page_views = _load_views()

_DATE_FORMATS = [
    "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
    "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
    "%d %b %Y %H:%M", "%d %b %Y", "%d %B %Y %H:%M", "%d %B %Y",
    "%Y%m%d %H:%M:%S", "%Y%m%d %H:%M", "%Y%m%d",
]
_DATE_FIELDS = {"eta", "etb", "etd", "accettazione", "fine_accettazione", "chiusura"}

def _normalize_date(val):
    if not val:
        return val
    s = str(val).strip()
    if not s or s in ("-", "—", "N/A", "n/a"):
        return None
    # Rimuovi timezone offset tipo "+0200"
    s = re.sub(r'\s*[+-]\d{4}$', '', s).strip()
    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.hour == 0 and dt.minute == 0:
                return dt.strftime("%d/%m/%Y")
            return dt.strftime("%d/%m/%Y %H:%M")
        except ValueError:
            continue
    return val  # non parsabile: restituisce as-is

def _normalize_dates(row: dict) -> dict:
    for k in _DATE_FIELDS:
        if k in row:
            row[k] = _normalize_date(row[k])
    return row

def _fetch_html(url):
    try:
        r = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT, verify=False)
        return r.text
    except: return None

def _empty_table_error(msg="Sito richiede rendering JS (dati non disponibili)"):
    return {"error": True, "message": msg, "data": []}

def _fetch_html_browser(url, *, wait_selector=None, wait_until="load"):
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
            page.goto(url, timeout=30000, wait_until=wait_until)
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
    
def scrape_spinelli():
    """Genova Spinelli — genoaterminal.com
    API pubblica, nessun WAF su chiamata diretta server-to-server.
    Restituisce IN_ACCETTAZIONE e PROSSIME_APERTURE.
    """
    URL = "https://www.genoaterminal.com/gptPublicService/getvesselsfull"
    HEADERS = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (compatible; dashboard/1.0)",
    }

    try:
        r = requests.get(URL, headers=HEADERS, timeout=15)
        r.raise_for_status()
        body = r.json()
    except requests.exceptions.RequestException as e:
        log.error(f"scrape_spinelli: {e}")
        return {"error": True, "message": str(e), "data": []}
    except ValueError as e:
        log.error(f"scrape_spinelli JSON parse error: {e}")
        return {"error": True, "message": f"Risposta non JSON: {e}", "data": []}

    def _parse_vessel(x, section):
        nave = x.get("name")
        if not nave:
            return None
        
        def fmt_dt(val):
            """Formatta ISO datetime → stringa leggibile, None se assente o data anomala."""
            if not val:
                return None
            try:
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                # Scarta date farlocche (anno < 2000)
                if dt.year < 2000:
                    return None
                return dt.astimezone(timezone.utc).strftime("%d/%m/%Y %H:%M")
            except Exception:
                return val

        return {
            "nave":       nave,
            "viaggio":    x.get("exportVoyCode") or x.get("importVoyCode"),
            "eta":        fmt_dt(x.get("eta")),
            "etd":        fmt_dt(x.get("etd")),
            "chiusura":   fmt_dt(x.get("customsDeadline")),
            "imo_reefer": fmt_dt(x.get("imoReeferAcceptance")),
            "nota":       x.get("note") or None,
            "porto":      "Genova Spinelli",
        }

    data = []
    for section_key in ("IN_ACCETTAZIONE", "PROSSIME_APERTURE"):
        for x in body.get(section_key) or []:
            vessel = _parse_vessel(x, section_key)
            if vessel:
                data.append(vessel)

    log.info(f"Spinelli: {len(data)} navi")

    return {"error": False, "data": data}
def scrape_san_giorgio():
    """Terminal San Giorgio — terminalsangiorgio.it (JS-rendered).
    3 tabelle class=tab-elenco: la prima è solo header, le altre contengono i dati.
    Colonne: NAME(0) | ETA(1) | VOY IN(2) | VOY OUT(3) | CUSTOMS CLOSED(4) | ETD(5)
    """
    url = "https://www.terminalsangiorgio.it/"
    html = _fetch_html_browser(url, wait_selector="table.tab-elenco")
    if not html:
        return _empty_table_error("Errore connessione Terminal San Giorgio")
    try:
        soup   = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table", class_="tab-elenco")
        log.info(f"San Giorgio: {len(tables)} tabelle tab-elenco trovate")
        data_tables = tables[1:] if len(tables) > 1 else tables
        if not data_tables:
            return _empty_table_error("Terminal San Giorgio: tabelle dati non trovate")
        data = []
        for table in data_tables:
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(cells) < 2:
                    continue
                nave = cells[0]
                if not nave:
                    continue
                customs = cells[4] if len(cells) > 4 else None
                if customs == "-":
                    customs = None
                data.append({
                    "nave":              nave,
                    "eta":               cells[1] if len(cells) > 1 else None,
                    "viaggio":           cells[2] if len(cells) > 2 else None,
                    "etd":               cells[5] if len(cells) > 5 else None,
                    "fine_accettazione": customs,
                    "porto":             "Genova San Giorgio",
                })
        if not data:
            return _empty_table_error("Terminal San Giorgio: nessun dato estratto")
        log.info(f"San Giorgio: {len(data)} navi")
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_san_giorgio: {e}")
        return {"error": True, "message": str(e), "data": []}

def scrape_conateco():
    """Conateco Napoli — API JSON pubblica."""
    headers = {**HTTP_HEADERS,
               "Referer": "https://www.conateco.it/berth-forecast/",
               "Origin":  "https://www.conateco.it",
               "Accept":  "application/json, text/plain, */*"}
    data = []
    for stato in ("PRESENTI", "PREVISTE", "ORMEGGIATE"):
        url = f"https://api.conateco.it/ConatecoServicesApi/BerthForecast?stato={stato}"
        try:
            r = requests.get(url, headers=headers, timeout=15, verify=False)
            log.info(f"Conateco {stato}: HTTP {r.status_code} ({len(r.text)} bytes)")
            items = r.json()
        except Exception as e:
            log.warning(f"Conateco {stato}: {e}")
            continue
        if not isinstance(items, list):
            log.warning(f"Conateco {stato}: risposta non è lista: {str(items)[:100]}")
            continue
        for x in items:
            nave = x.get("nome_nave")
            if not nave:
                continue
            d = x.get("data_previ") or ""
            o = (x.get("ora_previs") or "").replace(".", ":")
            eta = f"{d} {o}".strip() or None
            dac = x.get("tdtac1") or ""
            oac = x.get("torac1") or ""
            accettazione = f"{dac} {oac}".strip() or None
            dcd = x.get("tdtcd1") or ""
            ocd = x.get("torcd1") or ""
            chiusura = f"{dcd} {ocd}".strip() or None
            data.append({
                "nave":         nave,
                "viaggio":      x.get("id_viaggio"),
                "servizio":     (x.get("cod_lin_nv") or "").strip() or None,
                "eta":          eta,
                "accettazione": accettazione,
                "chiusura":     chiusura,
                "porto":        "Napoli Conateco",
            })
    if not data:
        return _empty_table_error("Conateco: nessun dato ricevuto dalle API")
    log.info(f"Conateco: {len(data)} navi totali")
    return {"error": False, "data": data}


SCRAPERS = {
    "GENOVA_PSA": scrape_genova_psa, "SPINELLI": scrape_spinelli, "LIVORNO": scrape_livorno,
    "NAPOLI": scrape_napoli, "VENEZIA": scrape_venezia, "TRIESTE": scrape_trieste,
    "LA_SPEZIA": scrape_lsz, "SALERNO": scrape_salerno, "GENOVA_SECH": scrape_sech,
    "SAN_GIORGIO": scrape_san_giorgio, "CONATECO": scrape_conateco,
}

# --- ROUTES ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('username') == ADMIN_USER and request.form.get('password') == 'cma2026':
            login_user(User(ADMIN_USER)); return redirect(url_for('index'))
    return render_template('login.html')

@app.route('/refresh/<key>')
@login_required
def refresh_port(key):
    if key not in SCRAPERS: return jsonify({"error": True}), 404
    result = SCRAPERS[key]()
    if not result.get("error") and result.get("data"):
        result["data"] = [_normalize_dates(r) for r in result["data"]]
    return jsonify({"key": key, "result": result})

@app.route('/')
@login_required
def index():
    global _page_views
    _page_views += 1
    _save_views(_page_views)
    ports_json = json.dumps([{"key": p["key"], "name": p["name"], "code": p["code"]} for p in PORTS])
    groups = [{**g, "ports": [{p["key"]: p for p in PORTS}[k] for k in g["keys"] if k in {p["key"]: p for p in PORTS}]} for g in PORT_GROUPS]
    return render_template("index.html", port_groups=groups, ports_json=ports_json, page_views=_page_views)

@app.route('/vessel_positions', methods=['POST'])
@login_required
def vessel_positions():
    if not AISHUB_USERNAME:
        return jsonify({"error": True, "message": "AISHUB_USERNAME non configurato"})
    names_req = request.json.get("names", []) if request.is_json else []
    names = {n.upper().strip() for n in names_req if n}
    if not names:
        return jsonify({})
    # Acque italiane + Adriatico + Tirreno
    url = (f"https://data.aishub.net/ws.php?username={AISHUB_USERNAME}"
           f"&format=1&output=json&compress=0"
           f"&latmin=36&latmax=46&lonmin=6&lonmax=22")
    try:
        r = requests.get(url, timeout=20)
        payload = r.json()
    except Exception as e:
        log.error(f"AISHub: {e}")
        return jsonify({"error": True, "message": str(e)})
    # AISHub format 1: [{ERROR:false}, [{MMSI:..., NAME:..., LATITUDE:..., ...}, ...]]
    vessels = []
    if isinstance(payload, list) and len(payload) >= 2 and isinstance(payload[1], list):
        vessels = payload[1]
    elif isinstance(payload, list) and payload and isinstance(payload[0], dict) and "MMSI" in payload[0]:
        vessels = payload
    positions = {}
    for v in vessels:
        vname = (v.get("NAME") or "").upper().strip()
        if not vname:
            continue
        for name in names:
            if name == vname or name in vname or vname in name:
                positions[name] = {
                    "lat":     v.get("LATITUDE"),
                    "lon":     v.get("LONGITUDE"),
                    "sog":     v.get("SOG"),
                    "cog":     v.get("COG"),
                    "heading": v.get("HEADING"),
                    "mmsi":    v.get("MMSI"),
                    "name":    v.get("NAME"),
                }
                break
    log.info(f"AISHub: {len(vessels)} navi in area, {len(positions)} match")
    return jsonify(positions)

@app.route('/logout')
def logout():
    from flask_login import logout_user
    logout_user()
    return redirect(url_for('login'))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
