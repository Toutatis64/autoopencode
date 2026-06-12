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
