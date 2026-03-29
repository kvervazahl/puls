"""
Migrerer eksisterende JSON-filer til SQLite.
Kjøres automatisk av startup.sh hvis JSON-filer finnes og DB mangler data.
Trygt å kjøre flere ganger (idempotent).
"""
import json
import sqlite3
import os
from pathlib import Path

DATA_DIR = Path("/home/data") if os.environ.get("WEBSITE_SITE_NAME") else Path(__file__).parent / "data"
DB_FIL          = DATA_DIR / "puls.db"
BRUKERE_FIL     = DATA_DIR / "brukere.json"
INV_FIL         = DATA_DIR / "investeringer.json"
SVAR_FIL        = DATA_DIR / "svar.json"

def les_json(fil, default):
    if not fil.exists():
        return default
    try:
        return json.loads(fil.read_text(encoding="utf-8"))
    except Exception:
        return default

def migrer():
    con = sqlite3.connect(DB_FIL)
    con.row_factory = sqlite3.Row

    # Opprett tabeller hvis de ikke finnes
    con.executescript("""
        CREATE TABLE IF NOT EXISTS brukere (
            token TEXT PRIMARY KEY,
            navn  TEXT NOT NULL,
            epost TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS investeringer (
            navn      TEXT PRIMARY KEY,
            rekkefølge INTEGER NOT NULL DEFAULT 0
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
    """)

    # Brukere
    brukere = les_json(BRUKERE_FIL, {})
    if brukere:
        for token, b in brukere.items():
            con.execute(
                "INSERT OR IGNORE INTO brukere (token, navn, epost) VALUES (?,?,?)",
                (token, b["navn"], b["epost"])
            )
        print(f"  Migrerte {len(brukere)} brukere")

    # Investeringer
    inv = les_json(INV_FIL, [])
    if inv:
        for i, navn in enumerate(inv):
            con.execute(
                "INSERT OR IGNORE INTO investeringer (navn, rekkefølge) VALUES (?,?)",
                (navn, i)
            )
        print(f"  Migrerte {len(inv)} investeringer")

    # Svar
    svar = les_json(SVAR_FIL, [])
    if svar:
        antall = 0
        for s in svar:
            try:
                con.execute("""
                    INSERT OR IGNORE INTO svar (token, navn, epost, uke, år, fravar, timer, total, tidspunkt)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, (
                    s["token"], s["navn"], s.get("epost", ""),
                    s["uke"], s["år"],
                    int(s.get("fravær", False)),
                    json.dumps(s.get("timer", {}), ensure_ascii=False),
                    s.get("total", 0),
                    s["tidspunkt"],
                ))
                antall += 1
            except Exception as e:
                print(f"  Advarsel: kunne ikke migrere svar {s}: {e}")
        print(f"  Migrerte {antall} svar")

    con.commit()
    con.close()
    print("Migrering ferdig.")

if __name__ == "__main__":
    migrer()
