"""
Puls — Ukentlig timerapportering
Start: uvicorn puls.app:app --reload --port 8502
Eller: python puls/app.py
"""
from fastapi import FastAPI, Request, Query, Cookie
from typing import Optional
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup
from pathlib import Path
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date, timedelta
import uvicorn
import openpyxl
import os
import secrets
import calendar

app = FastAPI(title="Puls")
BASE = Path(__file__).parent
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "puls-admin")
EXPORT_API_KEY = os.environ.get("EXPORT_API_KEY", "")

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
DB_FIL   = DATA_DIR / "puls.db"
FAKTA_FIL = DATA_DIR / "fakta_puls.xlsx"

# ── Database ──────────────────────────────────────────────────────────────────

@contextmanager
def db():
    con = sqlite3.connect(DB_FIL, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()

def init_db():
    with db() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS brukere (
                token TEXT PRIMARY KEY,
                navn  TEXT NOT NULL,
                epost TEXT NOT NULL,
                lønn  INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS investeringer (
                navn       TEXT PRIMARY KEY,
                rekkefølge INTEGER NOT NULL DEFAULT 0,
                kategori   TEXT NOT NULL DEFAULT 'Annet'
            );
            CREATE TABLE IF NOT EXISTS svar (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                token     TEXT NOT NULL,
                navn      TEXT NOT NULL,
                epost     TEXT NOT NULL,
                uke       INTEGER NOT NULL,
                år        INTEGER NOT NULL,
                fravar    INTEGER NOT NULL DEFAULT 0,
                timer     TEXT NOT NULL DEFAULT '{}',
                total     REAL NOT NULL DEFAULT 0,
                tidspunkt TEXT NOT NULL,
                UNIQUE(token, uke, år)
            );
            CREATE TABLE IF NOT EXISTS trivsel_utsendelser (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                måned      INTEGER NOT NULL,
                år         INTEGER NOT NULL,
                opprettet  TEXT NOT NULL,
                åpen_dager INTEGER NOT NULL DEFAULT 10,
                stengt     INTEGER NOT NULL DEFAULT 0,
                UNIQUE(måned, år)
            );
            CREATE TABLE IF NOT EXISTS trivsel_tokens (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                survey_token  TEXT UNIQUE NOT NULL,
                utsendelse_id INTEGER NOT NULL,
                bruker_token  TEXT NOT NULL,
                brukt         INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS trivsel_svar (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                utsendelse_id INTEGER NOT NULL,
                trivsel       INTEGER NOT NULL,
                samarbeid     INTEGER NOT NULL,
                innsendt      TEXT NOT NULL
            );
        """)
        # Migrering: legg til kategori-kolonne hvis den ikke finnes
        cols = [r[1] for r in con.execute("PRAGMA table_info(investeringer)").fetchall()]
        if "kategori" not in cols:
            con.execute("ALTER TABLE investeringer ADD COLUMN kategori TEXT NOT NULL DEFAULT 'Annet'")
        # Migrering: legg til lønn og team på brukere
        cols_b = [r[1] for r in con.execute("PRAGMA table_info(brukere)").fetchall()]
        if "lønn" not in cols_b:
            con.execute("ALTER TABLE brukere ADD COLUMN lønn INTEGER NOT NULL DEFAULT 0")
        if "team" not in cols_b:
            con.execute("ALTER TABLE brukere ADD COLUMN team TEXT NOT NULL DEFAULT 'investering'")
        if "aktiv" not in cols_b:
            con.execute("ALTER TABLE brukere ADD COLUMN aktiv INTEGER NOT NULL DEFAULT 1")
        # Migrering: trivsel_svar fra gammelt skjema (runde_id/spm1/spm2) til nytt
        cols_ts = [r[1] for r in con.execute("PRAGMA table_info(trivsel_svar)").fetchall()]
        if cols_ts and "utsendelse_id" not in cols_ts:
            con.execute("DROP TABLE trivsel_svar")
            con.execute("""
                CREATE TABLE trivsel_svar (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    utsendelse_id INTEGER NOT NULL,
                    trivsel       INTEGER NOT NULL,
                    samarbeid     INTEGER NOT NULL,
                    innsendt      TEXT NOT NULL
                )
            """)

init_db()

# ── DB-hjelpefunksjoner ───────────────────────────────────────────────────────

KATEGORIER = ["Laks", "Sjømat", "Investeringer", "Kapital", "Annet"]

DEFAULT_INVESTERINGER = [
    {"navn": "SalMar",            "kategori": "Laks"},
    {"navn": "Sinkaberg-Hansen",  "kategori": "Laks"},
    {"navn": "Arctic Fish",       "kategori": "Laks"},
    {"navn": "Kingfish Company",  "kategori": "Laks"},
    {"navn": "LaxValoris",        "kategori": "Laks"},
    {"navn": "Scale",             "kategori": "Sjømat"},
    {"navn": "Pelagia",           "kategori": "Sjømat"},
    {"navn": "Insula",            "kategori": "Sjømat"},
    {"navn": "BEWi",              "kategori": "Investeringer"},
    {"navn": "Salvesen & Thams",  "kategori": "Investeringer"},
    {"navn": "Kvarv",             "kategori": "Annet"},
    {"navn": "Kverva-møter",      "kategori": "Annet"},
    {"navn": "Styremøter",        "kategori": "Annet"},
    {"navn": "Admin / Annet",     "kategori": "Annet"},
]

def les_investeringer() -> list[dict]:
    """Returnerer liste av dicts: [{navn, kategori}, ...]"""
    with db() as con:
        rader = con.execute("SELECT navn, kategori FROM investeringer ORDER BY rekkefølge").fetchall()
    if not rader:
        return DEFAULT_INVESTERINGER
    return [{"navn": r["navn"], "kategori": r["kategori"]} for r in rader]

def les_inv_navn() -> list[str]:
    """Kun navneliste — for bakoverkompatibel bruk."""
    return [i["navn"] for i in les_investeringer()]

def lagre_investeringer(liste: list[dict]):
    """liste = [{navn, kategori}, ...]"""
    with db() as con:
        con.execute("DELETE FROM investeringer")
        for i, item in enumerate(liste):
            con.execute(
                "INSERT OR REPLACE INTO investeringer (navn, rekkefølge, kategori) VALUES (?,?,?)",
                (item["navn"], i, item.get("kategori", "Annet"))
            )

def finn_bruker(token: str):
    with db() as con:
        r = con.execute("SELECT navn, epost FROM brukere WHERE token=?", (token,)).fetchone()
    return dict(r) if r else None

def hent_alle_brukere() -> dict:
    with db() as con:
        rader = con.execute("SELECT token, navn, epost, lønn, team, aktiv FROM brukere").fetchall()
    return {r["token"]: {"navn": r["navn"], "epost": r["epost"], "lønn": r["lønn"], "team": r["team"], "aktiv": r["aktiv"]} for r in rader}

def sett_aktiv_bruker(token: str, aktiv: int):
    with db() as con:
        con.execute("UPDATE brukere SET aktiv=? WHERE token=?", (aktiv, token))

def lagre_bruker(token: str, navn: str, epost: str):
    with db() as con:
        con.execute("INSERT OR REPLACE INTO brukere (token, navn, epost) VALUES (?,?,?)", (token, navn, epost))

def sett_lønn_bruker(token: str, lønn: int):
    with db() as con:
        con.execute("UPDATE brukere SET lønn=? WHERE token=?", (lønn, token))

def sett_team_bruker(token: str, team: str):
    with db() as con:
        con.execute("UPDATE brukere SET team=? WHERE token=?", (team, token))

def fjern_bruker(token: str) -> str:
    with db() as con:
        r = con.execute("SELECT navn FROM brukere WHERE token=?", (token,)).fetchone()
        navn = r["navn"] if r else token
        con.execute("DELETE FROM brukere WHERE token=?", (token,))
    return navn

def _rad_til_svar(r) -> dict:
    return {
        "token": r["token"],
        "navn": r["navn"],
        "epost": r["epost"],
        "uke": r["uke"],
        "år": r["år"],
        "fravær": bool(r["fravar"]),
        "timer": json.loads(r["timer"]),
        "total": r["total"],
        "tidspunkt": r["tidspunkt"],
    }

def hent_alle_svar() -> list:
    with db() as con:
        rader = con.execute("SELECT * FROM svar ORDER BY tidspunkt").fetchall()
    return [_rad_til_svar(r) for r in rader]

def upsert_svar(token, navn, epost, uke, år, fravar, timer, total, tidspunkt):
    with db() as con:
        con.execute("""
            INSERT INTO svar (token, navn, epost, uke, år, fravar, timer, total, tidspunkt)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(token, uke, år) DO UPDATE SET
                navn=excluded.navn, epost=excluded.epost,
                fravar=excluded.fravar, timer=excluded.timer,
                total=excluded.total, tidspunkt=excluded.tidspunkt
        """, (token, navn, epost, uke, år, int(fravar), json.dumps(timer, ensure_ascii=False), total, tidspunkt))

# ── Hjelpefunksjoner (uendret logikk) ────────────────────────────────────────

FAKTA_KOLONNER = ["Navn", "Epost", "Uke", "År", "Dato innsending", "Investering", "Timer"]

def skriv_fakta_puls(navn, epost, uke, år, tidspunkt, timer: dict):
    if FAKTA_FIL.exists():
        wb = openpyxl.load_workbook(FAKTA_FIL)
        ws = wb.active
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Puls"
        ws.append(FAKTA_KOLONNER)

    dato_str = tidspunkt[:10]
    rader_å_beholde = [ws[1]]
    for row in ws.iter_rows(min_row=2, values_only=False):
        vals = [c.value for c in row]
        if not (vals[0] == navn and vals[2] == uke and vals[3] == år):
            rader_å_beholde.append(row)

    wb2 = openpyxl.Workbook()
    ws2 = wb2.active
    ws2.title = "Puls"
    ws2.append(FAKTA_KOLONNER)
    for row in rader_å_beholde[1:]:
        ws2.append([c.value for c in row])
    for inv, t in timer.items():
        ws2.append([navn, epost, uke, år, dato_str, inv, t])
    wb2.save(FAKTA_FIL)

def get_uke_år():
    iso = date.today().isocalendar()
    return iso[1], iso[0]

def forrige_uke_svar(token: str, uke: int, år: int) -> dict:
    fu, få = (52, år - 1) if uke == 1 else (uke - 1, år)
    with db() as con:
        r = con.execute("SELECT timer FROM svar WHERE token=? AND uke=? AND år=?", (token, fu, få)).fetchone()
    return json.loads(r["timer"]) if r else {}

def har_svart(token: str, uke: int, år: int) -> bool:
    with db() as con:
        r = con.execute("SELECT 1 FROM svar WHERE token=? AND uke=? AND år=?", (token, uke, år)).fetchone()
    return r is not None

def historikk_bruker(token: str, år: int) -> list:
    with db() as con:
        rader = con.execute("SELECT * FROM svar WHERE token=? AND år=? ORDER BY uke", (token, år)).fetchall()
    return [_rad_til_svar(r) for r in rader]

def siste_svar(token: str, uke: int, år: int) -> dict | None:
    with db() as con:
        r = con.execute("""
            SELECT * FROM svar WHERE token=? AND NOT (uke=? AND år=?)
            ORDER BY år DESC, uke DESC LIMIT 1
        """, (token, uke, år)).fetchone()
    return _rad_til_svar(r) if r else None

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
    with db() as con:
        rader = con.execute(
            "SELECT * FROM svar WHERE uke=? AND år=? AND fravar=0", (uke, år)
        ).fetchall()
    resultat = []
    for r in rader:
        s = _rad_til_svar(r)
        delta = max(0, (datetime.fromisoformat(s["tidspunkt"]) - t0).total_seconds() / 60)
        resultat.append({"navn": s["navn"].split()[0], "delta_min": delta, "delta_fmt": fmt_delta(delta), "total": s.get("total", 0)})
    return sorted(resultat, key=lambda x: x["delta_min"])

def måneds_ranking(måned: int, år: int) -> list:
    with db() as con:
        rader = con.execute("SELECT * FROM svar WHERE år=? AND fravar=0", (år,)).fetchall()
    per_person: dict = {}
    for r in rader:
        s = _rad_til_svar(r)
        if datetime.fromisoformat(s["tidspunkt"]).month != måned:
            continue
        t0 = fredag_kl_12(s["uke"], s["år"])
        delta = max(0, (datetime.fromisoformat(s["tidspunkt"]) - t0).total_seconds() / 60)
        navn = s["navn"].split()[0]
        per_person.setdefault(navn, []).append(delta)
    resultat = [{"navn": n, "snitt_min": sum(v) / len(v), "snitt_fmt": fmt_delta(sum(v) / len(v)), "antall": len(v)} for n, v in per_person.items()]
    return sorted(resultat, key=lambda x: x["snitt_min"])[:5]

def all_time_toppliste() -> list:
    with db() as con:
        uker = con.execute("SELECT DISTINCT uke, år FROM svar").fetchall()
    poeng: dict = {}
    for row in uker:
        for i, r in enumerate(ranker_uke(row["uke"], row["år"])):
            p = poeng.setdefault(r["navn"], {"poeng": 0, "nr1": 0, "antall": 0})
            p["antall"] += 1
            p["nr1"] += (i == 0)
            p["poeng"] += max(0, 5 - i)
    return sorted([{"navn": n, **v} for n, v in poeng.items()], key=lambda x: -x["poeng"])[:8]

def hall_of_shame_liste(nå_uke: int, nå_år: int) -> list:
    brukere = {t: b for t, b in hent_alle_brukere().items() if b["aktiv"]}
    resultat = []
    for token, b in brukere.items():
        with db() as con:
            rapporterte = {(r["uke"], r["år"]) for r in con.execute(
                "SELECT uke, år FROM svar WHERE token=?", (token,)
            ).fetchall()}
        mangler_n = sum(1 for u in range(1, nå_uke) if (u, nå_år) not in rapporterte)
        if mangler_n > 0:
            resultat.append({"navn": b["navn"].split()[0], "mangler": mangler_n})
    return sorted(resultat, key=lambda x: -x["mangler"])[:5]

def personlig_stats(token: str, nå_uke: int, nå_år: int) -> dict:
    with db() as con:
        rader = con.execute(
            "SELECT * FROM svar WHERE token=? AND år=? AND fravar=0", (token, nå_år)
        ).fetchall()
    mine = [_rad_til_svar(r) for r in rader]
    if not mine:
        return {}
    total_timer = sum(s.get("total", 0) for s in mine)
    antall_uker = len(mine)
    inv_sum: dict = {}
    for s in mine:
        for inv, t in s.get("timer", {}).items():
            inv_sum[inv] = inv_sum.get(inv, 0) + t
    favoritt = max(inv_sum, key=lambda k: inv_sum[k]) if inv_sum else "–"
    streak = 0
    uker_svart = {s["uke"] for s in mine}
    for u in range(nå_uke - 1, 0, -1):
        if u in uker_svart:
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
    with db() as con:
        rapporterte = {(r["uke"], r["år"]) for r in con.execute(
            "SELECT uke, år FROM svar WHERE token=?", (token,)
        ).fetchall()}
    return [(u, nå_år) for u in range(1, nå_uke) if (u, nå_år) not in rapporterte]

# ── Kostnadsfordeling ────────────────────────────────────────────────────────

MÅNEDS_NAVN = ["Januar","Februar","Mars","April","Mai","Juni",
               "Juli","August","September","Oktober","November","Desember"]

def finn_uker_for_måned(måned: int, år: int) -> list:
    _, days = calendar.monthrange(år, måned)
    uker = set()
    for dag in range(1, days + 1):
        iso = date(år, måned, dag).isocalendar()
        uker.add((iso[1], iso[0]))
    return list(uker)

def hent_alle_timer_for_uker(token: str, uker: list) -> dict:
    """Returnerer alle timer inkl. Annet-kategorier."""
    timer_inv: dict = {}
    for (uke, å) in uker:
        with db() as con:
            r = con.execute("SELECT * FROM svar WHERE token=? AND uke=? AND år=?", (token, uke, å)).fetchone()
        if r:
            s = _rad_til_svar(r)
            if not s["fravær"]:
                for inv, t in s["timer"].items():
                    if t > 0:
                        timer_inv[inv] = timer_inv.get(inv, 0) + t
    return timer_inv

def hent_timer_for_uker(token: str, uker: list, inkl_navn: set) -> dict:
    timer_inv: dict = {}
    for (uke, å) in uker:
        with db() as con:
            r = con.execute("SELECT * FROM svar WHERE token=? AND uke=? AND år=?", (token, uke, å)).fetchone()
        if r:
            s = _rad_til_svar(r)
            if not s["fravær"]:
                for inv, t in s["timer"].items():
                    if inv in inkl_navn and t > 0:
                        timer_inv[inv] = timer_inv.get(inv, 0) + t
    return timer_inv

def hent_ytd_snitt(token: str, aktuell_måned: int, år: int, inkl_navn: set) -> dict:
    måneder_data = []
    for m in range(1, aktuell_måned):
        uker = finn_uker_for_måned(m, år)
        t = hent_timer_for_uker(token, uker, inkl_navn)
        if t:
            måneder_data.append(t)
    if not måneder_data:
        return {}
    snitt: dict = {}
    for md in måneder_data:
        for inv, t in md.items():
            snitt[inv] = snitt.get(inv, 0) + t
    return {inv: t / len(måneder_data) for inv, t in snitt.items()}

def beregn_fordeling(total_kostnad: float, måned: int, år: int) -> dict:
    uker = finn_uker_for_måned(måned, år)
    inkl_inv = [i for i in les_investeringer() if i["kategori"] != "Annet"]
    inkl_navn = {i["navn"] for i in inkl_inv}
    inv_order = {i["navn"]: idx for idx, i in enumerate(inkl_inv)}

    with db() as con:
        brukere_rader = con.execute("SELECT token, navn, lønn, team FROM brukere ORDER BY navn").fetchall()

    # lønn=0 → telles som 1 (lik vekt)
    total_lønn = sum(max(b["lønn"] or 0, 1) for b in brukere_rader)

    personer = []
    for b in brukere_rader:
        lønn = max(b["lønn"] or 0, 1)
        andel = lønn / total_lønn
        kostnad_person = total_kostnad * andel
        team = b["team"] or "investering"

        if team == "investering":
            # Investeringsteam: kun inkluderte investeringer, YTD-fallback
            timer = hent_timer_for_uker(b["token"], uker, inkl_navn)
            brukt_ytd = False
            if sum(timer.values()) == 0:
                timer = hent_ytd_snitt(b["token"], måned, år, inkl_navn)
                brukt_ytd = bool(timer)
            total_timer = sum(timer.values())
            inv_kostnad_person: dict = {}
            if total_timer > 0:
                for inv, t in timer.items():
                    inv_kostnad_person[inv] = kostnad_person * (t / total_timer)
            personer.append({
                "token": b["token"], "navn": b["navn"], "lønn": b["lønn"] or 0,
                "team": team, "andel_prosent": round(andel * 100, 2),
                "kostnad_person": round(kostnad_person),
                "timer": timer, "total_timer": round(total_timer, 1),
                "annet_timer": 0, "annet_kostnad": 0,
                "inv_kostnad": inv_kostnad_person,
                "brukt_ytd": brukt_ytd, "brukt_team_nøkkel": False,
                "ingen_timer": total_timer == 0,
            })

        else:  # støtte
            # Hent ALLE timer inkl. Annet for å finne riktig proporsjon
            alle_timer = hent_alle_timer_for_uker(b["token"], uker)
            alle_total = sum(alle_timer.values())
            inkl_timer = {k: v for k, v in alle_timer.items() if k in inkl_navn}
            inkl_total = sum(inkl_timer.values())
            annet_total = alle_total - inkl_total

            inv_kostnad_person = {}
            annet_kostnad = 0.0

            if alle_total > 0:
                # Steg 1: direkte allokering fra investeringstimer
                for inv, t in inkl_timer.items():
                    inv_kostnad_person[inv] = kostnad_person * (t / alle_total)
                # Steg 2: resten (Annet-timer) → team-nøkkel i neste pass
                annet_kostnad = kostnad_person * (annet_total / alle_total)
            else:
                # Ingen timer i det hele tatt → alt til team-nøkkel
                annet_kostnad = kostnad_person

            personer.append({
                "token": b["token"], "navn": b["navn"], "lønn": b["lønn"] or 0,
                "team": team, "andel_prosent": round(andel * 100, 2),
                "kostnad_person": round(kostnad_person),
                "timer": inkl_timer, "total_timer": round(inkl_total, 1),
                "annet_timer": round(annet_total, 1), "annet_kostnad": annet_kostnad,
                "inv_kostnad": inv_kostnad_person,
                "brukt_ytd": False, "brukt_team_nøkkel": False,
                "ingen_timer": alle_total == 0,
            })

    # Bygg team-nøkkel fra alle direkte allokerte investeringskostnader
    inv_kr_direkte: dict = {}
    totalt_kr_direkte = 0.0
    for p in personer:
        for inv, kr in p["inv_kostnad"].items():
            inv_kr_direkte[inv] = inv_kr_direkte.get(inv, 0.0) + kr
            totalt_kr_direkte += kr

    team_nøkkel = {inv: kr / totalt_kr_direkte for inv, kr in inv_kr_direkte.items()} if totalt_kr_direkte > 0 else {}

    # Støtte: fordel annet_kostnad via team-nøkkel
    for p in personer:
        if p["team"] == "støtte" and p["annet_kostnad"] > 0 and team_nøkkel:
            for inv, nøkkel in team_nøkkel.items():
                p["inv_kostnad"][inv] = p["inv_kostnad"].get(inv, 0.0) + p["annet_kostnad"] * nøkkel
            p["brukt_team_nøkkel"] = True
            p["ingen_timer"] = False

    # Summer kostnad per investering på tvers av alle personer
    inv_kostnad_total: dict = {}
    for p in personer:
        for inv, kr in p["inv_kostnad"].items():
            inv_kostnad_total[inv] = inv_kostnad_total.get(inv, 0.0) + kr

    resultat = []
    for inv_navn in sorted(inv_kostnad_total.keys(), key=lambda n: inv_order.get(n, 999)):
        kr = inv_kostnad_total[inv_navn]
        kat = next((i["kategori"] for i in inkl_inv if i["navn"] == inv_navn), "")
        resultat.append({
            "investering": inv_navn,
            "kategori": kat,
            "prosent": round(kr / total_kostnad * 100, 2) if total_kostnad else 0,
            "kostnad": round(kr),
        })

    totalt_fordelt = sum(r["kostnad"] for r in resultat)

    return {
        "resultat": resultat,
        "total_lønn": total_lønn,
        "totalt_fordelt": totalt_fordelt,
        "total_kostnad": total_kostnad,
        "personer": personer,
        "måned": måned,
        "år": år,
        "måned_navn": MÅNEDS_NAVN[måned - 1],
    }

# ── Ruter ────────────────────────────────────────────────────────────────────

@app.get("/puls/{token}", response_class=HTMLResponse)
async def vis_skjema(request: Request, token: str,
                     uke: Optional[int] = Query(None),
                     år: Optional[int] = Query(None)):
    bruker = finn_bruker(token)
    if not bruker:
        return HTMLResponse("<h1 style='font-family:sans-serif;padding:40px'>Ugyldig eller utløpt lenke.</h1>", status_code=404)
    nå_uke, nå_år = get_uke_år()
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
    nå_uke, nå_år = get_uke_år()
    try:
        uke = int(form.get("_uke", nå_uke))
        år  = int(form.get("_år",  nå_år))
    except (ValueError, TypeError):
        uke, år = nå_uke, nå_år
    investeringer = les_inv_navn()
    timer = {}
    total = 0
    for inv in investeringer:
        v = min(40, max(0, int(form.get(f"t_{inv.replace(' ','_').replace('/','_')}", 0) or 0)))
        timer[inv] = v
        total += v
    fravar = form.get("_fravar") == "1"

    if fravar:
        upsert_svar(token, bruker["navn"], bruker["epost"], uke, år, True, {}, 0, datetime.now().isoformat())
        return RedirectResponse(f"/puls/{token}/takk?uke={uke}&år={år}&fravar=1", status_code=303)

    if total > 40:
        total = 40
    upsert_svar(token, bruker["navn"], bruker["epost"], uke, år, False, timer, total, datetime.now().isoformat())
    return RedirectResponse(f"/puls/{token}/takk?uke={uke}&år={år}", status_code=303)

@app.get("/puls/{token}/takk", response_class=HTMLResponse)
async def takk(request: Request, token: str,
               uke: Optional[int] = Query(None),
               år: Optional[int] = Query(None),
               fravar: Optional[int] = Query(None)):
    bruker = finn_bruker(token)
    if not bruker:
        return HTMLResponse("<h1>Ugyldig lenke</h1>", status_code=404)
    nå_uke, nå_år = get_uke_år()
    uke = uke if uke is not None else nå_uke
    år  = år  if år  is not None else nå_år
    hist = historikk_bruker(token, år)
    siste = next((s for s in reversed(hist) if s["uke"] == uke), None)
    mangler = manglende_uker(token, nå_uke, nå_år)
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

# ── Admin ────────────────────────────────────────────────────────────────────

ADMIN_COOKIE = "puls_admin"

def er_innlogget(request: Request) -> bool:
    token = request.cookies.get(ADMIN_COOKIE, "")
    return secrets.compare_digest(token, ADMIN_PASSWORD)

@app.get("/admin", response_class=HTMLResponse)
async def admin_get(request: Request):
    innlogget = er_innlogget(request)
    return render("admin.html",
        innlogget=innlogget,
        feil=False,
        melding=request.query_params.get("melding", ""),
        brukere=hent_alle_brukere() if innlogget else {},
        investeringer=les_investeringer() if innlogget else [],
    )

@app.post("/admin/login")
async def admin_login(request: Request):
    form = await request.form()
    passord = form.get("passord", "")
    if secrets.compare_digest(passord, ADMIN_PASSWORD):
        response = RedirectResponse("/admin", status_code=303)
        response.set_cookie(ADMIN_COOKIE, ADMIN_PASSWORD, httponly=True, samesite="lax")
        return response
    return render("admin.html", innlogget=False, feil=True, melding="", brukere={}, investeringer=[])

@app.get("/admin/logout")
async def admin_logout():
    response = RedirectResponse("/admin", status_code=303)
    response.delete_cookie(ADMIN_COOKIE)
    return response

@app.post("/admin/brukere/legg-til")
async def admin_legg_til_bruker(request: Request):
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    token = form.get("token", "").strip().lower()
    navn  = form.get("navn", "").strip()
    epost = form.get("epost", "").strip()
    if token and navn and epost:
        lagre_bruker(token, navn, epost)
        # Auto-opprett trivsel-token for alle åpne utsendelser inneværende år
        nå = datetime.now()
        with db() as con:
            åpne = con.execute(
                "SELECT id FROM trivsel_utsendelser WHERE år=? AND stengt=0", (nå.year,)
            ).fetchall()
            for u in åpne:
                eks = con.execute(
                    "SELECT 1 FROM trivsel_tokens WHERE utsendelse_id=? AND bruker_token=?",
                    (u["id"], token)
                ).fetchone()
                if not eks:
                    con.execute(
                        "INSERT INTO trivsel_tokens (survey_token, utsendelse_id, bruker_token) VALUES (?,?,?)",
                        (secrets.token_urlsafe(24), u["id"], token)
                    )
    return RedirectResponse(f"/admin?melding={navn}+lagt+til", status_code=303)

@app.post("/admin/brukere/fjern")
async def admin_fjern_bruker(request: Request):
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    token = form.get("token", "").strip()
    navn = fjern_bruker(token)
    return RedirectResponse(f"/admin?melding={navn}+fjernet", status_code=303)



@app.post("/admin/brukere/sett-aktiv")
async def admin_sett_aktiv(request: Request):
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    token = form.get("token", "").strip()
    aktiv = int(form.get("aktiv", "1"))
    sett_aktiv_bruker(token, aktiv)
    with db() as con:
        navn = con.execute("SELECT navn FROM brukere WHERE token=?", (token,)).fetchone()
    navn = navn["navn"] if navn else token
    status = "aktivert" if aktiv else "deaktivert"
    return RedirectResponse(f"/admin?melding={navn}+{status}", status_code=303)

@app.post("/admin/investeringer/legg-til")
async def admin_legg_til_inv(request: Request):
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    navn     = form.get("navn", "").strip()
    kategori = form.get("kategori", "Annet").strip()
    if navn:
        inv = les_investeringer()
        if navn not in [i["navn"] for i in inv]:
            inv.append({"navn": navn, "kategori": kategori})
            lagre_investeringer(inv)
    return RedirectResponse(f"/admin?melding={navn}+lagt+til", status_code=303)

@app.post("/admin/investeringer/fjern")
async def admin_fjern_inv(request: Request):
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    navn = form.get("navn", "").strip()
    lagre_investeringer([i for i in les_investeringer() if i["navn"] != navn])
    return RedirectResponse(f"/admin?melding={navn}+fjernet", status_code=303)

@app.post("/admin/brukere/sett-team")
async def admin_sett_team(request: Request):
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    token = form.get("token", "").strip()
    team = form.get("team", "investering").strip()
    if team not in ("investering", "støtte"):
        team = "investering"
    sett_team_bruker(token, team)
    return RedirectResponse("/admin?melding=Team+oppdatert", status_code=303)

@app.post("/admin/brukere/sett-lønn")
async def admin_sett_lønn(request: Request):
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    token = form.get("token", "").strip()
    raw = form.get("lønn", "0").replace(" ", "").replace("\u00a0", "").replace(",", "").replace(".", "") or "0"
    try:
        lønn = int(raw)
    except ValueError:
        lønn = 0
    sett_lønn_bruker(token, lønn)
    return RedirectResponse("/admin?melding=Lønn+oppdatert", status_code=303)

@app.post("/admin/investeringer/reorder")
async def admin_reorder_inv(request: Request):
    if not er_innlogget(request):
        return JSONResponse({"ok": False}, status_code=401)
    data = await request.json()
    lagre_investeringer(data)
    return JSONResponse({"ok": True})

@app.post("/admin/investeringer/endre-kategori")
async def admin_endre_kategori(request: Request):
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    navn     = form.get("navn", "").strip()
    kategori = form.get("kategori", "Annet").strip()
    inv = les_investeringer()
    for i in inv:
        if i["navn"] == navn:
            i["kategori"] = kategori
            break
    lagre_investeringer(inv)
    return RedirectResponse(f"/admin?melding={navn}+oppdatert", status_code=303)


@app.get("/api/brukere")
async def api_brukere(request: Request, key: Optional[str] = Query(default=None)):
    api_key_ok = EXPORT_API_KEY and key and secrets.compare_digest(key, EXPORT_API_KEY)
    if not api_key_ok:
        return JSONResponse({"error": "Ikke autorisert"}, status_code=403)
    base = str(request.base_url).rstrip("/")
    with db() as con:
        rader = con.execute("SELECT token, navn, epost FROM brukere ORDER BY navn").fetchall()
    return JSONResponse([{
        "token": r["token"],
        "navn":  r["navn"],
        "epost": r["epost"],
        "link":  f"{base}/puls/{r['token']}",
    } for r in rader])

@app.get("/admin/fordeling", response_class=HTMLResponse)
async def admin_fordeling_get(request: Request,
                               total_kostnad: Optional[float] = Query(None),
                               måned: Optional[int] = Query(None),
                               år: Optional[int] = Query(None)):
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    beregning = None
    feil = None
    if total_kostnad is not None and måned is not None and år is not None:
        try:
            beregning = beregn_fordeling(total_kostnad, måned, år)
        except Exception as e:
            feil = str(e)
    today = date.today()
    return render("fordeling.html",
        beregning=beregning,
        feil=feil,
        default_måned=måned or today.month,
        default_år=år or today.year,
        default_kostnad=total_kostnad or "",
        måneder=list(enumerate(MÅNEDS_NAVN, 1)),
    )

@app.get("/admin/fordeling/eksport.csv")
async def admin_fordeling_eksport(request: Request,
                                   total_kostnad: float = Query(...),
                                   måned: int = Query(...),
                                   år: int = Query(...)):
    if not er_innlogget(request):
        return HTMLResponse("Ikke innlogget", status_code=403)
    beregning = beregn_fordeling(total_kostnad, måned, år)
    dato_str = date(år, måned, 1).strftime("%Y-%m-%d")
    linjer = ["investering,dato,sum,kommentar"]
    for rad in beregning["resultat"]:
        kommentar = f"{rad['prosent']}% av total"
        linjer.append(f'"{rad["investering"]}",{dato_str},{rad["kostnad"]},"{kommentar}"')
    csv_data = "\n".join(linjer)
    return Response(
        content=csv_data.encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename=fordeling_{år}-{måned:02d}.csv'},
    )

@app.get("/admin/eksport.csv")
async def eksport_csv(request: Request, key: Optional[str] = Query(default=None)):
    api_key_ok = EXPORT_API_KEY and key and secrets.compare_digest(key, EXPORT_API_KEY)
    if not api_key_ok and not er_innlogget(request):
        return HTMLResponse("Ikke innlogget", status_code=403)
    with db() as con:
        rader = con.execute("SELECT * FROM svar ORDER BY år, uke, navn").fetchall()
    linjer = ["Navn,Epost,Uke,År,Investering,Timer,Tidspunkt"]
    for r in rader:
        if r["fravar"]:
            linjer.append(f'{r["navn"]},{r["epost"]},{r["uke"]},{r["år"]},Fravær,0,{r["tidspunkt"]}')
        else:
            timer = json.loads(r["timer"])
            for inv, t in timer.items():
                linjer.append(f'{r["navn"]},{r["epost"]},{r["uke"]},{r["år"]},{inv},{t},{r["tidspunkt"]}')
    csv_data = "\n".join(linjer)
    return Response(
        content=csv_data.encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=puls_eksport.csv"},
    )


# ── Trivsel ───────────────────────────────────────────────────────────────────

TRIVSEL_MIN_SVAR = 3

def trivsel_er_stengt(u) -> bool:
    if u["stengt"]:
        return True
    try:
        åpnet = datetime.fromisoformat(u["opprettet"])
        return datetime.now() > åpnet + timedelta(days=u["åpen_dager"])
    except Exception:
        return False

def trivsel_opprett_utsendelse(år: int, måned: int) -> tuple[int, list]:
    """Oppretter utsendelse + survey_tokens for alle aktive brukere. Idempotent."""
    with db() as con:
        rad = con.execute(
            "SELECT id FROM trivsel_utsendelser WHERE år=? AND måned=?", (år, måned)
        ).fetchone()
        if rad:
            uid = rad["id"]
        else:
            con.execute(
                "INSERT INTO trivsel_utsendelser (måned, år, opprettet) VALUES (?,?,?)",
                (måned, år, datetime.now().isoformat())
            )
            uid = con.execute("SELECT last_insert_rowid()").fetchone()[0]

        brukere_rader = con.execute("SELECT token, navn, epost FROM brukere WHERE aktiv=1").fetchall()
        resultat = []
        for b in brukere_rader:
            eks = con.execute(
                "SELECT survey_token FROM trivsel_tokens WHERE utsendelse_id=? AND bruker_token=?",
                (uid, b["token"])
            ).fetchone()
            if eks:
                stok = eks["survey_token"]
            else:
                stok = secrets.token_urlsafe(24)
                con.execute(
                    "INSERT INTO trivsel_tokens (survey_token, utsendelse_id, bruker_token) VALUES (?,?,?)",
                    (stok, uid, b["token"])
                )
            resultat.append({"navn": b["navn"], "epost": b["epost"], "survey_token": stok})
    return uid, resultat

def trivsel_hent_resultater(utsendelse_id: int) -> dict:
    with db() as con:
        rader = con.execute(
            "SELECT trivsel, samarbeid FROM trivsel_svar WHERE utsendelse_id=?",
            (utsendelse_id,)
        ).fetchall()
    antall = len(rader)
    if antall < TRIVSEL_MIN_SVAR:
        return {"antall": antall, "skjult": True}
    snitt_t = sum(r["trivsel"]   for r in rader) / antall
    snitt_s = sum(r["samarbeid"] for r in rader) / antall
    dist_t = {i: 0 for i in range(1, 8)}
    dist_s = {i: 0 for i in range(1, 8)}
    for r in rader:
        dist_t[r["trivsel"]]   += 1
        dist_s[r["samarbeid"]] += 1
    return {
        "antall": antall,
        "skjult": False,
        "snitt_trivsel":   round(snitt_t, 1),
        "snitt_samarbeid": round(snitt_s, 1),
        "dist_trivsel":    dist_t,
        "dist_samarbeid":  dist_s,
    }

@app.get("/trivsel/takk", response_class=HTMLResponse)
async def trivsel_takk_get():
    return render("trivsel_takk.html")

@app.get("/trivsel/allerede-svart", response_class=HTMLResponse)
async def trivsel_allerede_svart_get():
    return render("trivsel_allerede_svart.html", måned="", år="")

@app.get("/trivsel/{survey_token}", response_class=HTMLResponse)
async def trivsel_vis_skjema(survey_token: str):
    with db() as con:
        row = con.execute("""
            SELECT tt.id, tt.brukt, tt.utsendelse_id,
                   tu.måned, tu.år, tu.stengt, tu.åpen_dager, tu.opprettet
            FROM trivsel_tokens tt
            JOIN trivsel_utsendelser tu ON tu.id = tt.utsendelse_id
            WHERE tt.survey_token = ?
        """, (survey_token,)).fetchone()
    if not row:
        return render("trivsel_feil.html", melding="Lenken er ugyldig eller utløpt.")
    måned_navn = MÅNEDS_NAVN[row["måned"] - 1]
    if trivsel_er_stengt(row):
        return render("trivsel_stengt.html", måned=måned_navn, år=row["år"])
    if row["brukt"]:
        return render("trivsel_allerede_svart.html", måned=måned_navn, år=row["år"])
    return render("trivsel_svar.html",
                  survey_token=survey_token,
                  måned=måned_navn, år=row["år"],
                  forhåndsvis=False)

@app.post("/trivsel/{survey_token}")
async def trivsel_send_svar(survey_token: str, request: Request):
    form = await request.form()
    try:
        trivsel_score   = int(form.get("trivsel",   0))
        samarbeid_score = int(form.get("samarbeid", 0))
    except (TypeError, ValueError):
        from fastapi import HTTPException as _H
        raise _H(400, "Ugyldig input")
    if not (1 <= trivsel_score <= 7 and 1 <= samarbeid_score <= 7):
        from fastapi import HTTPException as _H
        raise _H(400, "Score må være mellom 1 og 7")
    with db() as con:
        tok = con.execute(
            "SELECT id, brukt, utsendelse_id FROM trivsel_tokens WHERE survey_token=?",
            (survey_token,)
        ).fetchone()
        if not tok:
            from fastapi import HTTPException as _H
            raise _H(404)
        if tok["brukt"]:
            return RedirectResponse("/trivsel/allerede-svart", status_code=303)
        con.execute("UPDATE trivsel_tokens SET brukt=1 WHERE survey_token=?", (survey_token,))
        con.execute(
            "INSERT INTO trivsel_svar (utsendelse_id, trivsel, samarbeid, innsendt) VALUES (?,?,?,?)",
            (tok["utsendelse_id"], trivsel_score, samarbeid_score, datetime.now().isoformat())
        )
    return RedirectResponse("/trivsel/takk", status_code=303)

@app.get("/admin/trivsel", response_class=HTMLResponse)
async def admin_trivsel(request: Request):
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    nå = datetime.now()

    # Auto-opprett alle måneder Jan–nåværende for inneværende år
    for m in range(1, nå.month + 1):
        trivsel_opprett_utsendelse(nå.year, m)
        # Auto-steng måneder eldre enn 10 dager etter siste dag
        with db() as con:
            u = con.execute(
                "SELECT id, stengt, opprettet, åpen_dager FROM trivsel_utsendelser WHERE år=? AND måned=?",
                (nå.year, m)
            ).fetchone()
            if u and not u["stengt"] and m < nå.month:
                # Steng foregående måneder automatisk hvis de er eldre
                try:
                    åpnet = datetime.fromisoformat(u["opprettet"])
                    if nå > åpnet + timedelta(days=u["åpen_dager"]):
                        con.execute("UPDATE trivsel_utsendelser SET stengt=1 WHERE id=?", (u["id"],))
                except Exception:
                    pass

    with db() as con:
        utsendelser = con.execute(
            "SELECT * FROM trivsel_utsendelser ORDER BY år DESC, måned DESC"
        ).fetchall()
        antall_brukere = con.execute("SELECT COUNT(*) FROM brukere").fetchone()[0]

    måneder_data = []
    for u in utsendelser:
        res = trivsel_hent_resultater(u["id"])
        with db() as con:
            totalt = con.execute("SELECT COUNT(*) FROM trivsel_tokens WHERE utsendelse_id=?", (u["id"],)).fetchone()[0]
            svart  = con.execute("SELECT COUNT(*) FROM trivsel_tokens WHERE utsendelse_id=? AND brukt=1", (u["id"],)).fetchone()[0]
        stengt_nå = trivsel_er_stengt(u)
        måneder_data.append({
            "id":       u["id"],
            "måned":    MÅNEDS_NAVN[u["måned"] - 1],
            "måned_nr": u["måned"],
            "år":       u["år"],
            "totalt":   totalt,
            "svart":    svart,
            "pst":      round(svart / totalt * 100) if totalt else 0,
            "res":      res,
            "stengt":   stengt_nå,
        })

    return render("admin_trivsel.html",
                  måneder=måneder_data,
                  antall_brukere=antall_brukere,
                  default_måned=nå.month,
                  default_år=nå.year,
                  månednavn=MÅNEDS_NAVN,
                  min_svar=TRIVSEL_MIN_SVAR,
                  melding=request.query_params.get("melding", ""))

@app.post("/admin/trivsel/start")
async def admin_trivsel_start(request: Request):
    """Start eller gjenåpne en trivsel-runde for valgt måned/år."""
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    try:
        år   = int(form.get("år",   0))
        måned = int(form.get("måned", 0))
    except (ValueError, TypeError):
        return RedirectResponse("/admin/trivsel?melding=Ugyldig+dato", status_code=303)
    if not (1 <= måned <= 12 and 2020 <= år <= 2035):
        return RedirectResponse("/admin/trivsel?melding=Ugyldig+dato", status_code=303)
    trivsel_opprett_utsendelse(år, måned)
    return RedirectResponse(f"/admin/trivsel/lenker/{år}/{måned}", status_code=303)

@app.post("/admin/trivsel/steng/{uid}")
async def admin_trivsel_steng(uid: int, request: Request):
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    with db() as con:
        con.execute("UPDATE trivsel_utsendelser SET stengt=1 WHERE id=?", (uid,))
    return RedirectResponse("/admin/trivsel?melding=Periode+stengt", status_code=303)

@app.get("/admin/trivsel/forhåndsvis", response_class=HTMLResponse)
async def admin_trivsel_preview(request: Request):
    """Vis skjemaet slik brukerne ser det — uten å registrere svar."""
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    nå = datetime.now()
    return render("trivsel_svar.html",
                  survey_token="__PREVIEW__",
                  måned=MÅNEDS_NAVN[nå.month - 1],
                  år=nå.year,
                  forhåndsvis=True)

@app.post("/admin/trivsel/testdata/{uid}")
async def admin_trivsel_testdata(uid: int, request: Request):
    """Generer 5 tilfeldige testsvar for en utsendelse (vises som demo-data)."""
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    import random as _r
    with db() as con:
        u = con.execute("SELECT id, måned, år FROM trivsel_utsendelser WHERE id=?", (uid,)).fetchone()
        if not u:
            return RedirectResponse("/admin/trivsel", status_code=303)
        # Hent tokens som ikke er brukt
        ubrukte = con.execute(
            "SELECT survey_token, bruker_token FROM trivsel_tokens WHERE utsendelse_id=? AND brukt=0",
            (uid,)
        ).fetchall()
        antall = min(5, len(ubrukte))
        valgte = _r.sample(list(ubrukte), antall)
        for t in valgte:
            trivsel_score   = _r.randint(4, 7)
            samarbeid_score = _r.randint(4, 7)
            con.execute("UPDATE trivsel_tokens SET brukt=1 WHERE survey_token=?", (t["survey_token"],))
            con.execute(
                "INSERT INTO trivsel_svar (utsendelse_id, trivsel, samarbeid, innsendt) VALUES (?,?,?,?)",
                (uid, trivsel_score, samarbeid_score, datetime.now().isoformat())
            )
    return RedirectResponse(f"/admin/trivsel?melding={antall}+testsvar+lagt+inn", status_code=303)

@app.post("/admin/trivsel/nullstill-svar")
async def admin_trivsel_nullstill(request: Request):
    """Nullstill én persons svar — for testing."""
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    form = await request.form()
    survey_token = (form.get("survey_token") or "").strip()
    year  = int(form.get("year",  0) or 0)
    month = int(form.get("month", 0) or 0)
    if survey_token:
        with db() as con:
            tok = con.execute(
                "SELECT id, utsendelse_id FROM trivsel_tokens WHERE survey_token=? AND brukt=1",
                (survey_token,)
            ).fetchone()
            if tok:
                con.execute("""
                    DELETE FROM trivsel_svar WHERE id = (
                        SELECT id FROM trivsel_svar
                        WHERE utsendelse_id=? ORDER BY id DESC LIMIT 1
                    )
                """, (tok["utsendelse_id"],))
                con.execute("UPDATE trivsel_tokens SET brukt=0 WHERE id=?", (tok["id"],))
    return RedirectResponse(f"/admin/trivsel/lenker/{year}/{month}", status_code=303)

@app.get("/admin/trivsel/lenker/{year}/{month}", response_class=HTMLResponse)
async def admin_trivsel_lenker(year: int, month: int, request: Request):
    if not er_innlogget(request):
        return RedirectResponse("/admin", status_code=303)
    trivsel_opprett_utsendelse(year, month)  # idempotent — sikrer tokens finnes
    with db() as con:
        u = con.execute(
            "SELECT id FROM trivsel_utsendelser WHERE år=? AND måned=?", (year, month)
        ).fetchone()
        rader = con.execute("""
            SELECT b.navn, b.epost, tt.survey_token, tt.brukt
            FROM trivsel_tokens tt
            JOIN brukere b ON b.token = tt.bruker_token
            WHERE tt.utsendelse_id = ?
            ORDER BY tt.brukt ASC, b.navn
        """, (u["id"],)).fetchall()
    base = str(request.base_url).rstrip("/")
    lenker = [
        {
            "navn":         r["navn"],
            "epost":        r["epost"],
            "link":         f"{base}/trivsel/{r['survey_token']}",
            "survey_token": r["survey_token"],
            "brukt":        bool(r["brukt"]),
        }
        for r in rader
    ]
    svart = sum(1 for l in lenker if l["brukt"])
    return render("trivsel_lenker.html",
                  lenker=lenker,
                  måned=MÅNEDS_NAVN[month - 1],
                  måned_nr=month,
                  år=year,
                  svart=svart)

@app.get("/api/trivsel/lenker/{year}/{month}")
async def api_trivsel_lenker(year: int, month: int, request: Request):
    """Power Automate: hent survey-lenker for utsending via e-post."""
    key = request.query_params.get("api_key") or request.headers.get("x-api-key", "")
    if not (EXPORT_API_KEY and secrets.compare_digest(key, EXPORT_API_KEY)):
        from fastapi import HTTPException as _H
        raise _H(401, "Ugyldig API-nøkkel")
    _, tokens = trivsel_opprett_utsendelse(year, month)
    base = str(request.base_url).rstrip("/")
    return JSONResponse([
        {"navn": t["navn"], "epost": t["epost"], "link": f"{base}/trivsel/{t['survey_token']}"}
        for t in tokens
    ])


@app.get("/api/trivsel/eksport.csv")
async def api_trivsel_eksport(request: Request):
    """Power BI: eksporter alle trivsel-svar som CSV."""
    key = request.query_params.get("key") or request.headers.get("x-api-key", "")
    if not (EXPORT_API_KEY and secrets.compare_digest(key, EXPORT_API_KEY)):
        from fastapi import HTTPException as _H
        raise _H(401, "Ugyldig API-nøkkel")
    with db() as con:
        rader = con.execute("""
            SELECT u.år, u.måned, ts.trivsel, ts.samarbeid, ts.innsendt
            FROM trivsel_svar ts
            JOIN trivsel_utsendelser u ON ts.utsendelse_id = u.id
            ORDER BY u.år, u.måned, ts.innsendt
        """).fetchall()
    lines = ["År,Måned,Trivsel,Samarbeid,Innsendt"]
    for r in rader:
        lines.append(f"{r[0]},{r[1]},{r[2]},{r[3]},{r[4]}")
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines), media_type="text/csv; charset=utf-8")

@app.get("/admin/migrering-trivsel-historikk")
async def admin_mig_trivsel(request: Request):
    if not er_innlogget(request):
        return JSONResponse({"feil": "ikke innlogget"}, status_code=401)
    import base64 as _b64, json as _j
    DATA_B64 = "eyJ1dHNlbmRlbHNlciI6IFtbMTMsIDUsIDIwMjMsICIyMDIzLTA1LTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbNSwgMTEsIDIwMjMsICIyMDIzLTExLTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbNiwgMTIsIDIwMjMsICIyMDIzLTEyLTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbNywgMSwgMjAyNCwgIjIwMjQtMDEtMDFUMDA6MDA6MDAiLCAxMCwgMV0sIFs4LCAyLCAyMDI0LCAiMjAyNC0wMi0wMVQwMDowMDowMCIsIDEwLCAxXSwgWzksIDMsIDIwMjQsICIyMDI0LTAzLTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMTAsIDQsIDIwMjQsICIyMDI0LTA0LTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMTEsIDUsIDIwMjQsICIyMDI0LTA1LTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMTIsIDYsIDIwMjQsICIyMDI0LTA2LTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMTQsIDgsIDIwMjQsICIyMDI0LTA4LTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMTUsIDksIDIwMjQsICIyMDI0LTA5LTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMTYsIDEwLCAyMDI0LCAiMjAyNC0xMC0wMVQwMDowMDowMCIsIDEwLCAxXSwgWzE3LCAxMSwgMjAyNCwgIjIwMjQtMTEtMDFUMDA6MDA6MDAiLCAxMCwgMV0sIFsxOCwgMTIsIDIwMjQsICIyMDI0LTEyLTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMTksIDEsIDIwMjUsICIyMDI1LTAxLTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMjAsIDIsIDIwMjUsICIyMDI1LTAyLTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMjEsIDMsIDIwMjUsICIyMDI1LTAzLTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMjIsIDQsIDIwMjUsICIyMDI1LTA0LTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMjMsIDUsIDIwMjUsICIyMDI1LTA1LTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMjQsIDYsIDIwMjUsICIyMDI1LTA2LTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMjUsIDcsIDIwMjUsICIyMDI1LTA3LTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMjYsIDgsIDIwMjUsICIyMDI1LTA4LTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMjcsIDksIDIwMjUsICIyMDI1LTA5LTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMjgsIDEwLCAyMDI1LCAiMjAyNS0xMC0wMVQwMDowMDowMCIsIDEwLCAxXSwgWzI5LCAxMSwgMjAyNSwgIjIwMjUtMTEtMDFUMDA6MDA6MDAiLCAxMCwgMV0sIFszMCwgMTIsIDIwMjUsICIyMDI1LTEyLTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMzIsIDEsIDIwMjYsICIyMDI2LTAxLTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMzMsIDIsIDIwMjYsICIyMDI2LTAyLTAxVDAwOjAwOjAwIiwgMTAsIDFdLCBbMzQsIDMsIDIwMjYsICIyMDI2LTAzLTAxVDAwOjAwOjAwIiwgMTAsIDFdXSwgInN2YXIiOiBbWzUsIDcsIDUsICIxOS4xMS4yMDIzIDE0OjE3OjU2Il0sIFs1LCA2LCA1LCAiMTcuMTEuMjAyMyAxMjoxNDowMSJdLCBbNSwgNSwgNSwgIjIwLjExLjIwMjMgMTE6NDQ6MzkiXSwgWzUsIDcsIDYsICIxNy4xMS4yMDIzIDEyOjE5OjI3Il0sIFs1LCA2LCA1LCAiMTcuMTEuMjAyMyAxMjo1MjoyNyJdLCBbNSwgNiwgNiwgIjE3LjExLjIwMjMgMTM6MDA6NDMiXSwgWzUsIDQsIDQsICIxNy4xMS4yMDIzIDEyOjM3OjA5Il0sIFs1LCA3LCA2LCAiMjAuMTEuMjAyMyAwNzoxNTo0MiJdLCBbNSwgNSwgNSwgIjE3LjExLjIwMjMgMTc6NTY6MTciXSwgWzUsIDcsIDYsICIxNy4xMS4yMDIzIDE0OjMzOjU0Il0sIFs1LCA0LCA1LCAiMTcuMTEuMjAyMyAxMjoyMDo0MCJdLCBbNSwgNywgNiwgIjE3LjExLjIwMjMgMTI6NTI6MjgiXSwgWzUsIDYsIDQsICIxNy4xMS4yMDIzIDE0OjA1OjM2Il0sIFs1LCA2LCA1LCAiMTcuMTEuMjAyMyAxMjo0MToxOCJdLCBbNSwgMiwgNCwgIjIwLjExLjIwMjMgMTY6Mjg6MzAiXSwgWzUsIDcsIDcsICIxNy4xMS4yMDIzIDE2OjA3OjEzIl0sIFs2LCA1LCA0LCAiMjIuMTIuMjAyMyAxMTozMTozMyJdLCBbNiwgNiwgNiwgIjIyLjEyLjIwMjMgMTE6MDg6NTQiXSwgWzYsIDYsIDQsICIwMi4wMS4yMDI0IDEwOjA2OjEwIl0sIFs2LCA2LCA1LCAiMjIuMTIuMjAyMyAxMDoxODoyMSJdLCBbNiwgNywgNywgIjIyLjEyLjIwMjMgMTA6NTE6NDEiXSwgWzYsIDYsIDYsICIyMi4xMi4yMDIzIDEwOjIyOjM4Il0sIFs2LCA3LCA3LCAiMjIuMTIuMjAyMyAxMDoxMzozOSJdLCBbNiwgNywgNSwgIjI3LjEyLjIwMjMgMDc6MzY6NDgiXSwgWzYsIDcsIDYsICIyMi4xMi4yMDIzIDEyOjQ1OjQwIl0sIFs2LCA1LCAzLCAiMjIuMTIuMjAyMyAxMTo0NTo1NSJdLCBbNiwgNSwgNiwgIjIyLjEyLjIwMjMgMTI6NDA6MTgiXSwgWzYsIDMsIDUsICIyMy4xMi4yMDIzIDA4OjI1OjQzIl0sIFs3LCAzLCAzLCAiMjYuMDEuMjAyNCAxNzoxNDoxNCJdLCBbNywgNywgNiwgIjMxLjAxLjIwMjQgMTI6NDU6MzAiXSwgWzcsIDcsIDYsICIyOS4wMS4yMDI0IDE1OjIyOjMyIl0sIFs3LCA2LCA2LCAiMjguMDEuMjAyNCAyMDoyMjo1MCJdLCBbNywgNSwgMywgIjI2LjAxLjIwMjQgMTQ6MDM6MTMiXSwgWzcsIDYsIDYsICIyNi4wMS4yMDI0IDE3OjQzOjQyIl0sIFs3LCA3LCA3LCAiMjYuMDEuMjAyNCAxNDozMjoyMiJdLCBbNywgNiwgNCwgIjI2LjAxLjIwMjQgMTM6MDM6MTciXSwgWzcsIDYsIDYsICIyNi4wMS4yMDI0IDE1OjM5OjA4Il0sIFs3LCA0LCA0LCAiMjYuMDEuMjAyNCAxMjo1MDozNiJdLCBbNywgNywgNSwgIjI2LjAxLjIwMjQgMTQ6NTQ6MDciXSwgWzcsIDYsIDUsICIyNi4wMS4yMDI0IDEzOjE1OjQ5Il0sIFs3LCA2LCA1LCAiMjYuMDEuMjAyNCAxMzoyNjowOCJdLCBbNywgNCwgNSwgIjI2LjAxLjIwMjQgMTI6NTY6NDgiXSwgWzcsIDcsIDQsICIyOS4wMS4yMDI0IDEwOjQ0OjUxIl0sIFs3LCA1LCA1LCAiMjYuMDEuMjAyNCAxNDozMToxOSJdLCBbNywgNywgNywgIjI2LjAxLjIwMjQgMTI6NTA6MjAiXSwgWzcsIDYsIDYsICIzMS4wMS4yMDI0IDEzOjQwOjU4Il0sIFs3LCA2LCA2LCAiMjYuMDEuMjAyNCAxMjo1MzoxMSJdLCBbOCwgNSwgNSwgIjI1LjAyLjIwMjQgMTk6MDA6NDYiXSwgWzgsIDQsIDQsICIyNi4wMi4yMDI0IDA4OjAxOjA1Il0sIFs4LCA2LCAzLCAiMjYuMDIuMjAyNCAwOTo1Mzo0OCJdLCBbOCwgNiwgNSwgIjI2LjAyLjIwMjQgMjI6MTU6MDkiXSwgWzgsIDYsIDYsICIyNi4wMi4yMDI0IDA5OjA3OjA2Il0sIFs4LCAzLCA0LCAiMjYuMDIuMjAyNCAxMToyNzoyMSJdLCBbOCwgNywgNywgIjI2LjAyLjIwMjQgMDg6MjE6NDMiXSwgWzgsIDUsIDUsICIwMS4wMy4yMDI0IDEzOjMxOjMyIl0sIFs4LCA3LCA1LCAiMjUuMDIuMjAyNCAxOToyNjo1NCJdLCBbOCwgNiwgNiwgIjAxLjAzLjIwMjQgMTM6MTc6MzkiXSwgWzgsIDYsIDYsICIyNi4wMi4yMDI0IDE1OjQ4OjEwIl0sIFs4LCAzLCA0LCAiMjYuMDIuMjAyNCAwODo0MDozMiJdLCBbOCwgNSwgNCwgIjI2LjAyLjIwMjQgMDk6Mjc6NDYiXSwgWzgsIDcsIDYsICIyNi4wMi4yMDI0IDEwOjU4OjMxIl0sIFs4LCA2LCA2LCAiMjYuMDIuMjAyNCAwODo1Mzo1MiJdLCBbOCwgNiwgNSwgIjI2LjAyLjIwMjQgMDg6NTY6MjYiXSwgWzgsIDcsIDcsICIyNS4wMi4yMDI0IDE4OjM2OjM1Il0sIFs4LCA1LCA1LCAiMjUuMDIuMjAyNCAyMjowMjowNCJdLCBbOCwgNiwgNSwgIjI1LjAyLjIwMjQgMTk6MTM6MDIiXSwgWzgsIDcsIDcsICIyOS4wMi4yMDI0IDA4OjUzOjAwIl0sIFs5LCA1LCA2LCAiMTUuMDMuMjAyNCAwODoxOTowNiJdLCBbOSwgNCwgNCwgIjE1LjAzLjIwMjQgMDg6MTk6MDgiXSwgWzksIDcsIDYsICIxNS4wMy4yMDI0IDA4OjE3OjU2Il0sIFs5LCA2LCA2LCAiMTUuMDMuMjAyNCAwODoxMzoxMyJdLCBbOSwgNiwgNiwgIjA4LjAzLjIwMjQgMDk6Mjk6MTYiXSwgWzksIDcsIDQsICIwOC4wMy4yMDI0IDExOjI0OjQ1Il0sIFs5LCA3LCA3LCAiMDEuMDMuMjAyNCAxMDo1MjozNiJdLCBbOSwgNiwgNiwgIjAxLjAzLjIwMjQgMTA6MzE6NDgiXSwgWzksIDYsIDUsICIwMS4wMy4yMDI0IDEwOjM3OjU5Il0sIFs5LCA3LCA2LCAiMjIuMDMuMjAyNCAxMToyNzozNyJdLCBbOSwgNywgNywgIjIyLjAzLjIwMjQgMTE6MjY6NDciXSwgWzksIDYsIDYsICIwMS4wMy4yMDI0IDEwOjMzOjU3Il0sIFs5LCAzLCAzLCAiMjIuMDMuMjAyNCAxMjo0NTowMCJdLCBbOSwgNywgNiwgIjExLjAzLjIwMjQgMDY6NTM6MDYiXSwgWzksIDcsIDcsICIxNS4wMy4yMDI0IDEyOjA4OjU3Il0sIFs5LCA2LCA2LCAiMDguMDMuMjAyNCAxMzowOToxMSJdLCBbOSwgNywgMywgIjE5LjAzLjIwMjQgMDQ6NDM6MjEiXSwgWzksIDcsIDcsICIwMy4wMy4yMDI0IDE3OjA3OjUxIl0sIFs5LCA2LCA2LCAiMDguMDMuMjAyNCAxMToxMjoxNiJdLCBbOSwgMywgMiwgIjIyLjAzLjIwMjQgMTE6MjE6NDAiXSwgWzEwLCA1LCA1LCAiMjkuMDQuMjAyNCAxMzowOTowNSJdLCBbMTAsIDUsIDYsICIyOS4wNC4yMDI0IDA4OjMxOjE5Il0sIFsxMCwgNywgNywgIjI5LjA0LjIwMjQgMDg6NDY6MzMiXSwgWzEwLCAzLCAzLCAiMjkuMDQuMjAyNCAxNToxMzozNyJdLCBbMTAsIDcsIDYsICIyOS4wNC4yMDI0IDEzOjAwOjA3Il0sIFsxMCwgNywgNywgIjI5LjA0LjIwMjQgMDg6MjI6MzgiXSwgWzEwLCA3LCA3LCAiMDIuMDUuMjAyNCAxNjowMTozMyJdLCBbMTAsIDcsIDYsICIyOS4wNC4yMDI0IDExOjA3OjI1Il0sIFsxMCwgNiwgNiwgIjI5LjA0LjIwMjQgMDg6MDM6NTkiXSwgWzEwLCA2LCA2LCAiMjkuMDQuMjAyNCAwOToxMToyMiJdLCBbMTAsIDYsIDYsICIyOS4wNC4yMDI0IDEzOjIyOjMwIl0sIFsxMCwgNiwgNSwgIjI5LjA0LjIwMjQgMTQ6MzA6MzQiXSwgWzEwLCA2LCA2LCAiMDIuMDUuMjAyNCAxMjozNjozMSJdLCBbMTAsIDYsIDUsICIwMi4wNS4yMDI0IDIzOjI3OjQ3Il0sIFsxMCwgNywgNywgIjI5LjA0LjIwMjQgMTU6NDk6MzgiXSwgWzEwLCA2LCA2LCAiMDIuMDUuMjAyNCAwOToyMTowMyJdLCBbMTEsIDYsIDYsICIzMS4wNS4yMDI0IDEwOjE3OjExIl0sIFsxMSwgNywgNiwgIjMxLjA1LjIwMjQgMTA6NDU6MzMiXSwgWzExLCA2LCA2LCAiMzEuMDUuMjAyNCAxNDoxMDoyMiJdLCBbMTEsIDYsIDYsICIzMS4wNS4yMDI0IDEwOjE0OjQ5Il0sIFsxMSwgNywgNiwgIjMxLjA1LjIwMjQgMTA6Mjk6MTAiXSwgWzExLCA1LCA2LCAiMzEuMDUuMjAyNCAxMDozMTozMCJdLCBbMTEsIDUsIDQsICIzMS4wNS4yMDI0IDEwOjE4OjU4Il0sIFsxMSwgNywgNywgIjMxLjA1LjIwMjQgMTA6MjQ6MjEiXSwgWzExLCA1LCA1LCAiMzEuMDUuMjAyNCAxMDoxMjoxOCJdLCBbMTEsIDcsIDUsICIzMS4wNS4yMDI0IDEwOjI2OjAzIl0sIFsxMSwgNiwgNiwgIjAzLjA2LjIwMjQgMDg6MTQ6NTciXSwgWzExLCA3LCA1LCAiMzEuMDUuMjAyNCAxMDozMTozMiJdLCBbMTEsIDcsIDcsICIzMS4wNS4yMDI0IDE0OjI1OjAyIl0sIFsxMSwgNywgNywgIjE1LjA1LjIwMjQgMDk6NDA6MjMiXSwgWzExLCA0LCA0LCAiMTUuMDUuMjAyNCAwOTo0NzowNSJdLCBbMTEsIDYsIDYsICIxMy4wNS4yMDI0IDA4OjA2OjM2Il0sIFsxMSwgMywgMywgIjExLjA1LjIwMjQgMTI6MTM6MjUiXSwgWzExLCA3LCA2LCAiMTIuMDUuMjAyNCAxOTo1MjoxMCJdLCBbMTIsIDYsIDYsICIyMy4wNi4yMDI0IDE3OjExOjE0Il0sIFsxMiwgNiwgNiwgIjI0LjA2LjIwMjQgMDg6NTI6NTAiXSwgWzEyLCA3LCA2LCAiMjEuMDYuMjAyNCAxMTo1NTo0MyJdLCBbMTIsIDYsIDYsICIyNC4wNi4yMDI0IDEwOjQ0OjQ1Il0sIFsxMiwgNiwgNiwgIjIxLjA2LjIwMjQgMTI6MTY6NDYiXSwgWzEyLCA1LCA2LCAiMjEuMDYuMjAyNCAxNDo1MjoyOCJdLCBbMTIsIDcsIDUsICIyMS4wNi4yMDI0IDEzOjE5OjUxIl0sIFsxMiwgMywgMywgIjI0LjA2LjIwMjQgMDk6MjY6MDIiXSwgWzEyLCAzLCAzLCAiMjQuMDYuMjAyNCAwNzo1Njo0MSJdLCBbMTIsIDUsIDUsICIyNS4wNi4yMDI0IDEyOjA3OjQxIl0sIFsxMiwgNywgNywgIjI0LjA2LjIwMjQgMDg6MzU6NDAiXSwgWzEyLCA3LCA3LCAiMjEuMDYuMjAyNCAxNDoxMDozOCJdLCBbMTIsIDYsIDYsICIyMS4wNi4yMDI0IDEyOjMxOjI2Il0sIFsxMiwgNSwgNiwgIjIxLjA2LjIwMjQgMTU6MDY6NDkiXSwgWzEyLCA3LCA3LCAiMjMuMDYuMjAyNCAyMTozNjo1MSJdLCBbMTIsIDYsIDYsICIyMS4wNi4yMDI0IDE0OjE4OjAyIl0sIFsxMiwgNywgNywgIjIxLjA2LjIwMjQgMTI6NDk6NDkiXSwgWzEzLCA0LCA0LCAiMjAyNC0wOC0wNVQwODoyMjoxOSJdLCBbMTQsIDUsIDUsICIyMDI0LTA4LTI1VDA2OjAwOjU2Il0sIFsxNCwgNywgNiwgIjIwMjQtMDgtMjVUMDY6MDc6NTUiXSwgWzE0LCA2LCA1LCAiMjAyNC0wOC0yNVQwNzoxNjozNSJdLCBbMTQsIDcsIDcsICIyMDI0LTA4LTI1VDA4OjIzOjQwIl0sIFsxNCwgNiwgNCwgIjIwMjQtMDgtMjVUMTM6MDA6NDUiXSwgWzE0LCA3LCA3LCAiMjAyNC0wOC0yNVQxMzowNDowMiJdLCBbMTQsIDYsIDYsICIyMDI0LTA4LTI1VDEzOjU5OjA4Il0sIFsxNCwgNiwgNiwgIjIwMjQtMDgtMjVUMTQ6Mzk6MzAiXSwgWzE0LCA2LCA0LCAiMjAyNC0wOC0yNVQxNDo0ODo1OCJdLCBbMTQsIDQsIDUsICIyMDI0LTA4LTI1VDE1OjA2OjUxIl0sIFsxNCwgNSwgNiwgIjIwMjQtMDgtMjVUMTU6NTA6MDYiXSwgWzE0LCA2LCA1LCAiMjAyNC0wOC0yNlQwMjoxNzoxNiJdLCBbMTQsIDcsIDcsICIyMDI0LTA4LTI2VDAyOjE4OjUwIl0sIFsxNCwgNSwgNCwgIjIwMjQtMDgtMjZUMDI6MjM6NDIiXSwgWzE0LCA2LCA1LCAiMjAyNC0wOC0yNlQwMzoxMToyNyJdLCBbMTQsIDcsIDYsICIyMDI0LTA4LTI2VDAzOjIxOjAyIl0sIFsxNCwgNiwgNywgIjIwMjQtMDgtMjZUMDM6Mjk6NDYiXSwgWzE0LCA0LCA0LCAiMjAyNC0wOC0yNlQwNzowNzo0NiJdLCBbMTQsIDcsIDcsICIyMDI0LTA4LTI2VDEzOjUwOjIwIl0sIFsxNSwgNSwgNSwgIjIwMjQtMDktMjVUMDY6MDc6MDQiXSwgWzE1LCA3LCA3LCAiMjAyNC0wOS0yNVQwNjowODowNCJdLCBbMTUsIDYsIDYsICIyMDI0LTA5LTI1VDA2OjA5OjQ5Il0sIFsxNSwgNiwgNiwgIjIwMjQtMDktMjVUMDY6MjM6MDAiXSwgWzE1LCA2LCA1LCAiMjAyNC0wOS0yNVQwNjozMzowNiJdLCBbMTUsIDcsIDcsICIyMDI0LTA5LTI1VDA2OjM1OjM1Il0sIFsxNSwgNywgNywgIjIwMjQtMDktMjVUMDY6NDY6NTIiXSwgWzE1LCA3LCA2LCAiMjAyNC0wOS0yNVQwNjo1MTo0MyJdLCBbMTUsIDcsIDYsICIyMDI0LTA5LTI1VDA2OjUyOjMwIl0sIFsxNSwgNywgNiwgIjIwMjQtMDktMjVUMDc6MDU6NTYiXSwgWzE1LCA2LCA0LCAiMjAyNC0wOS0yNVQwNzoyMToxOCJdLCBbMTUsIDUsIDQsICIyMDI0LTA5LTI1VDA3OjQxOjQ5Il0sIFsxNSwgNiwgNiwgIjIwMjQtMDktMjVUMDk6MTY6NTUiXSwgWzE1LCA3LCA3LCAiMjAyNC0wOS0yNlQwMTo1ODozMSJdLCBbMTUsIDcsIDcsICIyMDI0LTA5LTI2VDA1OjQ4OjUwIl0sIFsxNiwgNywgNywgIjIwMjQtMTAtMjVUMDY6MDA6MzEiXSwgWzE2LCA3LCA3LCAiMjAyNC0xMC0yNVQwNjowMTowNCJdLCBbMTYsIDYsIDUsICIyMDI0LTEwLTI1VDA2OjAzOjM5Il0sIFsxNiwgMywgMywgIjIwMjQtMTAtMjVUMDY6MDc6MzkiXSwgWzE2LCA1LCA1LCAiMjAyNC0xMC0yNVQwNjoxMzoxMiJdLCBbMTYsIDcsIDUsICIyMDI0LTEwLTI1VDA2OjM5OjI0Il0sIFsxNiwgNSwgNSwgIjIwMjQtMTAtMjVUMDY6NDA6MTEiXSwgWzE2LCA3LCA3LCAiMjAyNC0xMC0yNVQwNzowMDozMiJdLCBbMTYsIDcsIDcsICIyMDI0LTEwLTI1VDA3OjIxOjUwIl0sIFsxNiwgNiwgNCwgIjIwMjQtMTAtMjVUMDg6NTg6MzYiXSwgWzE2LCA3LCA2LCAiMjAyNC0xMC0yNVQxMDo0NDozOCJdLCBbMTYsIDcsIDYsICIyMDI0LTEwLTI3VDE1OjI1OjIzIl0sIFsxNiwgNiwgNiwgIjIwMjQtMTEtMDFUMDc6MTc6MTIiXSwgWzE2LCA1LCA1LCAiMjAyNC0xMS0wM1QwNDo1NTo0MCJdLCBbMTYsIDQsIDMsICIyMDI0LTExLTA0VDA5OjEzOjI0Il0sIFsxNywgNiwgNiwgIjIwMjQtMTEtMjVUMDY6MDc6MjAiXSwgWzE3LCA2LCA2LCAiMjAyNC0xMS0yNVQwNjoxMTo1NCJdLCBbMTcsIDcsIDUsICIyMDI0LTExLTI1VDA2OjIyOjAzIl0sIFsxNywgNywgNywgIjIwMjQtMTEtMjVUMDY6MjU6MTAiXSwgWzE3LCA3LCA3LCAiMjAyNC0xMS0yNVQwNjozNDo0MyJdLCBbMTcsIDcsIDcsICIyMDI0LTExLTI1VDA4OjE3OjI4Il0sIFsxNywgNywgNywgIjIwMjQtMTEtMjVUMDg6MjM6NTciXSwgWzE3LCA3LCA3LCAiMjAyNC0xMS0yNVQwODozOTo1MSJdLCBbMTcsIDcsIDcsICIyMDI0LTExLTI1VDA4OjU0OjM0Il0sIFsxNywgNiwgNSwgIjIwMjQtMTEtMjVUMTQ6MDU6NTciXSwgWzE3LCAzLCAzLCAiMjAyNC0xMS0yNlQwMjoxNjoxMyJdLCBbMTcsIDMsIDUsICIyMDI0LTExLTI2VDAzOjUxOjUwIl0sIFsxNywgNiwgNiwgIjIwMjQtMTEtMjZUMDk6MTE6MDIiXSwgWzE3LCA2LCA0LCAiMjAyNC0xMS0yNlQxMDoyOToxNCJdLCBbMTcsIDcsIDYsICIyMDI0LTExLTI2VDE1OjMzOjQ1Il0sIFsxOCwgNywgNywgIjIwMjQtMTItMjVUMDc6MDI6MzgiXSwgWzE4LCA1LCA1LCAiMjAyNC0xMi0yNVQwNzoxMjowMCJdLCBbMTgsIDYsIDYsICIyMDI0LTEyLTI1VDA3OjE0OjE3Il0sIFsxOCwgNywgNywgIjIwMjQtMTItMjVUMDk6Mzk6MzAiXSwgWzE4LCA3LCA3LCAiMjAyNC0xMi0yN1QwMToxMTo0MiJdLCBbMTgsIDYsIDYsICIyMDI0LTEyLTI3VDAzOjI0OjM3Il0sIFsxOCwgNywgNywgIjIwMjQtMTItMjdUMDY6NDg6MDUiXSwgWzE4LCA1LCA1LCAiMjAyNC0xMi0yOVQwNTo1Mjo0NiJdLCBbMTgsIDcsIDYsICIyMDI0LTEyLTMxVDAxOjM5OjQwIl0sIFsxOCwgNSwgNiwgIjIwMjUtMDEtMDJUMDY6MDg6NDAiXSwgWzE4LCA2LCA2LCAiMjAyNS0wMS0wM1QwNTo1MjozMSJdLCBbMTgsIDcsIDcsICIyMDI1LTAxLTA2VDAyOjQ0OjI0Il0sIFsxOCwgNiwgNiwgIjIwMjUtMDEtMDZUMDQ6NDE6MDEiXSwgWzE4LCA2LCA0LCAiMjAyNS0wMS0wN1QwMjo1MTo1MiJdLCBbMTksIDYsIDUsICIyMDI1LTAxLTI1VDA2OjExOjQ5Il0sIFsxOSwgMywgNiwgIjIwMjUtMDEtMjVUMDc6Mjc6MzgiXSwgWzE5LCA2LCA1LCAiMjAyNS0wMS0yNlQwMzowMzoxOCJdLCBbMTksIDcsIDcsICIyMDI1LTAxLTI2VDA2OjE2OjEzIl0sIFsxOSwgNywgNywgIjIwMjUtMDEtMjZUMDY6Mjc6NTYiXSwgWzE5LCA3LCA1LCAiMjAyNS0wMS0yNlQxNTowNDo1NSJdLCBbMTksIDcsIDYsICIyMDI1LTAxLTI3VDAwOjQ2OjExIl0sIFsxOSwgNiwgNiwgIjIwMjUtMDEtMjdUMDE6Mjg6NTQiXSwgWzE5LCA2LCA3LCAiMjAyNS0wMS0yN1QwMTozMzozMCJdLCBbMTksIDcsIDcsICIyMDI1LTAxLTI3VDAyOjIyOjA4Il0sIFsxOSwgNiwgNSwgIjIwMjUtMDEtMjdUMDI6NDc6MDMiXSwgWzE5LCA1LCAzLCAiMjAyNS0wMS0yN1QwMzoxMTozMiJdLCBbMTksIDYsIDYsICIyMDI1LTAxLTI3VDAzOjIwOjMwIl0sIFsxOSwgNiwgNiwgIjIwMjUtMDEtMjdUMDQ6MjU6MDYiXSwgWzE5LCA1LCA1LCAiMjAyNS0wMS0yN1QwNTowOTo1NiJdLCBbMTksIDYsIDYsICIyMDI1LTAxLTI3VDA4OjM0OjAyIl0sIFsxOSwgNiwgNiwgIjIwMjUtMDItMDJUMTY6NDg6NTAiXSwgWzIwLCA3LCA3LCAiMjAyNS0wMi0yNVQwNjo0NToyMSJdLCBbMjAsIDYsIDYsICIyMDI1LTAyLTI1VDA3OjEzOjA4Il0sIFsyMCwgNSwgNSwgIjIwMjUtMDItMjVUMTA6MTk6MDEiXSwgWzIwLCA3LCA2LCAiMjAyNS0wMi0yNVQxMDozMjozNyJdLCBbMjAsIDUsIDMsICIyMDI1LTAyLTI1VDEyOjA4OjM0Il0sIFsyMCwgNywgNiwgIjIwMjUtMDItMjVUMTQ6NDc6NTAiXSwgWzIwLCA2LCA2LCAiMjAyNS0wMi0yNVQxNzoxMDo0MSJdLCBbMjAsIDcsIDYsICIyMDI1LTAyLTI4VDAyOjAzOjExIl0sIFsyMCwgNiwgNiwgIjIwMjUtMDMtMDNUMDk6MDI6MjgiXSwgWzIwLCA0LCA0LCAiMjAyNS0wMy0wNFQwMTo1MTo0MyJdLCBbMjEsIDYsIDYsICIyMDI1LTAzLTI1VDA3OjAyOjUyIl0sIFsyMSwgNywgNSwgIjIwMjUtMDMtMjVUMDc6MTM6MzIiXSwgWzIxLCA1LCAzLCAiMjAyNS0wMy0yNVQwNzoyMzo0OCJdLCBbMjEsIDcsIDcsICIyMDI1LTAzLTI1VDA3OjQzOjEzIl0sIFsyMSwgNiwgNSwgIjIwMjUtMDMtMjVUMDc6NDY6NTgiXSwgWzIxLCA2LCA2LCAiMjAyNS0wMy0yNVQwNzo0Njo1OSJdLCBbMjEsIDYsIDUsICIyMDI1LTAzLTI1VDA4OjE2OjI2Il0sIFsyMSwgNywgNywgIjIwMjUtMDMtMjVUMDg6MzQ6MDQiXSwgWzIxLCA1LCAyLCAiMjAyNS0wMy0yNVQwOToxNjoxNSJdLCBbMjEsIDYsIDYsICIyMDI1LTAzLTI1VDA5OjIwOjAwIl0sIFsyMSwgNCwgNCwgIjIwMjUtMDMtMjVUMTA6MjA6MjYiXSwgWzIxLCA2LCA2LCAiMjAyNS0wMy0yNVQxMDo0ODoxMiJdLCBbMjEsIDQsIDQsICIyMDI1LTAzLTI1VDE3OjA0OjE5Il0sIFsyMSwgNCwgNCwgIjIwMjUtMDMtMjZUMDM6MTA6MjkiXSwgWzIxLCA2LCA0LCAiMjAyNS0wMy0yNlQwNjoyNDo1OCJdLCBbMjEsIDcsIDYsICIyMDI1LTAzLTI4VDAyOjQyOjM1Il0sIFsyMiwgNywgNywgIjIwMjUtMDQtMjVUMDY6MDQ6NTciXSwgWzIyLCA1LCA1LCAiMjAyNS0wNC0yNVQwNjowODoxMyJdLCBbMjIsIDQsIDMsICIyMDI1LTA0LTI1VDA2OjEyOjMyIl0sIFsyMiwgNiwgNiwgIjIwMjUtMDQtMjVUMDY6MTY6MzIiXSwgWzIyLCA2LCA2LCAiMjAyNS0wNC0yNVQwNjoyMDozNSJdLCBbMjIsIDcsIDcsICIyMDI1LTA0LTI1VDA2OjUzOjAzIl0sIFsyMiwgNSwgNSwgIjIwMjUtMDQtMjVUMDc6NDQ6MzQiXSwgWzIyLCA2LCA1LCAiMjAyNS0wNC0yNVQwODo0NjoyMiJdLCBbMjIsIDcsIDYsICIyMDI1LTA0LTI1VDA5OjA5OjU3Il0sIFsyMiwgNiwgNiwgIjIwMjUtMDQtMjVUMDk6MjQ6NDIiXSwgWzIyLCA1LCA1LCAiMjAyNS0wNC0yOFQwMzoyNjoyOSJdLCBbMjIsIDUsIDUsICIyMDI1LTA0LTI4VDA0OjI5OjAzIl0sIFsyMiwgNiwgMywgIjIwMjUtMDQtMjhUMDU6MjE6MTkiXSwgWzIyLCA0LCA0LCAiMjAyNS0wNC0yOFQwNToyNDo1MiJdLCBbMjIsIDYsIDUsICIyMDI1LTA0LTI5VDA0OjQwOjI2Il0sIFsyMiwgNywgNiwgIjIwMjUtMDQtMjlUMDQ6NDE6MjAiXSwgWzIyLCA0LCA1LCAiMjAyNS0wNS0wNVQwMjozNjoyNSJdLCBbMjMsIDYsIDYsICIyMDI1LTA1LTI1VDA2OjEwOjI3Il0sIFsyMywgNiwgNiwgIjIwMjUtMDUtMjVUMDc6MDc6MjQiXSwgWzIzLCA2LCA1LCAiMjAyNS0wNS0yNVQxMzozNDo0OSJdLCBbMjMsIDYsIDYsICIyMDI1LTA1LTI1VDEzOjUyOjI5Il0sIFsyMywgNSwgNiwgIjIwMjUtMDUtMjZUMDA6MjQ6MzIiXSwgWzIzLCA2LCA1LCAiMjAyNS0wNS0yNlQwMTo0Mjo0NiJdLCBbMjMsIDUsIDUsICIyMDI1LTA1LTI2VDAxOjQ3OjM4Il0sIFsyMywgNywgNCwgIjIwMjUtMDUtMjZUMDI6MTM6MzAiXSwgWzIzLCA0LCAzLCAiMjAyNS0wNS0yNlQwMjoyOToxNCJdLCBbMjMsIDcsIDcsICIyMDI1LTA1LTI2VDAzOjA1OjIxIl0sIFsyMywgNywgNiwgIjIwMjUtMDUtMjZUMDM6MjM6MzkiXSwgWzIzLCA2LCA1LCAiMjAyNS0wNS0yNlQwMzoyNToyMSJdLCBbMjMsIDUsIDUsICIyMDI1LTA1LTI2VDA0OjM5OjMxIl0sIFsyMywgMywgMywgIjIwMjUtMDUtMjZUMDU6MDc6MDYiXSwgWzIzLCA2LCA2LCAiMjAyNS0wNS0yNlQwODozMDo1OSJdLCBbMjMsIDYsIDUsICIyMDI1LTA1LTMwVDA2OjI2OjE1Il0sIFsyNCwgNSwgNSwgIjIwMjUtMDYtMjVUMDY6MDQ6MTEiXSwgWzI0LCA1LCA1LCAiMjAyNS0wNi0yNVQwNjowNzo1MiJdLCBbMjQsIDcsIDYsICIyMDI1LTA2LTI1VDA2OjA4OjQwIl0sIFsyNCwgNywgNywgIjIwMjUtMDYtMjVUMDY6MTg6MTYiXSwgWzI0LCA2LCA2LCAiMjAyNS0wNi0yNVQwNjozMjo1OCJdLCBbMjQsIDYsIDYsICIyMDI1LTA2LTI1VDA2OjQzOjU4Il0sIFsyNCwgNywgNiwgIjIwMjUtMDYtMjVUMDY6NTg6MzAiXSwgWzI0LCA2LCA2LCAiMjAyNS0wNi0yNVQwNjo1OToxOCJdLCBbMjQsIDUsIDUsICIyMDI1LTA2LTI1VDA4OjA1OjIzIl0sIFsyNCwgNiwgNSwgIjIwMjUtMDYtMjVUMTQ6MDM6NDMiXSwgWzI0LCA2LCA1LCAiMjAyNS0wNi0yNlQwMzoyMzoxNCJdLCBbMjQsIDQsIDUsICIyMDI1LTA2LTI2VDA4OjUyOjAzIl0sIFsyNCwgNywgNywgIjIwMjUtMDYtMjdUMDc6MTY6NDIiXSwgWzI0LCA2LCA1LCAiMjAyNS0wNi0yOVQwNTo1MzoyOCJdLCBbMjQsIDUsIDUsICIyMDI1LTA2LTMwVDAyOjAxOjE3Il0sIFsyNSwgNiwgNiwgIjIwMjUtMDctMjVUMDY6MjU6MTEiXSwgWzI1LCAzLCA0LCAiMjAyNS0wNy0yNVQwNzowNzoyOSJdLCBbMjUsIDUsIDUsICIyMDI1LTA3LTI1VDEyOjUxOjQxIl0sIFsyNSwgNywgNiwgIjIwMjUtMDctMjhUMDM6MDg6MjMiXSwgWzI1LCA3LCA1LCAiMjAyNS0wNy0yOFQxNTo0ODoxMiJdLCBbMjYsIDQsIDUsICIyMDI1LTA4LTI1VDA2OjA0OjAxIl0sIFsyNiwgNCwgNSwgIjIwMjUtMDgtMjVUMDY6MjE6NTgiXSwgWzI2LCA2LCA2LCAiMjAyNS0wOC0yNVQwNjoyMzozOSJdLCBbMjYsIDcsIDQsICIyMDI1LTA4LTI1VDA2OjIzOjQ5Il0sIFsyNiwgNywgNiwgIjIwMjUtMDgtMjVUMDY6MzE6NDYiXSwgWzI2LCA2LCA2LCAiMjAyNS0wOC0yNVQwNzoyNToyMSJdLCBbMjYsIDMsIDMsICIyMDI1LTA4LTI1VDA3OjI5OjM5Il0sIFsyNiwgNywgNiwgIjIwMjUtMDgtMjVUMDc6NTA6MzkiXSwgWzI2LCA2LCA2LCAiMjAyNS0wOC0yNVQwODoxNTowNyJdLCBbMjYsIDYsIDYsICIyMDI1LTA4LTI1VDA4OjE3OjA5Il0sIFsyNiwgNywgNywgIjIwMjUtMDgtMjVUMDk6MjY6MDAiXSwgWzI2LCA3LCA3LCAiMjAyNS0wOC0yNVQwOTo0ODo1NyJdLCBbMjYsIDYsIDUsICIyMDI1LTA4LTI1VDEzOjIxOjUzIl0sIFsyNiwgNiwgNiwgIjIwMjUtMDgtMjZUMDI6MDg6MjMiXSwgWzI2LCA0LCA1LCAiMjAyNS0wOC0yOFQxNjo1NDo0NCJdLCBbMjcsIDcsIDUsICIyMDI1LTA5LTI1VDA2OjAxOjA3Il0sIFsyNywgNCwgNCwgIjIwMjUtMDktMjVUMDY6MDg6MTQiXSwgWzI3LCA0LCA0LCAiMjAyNS0wOS0yNVQwNjowODoxNyJdLCBbMjcsIDYsIDYsICIyMDI1LTA5LTI1VDA2OjIwOjEyIl0sIFsyNywgNSwgNSwgIjIwMjUtMDktMjVUMDY6Mjg6NTMiXSwgWzI3LCA3LCA1LCAiMjAyNS0wOS0yNVQwNjo0NjowNyJdLCBbMjcsIDYsIDYsICIyMDI1LTA5LTI1VDA3OjE5OjQ0Il0sIFsyNywgNywgNywgIjIwMjUtMDktMjVUMDc6NDU6MDkiXSwgWzI3LCA3LCA3LCAiMjAyNS0wOS0yNVQwODoyOTo1OCJdLCBbMjcsIDUsIDIsICIyMDI1LTA5LTI1VDA5OjU4OjI1Il0sIFsyNywgNywgNywgIjIwMjUtMDktMjVUMTA6MDI6MTkiXSwgWzI3LCA2LCA0LCAiMjAyNS0wOS0yNVQxMjo0MTozMiJdLCBbMjcsIDQsIDYsICIyMDI1LTA5LTI2VDAyOjI1OjQ2Il0sIFsyNywgNywgNiwgIjIwMjUtMDktMjZUMDc6MTY6MzMiXSwgWzI3LCA2LCA1LCAiMjAyNS0wOS0yOVQwODowNzozNCJdLCBbMjcsIDUsIDUsICIyMDI1LTA5LTI5VDEyOjUzOjM1Il0sIFsyOCwgNywgNywgIjIwMjUtMTAtMjVUMDY6MjI6MDQiXSwgWzI4LCA2LCA2LCAiMjAyNS0xMC0yNVQwOToxMDoyNyJdLCBbMjgsIDYsIDUsICIyMDI1LTEwLTI2VDExOjMyOjMxIl0sIFsyOCwgNCwgNiwgIjIwMjUtMTAtMjZUMTM6MzY6MDUiXSwgWzI4LCA2LCA1LCAiMjAyNS0xMC0yN1QwMjoxMToyMCJdLCBbMjgsIDYsIDUsICIyMDI1LTEwLTI3VDAyOjI2OjEzIl0sIFsyOCwgNSwgNCwgIjIwMjUtMTAtMjdUMDM6MDE6MzkiXSwgWzI4LCA3LCA3LCAiMjAyNS0xMC0yN1QwMzowNTo0NCJdLCBbMjgsIDcsIDYsICIyMDI1LTEwLTI3VDAzOjMxOjI4Il0sIFsyOCwgNCwgNCwgIjIwMjUtMTAtMjdUMDM6MzQ6MzMiXSwgWzI4LCA2LCA2LCAiMjAyNS0xMC0yN1QwMzo1MDoyNyJdLCBbMjgsIDYsIDYsICIyMDI1LTEwLTI3VDExOjIwOjI0Il0sIFsyOCwgNiwgNSwgIjIwMjUtMTAtMjlUMTI6MjA6MDMiXSwgWzI4LCA2LCA2LCAiMjAyNS0xMC0zMVQwODowMjo0OCJdLCBbMjksIDYsIDYsICIyMDI1LTExLTI1VDA2OjA1OjMxIl0sIFsyOSwgNiwgNSwgIjIwMjUtMTEtMjVUMDY6MTQ6MDAiXSwgWzI5LCA2LCA2LCAiMjAyNS0xMS0yNVQwNjoyMDo0OSJdLCBbMjksIDcsIDcsICIyMDI1LTExLTI1VDA2OjM1OjEwIl0sIFsyOSwgNSwgNSwgIjIwMjUtMTEtMjVUMDY6NTQ6MjQiXSwgWzI5LCA3LCA3LCAiMjAyNS0xMS0yNVQwNzozNzoxNSJdLCBbMjksIDYsIDUsICIyMDI1LTExLTI1VDA3OjU5OjI4Il0sIFsyOSwgNywgNywgIjIwMjUtMTEtMjVUMDk6NDU6NTYiXSwgWzI5LCA2LCA1LCAiMjAyNS0xMS0yNVQxMDoyMTowNyJdLCBbMjksIDYsIDUsICIyMDI1LTExLTI1VDEwOjMzOjA1Il0sIFsyOSwgNiwgNiwgIjIwMjUtMTEtMjZUMDM6MTI6NTQiXSwgWzI5LCA2LCA2LCAiMjAyNS0xMi0wMlQwMjowODo0MiJdLCBbMjksIDYsIDYsICIyMDI1LTEyLTAyVDAyOjEwOjAxIl0sIFsyOSwgNiwgNiwgIjIwMjUtMTItMDJUMDI6MTA6MTgiXSwgWzI4LCA2LCA2LCAiMjAyNS0xMi0wMlQwMjoxMDozMiJdLCBbMjgsIDcsIDYsICIyMDI1LTEyLTA1VDEwOjExOjM4Il0sIFszMCwgNiwgNiwgIjIwMjUtMTItMjVUMDY6MTE6MzciXSwgWzMwLCA2LCA2LCAiMjAyNS0xMi0yNVQwNzozNjo1MyJdLCBbMzAsIDcsIDYsICIyMDI1LTEyLTI1VDA3OjUxOjM5Il0sIFszMCwgNiwgNiwgIjIwMjUtMTItMjVUMDk6NTU6NDgiXSwgWzMwLCA1LCA2LCAiMjAyNS0xMi0yNVQxMDo1NzozOSJdLCBbMzAsIDYsIDcsICIyMDI1LTEyLTI1VDEyOjIwOjEzIl0sIFszMCwgNiwgNiwgIjIwMjUtMTItMjZUMDE6MjQ6MzQiXSwgWzMwLCA3LCA3LCAiMjAyNS0xMi0yNlQwNDo0NDo1MiJdLCBbMzAsIDYsIDcsICIyMDI1LTEyLTI2VDA3OjU4OjMzIl0sIFszMCwgNiwgNiwgIjIwMjUtMTItMjdUMDI6MTk6MTkiXSwgWzMwLCA3LCA3LCAiMjAyNS0xMi0yN1QwNDoyNDo1NyJdLCBbMzAsIDYsIDUsICIyMDI1LTEyLTI3VDEyOjI0OjQ2Il0sIFszMCwgNiwgNiwgIjIwMjUtMTItMjlUMDQ6MTM6NDEiXSwgWzMwLCA2LCA2LCAiMjAyNS0xMi0yOVQwNDozNzo1NiJdLCBbMzAsIDYsIDYsICIyMDI1LTEyLTI5VDA5OjMwOjE4Il0sIFszMiwgNiwgNSwgIjIwMjYtMDEtMjVUMDY6MDQ6MjciXSwgWzMyLCA2LCA3LCAiMjAyNi0wMS0yNVQwNzoyNDoyNCJdLCBbMzIsIDcsIDcsICIyMDI2LTAxLTI1VDA5OjA2OjQzIl0sIFszMiwgNiwgNSwgIjIwMjYtMDEtMjVUMTQ6NTc6MzEiXSwgWzMyLCA3LCA2LCAiMjAyNi0wMS0yNVQxNTowOTowNiJdLCBbMzIsIDcsIDcsICIyMDI2LTAxLTI2VDAxOjM5OjQ4Il0sIFszMiwgNywgNywgIjIwMjYtMDEtMjZUMDE6NTg6MDMiXSwgWzMyLCA3LCA3LCAiMjAyNi0wMS0yNlQwMjowMToyOCJdLCBbMzIsIDYsIDcsICIyMDI2LTAxLTI2VDAyOjEwOjUxIl0sIFszMiwgNywgNSwgIjIwMjYtMDEtMjZUMDI6MjQ6MTgiXSwgWzMyLCA2LCA2LCAiMjAyNi0wMS0yNlQwMjozNTo0NiJdLCBbMzIsIDYsIDUsICIyMDI2LTAxLTI2VDAyOjU0OjU3Il0sIFszMiwgNiwgNiwgIjIwMjYtMDEtMjZUMDM6MDI6MDQiXSwgWzMyLCA2LCA1LCAiMjAyNi0wMS0yNlQwMzoxNjozNiJdLCBbMzIsIDYsIDYsICIyMDI2LTAxLTI2VDA1OjU4OjE0Il0sIFszMiwgNiwgNiwgIjIwMjYtMDEtMjdUMTI6MDY6MzUiXSwgWzMzLCA3LCA3LCAiMjAyNi0wMi0yNVQwNjowMDozNCJdLCBbMzMsIDYsIDYsICIyMDI2LTAyLTI1VDA2OjAyOjQyIl0sIFszMywgNywgNywgIjIwMjYtMDItMjVUMDY6MTM6NDEiXSwgWzMzLCA2LCA0LCAiMjAyNi0wMi0yNVQwNjoxNTowNCJdLCBbMzMsIDcsIDcsICIyMDI2LTAyLTI1VDA2OjI3OjM1Il0sIFszMywgNywgNywgIjIwMjYtMDItMjVUMDY6MzI6MzUiXSwgWzMzLCA2LCA2LCAiMjAyNi0wMi0yNVQwNzo1MTo1NCJdLCBbMzMsIDYsIDYsICIyMDI2LTAyLTI1VDA4OjQ1OjAyIl0sIFszMywgNiwgNywgIjIwMjYtMDItMjVUMDg6NDc6MjkiXSwgWzMzLCA1LCA1LCAiMjAyNi0wMi0yNVQwODo1MDoyMiJdLCBbMzMsIDYsIDYsICIyMDI2LTAyLTI2VDA5OjMxOjI3Il0sIFszMywgMywgNiwgIjIwMjYtMDMtMDJUMTM6NTI6MzgiXSwgWzM0LCA3LCA3LCAiMjAyNi0wNC0wOFQwNDowODowOCJdLCBbMzQsIDcsIDcsICIyMDI2LTA0LTA4VDA0OjE1OjA1Il0sIFszNCwgNSwgNiwgIjIwMjYtMDQtMDhUMDQ6Mjc6MDYiXSwgWzM0LCA3LCA3LCAiMjAyNi0wNC0wOFQwNDoyODoxNCJdLCBbMzQsIDcsIDYsICIyMDI2LTA0LTA4VDA0OjM3OjEyIl0sIFszNCwgNiwgNiwgIjIwMjYtMDQtMDhUMDU6MDU6MzYiXSwgWzM0LCA3LCA2LCAiMjAyNi0wNC0wOFQwNTowOToxMiJdLCBbMzQsIDcsIDcsICIyMDI2LTA0LTA4VDA1OjQ5OjI3Il0sIFszNCwgNiwgNiwgIjIwMjYtMDQtMDhUMDY6NDM6MDYiXSwgWzM0LCA3LCA3LCAiMjAyNi0wNC0wOFQwNzoyNTo1NiJdLCBbMzQsIDYsIDYsICIyMDI2LTA0LTA4VDA4OjI3OjA1Il0sIFszNCwgNiwgNiwgIjIwMjYtMDQtMDhUMTA6MDU6NDAiXSwgWzM0LCAzLCA2LCAiMjAyNi0wNC0wOVQwMjo1MzozOSJdLCBbMzQsIDYsIDYsICIyMDI2LTA0LTA5VDE0OjI4OjIwIl0sIFszNCwgNywgNywgIjIwMjYtMDQtMTBUMTQ6MTM6MjciXV19"
    data = _j.loads(_b64.b64decode(DATA_B64).decode())
    with db() as con:
        con.execute("DELETE FROM trivsel_svar")
        con.execute("DELETE FROM trivsel_tokens")
        con.execute("DELETE FROM trivsel_utsendelser")
        for u in data["utsendelser"]:
            con.execute("INSERT OR IGNORE INTO trivsel_utsendelser (id, måned, år, opprettet, åpen_dager, stengt) VALUES (?,?,?,?,?,?)", u)
        for s in data["svar"]:
            con.execute("INSERT INTO trivsel_svar (utsendelse_id, trivsel, samarbeid, innsendt) VALUES (?,?,?,?)", s)
    with db() as con:
        nu = con.execute("SELECT COUNT(*) FROM trivsel_utsendelser").fetchone()[0]
        ns = con.execute("SELECT COUNT(*) FROM trivsel_svar").fetchone()[0]
    return JSONResponse({"ok": True, "utsendelser": nu, "svar": ns})

if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8502, reload=True, app_dir=str(BASE))
