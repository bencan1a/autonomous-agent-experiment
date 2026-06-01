#!/usr/bin/env bash
# Idempotent advisory-watcher start for one instance: kill any existing watcher
# for it, then start a fresh polling loop under nohup. Safe to call repeatedly.
#
# Usage: ./scripts/start_advisory_watch.sh <instance_id> [interval_seconds]
set -euo pipefail

INSTANCE="${1:?usage: start_advisory_watch.sh <instance_id> [interval_seconds]}"
INTERVAL="${2:-60}"

AGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${AGENT_ROOT}/logs/advisory_${INSTANCE}.log"
PY="${AGENT_ROOT}/venv/bin/python"

mkdir -p "${AGENT_ROOT}/logs"

# Kill any previously-running watcher for THIS instance only.
pkill -u "$(id -un)" -f "run_advisory_watch.py --instance ${INSTANCE}" >/dev/null 2>&1 || true
sleep 1

cd "${AGENT_ROOT}"
nohup "${PY}" scripts/run_advisory_watch.py --instance "${INSTANCE}" --loop "${INTERVAL}" \
    >> "${LOG}" 2>&1 &
disown || true

echo "[$(date -Is)] advisory watcher launched for ${INSTANCE} (pid $!, every ${INTERVAL}s)" >> "${LOG}"
echo "advisory watcher started for ${INSTANCE} (every ${INTERVAL}s); log: ${LOG}"
