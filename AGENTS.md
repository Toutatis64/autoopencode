# OpenAutopilot
The autopilot for opencode

## Core rules
- Smallest correct change. Tests for every fix.
- One focused validation per edit: `make test`, `make check`, `make lint`
- Ask when ambiguous. No speculative code.

## Build, test, lint
- Test: `make test` · Typecheck: `make check`
- Lint: `make lint` · Build: `make build`
- Full: `make build && make test`

## Autopilot
- Agent `goal-autopilot` for bounded-iteration autonomous work
- Agent `goal-meta-autopilot` to improve the autopilot itself
- Launchers: `./run_autopilot.sh` · `./run_meta_autopilot.sh`
- KB: `knowledge/autopilot_kb.yaml` · Goals: `autocode.yaml` → `project_goals`

## Dashboard
- View autopilot KPIs: `python3 .opencode/autopilot/kpi.py`
