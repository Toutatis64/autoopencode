#!/usr/bin/env bash
set -euo pipefail

ORIGINAL_GOAL=".opencode/autopilot/goal.md"
RUN_PY=".opencode/autopilot/run_autopilot.py"
META_PY=".opencode/autopilot/meta_autopilot.py"

META_CYCLES="${1:-10}"
BATCH="${2:-10}"
SLEEP="${3:-30}"

if [ ! -f "$ORIGINAL_GOAL" ]; then
    echo "ERROR: Goal file not found at $ORIGINAL_GOAL"
    exit 1
fi

echo "═══ Meta-Autopilot — Autocode ═══"

python3 "$META_PY" init 2>/dev/null || true
python3 "$META_PY" discover 2>/dev/null || true

for cycle in $(seq 1 "$META_CYCLES"); do
    echo "══ Cycle $cycle/$META_CYCLES ══"
    python3 "$RUN_PY" --goal-file "$ORIGINAL_GOAL" --max-iterations "$BATCH" --sleep-seconds "$SLEEP" || true
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
        python3 "$RUN_PY" --goal-file "$G" --agent goal-meta-autopilot --max-iterations 1 || true
        rm -f "$G"
        python3 "$META_PY" discover 2>/dev/null || true
    fi
    python3 "$META_PY" status 2>&1 | sed 's/^/  /' || true
    [ "$cycle" -lt "$META_CYCLES" ] && [ "$SLEEP" -gt 0 ] && sleep "$SLEEP"
done
echo "Meta-autopilot complete."
