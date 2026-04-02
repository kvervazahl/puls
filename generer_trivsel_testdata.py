"""
Genererer trivsel-testdata i lokal puls.db.
Kjøres én gang: python puls/generer_trivsel_testdata.py

Oppretter:
 - 6 testbrukere (hvis de ikke finnes)
 - Trivsel-runder for Jan–Apr 2026 med realistiske svar
"""
import sqlite3, secrets, random
from pathlib import Path
from datetime import datetime, timedelta

DB = Path(__file__).parent / "data" / "puls.db"

BRUKERE = [
    ("torstein", "Torstein Edvardsen", "tze@kverva.no"),
    ("kristin",  "Kristin Haugen",     "kh@kverva.no"),
    ("per",      "Per Olsen",          "po@kverva.no"),
    ("anne",     "Anne Dahl",          "ad@kverva.no"),
    ("lars",     "Lars Berg",          "lb@kverva.no"),
    ("maria",    "Maria Svendsen",     "ms@kverva.no"),
]

# (måned, dager_siden_opprettelse, antall_svarere)
RUNDER = [
    (1,  85, 6),   # Januar: alle svarte, stengt
    (2,  55, 5),   # Februar: 5 av 6 svarte, stengt
    (3,  25, 4),   # Mars: 4 av 6 svarte, stengt
    (4,   2, 2),   # April: akkurat startet, 2 svar → skjulte resultater
]

random.seed(42)

def score(token: str) -> tuple[int, int]:
    positive = {"torstein", "anne", "maria"}
    if token in positive:
        return random.randint(5, 7), random.randint(5, 7)
    else:
        return random.randint(4, 6), random.randint(4, 7)

with sqlite3.connect(DB) as c:
    c.row_factory = sqlite3.Row

    # Sørg for korrekt tabellstruktur (dropp og gjenskap hvis feil kolonnernavn)
    svar_cols = [r[1] for r in c.execute("PRAGMA table_info(trivsel_svar)").fetchall()]
    if "utsendelse_id" not in svar_cols:
        c.execute("DROP TABLE IF EXISTS trivsel_svar")
    c.executescript("""
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

    # Legg til testbrukere hvis de ikke finnes
    for token, navn, epost in BRUKERE:
        try:
            c.execute("INSERT INTO brukere (token, navn, epost) VALUES (?,?,?)", (token, navn, epost))
            print(f"  + Bruker: {navn}")
        except sqlite3.IntegrityError:
            pass  # finnes allerede

    # Rens gammel trivsel-testdata
    c.execute("DELETE FROM trivsel_svar")
    c.execute("DELETE FROM trivsel_tokens")
    c.execute("DELETE FROM trivsel_utsendelser")
    c.execute("DELETE FROM sqlite_sequence WHERE name IN ('trivsel_utsendelser','trivsel_tokens','trivsel_svar')")

    alle_brukere = c.execute("SELECT token, navn FROM brukere").fetchall()

    for måned, dager_siden, antall_svarere in RUNDER:
        opprettet = datetime.now() - timedelta(days=dager_siden)
        stengt = 1 if dager_siden > 12 else 0

        c.execute(
            "INSERT INTO trivsel_utsendelser (måned, år, opprettet, åpen_dager, stengt) VALUES (?,2026,?,10,?)",
            (måned, opprettet.isoformat(), stengt)
        )
        uid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        # Generer survey_token for alle brukere
        survey_token_map = {}
        for b in alle_brukere:
            stok = secrets.token_urlsafe(24)
            c.execute(
                "INSERT INTO trivsel_tokens (survey_token, utsendelse_id, bruker_token, brukt) VALUES (?,?,?,0)",
                (stok, uid, b["token"])
            )
            survey_token_map[b["token"]] = stok

        # La noen svare
        svarere = random.sample(alle_brukere, min(antall_svarere, len(alle_brukere)))
        for b in svarere:
            t, s = score(b["token"])
            c.execute("UPDATE trivsel_tokens SET brukt=1 WHERE utsendelse_id=? AND bruker_token=?", (uid, b["token"]))
            c.execute(
                "INSERT INTO trivsel_svar (utsendelse_id, trivsel, samarbeid, innsendt) VALUES (?,?,?,?)",
                (uid, t, s, (opprettet + timedelta(hours=random.randint(2, 72))).isoformat())
            )

    # Hent Torsteins april-lenke
    april_uid = c.execute("SELECT id FROM trivsel_utsendelser WHERE måned=4 AND år=2026").fetchone()["id"]
    torstein_stok = c.execute(
        "SELECT survey_token FROM trivsel_tokens WHERE utsendelse_id=? AND bruker_token='torstein'",
        (april_uid,)
    ).fetchone()["survey_token"]

print("\n✅ Trivsel-testdata generert!")
print(f"\nDin April-lenke (lokal):")
print(f"  http://localhost:8502/trivsel/{torstein_stok}")
print(f"\nAdmin trivsel (lokal):")
print(f"  http://localhost:8502/admin/trivsel")
print(f"\nOversikt:")
månednavn = ["Jan","Feb","Mar","Apr","Mai","Jun","Jul","Aug","Sep","Okt","Nov","Des"]
for måned, dager, antall in RUNDER:
    print(f"  {månednavn[måned-1]} 2026: {antall}/{len(BRUKERE)} svarte, {'stengt' if dager > 12 else 'åpen'}")
