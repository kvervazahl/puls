#!/bin/bash
mkdir -p /home/data

# Kopier seed-filer til /home/data/ hvis de ikke finnes fra før (JSON-filer for migrering)
for f in /home/site/wwwroot/seed/*.json; do
    fname=$(basename "$f")
    if [ ! -f "/home/data/$fname" ]; then
        cp "$f" "/home/data/$fname"
        echo "Seed: kopierte $fname til /home/data/"
    fi
done

# Migrer JSON → SQLite hvis DB-tabeller er tomme
python migrer_til_sqlite.py

uvicorn app:app --host 0.0.0.0 --port 8000
