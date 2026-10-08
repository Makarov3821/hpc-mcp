# Repository Guidelines

## Project Structure & Module Organization

This workspace is currently an initial scaffold: no application source, tests, assets, or package manifests are present. The `.agents/`, `.codex/`, `.aws/`, and `.git/` directories are environment-managed locations; do not modify them as part of ordinary contributions.

When adding the first implementation, organize application code in `src/`, automated tests in `tests/`, and static resources in `assets/` where appropriate. Group modules by responsibility and document the actual layout in a root `README.md`.

## Build, Test, and Development Commands

No build, test, lint, or local development commands are configured yet. Do not assume commands such as `npm test` or `make build` are available.

When introducing a toolchain, include its manifest and dependency lockfile, and document exact installation, development, build, and test commands in `README.md`. Prefer repository-local scripts so contributors can reproduce checks consistently.

## Coding Style & Naming Conventions

No language-specific style or formatter has been established. Follow the conventions of the chosen language, use consistent indentation, and select a formatter and linter alongside the initial implementation.

Use descriptive module and function names. Keep filenames consistent within each language and avoid introducing competing naming styles. Keep changes focused and avoid unrelated reformatting.

## Testing Guidelines

No testing framework or coverage threshold is configured. Add tests with new behavior and regression tests with bug fixes. Use descriptive names that identify the behavior being verified, following the selected framework's discovery rules.

Document how to run the full suite and individual tests. Keep tests deterministic and independent of credentials or live external services wherever possible.

## Commit & Pull Request Guidelines

Git history is unavailable in this workspace, so no existing commit convention can be inferred. Use concise, imperative commit subjects, such as `Add configuration loader`.

Pull requests should describe the change, its purpose, and validation performed. Link relevant issues and include screenshots for visual changes. Explain any configuration changes and explicitly state when checks could not be run.

## Security & Configuration

Never commit credentials, tokens, or private configuration. Provide placeholder values in example configuration files and document required environment variables.
