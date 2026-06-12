# Token diet (5K cap)
System prompt ≤ 1.5K tokens · Per-tool output ≤ 625 tokens

## Compression
- `ctx_read` modes: `map`/`signatures` for context, `full` only when editing
- `ctx_search` over `grep`, `ctx_shell` over `bash`

## 50k threshold
When >50k tokens: snapshot → signatures mode → avoid full reads until compacted

## Memory
Save non-obvious findings as knowledge entries
Persist only decisions/patterns that survive the session
