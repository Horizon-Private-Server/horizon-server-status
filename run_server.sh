#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="echo-probe:latest"

# Build image
docker build -t "${IMAGE_NAME}" .

# Env vars
TCP_PORT="${HORIZON_SERVER_METRICS_TCP_PORT:-5000}"
UDP_PORT="${HORIZON_SERVER_METRICS_UDP_PORT:-5001}"
PASSWORD="${HORIZON_SERVER_METRICS_PASSWORD:-}"

if [[ -z "${PASSWORD}" ]]; then
  echo "ERROR: HORIZON_SERVER_METRICS_PASSWORD must be set for server."
  exit 1
fi

echo "Starting Horizon Metrics Echo Server..."
echo "  TCP = ${TCP_PORT}"
echo "  UDP = ${UDP_PORT}"

docker run --rm -d --name horizon-status \
  -p "${TCP_PORT}:${TCP_PORT}/tcp" \
  -p "${UDP_PORT}:${UDP_PORT}/udp" \
  "${IMAGE_NAME}" \
  server \
    --tcp-port "${TCP_PORT}" \
    --udp-port "${UDP_PORT}" \
    --password "${PASSWORD}"
