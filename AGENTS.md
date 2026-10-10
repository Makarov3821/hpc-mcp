# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `src/hpc_mcp/`, automated tests in `tests/`, usage and interface documentation in `README.md` and `docs/`, and runnable examples in `examples/`. The `.agents/`, `.codex/`, `.aws/`, and `.git/` directories are environment-managed locations; do not modify them as part of ordinary contributions.

Group modules by responsibility and keep the root `README.md` aligned with the actual layout.

## Build, Test, and Development Commands

Install with `python3 -m venv .venv` and `.venv/bin/python -m pip install -c requirements-mcp.lock -e '.[mcp]'`. Run the complete suite with `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`; run an individual module with `PYTHONPATH=src:tests .venv/bin/python -m unittest test_jobs -v`. The MCP extra is required for protocol checks.

Keep `pyproject.toml`, `requirements-mcp.lock`, and the installation and development commands in `README.md` consistent.

## Coding Style & Naming Conventions

Follow the existing Python style with four-space indentation. Ruff targets Python 3.11 and a 100-character line length in `pyproject.toml`; no repository lint script is configured.

Use descriptive module and function names. Keep filenames consistent within each language and avoid introducing competing naming styles. Keep changes focused and avoid unrelated reformatting.

## Testing Guidelines

Tests use Python unittest; no coverage threshold is configured. Add tests with new behavior and regression tests with bug fixes. Use descriptive test names following unittest discovery rules.

Document how to run the full suite and individual tests. Keep tests deterministic and independent of credentials or live external services wherever possible.

## Commit & Pull Request Guidelines

Use concise, imperative commit subjects, such as `Add configuration loader`.

Pull requests should describe the change, its purpose, and validation performed. Link relevant issues and include screenshots for visual changes. Explain any configuration changes and explicitly state when checks could not be run.

## Security & Configuration

Never commit credentials, tokens, or private configuration. Provide placeholder values in example configuration files and document required environment variables.
