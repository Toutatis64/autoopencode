"""Memory subsystem for AutoOpencode — persistent RAG-style memory with SQLite+FTS5."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

TOKEN_RE = re.compile(r"[a-z0-9]+")
MEMORY_IMPORTANCE: dict[str, float] = {
    "summary": 2.0,
    "goal_progress": 4.0,
    "fact": 5.0,
    "decision": 4.0,
    "next_step": 4.0,
    "risk": 3.0,
    "artifact": 3.0,
}
STOPWORDS: set[str] = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "then",
    "this",
    "to",
    "use",
    "using",
    "with",
    "you",
    "your",
}
MEMORY_ENTRY_LABELS: dict[str, str] = {
    "fact": "Facts",
    "decision": "Decisions",
    "next_step": "Next Steps",
    "risk": "Risks",
    "goal_progress": "Goal Progress",
    "artifact": "Artifacts",
    "summary": "Summaries",
}


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(msg: str) -> None:
    print(f"[{utc_now()}] {msg}", flush=True)


def unique_preserve(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def extract_tags(*values: Any, limit: int = 16) -> list[str]:
    tags: list[str] = []
    for value in values:
        text = clean_text(value).lower()
        if not text:
            continue
        tokens = TOKEN_RE.findall(text)
        for token in tokens:
            if token in STOPWORDS:
                continue
            if len(token) < 2 and not token.isdigit():
                continue
            if len(token) > 40:
                continue
            tags.append(token)
        for i in range(len(tokens) - 1):
            bigram = tokens[i] + "_" + tokens[i + 1]
            if len(bigram) > 4 and len(bigram) <= 60:
                if tokens[i] not in STOPWORDS or tokens[i + 1] not in STOPWORDS:
                    tags.append(bigram)
    return unique_preserve(tags)[:limit]


def memory_tags_json(tags: list[str]) -> str:
    return json.dumps(unique_preserve(tags), sort_keys=True)


def upsert_memory_entry(
    conn: sqlite3.Connection,
    run_id: str,
    entry_type: str,
    content: str,
    tags: list[str],
    importance: float,
    source_iteration: int,
    timestamp: str,
) -> None:
    entry = clean_text(content)
    if not entry:
        return
    conn.execute(
        """
        INSERT INTO memory_entries (
            run_id, entry_type, content, tags_json, importance, source_iteration, created_at, last_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id, entry_type, content) DO UPDATE SET
            tags_json = excluded.tags_json,
            importance = MAX(memory_entries.importance, excluded.importance),
            source_iteration = MAX(memory_entries.source_iteration, excluded.source_iteration),
            last_seen_at = excluded.last_seen_at
        """,
        (
            run_id,
            entry_type,
            entry,
            memory_tags_json(tags),
            importance,
            source_iteration,
            timestamp,
            timestamp,
        ),
    )
    row_id = conn.execute(
        "SELECT id FROM memory_entries WHERE run_id = ? AND entry_type = ? AND content = ?",
        (run_id, entry_type, entry),
    ).fetchone()
    if row_id:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO memory_fts (rowid, content, entry_type) VALUES (?, ?, ?)",
                (row_id["id"], entry, entry_type),
            )
        except sqlite3.OperationalError:
            pass


def touch_memory_entry(conn: sqlite3.Connection, entry_id: int, timestamp: str) -> None:
    conn.execute(
        "UPDATE memory_entries SET last_seen_at = ? WHERE id = ?",
        (timestamp, entry_id),
    )


def backfill_legacy_facts(conn: sqlite3.Connection, run_id: str) -> None:
    rows = conn.execute(
        "SELECT fact, first_iteration, created_at FROM facts WHERE run_id = ? ORDER BY id ASC",
        (run_id,),
    ).fetchall()
    for row in rows:
        upsert_memory_entry(
            conn,
            run_id,
            "fact",
            row["fact"],
            extract_tags(row["fact"]),
            MEMORY_IMPORTANCE["fact"],
            row["first_iteration"],
            row["created_at"],
        )
    conn.commit()


def persist_memory_entries(
    conn: sqlite3.Connection,
    run_id: str,
    iteration_number: int,
    checkpoint: dict[str, Any],
    timestamp: str,
) -> None:
    upsert_memory_entry(
        conn,
        run_id,
        "summary",
        checkpoint["summary"],
        extract_tags(checkpoint["summary"]),
        MEMORY_IMPORTANCE["summary"],
        iteration_number,
        timestamp,
    )
    if checkpoint.get("goal_progress"):
        upsert_memory_entry(
            conn,
            run_id,
            "goal_progress",
            checkpoint["goal_progress"],
            extract_tags(checkpoint["goal_progress"]),
            MEMORY_IMPORTANCE["goal_progress"],
            iteration_number,
            timestamp,
        )
    typed_entries = {
        "fact": checkpoint.get("new_facts", []),
        "decision": checkpoint.get("decisions", []),
        "next_step": checkpoint.get("next_steps", []),
        "risk": checkpoint.get("risks", []),
    }
    for entry_type, values in typed_entries.items():
        for value in values:
            upsert_memory_entry(
                conn,
                run_id,
                entry_type,
                value,
                extract_tags(value),
                MEMORY_IMPORTANCE[entry_type],
                iteration_number,
                timestamp,
            )
    for artifact in checkpoint.get("artifacts", []):
        artifact_text = artifact["path"]
        if artifact.get("description"):
            artifact_text = f"{artifact['path']}: {artifact['description']}"
        upsert_memory_entry(
            conn,
            run_id,
            "artifact",
            artifact_text,
            extract_tags(artifact["path"], artifact.get("description", "")),
            MEMORY_IMPORTANCE["artifact"],
            iteration_number,
            timestamp,
        )


def prune_memory_entries(conn: sqlite3.Connection, run_id: str, current_iteration: int) -> None:
    cutoff_iteration = max(1, current_iteration - 50)
    conn.execute(
        """
        DELETE FROM memory_entries
        WHERE run_id = ?
          AND source_iteration < ?
          AND importance < 4.0
          AND entry_type NOT IN ('summary', 'artifact')
          AND (last_seen_at IS NULL OR last_seen_at < (
              SELECT COALESCE(MAX(created_at), '1970-01-01') FROM memory_entries
              WHERE run_id = ? AND source_iteration > ?
          ))
        """,
        (run_id, cutoff_iteration, run_id, current_iteration - 10),
    )
    conn.execute(
        """
        DELETE FROM facts
        WHERE run_id = ?
          AND id NOT IN (
              SELECT id FROM facts
              WHERE run_id = ?
              ORDER BY id DESC LIMIT 200
          )
        """,
        (run_id, run_id),
    )
    try:
        conn.execute(
            """
            DELETE FROM memory_fts WHERE rowid NOT IN (
                SELECT id FROM memory_entries WHERE run_id = ?
            )
            """,
            (run_id,),
        )
    except sqlite3.OperationalError:
        pass


def load_tags_json(value: str) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [clean_text(item) for item in parsed if clean_text(item)]


def score_memory_entry(row: sqlite3.Row, query_tags: list[str], current_iteration: int) -> float:
    entry_tags = load_tags_json(row["tags_json"])
    query_set = set(query_tags)
    overlap = len(query_set.intersection(entry_tags))
    text = row["content"].lower()
    direct_hits = sum(1 for tag in query_set if tag in text)
    age = max(0, current_iteration - row["source_iteration"])
    recency_bonus = max(0.0, 3.0 - (age * 0.2))
    return (overlap * 4.0) + min(direct_hits, 6) + float(row["importance"]) + recency_bonus


def build_query_tags(goal_text: str, latest_checkpoint: dict[str, Any] | None) -> list[str]:
    query_parts: list[Any] = [goal_text]
    if latest_checkpoint:
        query_parts.extend(
            [
                latest_checkpoint.get("summary"),
                latest_checkpoint.get("goal_progress"),
                *latest_checkpoint.get("next_steps", []),
                *latest_checkpoint.get("risks", []),
                *latest_checkpoint.get("files_touched", []),
            ]
        )
    return extract_tags(*query_parts, limit=24)


def retrieve_relevant_memory(
    conn: sqlite3.Connection,
    run_id: str,
    goal_text: str,
    iteration_count: int,
    latest_checkpoint: dict[str, Any] | None,
    limit: int = 18,
) -> tuple[list[sqlite3.Row], list[str], list[tuple[str, int]]]:
    backfill_legacy_facts(conn, run_id)
    rows = conn.execute(
        "SELECT id, entry_type, content, tags_json, importance, source_iteration, last_seen_at FROM memory_entries WHERE run_id = ? ORDER BY source_iteration DESC, id DESC LIMIT 480",
        (run_id,),
    ).fetchall()

    _log("Retrieving relevant memory...")
    query_tags = build_query_tags(goal_text, latest_checkpoint)

    query_text = " ".join(query_tags).replace("_", " ")
    fts_scores: dict[int, float] = {}
    if query_text.strip():
        try:
            fts_rows = conn.execute(
                "SELECT rowid, rank FROM memory_fts WHERE memory_fts MATCH ? ORDER BY rank LIMIT 120",
                (query_text,),
            ).fetchall()
            if fts_rows:
                max_rank = max(abs(r["rank"]) for r in fts_rows) or 1.0
                for r in fts_rows:
                    fts_scores[r["rowid"]] = max(0.0, 5.0 * (1.0 - abs(r["rank"]) / max_rank))
        except sqlite3.OperationalError:
            pass

    now = utc_now()
    scored_rows = []
    for row in rows:
        score = score_memory_entry(row, query_tags, max(1, iteration_count))
        fts_boost = fts_scores.get(row["id"], 0.0)
        score += fts_boost
        scored_rows.append((score, row))

    scored_rows.sort(key=lambda item: (item[0], item[1]["source_iteration"], item[1]["id"]), reverse=True)
    selected_rows = [row for score, row in scored_rows[:limit] if score > 0]
    if not selected_rows:
        selected_rows = [row for _score, row in scored_rows[: min(limit, 8)]]

    for row in selected_rows:
        if row["importance"] >= 3.0:
            touch_memory_entry(conn, row["id"], now)

    topic_counter: Counter[str] = Counter()
    for row in selected_rows:
        for tag in load_tags_json(row["tags_json"]):
            if tag in set(query_tags):
                topic_counter[tag] += 2
            else:
                topic_counter[tag] += 1
    conn.commit()
    return selected_rows, query_tags, topic_counter.most_common(12)


def render_relevant_memory(entries: list[sqlite3.Row], query_tags: list[str]) -> str:
    lines = ["# Relevant Memory", ""]
    if query_tags:
        lines.append("## Query Tags")
        lines.append("")
        lines.append("- " + ", ".join(f"`{tag}`" for tag in query_tags))
        lines.append("")

    if not entries:
        lines.append("- No tagged memory available yet.")
        return "\n".join(lines) + "\n"

    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in entries:
        grouped[row["entry_type"]].append(row)

    for entry_type in ("fact", "decision", "next_step", "risk", "goal_progress", "artifact", "summary"):
        group_rows = grouped.get(entry_type)
        if not group_rows:
            continue
        lines.append(f"## {MEMORY_ENTRY_LABELS[entry_type]}")
        lines.append("")
        for row in group_rows:
            tags = load_tags_json(row["tags_json"])
            tag_suffix = f" [tags: {', '.join(tags[:6])}]" if tags else ""
            lines.append(f"- Iteration {row['source_iteration']}: {row['content']}{tag_suffix}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_memory_topics(topic_counts: list[tuple[str, int]]) -> str:
    lines = ["# Memory Topics", ""]
    if not topic_counts:
        lines.append("- No topic clusters available yet.")
        return "\n".join(lines) + "\n"
    for tag, count in topic_counts:
        lines.append(f"- `{tag}`: {count}")
    return "\n".join(lines) + "\n"


def compact_summary(text: str, limit: int = 180) -> str:
    value = clean_text(text)
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."
