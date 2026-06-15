#!/usr/bin/env bash
set -euo pipefail

# ══════════════════════════════════════════════════════════════════════════════
# Autocode Setup — Interactive Installer
# ══════════════════════════════════════════════════════════════════════════════
# Run this script from your project root to install autocode.
#
# Usage:
#   bash /path/to/autocode/setup.sh
#
# ══════════════════════════════════════════════════════════════════════════════

AUTOCODE_SRC="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-.}"
TARGET="$(cd "$TARGET" && pwd)"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║           Autocode — Autonomous Improvement Loop            ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Any language. Any framework. Any repo."

# ── Project type presets ─────────────────────────────────────────────────────
# Each preset provides validation commands and module areas appropriate to the stack.
# key => "Display Name [default cmd/module]"
declare -A PRESET_LABELS
PRESET_LABELS["python"]="Python (pytest, mypy, ruff)"
PRESET_LABELS["node_ts"]="Node.js / TypeScript (npm, tsc)"
PRESET_LABELS["rust"]="Rust (cargo test, clippy)"
PRESET_LABELS["go"]="Go (go test, vet)"
PRESET_LABELS["config"]="Config-only (YAML, JSON, TOML, etc.)"
PRESET_LABELS["generic"]="Generic / Other (make, custom)"

echo ""
echo "── Step 1: Project Type ──"
echo ""
echo "  Choose your project's language/stack for appropriate defaults."
echo "  You can override any default in the next steps."
echo ""
PS3="  Select (1-5): "
select PT_CHOICE in "${PRESET_LABELS[@]}"; do
    case "$PT_CHOICE" in
        "Python (pytest, mypy, ruff)") PROJECT_TYPE="python"; break ;;
        "Node.js / TypeScript (npm, tsc)") PROJECT_TYPE="node_ts"; break ;;
        "Rust (cargo test, clippy)") PROJECT_TYPE="rust"; break ;;
        "Go (go test, vet)") PROJECT_TYPE="go"; break ;;
        "Config-only (YAML, JSON, TOML, etc.)") PROJECT_TYPE="config"; break ;;
        "Generic / Other (make, custom)") PROJECT_TYPE="generic"; break ;;
        *) echo "  Invalid choice. Select 1-6." ;;
    esac
done

# ── Stack-specific defaults ─────────────────────────────────────────────────-
case "$PROJECT_TYPE" in
    python)
        DEF_TEST="pytest"
        DEF_TYPECHECK="mypy ."
        DEF_LINT="ruff check ."
        DEF_BUILD="python -m build"
        DEF_FULL="pytest && mypy . && ruff check ."
        DEF_MODULES="src,tests,scripts,docs,data"
        MODULES_DESC="src, tests, scripts, docs, data"
        ;;
    node_ts)
        DEF_TEST="npm test"
        DEF_TYPECHECK="tsc --noEmit"
        DEF_LINT="npm run lint"
        DEF_BUILD="npm run build"
        DEF_FULL="npm run build && npm test"
        DEF_MODULES="api,web,shared,database,tests"
        MODULES_DESC="api, web, shared, database, tests"
        ;;
    rust)
        DEF_TEST="cargo test"
        DEF_TYPECHECK="cargo check"
        DEF_LINT="cargo clippy -- -D warnings"
        DEF_BUILD="cargo build"
        DEF_FULL="cargo build && cargo test && cargo clippy -- -D warnings"
        DEF_MODULES="src,tests,benches,examples"
        MODULES_DESC="src, tests, benches, examples"
        ;;
    go)
        DEF_TEST="go test ./..."
        DEF_TYPECHECK="go vet ./..."
        DEF_LINT="golangci-lint run"
        DEF_BUILD="go build ./..."
        DEF_FULL="go build && go test && go vet"
        DEF_MODULES="cmd,pkg,internal,api"
        MODULES_DESC="cmd, pkg, internal, api"
        ;;
    generic)
        DEF_TEST="make test"
        DEF_TYPECHECK="make check"
        DEF_LINT="make lint"
        DEF_BUILD="make build"
        DEF_FULL="make build && make test"
        DEF_MODULES="core,tests,docs,scripts,config"
        MODULES_DESC="core, tests, docs, scripts, config"
        ;;
    config)
        DEF_TEST="yamllint ."
        DEF_TYPECHECK="prettier --check ."
        DEF_LINT="yamllint ."
        DEF_BUILD="echo 'Config-only project — no build needed'"
        DEF_FULL="yamllint . && prettier --check ."
        DEF_MODULES="configs,schemas,ci,docs,templates"
        MODULES_DESC="configs, schemas, ci, docs, templates"
        ;;
esac

# ── Gather project info ──────────────────────────────────────────────────────
echo ""
echo "── Step 2: Project Details ──"
read -r -p "  Project name: " PROJECT_NAME
read -r -p "  Description (short): " PROJECT_DESC

echo ""
echo "── Step 3: Validation Commands ──"
echo "  (press Enter to accept the $PROJECT_TYPE default in brackets)"
read -r -p "  Test [$DEF_TEST]: " CMD_DEFAULT
CMD_DEFAULT="${CMD_DEFAULT:-$DEF_TEST}"
read -r -p "  Typecheck [$DEF_TYPECHECK]: " CMD_TYPECHECK
CMD_TYPECHECK="${CMD_TYPECHECK:-$DEF_TYPECHECK}"
read -r -p "  Lint [$DEF_LINT]: " CMD_LINT
CMD_LINT="${CMD_LINT:-$DEF_LINT}"
read -r -p "  Build [$DEF_BUILD]: " CMD_BUILD
CMD_BUILD="${CMD_BUILD:-$DEF_BUILD}"
read -r -p "  Full validation [$DEF_FULL]: " CMD_FULL
CMD_FULL="${CMD_FULL:-$DEF_FULL}"

echo ""
echo "── Step 4: Module Areas ──"
echo "  Your codebase's main areas (used for branch classification)."
echo "  Separate with commas.  Default for $PROJECT_TYPE: $MODULES_DESC"
read -r -p "  Module names [$DEF_MODULES]: " MODULE_INPUT
MODULE_INPUT="${MODULE_INPUT:-$DEF_MODULES}"

echo ""
echo "── Step 5: Conventions ──"
echo "  Non-negotiable rules the autopilot must follow (empty line to finish)"
CONVENTIONS=()
while true; do
    read -r -p "  Convention: " CONV
    [[ -z "$CONV" ]] && break
    CONVENTIONS+=("$CONV")
done
if [[ ${#CONVENTIONS[@]} -eq 0 ]]; then
    CONVENTIONS=(
        "Tests for every fix: regression test that fails before the fix"
        "One focused validation per edit"
        "Preserve existing code patterns"
    )
fi

echo ""
echo "── Step 6: Project Goals ──"
echo "  High-level direction for the autopilot loop (persistent across sessions)."
echo "  Describe what your project aims to achieve."
echo "  (press Enter to use generic defaults)"
read -r -p "  Goal description: " PROJECT_GOAL_DESC
PROJECT_GOAL_DESC="${PROJECT_GOAL_DESC:-"Perpetually improve $PROJECT_NAME. No fixed endpoint — each iteration must leave the repo better."}"
echo "  Enter principles (one per line, empty line to finish):"
PRINCIPLES=()
while true; do
    read -r -p "  Principle: " PRIN
    [[ -z "$PRIN" ]] && break
    PRINCIPLES+=("$PRIN")
done
if [[ ${#PRINCIPLES[@]} -eq 0 ]]; then
    PRINCIPLES=(
        "Improve quality, coverage, performance, and architecture"
        "Add tests for uncovered code"
        "Fix bugs with regression tests"
    )
fi

echo ""
echo "══ Dependency Checks & Auto-Install ══"

_install_attempts=0
_install_failures=0

_check_install() {
    local name="$1" check_cmd="$2" install_cmd="$3" required="${4:-no}"
    if eval "$check_cmd" 2>/dev/null; then
        echo "  ✓ $name"
    else
        echo "  ⚠ $name — not found, installing..."
        _install_attempts=$((_install_attempts + 1))
        if eval "$install_cmd" 2>&1 | tail -3; then
            echo "  ✓ $name installed"
        else
            _install_failures=$((_install_failures + 1))
            if [ "$required" = "yes" ]; then
                echo "  ✗ $name — install failed (REQUIRED)"
            else
                echo "  ⚠ $name — install failed (optional, continuing)"
            fi
        fi
    fi
}

# 1. Python 3.10+
_check_install \
    "Python 3.10+" \
    'python3 -c "import sys; exit(0 if sys.version_info >= (3,10) else 1)"' \
    "echo 'Install Python 3.10+ from https://python.org/downloads/'" \
    "yes"

# 2. PyYAML (needed by autopilot engine)
_check_install \
    "PyYAML" \
    'python3 -c "import yaml"' \
    'python3 -m pip install pyyaml -q 2>/dev/null || python3 -m pip install pyyaml -q --break-system-packages 2>/dev/null' \
    "yes"

# 3. lean-ctx (token-efficient file/shell tools)
_check_install \
    "lean-ctx" \
    'which lean-ctx' \
    'curl -fsSL https://raw.githubusercontent.com/yvgude/lean-ctx/main/skills/lean-ctx/scripts/install.sh | bash && lean-ctx setup'

# 4. graphify (knowledge graph for code understanding)
_check_install \
    "graphify" \
    'python3 -c "import graphify"' \
    'python3 -m pip install graphifyy -q 2>/dev/null || python3 -m pip install graphifyy -q --break-system-packages 2>/dev/null'

# 5. opencode CLI
if which opencode 2>/dev/null; then
    echo "  ✓ opencode CLI"
else
    echo "  ⚠ opencode CLI — not found (install: https://opencode.ai)"
    _install_failures=$((_install_failures + 1))
fi

echo ""
if [ "$_install_failures" -gt 0 ]; then
    echo "  $_install_failures dependency issue(s). Some features may be limited."
    echo "  Re-run setup after installing missing deps to clear this message."
fi

echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Installing..."

# ── Create directories ───────────────────────────────────────────────────────
mkdir -p "$TARGET/.opencode/instructions"
mkdir -p "$TARGET/.opencode/agents"
mkdir -p "$TARGET/.opencode/skills"
mkdir -p "$TARGET/.opencode/autopilot/runtime"
mkdir -p "$TARGET/.opencode/autopilot/kpi"
mkdir -p "$TARGET/knowledge"

# ── Copy Python engine ───────────────────────────────────────────────────────
for _py in \
    autocode_config.py run_autopilot.py self_improving_loop.py meta_autopilot.py \
    kpi.py launcher.py memory.py checkpoint.py controller.py reporting.py sync.py; do
    cp "$AUTOCODE_SRC/scripts/$_py" "$TARGET/.opencode/autopilot/$_py"
done
echo "  • Copied Python engine"

# ── Generate autocode.yaml ───────────────────────────────────────────────────
IFS=',' read -ra MODULE_NAMES <<< "$MODULE_INPUT"

cat > "$TARGET/autocode.yaml" <<YAMLEOF
# Autocode — Project Configuration
# Generated by setup.sh for $PROJECT_TYPE project

project:
  name: "$PROJECT_NAME"
  description: "$PROJECT_DESC"
  type: "$PROJECT_TYPE"

validation:
  default: "$CMD_DEFAULT"
  typecheck: "$CMD_TYPECHECK"
  lint: "$CMD_LINT"
  build: "$CMD_BUILD"
  full: "$CMD_FULL"

paths:
  knowledge_base: "knowledge/autopilot_kb.yaml"
  meta_knowledge_base: "knowledge/meta_kb.yaml"
  goal_file: ".opencode/autopilot/goal.md"
  runtime_dir: ".opencode/autopilot/runtime"
  components_dir: ".opencode/autopilot/components"

modules:
YAMLEOF

for mod in "${MODULE_NAMES[@]}"; do
    MOD=$(echo "$mod" | xargs)
    cat >> "$TARGET/autocode.yaml" <<YAMLEOF
  - name: $MOD
    keywords: ["$MOD"]
YAMLEOF
done

cat >> "$TARGET/autocode.yaml" <<YAMLEOF

project_goals:
  description: "$PROJECT_GOAL_DESC"
  principles:
YAMLEOF
for prin in "${PRINCIPLES[@]}"; do
    echo "    - \"$prin\"" >> "$TARGET/autocode.yaml"
done

cat >> "$TARGET/autocode.yaml" <<YAMLEOF

conventions:
YAMLEOF
for conv in "${CONVENTIONS[@]}"; do
    echo "  - \"$conv\"" >> "$TARGET/autocode.yaml"
done

cat >> "$TARGET/autocode.yaml" <<YAMLEOF

skills:
  paths:
    - ".opencode/skills"
YAMLEOF
echo "  • Created autocode.yaml"

# ── Generate root config files ───────────────────────────────────────────────

cat > "$TARGET/opencode.json" <<JSONEOF
{
  "\$schema": "https://opencode.ai/config.json",
  "instructions": [
    "AGENTS.md",
    ".opencode/instructions/vibe.md",
    ".opencode/instructions/SESSION-LIFECYCLE.md"
  ],
  "tool_output": {"max_lines": 40, "max_bytes": 2500},
  "compaction": {"auto": true, "prune": true, "reserved": 15000, "tail_turns": 3},
  "skills": {"paths": [".opencode/skills"]},
  "formatter": true,
  "lsp": true,
  "permission": {
    "bash": {
      "*": "allow",
      "git push*": "deny", "git commit *": "deny",
      "git reset *": "deny", "git restore *": "deny",
      "git checkout -- *": "deny",
      "rm *": "deny", "sudo *": "deny"
    }
  }
}
JSONEOF

cat > "$TARGET/.opencode/opencode.json" <<JSONEOF
{
  "\$schema": "https://opencode.ai/config.json",
  "compaction": {"auto": true, "prune": true, "reserved": 15000}
}
JSONEOF
echo "  • Created opencode config"

cat > "$TARGET/AGENTS.md" <<AGEOF
# $PROJECT_NAME
$PROJECT_DESC

## Core rules
- Smallest correct change. Tests for every fix.
- One focused validation per edit: \`$CMD_DEFAULT\`, \`$CMD_TYPECHECK\`, \`$CMD_LINT\`
- Ask when ambiguous. No speculative code.

## Build, test, lint
- Test: \`$CMD_DEFAULT\` · Typecheck: \`$CMD_TYPECHECK\`
- Lint: \`$CMD_LINT\` · Build: \`$CMD_BUILD\`
- Full: \`$CMD_FULL\`

## Autopilot
- Agent \`goal-autopilot\` for bounded-iteration autonomous work
- Agent \`goal-meta-autopilot\` to improve the autopilot itself
- Launchers: \`./run_autopilot.sh\` · \`./run_meta_autopilot.sh\`
- KB: \`knowledge/autopilot_kb.yaml\` · Goals: \`autocode.yaml\` → \`project_goals\`

## Dashboard
- View autopilot KPIs: \`python3 .opencode/autopilot/kpi.py\`
AGEOF
echo "  • Created AGENTS.md"

# ── Generate run_autopilot.sh ────────────────────────────────────────────────
cat > "$TARGET/run_autopilot.sh" <<SHEOF
#!/usr/bin/env bash
set -euo pipefail

MAX_ITERATIONS="\${1:-200}"
SLEEP_SECONDS="\${2:-60}"
SESSION_GOAL_FILE=".opencode/autopilot/goal.md"

echo "═══ Autopilot — $PROJECT_NAME ═══"
echo "  Project goals from autocode.yaml | Max: \$MAX_ITERATIONS | Sleep: \${SLEEP_SECONDS}s"

ARGS=(
    --max-iterations "\$MAX_ITERATIONS"
    --sleep-seconds "\$SLEEP_SECONDS"
)

if [ -f "\$SESSION_GOAL_FILE" ] && [ -s "\$SESSION_GOAL_FILE" ]; then
    echo "  Session goal: \$SESSION_GOAL_FILE"
    ARGS+=(--session-goal-file "\$SESSION_GOAL_FILE")
fi

python3 .opencode/autopilot/run_autopilot.py "\${ARGS[@]}"
SHEOF
chmod +x "$TARGET/run_autopilot.sh"

cat > "$TARGET/run_meta_autopilot.sh" <<SHEOF
#!/usr/bin/env bash
set -euo pipefail

RUN_PY=".opencode/autopilot/run_autopilot.py"
META_PY=".opencode/autopilot/meta_autopilot.py"
SESSION_GOAL_FILE=".opencode/autopilot/goal.md"

META_CYCLES="\${1:-10}"
BATCH="\${2:-10}"
SLEEP="\${3:-30}"

echo "═══ Meta-Autopilot — $PROJECT_NAME ═══"
echo "  Project goals from autocode.yaml"

python3 "\$META_PY" init 2>/dev/null || true
python3 "\$META_PY" discover 2>/dev/null || true

RUN_ARGS=(--max-iterations "\$BATCH" --sleep-seconds "\$SLEEP")
if [ -f "\$SESSION_GOAL_FILE" ] && [ -s "\$SESSION_GOAL_FILE" ]; then
    RUN_ARGS+=(--session-goal-file "\$SESSION_GOAL_FILE")
fi

for cycle in \$(seq 1 "\$META_CYCLES"); do
    echo "══ Cycle \$cycle/\$META_CYCLES ══"
    python3 "\$RUN_PY" "\${RUN_ARGS[@]}" || true
    python3 "\$META_PY" cycle --level 1 2>&1 | sed 's/^/  [meta] /' || true

    STAG=\$(python3 -c "
import sys; sys.path.insert(0, '.opencode/autopilot')
from meta_autopilot import MetaController
try:
    c=MetaController(); c.get_or_create_level(1)
    print(c.summary().get('levels',{}).get('1',{}).get('stagnation_score',0))
except: print(0.0)" 2>/dev/null || echo 0.0)

    if (( \$(echo "\$STAG > 0.4" | bc -l 2>/dev/null || echo 0) )); then
        echo "  Deep intervention (stagnation=\$STAG)..."
        G=\$(mktemp /tmp/meta_XXXX.md)
        echo "# Meta intervention — improve prompts, scripts, params, or code" > "\$G"
        python3 "\$RUN_PY" --goal-file "\$G" --agent goal-meta-autopilot --max-iterations 1 || true
        rm -f "\$G"
        python3 "\$META_PY" discover 2>/dev/null || true
    fi
    python3 "\$META_PY" status 2>&1 | sed 's/^/  /' || true
    [ "\$cycle" -lt "\$META_CYCLES" ] && [ "\$SLEEP" -gt 0 ] && sleep "\$SLEEP"
done
echo "Meta-autopilot complete."
SHEOF
chmod +x "$TARGET/run_meta_autopilot.sh"
echo "  • Created launchers"

# ── Generate .opencode instructions ──────────────────────────────────────────

cat > "$TARGET/.opencode/instructions/vibe.md" <<VIBEOF
# Token diet (5K cap)
System prompt ≤ 1.5K tokens · Per-tool output ≤ 625 tokens

## Compression
- \`ctx_read\` modes: \`map\`/\`signatures\` for context, \`full\` only when editing
- \`ctx_search\` over \`grep\`, \`ctx_shell\` over \`bash\`

## 50k threshold
When >50k tokens: snapshot → signatures mode → avoid full reads until compacted

## Memory
Save non-obvious findings as knowledge entries
Persist only decisions/patterns that survive the session
VIBEOF

cat > "$TARGET/.opencode/instructions/SESSION-LIFECYCLE.md" <<SESSEOF
# Session lifecycle
- Start: read the project goals (autocode.yaml \`project_goals\`) and KB, check current state
- Edit: one focused validation after each change
- End: record decisions in KB, generate KPI snapshot
SESSEOF
echo "  • Created instructions"

# ── Generate agents (generic, project-agnostic prompts) ──────────────────────

cat > "$TARGET/.opencode/agents/goal-autopilot.agent.md" <<AGENTEOF
---
description: Bounded-iteration autonomous executor for a long-horizon goal.
mode: primary
permission:
  read: allow
  edit: allow
  glob: allow
  grep: allow
  list: allow
  skill: allow
  task: allow
  todowrite: allow
  question: deny
  webfetch: allow
  bash:
    "*": allow
    "git push*": deny
    "git commit *": deny
    "git reset *": deny
    "git restore *": deny
    "git checkout -- *": deny
    "rm *": deny
    "sudo *": deny
  external_directory: deny
---

You are a bounded-iteration autonomous executor used by an external loop.

**Operating model:**
- Fresh context each invocation. Files attached = durable memory.
- Make real progress: inspect, edit, validate, leave artifacts.
- Small verified increments over speculative rewrites.
- Never ask the user. If blocked, report in the checkpoint.
- Do not commit, push, reset, restore, or remove files.

**Memory discipline:**
- Always read \`knowledge/autopilot_kb.yaml\` first — it is authoritative.
- After any win or dead end, append a structured entry to the KB YAML.
- Avoid repeating the same failed experiment without a concrete new reason.

**Execution discipline:**
- Start by identifying the best next bounded step toward the goal.
- Before acting, state: the code weakness targeted, why this fixes it,
  the smallest validation, and why this beats alternatives.
- Run the narrowest validation that reduces uncertainty.
- If the current path looks non-viable, pivot with evidence.
- Prefer work that expands what the codebase does well.
- When recent iterations focused on one module, consider a different area next.

**Validation (from project config):**
- Default: \`$CMD_DEFAULT\`
- Typecheck: \`$CMD_TYPECHECK\`
- Lint: \`$CMD_LINT\`
- Build: \`$CMD_BUILD\`
- Full: \`$CMD_FULL\`

**Checkpoint format — end every response with exactly one:**
AUTOPILOT_CHECKPOINT_BEGIN
{"status":"continue","goal_complete":false,"summary":"","branch_family":"module_name","family_novelty":"minor","evidence_quality":"artifact_only","competitiveness":"non_competitive","new_facts":[],"decisions":[],"next_steps":[],"files_touched":[],"commands_run":[],"risks":[],"artifacts":[]}
AUTOPILOT_CHECKPOINT_END
AGENTEOF

cat > "$TARGET/.opencode/agents/goal-meta-autopilot.agent.md" <<METAEOF
---
description: Meta-autopilot — improves the autopilot system itself.
mode: primary
permission:
  read: allow
  edit: allow
  glob: allow
  grep: allow
  list: allow
  skill: allow
  task: allow
  todowrite: allow
  question: deny
  webfetch: allow
  bash:
    "*": allow
    "git push*": deny
    "git commit *": deny
    "git reset *": deny
    "git restore *": deny
    "git checkout -- *": deny
    "rm *": deny
    "sudo *": deny
  external_directory: deny
---

You are a meta-autopilot agent.

Never replace the original \`goal.md\`. Improve the autopilot so it is
*more effective* at pursuing that goal.

**What you can improve:**
- Prompts: \`goal.md\`, \`goal-autopilot.agent.md\`, your own prompt
- Scripts: build, test, validation scripts
- Autopilot code: \`run_autopilot.py\`, \`self_improving_loop.py\`, \`meta_autopilot.py\`
- Parameters: loop timing, retry logic, parallelism
- Algorithms: family inference, bottleneck detection

**How to make a change:**
1. Read \`knowledge/meta_kb.yaml\` first
2. Check state: \`python3 .opencode/autopilot/meta_autopilot.py status\`
3. Make the change, register via discover, record in meta_kb.yaml

End with a structured checkpoint (same format as goal-autopilot).
METAEOF
echo "  • Created agents (generic, any language)"

# ── Generate goal files (generic) ────────────────────────────────────────────

cat > "$TARGET/.opencode/autopilot/goal.template.md" <<GOALTEOF
# Session Goal

Describe the session-specific focus. This overrides the project-level
goals from \`autocode.yaml\` for this run.

## What to Achieve
- 

## Success Criteria
- What must be true for done.

## Hard Constraints
- Constraints for this session only.

## Stop Conditions
- Iteration budget for this session.
GOALTEOF

cat > "$TARGET/.opencode/autopilot/goal.md" <<GOALEOF
# Session Goal

Override the project goal for this session. Be specific about what to achieve.

Example: "Fix all type errors in the API layer and add integration tests."

Leave this file empty or use the template (goal.template.md) to set a tactical
session focus. Project-level goals from \`autocode.yaml\` are used by default.

## What to Focus On (edit this)
- 

## Success This Session
- 

## Stop When
- 
GOALEOF
echo "  • Created goal files"

# ── Generate skills ──────────────────────────────────────────────────────────
mkdir -p "$TARGET/.opencode/skills/long-horizon-autonomy"
cat > "$TARGET/.opencode/skills/long-horizon-autonomy/SKILL.md" <<SKILLEOF
---
name: long-horizon-autonomy
description: autopilot, long-running agent, multi-iteration goal, checkpoint, resume, disk memory
---
# Long Horizon Autonomy
Use when the task needs an external loop, resumable checkpoints, or durable memory.

**Rules:** Bounded iterations, durable artifacts, compact summaries.
**Checkpoint:** End with machine-readable JSON. Record facts, decisions, next steps.
**Compaction:** Stable facts → knowledge store. Recent summaries → working memory.
**Validation:** Narrowest test first. Pivot with evidence on repeated failure.
SKILLEOF

mkdir -p "$TARGET/.opencode/skills/prod-diagnose"
cat > "$TARGET/.opencode/skills/prod-diagnose/SKILL.md" <<SKILLEOF
---
name: prod-diagnose
description: Diagnose and fix production issues.
---
# Production Diagnosis
**Workflow:** Gather signals → narrow scope → reproduce → root cause → fix → validate
**Always:** Write a regression test. Document in knowledge/findings/.
SKILLEOF
echo "  • Created skills"

# ── Generate component configs ───────────────────────────────────────────────
mkdir -p "$TARGET/.opencode/autopilot/components/autopilot/parameters"
mkdir -p "$TARGET/.opencode/autopilot/components/autopilot/algorithms"
mkdir -p "$TARGET/.opencode/autopilot/components/autopilot/prompts"
mkdir -p "$TARGET/.opencode/autopilot/components/meta/parameters"
mkdir -p "$TARGET/.opencode/autopilot/components/meta/algorithms"
mkdir -p "$TARGET/.opencode/autopilot/components/meta/modules"
mkdir -p "$TARGET/.opencode/autopilot/components/meta/prompts"

cat > "$TARGET/.opencode/autopilot/components/autopilot/parameters/loop-params.yaml" <<YAMLEOF
sleep_seconds: 60
max_retries_per_iteration: 2
retry_backoff_seconds: 15
max_retry_backoff_seconds: 300
cpu_budget_percent: 90
parallel_branches_max: 3
timeout_seconds: 3600
YAMLEOF

cat > "$TARGET/.opencode/autopilot/components/autopilot/parameters/inference-params.yaml" <<YAMLEOF
exhaust_family_after: 3
hard_pivot_after: 5
diversity_window: 6
repeat_family_streak: 3
dominant_family_share: 0.6
default_stop_after: 100
YAMLEOF

cat > "$TARGET/.opencode/autopilot/components/meta/parameters/meta-loop-params.yaml" <<YAMLEOF
eval_window: 8
exhaustion_threshold: 5
stagnation_threshold: 6
YAMLEOF

cat > "$TARGET/.opencode/autopilot/components/meta/algorithms/variant-generation.yaml" <<YAMLEOF
strategies:
  parameter_perturb: {enabled: true, rate: 0.3, magnitude: 0.2}
  random_search: {enabled: true}
  evolutionary: {enabled: true, population_size: 5}
YAMLEOF

cat > "$TARGET/.opencode/autopilot/components/autopilot/algorithms/family-inference.yaml" <<YAMLEOF
method: config_driven
version: 1.0.0
metadata: {inference_backend: autocode_config, config_source: autocode.yaml > modules}
YAMLEOF

for f in goal-autopilot goal-meta-autopilot; do
    cat > "$TARGET/.opencode/autopilot/components/autopilot/prompts/${f}.md" <<YAMLEOF
ref: autopilot:prompt:${f}
active_file: .opencode/agents/${f}.agent.md
YAMLEOF
done
echo "  • Created component configs"

# ── Knowledge base files ─────────────────────────────────────────────────────
echo 'entries: []' > "$TARGET/knowledge/autopilot_kb.yaml"
echo 'entries: []' > "$TARGET/knowledge/meta_kb.yaml"
echo "  • Created knowledge bases"

# ── npm dependencies ─────────────────────────────────────────────────────────
if [ -f "$AUTOCODE_SRC/.opencode/package.json" ]; then
    cp "$AUTOCODE_SRC/.opencode/package.json" "$TARGET/.opencode/package.json"
    cp "$AUTOCODE_SRC/.opencode/package-lock.json" "$TARGET/.opencode/package-lock.json" 2>/dev/null || true
    echo "  • Installing npm dependencies..."
    (cd "$TARGET/.opencode" && npm install --silent 2>/dev/null) && echo "  • npm dependencies installed" || echo "  ⚠ npm install failed (non-critical)"
fi

# ── .gitignore ───────────────────────────────────────────────────────────────
if ! grep -q "autopilot/runtime" "$TARGET/.gitignore" 2>/dev/null; then
    cat >> "$TARGET/.gitignore" <<GITIGNEOF

# Autocode runtime
.opencode/autopilot/runtime/
.opencode/autopilot/*.sqlite3
.opencode/autopilot/*.db
tmp/autopilot_scratch/
.opencode/autopilot/kpi/*.html
GITIGNEOF
    echo "  • Updated .gitignore"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Autocode installed!                                        ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║                                                              ║"
echo "║  Next:                                                       ║"
echo "║  1. Review autocode.yaml in your project root.               ║"
echo "║  2. Edit .opencode/autopilot/goal.md to your goal.           ║"
echo "║  3. ./run_autopilot.sh         # start improving              ║"
echo "║  4. ./run_meta_autopilot.sh    # improve the autopilot        ║"
echo "║  5. python3 .opencode/autopilot/kpi.py  # view dashboard     ║"
echo "║                                                              ║"
echo "║  Project type: $PROJECT_TYPE                                    ║"
echo "║  Language: $PROJECT_TYPE                                      ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
