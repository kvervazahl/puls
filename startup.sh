#!/bin/bash
# Kopier seed-filer til /home/data/ hvis de ikke finnes fra før
mkdir -p /home/data
for f in /home/site/wwwroot/seed/*.json; do
    fname=$(basename "$f")
    if [ ! -f "/home/data/$fname" ]; then
        cp "$f" "/home/data/$fname"
        echo "Seed: kopierte $fname til /home/data/"
    fi
done

uvicorn app:app --host 0.0.0.0 --port 8000
