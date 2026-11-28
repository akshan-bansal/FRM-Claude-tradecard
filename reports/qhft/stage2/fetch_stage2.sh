#!/usr/bin/env bash
# Stage-2 data: the FRED series behind the eight BSM_Spectral underlyings plus the macro factors.
# Run only AFTER freeze.py. Not committed: index and Cboe data are licensed, and the repo never stores
# market data. Record the printed hashes in the run's results.
set -euo pipefail
DIR="${1:-fred}"
[ -f "$(dirname "$0")/FROZEN.sha256" ] || { echo "freeze the pre-registration first (python freeze.py)"; exit 1; }
mkdir -p "$DIR"
for id in SP500 VIXCLS NASDAQ100 VXNCLS DJIA VXDCLS DCOILWTICO OVXCLS CBBTCUSD CBETHUSD \
          DEXUSEU DEXJPUS DGS2 DGS10; do
  curl -fsS --max-time 60 -o "$DIR/$id.csv" "https://fred.stlouisfed.org/graph/fredgraph.csv?id=$id"
done
sha256sum "$DIR"/*.csv
