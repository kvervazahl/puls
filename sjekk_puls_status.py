"""
Puls — Månedsstatus
Sjekker hvor mange ukerapporter som er innsendt denne måneden
vs. hvor mange som skulle ha vært innsendt (ansatte × uker).
"""
from pathlib import Path
from datetime import date, timedelta
import json
import openpyxl

BASE      = Path(__file__).parent
DATA_DIR  = BASE / "data"
FAKTA_FIL = DATA_DIR / "fakta_puls.xlsx"
BRUKER_FIL = DATA_DIR / "brukere.json"


def fredager_i_måned(år: int, måned: int) -> list[date]:
    """Alle fredager i gitt måned t.o.m. i dag."""
    i_dag = date.today()
    fredager = []
    d = date(år, måned, 1)
    # Finn første fredag
    d += timedelta(days=(4 - d.weekday()) % 7)
    while d.month == måned and d <= i_dag:
        fredager.append(d)
        d += timedelta(weeks=1)
    return fredager


def iso_uke(d: date) -> int:
    return d.isocalendar()[1]


def main():
    i_dag   = date.today()
    år      = i_dag.year
    måned   = i_dag.month
    mnd_navn = i_dag.strftime("%B %Y").capitalize()

    # Ansatte
    if BRUKER_FIL.exists():
        brukere = json.loads(BRUKER_FIL.read_text(encoding="utf-8"))
        antall_ansatte = len(brukere)
        ansatte_navn = [b["navn"].split()[0] for b in brukere.values()]
    else:
        print("⚠️  Finner ikke brukere.json")
        return

    # Hvilke fredager (= rapporteringsuker) har passert denne måneden?
    fredager = fredager_i_måned(år, måned)
    uker_i_måned = [iso_uke(f) for f in fredager]

    if not uker_i_måned:
        print(f"Ingen rapporteringsuker passert i {mnd_navn} enda.")
        return

    forventet = antall_ansatte * len(uker_i_måned)

    # Les fakta_puls.xlsx — tell distinkte (Navn, Uke) for inneværende måned
    if not FAKTA_FIL.exists():
        print("⚠️  fakta_puls.xlsx ikke funnet — ingen rapporteringer enda.")
        return

    wb = openpyxl.load_workbook(FAKTA_FIL, read_only=True)
    ws = wb.active

    innsendt: set[tuple] = set()
    rapportert_av: set[str] = set()

    for row in ws.iter_rows(min_row=2, values_only=True):
        navn, epost, uke, row_år = row[0], row[1], row[2], row[3]
        if row_år == år and uke in uker_i_måned:
            innsendt.add((navn, uke))
            rapportert_av.add(navn.split()[0] if navn else "")

    wb.close()

    antall_innsendt = len(innsendt)
    pst = round(antall_innsendt / forventet * 100) if forventet else 0

    # Finn hvem som IKKE har rapportert
    ikke_rapportert = []
    for token, b in brukere.items():
        fornavn = b["navn"].split()[0]
        navn_full = b["navn"]
        mangler_uker = [u for u in uker_i_måned
                        if not any(n == navn_full and uk == u for n, uk in innsendt)]
        if mangler_uker:
            ikke_rapportert.append((fornavn, mangler_uker))

    # ── Output ──────────────────────────────────────────────────────────────
    print(f"{'─'*44}")
    print(f"  PULS — {mnd_navn}")
    print(f"{'─'*44}")
    print(f"  Ansatte:          {antall_ansatte}")
    print(f"  Rapporteringsuker: {', '.join(f'uke {u}' for u in uker_i_måned)}")
    print(f"  Forventet:        {forventet} rapporter")
    print(f"  Innsendt:         {antall_innsendt} rapporter")
    print(f"  Fullføring:       {antall_innsendt}/{forventet} ({pst}%)")
    print()

    # Enkel fremdriftslinje
    filled = round(pst / 5)
    bar = "█" * filled + "░" * (20 - filled)
    print(f"  [{bar}] {pst}%")
    print()

    if ikke_rapportert:
        print(f"  Mangler ({len(ikke_rapportert)} person{'er' if len(ikke_rapportert) != 1 else ''}):")
        for navn, uker in sorted(ikke_rapportert):
            uke_str = ", ".join(f"uke {u}" for u in uker)
            print(f"    • {navn:<12} — {uke_str}")
    else:
        print("  ✅ Alle har rapportert!")

    print(f"{'─'*44}")


if __name__ == "__main__":
    main()
