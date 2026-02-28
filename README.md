# cpa-warden

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![uv](https://img.shields.io/badge/deps-uv-6f42c1)

[简体中文](README.zh-CN.md)

`cpa-warden` is an interactive CPA account maintenance tool built around two CPA management APIs:

- `GET /v0/management/auth-files`
- `POST /v0/management/api-call` -> `GET https://chatgpt.com/backend-api/wham/usage`

The script reads the current auth inventory, stores state in SQLite, probes usage concurrently, and classifies accounts as:

- `401` invalid
- `quota limited`
- `recovered` from a previous quota-disabled state

## Project Status

This project is usable for local CPA account operations and is being prepared for open source usage.

Current focus:

- stable `scan` workflow
- safe `maintain` workflow
- clean external configuration for secrets
- concise production output with actionable logs

## Features

- External configuration only for sensitive values such as `base_url` and `token`
- Interactive mode by default
- `scan` mode for detection and export
- `maintain` mode for delete / disable / re-enable actions
- Concurrent `wham/usage` probing
- SQLite state tracking
- Short production output with optional Rich progress bar
- Verbose debug mode with full logs written to file

## Classification Rules

- `401`: `unavailable == true` or `api-call.status_code == 401`
- `quota limited`: `api-call.status_code == 200` and `body.rate_limit.limit_reached == true`
- `recovered`: previously marked as `quota_disabled`, and now `allowed == true` and `limit_reached == false`

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

## Installation

```bash
uv sync
```

## Configuration

Do not put sensitive values in code. Copy the example config first:

```bash
cp config.example.json config.json
```

Then edit `config.json` and provide at least:

- `base_url`
- `token`

`config.json` is ignored by git and should not be committed.

Example:

```json
{
  "base_url": "https://your-cpa.example.com",
  "token": "replace-with-your-management-token",
  "target_type": "codex",
  "provider": "",
  "probe_workers": 40,
  "action_workers": 20,
  "timeout": 15,
  "retries": 1,
  "quota_action": "disable",
  "delete_401": true,
  "auto_reenable": true,
  "db_path": "cpa_warden_state.sqlite3",
  "invalid_output": "cpa_warden_401_accounts.json",
  "quota_output": "cpa_warden_quota_accounts.json",
  "log_file": "cpa_warden.log",
  "debug": false,
  "user_agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"
}
```

## Usage

Interactive mode:

```bash
uv run python cpa_warden.py
```

CLI mode:

```bash
uv run python cpa_warden.py --mode scan
uv run python cpa_warden.py --mode scan --debug
uv run python cpa_warden.py --mode maintain
uv run python cpa_warden.py --mode maintain --quota-action delete --yes
uv run python cpa_warden.py --mode maintain --no-delete-401
```

## Modes

### `scan`

This mode:

- fetches all auth files
- probes `wham/usage` concurrently
- updates the local SQLite database
- exports current `401` and quota-limited accounts

### `maintain`

This mode runs `scan` first, then performs actions:

- delete `401` accounts if enabled
- disable or delete quota-limited accounts
- re-enable recovered accounts if enabled

## Roadmap

- Improve failure reporting and error categorization
- Add automated tests for probe classification and action flows
- Add CI for linting and smoke checks
- Expand export/report formats for operational review
- Continue simplifying open-source onboarding and documentation

## Output Files

- `cpa_warden_state.sqlite3`: local state database
- `cpa_warden_401_accounts.json`: current `401` export
- `cpa_warden_quota_accounts.json`: current quota-limited export
- `cpa_warden.log`: runtime log file

## Logging

- Production mode keeps terminal output short
- If the terminal supports TTY, production mode prefers a Rich progress bar
- `--debug` or `debug: true` enables more detailed terminal logs
- The log file always keeps full debug-level details

## Project Structure

- `cpa_warden.py`: main script
- `clean_codex_accounts.py`: compatibility wrapper for the old command
- `config.example.json`: example configuration
- `pyproject.toml`: project metadata and dependencies for `uv`

## Security Notes

- Never commit `config.json`
- Never commit real tokens or account identifiers
- Keep logs and exported JSON files local if they contain operational data

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT. See [LICENSE](LICENSE).
