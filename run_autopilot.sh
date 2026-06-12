#!/usr/bin/env bash
set -euo pipefail

GOAL_FILE=".opencode/autopilot/goal.md"
MAX_ITERATIONS="${1:-200}"
SLEEP_SECONDS="${2:-60}"

if [ ! -f "$GOAL_FILE" ]; then
    echo "ERROR: Goal file not found at $GOAL_FILE"
    echo "Create it: cp .opencode/autopilot/goal.template.md $GOAL_FILE"
    exit 1
fi

echo "═══ Autopilot — Autocode ═══"
echo "  Goal: $GOAL_FILE | Max: $MAX_ITERATIONS | Sleep: ${SLEEP_SECONDS}s"

python3 .opencode/autopilot/run_autopilot.py \
    --goal-file "$GOAL_FILE" \
    --max-iterations "$MAX_ITERATIONS" \
    --sleep-seconds "$SLEEP_SECONDS"
