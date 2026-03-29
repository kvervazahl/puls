# Puls — Ukentlig timerapportering

Intern erstatning for JotForm. Ansatte rapporterer timer på aktive investeringer én gang i uken.
Bygget med Python, lagrer til Excel på SharePoint via OneDrive.

---

## Konsept

Hver fredag sendes det automatisk en personlig lenke til alle ansatte.
De fyller ut timer per investering (maks 40 totalt) i et enkelt webskjema.
Svarene lagres i et Excel-ark på SharePoint og er synlig i kontrollsenteret.

---

## Arkitektur

```
ansatte.xlsx (OneDrive)          INP_Avkastningsmodellen.xlsx (OneDrive)
      │                                        │
      └──────────────┬────────────────────────┘
                     ▼
             send_puls.py          ← kjøres fredag kl 08:00
             (Microsoft Graph API)
                     │
                     ▼
         Personlig lenke per ansatt
         https://<host>/puls/<token>
                     │
                     ▼
             FastAPI webskjema
             (én side, pen og enkel)
                     │
                     ▼
             puls_svar.xlsx        → OneDrive → SharePoint → Power BI
                     │
                     ▼
         purring.py                ← kjøres tirsdag til ikke-besvarte
```

---

## Datakilder

### `ansatte.xlsx`
Vedlikeholdes manuelt av controller. Kolonner:

| Navn | Epost | Aktiv |
|------|-------|-------|
| Ola Nordmann | ola@kverva.no | Ja |

### `INP_Avkastningsmodellen.xlsx`
Aktive investeringer hentes fra en navngitt tabell/fane i denne filen.
Scriptet leser investeringsnavnene automatisk — ingen manuell vedlikehold av prosjektliste.

### `puls_svar.xlsx` (genereres automatisk)
Lagres på OneDrive → synkroniseres til SharePoint. Kolonner:

| Uke | År | Navn | Investering | Timer | Tidspunkt | Token |
|-----|----|------|-------------|-------|-----------|-------|

---

## Komponenter

### `send_puls.py`
- Leser `ansatte.xlsx` for aktive ansatte
- Genererer unik token per person per uke
- Sender e-post via Microsoft Graph API med personlig lenke
- Kjøres via Task Scheduler / cron hver fredag kl 08:00

### `app.py` (FastAPI)
- `GET /puls/<token>` — viser skjema for riktig person med investeringsliste
- `POST /puls/<token>` — lagrer svar til `puls_svar.xlsx`
- `GET /status` — enkel JSON med svarprosent (brukes av kontrollsenteret)

### `purring.py`
- Henter hvem som ikke har svart denne uken
- Sender påminnelses-e-post
- Kjøres tirsdag kl 10:00

### `monitor.py`
- Leser `puls_svar.xlsx`
- Returnerer statistikk: hvem har svart, hvem mangler, svarprosent per uke

---

## Skjema — brukeropplevelse

Enkel webside (HTML + litt CSS, samme mørke tema som kontrollsenteret):

```
┌─────────────────────────────────────────────────┐
│  Kverva — Puls                     Uke 14, 2026 │
│                                                 │
│  Hei Ola! Fyll ut timer for denne uken.         │
│  Totalt maks 40 timer.                          │
│                                                 │
│  SalMar ASA              [  8  ] timer          │
│  BEWi ASA                [  0  ] timer          │
│  Arctic Fish              [ 12  ] timer          │
│  Kingfish Company         [  0  ] timer          │
│  Internt / admin          [ 20  ] timer          │
│                                    ─────────    │
│                           Totalt: 40 / 40       │
│                                                 │
│              [ Send inn ]                       │
└─────────────────────────────────────────────────┘
```

Validering: totalsum > 40 → advarsel. Kan ikke sende inn tomt skjema.
Etter innsending: bekreftelsesside. Lenken fungerer ikke to ganger samme uke.

---

## Kontrollsenter-integrasjon

Ny tab eller seksjon i Streamlit-appen viser:

- Svarprosent denne uken (f.eks. 7 av 12 — 58%)
- Liste over hvem som mangler
- Knapp for å sende manuell purring
- Historikk per uke som graf

---

## Teknisk stack

| Komponent | Valg | Begrunnelse |
|-----------|------|-------------|
| Web-rammeverk | FastAPI + Jinja2 | Enkelt, Python, passer existing stack |
| E-post | Microsoft Graph API | Gratis, M365-lisens dekker det |
| Lagring | Excel via openpyxl | Samme mønster som resten av prosjektet |
| Hosting (test) | Lokal Mac | Fungerer på internt nettverk |
| Hosting (prod) | Mac Mini / Azure App Service | Mac Mini fra fremtidig arkitektur |
| Autentisering | Token i URL | Enkel, ingen login nødvendig |

---

## Fase 1 — MVP

- [ ] `ansatte.xlsx` med testdata (2-3 personer)
- [ ] Les investeringsliste fra INP_Avkastningsmodellen.xlsx
- [ ] FastAPI-app med skjema og POST-endepunkt
- [ ] Lagre svar til `puls_svar.xlsx`
- [ ] Manuell kjøring (ingen e-post ennå)

## Fase 2 — E-post og utsending

- [ ] Microsoft Graph API-integrasjon (epost)
- [ ] Token-generering per person per uke
- [ ] `send_puls.py` med ukentlig utsending
- [ ] Duplikatsjekk (kan ikke svare to ganger)

## Fase 3 — Overvåkning og purring

- [ ] Kontrollsenter-tab med svarprosent og manglende
- [ ] `purring.py` automatisk tirsdag
- [ ] Historikkvisning per uke

## Fase 4 — Produksjon

- [ ] Hosting på Mac Mini (alltid på)
- [ ] cron-jobb for fredag-utsending og tirsdag-purring
- [ ] Power BI-kobling mot `puls_svar.xlsx` på SharePoint

---

## Kostnadssammenligning

| | JotForm | Puls (denne løsningen) |
|--|---------|----------------------|
| Skjema | ✅ | ✅ |
| Ukentlig utsending | ✅ Power Automate | ✅ Graph API |
| Purringer | Manuell | ✅ Automatisk |
| Data i SharePoint | Nei | ✅ Direkte |
| Investeringsliste automatisk | Nei | ✅ Fra INP_Avk |
| Kostnad | X kr/mnd | ~0 kr/mnd |
