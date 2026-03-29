"""
Puls — Ukentlig timerapportering
Start: uvicorn puls.app:app --reload --port 8502
Eller: python puls/app.py
"""
from fastapi import FastAPI, Request, Query
from typing import Optional
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from pathlib import Path
import json
from datetime import datetime, date, timedelta
import uvicorn
import openpyxl
import os

app = FastAPI(title="Puls")
BASE = Path(__file__).parent

# Jinja2 direkte (omgår Starlette-wrapper som har bug i Python 3.14)
jinja_env = Environment(
    loader=FileSystemLoader(BASE / "templates"),
    autoescape=select_autoescape(["html"]),
    cache_size=0,
)
jinja_env.filters["tojson"] = lambda v: Markup(json.dumps(v, ensure_ascii=False))

def render(template_name: str, **ctx) -> HTMLResponse:
    html = jinja_env.get_template(template_name).render(**ctx)
    return HTMLResponse(html)

# På Azure App Service bruker vi /home/data (persistent på tvers av restarts)
# Lokalt bruker vi puls/data/
DATA_DIR = Path("/home/data") if os.environ.get("WEBSITE_SITE_NAME") else BASE / "data"
DATA_DIR.mkdir(exist_ok=True)
SVAR_FIL      = DATA_DIR / "svar.json"
BRUKERE_FIL   = DATA_DIR / "brukere.json"
INV_FIL       = DATA_DIR / "investeringer.json"
FAKTA_FIL     = DATA_DIR / "fakta_puls.xlsx"

def les_investeringer() -> list:
    return les_json(INV_FIL, [
        "SalMar", "Sinkaberg-Hansen", "BEWi", "Arctic Fish",
        "Kingfish Company", "Kvarv", "Kverva-møter", "Admin / Annet",
    ])

FAKTA_KOLONNER = ["Navn", "Epost", "Uke", "År", "Dato innsending", "Investering", "Timer"]

def skriv_fakta_puls(navn, epost, uke, år, tidspunkt, timer: dict):
    """Legg til rader i fakta_puls.xlsx — én rad per investering."""
    if FAKTA_FIL.exists():
        wb = openpyxl.load_workbook(FAKTA_FIL)
        ws = wb.active
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Puls"
        ws.append(FAKTA_KOLONNER)

    dato_str = tidspunkt[:10]
    # Fjern eksisterende rader for samme person+uke+år
    rader_å_beholde = [ws[1]]  # header
    for row in ws.iter_rows(min_row=2, values_only=False):
        vals = [c.value for c in row]
        if not (vals[0] == navn and vals[2] == uke and vals[3] == år):
            rader_å_beholde.append(row)

    # Skriv ny fil
    wb2 = openpyxl.Workbook()
    ws2 = wb2.active
    ws2.title = "Puls"
    ws2.append(FAKTA_KOLONNER)
    for row in rader_å_beholde[1:]:
        ws2.append([c.value for c in row])
    for inv, t in timer.items():
        ws2.append([navn, epost, uke, år, dato_str, inv, t])
    wb2.save(FAKTA_FIL)

# ── Hjelpefunksjoner ─────────────────────────────────────────────────────────

def les_json(fil: Path, default):
    if not fil.exists():
        return default
    try:
        return json.loads(fil.read_text(encoding="utf-8"))
    except Exception:
        return default

def lagre_json(fil: Path, data):
    fil.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def get_uke_år():
    iso = date.today().isocalendar()
    return iso[1], iso[0]

def finn_bruker(token: str):
    brukere = les_json(BRUKERE_FIL, {})
    return brukere.get(token)

def forrige_uke_svar(token: str, uke: int, år: int) -> dict:
    svar = les_json(SVAR_FIL, [])
    fu, få = (52, år - 1) if uke == 1 else (uke - 1, år)
    for s in reversed(svar):
        if s["token"] == token and s["uke"] == fu and s["år"] == få:
            return s["timer"]
    return {}

def har_svart(token: str, uke: int, år: int) -> bool:
    svar = les_json(SVAR_FIL, [])
    return any(s["token"] == token and s["uke"] == uke and s["år"] == år for s in svar)

def historikk_bruker(token: str, år: int) -> list:
    svar = les_json(SVAR_FIL, [])
    return [s for s in svar if s["token"] == token and s["år"] == år]

def siste_svar(token: str, uke: int, år: int) -> dict | None:
    """Returner siste innsendte svar (ikke inneværende uke), eller None."""
    svar = les_json(SVAR_FIL, [])
    kandidater = [s for s in svar if s["token"] == token and not (s["uke"] == uke and s["år"] == år)]
    if not kandidater:
        return None
    return max(kandidater, key=lambda s: (s["år"], s["uke"]))

def fredag_kl_12(uke: int, år: int) -> datetime:
    jan4 = date(år, 1, 4)
    mandag = jan4 - timedelta(days=jan4.weekday()) + timedelta(weeks=uke - 1)
    fredag = mandag + timedelta(days=4)
    return datetime(fredag.year, fredag.month, fredag.day, 12, 0, 0)

def fmt_delta(minutter: float) -> str:
    m = int(minutter)
    if m < 60:
        return f"{m} min"
    return f"{m // 60}t {m % 60:02d}min"

def ranker_uke(uke: int, år: int) -> list:
    t0 = fredag_kl_12(uke, år)
    alle = les_json(SVAR_FIL, [])
    aktuelle = [s for s in alle if s["uke"] == uke and s["år"] == år and not s.get("fravær")]
    resultat = []
    for s in aktuelle:
        delta = max(0, (datetime.fromisoformat(s["tidspunkt"]) - t0).total_seconds() / 60)
        resultat.append({"navn": s["navn"].split()[0], "delta_min": delta, "delta_fmt": fmt_delta(delta), "total": s.get("total", 0)})
    return sorted(resultat, key=lambda x: x["delta_min"])

def måneds_ranking(måned: int, år: int) -> list:
    alle = les_json(SVAR_FIL, [])
    per_person: dict = {}
    for s in alle:
        if s.get("fravær") or s["år"] != år:
            continue
        if datetime.fromisoformat(s["tidspunkt"]).month != måned:
            continue
        t0 = fredag_kl_12(s["uke"], s["år"])
        delta = max(0, (datetime.fromisoformat(s["tidspunkt"]) - t0).total_seconds() / 60)
        navn = s["navn"].split()[0]
        per_person.setdefault(navn, []).append(delta)
    resultat = [{"navn": n, "snitt_min": sum(v) / len(v), "snitt_fmt": fmt_delta(sum(v) / len(v)), "antall": len(v)} for n, v in per_person.items()]
    return sorted(resultat, key=lambda x: x["snitt_min"])[:5]

def all_time_toppliste() -> list:
    alle = les_json(SVAR_FIL, [])
    uker = {(s["uke"], s["år"]) for s in alle}
    poeng: dict = {}
    for uke, år in uker:
        for i, r in enumerate(ranker_uke(uke, år)):
            p = poeng.setdefault(r["navn"], {"poeng": 0, "nr1": 0, "antall": 0})
            p["antall"] += 1
            p["nr1"] += (i == 0)
            p["poeng"] += max(0, 5 - i)
    return sorted([{"navn": n, **v} for n, v in poeng.items()], key=lambda x: -x["poeng"])[:8]

def hall_of_shame_liste(nå_uke: int, nå_år: int) -> list:
    brukere = les_json(BRUKERE_FIL, {})
    alle = les_json(SVAR_FIL, [])
    resultat = []
    for token, b in brukere.items():
        rapporterte = {(s["uke"], s["år"]) for s in alle if s["token"] == token}
        mangler_n = sum(1 for u in range(1, nå_uke) if (u, nå_år) not in rapporterte)
        if mangler_n > 0:
            resultat.append({"navn": b["navn"].split()[0], "mangler": mangler_n})
    return sorted(resultat, key=lambda x: -x["mangler"])[:5]

def personlig_stats(token: str, nå_uke: int, nå_år: int) -> dict:
    alle = les_json(SVAR_FIL, [])
    mine = [s for s in alle if s["token"] == token and s["år"] == nå_år and not s.get("fravær")]
    if not mine:
        return {}
    total_timer = sum(s.get("total", 0) for s in mine)
    antall_uker = len(mine)
    # Favorittinvestering
    inv_sum: dict = {}
    for s in mine:
        for inv, t in s.get("timer", {}).items():
            inv_sum[inv] = inv_sum.get(inv, 0) + t
    favoritt = max(inv_sum, key=lambda k: inv_sum[k]) if inv_sum else "–"
    # Streak
    streak = 0
    for u in range(nå_uke - 1, 0, -1):
        if any(s["uke"] == u for s in mine):
            streak += 1
        else:
            break
    return {
        "antall_uker": antall_uker,
        "total_timer": total_timer,
        "snitt": round(total_timer / antall_uker, 1) if antall_uker else 0,
        "favoritt": favoritt,
        "streak": streak,
    }

def manglende_uker(token: str, nå_uke: int, nå_år: int) -> list:
    """Returner liste av (uke, år) som ikke er rapportert, fra uke 1 til forrige uke."""
    rapporterte = {(s["uke"], s["år"]) for s in les_json(SVAR_FIL, []) if s["token"] == token}
    mangler = []
    for u in range(1, nå_uke):
        if (u, nå_år) not in rapporterte:
            mangler.append((u, nå_år))
    return mangler

# ── Ruter ────────────────────────────────────────────────────────────────────

@app.get("/puls/{token}", response_class=HTMLResponse)
async def vis_skjema(request: Request, token: str,
                     uke: Optional[int] = Query(None),
                     år: Optional[int] = Query(None)):
    bruker = finn_bruker(token)
    if not bruker:
        return HTMLResponse("<h1 style='font-family:sans-serif;padding:40px'>Ugyldig eller utløpt lenke.</h1>", status_code=404)
    nå_uke, nå_år = get_uke_år()
    # Bruk query-param uke/år hvis oppgitt, ellers inneværende uke
    uke = uke if uke is not None else nå_uke
    år  = år  if år  is not None else nå_år
    allerede_svart = har_svart(token, uke, år)
    forrige = forrige_uke_svar(token, uke, år)
    hist    = historikk_bruker(token, år)
    mangler = manglende_uker(token, nå_uke, nå_år)
    mangler = [(u, å) for u, å in mangler if not (u == uke and å == år)]
    siste   = siste_svar(token, uke, år)
    return render("form.html",
        bruker=bruker, token=token, uke=uke, år=år,
        investeringer=les_investeringer(), forrige=forrige,
        allerede_svart=allerede_svart, historikk=hist,
        mangler=mangler, siste=siste,
    )

@app.post("/puls/{token}", response_class=HTMLResponse)
async def send_inn(request: Request, token: str):
    bruker = finn_bruker(token)
    if not bruker:
        return HTMLResponse("<h1>Ugyldig lenke</h1>", status_code=404)
    form = await request.form()
    # Uke/år kommer fra skjulte felt i skjemaet (støtter historisk rapportering)
    nå_uke, nå_år = get_uke_år()
    try:
        uke = int(form.get("_uke", nå_uke))
        år  = int(form.get("_år",  nå_år))
    except (ValueError, TypeError):
        uke, år = nå_uke, nå_år
    investeringer = les_investeringer()
    timer = {}
    total = 0
    for inv in investeringer:
        v = min(40, max(0, int(form.get(f"t_{inv.replace(' ','_').replace('/','_')}", 0) or 0)))
        timer[inv] = v
        total += v
    fravar = form.get("_fravar") == "1"

    if fravar:
        svar = les_json(SVAR_FIL, [])
        svar = [s for s in svar if not (s["token"] == token and s["uke"] == uke and s["år"] == år)]
        svar.append({
            "token": token,
            "navn": bruker["navn"],
            "epost": bruker["epost"],
            "uke": uke,
            "år": år,
            "fravær": True,
            "timer": {},
            "total": 0,
            "tidspunkt": datetime.now().isoformat(),
        })
        lagre_json(SVAR_FIL, svar)
        return RedirectResponse(f"/puls/{token}/takk?fravar=1", status_code=303)

    if total > 40:
        total = 40
    svar = les_json(SVAR_FIL, [])
    svar = [s for s in svar if not (s["token"] == token and s["uke"] == uke and s["år"] == år)]
    svar.append({
        "token": token,
        "navn": bruker["navn"],
        "epost": bruker["epost"],
        "uke": uke,
        "år": år,
        "timer": timer,
        "total": total,
        "tidspunkt": datetime.now().isoformat(),
    })
    lagre_json(SVAR_FIL, svar)
    skriv_fakta_puls(bruker["navn"], bruker["epost"], uke, år, datetime.now().isoformat(), timer)
    return RedirectResponse(f"/puls/{token}/takk", status_code=303)

@app.get("/puls/{token}/takk", response_class=HTMLResponse)
async def takk(request: Request, token: str, fravar: Optional[int] = Query(None)):
    bruker = finn_bruker(token)
    if not bruker:
        return HTMLResponse("<h1>Ugyldig lenke</h1>", status_code=404)
    uke, år = get_uke_år()
    hist = historikk_bruker(token, år)
    siste = next((s for s in reversed(hist) if s["uke"] == uke), None)
    mangler = manglende_uker(token, uke, år)
    return render("takk.html",
        bruker=bruker, token=token, uke=uke, år=år,
        historikk=hist, siste=siste, mangler=mangler,
        fravar=bool(fravar),
    )

@app.get("/puls/{token}/stats", response_class=HTMLResponse)
async def stats(request: Request, token: str):
    bruker = finn_bruker(token)
    if not bruker:
        return HTMLResponse("<h1 style='font-family:sans-serif;padding:40px'>Ugyldig lenke.</h1>", status_code=404)
    nå_uke, nå_år = get_uke_år()
    nå_måned = date.today().month
    return render("stats.html",
        bruker=bruker, token=token, uke=nå_uke, år=nå_år,
        denne_uken=ranker_uke(nå_uke, nå_år),
        måneds=måneds_ranking(nå_måned, nå_år),
        nå_måned_navn=date(nå_år, nå_måned, 1).strftime("%B %Y").capitalize(),
        alltime=all_time_toppliste(),
        shame=hall_of_shame_liste(nå_uke, nå_år),
        mine=personlig_stats(token, nå_uke, nå_år),
        historikk=historikk_bruker(token, nå_år),
    )

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8502, reload=True, app_dir=str(BASE))
