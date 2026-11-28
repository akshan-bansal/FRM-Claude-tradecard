#!/usr/bin/env bash
# Fetch the six FRED series this study used into DIR (default ./fred). Not committed: S&P and Cboe
# data are licensed, and the repo never stores market data. Hashes of the files used are in
# qhft/PREREGISTRATION.md; a later download will differ in its last rows.
set -euo pipefail
DIR="${1:-fred}"
mkdir -p "$DIR"
for id in SP500 VIXCLS DGS2 DGS10 DEXUSEU DEXJPUS; do
  curl -fsS --max-time 60 -o "$DIR/$id.csv" "https://fred.stlouisfed.org/graph/fredgraph.csv?id=$id"
done
sha256sum "$DIR"/*.csv
