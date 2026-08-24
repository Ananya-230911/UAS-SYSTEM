#!/usr/bin/env bash
# Runs the full Phase 1 stack as plain local processes -- api, gateway, sitl,
# and a static file server for gcs-web -- for environments without a working
# Docker daemon. See infra/docker-compose.yml for the containerized version.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-$ROOT_DIR/.venv}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/.run-logs}"
mkdir -p "$LOG_DIR"

if [ ! -d "$VENV" ]; then
  echo "Creating venv at $VENV"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q \
    -r "$ROOT_DIR/services/api/requirements.txt" \
    -r "$ROOT_DIR/services/telemetry-gateway/requirements.txt" \
    -r "$ROOT_DIR/simulation/sitl/requirements.txt"
fi

PY="$VENV/bin/python3"
UVICORN="$VENV/bin/uvicorn"
PIDS=()

cleanup() {
  echo "Stopping..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
}
trap cleanup EXIT INT TERM

export UAS_INTERNAL_TOKEN="${UAS_INTERNAL_TOKEN:-dev-secret}"
export VEHICLE_ID="${VEHICLE_ID:-sim-1}"

echo "Starting API on :8000 ..."
(cd "$ROOT_DIR/services/api" && PYTHONPATH=. "$UVICORN" app.main:app --host 0.0.0.0 --port 8000 \
  > "$LOG_DIR/api.log" 2>&1) &
PIDS+=($!)

sleep 1

echo "Starting telemetry gateway on :8001 (MAVLink :14550/udp) ..."
(cd "$ROOT_DIR/services/telemetry-gateway" && PYTHONPATH=. API_BASE_URL=http://127.0.0.1:8000 \
  "$UVICORN" gateway.main:app --host 0.0.0.0 --port 8001 \
  > "$LOG_DIR/gateway.log" 2>&1) &
PIDS+=($!)

sleep 1

echo "Starting simulator ..."
(cd "$ROOT_DIR/simulation/sitl" && "$PY" sim.py --target-host 127.0.0.1 --target-port 14550 \
  > "$LOG_DIR/sitl.log" 2>&1) &
PIDS+=($!)

echo "Starting gcs-web on :8080 ..."
(cd "$ROOT_DIR/apps/gcs-web" && "$PY" -m http.server 8080 \
  > "$LOG_DIR/gcs-web.log" 2>&1) &
PIDS+=($!)

echo ""
echo "Stack is up. Logs in $LOG_DIR/"
echo "  API:      http://localhost:8000/health"
echo "  Gateway:  http://localhost:8001/health"
echo "  GCS web:  http://localhost:8080/?api=http://localhost:8000&vehicle=sim-1"
echo ""
echo "Press Ctrl+C to stop."
wait
