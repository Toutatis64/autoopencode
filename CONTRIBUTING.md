# Contributing to AutoOpencode

Thank you for considering contributing to AutoOpencode! We welcome contributions of all kinds, including bug fixes, features, documentation improvements, and tests.

## Code of conduct

This project adheres to the [Contributor Covenant](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code.

## How to contribute

### 1. Find or create an issue

- Browse [open issues](https://github.com/Toutatis64/autoopencode/issues) for something to work on.
- If you have a new idea or found a bug, [open a new issue](https://github.com/Toutatis64/autoopencode/issues/new) before writing code.

### 2. Fork and branch

```bash
git checkout -b my-feature-branch
```

### 3. Make your changes

- Follow the existing code style (run `make lint` before committing).
- Add tests for any new functionality.
- Ensure all existing tests pass (`make test`).
- Run the full validation suite: `make full`.

### 4. Commit

Write clear, concise commit messages. Reference the issue number if applicable:

```
feat: add support for custom module directories

Closes #42
```

### 5. Submit a pull request

- Push your branch and [open a PR](https://github.com/Toutatis64/autoopencode/compare).
- Fill out the PR template with a clear description of your changes.
- Keep PRs focused — one feature or fix per PR.

## Development setup

```bash
git clone https://github.com/Toutatis64/autoopencode.git
cd autocode
pip install pyyaml mypy ruff pytest
make test
```

## Project structure

| Path | Purpose |
|---|---|
| `.opencode/autopilot/` | Core engine: `run_autopilot.py`, `self_improving_loop.py`, `meta_autopilot.py` |
| `scripts/` | Symlinked copies of the autopilot engine (for deployment) |
| `tests/` | Pytest test suite |
| `knowledge/` | Knowledge base YAML files |
| `template/` | Goal and config templates |

## Validation commands

```bash
make test   # pytest -x -q
make check  # mypy scripts/ tests/
make lint   # ruff check scripts/ tests/
make build  # Verify imports
make full   # lint + check + test (everything)
```

## Questions?

Open a [Discussion](https://github.com/Toutatis64/autoopencode/discussions) or reach out via issues.
