from __future__ import annotations

import json
import time
import os
import logging
import threading
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request, redirect, url_for, flash
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash

# Proxy aziendale CMA CGM fa SSL inspection -> disabilita verifica certificati
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
ROME_TZ        = ZoneInfo("Europe/Rome")
SCRIPT_DIR     = Path(__file__).parent
STATE_FILE     = SCRIPT_DIR / "last_state.json"
HTTP_TIMEOUT   = 15
HTTP_HEADERS   = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

SECRET_KEY = os.environ.get("SECRET_KEY", "cma-cgm-italy-secret-2026")
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", generate_password_hash("cma2026"))

PORT_GROUPS = [
    {"label": "Liguria",          "keys": ["GENOVA_PSA", "SPINELLI", "GENOVA_SECH", "SAN_GIORGIO", "LA_SPEZIA"]},
    {"label": "Toscana",          "keys": ["LIVORNO"]},
    {"label": "Adriatico",        "keys": ["VENEZIA", "TRIESTE"]},
    {"label": "Campania",         "keys": ["NAPOLI", "SALERNO"]},
]

PORTS = [
    {"key": "GENOVA_PSA",  "name": "Genova PSA",     "code": "ITGOA"},
    {"key": "SPINELLI",    "name": "Genova Spinelli", "code": "ITGOA"},
    {"key": "GENOVA_SECH", "name": "Genova SECH",       "code": "ITGOA"},
    {"key": "SAN_GIORGIO", "name": "Genova San Giorgio", "code": "ITGOA"},
    {"key": "LA_SPEZIA",   "name": "La Spezia",          "code": "ITSPE"},
    {"key": "LIVORNO",     "name": "Livorno",         "code": "ITLGH"},
    {"key": "VENEZIA",     "name": "Venezia",         "code": "ITVCE"},
    {"key": "TRIESTE",     "name": "Trieste",         "code": "ITTRS"},
    {"key": "NAPOLI",      "name": "Napoli",          "code": "ITNAP"},
    {"key": "SALERNO",     "name": "Salerno",         "code": "ITSAL"},
]

app = Flask(__name__)
app.secret_key = SECRET_KEY

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Selenium Helper (per porti JS)
# ---------------------------------------------------------------------------
def _fetch_html_browser(url: str, *, wait_selector: str | None = None) -> str | None:
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.by import By
    except ImportError:
        return None
    try:
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--ignore-certificate-errors")
        opts.add_argument("--ignore-ssl-errors")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--log-level=3")
        driver = webdriver.Chrome(options=opts)
        try:
            driver.get(url)
            if wait_selector:
                WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector)))
            return driver.page_source
        finally:
            driver.quit()
    except Exception as e:
        log.error(f"Selenium error {url}: {e}")
        return None

# ---------------------------------------------------------------------------
# Auth, State & Date Helpers
# ---------------------------------------------------------------------------
class User(UserMixin):
    def __init__(self, id): self.id = id

@login_manager.user_loader
def load_user(user_id): return User(user_id) if user_id == ADMIN_USER else None

def load_last_state() -> dict:
    try:
        if STATE_FILE.exists(): return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except: pass
    return {}

def save_state(state: dict) -> None:
    try: STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except: pass

def detect_changes(new_rows: list, old_rows: list) -> list:
    DATE_FIELDS = {"eta", "accettazione", "fine_accettazione", "chiusura", "etd"}
    if not old_rows: return [{**r, "is_new": False, "changes": {}} for r in new_rows]
    old_index = {f"{r.get('nave','')}|{r.get('viaggio','')}|{r.get('porto','')}": r for r in old_rows}
    result = []
    for row in new_rows:
        key = f"{row.get('nave','')}|{row.get('viaggio','')}|{row.get('porto','')}"
        old = old_index.get(key)
        if old is None: result.append({**row, "is_new": True, "changes": {}})
        else:
            changes = {f: old[f] for f in DATE_FIELDS if f in row and f in old and str(row[f]) != str(old[f]) and old[f] is not None}
            result.append({**row, "is_new": False, "changes": changes})
    return result

_IT_MONTHS = {"gen": "Jan", "feb": "Feb", "mar": "Mar", "apr": "Apr", "mag": "May", "giu": "Jun", "lug": "Jul", "ago": "Aug", "set": "Sep", "ott": "Oct", "nov": "Nov", "dic": "Dec"}

def _parse_port_date(s: str) -> datetime | None:
    if not s: return None
    s = s.strip()
    if "T" in s and ("+" in s or s.endswith("Z")):
        try:
            clean = s.split(".")[0] if "." in s else s.rstrip("Z")
            if s.endswith("Z"): clean += "+00:00"
            return datetime.fromisoformat(clean).astimezone(ROME_TZ).replace(tzinfo=ROME_TZ)
        except: pass
    s_en = s.lower()
    for it, en in _IT_MONTHS.items():
        if it in s_en: s_en = s_en.replace(it, en); break
    s_conv = s_en.title() if s_en != s.lower() else s
    s = s.replace(" - ", " ").replace(" - ", " ")
    s_conv = s_conv.replace(" - ", " ")
    for candidate in (s_conv, s):
        for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y", "%d/%m/%Y %H:%M", "%d/%m/%Y", "%d/%m/%y %H:%M", "%d/%m/%y", "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try: return datetime.strptime(candidate, fmt).replace(tzinfo=ROME_TZ)
            except ValueError: continue
    return None

def _fmt_date(dt: datetime | None) -> str:
    if dt is None: return ""
    if dt.second >= 30 or dt.microsecond >= 500000: dt = dt + timedelta(minutes=1)
    return dt.replace(second=0, microsecond=0).strftime("%d/%m/%Y %H:%M")

def _norm_date_str(s: str | None) -> str:
    if not s: return ""
    dt = _parse_port_date(s)
    return _fmt_date(dt) if dt else s

# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------
def _fetch_html(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT, verify=False)
        resp.raise_for_status()
        return resp.text
    except Exception as e: log.error(f"HTTP error {url}: {e}"); return None

def scrape_genova_psa() -> dict:
    ts  = int(time.time() * 1000)
    url = f"https://online.psagp.it/report_get_data/146?queryArgs=0&clientCodeArgs=0&dhxr{ts}=1"
    html = _fetch_html(url)
    if not html: return {"error": True, "message": "Errore connessione", "data": []}
    try:
        raw = html.strip()
        if "body" in raw.lower():
            soup = BeautifulSoup(html, "html.parser")
            body = soup.find("body")
            raw = body.get_text(separator="").strip() if body else raw
        if not raw or "^$#" not in raw: return {"error": False, "data": []}
        cols_map = ["nave", "viaggio", "eta", "accettazione", "fine_accettazione", "chiusura", "reefer", "imo"]
        data = []
        for row_str in [r for r in raw.split("^$#") if r.strip()]:
            parts = row_str.split("#$^")
            if len(parts) < 2: continue
            row = {cols_map[i]: (parts[i].strip() or None) if i < len(parts) else None for i in range(len(cols_map))}
            if not row.get("nave"): continue
            row.update({"porto": "Genova PSA", "port_code": "ITGOA"})
            data.append(row)
        return {"error": False, "data": data}
    except Exception as e: return {"error": True, "message": str(e), "data": []}

def scrape_spinelli() -> dict:
    url = "https://www.genoaterminal.com/gptPublicService/getvesselsfull"
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT, verify=False)
        body = resp.json()
        vessels = body.get("IN_ACCETTAZIONE", [])
        data = [{"nave": v.get("name"), "viaggio": v.get("exportVoyCode"), "eta": v.get("eta"), "etd": v.get("etd"), "chiusura": v.get("customsDeadline"), "porto": "Genova Spinelli", "port_code": "ITGOA"} for v in vessels] if vessels else []
        return {"error": False, "data": data}
    except Exception as e: return {"error": True, "message": str(e), "data": []}

def scrape_livorno() -> dict:
    url  = "https://www.tdt.it/"
    html = _fetch_html(url)
    if not html: return {"error": True, "message": "Errore connessione", "data": []}
    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one("table.navi-accettazione")
        if not table: return {"error": False, "data": []}
        rows = table.find_all("tr")
        if len(rows) < 2: return {"error": False, "data": []}
        h = [td.get_text(strip=True) for td in rows[0].find_all(["th", "td"])]
        c = {h[i]: i for i in range(len(h))}
        data = []
        for tr in rows[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 2: continue
            data.append({"nave": cells[c["Nome nave"]] if "Nome nave" in c else None, "viaggio": cells[c["Viaggio uscita"]] if "Viaggio uscita" in c else None, "eta": cells[c["ETB"]] if "ETB" in c else None, "fine_accettazione": cells[c["Chiusura accettazione"]] if "Chiusura accettazione" in c else None, "chiusura": cells[c["Chiusura doganale"]] if "Chiusura doganale" in c else None, "porto": "Livorno", "port_code": "ITLGH"})
        return {"error": False, "data": data}
    except Exception as e: return {"error": True, "message": str(e), "data": []}

def scrape_napoli() -> dict:
    url = "https://tfg.bucci.it/@/TFGW_TP_ETA"
    html = _fetch_html(url)
    if not html: return {"error": True, "message": "Errore connessione", "data": []}
    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one("table#dati")
        if not table: return {"error": False, "data": []}
        rows = table.find_all("tr")
        if len(rows) < 2: return {"error": False, "data": []}
        h = [td.get_text(strip=True) for td in rows[0].find_all(["th", "td"])]
        c = {h[i]: i for i in range(len(h))}
        data = []
        for tr in rows[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) < 2: continue
            data.append({"nave": cells[c["E.T.S."]] if "E.T.S." in c else None, "viaggio": cells[c["TERMINAL DI CONSEGNA"]] if "TERMINAL DI CONSEGNA" in c else None, "eta": f"{cells[c['E.T.A.']]} {cells[c['E.T.B.']]}".strip() if "E.T.A." in c else None, "accettazione": cells[c["INT.RIF."]] if "INT.RIF." in c else None, "porto": "Napoli", "port_code": "ITNAP"})
        return {"error": False, "data": data}
    except Exception as e: return {"error": True, "message": str(e), "data": []}

def scrape_venezia() -> dict:
    url = "https://www.vecon.it/tools/info-nave-partenze-arrivi/"
    html = _fetch_html(url)
    if not html: return {"error": True, "message": "Errore connessione", "data": []}
    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        rows = table.find_all("tr") if table else []
        if len(rows) < 2: return {"error": False, "data": []}
        h = [td.get_text(strip=True) for td in rows[0].find_all(["th", "td"])]
        c = {h[i]: i for i in range(len(h))}
        data = [{"nave": cells[c["Nome Nave"]], "viaggio": cells[c["VOY. / RIF. VECON"]], "eta": cells[c["Eta"]], "fine_accettazione": cells[c["Chiusura Gate"]], "porto": "Venezia", "port_code": "ITVCE"} for tr in rows[1:] for cells in [[td.get_text(strip=True) for td in tr.find_all("td")]] if len(cells) > 2]
        return {"error": False, "data": data}
    except Exception as e: return {"error": True, "message": str(e), "data": []}

def scrape_salerno() -> dict:
    url = "https://www.salernocontainerterminal.com/ca/an/vessel_schedule.php"
    html = _fetch_html_browser(url, wait_selector="table#tbanavi")
    if not html: return {"error": True, "message": "Errore connessione Salerno (Selenium)", "data": []}
    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.select_one("table#tbanavi")
        if not table: return {"error": False, "data": []}
        rows = table.find_all("tr")
        if len(rows) < 2: return {"error": False, "data": []}
        headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        col = {h: i for i, h in enumerate(headers)}
        status_key = next((h for h in headers if "STATUS" in h.upper()), None)
        data = []
        for tr in rows[1:]:
            tds = tr.find_all("td")
            if len(tds) == 1: continue
            cells = [td.get_text(strip=True) for td in tds]
            if not cells: continue
            def gc(name, c=cells): return (c[col[name]] or None) if name in col and col[name] < len(c) else None
            nave = gc("VESSEL")
            if not nave: continue
            reef = gc("ACCEPTANCE REEF") or ""
            if reef.strip(): continue
            data.append({
                "nave": nave,
                "viaggio": gc("VOYAGE"),
                "eta": gc("E.T.A."),
                "fine_accettazione": gc("CLOSING TIME"),
                "status": gc(status_key) if status_key else None,
                "accettazione": None,
                "chiusura": None,
                "porto": "Salerno",
                "port_code": "ITSAL",
            })
        return {"error": False, "data": data}
    except Exception as e: return {"error": True, "message": str(e), "data": []}

def scrape_sech() -> dict:
    url = "https://www.sech.it/"
    html = _fetch_html_browser(url, wait_selector="table.vessels")
    if not html: return {"error": True, "message": "Errore connessione Genova SECH (Selenium)", "data": []}
    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_="vessels")
        if not table: return {"error": False, "data": []}
        rows = table.find_all("tr")
        if len(rows) < 2: return {"error": False, "data": []}
        headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        col_map = {header: idx for idx, header in enumerate(headers)}
        data = []
        for tr in rows[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if not cells or len(cells) < 2: continue
            def get_cell(col_name): return cells[col_map[col_name]] if col_name in col_map and col_map[col_name] < len(cells) else None
            nave = get_cell("Nave")
            if not nave: continue
            data.append({
                "nave": nave,
                "eta": get_cell("ETA"),
                "viaggio": get_cell("Voy In Agenzia"),
                "service": get_cell("Servizio") or None,
                "chiusura": get_cell("Chiusura Doganale"),
                "accettazione": None,
                "fine_accettazione": None,
                "porto": "Genova SECH",
                "port_code": "ITGOA",
            })
        return {"error": False, "data": data}
    except Exception as e: return {"error": True, "message": str(e), "data": []}

def scrape_san_giorgio() -> dict:
    url = "https://www.terminalsangiorgio.it/"
    html = _fetch_html_browser(url, wait_selector="table.tab-elenco")
    if not html: return {"error": True, "message": "Errore connessione Terminal San Giorgio (Selenium)", "data": []}
    try:
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table", class_="tab-elenco")
        data_tables = tables[1:]
        if not data_tables: return {"error": False, "data": []}
        data = []
        for table in data_tables:
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(cells) < 2: continue
                nave = cells[0] if len(cells) > 0 else None
                if not nave: continue
                customs = cells[4] if len(cells) > 4 else None
                if customs == "-": customs = None
                data.append({
                    "nave": nave,
                    "eta": cells[1] if len(cells) > 1 else None,
                    "viaggio": cells[2] if len(cells) > 2 else None,
                    "fine_accettazione": customs,
                    "accettazione": None,
                    "chiusura": None,
                    "status": None,
                    "porto": "Genova San Giorgio",
                    "port_code": "ITGOA",
                })
        return {"error": False, "data": data}
    except Exception as e: return {"error": True, "message": str(e), "data": []}

def scrape_js_port(url, selector, port_name, port_code, mapper_fn):
    html = _fetch_html_browser(url, wait_selector=selector)
    if not html: return {"error": True, "message": "Selenium non configurato o errore rendering", "data": []}
    try:
        soup = BeautifulSoup(html, "html.parser")
        return {"error": False, "data": mapper_fn(soup, port_name, port_code)}
    except Exception as e: return {"error": True, "message": str(e), "data": []}

def map_lsz(soup, name, code):
    rows = soup.select("table#open-vessel-voyages tbody tr")
    return [{"nave": c[1], "viaggio": c[2], "accettazione": c[4], "porto": name, "port_code": code} for tr in rows for c in [[td.get_text(strip=True) for td in tr.find_all("td")]] if len(c) > 4]

def map_trieste(soup, name, code):
    table = soup.select_one("table.table-hover")
    rows = table.find_all("tr")[1:] if table else []
    h = [td.get_text(strip=True) for td in table.find_all("tr")[0].find_all(["th", "td"])] if table else []
    c = {h[i]: i for i in range(len(h))}
    return [{"nave": cells[c["Vessel"]], "viaggio": cells[c["Viaggio"]], "eta": cells[c["ETB"]], "accettazione": cells[c["Begin Rcv"]], "porto": name, "port_code": code} for tr in rows for cells in [[td.get_text(strip=True) for td in tr.find_all("td")]] if len(cells) > 4]

SCRAPERS = {
    "GENOVA_PSA": scrape_genova_psa,
    "SPINELLI":   scrape_spinelli,
    "LIVORNO":    scrape_livorno,
    "NAPOLI":     scrape_napoli,
    "VENEZIA":    scrape_venezia,
    "LA_SPEZIA":  lambda: scrape_js_port("https://services.contshipitalia.com/it/reports/vessel-acceptance-report.html?terminal=LSCT", "table#open-vessel-voyages", "La Spezia", "ITSPE", map_lsz),
    "TRIESTE":    lambda: scrape_js_port("https://www.trieste-marine-terminal.com/it/content/navi-banchina-arrivi-e-partenze", "table.table-hover", "Trieste", "ITTRS", map_trieste),
    "SALERNO":    scrape_salerno,
    "GENOVA_SECH": scrape_sech,
    "SAN_GIORGIO": scrape_san_giorgio,
}

# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('username') == ADMIN_USER and check_password_hash(ADMIN_PASSWORD_HASH, request.form.get('password')):
            login_user(User(ADMIN_USER)); return redirect(url_for('index'))
        flash('Credenziali non valide')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout(): logout_user(); return redirect(url_for('login'))

@app.route('/refresh')
@login_required
def refresh():
    last_state, new_state, results = load_last_state(), {}, {}
    with ThreadPoolExecutor(max_workers=4) as exec:
        fut = {exec.submit(fn): key for key, fn in SCRAPERS.items()}
        for f in as_completed(fut):
            k = fut[f]
            try: res = f.result()
            except Exception as e: res = {"error": True, "message": str(e), "data": []}
            if res["error"]: results[k] = {"error": True, "message": res.get("message"), "stale_data": last_state.get(k, [])}
            else:
                formatted_data = []
                for row in res["data"]:
                    row_copy = dict(row)
                    for fld in ("eta", "accettazione", "fine_accettazione", "chiusura", "etd"):
                        if row_copy.get(fld): row_copy[fld] = _norm_date_str(row_copy[fld])
                    formatted_data.append(row_copy)
                results[k] = {"error": False, "data": detect_changes(formatted_data, last_state.get(k, []))}
                new_state[k] = formatted_data
    save_state(new_state)
    return jsonify({"timestamp": datetime.now(tz=ROME_TZ).isoformat(timespec="seconds"), "ports": results})

@app.route('/')
@login_required
def index():
    ports_json = json.dumps([{"key": p["key"], "name": p["name"], "code": p["code"]} for p in PORTS])
    groups = [{**g, "ports": [{p["key"]: p for p in PORTS}[k] for k in g["keys"] if k in {p["key"]: p for p in PORTS}]} for g in PORT_GROUPS]
    return render_template("index.html", port_groups=groups, ports_json=ports_json)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
