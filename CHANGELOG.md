# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Interactive `scan` and `maintain` workflows
- External JSON configuration for CPA `base_url` and `token`
- Concurrent `wham/usage` probing through CPA `api-call`
- SQLite state database for auth inventory and probe results
- Rich progress bar support for production runs
- Debug logging with full file log output
- English and Simplified Chinese README files
- MIT license and contribution guide
- GitHub issue templates and pull request template
- GitHub Actions CI workflow for dependency sync and smoke checks

### Changed

- Reworked account classification around `auth-files` and `wham/usage`
- Kept production terminal output short while preserving detailed debug logs
- Renamed the project identity from `cpa-clean` to `cpa-warden`
