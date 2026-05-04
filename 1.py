import requests
from datetime import datetime, timezone

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
            "nave":          nave,
            "callSign":      x.get("callSign"),
            "viaggio":       x.get("exportVoyCode") or x.get("importVoyCode"),
            "gsVoyCode":     x.get("gsVoyCode"),
            "eta":           fmt_dt(x.get("eta")),
            "etd":           fmt_dt(x.get("etd")),
            "chiusura":      fmt_dt(x.get("customsDeadline")),
            "imo_reefer":    fmt_dt(x.get("imoReeferAcceptance")),
            "nota":          x.get("note"),
            "sezione":       section,   # "IN_ACCETTAZIONE" | "PROSSIME_APERTURE"
            "porto":         "Genova Spinelli",
        }

    data = []
    for section_key in ("IN_ACCETTAZIONE", "PROSSIME_APERTURE"):
        for x in body.get(section_key) or []:
            vessel = _parse_vessel(x, section_key)
            if vessel:
                data.append(vessel)

    log.info(f"Spinelli: {len(data)} navi totali "
             f"({sum(1 for d in data if d['sezione']=='IN_ACCETTAZIONE')} acc. + "
             f"{sum(1 for d in data if d['sezione']=='PROSSIME_APERTURE')} pross.)")

    return {"error": False, "data": data}