#!/usr/bin/env bash
# Runs the full Phase 1/2/3 stack as plain local processes -- api, a small
# fleet of (gateway, sitl) pairs, and a static file server for gcs-web --
# for environments without a working Docker daemon. See
# infra/docker-compose.yml for the containerized version.
#
# Fleet size defaults to 2 vehicles (sim-1, sim-2), matching
# docker-compose's default -- see docs/adr/0009-multi-vehicle-fleet.md.
# Set FLEET_SIZE=1 for the Phase 1/2 single-vehicle behavior, or higher
# for a bigger fleet (each vehicle gets its own MAVLink UDP port
# 14550+N-1 and gateway HTTP port 8001+N-1).
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${VENV:-$ROOT_DIR/.venv}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/.run-logs}"
FLEET_SIZE="${FLEET_SIZE:-2}"
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

# Build GATEWAY_URLS_JSON so the API routes each vehicle's commands to its
# own gateway (sim-1 is omitted -- it uses GATEWAY_BASE_URL, the default).
gateway_urls_json="{"
for i in $(seq 2 "$FLEET_SIZE"); do
  gw_port=$((8000 + i))
  [ "$i" -gt 2 ] && gateway_urls_json+=","
  gateway_urls_json+="\"sim-$i\": \"http://127.0.0.1:$gw_port\""
done
gateway_urls_json+="}"

echo "Starting API on :8000 (fleet size $FLEET_SIZE) ..."
(cd "$ROOT_DIR/services/api" && PYTHONPATH=. GATEWAY_URLS_JSON="$gateway_urls_json" \
  "$UVICORN" app.main:app --host 0.0.0.0 --port 8000 \
  > "$LOG_DIR/api.log" 2>&1) &
PIDS+=($!)

sleep 1

for i in $(seq 1 "$FLEET_SIZE"); do
  gw_port=$((8000 + i))
  mav_port=$((14549 + i))
  vehicle_id="sim-$i"
  # Small offset per vehicle so a fleet doesn't spawn every vehicle on top
  # of each other on the map.
  home_lat=$(awk -v n="$i" 'BEGIN{printf "%.6f", 37.4275 + (n-1)*0.0015}')
  home_lon=$(awk -v n="$i" 'BEGIN{printf "%.6f", -122.1697 + (n-1)*0.0015}')

  echo "Starting gateway for $vehicle_id on :$gw_port (MAVLink :$mav_port/udp) ..."
  (cd "$ROOT_DIR/services/telemetry-gateway" && PYTHONPATH=. \
    API_BASE_URL=http://127.0.0.1:8000 \
    VEHICLE_ID="$vehicle_id" \
    GATEWAY_MAVLINK_PORT="$mav_port" \
    "$UVICORN" gateway.main:app --host 0.0.0.0 --port "$gw_port" \
    > "$LOG_DIR/gateway-$i.log" 2>&1) &
  PIDS+=($!)

  sleep 1

  echo "Starting simulator for $vehicle_id ..."
  (cd "$ROOT_DIR/simulation/sitl" && "$PY" sim.py \
    --target-host 127.0.0.1 --target-port "$mav_port" \
    --home-lat "$home_lat" --home-lon "$home_lon" \
    > "$LOG_DIR/sitl-$i.log" 2>&1) &
  PIDS+=($!)
done

echo "Starting gcs-web on :8080 ..."
(cd "$ROOT_DIR/apps/gcs-web" && "$PY" -m http.server 8080 \
  > "$LOG_DIR/gcs-web.log" 2>&1) &
PIDS+=($!)

echo ""
echo "Stack is up ($FLEET_SIZE vehicle(s)). Logs in $LOG_DIR/"
echo "  API:      http://localhost:8000/health"
echo "  GCS web:  http://localhost:8080/?api=http://localhost:8000"
echo ""
echo "Press Ctrl+C to stop."
wait
