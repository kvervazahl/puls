#!/bin/bash
cd "$(dirname "$0")/.."
source .venv/bin/activate

# Åpne Safari etter 2 sekunder (gir serveren tid til å starte)
sleep 2 && open -a Safari "http://localhost:8502/puls/torstein" &

uvicorn puls.app:app --host 0.0.0.0 --port 8502 --reload
