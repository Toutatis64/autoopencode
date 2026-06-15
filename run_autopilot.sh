#!/usr/bin/env bash
set -euo pipefail

MAX_ITERATIONS="${1:-200}"
SLEEP_SECONDS="${2:-60}"
SESSION_GOAL_FILE=".opencode/autopilot/goal.md"

echo "═══ Autopilot — Autocode ═══"
echo "  Project goals from autocode.yaml | Max: $MAX_ITERATIONS | Sleep: ${SLEEP_SECONDS}s"

ARGS=(
    --max-iterations "$MAX_ITERATIONS"
    --sleep-seconds "$SLEEP_SECONDS"
)

if [ -f "$SESSION_GOAL_FILE" ] && [ -s "$SESSION_GOAL_FILE" ]; then
    echo "  Session goal: $SESSION_GOAL_FILE"
    ARGS+=(--session-goal-file "$SESSION_GOAL_FILE")
fi

python3 .opencode/autopilot/run_autopilot.py "${ARGS[@]}"
