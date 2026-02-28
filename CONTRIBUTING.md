# Contributing

Thanks for contributing to `cpa-warden`.

## Before You Start

- Use `uv` for dependency management
- Keep sensitive values out of code and commits
- Do not commit local runtime artifacts such as `config.json`, SQLite databases, logs, or exported account lists

## Development Setup

```bash
uv sync
```

Run the script:

```bash
uv run python cpa_warden.py --mode scan
```

## Contribution Guidelines

- Keep changes focused and easy to review
- Prefer small pull requests
- Preserve the external-config design for secrets
- Update documentation when behavior or CLI options change
- Keep terminal output concise in production mode
- Put verbose troubleshooting details behind debug logging

## Validation

At minimum, before opening a pull request, run:

```bash
uv run python -m py_compile cpa_warden.py clean_codex_accounts.py
uv run python cpa_warden.py --help
```

If your change affects runtime behavior, test against your own local CPA environment and sanitize any output before sharing.

## Pull Requests

- Describe the problem and the behavior change
- Mention any config or output format changes
- Include sample output only after removing secrets and account identifiers where necessary

## Security

- Never commit real `token` values
- Never commit real account identifiers unless absolutely required and explicitly sanitized
- If you find a security issue, avoid posting sensitive details in a public issue

## License

By contributing to this project, you agree that your contributions will be licensed under the MIT License.
