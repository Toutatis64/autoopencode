#!/usr/bin/env bash
set -euo pipefail

RUN_PY=".opencode/autopilot/run_autopilot.py"
META_PY=".opencode/autopilot/meta_autopilot.py"
SESSION_GOAL_FILE=".opencode/autopilot/goal.md"

META_CYCLES="${1:-10}"
BATCH="${2:-10}"
SLEEP="${3:-30}"

echo "═══ Meta-Autopilot — Autocode ═══"
echo "  Project goals from autocode.yaml"

python3 "$META_PY" init 2>/dev/null || true
python3 "$META_PY" discover 2>/dev/null || true

RUN_ARGS=(--max-iterations "$BATCH" --sleep-seconds "$SLEEP")
if [ -f "$SESSION_GOAL_FILE" ] && [ -s "$SESSION_GOAL_FILE" ]; then
    RUN_ARGS+=(--session-goal-file "$SESSION_GOAL_FILE")
fi

for cycle in $(seq 1 "$META_CYCLES"); do
    echo "══ Cycle $cycle/$META_CYCLES ══"
    python3 "$RUN_PY" "${RUN_ARGS[@]}" || true
    python3 "$META_PY" cycle --level 1 2>&1 | sed 's/^/  [meta] /' || true

    STAG=$(python3 -c "
import sys; sys.path.insert(0, '.opencode/autopilot')
from meta_autopilot import MetaController
try:
    c=MetaController(); c.get_or_create_level(1)
    print(c.summary().get('levels',{}).get('1',{}).get('stagnation_score',0))
except: print(0.0)" 2>/dev/null || echo 0.0)

    if (( $(echo "$STAG > 0.4" | bc -l 2>/dev/null || echo 0) )); then
        echo "  Deep intervention (stagnation=$STAG)..."
        G=$(mktemp /tmp/meta_XXXX.md)
        echo "# Meta intervention — improve prompts, scripts, params, or code" > "$G"
        python3 "$RUN_PY" --goal "$(cat "$G")" --agent goal-meta-autopilot --max-iterations 1 || true
        rm -f "$G"
        python3 "$META_PY" discover 2>/dev/null || true
    fi
    python3 "$META_PY" status 2>&1 | sed 's/^/  /' || true
    [ "$cycle" -lt "$META_CYCLES" ] && [ "$SLEEP" -gt 0 ] && sleep "$SLEEP"
done
echo "Meta-autopilot complete."
