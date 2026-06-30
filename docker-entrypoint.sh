#!/bin/bash
set -e

echo "=== off_upc_ddb ==="
echo "DB path: ${OFF_UPC_DDB_PATH}"
echo "HF cache: ${HF_HOME:-~/.cache/huggingface}"

# Fetch / update the database.
# - First run: downloads ~7.6 GB to /data/food.parquet
# - Subsequent runs: API check → skips if revision matches
# - Dataset updated upstream: re-downloads (HF cache enables resume)
off_upc_ddb fetch -d "$OFF_UPC_DDB_PATH"

echo ""
echo "Starting HTTP server…"
exec off_upc_ddb serve --host 0.0.0.0 --port 5000 -d "$OFF_UPC_DDB_PATH"
