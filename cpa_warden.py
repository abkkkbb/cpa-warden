#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sqlite3
import sys
import urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    import aiohttp
except Exception:
    aiohttp = None

try:
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
except Exception:
    Progress = None


DEFAULT_CONFIG_PATH = "config.json"
DEFAULT_TARGET_TYPE = "codex"
DEFAULT_USER_AGENT = "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"
DEFAULT_PROBE_WORKERS = 40
DEFAULT_ACTION_WORKERS = 20
DEFAULT_TIMEOUT = 15
DEFAULT_RETRIES = 1
DEFAULT_QUOTA_ACTION = "disable"
DEFAULT_DELETE_401 = True
DEFAULT_AUTO_REENABLE = True
DEFAULT_DB_PATH = "cpa_warden_state.sqlite3"
DEFAULT_INVALID_OUTPUT = "cpa_warden_401_accounts.json"
DEFAULT_QUOTA_OUTPUT = "cpa_warden_quota_accounts.json"
DEFAULT_LOG_FILE = "cpa_warden.log"
WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

AUTH_ACCOUNT_COLUMNS = [
    "name",
    "disabled",
    "id_token_json",
    "email",
    "provider",
    "source",
    "unavailable",
    "auth_index",
    "account",
    "type",
    "runtime_only",
    "status",
    "status_message",
    "chatgpt_account_id",
    "id_token_plan_type",
    "auth_updated_at",
    "auth_modtime",
    "auth_last_refresh",
    "api_http_status",
    "api_status_code",
    "usage_allowed",
    "usage_limit_reached",
    "usage_plan_type",
    "usage_email",
    "usage_reset_at",
    "usage_reset_after_seconds",
    "is_invalid_401",
    "is_quota_limited",
    "is_recovered",
    "probe_error_kind",
    "probe_error_text",
    "managed_reason",
    "last_action",
    "last_action_status",
    "last_action_error",
    "last_seen_at",
    "last_probed_at",
    "updated_at",
]


LOGGER = logging.getLogger("cpa_warden")


def configure_logging(log_file: str, debug: bool) -> None:
    LOGGER.handlers.clear()
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.propagate = False

    log_path = Path(log_file)
    if log_path.parent and str(log_path.parent) not in {"", "."}:
        log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if debug else logging.INFO)
    console_handler.setFormatter(formatter)
    LOGGER.addHandler(console_handler)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)


def rich_progress_enabled(debug: bool) -> bool:
    return bool((Progress is not None) and (not debug) and hasattr(sys.stdout, "isatty") and sys.stdout.isatty())


class ProgressReporter:
    def __init__(self, description: str, total: int, *, debug: bool) -> None:
        self.description = description
        self.total = max(0, int(total))
        self.debug = debug
        self.enabled = rich_progress_enabled(debug)
        self._progress: Progress | None = None
        self._task_id: Any = None

    def __enter__(self) -> "ProgressReporter":
        if self.enabled:
            self._progress = Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                TimeRemainingColumn(),
                transient=False,
            )
            self._progress.start()
            self._task_id = self._progress.add_task(self.description, total=self.total)
        return self

    def advance(self, step: int = 1) -> None:
        if self._progress is not None and self._task_id is not None:
            self._progress.advance(self._task_id, step)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._progress is not None:
            self._progress.stop()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_json(resp: requests.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def maybe_json_loads(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except Exception:
        return None


def compact_text(text: Any, limit: int = 240) -> str | None:
    if text is None:
        return None
    normalized = str(text).replace("\r", " ").replace("\n", " ").strip()
    if not normalized:
        return None
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 3)] + "..."


def ensure_aiohttp() -> None:
    if aiohttp is None:
        print("错误: 未安装 aiohttp。请先执行 `uv sync`。", file=sys.stderr)
        sys.exit(1)


def get_item_name(item: dict[str, Any]) -> str:
    return str(item.get("name") or item.get("id") or "").strip()


def get_item_type(item: dict[str, Any]) -> str:
    return str(item.get("type") or item.get("typo") or "").strip()


def get_item_account(item: dict[str, Any]) -> str:
    return str(item.get("account") or item.get("email") or "").strip()


def get_id_token_object(item: dict[str, Any]) -> dict[str, Any]:
    parsed = maybe_json_loads(item.get("id_token"))
    return parsed if isinstance(parsed, dict) else {}


def extract_chatgpt_account_id_from_item(item: dict[str, Any]) -> str:
    id_token = get_id_token_object(item)
    for source in (id_token, item):
        for key in ("chatgpt_account_id", "chatgptAccountId", "account_id", "accountId"):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def extract_id_token_plan_type(item: dict[str, Any]) -> str:
    id_token = get_id_token_object(item)
    value = id_token.get("plan_type")
    return value.strip() if isinstance(value, str) else ""


def mgmt_headers(token: str, include_json: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
    }
    if include_json:
        headers["Content-Type"] = "application/json"
    return headers


def config_lookup(conf: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in conf and conf.get(key) not in (None, ""):
            return conf.get(key)
    return default


def load_config_json(path: str, required: bool = False) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        if required:
            raise RuntimeError(f"配置文件不存在: {path}")
        return {}

    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"读取配置文件失败: {exc}") from exc

    if not isinstance(data, dict):
        raise RuntimeError("配置文件格式错误: 顶层必须是 JSON 对象")

    return data


def build_settings(args: argparse.Namespace, conf: dict[str, Any]) -> dict[str, Any]:
    quota_action = str(
        args.quota_action
        or config_lookup(conf, "quota_action", default=DEFAULT_QUOTA_ACTION)
    ).strip().lower()
    if quota_action not in {"disable", "delete"}:
        raise RuntimeError("quota_action 只能是 disable 或 delete")

    settings = {
        "config_path": args.config,
        "base_url": str(config_lookup(conf, "base_url", default="")).strip(),
        "token": str(config_lookup(conf, "token", default="")).strip(),
        "mode": args.mode,
        "target_type": str(
            args.target_type
            or config_lookup(conf, "target_type", default=DEFAULT_TARGET_TYPE)
        ).strip(),
        "provider": str(
            args.provider if args.provider is not None else config_lookup(conf, "provider", default="")
        ).strip(),
        "probe_workers": int(
            args.probe_workers
            if args.probe_workers is not None
            else config_lookup(conf, "probe_workers", "workers", default=DEFAULT_PROBE_WORKERS)
        ),
        "action_workers": int(
            args.action_workers
            if args.action_workers is not None
            else config_lookup(conf, "action_workers", "delete_workers", default=DEFAULT_ACTION_WORKERS)
        ),
        "timeout": int(
            args.timeout if args.timeout is not None else config_lookup(conf, "timeout", default=DEFAULT_TIMEOUT)
        ),
        "retries": int(
            args.retries if args.retries is not None else config_lookup(conf, "retries", default=DEFAULT_RETRIES)
        ),
        "quota_action": quota_action,
        "delete_401": (
            args.delete_401
            if args.delete_401 is not None
            else bool(config_lookup(conf, "delete_401", default=DEFAULT_DELETE_401))
        ),
        "auto_reenable": (
            args.auto_reenable
            if args.auto_reenable is not None
            else bool(config_lookup(conf, "auto_reenable", default=DEFAULT_AUTO_REENABLE))
        ),
        "db_path": str(
            args.db_path
            or config_lookup(conf, "db_path", default=DEFAULT_DB_PATH)
        ).strip(),
        "invalid_output": str(
            args.invalid_output
            or config_lookup(conf, "invalid_output", "output", default=DEFAULT_INVALID_OUTPUT)
        ).strip(),
        "quota_output": str(
            args.quota_output
            or config_lookup(conf, "quota_output", default=DEFAULT_QUOTA_OUTPUT)
        ).strip(),
        "log_file": str(
            args.log_file
            or config_lookup(conf, "log_file", default=DEFAULT_LOG_FILE)
        ).strip(),
        "user_agent": str(
            args.user_agent
            or config_lookup(conf, "user_agent", default=DEFAULT_USER_AGENT)
        ).strip(),
        "debug": bool(
            args.debug if args.debug is not None else config_lookup(conf, "debug", default=False)
        ),
        "assume_yes": bool(args.yes),
    }

    if settings["probe_workers"] < 1:
        raise RuntimeError("probe_workers 必须 >= 1")
    if settings["action_workers"] < 1:
        raise RuntimeError("action_workers 必须 >= 1")
    if settings["timeout"] < 1:
        raise RuntimeError("timeout 必须 >= 1")
    if settings["retries"] < 0:
        raise RuntimeError("retries 不能小于 0")
    if not settings["target_type"]:
        raise RuntimeError("target_type 不能为空")
    if not settings["log_file"]:
        raise RuntimeError("log_file 不能为空")

    return settings


def connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS auth_accounts (
            name TEXT PRIMARY KEY,
            disabled INTEGER NOT NULL,
            id_token_json TEXT,
            email TEXT,
            provider TEXT,
            source TEXT,
            unavailable INTEGER NOT NULL,
            auth_index TEXT,
            account TEXT,
            type TEXT,
            runtime_only INTEGER NOT NULL,
            status TEXT,
            status_message TEXT,
            chatgpt_account_id TEXT,
            id_token_plan_type TEXT,
            auth_updated_at TEXT,
            auth_modtime TEXT,
            auth_last_refresh TEXT,
            api_http_status INTEGER,
            api_status_code INTEGER,
            usage_allowed INTEGER,
            usage_limit_reached INTEGER,
            usage_plan_type TEXT,
            usage_email TEXT,
            usage_reset_at INTEGER,
            usage_reset_after_seconds INTEGER,
            is_invalid_401 INTEGER NOT NULL DEFAULT 0,
            is_quota_limited INTEGER NOT NULL DEFAULT 0,
            is_recovered INTEGER NOT NULL DEFAULT 0,
            probe_error_kind TEXT,
            probe_error_text TEXT,
            managed_reason TEXT,
            last_action TEXT,
            last_action_status TEXT,
            last_action_error TEXT,
            last_seen_at TEXT NOT NULL,
            last_probed_at TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scan_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            mode TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            total_files INTEGER NOT NULL,
            filtered_files INTEGER NOT NULL,
            probed_files INTEGER NOT NULL,
            invalid_401_count INTEGER NOT NULL,
            quota_limited_count INTEGER NOT NULL,
            recovered_count INTEGER NOT NULL,
            delete_401 INTEGER NOT NULL,
            quota_action TEXT NOT NULL,
            probe_workers INTEGER NOT NULL,
            action_workers INTEGER NOT NULL,
            timeout_seconds INTEGER NOT NULL,
            retries INTEGER NOT NULL
        );
        """
    )
    conn.commit()


def load_existing_state(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute("SELECT * FROM auth_accounts").fetchall()
    return {str(row["name"]): dict(row) for row in rows}


def start_scan_run(conn: sqlite3.Connection, settings: dict[str, Any]) -> int:
    cur = conn.execute(
        """
        INSERT INTO scan_runs (
            mode, started_at, finished_at, status, total_files, filtered_files, probed_files,
            invalid_401_count, quota_limited_count, recovered_count, delete_401, quota_action,
            probe_workers, action_workers, timeout_seconds, retries
        ) VALUES (?, ?, NULL, 'running', 0, 0, 0, 0, 0, 0, ?, ?, ?, ?, ?, ?)
        """,
        (
            settings["mode"],
            utc_now_iso(),
            int(bool(settings["delete_401"])),
            settings["quota_action"],
            settings["probe_workers"],
            settings["action_workers"],
            settings["timeout"],
            settings["retries"],
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_scan_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    total_files: int,
    filtered_files: int,
    probed_files: int,
    invalid_401_count: int,
    quota_limited_count: int,
    recovered_count: int,
) -> None:
    conn.execute(
        """
        UPDATE scan_runs
        SET finished_at = ?, status = ?, total_files = ?, filtered_files = ?, probed_files = ?,
            invalid_401_count = ?, quota_limited_count = ?, recovered_count = ?
        WHERE run_id = ?
        """,
        (
            utc_now_iso(),
            status,
            int(total_files),
            int(filtered_files),
            int(probed_files),
            int(invalid_401_count),
            int(quota_limited_count),
            int(recovered_count),
            int(run_id),
        ),
    )
    conn.commit()


def upsert_auth_accounts(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    columns_sql = ", ".join(AUTH_ACCOUNT_COLUMNS)
    placeholders = ", ".join(f":{column}" for column in AUTH_ACCOUNT_COLUMNS)
    updates = ", ".join(
        f"{column} = excluded.{column}"
        for column in AUTH_ACCOUNT_COLUMNS
        if column != "name"
    )
    conn.executemany(
        f"""
        INSERT INTO auth_accounts ({columns_sql})
        VALUES ({placeholders})
        ON CONFLICT(name) DO UPDATE SET
            {updates}
        """,
        rows,
    )
    conn.commit()


def row_to_bool(value: Any) -> bool:
    return bool(int(value)) if value is not None else False


def build_auth_record(
    item: dict[str, Any],
    existing_row: dict[str, Any] | None,
    now_iso: str,
) -> dict[str, Any]:
    id_token_obj = get_id_token_object(item)
    id_token_json = json.dumps(id_token_obj, ensure_ascii=False) if id_token_obj else None
    existing_row = existing_row or {}
    return {
        "name": get_item_name(item),
        "disabled": int(bool(item.get("disabled"))),
        "id_token_json": id_token_json,
        "email": str(item.get("email") or "").strip() or None,
        "provider": str(item.get("provider") or "").strip() or None,
        "source": str(item.get("source") or "").strip() or None,
        "unavailable": int(bool(item.get("unavailable"))),
        "auth_index": str(item.get("auth_index") or "").strip() or None,
        "account": get_item_account(item) or None,
        "type": get_item_type(item) or None,
        "runtime_only": int(bool(item.get("runtime_only"))),
        "status": str(item.get("status") or "").strip() or None,
        "status_message": compact_text(item.get("status_message"), 1200),
        "chatgpt_account_id": extract_chatgpt_account_id_from_item(item) or None,
        "id_token_plan_type": extract_id_token_plan_type(item) or None,
        "auth_updated_at": str(item.get("updated_at") or "").strip() or None,
        "auth_modtime": str(item.get("modtime") or "").strip() or None,
        "auth_last_refresh": str(item.get("last_refresh") or "").strip() or None,
        "api_http_status": None,
        "api_status_code": None,
        "usage_allowed": None,
        "usage_limit_reached": None,
        "usage_plan_type": None,
        "usage_email": None,
        "usage_reset_at": None,
        "usage_reset_after_seconds": None,
        "is_invalid_401": 0,
        "is_quota_limited": 0,
        "is_recovered": 0,
        "probe_error_kind": None,
        "probe_error_text": None,
        "managed_reason": existing_row.get("managed_reason"),
        "last_action": existing_row.get("last_action"),
        "last_action_status": existing_row.get("last_action_status"),
        "last_action_error": existing_row.get("last_action_error"),
        "last_seen_at": now_iso,
        "last_probed_at": None,
        "updated_at": now_iso,
    }


def matches_filters(record: dict[str, Any], target_type: str, provider: str) -> bool:
    if str(record.get("type") or "").lower() != target_type.lower():
        return False
    if provider and str(record.get("provider") or "").lower() != provider.lower():
        return False
    return True


def fetch_auth_files(base_url: str, token: str, timeout: int) -> list[dict[str, Any]]:
    LOGGER.info("开始拉取 auth-files 列表")
    LOGGER.debug("GET %s/v0/management/auth-files", base_url.rstrip("/"))
    resp = requests.get(
        f"{base_url.rstrip('/')}/v0/management/auth-files",
        headers=mgmt_headers(token),
        timeout=timeout,
    )
    resp.raise_for_status()
    data = safe_json(resp)
    files = data.get("files")
    LOGGER.info("auth-files 拉取完成: %s", len(files) if isinstance(files, list) else 0)
    return files if isinstance(files, list) else []


def build_wham_usage_payload(auth_index: str, user_agent: str, chatgpt_account_id: str) -> dict[str, Any]:
    return {
        "authIndex": auth_index,
        "method": "GET",
        "url": WHAM_USAGE_URL,
        "header": {
            "Authorization": "Bearer $TOKEN$",
            "Content-Type": "application/json",
            "User-Agent": user_agent,
            "Chatgpt-Account-Id": chatgpt_account_id,
        },
    }


async def probe_wham_usage_async(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    base_url: str,
    token: str,
    record: dict[str, Any],
    timeout: int,
    retries: int,
    user_agent: str,
) -> dict[str, Any]:
    result = dict(record)
    result["last_probed_at"] = utc_now_iso()
    LOGGER.debug(
        "开始探测账号: name=%s auth_index=%s unavailable=%s disabled=%s has_account_id=%s",
        result.get("name"),
        result.get("auth_index"),
        bool(result.get("unavailable")),
        bool(result.get("disabled")),
        "yes" if result.get("chatgpt_account_id") else "no",
    )

    auth_index = str(result.get("auth_index") or "").strip()
    account_id = str(result.get("chatgpt_account_id") or "").strip()

    if not auth_index:
        result["probe_error_kind"] = "missing_auth_index"
        result["probe_error_text"] = "missing auth_index"
        LOGGER.debug("跳过账号: name=%s reason=missing_auth_index", result.get("name"))
        return result

    if not account_id:
        result["probe_error_kind"] = "missing_chatgpt_account_id"
        result["probe_error_text"] = "missing Chatgpt-Account-Id"
        LOGGER.debug("跳过账号: name=%s reason=missing_chatgpt_account_id", result.get("name"))
        return result

    payload = build_wham_usage_payload(auth_index, user_agent, account_id)
    url = f"{base_url.rstrip('/')}/v0/management/api-call"

    for attempt in range(retries + 1):
        try:
            async with semaphore:
                async with session.post(
                    url,
                    headers=mgmt_headers(token, include_json=True),
                    json=payload,
                    timeout=timeout,
                ) as resp:
                    text = await resp.text()
                    result["api_http_status"] = resp.status

                    if resp.status >= 500:
                        result["probe_error_kind"] = "management_api_http_5xx"
                        result["probe_error_text"] = f"management api-call http {resp.status}"
                        LOGGER.debug("探测失败: name=%s api_http=%s", result.get("name"), resp.status)
                        return result
                    if resp.status >= 400:
                        result["probe_error_kind"] = "management_api_http_4xx"
                        result["probe_error_text"] = f"management api-call http {resp.status}"
                        LOGGER.debug("探测失败: name=%s api_http=%s", result.get("name"), resp.status)
                        return result

                    try:
                        outer = json.loads(text)
                    except Exception:
                        result["probe_error_kind"] = "api_call_invalid_json"
                        result["probe_error_text"] = "api-call response is not valid JSON"
                        LOGGER.debug("探测失败: name=%s reason=api_call_invalid_json", result.get("name"))
                        return result

                    if not isinstance(outer, dict):
                        result["probe_error_kind"] = "api_call_not_object"
                        result["probe_error_text"] = f"api-call response is not JSON object: {type(outer).__name__}"
                        LOGGER.debug("探测失败: name=%s reason=api_call_not_object", result.get("name"))
                        return result

                    status_code = outer.get("status_code")
                    result["api_status_code"] = status_code
                    if status_code is None:
                        result["probe_error_kind"] = "missing_status_code"
                        result["probe_error_text"] = "missing status_code in api-call response"
                        LOGGER.debug("探测失败: name=%s reason=missing_status_code", result.get("name"))
                        return result

                    if status_code == 401:
                        result["probe_error_kind"] = None
                        result["probe_error_text"] = None
                        LOGGER.debug("探测完成: name=%s status_code=401", result.get("name"))
                        return result

                    body = outer.get("body")
                    if isinstance(body, dict):
                        parsed_body = body
                    elif isinstance(body, str):
                        try:
                            parsed_body = json.loads(body)
                        except Exception:
                            result["probe_error_kind"] = "body_invalid_json"
                            result["probe_error_text"] = "api-call body is not valid JSON"
                            LOGGER.debug("探测失败: name=%s reason=body_invalid_json", result.get("name"))
                            return result
                    elif body is None:
                        parsed_body = {}
                    else:
                        result["probe_error_kind"] = "body_not_object"
                        result["probe_error_text"] = f"api-call body is not JSON object: {type(body).__name__}"
                        LOGGER.debug("探测失败: name=%s reason=body_not_object", result.get("name"))
                        return result

                    if parsed_body and not isinstance(parsed_body, dict):
                        result["probe_error_kind"] = "body_not_object"
                        result["probe_error_text"] = f"api-call body is not JSON object: {type(parsed_body).__name__}"
                        LOGGER.debug("探测失败: name=%s reason=body_not_object", result.get("name"))
                        return result

                    rate_limit = parsed_body.get("rate_limit") if isinstance(parsed_body, dict) else None
                    primary_window = rate_limit.get("primary_window") if isinstance(rate_limit, dict) else None
                    result["usage_allowed"] = (
                        int(rate_limit.get("allowed"))
                        if isinstance(rate_limit, dict) and isinstance(rate_limit.get("allowed"), bool)
                        else None
                    )
                    result["usage_limit_reached"] = (
                        int(rate_limit.get("limit_reached"))
                        if isinstance(rate_limit, dict) and isinstance(rate_limit.get("limit_reached"), bool)
                        else None
                    )
                    result["usage_plan_type"] = (
                        str(parsed_body.get("plan_type") or "").strip() or None
                        if isinstance(parsed_body, dict)
                        else None
                    )
                    result["usage_email"] = (
                        str(parsed_body.get("email") or "").strip() or None
                        if isinstance(parsed_body, dict)
                        else None
                    )
                    result["usage_reset_at"] = (
                        int(primary_window.get("reset_at"))
                        if isinstance(primary_window, dict) and primary_window.get("reset_at") is not None
                        else None
                    )
                    result["usage_reset_after_seconds"] = (
                        int(primary_window.get("reset_after_seconds"))
                        if isinstance(primary_window, dict) and primary_window.get("reset_after_seconds") is not None
                        else None
                    )

                    if status_code == 200:
                        result["probe_error_kind"] = None
                        result["probe_error_text"] = None
                        LOGGER.debug(
                            "探测完成: name=%s api_http=%s status_code=%s limit_reached=%s allowed=%s",
                            result.get("name"),
                            result.get("api_http_status"),
                            result.get("api_status_code"),
                            result.get("usage_limit_reached"),
                            result.get("usage_allowed"),
                        )
                        return result

                    result["probe_error_kind"] = "other"
                    result["probe_error_text"] = f"unexpected upstream status_code={status_code}"
                    LOGGER.debug(
                        "探测异常: name=%s api_http=%s status_code=%s",
                        result.get("name"),
                        result.get("api_http_status"),
                        status_code,
                    )
                    return result
        except asyncio.TimeoutError:
            result["probe_error_kind"] = "timeout"
            result["probe_error_text"] = "timeout"
            LOGGER.debug("探测超时: name=%s attempt=%s", result.get("name"), attempt + 1)
        except Exception as exc:
            result["probe_error_kind"] = "other"
            result["probe_error_text"] = str(exc)
            LOGGER.debug("探测异常: name=%s error=%s", result.get("name"), exc)

        if attempt >= retries:
            return result

    return result


def classify_account_state(record: dict[str, Any]) -> dict[str, Any]:
    invalid_401 = bool(record.get("unavailable")) or record.get("api_status_code") == 401
    quota_limited = (
        not invalid_401
        and not bool(record.get("unavailable"))
        and record.get("api_status_code") == 200
        and record.get("usage_limit_reached") == 1
    )
    recovered = (
        not invalid_401
        and not quota_limited
        and bool(record.get("disabled"))
        and str(record.get("managed_reason") or "") == "quota_disabled"
        and record.get("api_status_code") == 200
        and record.get("usage_allowed") == 1
        and record.get("usage_limit_reached") == 0
    )

    record["is_invalid_401"] = int(invalid_401)
    record["is_quota_limited"] = int(quota_limited)
    record["is_recovered"] = int(recovered)
    record["updated_at"] = utc_now_iso()
    return record


async def probe_accounts_async(
    records: list[dict[str, Any]],
    *,
    base_url: str,
    token: str,
    timeout: int,
    retries: int,
    user_agent: str,
    probe_workers: int,
    debug: bool,
) -> list[dict[str, Any]]:
    if not records:
        return []

    LOGGER.info(
        "开始并发探测 wham/usage: candidates=%s workers=%s timeout=%ss retries=%s",
        len(records),
        probe_workers,
        timeout,
        retries,
    )

    connector = aiohttp.TCPConnector(limit=max(1, probe_workers), limit_per_host=max(1, probe_workers))
    client_timeout = aiohttp.ClientTimeout(total=max(1, timeout))
    semaphore = asyncio.Semaphore(max(1, probe_workers))

    results: list[dict[str, Any]] = []
    async with aiohttp.ClientSession(connector=connector, timeout=client_timeout, trust_env=True) as session:
        tasks = [
            asyncio.create_task(
                probe_wham_usage_async(
                    session,
                    semaphore,
                    base_url,
                    token,
                    record,
                    timeout,
                    retries,
                    user_agent,
                )
            )
            for record in records
        ]

        done = 0
        total = len(tasks)
        next_report = 100
        with ProgressReporter("探测账号", total, debug=debug) as progress:
            for task in asyncio.as_completed(tasks):
                probed = classify_account_state(await task)
                results.append(probed)
                done += 1
                progress.advance()
                if (not progress.enabled) and (done >= next_report or done == total):
                    LOGGER.info("探测进度: %s/%s", done, total)
                    next_report += 100

    return results


def build_invalid_export_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": record.get("name"),
        "account": record.get("account") or record.get("email") or "",
        "email": record.get("email") or "",
        "provider": record.get("provider"),
        "source": record.get("source"),
        "disabled": bool(record.get("disabled")),
        "unavailable": bool(record.get("unavailable")),
        "auth_index": record.get("auth_index"),
        "chatgpt_account_id": record.get("chatgpt_account_id"),
        "api_http_status": record.get("api_http_status"),
        "api_status_code": record.get("api_status_code"),
        "status": record.get("status"),
        "status_message": record.get("status_message"),
        "probe_error_kind": record.get("probe_error_kind"),
        "probe_error_text": record.get("probe_error_text"),
    }


def build_quota_export_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": record.get("name"),
        "account": record.get("account") or record.get("email") or "",
        "email": record.get("usage_email") or record.get("email") or "",
        "provider": record.get("provider"),
        "source": record.get("source"),
        "disabled": bool(record.get("disabled")),
        "unavailable": bool(record.get("unavailable")),
        "auth_index": record.get("auth_index"),
        "chatgpt_account_id": record.get("chatgpt_account_id"),
        "api_http_status": record.get("api_http_status"),
        "api_status_code": record.get("api_status_code"),
        "limit_reached": bool(record.get("usage_limit_reached")),
        "allowed": bool(record.get("usage_allowed")) if record.get("usage_allowed") is not None else None,
        "plan_type": record.get("usage_plan_type") or record.get("id_token_plan_type"),
        "reset_at": record.get("usage_reset_at"),
        "reset_after_seconds": record.get("usage_reset_after_seconds"),
        "probe_error_kind": record.get("probe_error_kind"),
        "probe_error_text": record.get("probe_error_text"),
    }


def export_records(path: str, rows: list[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)


def summarize_failures(records: list[dict[str, Any]], sample_limit: int = 3) -> None:
    failed = [row for row in records if row.get("probe_error_kind")]
    if not failed:
        return

    labels = {
        "missing_auth_index": "缺少 auth_index",
        "missing_chatgpt_account_id": "缺少 Chatgpt-Account-Id",
        "management_api_http_4xx": "管理接口 HTTP 4xx",
        "management_api_http_5xx": "管理接口 HTTP 5xx",
        "api_call_invalid_json": "api-call 返回不是 JSON",
        "api_call_not_object": "api-call 返回不是对象",
        "missing_status_code": "api-call 缺少 status_code",
        "body_invalid_json": "api-call body 不是合法 JSON",
        "body_not_object": "api-call body 不是对象",
        "timeout": "请求超时",
        "other": "其他异常",
    }
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "samples": []})

    for row in failed:
        key = str(row.get("probe_error_kind") or "other")
        bucket = buckets[key]
        bucket["count"] += 1
        if len(bucket["samples"]) < sample_limit:
            bucket["samples"].append(
                " | ".join(
                    [
                        row.get("name") or "-",
                        f"account={row.get('account') or row.get('email') or '-'}",
                        f"auth_index={row.get('auth_index') or '-'}",
                        f"has_account_id={'yes' if row.get('chatgpt_account_id') else 'no'}",
                        f"api_http={row.get('api_http_status') if row.get('api_http_status') is not None else '-'}",
                        f"status_code={row.get('api_status_code') if row.get('api_status_code') is not None else '-'}",
                        f"error_kind={key}",
                        f"error={compact_text(row.get('probe_error_text'), 100) or '-'}",
                    ]
                )
            )

    LOGGER.info("失败原因统计:")
    for key, payload in sorted(buckets.items(), key=lambda item: (-item[1]["count"], item[0])):
        LOGGER.info("  - %s: %s", labels.get(key, key), payload["count"])

    LOGGER.debug("失败样例:")
    for key, payload in sorted(buckets.items(), key=lambda item: (-item[1]["count"], item[0])):
        LOGGER.debug("  [%s]", labels.get(key, key))
        for sample in payload["samples"]:
            LOGGER.debug("    %s", sample)


async def delete_account_async(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    base_url: str,
    token: str,
    name: str,
    timeout: int,
) -> dict[str, Any]:
    encoded_name = urllib.parse.quote(name, safe="")
    url = f"{base_url.rstrip('/')}/v0/management/auth-files?name={encoded_name}"
    try:
        async with semaphore:
            async with session.delete(url, headers=mgmt_headers(token), timeout=timeout) as resp:
                text = await resp.text()
                data = maybe_json_loads(text)
                ok = resp.status == 200 and isinstance(data, dict) and data.get("status") == "ok"
                return {
                    "name": name,
                    "ok": ok,
                    "status_code": resp.status,
                    "error": None if ok else compact_text(text, 200),
                }
    except Exception as exc:
        return {"name": name, "ok": False, "status_code": None, "error": str(exc)}


async def set_account_disabled_async(
    session: aiohttp.ClientSession,
    semaphore: asyncio.Semaphore,
    base_url: str,
    token: str,
    name: str,
    disabled: bool,
    timeout: int,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/v0/management/auth-files/status"
    payload = {"name": name, "disabled": bool(disabled)}
    try:
        async with semaphore:
            async with session.patch(
                url,
                headers=mgmt_headers(token, include_json=True),
                json=payload,
                timeout=timeout,
            ) as resp:
                text = await resp.text()
                data = maybe_json_loads(text)
                ok = resp.status == 200 and isinstance(data, dict) and data.get("status") == "ok"
                return {
                    "name": name,
                    "ok": ok,
                    "disabled": bool(disabled),
                    "status_code": resp.status,
                    "error": None if ok else compact_text(text, 200),
                }
    except Exception as exc:
        return {
            "name": name,
            "ok": False,
            "disabled": bool(disabled),
            "status_code": None,
            "error": str(exc),
        }


async def run_action_group_async(
    *,
    base_url: str,
    token: str,
    timeout: int,
    workers: int,
    items: list[str],
    fn_name: str,
    disabled: bool | None = None,
    debug: bool = False,
) -> list[dict[str, Any]]:
    if not items:
        return []

    connector = aiohttp.TCPConnector(limit=max(1, workers), limit_per_host=max(1, workers))
    client_timeout = aiohttp.ClientTimeout(total=max(1, timeout))
    semaphore = asyncio.Semaphore(max(1, workers))

    async with aiohttp.ClientSession(connector=connector, timeout=client_timeout, trust_env=True) as session:
        tasks = []
        for name in items:
            if fn_name == "delete":
                tasks.append(
                    asyncio.create_task(delete_account_async(session, semaphore, base_url, token, name, timeout))
                )
            else:
                tasks.append(
                    asyncio.create_task(
                        set_account_disabled_async(
                            session,
                            semaphore,
                            base_url,
                            token,
                            name,
                            bool(disabled),
                            timeout,
                        )
                    )
                )

        results: list[dict[str, Any]] = []
        done = 0
        total = len(tasks)
        next_report = 100
        action_label = "删除" if fn_name == "delete" else ("禁用" if disabled else "启用")
        with ProgressReporter(f"{action_label}账号", total, debug=debug) as progress:
            for task in asyncio.as_completed(tasks):
                results.append(await task)
                done += 1
                progress.advance()
                if (not progress.enabled) and (done >= next_report or done == total):
                    LOGGER.info("%s进度: %s/%s", action_label, done, total)
                    next_report += 100
        return results


def apply_action_results(
    records_by_name: dict[str, dict[str, Any]],
    results: list[dict[str, Any]],
    *,
    action: str,
    managed_reason_on_success: str | None,
    disabled_value: int | None,
) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    now_iso = utc_now_iso()
    for result in results:
        name = result.get("name")
        record = records_by_name.get(name)
        if not record:
            continue
        record["last_action"] = action
        record["last_action_status"] = "success" if result.get("ok") else "failed"
        record["last_action_error"] = result.get("error")
        record["updated_at"] = now_iso
        if result.get("ok"):
            if managed_reason_on_success is None:
                record["managed_reason"] = None
            else:
                record["managed_reason"] = managed_reason_on_success
            if disabled_value is not None:
                record["disabled"] = disabled_value
            LOGGER.debug("动作成功: action=%s name=%s", action, name)
        else:
            LOGGER.debug(
                "动作失败: action=%s name=%s status_code=%s error=%s",
                action,
                name,
                result.get("status_code"),
                compact_text(result.get("error"), 200),
            )
        updated.append(record)
    return updated


def mark_quota_already_disabled(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now_iso = utc_now_iso()
    updated = []
    for record in records:
        record["managed_reason"] = "quota_disabled"
        record["last_action"] = "mark_quota_disabled"
        record["last_action_status"] = "success"
        record["last_action_error"] = None
        record["updated_at"] = now_iso
        LOGGER.debug("标记已禁用限额账号: name=%s", record.get("name"))
        updated.append(record)
    return updated


def print_scan_summary(
    *,
    total_files: int,
    candidate_records: list[dict[str, Any]],
    invalid_records: list[dict[str, Any]],
    quota_records: list[dict[str, Any]],
    recovered_records: list[dict[str, Any]],
) -> None:
    status_counter = Counter(str(row.get("status") or "") for row in candidate_records)
    LOGGER.info("总认证文件数: %s", total_files)
    LOGGER.info("符合过滤条件账号数: %s", len(candidate_records))
    LOGGER.info("401 账号数: %s", len(invalid_records))
    LOGGER.info("限额账号数: %s", len(quota_records))
    LOGGER.info("恢复候选账号数: %s", len(recovered_records))
    LOGGER.debug("状态分布: %s", dict(sorted(status_counter.items(), key=lambda item: item[0])))


def summarize_action_results(label: str, results: list[dict[str, Any]]) -> None:
    if not results:
        LOGGER.info("%s: 0", label)
        return
    success = [row for row in results if row.get("ok")]
    failed = [row for row in results if not row.get("ok")]
    LOGGER.info("%s: 成功=%s，失败=%s", label, len(success), len(failed))
    for row in failed[:10]:
        LOGGER.warning("[%s失败] %s | %s", label, row.get("name"), compact_text(row.get("error"), 160) or "-")


def confirm_action(message: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        LOGGER.warning("缺少交互终端，已取消: %s", message)
        return False
    answer = input(f"{message}，输入 DELETE 确认: ").strip()
    return answer == "DELETE"


def export_current_results(
    invalid_output: str,
    quota_output: str,
    invalid_records: list[dict[str, Any]],
    quota_records: list[dict[str, Any]],
) -> None:
    export_records(invalid_output, [build_invalid_export_record(row) for row in invalid_records])
    export_records(quota_output, [build_quota_export_record(row) for row in quota_records])
    LOGGER.info("已导出 401 列表: %s", invalid_output)
    LOGGER.info("已导出限额列表: %s", quota_output)


async def run_scan_async(conn: sqlite3.Connection, settings: dict[str, Any]) -> dict[str, Any]:
    run_id = start_scan_run(conn, settings)
    LOGGER.info("开始扫描: mode=%s db=%s log=%s", settings["mode"], settings["db_path"], settings["log_file"])
    try:
        now_iso = utc_now_iso()
        files = fetch_auth_files(settings["base_url"], settings["token"], settings["timeout"])
        existing_state = load_existing_state(conn)

        inventory_records = []
        for item in files:
            name = get_item_name(item)
            if not name:
                continue
            inventory_records.append(build_auth_record(item, existing_state.get(name), now_iso))

        upsert_auth_accounts(conn, inventory_records)

        candidate_records = [
            record
            for record in inventory_records
            if matches_filters(record, settings["target_type"], settings["provider"])
        ]
        probed_records = await probe_accounts_async(
            candidate_records,
            base_url=settings["base_url"],
            token=settings["token"],
            timeout=settings["timeout"],
            retries=settings["retries"],
            user_agent=settings["user_agent"],
            probe_workers=settings["probe_workers"],
            debug=settings["debug"],
        )
        probed_map = {record["name"]: record for record in probed_records}

        for record in inventory_records:
            if record["name"] in probed_map:
                record.update(probed_map[record["name"]])
                record["updated_at"] = utc_now_iso()

        upsert_auth_accounts(conn, inventory_records)

        current_candidates = [record for record in inventory_records if matches_filters(record, settings["target_type"], settings["provider"])]
        invalid_records = [row for row in current_candidates if row.get("is_invalid_401") == 1]
        quota_records = [row for row in current_candidates if row.get("is_quota_limited") == 1]
        recovered_records = [row for row in current_candidates if row.get("is_recovered") == 1]
        failure_records = [row for row in current_candidates if row.get("probe_error_kind")]
        probed_files = sum(1 for row in current_candidates if row.get("last_probed_at"))

        finish_scan_run(
            conn,
            run_id,
            status="success",
            total_files=len(files),
            filtered_files=len(current_candidates),
            probed_files=probed_files,
            invalid_401_count=len(invalid_records),
            quota_limited_count=len(quota_records),
            recovered_count=len(recovered_records),
        )

        print_scan_summary(
            total_files=len(files),
            candidate_records=current_candidates,
            invalid_records=invalid_records,
            quota_records=quota_records,
            recovered_records=recovered_records,
        )
        if failure_records:
            summarize_failures(failure_records)
        export_current_results(settings["invalid_output"], settings["quota_output"], invalid_records, quota_records)
        LOGGER.info("扫描完成")

        return {
            "run_id": run_id,
            "all_records": inventory_records,
            "candidate_records": current_candidates,
            "invalid_records": invalid_records,
            "quota_records": quota_records,
            "recovered_records": recovered_records,
        }
    except Exception:
        finish_scan_run(
            conn,
            run_id,
            status="failed",
            total_files=0,
            filtered_files=0,
            probed_files=0,
            invalid_401_count=0,
            quota_limited_count=0,
            recovered_count=0,
        )
        raise


async def run_maintain_async(conn: sqlite3.Connection, settings: dict[str, Any]) -> dict[str, Any]:
    LOGGER.info(
        "开始维护: delete_401=%s quota_action=%s auto_reenable=%s",
        settings["delete_401"],
        settings["quota_action"],
        settings["auto_reenable"],
    )
    scan_result = await run_scan_async(conn, settings)

    records_by_name = {
        row["name"]: row
        for row in scan_result["candidate_records"]
        if row.get("name")
    }
    invalid_records = scan_result["invalid_records"]
    quota_records = [row for row in scan_result["quota_records"] if row.get("is_invalid_401") != 1]
    recovered_records = [
        row
        for row in scan_result["recovered_records"]
        if row.get("is_invalid_401") != 1 and row.get("is_quota_limited") != 1
    ]

    delete_401_results: list[dict[str, Any]] = []
    quota_action_results: list[dict[str, Any]] = []
    reenable_results: list[dict[str, Any]] = []

    if settings["delete_401"] and invalid_records:
        names = [row["name"] for row in invalid_records if row.get("name")]
        LOGGER.info("待删除 401 账号: %s", len(names))
        if confirm_action(f"即将删除 {len(names)} 个 401 账号", settings["assume_yes"]):
            delete_401_results = await run_action_group_async(
                base_url=settings["base_url"],
                token=settings["token"],
                timeout=settings["timeout"],
                workers=settings["action_workers"],
                items=names,
                fn_name="delete",
                debug=settings["debug"],
            )
            updated = apply_action_results(
                records_by_name,
                delete_401_results,
                action="delete_401",
                managed_reason_on_success="deleted_401",
                disabled_value=None,
            )
            upsert_auth_accounts(conn, updated)

    deleted_401_names = {row["name"] for row in delete_401_results if row.get("ok")}

    if settings["quota_action"] == "disable":
        already_disabled = [row for row in quota_records if row.get("name") not in deleted_401_names and row.get("disabled") == 1]
        to_disable = [row for row in quota_records if row.get("name") not in deleted_401_names and row.get("disabled") != 1]
        LOGGER.info("待禁用限额账号: %s", len(to_disable))
        LOGGER.debug("已处于禁用状态的限额账号: %s", len(already_disabled))

        if already_disabled:
            upsert_auth_accounts(conn, mark_quota_already_disabled(already_disabled))

        if to_disable:
            quota_action_results = await run_action_group_async(
                base_url=settings["base_url"],
                token=settings["token"],
                timeout=settings["timeout"],
                workers=settings["action_workers"],
                items=[row["name"] for row in to_disable if row.get("name")],
                fn_name="toggle",
                disabled=True,
                debug=settings["debug"],
            )
            updated = apply_action_results(
                records_by_name,
                quota_action_results,
                action="disable_quota",
                managed_reason_on_success="quota_disabled",
                disabled_value=1,
            )
            upsert_auth_accounts(conn, updated)
    else:
        quota_delete_targets = [row["name"] for row in quota_records if row.get("name") and row.get("name") not in deleted_401_names]
        LOGGER.info("待删除限额账号: %s", len(quota_delete_targets))
        if quota_delete_targets and confirm_action(f"即将删除 {len(quota_delete_targets)} 个限额账号", settings["assume_yes"]):
            quota_action_results = await run_action_group_async(
                base_url=settings["base_url"],
                token=settings["token"],
                timeout=settings["timeout"],
                workers=settings["action_workers"],
                items=quota_delete_targets,
                fn_name="delete",
                debug=settings["debug"],
            )
            updated = apply_action_results(
                records_by_name,
                quota_action_results,
                action="delete_quota",
                managed_reason_on_success="quota_deleted",
                disabled_value=None,
            )
            upsert_auth_accounts(conn, updated)

    deleted_quota_names = {row["name"] for row in quota_action_results if row.get("ok") and settings["quota_action"] == "delete"}

    if settings["auto_reenable"]:
        reenable_targets = [
            row["name"]
            for row in recovered_records
            if row.get("name") not in deleted_401_names and row.get("name") not in deleted_quota_names
        ]
        LOGGER.info("待恢复启用账号: %s", len(reenable_targets))
        if reenable_targets:
            reenable_results = await run_action_group_async(
                base_url=settings["base_url"],
                token=settings["token"],
                timeout=settings["timeout"],
                workers=settings["action_workers"],
                items=reenable_targets,
                fn_name="toggle",
                disabled=False,
                debug=settings["debug"],
            )
            updated = apply_action_results(
                records_by_name,
                reenable_results,
                action="reenable_quota",
                managed_reason_on_success=None,
                disabled_value=0,
            )
            upsert_auth_accounts(conn, updated)

    summarize_action_results("删除 401", delete_401_results)
    summarize_action_results("处理限额", quota_action_results)
    summarize_action_results("恢复启用", reenable_results)
    LOGGER.info("维护完成")

    return {
        "scan": scan_result,
        "delete_401_results": delete_401_results,
        "quota_action_results": quota_action_results,
        "reenable_results": reenable_results,
    }


def prompt_string(label: str, default: str, *, secret: bool = False) -> str:
    shown_default = default or "空"
    prompt = f"{label}（默认 {shown_default}）: "
    raw = getpass.getpass(prompt) if secret else input(prompt)
    raw = raw.strip()
    return raw or default


def prompt_int(label: str, default: int, *, min_value: int = 0) -> int:
    raw = input(f"{label}（默认 {default}）: ").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except Exception:
        print("输入无效，使用默认值。")
        return default
    if value < min_value:
        print(f"输入过小，使用最小值 {min_value}。")
        return min_value
    return value


def prompt_yes_no(label: str, default: bool) -> bool:
    default_text = "yes" if default else "no"
    raw = input(f"{label}（默认 {default_text}）[yes/no]: ").strip().lower()
    if not raw:
        return default
    if raw in {"y", "yes"}:
        return True
    if raw in {"n", "no"}:
        return False
    print("输入无效，使用默认值。")
    return default


def prompt_choice(label: str, options: list[str], default: str) -> str:
    raw = input(f"{label}（默认 {default}）[{ '/'.join(options) }]: ").strip().lower()
    if not raw:
        return default
    if raw in options:
        return raw
    print("输入无效，使用默认值。")
    return default


def choose_mode_interactive() -> str:
    print("\n请选择操作:")
    print("1) scan - 检测 401 和限额并导出")
    print("2) maintain - 删除 401、处理限额、恢复账号")
    print("0) exit")
    while True:
        choice = input("请输入选项编号: ").strip()
        if choice == "1":
            return "scan"
        if choice == "2":
            return "maintain"
        if choice == "0":
            return "exit"
        print("无效选项，请重新输入。")


def prompt_interactive_settings(settings: dict[str, Any]) -> dict[str, Any]:
    settings["probe_workers"] = prompt_int("请输入探测并发 probe_workers", settings["probe_workers"], min_value=1)
    settings["timeout"] = prompt_int("请输入请求超时 timeout(秒)", settings["timeout"], min_value=1)
    settings["retries"] = prompt_int("请输入失败重试 retries", settings["retries"], min_value=0)
    settings["target_type"] = prompt_string("请输入 target_type", settings["target_type"])
    settings["provider"] = prompt_string("请输入 provider", settings["provider"])
    settings["db_path"] = prompt_string("请输入 SQLite 路径 db_path", settings["db_path"])
    settings["invalid_output"] = prompt_string("请输入 401 导出路径 invalid_output", settings["invalid_output"])
    settings["quota_output"] = prompt_string("请输入限额导出路径 quota_output", settings["quota_output"])
    settings["log_file"] = prompt_string("请输入日志文件路径 log_file", settings["log_file"])
    settings["debug"] = prompt_yes_no("是否开启调试模式 debug", settings["debug"])

    if settings["mode"] == "maintain":
        settings["action_workers"] = prompt_int("请输入动作并发 action_workers", settings["action_workers"], min_value=1)
        settings["delete_401"] = prompt_yes_no("是否自动删除 401 账号", settings["delete_401"])
        settings["quota_action"] = prompt_choice("限额账号动作 quota_action", ["disable", "delete"], settings["quota_action"])
        settings["auto_reenable"] = prompt_yes_no("是否自动重新启用恢复账号", settings["auto_reenable"])
        settings["assume_yes"] = prompt_yes_no("是否跳过危险操作确认", settings["assume_yes"])

    return settings


def ensure_credentials(settings: dict[str, Any], interactive: bool) -> None:
    if not settings["base_url"] and interactive:
        settings["base_url"] = prompt_string("请输入 CPA base_url", "")
    if not settings["token"] and interactive:
        settings["token"] = prompt_string("请输入 CPA 管理 token", "", secret=True)

    settings["base_url"] = settings["base_url"].rstrip("/")

    if not settings["base_url"]:
        raise RuntimeError("缺少 base_url，请在配置文件中提供。")
    if not settings["token"]:
        raise RuntimeError("缺少 token，请在配置文件中提供。")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="基于 auth-files + wham/usage 的交互式 CPA 账号维护脚本")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="配置文件路径（默认: config.json）")
    parser.add_argument("--mode", choices=["scan", "maintain"], help="运行模式")
    parser.add_argument("--target-type", help="按 files[].type 过滤")
    parser.add_argument("--provider", help="按 provider 过滤")
    parser.add_argument("--probe-workers", type=int, help="api-call 探测并发")
    parser.add_argument("--action-workers", type=int, help="删除/禁用/启用并发")
    parser.add_argument("--timeout", type=int, help="请求超时秒数")
    parser.add_argument("--retries", type=int, help="单账号探测失败重试次数")
    parser.add_argument("--user-agent", help="wham/usage 探测使用的 User-Agent")
    parser.add_argument("--quota-action", choices=["disable", "delete"], help="限额账号处理动作")
    parser.add_argument("--db-path", help="SQLite 状态库路径")
    parser.add_argument("--invalid-output", help="401 导出文件路径")
    parser.add_argument("--quota-output", help="限额导出文件路径")
    parser.add_argument("--log-file", help="日志文件路径")
    parser.add_argument("--debug", action="store_true", help="开启调试模式，在终端打印更详细信息")

    delete_group = parser.add_mutually_exclusive_group()
    delete_group.add_argument("--delete-401", dest="delete_401", action="store_true", help="维护模式下自动删除 401")
    delete_group.add_argument("--no-delete-401", dest="delete_401", action="store_false", help="维护模式下不删除 401")

    reenable_group = parser.add_mutually_exclusive_group()
    reenable_group.add_argument("--auto-reenable", dest="auto_reenable", action="store_true", help="维护模式下自动启用恢复账号")
    reenable_group.add_argument("--no-auto-reenable", dest="auto_reenable", action="store_false", help="维护模式下不自动启用恢复账号")

    parser.set_defaults(delete_401=None, auto_reenable=None, debug=None)
    parser.add_argument("--yes", action="store_true", help="跳过删除确认")
    return parser.parse_args()


def run_async_or_exit(coro: Any) -> Any:
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        LOGGER.error("已中断。")
        sys.exit(130)
    except Exception as exc:
        LOGGER.error("错误: %s", exc)
        sys.exit(1)


def main() -> int:
    args = parse_args()
    ensure_aiohttp()

    interactive = False
    if args.mode is None:
        if not sys.stdin.isatty():
            print("错误: 未指定 --mode，且当前不是交互终端。", file=sys.stderr)
            return 1
        interactive = True
        mode = choose_mode_interactive()
        if mode == "exit":
            print("已退出。")
            return 0
        args.mode = mode
        args.config = prompt_string("配置文件路径", args.config)

    config_required = "--config" in sys.argv and not interactive
    conf = load_config_json(args.config, required=config_required)
    settings = build_settings(args, conf)

    if interactive:
        settings = prompt_interactive_settings(settings)

    configure_logging(settings["log_file"], settings["debug"])
    LOGGER.info("日志文件: %s", settings["log_file"])
    LOGGER.info("调试模式: %s", "on" if settings["debug"] else "off")

    ensure_credentials(settings, interactive)
    LOGGER.debug(
        "运行参数: mode=%s target_type=%s provider=%s probe_workers=%s action_workers=%s timeout=%s retries=%s quota_action=%s delete_401=%s auto_reenable=%s db_path=%s",
        settings["mode"],
        settings["target_type"],
        settings["provider"] or "",
        settings["probe_workers"],
        settings["action_workers"],
        settings["timeout"],
        settings["retries"],
        settings["quota_action"],
        settings["delete_401"],
        settings["auto_reenable"],
        settings["db_path"],
    )

    conn = connect_db(settings["db_path"])
    try:
        init_db(conn)
        if settings["mode"] == "scan":
            run_async_or_exit(run_scan_async(conn, settings))
            return 0
        run_async_or_exit(run_maintain_async(conn, settings))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
