#!/usr/bin/env bash
set -euo pipefail

IMAGE_NAME="horizon-status-client"

# Build image
docker build -t "${IMAGE_NAME}" .

# Env vars
SERVER_HOST="${HORIZON_SERVER_METRICS_SERVER_HOST:-}"
TCP_PORT="${HORIZON_SERVER_METRICS_TCP_PORT:-5000}"
UDP_PORT="${HORIZON_SERVER_METRICS_UDP_PORT:-5001}"
PASSWORD="${HORIZON_SERVER_METRICS_PASSWORD:-}"
DELAY_MS="${HORIZON_SERVER_METRICS_DELAY_MS:-20}"
DISCORD_TOKEN="${HORIZON_STATUS_DISCORD_TOKEN:-}"
DISCORD_CHANNEL_ID="${HORIZON_STATUS_CHANNEL_ID:-}"
DISCORD_MESSAGE_ID="${HORIZON_STATUS_MESSAGE_ID:-}"
STATE_DIR="${HORIZON_STATUS_STATE_DIR:-$(pwd)/.horizon-status}"
MESSAGE_ID_FILE="/state/discord_message_id"

mkdir -p "${STATE_DIR}"

if [[ -z "${SERVER_HOST}" ]]; then
  echo "ERROR: HORIZON_SERVER_METRICS_SERVER_HOST must be set for client."
  exit 1
fi

if [[ -z "${PASSWORD}" ]]; then
  echo "ERROR: HORIZON_SERVER_METRICS_PASSWORD must be set for client."
  exit 1
fi

echo "Starting Horizon Metrics Client..."
echo "  SERVER     = ${SERVER_HOST}"
echo "  TCP_PORT   = ${TCP_PORT}"
echo "  UDP_PORT   = ${UDP_PORT}"
echo "  DELAY_MS   = ${DELAY_MS}"
echo "  STATE_DIR  = ${STATE_DIR}"

docker run --rm -d --name horizon-status-client \
  --network host \
  -v "${STATE_DIR}:/state" \
  -e "HORIZON_STATUS_DISCORD_TOKEN=${DISCORD_TOKEN}" \
  -e "HORIZON_STATUS_CHANNEL_ID=${DISCORD_CHANNEL_ID}" \
  -e "HORIZON_STATUS_MESSAGE_ID=${DISCORD_MESSAGE_ID}" \
  -e "HORIZON_STATUS_MESSAGE_ID_FILE=${MESSAGE_ID_FILE}" \
  "${IMAGE_NAME}" \
  client \
    --host "${SERVER_HOST}" \
    --tcp-port "${TCP_PORT}" \
    --udp-port "${UDP_PORT}" \
    --delay-ms "${DELAY_MS}" \
    --password "${PASSWORD}"
