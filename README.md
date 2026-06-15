# AutoOpencode

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](pyproject.toml)
[![Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Mypy](https://img.shields.io/badge/types-mypy-blue)](http://mypy-lang.org/)

AutoOpencode is a **drop-in autonomous improvement loop** for any repository. It turns an opencode agent into a self-improving, long-running worker that identifies improvements, implements them, validates them, and records results — then starts the next iteration.

**Any language. Any framework. Any repo.** Built-in presets for Python, TypeScript/Node, Rust, Go, and generic projects.

---

## How it works

```
bash setup.sh → ./run_autopilot.sh → perpetual improvement
                   ↓
           python3 kpi.py → HTML dashboard
```

| Layer | What it does | How |
|---|---|---|
| **Autopilot** (Level 0) | Improves your codebase | Runs `goal-autopilot` agent in bounded iterations with SQLite memory, FTS5 retrieval, checkpoint parsing, retry logic |
| **Self-Improving Loop** | Diagnoses bottlenecks | Phase classification, creative divergence engine, meta-controller that auto-tunes loop parameters |
| **Meta-Autopilot** (Level 1) | Improves the autopilot itself | Component registry, A/B experiments, mutation strategies |
| **KPI Dashboard** | Tracks progress over time | Reads SQLite + KB, generates HTML reports with metrics, trends, family distribution |

## Quick start

```bash
# From your project root:
bash /path/to/autocode/setup.sh

# Follow the prompts (it asks about your language/stack), then:
./run_autopilot.sh                 # Start improving
./run_meta_autopilot.sh            # Improve the autopilot itself
python3 .opencode/autopilot/kpi.py # View KPI dashboard
```

### Session goals (steering the loop)

```bash
# Inline session goal (single use):
./run_autopilot.sh --session-goal "Fix the broken Stripe webhook handler"

# File-based session goal (re-read each iteration — edit it while the loop runs):
echo "Add input validation to user registration" > .opencode/autopilot/session.md
./run_autopilot.sh --session-goal-file .opencode/autopilot/session.md
```

## Project-type presets

Select your language during setup and get sensible defaults:

| Type | Test | Typecheck | Lint | Default modules |
|---|---|---|---|---|
| **Python** | `pytest` | `mypy .` | `ruff check .` | src, tests, scripts, docs, data |
| **Node/TS** | `npm test` | `tsc --noEmit` | `npm run lint` | api, web, shared, database, tests |
| **Rust** | `cargo test` | `cargo check` | `cargo clippy` | src, tests, benches, examples |
| **Go** | `go test ./...` | `go vet ./...` | `golangci-lint run` | cmd, pkg, internal, api |
| **Generic** | `make test` | `make check` | `make lint` | core, tests, docs, scripts, config |

Every default can be overridden during setup or by editing `autocode.yaml`.

## What you get

```
your-project/
├── autocode.yaml                 # 🔧 Your config
├── opencode.json                 # Opencode configuration
├── AGENTS.md                     # Agent instructions
├── run_autopilot.sh              # Launcher
├── run_meta_autopilot.sh         # Meta-autopilot launcher
└── .opencode/
    ├── agents/
    │   ├── goal-autopilot.agent.md
    │   └── goal-meta-autopilot.agent.md
    ├── autopilot/
    │   ├── run_autopilot.py          # Loop engine
    │   ├── self_improving_loop.py    # Phase diagnostics, divergence
    │   ├── meta_autopilot.py         # Component registry, experiments
    │   ├── autocode_config.py        # Config loader
    │   ├── kpi.py                    # 📊 Dashboard generator
    │   ├── goal.md                   # 🎯 Your goal
    │   └── components/               # Versioned configs for A/B testing
    ├── instructions/
    └── skills/
        ├── long-horizon-autonomy/
        └── prod-diagnose/
```

## Configuration

Everything is driven by `autocode.yaml` — edit it at any time:

```yaml
project:
  name: "My App"
  type: "python"                     # python | node_ts | rust | go | generic

validation:
  default: "pytest"                  # Your test command
  typecheck: "mypy ."                # Your typecheck command
  lint: "ruff check ."               # Your linter command

modules:                             # Branch families (for classification)
  - name: src
    keywords: ["src", "module", "package"]
  - name: tests
    keywords: ["test", "pytest", "spec"]

conventions:
  - "Tests for every fix: regression test that fails before the fix"
```

## KPI Dashboard

Autopilot activity is automatically tracked in SQLite. Generate a dashboard:

```bash
python3 .opencode/autopilot/kpi.py
```

This produces a self-contained HTML file showing aggregate metrics, run scorecards, family distribution charts, trend visualization, and KB summaries. Dashboards are saved to `.opencode/autopilot/kpi/`.

## Components (Meta-Autopilot)

The meta-autopilot treats every part of the autopilot as a **component** that can be versioned, mutated, and A/B tested:

| Component type | What it controls |
|---|---|
| `prompts` | Agent instructions, goal formulations |
| `parameters` | Loop timing, retry logic, parallelism |
| `algorithms` | Family inference, bottleneck detection |
| `modules` | Python code modules |

## Architecture decisions

- **One session per iteration**: fresh `opencode run` each time, context reset and rebuilt from SQLite.
- **Disk-backed memory**: SQLite with FTS5, tag-based relevance scoring, cross-run memory import.
- **Self-improving**: phase bottleneck analysis, creative divergence, meta-controller auto-tunes parameters.
- **Domain-agnostic**: all project config in `autocode.yaml`. Engine never hardcodes paths, frameworks, or language-specific values.
- **Config-driven family inference**: branch classification reads module keywords from your `autocode.yaml` — works for any project structure.

## Requirements

- Python 3.10+
- PyYAML (`pip install pyyaml`)
- opencode CLI installed and configured

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for details on our code of conduct and the process for submitting pull requests.

## License

Apache 2.0. See [LICENSE](LICENSE).
