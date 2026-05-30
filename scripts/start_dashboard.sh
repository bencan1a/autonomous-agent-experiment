#!/usr/bin/env bash
# Idempotent dashboard start: kill any existing instance, then start fresh under nohup.
# Safe to call multiple times. Intended for @reboot cron and manual restart.
set -euo pipefail

# Resolve the project root from this script's own location (portable).
AGENT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="${AGENT_ROOT}/logs/dashboard.log"
PY="${AGENT_ROOT}/venv/bin/python"
APP="${AGENT_ROOT}/dashboard/app.py"

mkdir -p "${AGENT_ROOT}/logs"

# Kill any previously-running dashboard.
pkill -u "$(id -un)" -f "dashboard/app.py" >/dev/null 2>&1 || true
sleep 1

# Start under nohup, detached, with stdout+stderr appended to the log.
cd "${AGENT_ROOT}"
nohup "${PY}" "${APP}" >> "${LOG}" 2>&1 &
disown || true

echo "[$(date -Is)] dashboard launched (pid $!)" >> "${LOG}"
