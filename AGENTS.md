# Repository Guidelines

## Project Structure & Module Organization

This repository contains a Python library/CLI and a Home Assistant custom integration for controlling Chihiros LEDs over Bluetooth LE.

- `src/chihiros_led_control/`: reusable library code and the `chihirosctl` Typer CLI.
- `custom_components/chihiros/`: Home Assistant integration files, translations, and manifest.
- `custom_components/chihiros/vendor/`: vendored copy of the library used by HACS installs.
- `tests/`: pytest tests for protocol encoding, factory behavior, weekday encoding, and vendor sync.
- `scripts/sync_vendor.py`: copies package code into the integration vendor directory.
- `docs/`: architecture and local Home Assistant Docker setup notes.
- `dev/homeassistant/`: local Home Assistant configuration used with Docker Compose.

## Build, Test, and Development Commands

- `uv sync --group dev`: install development dependencies.
- `uv run --group dev pytest`: run the test suite.
- `uv run --group dev pre-commit run --all-files`: run formatting, linting, doc, YAML/TOML, and AST checks.
- `uv run chihirosctl --help`: inspect CLI commands after syncing dependencies.
- `uv run python scripts/sync_vendor.py`: refresh `custom_components/chihiros/vendor/` after library changes.
- `uv run python scripts/sync_vendor.py --check`: verify the vendored copy is current.
- `docker compose up`: start the local Home Assistant environment; see `docs/home-assistant-docker.md`.

## Coding Style & Naming Conventions

Target Python 3.13. Use 4-space indentation, type hints for public APIs, and descriptive snake_case names for modules, functions, and variables. Classes should use PascalCase; constants should use UPPER_SNAKE_CASE.

Ruff is the formatter and linter. The configured line length is 120 characters, and lint rules include docstrings, pycodestyle, pyflakes, imports, and warnings. The vendored integration copy is excluded from Ruff checks; edit `src/chihiros_led_control/` first, then sync vendor code.

## Testing Guidelines

Tests use pytest and live in `tests/` with `test_*.py` names. Prefer focused unit tests for command encoding, protocol behavior, model/factory changes, and Home Assistant-facing compatibility. When changing vendored behavior, run both `pytest` and `scripts/sync_vendor.py --check`.

## Commit & Pull Request Guidelines

Git history uses short imperative subjects, with occasional Conventional Commit prefixes such as `fix:`. Keep commits focused, for example `fix: relax bleak version pin` or `add model code for wrgb2 slim`.

Pull requests should describe the user-visible change, list validation commands run, link related issues, and include screenshots only for Home Assistant UI changes. If library code changes, mention whether the vendored copy was refreshed.

## Security & Configuration Tips

Do not commit Bluetooth device addresses, Home Assistant secrets, tokens, or local `.venv` contents. Keep dependency changes in `pyproject.toml` and `uv.lock` together.
