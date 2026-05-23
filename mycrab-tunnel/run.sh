#!/usr/bin/env bash
set -e

CONFIG_PATH="/data/options.json"
DATA_DIR="/data/tunnels"
mkdir -p "$DATA_DIR"

export CONFIG_PATH DATA_DIR

echo "[mycrab] starting dashboard on :8099"
exec python3 /app/main.py
