"""
Navi Aperte Dashboard — CMA CGM Italy
======================================
Server Flask locale per monitorare le navi in accettazione nei porti italiani.

Avvio:  python dashboard.py
Apre automaticamente http://localhost:5000 nel browser.

Dipendenze:  pip install flask requests beautifulsoup4 lxml
"""
from __future__ import annotations

import json
import time
import threading
import webbrowser
import logging

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


try:
    import openpyxl
    _HAS_OPENPYXL = True
except ImportError:
    _HAS_OPENPYXL = False

import requests
import urllib3
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string

import os
import shutil
import glob
from pathlib import Path

DOWNLOADS   = Path.home() / "Downloads"
SCRIPT_DIR  = Path(__file__).parent
NOME_FILE   = "schedule test 1.xlsx"

SHAREPOINT_URL = (
    "https://cmacgmgroup.sharepoint.com/sites/CMA-ITALYTEAMSITE"
    "/Documents%20partages/schedules/schedule%20test%201.xlsx"
)


def download_schedule_sharepoint() -> tuple:
    """
    Scarica il file schedule da SharePoint Online usando Excel COM (win32com).
    Ritorna tupla: (success: bool, message: str)
    """
    dst = SCRIPT_DIR / NOME_FILE
    try:
        import pythoncom
        import win32com.client
    except ImportError:
        return False, "❌ pythoncom non disponibile — installa pywin32"

    pythoncom.CoInitialize()
    xl            = None
    started_excel = False
    wb            = None
    try:
        # DispatchEx forza sempre un nuovo processo Excel separato
        xl = win32com.client.DispatchEx("Excel.Application")
        started_excel = True

        xl.Visible       = False
        xl.DisplayAlerts = False

        # Chiudi eventuale versione già aperta dello stesso file
        for existing in list(xl.Workbooks):
            try:
                if os.path.normcase(existing.FullName) in (
                    os.path.normcase(str(dst)),
                    os.path.normcase(SHAREPOINT_URL),
                ):
                    existing.Close(SaveChanges=False)
                    break
            except Exception:
                pass

        # Apri direttamente l'URL SharePoint — Excel usa il token SSO
        wb = xl.Workbooks.Open(SHAREPOINT_URL)

        # Salva copia locale come xlsx puro (format 51 = xlOpenXMLWorkbook)
        wb.SaveAs(str(dst), FileFormat=51, ConflictResolution=2)
        return True, f"✅ Schedule scaricato: {dst}"

    except Exception as e:
        return False, f"❌ Download fallito: {str(e)}"

    finally:
        if wb is not None:
            try:
                wb.Close(SaveChanges=False)
            except Exception:
                pass
        if started_excel and xl is not None:
            try:
                if xl.Workbooks.Count == 0:
                    xl.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()


def sposta_schedule():
    # Cerca il file in Downloads (case insensitive)
    matches = list(DOWNLOADS.glob("*[Ss]chedule*test*1*.xlsx"))
    
    if not matches:
        print(f"ATTENZIONE: nessun file Schedule trovato in {DOWNLOADS}")
        print("Scaricalo da SharePoint e riavvia.")
        return False
    
    # Prende il più recente se ce ne sono più di uno
    src = max(matches, key=lambda f: f.stat().st_mtime)
    dst = SCRIPT_DIR / NOME_FILE
    shutil.copy2(src, dst)
    print(f"File copiato: {src.name} → {dst}")
    return True



# Proxy aziendale CMA CGM fa SSL inspection → disabilita verifica certificati
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Configurazione
# ---------------------------------------------------------------------------
ROME_TZ        = ZoneInfo("Europe/Rome")
STATE_FILE     = Path(__file__).parent / "last_state.json"
SCHEDULE_FILE  = Path(__file__).parent / "schedule test 1.xlsx"
SCHEDULE_DAYS  = 14   # quanti giorni in avanti mostrare
HTTP_TIMEOUT = 15
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

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
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_last_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        log.warning(f"last_state.json malformato, ignorato: {e}")
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def detect_changes(new_rows: list, old_rows: list) -> list:
    """
    Confronta new_rows con old_rows.
    Aggiunge 'is_new' e 'changes' (dict {campo: valore_precedente}) a ogni riga.
    Prima esecuzione (old_rows vuoto) → is_new=False, changes={} per tutti.
    """
    DATE_FIELDS = {"eta", "accettazione", "fine_accettazione", "chiusura", "etd"}

    if not old_rows:
        return [{**r, "is_new": False, "changes": {}} for r in new_rows]

    old_index = {
        f"{r.get('nave','')}|{r.get('viaggio','')}|{r.get('porto','')}": r
        for r in old_rows
    }
    result = []
    for row in new_rows:
        key = f"{row.get('nave','')}|{row.get('viaggio','')}|{row.get('porto','')}"
        old = old_index.get(key)
        if old is None:
            result.append({**row, "is_new": True, "changes": {}})
        else:
            changes = {
                field: old[field]
                for field in DATE_FIELDS
                if field in row and field in old
                and str(row[field]) != str(old[field])
                and old[field] is not None
            }
            result.append({**row, "is_new": False, "changes": changes})
    return result


# ---------------------------------------------------------------------------
# Schedule helpers
# ---------------------------------------------------------------------------

_IT_MONTHS = {
    "gen": "Jan", "feb": "Feb", "mar": "Mar", "apr": "Apr",
    "mag": "May", "giu": "Jun", "lug": "Jul", "ago": "Aug",
    "set": "Sep", "ott": "Oct", "nov": "Nov", "dic": "Dec",
}

def _parse_port_date(s: str) -> datetime | None:
    """Prova a parsare una stringa data proveniente dagli scraper (vari formati)."""
    if not s:
        return None
    s = s.strip()

    # Spinelli: ISO 8601 con timezone e millisecondi  es. "2026-03-25T10:00:00.000+00:00"
    if "T" in s and ("+" in s or s.endswith("Z")):
        try:
            clean = s.split(".")[0] if "." in s else s.rstrip("Z")
            if s.endswith("Z"):
                clean += "+00:00"
            elif "." in s:
                clean += s[s.index(".")+4:]
            dt = datetime.fromisoformat(clean)
            return dt.astimezone(ROME_TZ).replace(tzinfo=ROME_TZ)
        except (ValueError, IndexError):
            pass

    # La Spezia: mesi abbreviati in italiano  es. "25-mar-2026 07:00", "01-gen-2024 00:00"
    s_en = s.lower()
    for it, en in _IT_MONTHS.items():
        if it in s_en:
            s_en = s_en.replace(it, en)
            break
    # capitalizza la prima lettera del mese per strptime %b
    s_conv = s_en.title() if s_en != s.lower() else s

    # Normalizza separatore " - " usato da Livorno (es. "2026-03-28 - 13:30:00")
    s      = s.replace(" - ", " ")
    s_conv = s_conv.replace(" - ", " ")

    for candidate in (s_conv, s):
        for fmt in (
            "%d-%b-%Y %H:%M", "%d-%b-%Y",                       # La Spezia italiano→inglese
            "%d/%m/%Y %H:%M", "%d/%m/%Y",
            "%d/%m/%y %H:%M", "%d/%m/%y",                       # Napoli anno 2 cifre
            "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y", # Trieste
            "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", # Livorno / ISO senza tz
            "%Y-%m-%dT%H:%M:%S",
        ):
            try:
                return datetime.strptime(candidate, fmt).replace(tzinfo=ROME_TZ)
            except ValueError:
                continue
    return None


def _fmt_date(dt: datetime | None) -> str:
    """Formatta una datetime in DD/MM/YYYY HH:MM (stringa uniforme per la UI)."""
    if dt is None:
        return ""
    # Arrotonda al minuto più vicino (Excel salva spesso "10:00" come "09:59:59.999")
    if dt.second >= 30 or dt.microsecond >= 500000:
        dt = dt + timedelta(minutes=1)
    dt = dt.replace(second=0, microsecond=0)
    return dt.strftime("%d/%m/%Y %H:%M")


def _norm_date_str(s: str | None) -> str:
    """Parsa e ri-formatta una stringa data in formato uniforme DD/MM/YYYY HH:MM."""
    if not s:
        return ""
    dt = _parse_port_date(s)
    return _fmt_date(dt) if dt else s   # fallback alla stringa originale se non parsabile


def load_schedule(days_ahead: int = SCHEDULE_DAYS) -> list:
    """Legge il file Excel e restituisce le navi con ETA nei prossimi `days_ahead` giorni."""
    if not _HAS_OPENPYXL:
        log.warning("openpyxl non installato — schedule non disponibile (pip install openpyxl)")
        return []
    if not SCHEDULE_FILE.exists():
        log.warning(f"File schedule non trovato: {SCHEDULE_FILE}")
        return []
    try:
        wb = openpyxl.load_workbook(str(SCHEDULE_FILE), read_only=True, data_only=True)
        ws = wb.active
    except Exception as e:
        log.error(f"load_schedule: {e}")
        return []

    now    = datetime.now(tz=ROME_TZ)
    cutoff = now + timedelta(days=days_ahead)
    col_map: dict = {}
    rows: list = []
    header_found = False

    for raw_row in ws.iter_rows(values_only=True):
        if not header_found:
            if raw_row[0] == "VES - Vessel Name":
                header_found = True
                col_map = {v: i for i, v in enumerate(raw_row) if v}
            continue

        nave_val = raw_row[col_map.get("VES - Vessel Name", 0)]
        if not nave_val:
            continue

        eta_raw = raw_row[col_map.get("VVS - ETA Date / Time", 5)]
        if not isinstance(eta_raw, datetime):
            continue

        EXCEL_OFFSET = timedelta(hours=+2)   # il file Excel è sfasato di +2h rispetto all'ora locale

        eta = eta_raw.replace(tzinfo=ROME_TZ) if eta_raw.tzinfo is None else eta_raw
        eta += EXCEL_OFFSET
        if not (now <= eta <= cutoff):
            continue

        etd_raw = raw_row[col_map.get("VVS - ETD Date / Time", 7)]
        etd = None
        if isinstance(etd_raw, datetime):
            etd = etd_raw.replace(tzinfo=ROME_TZ) if etd_raw.tzinfo is None else etd_raw
            etd += EXCEL_OFFSET

        cargo_cutoff_raw = raw_row[col_map.get("VVS - Cutoff Date / Time", 20)]
        cargo_cutoff = None
        if isinstance(cargo_cutoff_raw, datetime):
            cargo_cutoff = cargo_cutoff_raw.replace(tzinfo=ROME_TZ) if cargo_cutoff_raw.tzinfo is None else cargo_cutoff_raw
            cargo_cutoff += EXCEL_OFFSET

        rows.append({
            "nave":        str(nave_val).strip(),
            "viaggio":     str(raw_row[col_map.get("VGI - Main Voyage Reference", 4)] or "").strip(),
            "porto_code":  str(raw_row[col_map.get("VVS - Port Code", 13)] or "").strip(),
            "porto_name":  str(raw_row[col_map.get("VVS - Port Name", 14)] or "").strip(),
            "service":     str(raw_row[col_map.get("VSI - Main Service", 18)] or "").strip(),
            "eta":         eta.strftime("%d/%m/%Y %H:%M"),
            "etd":         etd.strftime("%d/%m/%Y %H:%M") if etd else "",
            "cutoff":      cargo_cutoff.strftime("%d/%m/%Y %H:%M") if cargo_cutoff else "",
            "eta_iso":     eta.isoformat(),
            "etd_iso":     etd.isoformat() if etd else "",
            "cutoff_iso":  cargo_cutoff.isoformat() if cargo_cutoff else "",
        })

    wb.close()
    rows.sort(key=lambda r: r["eta_iso"])
    return rows


def build_schedule_lookup(schedule: list) -> dict:
    """
    Costruisce due indici per il matching:
      - (nave_norm, viaggio_norm) → riga schedule  (match esatto)
      - nave_norm                 → lista righe     (fallback per nome)
    """
    by_voyage: dict[tuple, dict]  = {}
    by_name:   dict[str,  list]   = {}
    for row in schedule:
        nave = row["nave"].upper().strip()
        voy  = (row.get("viaggio") or "").upper().strip()
        if voy:
            by_voyage[(nave, voy)] = row
        by_name.setdefault(nave, []).append(row)
    return {"by_voyage": by_voyage, "by_name": by_name}


def annotate_with_schedule(port_rows: list, sched_lookup: dict) -> list:
    """
    Aggiunge a ogni riga scraped i campi di confronto con lo schedule Excel:
      sched_eta, sched_cutoff, delta_eta_h, delta_cutoff_h
    Confronto: ETA porto ↔ VVS-ETA  /  accettazione porto ↔ VVS-Cutoff
    Normalizza anche le stringhe data del porto a DD/MM/YYYY HH:MM.
    """
    by_voyage = sched_lookup["by_voyage"]
    by_name   = sched_lookup["by_name"]

    result = []
    for raw in port_rows:
        row = dict(raw)

        # Normalizza date porto a formato uniforme DD/MM/YYYY HH:MM
        for field in ("eta", "accettazione", "fine_accettazione", "chiusura", "etd"):
            if row.get(field):
                row[field] = _norm_date_str(row[field])

        nave_norm = (row.get("nave") or "").upper().strip()
        voy_norm  = (row.get("viaggio") or "").upper().strip()

        # 1) match esatto nome+viaggio
        best = by_voyage.get((nave_norm, voy_norm))

        # 2) fallback: stessa nave, ETA più vicina
        if best is None:
            candidates = by_name.get(nave_norm, [])
            if candidates:
                port_eta_dt = _parse_port_date(row.get("eta") or "")
                if port_eta_dt and len(candidates) > 1:
                    best = min(candidates, key=lambda s: abs(
                        (datetime.fromisoformat(s["eta_iso"]) - port_eta_dt).total_seconds()
                    ))
                else:
                    best = candidates[0]

        if best is None:
            row.update(sched_eta=None, sched_cutoff=None,
                       delta_eta_h=None, delta_cutoff_h=None, sched_service=None)
            result.append(row)
            continue

        sched_eta_dt    = datetime.fromisoformat(best["eta_iso"])
        sched_cutoff_dt = datetime.fromisoformat(best["cutoff_iso"]) if best.get("cutoff_iso") else None

        port_eta_dt = _parse_port_date(row.get("eta") or "")
        delta_eta_h = None
        if port_eta_dt:
            delta_eta_h = round(abs((sched_eta_dt - port_eta_dt).total_seconds()) / 3600, 1)

        # Confronta cutoff schedule vs chiusura porto
        # Priorità: chiusura → fine_accettazione → accettazione (solo se ≤7gg dall'ETA schedule)
        port_closure_dt = (
            _parse_port_date(row.get("chiusura") or "")
            or _parse_port_date(row.get("fine_accettazione") or "")
        )
        if port_closure_dt is None:
            acc_dt = _parse_port_date(row.get("accettazione") or "")
            if acc_dt is not None:
                # Usa accettazione solo se è temporalmente vicina all'ETA schedule (≤7 giorni)
                if abs((sched_eta_dt - acc_dt).total_seconds()) <= 7 * 86400:
                    port_closure_dt = acc_dt

        delta_cutoff_h = None
        if sched_cutoff_dt and port_closure_dt:
            delta_cutoff_h = round(abs((sched_cutoff_dt - port_closure_dt).total_seconds()) / 3600, 1)

        row.update(
            sched_eta      = best["eta"],
            sched_cutoff   = best.get("cutoff", ""),
            sched_service  = best.get("service") or None,
            delta_eta_h    = delta_eta_h,
            delta_cutoff_h = delta_cutoff_h,
        )
        result.append(row)
    return result


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _fetch_html(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT, verify=False)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        log.error(f"HTTP error {url}: {e}")
        return None


# Headers completi che simulano un browser reale (per siti con basic browser-check)
_BROWSER_HEADERS = {
    **HTTP_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def _fetch_with_session(url: str, *, accept_json: bool = False) -> requests.Response | None:
    """GET con Session (cookie automatici) e headers browser-completi."""
    try:
        session = requests.Session()
        headers = dict(_BROWSER_HEADERS)
        if accept_json:
            headers["Accept"] = "application/json, text/plain, */*"
        resp = session.get(url, headers=headers, timeout=HTTP_TIMEOUT, verify=False)
        resp.raise_for_status()
        return resp
    except Exception as e:
        log.error(f"HTTP error {url}: {e}")
        return None


def _fetch_html_browser(url: str, *, wait_selector: str | None = None) -> str | None:
    """Fetch HTML con Selenium + Chrome headless per pagine JS-rendered.
    Equivalente di Web.BrowserContents() in Power Query.
    Usa il Chrome già installato sul PC — nessun download aggiuntivo.
    """
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.common.by import By
    except ImportError:
        log.error("Selenium non installato. Esegui: pip install selenium")
        return None
    try:
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--ignore-certificate-errors")   # bypass SSL proxy aziendale
        opts.add_argument("--ignore-ssl-errors")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--log-level=3")                 # silenzia log Chrome

        driver = webdriver.Chrome(options=opts)
        try:
            driver.get(url)
            if wait_selector:
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, wait_selector))
                )
            return driver.page_source
        finally:
            driver.quit()
    except Exception as e:
        log.error(f"Selenium error {url}: {e}")
        return None


def _empty_table_error(msg: str = "Sito richiede rendering JS (dati stale)") -> dict:
    return {"error": True, "message": msg, "data": []}


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

def scrape_genova_psa() -> dict:
    """PSA Genova — psagp.it, parsing custom ^$# / #$^"""
    ts  = int(time.time() * 1000)
    url = (
        f"https://online.psagp.it/report_get_data/146"
        f"?queryArgs=0&clientCodeArgs=0&dhxr{ts}=1"
    )
    html = _fetch_html(url)
    if not html:
        return _empty_table_error("Errore connessione Genova PSA")
    try:
        soup = BeautifulSoup(html, "lxml")
        body = soup.find("body")
        raw  = body.get_text(separator="").strip() if body else ""
        if not raw:
            return _empty_table_error()

        cols_map = ["nave", "viaggio", "eta", "accettazione",
                    "fine_accettazione", "chiusura", "reefer", "imo"]
        data = []
        for row_str in [r for r in raw.split("^$#") if r.strip()]:
            parts = row_str.split("#$^")
            if len(parts) < 2:
                continue
            row = {
                cols_map[i]: (parts[i].strip() or None) if i < len(parts) else None
                for i in range(len(cols_map))
            }
            if not row.get("nave"):
                continue
            row["porto"]     = "Genova PSA"
            row["port_code"] = "ITGOA"
            data.append(row)

        if not data:
            return _empty_table_error()
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_genova_psa: {e}")
        return {"error": True, "message": str(e), "data": []}


def scrape_spinelli() -> dict:
    """Spinelli Genova — JSON REST API"""
    url = "https://www.genoaterminal.com/gptPublicService/getvesselsfull"
    try:
        resp = requests.get(url, headers=HTTP_HEADERS, timeout=HTTP_TIMEOUT, verify=False)
        resp.raise_for_status()
        body = resp.json()
        if "IN_ACCETTAZIONE" not in body:
            return _empty_table_error()
        vessels = body["IN_ACCETTAZIONE"]
        if vessels is None:
            return _empty_table_error()
        data = []
        for v in vessels:
            data.append({
                "nave":              v.get("name"),
                "viaggio":           v.get("exportVoyCode"),
                "eta":               v.get("eta"),
                "etd":               v.get("etd"),
                "chiusura":          v.get("customsDeadline"),
                "reefer":            v.get("imoReeferAcceptance"),
                "accettazione":      None,
                "fine_accettazione": None,
                "accettazione_dal":  "aperta",
                "porto":             "Genova Spinelli",
                "port_code":         "ITGOA",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_spinelli: {e}")
        return {"error": True, "message": str(e), "data": []}


def scrape_lsz() -> dict:
    """La Spezia — contshipitalia.com  (JS-rendered, richiede Playwright).
    Tabella: TABLE#open-vessel-voyages
    Colonne PQ: col1(#), nave, viaggio, col4(#), accettazione, col6, col7, col8, status
    """
    url  = "https://services.contshipitalia.com/it/reports/vessel-acceptance-report.html?terminal=LSCT"
    html = _fetch_html_browser(url, wait_selector="table#open-vessel-voyages")
    if not html:
        return _empty_table_error("Errore connessione La Spezia (Playwright)")
    try:
        soup  = BeautifulSoup(html, "lxml")
        table = soup.select_one("table#open-vessel-voyages")
        if not table:
            return _empty_table_error("La Spezia: tabella non trovata dopo rendering")
        rows = table.select("tbody tr")
        if not rows:
            return _empty_table_error()
        data = []
        for tr in rows:
            cols = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cols) < 3:
                continue
            # PQ: col0=#, col1=nave, col2=viaggio, col3=#, col4=accettazione, col8=status
            data.append({
                "nave":              cols[1] if len(cols) > 1 else None,
                "viaggio":           cols[2] if len(cols) > 2 else None,
                "accettazione":      cols[4] if len(cols) > 4 else None,
                "status":            cols[8] if len(cols) > 8 else None,
                "eta":               None,
                "fine_accettazione": None,
                "chiusura":          None,
                "porto":             "La Spezia",
                "port_code":         "ITSPE",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_lsz: {e}")
        return {"error": True, "message": str(e), "data": []}


def scrape_livorno() -> dict:
    """Livorno — tdt.it"""
    url  = "https://www.tdt.it/"
    html = _fetch_html(url)
    if not html:
        return _empty_table_error("Errore connessione Livorno")
    try:
        soup  = BeautifulSoup(html, "lxml")
        table = soup.select_one("table.navi-accettazione")
        if not table:
            return _empty_table_error()
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
                "accettazione":      None,
                "porto":             "Livorno",
                "port_code":         "ITLGH",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_livorno: {e}")
        return {"error": True, "message": str(e), "data": []}


def scrape_trieste() -> dict:
    """Trieste — trieste-marine-terminal.com  (JS-rendered, richiede Playwright).
    Tabella: TABLE.table.table-hover
    Colonne PQ: ETB, ETD, Vessel, Viaggio, Agent, Begin Rcv, Begin Dlv
    """
    url  = "https://www.trieste-marine-terminal.com/it/content/navi-banchina-arrivi-e-partenze"
    html = _fetch_html_browser(url, wait_selector="table.table-hover")
    if not html:
        return _empty_table_error("Errore connessione Trieste (Playwright)")
    try:
        soup  = BeautifulSoup(html, "lxml")
        table = soup.select_one("table.table-hover") or soup.select_one("table.table")
        if not table:
            return _empty_table_error("Trieste: tabella non trovata dopo rendering")
        rows = table.find_all("tr")
        if len(rows) < 2:
            return _empty_table_error()
        headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        col     = {h: i for i, h in enumerate(headers)}

        data = []
        for tr in rows[1:]:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if not cells:
                continue
            def gc(name, c=cells):
                return (c[col[name]] or None) if name in col and col[name] < len(c) else None
            data.append({
                "nave":              gc("Vessel"),
                "viaggio":           gc("Viaggio"),
                "eta":               gc("ETB"),
                "etd":               gc("ETD"),
                "accettazione":      gc("Begin Rcv"),
                "fine_accettazione": None,
                "chiusura":          None,
                "porto":             "Trieste",
                "port_code":         "ITTRS",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_trieste: {e}")
        return {"error": True, "message": str(e), "data": []}


def scrape_napoli() -> dict:
    """Napoli — tfg.bucci.it"""
    url  = "https://tfg.bucci.it/@/TFGW_TP_ETA"
    html = _fetch_html(url)
    if not html:
        return _empty_table_error("Errore connessione Napoli")
    try:
        soup  = BeautifulSoup(html, "lxml")
        table = soup.select_one("table#dati")
        if not table:
            return _empty_table_error()
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
                "nave":              gc("E.T.S."),
                "viaggio":           gc("TERMINAL DI CONSEGNA"),
                "eta":               eta,
                "accettazione":      gc("INT.RIF."),
                "fine_accettazione": None,
                "chiusura":          None,
                "porto":             "Napoli",
                "port_code":         "ITNAP",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_napoli: {e}")
        return {"error": True, "message": str(e), "data": []}


def scrape_venezia() -> dict:
    """Venezia — vecon.it"""
    url  = "https://www.vecon.it/tools/info-nave-partenze-arrivi/"
    html = _fetch_html(url)
    if not html:
        return _empty_table_error("Errore connessione Venezia")
    try:
        soup  = BeautifulSoup(html, "lxml")
        table = soup.find("table")
        if not table:
            return _empty_table_error()
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
                "chiusura":          None,
                "porto":             "Venezia",
                "port_code":         "ITVCE",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_venezia: {e}")
        return {"error": True, "message": str(e), "data": []}


def scrape_salerno() -> dict:
    """Salerno — salernocontainerterminal.com  (JS-rendered + 403 senza browser, richiede Playwright).
    Tabella: TABLE#tbanavi  (9 colonne dati + righe separatore con colspan)
    Colonne PQ: VESSEL, VOYAGE, E.T.A., STATUS, CLOSING TIME, ACCEPTANCE REEF, ACCEPTANCE DRY, ACTIONS, TRADE
    Righe separatore (colspan=10 o 12) vengono scartate.
    """
    url  = "https://www.salernocontainerterminal.com/ca/an/vessel_schedule.php"
    html = _fetch_html_browser(url, wait_selector="table#tbanavi")
    if not html:
        return _empty_table_error("Errore connessione Salerno (Playwright)")
    try:
        soup  = BeautifulSoup(html, "lxml")
        table = soup.select_one("table#tbanavi")
        if not table:
            return _empty_table_error("Salerno: tabella non trovata dopo rendering")
        rows = table.find_all("tr")
        if len(rows) < 2:
            return _empty_table_error()

        # Prima riga = intestazioni
        headers = [th.get_text(strip=True) for th in rows[0].find_all(["th", "td"])]
        col = {h: i for i, h in enumerate(headers)}

        # Cerca STATUS in modo case-insensitive (può chiamarsi "STATUS", "Stato", ecc.)
        status_key = next((h for h in headers if "STATUS" in h.upper()), None)

        data = []
        for tr in rows[1:]:
            tds = tr.find_all("td")
            # Salta righe separatore (una sola cella con colspan ampio)
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

            # Acceptance REEF: se non vuota la riga è reefer-only → la saltiamo
            # (stesso filtro del Power Query: teniamo solo righe con ACCEPTANCE REEF vuoto)
            reef = gc("ACCEPTANCE REEF") or ""
            if reef.strip():
                continue

            data.append({
                "nave":              nave,
                "viaggio":           gc("VOYAGE"),
                "eta":               gc("E.T.A."),
                "fine_accettazione": gc("CLOSING TIME"),
                "status":            gc(status_key) if status_key else None,
                "accettazione":      None,
                "chiusura":          None,
                "porto":             "Salerno",
                "port_code":         "ITSAL",
            })
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_salerno: {e}")
        return {"error": True, "message": str(e), "data": []}


def scrape_sech() -> dict:
    """Savona SECH (Genova) — sech.it  (JS-rendered, richiede Selenium).
    Tabella Angular Material con colonne: Nave, ETA, Voy In Agenzia, Servizio, Chiusura Doganale
    """
    url = "https://www.sech.it/"
    html = _fetch_html_browser(url, wait_selector="table.vessels")
    if not html:
        return _empty_table_error("Errore connessione Genova SECH (Selenium)")
    try:
        soup = BeautifulSoup(html, "lxml")
        table = soup.find("table", class_="vessels")
        if not table:
            return _empty_table_error("Tabella vessels non trovata")
        
        rows = table.find_all("tr")
        if len(rows) < 2:
            return _empty_table_error("Nessuna riga trovata")
        
        # Estrai headers dalla prima riga
        headers = []
        for th in rows[0].find_all(["th", "td"]):
            headers.append(th.get_text(strip=True))
        
        # Crea mapping colonna -> indice
        col_map = {}
        for idx, header in enumerate(headers):
            col_map[header] = idx
        
        log.info(f"SECH headers: {headers}")
        
        data = []
        for tr in rows[1:]:
            cells = []
            for td in tr.find_all("td"):
                cells.append(td.get_text(strip=True))
            
            if not cells or len(cells) < 2:
                continue
            
            def get_cell(col_name):
                """Estrae il valore da una colonna per nome"""
                if col_name not in col_map:
                    return None
                idx = col_map[col_name]
                return cells[idx] if idx < len(cells) else None
            
            nave = get_cell("Nave")
            if not nave:
                continue
            
            data.append({
                "nave":              nave,
                "eta":               get_cell("ETA"),
                "viaggio":           get_cell("Voy In Agenzia"),
                "service":           get_cell("Servizio") or None,
                "chiusura":          get_cell("Chiusura Doganale"),
                "accettazione":      None,
                "fine_accettazione": None,
                "porto":             "Genova SECH",  # Terminal di Genova, non Savona!
                "port_code":         "ITGOA",
            })
        
        if not data:
            return _empty_table_error("Nessun dato estratto dalla tabella")
        
        log.info(f"SECH: {len(data)} navi estratte")
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_sech: {e}")
        return {"error": True, "message": str(e), "data": []}


def scrape_san_giorgio() -> dict:
    """Terminal San Giorgio (Genova) — terminalsangiorgio.it  (JS-rendered, richiede Selenium).
    Homepage: 3 tabelle class="tab-elenco" — la prima è solo header, le altre contengono le righe.
    Colonne: NAME(0), ETA(1), VOY IN(2), VOY OUT(3), CUSTOMS CLOSED(4), ETD(5)
    """
    url  = "https://www.terminalsangiorgio.it/"
    html = _fetch_html_browser(url, wait_selector="table.tab-elenco")
    if not html:
        return _empty_table_error("Errore connessione Terminal San Giorgio (Selenium)")
    try:
        soup   = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table", class_="tab-elenco")
        # Prima tabella = solo intestazioni → skip; le successive contengono i dati
        data_tables = tables[1:]
        if not data_tables:
            return _empty_table_error("Terminal San Giorgio: tabelle dati non trovate")

        data = []
        for table in data_tables:
            for tr in table.find_all("tr"):
                cells = [td.get_text(strip=True) for td in tr.find_all("td")]
                if len(cells) < 2:
                    continue
                nave = cells[0] if len(cells) > 0 else None
                if not nave:
                    continue
                customs = cells[4] if len(cells) > 4 else None
                if customs == "-":
                    customs = None
                data.append({
                    "nave":              nave,
                    "eta":               cells[1] if len(cells) > 1 else None,
                    "viaggio":           cells[2] if len(cells) > 2 else None,
                    "fine_accettazione": customs,
                    "accettazione":      None,
                    "chiusura":          None,
                    "status":            None,
                    "porto":             "Genova San Giorgio",
                    "port_code":         "ITGOA",
                })

        if not data:
            return _empty_table_error("Terminal San Giorgio: nessun dato estratto")
        log.info(f"San Giorgio: {len(data)} navi estratte")
        return {"error": False, "data": data}
    except Exception as e:
        log.error(f"scrape_san_giorgio: {e}")
        return {"error": True, "message": str(e), "data": []}


SCRAPERS = {
    "GENOVA_PSA": scrape_genova_psa,
    "SPINELLI":   scrape_spinelli,
    "LA_SPEZIA":  scrape_lsz,
    "LIVORNO":    scrape_livorno,
    "TRIESTE":    scrape_trieste,
    "NAPOLI":     scrape_napoli,
    "VENEZIA":    scrape_venezia,
    "SALERNO":    scrape_salerno,
    "GENOVA_SECH": scrape_sech,
    "SAN_GIORGIO": scrape_san_giorgio,
}


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

@app.route("/refresh")
def refresh():
    last_state = load_last_state()
    new_state  = dict(last_state)
    results    = {}

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fn): key for key, fn in SCRAPERS.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = {"error": True, "message": str(e), "data": []}

            if result["error"]:
                stale = last_state.get(key, [])
                results[key] = {
                    "error":      True,
                    "message":    result.get("message", "Errore sconosciuto"),
                    "stale_data": stale,
                }
            else:
                old_rows  = last_state.get(key, [])
                new_rows  = detect_changes(result["data"], old_rows)
                new_state[key] = result["data"]
                results[key]   = {"error": False, "data": new_rows}

    save_state(new_state)

    # Scarica il file schedule aggiornato da SharePoint (credenziali Windows SSO)
    dl_ok, _ = download_schedule_sharepoint()

    schedule     = load_schedule()
    sched_lookup = build_schedule_lookup(schedule)

    # Annota ogni riga scraped con il confronto Excel (solo porti che monitoriamo)
    for key in results:
        if not results[key].get("error"):
            results[key]["data"] = annotate_with_schedule(results[key]["data"], sched_lookup)
        elif results[key].get("stale_data"):
            results[key]["stale_data"] = annotate_with_schedule(results[key]["stale_data"], sched_lookup)

    # Timestamp del file schedule su disco
    schedule_file_ts = None
    if SCHEDULE_FILE.exists():
        mtime = SCHEDULE_FILE.stat().st_mtime
        schedule_file_ts = datetime.fromtimestamp(mtime, tz=ROME_TZ).isoformat(timespec="seconds")

    now = datetime.now(tz=ROME_TZ)
    return jsonify({
        "timestamp":          now.isoformat(timespec="seconds"),
        "ports":              results,
        "schedule":           schedule,
        "schedule_file_ts":   schedule_file_ts,
        "schedule_dl_ok":     dl_ok,
    })


@app.route("/download-schedule")
def download_schedule():
    """Endpoint per il download manuale del file schedule da SharePoint"""
    success, msg = download_schedule_sharepoint()
    return jsonify({"success": success, "message": msg})


# ---------------------------------------------------------------------------
# HTML Template
# ---------------------------------------------------------------------------

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Navi Aperte — CMA CGM</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: Arial, sans-serif; background: #f0f2f5; color: #333; }

  /* ---- Header ---- */
  .header {
    background: #002B5C; color: #fff;
    padding: 14px 24px;
    display: flex; align-items: center; justify-content: space-between;
    position: sticky; top: 0; z-index: 100;
    box-shadow: 0 2px 8px rgba(0,0,0,.35);
  }
  .header-title { font-size: 1.35rem; font-weight: bold; letter-spacing: .5px; }
  .header-sub   { font-size: .75rem; opacity: .7; margin-top: 3px; }
  .header-right { display: flex; align-items: center; gap: 12px; }

  /* ---- Refresh button ---- */
  .btn-refresh {
    background: #E87722; color: #fff; border: none;
    padding: 9px 20px; border-radius: 4px;
    font-size: .9rem; font-weight: bold; cursor: pointer;
    transition: background .15s;
    display: flex; align-items: center; gap: 6px;
  }
  .btn-refresh:hover    { background: #cf6510; }
  .btn-refresh:disabled { background: #aaa; cursor: not-allowed; }

  /* ---- Spinner ---- */
  .spinner {
    display: none; width: 18px; height: 18px;
    border: 3px solid rgba(255,255,255,.3);
    border-top-color: #fff; border-radius: 50%;
    animation: spin .7s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ---- Grid ---- */
  .grid {
    display: grid; grid-template-columns: 1fr;
    gap: 18px; padding: 20px;
    max-width: 1440px; margin: 0 auto;
  }

  /* ---- Port card ---- */
  .port-card {
    background: #fff; border-radius: 8px; overflow: hidden;
    box-shadow: 0 2px 6px rgba(0,0,0,.09);
  }

  /* ---- Port header ---- */
  .port-header {
    background: #002B5C; color: #fff;
    padding: 11px 16px;
    display: flex; justify-content: space-between; align-items: center;
  }
  .port-name { font-size: .95rem; font-weight: bold; }
  .port-code { font-size: .7rem; opacity: .65; margin-top: 1px; }
  .port-total {
    background: #E87722; color: #fff;
    border-radius: 12px; padding: 2px 11px; font-size: .82rem; font-weight: bold;
  }

  /* ---- Counters ---- */
  .counters {
    display: flex; flex-wrap: wrap; gap: 10px;
    padding: 8px 14px; background: #f8f9fb;
    border-bottom: 1px solid #eee; font-size: .8rem;
  }

  /* ---- Error banner ---- */
  .error-banner {
    background: #fdecea; color: #c62828;
    padding: 7px 14px; font-size: .8rem;
    border-left: 4px solid #c62828;
  }

  /* ---- Table ---- */
  .table-wrap { overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: .8rem; }
  th {
    background: #e8edf2; color: #002B5C;
    padding: 7px 10px; text-align: left;
    font-weight: bold; white-space: nowrap;
    border-bottom: 2px solid #c8d3de;
  }
  td { padding: 6px 10px; border-bottom: 1px solid #f0f0f0; white-space: nowrap; }
  tr:last-child td { border-bottom: none; }
  tbody tr:hover td { background: #f5f8ff; }

  /* ---- Row states ---- */
  tr.row-new td { background: #f0fff4 !important; }

  /* ---- Semaphore ---- */
  .g  { color: #1b7e34; font-weight: bold; }
  .o  { color: #E87722; font-weight: bold; }
  .r  { color: #c62828; font-weight: bold; }
  .gr { color: #aaa; }

  /* ---- Change indicator ---- */
  .changed {
    outline: 2px solid #E87722;
    border-radius: 3px; padding: 0 3px; cursor: help;
  }
  .badge-new {
    background: #e8f5e9; color: #1b7e34;
    font-size: .65rem; padding: 1px 5px;
    border-radius: 3px; margin-left: 5px;
    font-weight: bold; vertical-align: middle;
  }
  .bell { font-size: .75rem; margin-left: 2px; }

  /* ---- Empty / loading ---- */
  .empty   { padding: 18px; text-align: center; color: #aaa; font-style: italic; font-size: .82rem; }
  .loading { padding: 18px; text-align: center; color: #666; font-size: .82rem; }

  /* ---- Schedule in fondo ---- */
  .schedule-bottom {
    max-width: 1440px; margin: 0 auto; padding: 0 20px 28px;
  }
  .section-card {
    background: #fff; border-radius: 8px; overflow: hidden;
    box-shadow: 0 2px 6px rgba(0,0,0,.09);
  }
  .section-header {
    background: #004080; color: #fff;
    padding: 10px 16px; font-size: .9rem; font-weight: bold;
  }
  /* ---- Colonna Δ Schedule nelle tabelle porto ---- */
  .disc-ok  { color: #1b7e34; font-weight: bold; }
  .disc-md  { color: #E87722; font-weight: bold; }
  .disc-hi  { color: #c62828; font-weight: bold; }
  td.disc-cell { font-size: .75rem; line-height: 1.6; vertical-align: top; }

  /* ---- Mappa Italia ---- */
  .map-wrap {
    max-width: 1440px; margin: 16px auto 0; padding: 0 20px;
  }
  .map-card {
    background: #fff; border-radius: 8px; overflow: hidden;
    box-shadow: 0 2px 6px rgba(0,0,0,.09);
  }
  .map-card-header {
    background: #002B5C; color: #fff;
    padding: 10px 16px; font-size: .9rem; font-weight: bold;
    display: flex; align-items: center; justify-content: space-between;
  }
  #italy-map { height: 380px; width: 100%; }

  /* Marker personalizzato (div circle) */
  .port-marker {
    border-radius: 50%; display: flex; align-items: center; justify-content: center;
    font-weight: bold; color: #fff; border: 2px solid #fff;
    box-shadow: 0 1px 5px rgba(0,0,0,.45); cursor: pointer;
    font-size: 12px; line-height: 1;
  }

  /* ---- Region group header ---- */
  .region-wrap { margin-bottom: 4px; }
  .region-header {
    max-width: 1440px; margin: 18px auto 6px; padding: 0 20px;
    font-size: .8rem; font-weight: bold; letter-spacing: .8px;
    text-transform: uppercase; color: #002B5C;
    display: flex; align-items: center; gap: 8px;
  }
  .region-header::after {
    content: ''; flex: 1; height: 1px; background: #c8d3de;
  }
</style>
</head>
<body>

<div class="header">
  <div>
    <div class="header-title">&#x1F6A2; NAVI APERTE &mdash; CMA CGM Italy</div>
    <div class="header-sub" id="last-update">Caricamento dati in corso&hellip;</div>
  </div>
  <div class="header-right">
    <div class="spinner" id="spinner"></div>
    <button class="btn-refresh" id="btn-download-schedule" onclick="doDownloadSchedule()">
      📥 Schedule
    </button>
    <button class="btn-refresh" id="btn-refresh" onclick="doRefresh()">
      &#x1F504; Aggiorna dati
    </button>
  </div>
</div>

<div class="map-wrap">
  <div class="map-card">
    <div class="map-card-header">
      &#x1F5FA;&#xFE0F; Panoramica discrepanze &mdash; porti italiani
      <span style="font-size:.75rem;opacity:.7">Aggiornato ad ogni refresh &bull; verde=OK, arancio=warning, rosso=alert</span>
    </div>
    <div id="italy-map"></div>
  </div>
</div>

{% for group in port_groups %}
<div class="region-wrap">
  <div class="region-header">{{ group.label }}</div>
  <div class="grid">
  {% for p in group.ports %}
  <div class="port-card" id="card-{{ p.key }}">
    <div class="port-header">
      <div>
        <div class="port-name">{{ p.name }}</div>
        <div class="port-code">{{ p.code }}</div>
      </div>
      <div class="port-total" id="total-{{ p.key }}">–</div>
    </div>
    <div class="counters" id="ctrs-{{ p.key }}">
      <span>&#x23F3; Caricamento&hellip;</span>
    </div>
    <div id="body-{{ p.key }}">
      <div class="loading">Recupero dati&hellip;</div>
    </div>
  </div>
  {% endfor %}
  </div>
</div>
{% endfor %}

<div class="schedule-bottom">
  <div class="section-card">
    <div class="section-header" style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px">
      <span>&#x1F4C5; Prossime navi in arrivo &mdash; prossimi {{ schedule_days }} giorni (da schedule)</span>
      <span id="schedule-file-info" style="font-size:.72rem;font-weight:normal;opacity:.85">
        <span id="schedule-file-ts" style="background:rgba(255,255,255,.15);padding:2px 8px;border-radius:10px">&#x23F3; in attesa&hellip;</span>
      </span>
    </div>
    <div id="schedule-body"><div class="loading">Caricamento&hellip;</div></div>
  </div>
</div>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const PORTS = {{ ports_json | safe }};

/* ---- Mappa Italia ---- */
const PORT_COORDS = {
  GENOVA_PSA:  [44.413, 8.920],
  SPINELLI:    [44.406, 8.927],
  GENOVA_SECH: [44.409, 8.912],
  LA_SPEZIA:   [44.104, 9.831],
  LIVORNO:     [43.548, 10.308],
  VENEZIA:     [45.430, 12.340],
  TRIESTE:     [45.649, 13.802],
  NAPOLI:      [40.841, 14.268],
  SALERNO:     [40.692, 14.771],
};

const _map = L.map('italy-map', { zoomControl: true }).setView([43.5, 12.5], 6);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  maxZoom: 18,
}).addTo(_map);

// Markers indicizzati per port key
const _markers = {};

function _makeMarkerIcon(count, severity) {
  const bg = severity === 'alert' ? '#c62828'
           : severity === 'warn'  ? '#E87722'
           : severity === 'ok'    ? '#1b7e34'
           :                        '#999';
  const size = count > 0 ? 34 : 28;
  return L.divIcon({
    className: '',
    html: `<div class="port-marker" style="width:${size}px;height:${size}px;background:${bg}">${count > 0 ? count : '&#10003;'}</div>`,
    iconSize:   [size, size],
    iconAnchor: [size/2, size/2],
    popupAnchor:[0, -size/2],
  });
}

// Crea marker iniziali grigi (nessun dato ancora)
PORTS.forEach(p => {
  const coords = PORT_COORDS[p.key];
  if (!coords) return;
  const marker = L.marker(coords, { icon: _makeMarkerIcon(0, 'none') })
    .addTo(_map)
    .bindPopup(`<b>${p.name}</b><br><i>In attesa di dati&hellip;</i>`);
  marker.on('click', () => {
    const el = document.getElementById('card-' + p.key);
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  _markers[p.key] = marker;
});

function updateMapMarkers(portsData) {
  PORTS.forEach(p => {
    const marker = _markers[p.key];
    if (!marker) return;
    const res = portsData[p.key];
    if (!res || res.error) {
      marker.setIcon(_makeMarkerIcon(0, 'none'));
      marker.setPopupContent(`<b>${p.name}</b><br><span style="color:#c62828">&#x26A0; Dati non disponibili</span>`);
      return;
    }
    const rows = res.data || [];
    let alerts = 0, warns = 0;
    rows.forEach(r => {
      const d = Math.max(
        r.delta_eta_h    != null ? r.delta_eta_h    : 0,
        r.delta_cutoff_h != null ? r.delta_cutoff_h : 0,
      );
      if (d >= 24) alerts++;
      else if (d >= 4) warns++;
    });
    const severity = alerts > 0 ? 'alert' : warns > 0 ? 'warn' : rows.length > 0 ? 'ok' : 'none';
    const discCount = alerts + warns;
    marker.setIcon(_makeMarkerIcon(discCount, severity));
    const popBody = discCount === 0
      ? `<span style="color:#1b7e34">&#10003; Nessuna discrepanza</span>`
      : `<span style="color:${alerts>0?'#c62828':'#E87722'}">&#x26A0; ${discCount} discrepanza/e</span>`;
    marker.setPopupContent(`<b>${p.name}</b> <span style="color:#888;font-size:.8em">${rows.length} navi</span><br>${popBody}<br><small style="color:#888">Clicca per scorrere alla card</small>`);
  });
}

/* ---- Date helpers ---- */
function parseDate(s) {
  if (!s) return null;
  let t = s.trim();
  // Normalizza separatore " - " usato da Livorno
  t = t.replace(' - ', ' ');
  // DD/MM/YYYY → YYYY-MM-DD
  t = t.replace(/^(\\d{2})\\/(\\d{2})\\/(\\d{4})/, '$3-$2-$1');
  // DD-MM-YYYY → YYYY-MM-DD
  t = t.replace(/^(\\d{2})-(\\d{2})-(\\d{4})/, '$3-$2-$1');
  // space separator → T
  t = t.replace(' ', 'T');
  const d = new Date(t);
  return isNaN(d) ? null : d;
}
function fmtDate(s) {
  if (!s) return '—';
  const d = parseDate(s);
  if (!d) return s;
  const dd = String(d.getDate()).padStart(2,'0');
  const mm = String(d.getMonth()+1).padStart(2,'0');
  const yy = d.getFullYear();
  const hh = String(d.getHours()).padStart(2,'0');
  const min = String(d.getMinutes()).padStart(2,'0');
  return `${dd}/${mm}/${yy} ${hh}:${min}`;
}
function semClass(s) {
  const d = parseDate(s);
  if (!d) return 'gr';
  const h = (d - Date.now()) / 3600000;
  if (h < 0)  return 'r';
  if (h < 24) return 'o';
  return 'g';
}

/* ---- Render helpers ---- */
function dateCell(val, changes, field) {
  const v   = fmtDate(val);
  const cls = semClass(val);
  const chg = changes && changes[field];
  const chgAttr = chg ? ` title="&#x1F514; Era: ${fmtDate(chg)}" class="${cls} changed"` : ` class="${cls}"`;
  const bell = chg ? '<span class="bell">&#x1F514;</span>' : '';
  return `<td><span${chgAttr}>${v}${bell}</span></td>`;
}

/* ---- Delta schedule cell (colonna extra nelle tabelle porto) ---- */
function discCell(r) {
  if (r.sched_eta === null || r.sched_eta === undefined)
    return '<td class="gr" title="Non presente nel file schedule">—</td>';

  const lines = [];

  const hasSchedEta    = !!r.sched_eta;
  const hasSchedCutoff = !!r.sched_cutoff;

  if (r.delta_eta_h !== null && r.delta_eta_h !== undefined) {
    const cls = r.delta_eta_h < 4 ? 'disc-ok' : r.delta_eta_h < 24 ? 'disc-md' : 'disc-hi';
    lines.push(`<span class="${cls}">ETA &#177;${r.delta_eta_h}h</span>`);
  } else if (hasSchedEta && !r.eta) {
    lines.push(`<span class="gr" title="ETA non disponibile sul sito porto">ETA n/d</span>`);
  } else if (hasSchedEta && r.eta && r.eta.length <= 5) {
    // Salerno restituisce solo l'ora (es. "17:00"), impossibile confrontare senza la data
    lines.push(`<span class="gr" title="Porto fornisce solo l'ora, non la data completa">ETA n/d</span>`);
  } else if (hasSchedEta) {
    lines.push(`<span class="gr" title="Impossibile confrontare ETA (${r.eta})">ETA ?</span>`);
  }

  if (r.delta_cutoff_h !== null && r.delta_cutoff_h !== undefined) {
    const cls = r.delta_cutoff_h < 4 ? 'disc-ok' : r.delta_cutoff_h < 24 ? 'disc-md' : 'disc-hi';
    lines.push(`<span class="${cls}">Chius &#177;${r.delta_cutoff_h}h</span>`);
  }

  const tooltip = `ETA schedule: ${r.sched_eta || '—'}&#10;Cutoff schedule: ${r.sched_cutoff || '—'}`;
  // "✓ OK" solo se abbiamo effettivamente confrontato entrambi i valori e sono nella soglia
  const bothCompared = (r.delta_eta_h !== null && r.delta_eta_h !== undefined) ||
                       (!hasSchedEta);   // se ETA non è in schedule, ignoriamo quel lato
  if (lines.length === 0 && bothCompared)
    return `<td class="disc-cell disc-ok" title="${tooltip}">&#10003; OK</td>`;
  if (lines.length === 0)
    return `<td class="disc-cell gr" title="${tooltip}">—</td>`;
  return `<td class="disc-cell" title="${tooltip}">${lines.join('<br>')}</td>`;
}

function buildTable(rows) {
  if (!rows || rows.length === 0)
    return '<div class="empty">Nessuna nave disponibile</div>';

  const heads = ['Nave','Viaggio','Servizio','ETA','Accettazione','Fine Accettazione','Chiusura',
                 'ETA (schedule)','Cutoff (schedule)','&#916; Schedule'];
  const ths   = heads.map(h => `<th>${h}</th>`).join('');

  const trs = rows.map(r => {
    const chg    = r.changes || {};
    const isNew  = r.is_new;
    const badge  = isNew ? '<span class="badge-new">NEW</span>' : '';
    const rowCls = isNew ? ' class="row-new"' : '';
    const schedEta    = r.sched_eta    || '<span class="gr">—</span>';
    const schedCutoff = r.sched_cutoff || '<span class="gr">—</span>';
    const svcLabel    = r.sched_service
      ? `<span style="font-size:.72rem;background:#e8f0fe;color:#1a73e8;padding:1px 5px;border-radius:3px;white-space:nowrap">${r.sched_service}</span>`
      : '<span class="gr">—</span>';
    return `<tr${rowCls}>
      <td><b>${r.nave || '—'}</b>${badge}</td>
      <td>${r.viaggio || '—'}</td>
      <td>${svcLabel}</td>
      ${dateCell(r.eta,              chg, 'eta')}
      ${dateCell(r.accettazione,      chg, 'accettazione')}
      ${dateCell(r.fine_accettazione, chg, 'fine_accettazione')}
      ${dateCell(r.chiusura,          chg, 'chiusura')}
      <td style="font-size:.78rem">${schedEta}</td>
      <td style="font-size:.78rem">${schedCutoff}</td>
      ${discCell(r)}
    </tr>`;
  }).join('');

  return `<div class="table-wrap"><table>
    <thead><tr>${ths}</tr></thead>
    <tbody>${trs}</tbody>
  </table></div>`;
}

/* ---- Render port card ---- */
function renderPort(key, res) {
  const totalEl = document.getElementById('total-' + key);
  const ctrsEl  = document.getElementById('ctrs-'  + key);
  const bodyEl  = document.getElementById('body-'  + key);

  if (res.error) {
    const stale = res.stale_data || [];
    totalEl.textContent = stale.length ? stale.length + ' (stale)' : '–';
    ctrsEl.innerHTML = `<span style="color:#c62828">&#x26A0;&#xFE0F; ${res.message}</span>`;
    bodyEl.innerHTML =
      `<div class="error-banner">&#x26A0;&#xFE0F; Dati non disponibili &mdash; mostrati dati precedenti</div>`
      + buildTable(stale);
    return;
  }

  const rows = res.data || [];
  const now  = Date.now();
  let open = 0, soon = 0, expired = 0;

  rows.forEach(r => {
    const key = r.fine_accettazione || r.accettazione || r.eta;
    const d   = parseDate(key);
    if (!d)       { open++; return; }
    const h = (d - now) / 3600000;
    if (h < 0)    expired++;
    else if (h < 24) soon++;
    else          open++;
  });

  totalEl.textContent = rows.length || '0';
  ctrsEl.innerHTML = `
    <span>&#x2705; Aperte: <b>${open}</b></span>
    <span>&#x26A0;&#xFE0F; Scadono oggi: <b>${soon}</b></span>
    <span style="color:#c62828">&#x1F534; Scadute: <b>${expired}</b></span>`;
  bodyEl.innerHTML = buildTable(rows);
}

/* ---- Schedule in fondo ---- */
function buildScheduleBottom(rows) {
  if (!rows || rows.length === 0)
    return '<div class="empty">Nessuna nave nei prossimi {{ schedule_days }} giorni</div>';
  const heads = ['Nave','Viaggio','Porto','Servizio','ETA','Cutoff','ETD'];
  const ths   = heads.map(h => `<th>${h}</th>`).join('');
  const trs   = rows.map(r => `<tr>
    <td><b>${r.nave}</b></td>
    <td>${r.viaggio || '—'}</td>
    <td>${r.porto_name} <span style="color:#aaa;font-size:.72rem">${r.porto_code}</span></td>
    <td>${r.service || '—'}</td>
    <td><span class="${semClass(r.eta)}">${r.eta}</span></td>
    <td><span class="${r.cutoff ? semClass(r.cutoff) : 'gr'}">${r.cutoff || '—'}</span></td>
    <td><span class="${r.etd ? semClass(r.etd) : 'gr'}">${r.etd || '—'}</span></td>
  </tr>`).join('');
  return `<div class="table-wrap"><table>
    <thead><tr>${ths}</tr></thead>
    <tbody>${trs}</tbody>
  </table></div>`;
}

/* ---- Refresh ---- */
function doRefresh() {
  const btn     = document.getElementById('btn-refresh');
  const spinner = document.getElementById('spinner');
  btn.disabled  = true;
  spinner.style.display = 'inline-block';

  fetch('/refresh')
    .then(r => r.json())
    .then(data => {
      const ts = new Date(data.timestamp);
      document.getElementById('last-update').textContent =
        'Ultimo aggiornamento: ' + ts.toLocaleString('it-IT');

      // Badge data/ora file schedule
      const tsEl = document.getElementById('schedule-file-ts');
      if (tsEl) {
        if (data.schedule_file_ts) {
          const d = new Date(data.schedule_file_ts);
          const label = '&#x1F4C2; File: ' + d.toLocaleString('it-IT');
          const color = data.schedule_dl_ok ? 'rgba(255,255,255,.18)' : 'rgba(232,119,34,.45)';
          const dlIcon = data.schedule_dl_ok ? '' : ' &#x26A0;&#xFE0F;';
          tsEl.style.background = color;
          tsEl.innerHTML = label + dlIcon;
        } else {
          tsEl.style.background = 'rgba(198,40,40,.45)';
          tsEl.innerHTML = '&#x26A0;&#xFE0F; File schedule non trovato';
        }
      }

      const portsData = data.ports || {};
      PORTS.forEach(p => {
        const res = portsData[p.key];
        if (res) renderPort(p.key, res);
      });
      updateMapMarkers(portsData);
      document.getElementById('schedule-body').innerHTML = buildScheduleBottom(data.schedule || []);
    })
    .catch(err => alert('Errore durante il refresh: ' + err))
    .finally(() => {
      btn.disabled = false;
      spinner.style.display = 'none';
    });
}

/* ---- Download Schedule ---- */
function doDownloadSchedule() {
  const btn = document.getElementById('btn-download-schedule');
  btn.disabled = true;

  fetch('/download-schedule')
    .then(r => r.json())
    .then(data => {
      const tsEl = document.getElementById('schedule-file-ts');
      if (tsEl) {
        if (data.success) {
          tsEl.style.background = 'rgba(76,175,80,.3)';
          tsEl.innerHTML = '✅ ' + data.message.substring(0, 30);
          setTimeout(() => {
            doRefresh();
          }, 500);
        } else {
          tsEl.style.background = 'rgba(198,40,40,.45)';
          tsEl.innerHTML = '❌ ' + data.message.substring(0, 30);
        }
      }
    })
    .catch(err => alert('Errore durante il download: ' + err))
    .finally(() => {
      btn.disabled = false;
    });
}

document.addEventListener('DOMContentLoaded', doRefresh);
</script>
</body>
</html>"""


@app.route("/")
def index():
    # Check: Scarica schedule all'avvio se non esiste
    if not SCHEDULE_FILE.exists():
        download_schedule_sharepoint()

    ports_json = json.dumps([
        {"key": p["key"], "name": p["name"], "code": p["code"]}
        for p in PORTS
    ])
    ports_by_key = {p["key"]: p for p in PORTS}
    port_groups_with_ports = [
        {**g, "ports": [ports_by_key[k] for k in g["keys"] if k in ports_by_key]}
        for g in PORT_GROUPS
    ]
    return render_template_string(
        HTML_TEMPLATE,
        ports=PORTS,
        ports_json=ports_json,
        port_groups=port_groups_with_ports,
        schedule_days=SCHEDULE_DAYS,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _start_server():
    import socket
    for port in [5000, 5001, 5002]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                url = f"http://localhost:{port}"
                print(f"\n  Dashboard avviata → {url}\n  Premi Ctrl+C per fermare.\n")
                threading.Timer(1.5, webbrowser.open, args=[url]).start()
                app.run(host="127.0.0.1", port=port, debug=False)
                return
        log.warning(f"Porta {port} occupata, provo la prossima...")
    print("ERRORE: porte 5000-5002 tutte occupate. Libera una porta e riprova.")


if __name__ == "__main__":
    _start_server()