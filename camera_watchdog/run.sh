#!/bin/sh
set -e

echo "======================================"
echo " Camera Watchdog Multicámara"
echo " Iniciando..."
echo "======================================"

exec /command/with-contenv python3 -u /app.py
