"""Tests for the memory subsystem (scripts/memory.py)."""
# mypy: disable-error-code="arg-type,list-item"

from __future__ import annotations

import json
import sqlite3

import pytest

from scripts.memory import (
    MEMORY_ENTRY_LABELS,
    clean_text,
    compact_summary,
    extract_tags,
    load_tags_json,
    memory_tags_json,
    render_memory_topics,
    render_relevant_memory,
    retrieve_relevant_memory,
    score_memory_entry,
    touch_memory_entry,
    unique_preserve,
    upsert_memory_entry,
    prune_memory_entries,
    persist_memory_entries,
    backfill_legacy_facts,
    build_query_tags,
)

# ── extract_tags ──────────────────────────────────────────────────────────────


def test_extract_tags_basic() -> None:
    tags = extract_tags("fixed bug in parser module")
    assert "bug" in tags
    assert "parser" in tags
    assert "module" in tags


def test_extract_tags_stopwords_excluded() -> None:
    tags = extract_tags("the and of to in")
    assert all(t not in tags for t in ["the", "and", "of", "to", "in"])


def test_extract_tags_short_tokens_excluded() -> None:
    tags = extract_tags("a b cd ef")
    assert all(t not in tags for t in ["a", "b"])
    assert "cd" in tags
    assert "ef" in tags


def test_extract_tags_long_tokens_excluded() -> None:
    long_word = "x" * 50
    tags = extract_tags(long_word)
    assert long_word not in tags


def test_extract_tags_bigrams() -> None:
    tags = extract_tags("memory subsystem fixed parser error")
    bigrams = [t for t in tags if "_" in t]
    assert len(bigrams) > 0


def test_extract_tags_limit() -> None:
    tags = extract_tags("a b c d e f g h i j k l m n o p q r s t u v w x y z", limit=5)
    assert len(tags) <= 5


def test_extract_tags_multiple_values() -> None:
    tags = extract_tags("parser bug", "memory fix")
    assert "parser" in tags
    assert "bug" in tags
    assert "memory" in tags
    assert "fix" in tags


def test_extract_tags_empty() -> None:
    assert extract_tags("") == []
    assert extract_tags() == []


def test_extract_tags_none() -> None:
    assert extract_tags(None) == []


def test_extract_tags_digits_kept() -> None:
    tags = extract_tags("python 3 42")
    assert "python" in tags
    assert "42" in tags


# ── memory_tags_json ──────────────────────────────────────────────────────────


def test_memory_tags_json_basic() -> None:
    result = json.loads(memory_tags_json(["bug", "parser"]))
    assert result == ["bug", "parser"]


def test_memory_tags_json_preserves_order() -> None:
    result = json.loads(memory_tags_json(["z", "a", "m"]))
    assert result == ["z", "a", "m"]


def test_memory_tags_json_empty() -> None:
    assert json.loads(memory_tags_json([])) == []


# ── load_tags_json ────────────────────────────────────────────────────────────


def test_load_tags_json_valid() -> None:
    assert load_tags_json('["a", "b"]') == ["a", "b"]


def test_load_tags_json_empty_string() -> None:
    assert load_tags_json("") == []


def test_load_tags_json_not_list() -> None:
    assert load_tags_json('"string"') == []


def test_load_tags_json_invalid() -> None:
    assert load_tags_json("{invalid") == []


def test_load_tags_json_whitespace_items_stripped() -> None:
    assert load_tags_json('["  a  ", "  "]') == ["a"]


# ── score_memory_entry ────────────────────────────────────────────────────────


class MockRow:
    def __init__(
        self,
        content: str,
        tags_json: str,
        importance: float,
        source_iteration: int,
        entry_type: str = "fact",
    ) -> None:
        self.content = content
        self.tags_json = tags_json
        self.importance = importance
        self.source_iteration = source_iteration
        self.entry_type = entry_type
        self.id = 0

    def __getitem__(self, key: str) -> str | float | int:
        return getattr(self, key)


def test_score_memory_entry_overlap_boosts_score() -> None:
    row = MockRow("fixed parser bug", '["parser", "bug"]', 3.0, 5)
    score = score_memory_entry(row, ["parser", "bug", "memory"], 10)
    assert score > 0
    assert score > 3.0


def test_score_memory_entry_no_overlap() -> None:
    row = MockRow("fixed parser bug", '["unrelated"]', 2.0, 5)
    score = score_memory_entry(row, ["memory", "api"], 10)
    recency = max(0.0, 3.0 - (5 * 0.2))
    assert score == pytest.approx(2.0 + recency)


def test_score_memory_entry_recent_entries_higher() -> None:
    old_row = MockRow("old bug", '["bug"]', 3.0, 1)
    new_row = MockRow("new bug", '["bug"]', 3.0, 9)
    query = ["bug"]
    current_iter = 10
    old_score = score_memory_entry(old_row, query, current_iter)
    new_score = score_memory_entry(new_row, query, current_iter)
    assert new_score > old_score


def test_score_memory_entry_direct_text_hit() -> None:
    row = MockRow("parser module needs refactoring", '["other"]', 2.0, 5)
    score = score_memory_entry(row, ["parser"], 10)
    assert score > 2.0


def test_score_memory_entry_old_entry_no_recency() -> None:
    row = MockRow("ancient fact", '["fact"]', 1.0, 1)
    score = score_memory_entry(row, ["fact"], 100)
    recency = max(0.0, 3.0 - (99 * 0.2))
    assert recency == 0.0
    assert score > 0


# ── render_relevant_memory ────────────────────────────────────────────────────


def test_render_relevant_memory_no_entries() -> None:
    result = render_relevant_memory([], ["tag1"])
    assert "No tagged memory available yet" in result
    assert "tag1" in result


def test_render_relevant_memory_no_query_tags() -> None:
    result = render_relevant_memory([], [])
    assert "No tagged memory available yet" in result
    assert "Query Tags" not in result


def test_render_relevant_memory_with_entries() -> None:
    row = MockRow("fixed a critical bug", '["bug", "critical"]', 5.0, 3)
    result = render_relevant_memory([row], ["bug"])
    assert MEMORY_ENTRY_LABELS["fact"] in result
    assert "fixed a critical bug" in result


def test_render_relevant_memory_groups_by_type() -> None:
    fact_row = MockRow("found bug", '["bug"]', 3.0, 1)
    decision_row = MockRow("chose sqlite", '["sqlite"]', 4.0, 2, entry_type="decision")
    result = render_relevant_memory([fact_row, decision_row], ["bug"])
    assert MEMORY_ENTRY_LABELS["fact"] in result
    assert MEMORY_ENTRY_LABELS["decision"] in result


# ── render_memory_topics ────────────────────────────────────────────────────


def test_render_memory_topics_no_topics() -> None:
    result = render_memory_topics([])
    assert "No topic clusters available yet" in result


def test_render_memory_topics_with_topics() -> None:
    topics = [("bug", 5), ("parser", 3), ("memory", 2)]
    result = render_memory_topics(topics)
    assert "`bug`" in result
    assert "5" in result
    assert "`parser`" in result
    assert "3" in result


# ── build_query_tags ──────────────────────────────────────────────────────────


def test_build_query_tags_no_checkpoint() -> None:
    tags = build_query_tags("fix parser bug", None)
    assert "fix" in tags or "parser" in tags or "bug" in tags


def test_build_query_tags_with_checkpoint() -> None:
    tags = build_query_tags("memory subsystem", {"summary": "extracted memory", "next_steps": ["add tests"]})
    assert "memory" in tags
    assert "extracted" in tags


# ── upsert_memory_entry (SQLite) ──────────────────────────────────────────────


@pytest.fixture
def mem_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memory_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            entry_type TEXT NOT NULL,
            content TEXT NOT NULL,
            tags_json TEXT NOT NULL DEFAULT '[]',
            importance REAL NOT NULL DEFAULT 1.0,
            source_iteration INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_seen_at TEXT,
            UNIQUE(run_id, entry_type, content)
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
            content, entry_type, content=memory_entries
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            fact TEXT NOT NULL,
            first_iteration INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def test_upsert_memory_entry_insert(mem_db: sqlite3.Connection) -> None:
    upsert_memory_entry(
        mem_db,
        "run1",
        "fact",
        "test fact content",
        ["test", "fact"],
        3.0,
        1,
        "2024-01-01T00:00:00",
    )
    rows = mem_db.execute("SELECT content FROM memory_entries").fetchall()
    assert len(rows) == 1
    assert rows[0]["content"] == "test fact content"


def test_upsert_memory_entry_empty_content_skipped(mem_db: sqlite3.Connection) -> None:
    upsert_memory_entry(
        mem_db,
        "run1",
        "fact",
        "",
        ["test"],
        3.0,
        1,
        "2024-01-01T00:00:00",
    )
    rows = mem_db.execute("SELECT content FROM memory_entries").fetchall()
    assert len(rows) == 0


def test_upsert_memory_entry_dedup(mem_db: sqlite3.Connection) -> None:
    upsert_memory_entry(
        mem_db,
        "run1",
        "fact",
        "duplicate content",
        ["v1"],
        3.0,
        1,
        "2024-01-01T00:00:00",
    )
    upsert_memory_entry(
        mem_db,
        "run1",
        "fact",
        "duplicate content",
        ["v2"],
        5.0,
        2,
        "2024-01-02T00:00:00",
    )
    rows = mem_db.execute("SELECT content, importance FROM memory_entries").fetchall()
    assert len(rows) == 1
    assert rows[0]["importance"] == 5.0


def test_upsert_memory_entry_different_run_id(mem_db: sqlite3.Connection) -> None:
    upsert_memory_entry(
        mem_db,
        "run_a",
        "fact",
        "same content",
        ["a"],
        3.0,
        1,
        "2024-01-01T00:00:00",
    )
    upsert_memory_entry(
        mem_db,
        "run_b",
        "fact",
        "same content",
        ["b"],
        3.0,
        1,
        "2024-01-01T00:00:00",
    )
    rows = mem_db.execute("SELECT content FROM memory_entries").fetchall()
    assert len(rows) == 2


# ── persist_memory_entries ────────────────────────────────────────────────────


def test_persist_memory_entries_basic(mem_db: sqlite3.Connection) -> None:
    checkpoint = {
        "summary": "Fixed parser bug",
        "new_facts": ["parser was broken", "tests added"],
        "decisions": ["use regex for parsing"],
        "next_steps": ["add more tests"],
        "risks": ["edge cases in unicode"],
        "files_touched": ["parser.py"],
        "artifacts": [{"path": "parser.py", "description": "fixed parser"}],
    }
    persist_memory_entries(mem_db, "run1", 5, checkpoint, "2024-01-01T00:00:00")
    rows = mem_db.execute("SELECT entry_type, content FROM memory_entries ORDER BY id").fetchall()
    types = [r["entry_type"] for r in rows]
    assert "summary" in types
    assert "fact" in types
    assert "decision" in types
    assert "next_step" in types
    assert "risk" in types
    assert "artifact" in types


def test_persist_memory_entries_with_goal_progress(mem_db: sqlite3.Connection) -> None:
    checkpoint = {
        "summary": "progress",
        "goal_progress": "50% done with refactoring",
    }
    persist_memory_entries(mem_db, "run1", 3, checkpoint, "2024-01-01T00:00:00")
    rows = mem_db.execute("SELECT content FROM memory_entries WHERE entry_type = 'goal_progress'").fetchall()
    assert len(rows) == 1
    assert "50%" in rows[0]["content"]


# ── prune_memory_entries ──────────────────────────────────────────────────────


def test_prune_memory_entries_removes_old_low_importance(mem_db: sqlite3.Connection) -> None:
    upsert_memory_entry(
        mem_db,
        "run1",
        "decision",
        "old decision",
        ["old"],
        2.0,
        0,
        "2024-01-01T00:00:00",
    )
    upsert_memory_entry(
        mem_db,
        "run1",
        "decision",
        "recent decision",
        ["recent"],
        3.0,
        45,
        "2024-01-02T00:00:00",
    )
    prune_memory_entries(mem_db, "run1", 51)
    rows = mem_db.execute("SELECT content FROM memory_entries WHERE run_id = 'run1'").fetchall()
    contents = [r["content"] for r in rows]
    assert "old decision" not in contents
    assert "recent decision" in contents


def test_prune_memory_entries_preserves_summary(mem_db: sqlite3.Connection) -> None:
    upsert_memory_entry(
        mem_db,
        "run1",
        "summary",
        "old summary",
        ["old"],
        2.0,
        1,
        "2024-01-01T00:00:00",
    )
    prune_memory_entries(mem_db, "run1", 60)
    rows = mem_db.execute("SELECT content FROM memory_entries").fetchall()
    assert len(rows) == 1


# ── backfill_legacy_facts ─────────────────────────────────────────────────────


def test_backfill_legacy_facts(mem_db: sqlite3.Connection) -> None:
    mem_db.execute(
        "INSERT INTO facts (run_id, fact, first_iteration, created_at) VALUES (?, ?, ?, ?)",
        ("run1", "legacy fact one", 1, "2024-01-01T00:00:00"),
    )
    mem_db.execute(
        "INSERT INTO facts (run_id, fact, first_iteration, created_at) VALUES (?, ?, ?, ?)",
        ("run1", "legacy fact two", 2, "2024-01-02T00:00:00"),
    )
    mem_db.commit()
    backfill_legacy_facts(mem_db, "run1")
    rows = mem_db.execute("SELECT content FROM memory_entries WHERE entry_type = 'fact'").fetchall()
    assert len(rows) == 2
    contents = {r["content"] for r in rows}
    assert "legacy fact one" in contents
    assert "legacy fact two" in contents


# ── clean_text ─────────────────────────────────────────────────────────────────


def test_clean_text_none() -> None:
    assert clean_text(None) == ""


def test_clean_text_empty() -> None:
    assert clean_text("") == ""


def test_clean_text_strips_whitespace() -> None:
    assert clean_text("  hello world  ") == "hello world"


def test_clean_text_non_string() -> None:
    assert clean_text(42) == "42"
    assert clean_text(3.14) == "3.14"


# ── unique_preserve ────────────────────────────────────────────────────────────


def test_unique_preserve_basic() -> None:
    assert unique_preserve(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


def test_unique_preserve_empty() -> None:
    assert unique_preserve([]) == []


def test_unique_preserve_already_unique() -> None:
    assert unique_preserve(["a", "b", "c"]) == ["a", "b", "c"]


# ── touch_memory_entry ─────────────────────────────────────────────────────────


def test_touch_memory_entry_updates_last_seen(mem_db: sqlite3.Connection) -> None:
    upsert_memory_entry(
        mem_db,
        "run1",
        "fact",
        "test entry",
        ["test"],
        3.0,
        1,
        "2024-01-01T00:00:00",
    )
    row = mem_db.execute("SELECT id FROM memory_entries WHERE content = 'test entry'").fetchone()
    assert row is not None
    entry_id = row["id"]
    touch_memory_entry(mem_db, entry_id, "2025-01-01T00:00:00")
    updated = mem_db.execute("SELECT last_seen_at FROM memory_entries WHERE id = ?", (entry_id,)).fetchone()
    assert updated is not None
    assert updated["last_seen_at"] == "2025-01-01T00:00:00"


# ── compact_summary ────────────────────────────────────────────────────────────


def test_compact_summary_short_text() -> None:
    assert compact_summary("hello world") == "hello world"


def test_compact_summary_truncates_long_text() -> None:
    long = "a" * 200
    result = compact_summary(long, limit=20)
    assert len(result) <= 23  # 20 - 3 + "..."
    assert result.endswith("...")


def test_compact_summary_empty() -> None:
    assert compact_summary("") == ""


def test_compact_summary_none() -> None:
    assert compact_summary(None) == ""  # type: ignore[arg-type]


# ── retrieve_relevant_memory ───────────────────────────────────────────────────


def test_retrieve_relevant_memory_no_entries(mem_db: sqlite3.Connection) -> None:
    entries, tags, topics = retrieve_relevant_memory(mem_db, "run1", "test goal", 1, None)
    assert isinstance(entries, list)
    assert isinstance(tags, list)
    assert isinstance(topics, list)


def test_retrieve_relevant_memory_with_matching_entry(mem_db: sqlite3.Connection) -> None:
    upsert_memory_entry(
        mem_db,
        "run1",
        "fact",
        "optimized query performance",
        ["query", "performance"],
        5.0,
        1,
        "2024-01-01T00:00:00",
    )
    upsert_memory_entry(
        mem_db,
        "run1",
        "fact",
        "unrelated styling change",
        ["styling"],
        3.0,
        1,
        "2024-01-01T00:00:00",
    )
    mem_db.commit()
    entries, tags, topics = retrieve_relevant_memory(mem_db, "run1", "optimize query", 5, None)
    contents = [e["content"] for e in entries]
    assert "optimized query performance" in contents


def test_retrieve_relevant_memory_excludes_other_run(mem_db: sqlite3.Connection) -> None:
    upsert_memory_entry(
        mem_db,
        "run_a",
        "fact",
        "run_a fact",
        ["run_a"],
        5.0,
        1,
        "2024-01-01T00:00:00",
    )
    upsert_memory_entry(
        mem_db,
        "run_b",
        "fact",
        "run_b fact",
        ["run_b"],
        5.0,
        1,
        "2024-01-01T00:00:00",
    )
    mem_db.commit()
    entries, tags, topics = retrieve_relevant_memory(mem_db, "run_a", "fact", 1, None)
    contents = [e["content"] for e in entries]
    assert "run_a fact" in contents
    assert "run_b fact" not in contents


# ── OperationalError fallbacks when memory_fts is unavailable ───────────────


def test_upsert_memory_entry_swallows_fts_error(mem_db: sqlite3.Connection) -> None:
    """If the FTS virtual table is missing or broken, the entry must still be persisted."""
    mem_db.execute("DROP TABLE memory_fts")
    upsert_memory_entry(
        mem_db,
        "run1",
        "fact",
        "no fts available",
        ["nope"],
        3.0,
        1,
        "2025-01-01T00:00:00Z",
    )
    mem_db.commit()
    row = mem_db.execute("SELECT content FROM memory_entries WHERE run_id = 'run1'").fetchone()
    assert row is not None
    assert row["content"] == "no fts available"


def test_prune_memory_entries_swallows_fts_error(mem_db: sqlite3.Connection) -> None:
    """Pruning must not raise if the FTS table is missing; entries still get pruned."""
    upsert_memory_entry(mem_db, "run1", "fact", "ancient low-importance entry", ["x"], 1.0, 1, "2020-01-01T00:00:00Z")
    upsert_memory_entry(mem_db, "run1", "summary", "recent summary", ["x"], 5.0, 100, "2025-01-01T00:00:00Z")
    mem_db.commit()
    mem_db.execute("DROP TABLE memory_fts")
    prune_memory_entries(mem_db, "run1", 100)
    remaining = mem_db.execute("SELECT content FROM memory_entries WHERE run_id = 'run1'").fetchall()
    contents = [r["content"] for r in remaining]
    assert "ancient low-importance entry" not in contents
    assert "recent summary" in contents


def test_retrieve_relevant_memory_swallows_fts_error(mem_db: sqlite3.Connection) -> None:
    """Retrieval must return memory rows even when FTS is unavailable."""
    upsert_memory_entry(
        mem_db,
        "run1",
        "fact",
        "fallback content",
        ["fallback"],
        4.0,
        1,
        "2025-01-01T00:00:00Z",
    )
    mem_db.commit()
    mem_db.execute("DROP TABLE memory_fts")
    entries, _tags, _topics = retrieve_relevant_memory(mem_db, "run1", "fallback", 3, None)
    assert any(e["content"] == "fallback content" for e in entries)
