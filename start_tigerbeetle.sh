#!/usr/bin/env bash
set -euo pipefail

# Directory of this script (so it can find ./tigerbeetle reliably)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo script dir $SCRIPT_DIR

# Defaults that work locally *and* in Docker
DATA_DIR="${TB_DATA_DIR:-${SCRIPT_DIR}/data}"
CLUSTER_ID="${TB_CLUSTER_ID:-0}"
REPLICA_ID="${TB_REPLICA_ID:-0}"
REPLICA_COUNT="${TB_REPLICA_COUNT:-1}"
ADDR="${TB_ADDRESS:-0.0.0.0:3000}"

REPLICA_FILE="${DATA_DIR}/${CLUSTER_ID}_${REPLICA_ID}.tigerbeetle"
TB_BIN="${SCRIPT_DIR}/tigerbeetle"

mkdir -p "${DATA_DIR}"

if [ ! -f "${REPLICA_FILE}" ]; then
  "${TB_BIN}" format \
    --cluster="${CLUSTER_ID}" \
    --replica="${REPLICA_ID}" \
    --replica-count="${REPLICA_COUNT}" \
    --development \
    "${REPLICA_FILE}"
fi

exec "${TB_BIN}" start \
  --addresses="${ADDR}" \
  --development \
  "${REPLICA_FILE}"
