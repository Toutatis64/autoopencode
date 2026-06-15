# OpenAutopilot — Setup Guide

## Prerequisites

- Python 3.10+ with `pyyaml` (`pip install pyyaml`)
- `opencode` CLI installed and configured
- A git repository you want to improve

## Installation

```bash
cd /path/to/your-project
bash /path/to/autocode/setup.sh
```

The script will guide you through:

### Step 1: Project type
Select your language/stack. This sets sensible defaults for validation commands and module areas:

| Option | Test | Typecheck | Lint | Modules |
|---|---|---|---|---|
| **Python** | `pytest` | `mypy .` | `ruff check .` | src, tests, scripts, docs, data |
| **Node/TS** | `npm test` | `tsc --noEmit` | `npm run lint` | api, web, shared, database, tests |
| **Rust** | `cargo test` | `cargo check` | `cargo clippy` | src, tests, benches, examples |
| **Go** | `go test ./...` | `go vet ./...` | `golangci-lint run` | cmd, pkg, internal, api |
| **Generic** | `make test` | `make check` | `make lint` | core, tests, docs, scripts, config |

### Step 2: Project details
Name and description (used in goal files and agent prompts).

### Step 3: Validation commands
Override the defaults if your project uses different commands.

### Step 4: Module areas
Comma-separated list of your repo's main areas (used for branch classification).
The autopilot classifies each iteration into one of these families.

### Step 5: Conventions
Non-negotiable rules the autopilot must follow (one per line, empty line to finish).

### Step 6: Project goals
High-level direction for the autopilot loop (persistent across sessions).
Describe what your project aims to achieve and the principles to follow.

## Post-installation

### 1. Review `autocode.yaml`

```yaml
project:
  type: "python"       # ← change to switch presets

validation:
  default: "pytest"    # ← your commands
  typecheck: "mypy ."

modules:               # ← branch families
  - name: src
    keywords: ["src", "module"]
  - name: tests
    keywords: ["test", "pytest"]
```

### 2. Edit project goals (optional)

```bash
# Edit autocode.yaml → project_goals to set the persistent direction
# Or edit .opencode/autopilot/goal.md for a session-specific override
```

### 3. Run the autopilot

```bash
./run_autopilot.sh              # 200 iterations
./run_autopilot.sh 50 30        # 50 iters, 30s sleep

# With a session-specific goal (steer the loop):
./run_autopilot.sh --session-goal "Fix the broken Stripe webhook"
./run_autopilot.sh --session-goal-file .opencode/autopilot/session.md
# Session goal file is re-read each iteration: edit it while the loop runs
```

### 4. Run the meta-autopilot (optional)

```bash
./run_meta_autopilot.sh         # improves the autopilot itself
```

### 5. View the KPI dashboard

```bash
python3 .opencode/autopilot/kpi.py
```

This generates an HTML dashboard at `.opencode/autopilot/kpi/` with:
- Aggregate metrics across all runs
- Per-run scorecards with competitiveness timelines
- Family distribution charts
- Knowledge base statistics

Open the HTML file in any browser.

## Customization

### Adding module areas

Edit `autocode.yaml`:

```yaml
modules:
  - name: my_module
    keywords: ["keyword1", "keyword2"]
```

The autopilot uses keyword matching to classify iterations into families.

### Switching project type

Change the `type` field in `autocode.yaml`:

```yaml
project:
  type: "rust"  # now defaults come from rust preset
```

### Tuning loop behavior

```yaml
autopilot:
  loop:
    sleep_seconds: 120           # longer pause between cycles
    max_retries_per_iteration: 3  # more retries on transient failures
    parallel_branches_max: 4      # allow more parallel exploration
```

## Troubleshooting

### Python import errors

```
ModuleNotFoundError: No module named 'autocode_config'
```

The `autocode_config.py` must be in the same directory as `run_autopilot.py`.
Re-run setup.sh or copy it manually.

### "pyyaml not installed"

```bash
pip install pyyaml
```

### "No active run recorded"

Project-level goals from `autocode.yaml` are used by default.
```bash
# Edit autocode.yaml → project_goals to set the persistent direction
```
