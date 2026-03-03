#!/bin/sh
set -eu

umask 077

# ══════════════════════════════════════════════════════════════
# Multi-instance support
#
# Single instance (backward compatible):
#   CPA_BASE_URL, CPA_TOKEN, CPA_MODE, RUN_INTERVAL_SECONDS ...
#
# Multi instance:
#   CPA_INSTANCES=2
#   CPA_BASE_URL_1, CPA_TOKEN_1, CPA_MODE_1, RUN_INTERVAL_SECONDS_1 ...
#   CPA_BASE_URL_2, CPA_TOKEN_2, CPA_MODE_2, RUN_INTERVAL_SECONDS_2 ...
# ══════════════════════════════════════════════════════════════

CPA_INSTANCES="${CPA_INSTANCES:-}"

# ── Helper: resolve env var with instance suffix fallback ────
resolve_env() {
  _var="$1" _suffix="$2" _default="$3"
  eval "_val=\"\${${_var}_${_suffix}:-}\""
  if [ -n "${_val}" ]; then printf '%s' "${_val}"; return; fi
  eval "_val=\"\${${_var}:-}\""
  if [ -n "${_val}" ]; then printf '%s' "${_val}"; return; fi
  printf '%s' "${_default}"
}

# ── Generate config JSON for one instance ────────────────────
generate_config() {
  _suffix="$1" _config_path="$2" _data_dir="$3"
  CPA_BASE_URL_RESOLVED="$(resolve_env CPA_BASE_URL "${_suffix}" "")"
  CPA_TOKEN_RESOLVED="$(resolve_env CPA_TOKEN "${_suffix}" "")"
  if [ -z "${CPA_BASE_URL_RESOLVED}" ] || [ -z "${CPA_TOKEN_RESOLVED}" ]; then
    echo "[entrypoint] ERROR: CPA_BASE_URL_${_suffix} and CPA_TOKEN_${_suffix} are required" >&2
    return 1
  fi
  export _GEN_BASE_URL="${CPA_BASE_URL_RESOLVED}"
  export _GEN_TOKEN="${CPA_TOKEN_RESOLVED}"
  export _GEN_SUFFIX="${_suffix}"
  export _GEN_CONFIG_PATH="${_config_path}"
  export _GEN_DATA_DIR="${_data_dir}"
  python3 - <<'PY'
import json, os
from pathlib import Path

sfx = os.environ["_GEN_SUFFIX"]
data = os.environ["_GEN_DATA_DIR"]

def env(name, default=""):
    for key in [f"{name}_{sfx}", name]:
        v = os.getenv(key)
        if v is not None and v != "":
            return v
    return default

def env_int(name, default):
    return int(env(name, str(default)))

def env_bool(name, default):
    return env(name, str(default).lower()).strip().lower() in ("1", "true", "yes", "on")

config = {
    "base_url":       os.environ["_GEN_BASE_URL"].rstrip("/"),
    "token":          os.environ["_GEN_TOKEN"],
    "target_type":    env("CPA_TARGET_TYPE", "codex"),
    "provider":       env("CPA_PROVIDER", ""),
    "probe_workers":  env_int("CPA_PROBE_WORKERS", 40),
    "action_workers": env_int("CPA_ACTION_WORKERS", 20),
    "timeout":        env_int("CPA_TIMEOUT", 15),
    "retries":        env_int("CPA_RETRIES", 1),
    "quota_action":   env("CPA_QUOTA_ACTION", "disable"),
    "delete_401":     env_bool("CPA_DELETE_401", True),
    "auto_reenable":  env_bool("CPA_AUTO_REENABLE", True),
    "db_path":        f"{data}/cpa_warden_state.sqlite3",
    "invalid_output": f"{data}/cpa_warden_401_accounts.json",
    "quota_output":   f"{data}/cpa_warden_quota_accounts.json",
    "log_file":       f"{data}/cpa_warden.log",
    "debug":          env_bool("CPA_DEBUG", False),
    "user_agent":     env("CPA_USER_AGENT", "zeabur-cpa-warden/1.0"),
}

Path(os.environ["_GEN_CONFIG_PATH"]).write_text(
    json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
)
PY
  unset _GEN_BASE_URL _GEN_TOKEN _GEN_SUFFIX _GEN_CONFIG_PATH _GEN_DATA_DIR
}

# ── Write instance metadata (read by web dashboard) ─────────
write_meta() {
  _id="$1" _data_dir="$2" _mode="$3" _interval="$4" _base_url="$5"
  cat > "${_data_dir}/meta.json" <<EOF
{"id":"${_id}","base_url":"${_base_url}","mode":"${_mode}","interval":${_interval}}
EOF
}

# ── Write run status (read by web dashboard) ─────────────────
write_status() {
  _id="$1" _data_dir="$2" _exit_code="$3" _run_mode="$4"
  _ts="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  cat > "${_data_dir}/last_run.json" <<EOF
{"exit_code":${_exit_code},"mode":"${_run_mode}","timestamp":"${_ts}"}
EOF
}

# ── Wait with trigger detection (poll every 5s) ─────────────
# Sets TRIGGER_MODE if a trigger file is found.
wait_or_trigger() {
  _id="$1" _interval="$2"
  _trigger="/tmp/trigger_instance_${_id}"
  _elapsed=0
  TRIGGER_MODE=""
  while [ "${_elapsed}" -lt "${_interval}" ]; do
    if [ -f "${_trigger}" ]; then
      TRIGGER_MODE="$(cat "${_trigger}" 2>/dev/null || true)"
      rm -f "${_trigger}"
      echo "[instance-${_id}] Manual trigger detected (mode=${TRIGGER_MODE:-default})"
      return 0
    fi
    sleep 5
    _elapsed=$(( _elapsed + 5 ))
  done
}

# ── Run cpa_warden once for an instance ──────────────────────
run_once() {
  _id="$1" _mode="$2" _assume_yes="$3" _config="$4" _data_dir="$5"
  echo "[instance-${_id}] Starting ${_mode} at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  touch "${_data_dir}/running"
  _exit_code=0
  if [ "${_mode}" = "maintain" ]; then
    if [ "${_assume_yes}" = "true" ]; then
      uv run --no-sync python cpa_warden.py --mode maintain --yes --config "${_config}" || _exit_code=$?
    else
      uv run --no-sync python cpa_warden.py --mode maintain --config "${_config}" || _exit_code=$?
    fi
  else
    uv run --no-sync python cpa_warden.py --mode scan --config "${_config}" || _exit_code=$?
  fi
  rm -f "${_data_dir}/running"
  write_status "${_id}" "${_data_dir}" "${_exit_code}" "${_mode}"
  if [ "${_exit_code}" -eq 0 ]; then
    echo "[instance-${_id}] Finished ${_mode} at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  else
    echo "[instance-${_id}] Failed ${_mode} (exit ${_exit_code}) at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  fi
  return "${_exit_code}"
}

# ── Instance loop ────────────────────────────────────────────
instance_loop() {
  _id="$1" _mode="$2" _assume_yes="$3" _config="$4" _interval="$5" _run_once_flag="$6" _data_dir="$7"

  run_once "${_id}" "${_mode}" "${_assume_yes}" "${_config}" "${_data_dir}" \
    || echo "[instance-${_id}] First run failed, continuing..."

  if [ "${_run_once_flag}" = "true" ]; then
    echo "[instance-${_id}] RUN_ONCE=true, done."
    return 0
  fi

  while true; do
    echo "[instance-${_id}] Sleeping ${_interval}s..."
    wait_or_trigger "${_id}" "${_interval}"
    _run_mode="${TRIGGER_MODE:-${_mode}}"
    run_once "${_id}" "${_run_mode}" "${_assume_yes}" "${_config}" "${_data_dir}" \
      || echo "[instance-${_id}] Run failed (exit $?), will retry next cycle"
  done
}

# ── Start web dashboard + health-check server ────────────────
python3 ./docker/health_server.py &
HEALTH_PID="$!"

cleanup() {
  kill "${HEALTH_PID}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

# ══════════════════════════════════════════════════════════════
# Dispatch: single vs multi instance
# ══════════════════════════════════════════════════════════════

if [ -z "${CPA_INSTANCES}" ]; then
  # ── Single instance (backward compatible) ──────────────────
  : "${CPA_BASE_URL:?CPA_BASE_URL is required}"
  : "${CPA_TOKEN:?CPA_TOKEN is required}"

  _mode="$(printf '%s' "${CPA_MODE:-scan}" | tr '[:upper:]' '[:lower:]')"
  _assume_yes="$(printf '%s' "${CPA_ASSUME_YES:-false}" | tr '[:upper:]' '[:lower:]')"
  _run_once="$(printf '%s' "${CPA_RUN_ONCE:-false}" | tr '[:upper:]' '[:lower:]')"
  _interval="${RUN_INTERVAL_SECONDS:-1800}"
  _config="/tmp/cpa_warden.runtime.json"

  case "${_mode}" in
    scan|maintain) ;;
    *) echo "CPA_MODE must be 'scan' or 'maintain'" >&2; exit 1 ;;
  esac

  mkdir -p /data
  generate_config "0" "${_config}" "/data"
  write_meta "0" "/data" "${_mode}" "${_interval}" "${CPA_BASE_URL}"
  echo "[entrypoint] Single instance, mode=${_mode}, interval=${_interval}s"
  instance_loop "0" "${_mode}" "${_assume_yes}" "${_config}" "${_interval}" "${_run_once}" "/data"

else
  # ── Multi instance ─────────────────────────────────────────
  if ! [ "${CPA_INSTANCES}" -ge 1 ] 2>/dev/null; then
    echo "CPA_INSTANCES must be an integer >= 1" >&2
    exit 1
  fi

  echo "[entrypoint] Multi-instance mode: ${CPA_INSTANCES} instance(s)"
  LOOP_PIDS=""
  _i=1
  while [ "${_i}" -le "${CPA_INSTANCES}" ]; do
    _mode="$(printf '%s' "$(resolve_env CPA_MODE "${_i}" "scan")" | tr '[:upper:]' '[:lower:]')"
    _assume_yes="$(printf '%s' "$(resolve_env CPA_ASSUME_YES "${_i}" "false")" | tr '[:upper:]' '[:lower:]')"
    _run_once="$(printf '%s' "$(resolve_env CPA_RUN_ONCE "${_i}" "false")" | tr '[:upper:]' '[:lower:]')"
    _interval="$(resolve_env RUN_INTERVAL_SECONDS "${_i}" "1800")"
    _data_dir="/data/instance_${_i}"
    _config="/tmp/cpa_warden_${_i}.json"
    _base_url="$(resolve_env CPA_BASE_URL "${_i}" "")"

    case "${_mode}" in
      scan|maintain) ;;
      *) echo "CPA_MODE_${_i} must be 'scan' or 'maintain'" >&2; exit 1 ;;
    esac

    mkdir -p "${_data_dir}"
    generate_config "${_i}" "${_config}" "${_data_dir}"
    write_meta "${_i}" "${_data_dir}" "${_mode}" "${_interval}" "${_base_url}"
    echo "[entrypoint] Instance ${_i}: mode=${_mode}, interval=${_interval}s"

    instance_loop "${_i}" "${_mode}" "${_assume_yes}" "${_config}" "${_interval}" "${_run_once}" "${_data_dir}" &
    LOOP_PIDS="${LOOP_PIDS} $!"
    _i=$(( _i + 1 ))
  done

  for _pid in ${LOOP_PIDS}; do
    wait "${_pid}" || true
  done

  echo "[entrypoint] All instances finished, idling..."
  tail -f /dev/null
fi
