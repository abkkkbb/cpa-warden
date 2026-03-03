#!/bin/sh
set -eu

# ── Required env vars ──────────────────────────────────────
: "${CPA_BASE_URL:?CPA_BASE_URL is required}"
: "${CPA_TOKEN:?CPA_TOKEN is required}"

# ── Optional env vars with defaults ────────────────────────
CPA_MODE="$(printf '%s' "${CPA_MODE:-scan}" | tr '[:upper:]' '[:lower:]')"
CPA_ASSUME_YES="$(printf '%s' "${CPA_ASSUME_YES:-false}" | tr '[:upper:]' '[:lower:]')"
CPA_RUN_ONCE="$(printf '%s' "${CPA_RUN_ONCE:-false}" | tr '[:upper:]' '[:lower:]')"
RUN_INTERVAL_SECONDS="${RUN_INTERVAL_SECONDS:-1800}"
RUNTIME_CONFIG="${RUNTIME_CONFIG:-/tmp/cpa_warden.runtime.json}"

# ── Validate ───────────────────────────────────────────────
case "${CPA_MODE}" in
  scan|maintain) ;;
  *)
    echo "CPA_MODE must be 'scan' or 'maintain', got '${CPA_MODE}'" >&2
    exit 1
    ;;
esac

if ! [ "${RUN_INTERVAL_SECONDS}" -ge 1 ] 2>/dev/null; then
  echo "RUN_INTERVAL_SECONDS must be an integer >= 1" >&2
  exit 1
fi

mkdir -p /data
umask 077

# ── Generate runtime config from env vars ──────────────────
python3 - <<'PY'
import json, os
from pathlib import Path

def env(name, default=""):
    v = os.getenv(name)
    return default if v is None or v == "" else v

def env_int(name, default):
    return int(env(name, str(default)))

def env_bool(name, default):
    return env(name, str(default).lower()).strip().lower() in ("1", "true", "yes", "on")

config = {
    "base_url":       os.environ["CPA_BASE_URL"].rstrip("/"),
    "token":          os.environ["CPA_TOKEN"],
    "target_type":    env("CPA_TARGET_TYPE", "codex"),
    "provider":       env("CPA_PROVIDER", ""),
    "probe_workers":  env_int("CPA_PROBE_WORKERS", 40),
    "action_workers": env_int("CPA_ACTION_WORKERS", 20),
    "timeout":        env_int("CPA_TIMEOUT", 15),
    "retries":        env_int("CPA_RETRIES", 1),
    "quota_action":   env("CPA_QUOTA_ACTION", "disable"),
    "delete_401":     env_bool("CPA_DELETE_401", True),
    "auto_reenable":  env_bool("CPA_AUTO_REENABLE", True),
    "db_path":        env("CPA_DB_PATH", "/data/cpa_warden_state.sqlite3"),
    "invalid_output": env("CPA_INVALID_OUTPUT", "/data/cpa_warden_401_accounts.json"),
    "quota_output":   env("CPA_QUOTA_OUTPUT", "/data/cpa_warden_quota_accounts.json"),
    "log_file":       env("CPA_LOG_FILE", "/data/cpa_warden.log"),
    "debug":          env_bool("CPA_DEBUG", False),
    "user_agent":     env("CPA_USER_AGENT", "zeabur-cpa-warden/1.0"),
}

Path(os.environ.get("RUNTIME_CONFIG", "/tmp/cpa_warden.runtime.json")).write_text(
    json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
)
PY

echo "[entrypoint] Config generated, mode=${CPA_MODE}, interval=${RUN_INTERVAL_SECONDS}s"

# ── Start health-check server in background ────────────────
python3 ./docker/health_server.py &
HEALTH_PID="$!"

cleanup() {
  kill "${HEALTH_PID}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# ── Run function ───────────────────────────────────────────
run_once() {
  echo "[entrypoint] Starting ${CPA_MODE} at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  if [ "${CPA_MODE}" = "maintain" ]; then
    if [ "${CPA_ASSUME_YES}" = "true" ]; then
      uv run --no-sync python cpa_warden.py --mode maintain --yes --config "${RUNTIME_CONFIG}"
    else
      uv run --no-sync python cpa_warden.py --mode maintain --config "${RUNTIME_CONFIG}"
    fi
  else
    uv run --no-sync python cpa_warden.py --mode scan --config "${RUNTIME_CONFIG}"
  fi
  echo "[entrypoint] Finished ${CPA_MODE} at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
}

# ── First run ──────────────────────────────────────────────
run_once

# ── If run-once mode, keep container alive for Zeabur ──────
if [ "${CPA_RUN_ONCE}" = "true" ]; then
  echo "[entrypoint] RUN_ONCE=true, idling..."
  tail -f /dev/null
fi

# ── Loop mode ──────────────────────────────────────────────
while true; do
  echo "[entrypoint] Sleeping ${RUN_INTERVAL_SECONDS}s until next run..."
  sleep "${RUN_INTERVAL_SECONDS}"
  run_once || echo "[entrypoint] Run failed (exit $?), will retry next cycle"
done
