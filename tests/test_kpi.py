from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from scripts.kpi import (
    _family_chart,
    _find_root,
    _kb_summary,
    _metric_box,
    _scorecard,
    _timeline_chart,
    collect_all_metrics,
    extract_run_metrics,
    generate_dashboard,
    load_kb,
    main,
)


# ── load_kb ──────────────────────────────────────────────────────────────────


def test_load_kb_nonexistent_path(tmp_path: Path) -> None:
    result = load_kb(tmp_path / "nonexistent.yaml")
    assert result == {"entries": []}


def test_load_kb_valid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "kb.yaml"
    p.write_text("entries:\n  - type: bug_fix\n    description: fixed it\n")
    result = load_kb(p)
    assert "entries" in result
    assert len(result["entries"]) == 1
    assert result["entries"][0]["type"] == "bug_fix"


def test_load_kb_invalid_yaml(tmp_path: Path) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text(": broken yaml [[[\n")
    result = load_kb(p)
    assert result == {"entries": []}


def test_load_kb_empty_yaml(tmp_path: Path) -> None:
    p = tmp_path / "empty.yaml"
    p.write_text("")
    result = load_kb(p)
    assert result == {"entries": []}


# ── _find_root ────────────────────────────────────────────────────────────────


def test_find_root_finds_autocode_yaml(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "autocode.yaml").write_text("project: test\n")
    fake_file = tmp_path / "subdir" / "inner" / "kpi.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.write_text("")
    monkeypatch.setattr("scripts.kpi.__file__", str(fake_file))
    result = _find_root()
    assert result == tmp_path


def test_find_root_falls_back_to_git(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    fake_file = tmp_path / "subdir" / "inner" / "kpi.py"
    fake_file.parent.mkdir(parents=True)
    fake_file.write_text("")
    monkeypatch.setattr("scripts.kpi.__file__", str(fake_file))
    result = _find_root()
    assert result == tmp_path


# ── extract_run_metrics ──────────────────────────────────────────────────────


def _in_memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            goal_text TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            iteration_count INTEGER NOT NULL DEFAULT 0,
            latest_summary TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS iterations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            iteration_number INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            exit_code INTEGER,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            checkpoint_json TEXT NOT NULL,
            stdout_path TEXT NOT NULL,
            stderr_path TEXT NOT NULL,
            UNIQUE(run_id, iteration_number)
        );
    """)
    return conn


def test_extract_run_metrics_empty_db() -> None:
    conn = _in_memory_db()
    result = extract_run_metrics("nonexistent", conn)
    assert result == {}


def test_extract_run_metrics_single_iteration() -> None:
    conn = _in_memory_db()
    conn.execute(
        "INSERT INTO runs (run_id, goal_text, status, created_at, updated_at, iteration_count, latest_summary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("test-run-1", "Improve autocode", "running", "2025-01-01T00:00:00Z", "2025-01-01T01:00:00Z", 1, "Started"),
    )
    cp = {
        "summary": "Fixed a bug",
        "competitiveness": "promising",
        "branch_family": "tests",
        "family_novelty": "moderate",
        "evidence_quality": "model_level",
        "promotion_recommendation": "hold",
        "new_facts": ["fact1"],
        "decisions": ["dec1"],
        "files_touched": ["file1.py"],
    }
    conn.execute(
        "INSERT INTO iterations (run_id, iteration_number, started_at, finished_at, exit_code, "
        "status, summary, checkpoint_json, stdout_path, stderr_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "test-run-1",
            1,
            "2025-01-01T00:00:00Z",
            "2025-01-01T01:00:00Z",
            0,
            "continue",
            "Iteration 1",
            json.dumps(cp),
            "/tmp/stdout",
            "/tmp/stderr",
        ),
    )
    result = extract_run_metrics("test-run-1", conn)
    assert result["run_id"] == "test-run-1"
    assert result["status"] == "running"
    assert result["total_iterations"] == 1
    assert result["wins"] == 1
    assert result["win_rate"] == 100.0
    assert result["dominant_family"] == "tests"
    assert result["unique_families"] == 1
    assert result["consecutive_noncompetitive"] == 0
    assert len(result["checkpoints"]) == 1
    assert result["checkpoints"][0]["competitiveness"] == "promising"
    assert result["checkpoints"][0]["branch_family"] == "tests"
    assert result["checkpoints"][0]["new_facts"] == ["fact1"]
    assert result["checkpoints"][0]["files_touched"] == ["file1.py"]


def test_extract_run_metrics_no_checkpoint_json() -> None:
    conn = _in_memory_db()
    conn.execute(
        "INSERT INTO runs (run_id, goal_text, status, created_at, updated_at, iteration_count, latest_summary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("test-run-2", "Goal", "done", "2025-01-01T00:00:00Z", "2025-01-02T00:00:00Z", 2, ""),
    )
    conn.execute(
        "INSERT INTO iterations (run_id, iteration_number, started_at, finished_at, exit_code, "
        "status, summary, checkpoint_json, stdout_path, stderr_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "test-run-2",
            1,
            "2025-01-01T00:00:00Z",
            "2025-01-01T01:00:00Z",
            0,
            "continue",
            "It 1",
            "",
            "/tmp/stdout",
            "/tmp/stderr",
        ),
    )
    conn.execute(
        "INSERT INTO iterations (run_id, iteration_number, started_at, finished_at, exit_code, "
        "status, summary, checkpoint_json, stdout_path, stderr_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "test-run-2",
            2,
            "2025-01-02T00:00:00Z",
            "2025-01-02T01:00:00Z",
            1,
            "continue",
            "It 2",
            "",
            "/tmp/stdout",
            "/tmp/stderr",
        ),
    )
    result = extract_run_metrics("test-run-2", conn)
    assert result["total_iterations"] == 2
    assert result["wins"] == 0
    assert result["win_rate"] == 0.0
    assert result["consecutive_noncompetitive"] == 0  # "unknown" != "non_competitive"
    assert len(result["checkpoints"]) == 2
    assert result["checkpoints"][0]["competitiveness"] == "unknown"
    assert result["checkpoints"][1]["competitiveness"] == "unknown"


def test_extract_run_metrics_consecutive_noncompetitive() -> None:
    conn = _in_memory_db()
    conn.execute(
        "INSERT INTO runs (run_id, goal_text, status, created_at, updated_at, iteration_count, latest_summary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("test-run-3", "Goal", "running", "2025-01-01T00:00:00Z", "2025-01-03T00:00:00Z", 3, ""),
    )
    for i, comp in enumerate([("promising", 1), ("non_competitive", 2), ("non_competitive", 3)], 1):
        cp = json.dumps({"competitiveness": comp[0], "branch_family": "tests", "family_novelty": "low"})
        conn.execute(
            "INSERT INTO iterations (run_id, iteration_number, started_at, finished_at, exit_code, "
            "status, summary, checkpoint_json, stdout_path, stderr_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "test-run-3",
                comp[1],
                f"2025-01-0{i}T00:00:00Z",
                f"2025-01-0{i}T01:00:00Z",
                0,
                "continue",
                f"It {comp[1]}",
                cp,
                "/tmp/stdout",
                "/tmp/stderr",
            ),
        )
    result = extract_run_metrics("test-run-3", conn)
    assert result["consecutive_noncompetitive"] == 2
    assert result["wins"] == 1


def test_extract_run_metrics_goal_preview_truncated() -> None:
    conn = _in_memory_db()
    long_goal = "x" * 200
    conn.execute(
        "INSERT INTO runs (run_id, goal_text, status, created_at, updated_at, iteration_count, latest_summary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("test-run-4", long_goal, "running", "2025-01-01T00:00:00Z", "2025-01-01T01:00:00Z", 1, ""),
    )
    conn.execute(
        "INSERT INTO iterations (run_id, iteration_number, started_at, finished_at, exit_code, "
        "status, summary, checkpoint_json, stdout_path, stderr_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "test-run-4",
            1,
            "2025-01-01T00:00:00Z",
            "2025-01-01T01:00:00Z",
            0,
            "continue",
            "It 1",
            "{}",
            "/tmp/stdout",
            "/tmp/stderr",
        ),
    )
    result = extract_run_metrics("test-run-4", conn)
    assert len(result["goal_preview"]) == 120
    assert result["goal_preview"] == "x" * 120


# ── _metric_box ──────────────────────────────────────────────────────────────


def test_metric_box_basic() -> None:
    html = _metric_box("Iterations", "42", "#3b82f6")
    assert "Iterations" in html
    assert "42" in html
    assert "#3b82f6" in html


def test_metric_box_with_suffix() -> None:
    html = _metric_box("Wins", "5", "#22c55e", "83%")
    assert "Wins" in html
    assert "5" in html
    assert "83%" in html


# ── _timeline_chart ──────────────────────────────────────────────────────────


def test_timeline_chart_empty() -> None:
    html = _timeline_chart([])
    assert "No iteration data" in html


def test_timeline_chart_single() -> None:
    checkpoints = [{"iteration": 1, "competitiveness": "promising", "started_at": "2025-01-01"}]
    html = _timeline_chart(checkpoints)
    assert "<svg" in html
    assert "promising" in html
    assert "1" in html


def test_timeline_chart_multiple() -> None:
    checkpoints = [
        {"iteration": 1, "competitiveness": "promotable", "started_at": "2025-01-01"},
        {"iteration": 2, "competitiveness": "marginal", "started_at": "2025-01-02"},
        {"iteration": 3, "competitiveness": "non_competitive", "started_at": "2025-01-03"},
    ]
    html = _timeline_chart(checkpoints)
    assert "<svg" in html
    assert "promotable" in html
    assert "marginal" in html
    assert "non_competitive" in html


def test_timeline_chart_unknown_competitiveness() -> None:
    checkpoints = [{"iteration": 1, "competitiveness": "unknown", "started_at": "2025-01-01"}]
    html = _timeline_chart(checkpoints)
    assert "<svg" in html


def test_timeline_chart_missing_competitiveness() -> None:
    checkpoints = [{"iteration": 1, "started_at": "2025-01-01"}]
    html = _timeline_chart(checkpoints)
    assert "<svg" in html


def test_timeline_chart_unexpected_competitiveness() -> None:
    checkpoints = [{"iteration": 1, "competitiveness": "super_competitive", "started_at": "2025-01-01"}]
    html = _timeline_chart(checkpoints)
    assert "<svg" in html


# ── _family_chart ────────────────────────────────────────────────────────────


def test_family_chart_empty() -> None:
    html = _family_chart([], 0)
    assert "No family data" in html


def test_family_chart_single_family() -> None:
    html = _family_chart([("tests", 5)], 10)
    assert "tests" in html
    assert "5" in html
    assert "50%" in html


def test_family_chart_multiple_families() -> None:
    html = _family_chart([("tests", 5), ("features", 3)], 10)
    assert "tests" in html
    assert "features" in html
    assert "50%" in html
    assert "30%" in html


def test_family_chart_sorted_by_count() -> None:
    families = [("features", 1), ("tests", 10), ("bugs", 3)]
    html = _family_chart(families, 14)
    assert html.index("tests") < html.index("features")
    assert html.index("bugs") < html.index("features")


# ── _kb_summary ──────────────────────────────────────────────────────────────


def test_kb_summary_empty() -> None:
    html = _kb_summary({"entries": []})
    assert "No KB entries yet" in html


def test_kb_summary_no_entries_key() -> None:
    html = _kb_summary({})
    assert "No KB entries yet" in html


def test_kb_summary_with_entries() -> None:
    kb = {
        "entries": [
            {"type": "bug_fix", "description": "fixed a bug"},
            {"type": "new_feature", "description": "added feature"},
            {"type": "dead_end", "description": "nope"},
        ]
    }
    html = _kb_summary(kb)
    assert "bug_fix" in html
    assert "new_feature" in html
    assert "dead_end" in html
    assert "3" in html
    assert "Total:" in html


def test_kb_summary_counts_by_type() -> None:
    kb = {
        "entries": [
            {"type": "bug_fix"},
            {"type": "bug_fix"},
            {"type": "new_feature"},
        ]
    }
    html = _kb_summary(kb)
    assert "bug_fix" in html
    assert "new_feature" in html
    assert "2" in html
    assert "1" in html


# ── _scorecard ───────────────────────────────────────────────────────────────


def _sample_metrics() -> dict:
    return {
        "run_id": "20250101-test-run-12345678",
        "status": "running",
        "goal_preview": "Improve autocode recursively",
        "created_at": "2025-01-01T00:00:00Z",
        "total_iterations": 3,
        "first_iteration": "2025-01-01T00:00:00Z",
        "last_iteration": "2025-01-03T00:00:00Z",
        "checkpoints": [
            {"iteration": 1, "competitiveness": "promising", "started_at": "2025-01-01"},
            {"iteration": 2, "competitiveness": "marginal", "started_at": "2025-01-02"},
            {"iteration": 3, "competitiveness": "promotable", "started_at": "2025-01-03"},
        ],
        "comp_counts": {"promising": 1, "marginal": 1, "promotable": 1},
        "status_counts": {"continue": 3},
        "novelty_counts": {"moderate": 3},
        "families": [("tests", 2), ("features", 1)],
        "unique_families": 2,
        "dominant_family": "tests",
        "dominant_count": 2,
        "consecutive_noncompetitive": 0,
        "wins": 2,
        "win_rate": 66.7,
        "trend_comps": [(1, "promising"), (2, "marginal"), (3, "promotable")],
    }


def test_scorecard_basic() -> None:
    m = _sample_metrics()
    html = _scorecard(m)
    assert m["run_id"][:20] in html
    assert "3 iterations" in html
    assert "running" in html
    assert "Goal:" in html


def test_scorecard_shows_all_metric_boxes() -> None:
    m = _sample_metrics()
    html = _scorecard(m)
    assert "Iterations" in html
    assert "Wins" in html
    assert "Families" in html
    assert "Consec." in html
    assert "Dominant" in html


def test_scorecard_includes_chart() -> None:
    m = _sample_metrics()
    html = _scorecard(m)
    assert "<svg" in html


def test_scorecard_includes_family_distribution() -> None:
    m = _sample_metrics()
    html = _scorecard(m)
    assert "Family distribution" in html


def test_scorecard_empty_goal_preview() -> None:
    m = _sample_metrics()
    m["goal_preview"] = ""
    html = _scorecard(m)
    assert "Goal:" in html


# ── generate_dashboard ───────────────────────────────────────────────────────


def _sample_kb() -> dict:
    return {
        "entries": [
            {"type": "bug_fix", "description": "fix 1"},
            {"type": "dead_end", "description": "dead 1"},
        ]
    }


def test_generate_dashboard_basic() -> None:
    metrics = [_sample_metrics()]
    kb = _sample_kb()
    html = generate_dashboard(metrics, kb, {})
    assert "<!DOCTYPE html>" in html
    assert "AutoOpencode Dashboard" in html
    assert "1 run(s)" in html
    assert "3 iteration(s)" in html
    assert "2 win(s)" in html


def test_generate_dashboard_empty_metrics() -> None:
    html = generate_dashboard([], {"entries": []}, {})
    assert "<!DOCTYPE html>" in html
    assert "0 run(s)" in html
    assert "0 iteration(s)" in html


def test_generate_dashboard_aggregate_section() -> None:
    metrics = [_sample_metrics()]
    html = generate_dashboard(metrics, _sample_kb(), {})
    assert "Aggregate" in html
    assert "Top families" in html
    assert "Knowledge Base" in html


def test_generate_dashboard_multiple_runs() -> None:
    m1 = _sample_metrics()
    m2 = dict(_sample_metrics())
    m2["run_id"] = "20250102-another-run"
    m2["total_iterations"] = 5
    m2["wins"] = 3
    html = generate_dashboard([m1, m2], _sample_kb(), {})
    assert "2 run(s)" in html
    assert "8 iteration(s)" in html
    assert "5 win(s)" in html


def test_generate_dashboard_shows_runs_section() -> None:
    metrics = [_sample_metrics()]
    html = generate_dashboard(metrics, _sample_kb(), {})
    assert "Runs" in html
    assert "Scorecard" not in html  # section header is "Runs", individual cards use _scorecard


# ── collect_all_metrics ──────────────────────────────────────────────────────


def _sample_run_row(run_id: str = "test-run-1") -> None:
    """Populate the in-memory DB with a single run row."""
    pass  # handled inline in each test


def test_collect_all_metrics_no_db(tmp_path: Path) -> None:
    metrics, kb, meta_kb = collect_all_metrics(
        db_path=tmp_path / "nonexistent.sqlite3",
        kb_path=tmp_path / "nokb.yaml",
        meta_kb_path=tmp_path / "nometa.yaml",
    )
    assert metrics == []
    assert kb == {"entries": []}
    assert meta_kb == {"entries": []}


def test_collect_all_metrics_with_data(tmp_path: Path) -> None:
    db_path = tmp_path / "test.sqlite3"
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            goal_text TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            iteration_count INTEGER NOT NULL DEFAULT 0,
            latest_summary TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE iterations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            iteration_number INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            exit_code INTEGER,
            status TEXT NOT NULL,
            summary TEXT NOT NULL,
            checkpoint_json TEXT NOT NULL,
            stdout_path TEXT NOT NULL,
            stderr_path TEXT NOT NULL,
            UNIQUE(run_id, iteration_number)
        );
        INSERT INTO runs (run_id, goal_text, status, created_at, updated_at, iteration_count, latest_summary)
        VALUES ('test-run', 'Improve', 'running', '2025-01-01T00:00:00Z', '2025-01-01T01:00:00Z', 1, '');
        INSERT INTO iterations (run_id, iteration_number, started_at, finished_at, exit_code,
            status, summary, checkpoint_json, stdout_path, stderr_path)
        VALUES ('test-run', 1, '2025-01-01T00:00:00Z', '2025-01-01T01:00:00Z', 0,
            'continue', 'It 1', '{"competitiveness":"promising","branch_family":"tests","family_novelty":"moderate"}',
            '/tmp/stdout', '/tmp/stderr');
    """)
    conn.commit()
    conn.close()

    kb_path = tmp_path / "kb.yaml"
    kb_path.write_text("entries:\n  - type: bug_fix\n    description: fixed\n")
    meta_kb_path = tmp_path / "meta.yaml"
    meta_kb_path.write_text("entries: []\n")

    metrics, kb, meta_kb = collect_all_metrics(
        db_path=db_path,
        kb_path=kb_path,
        meta_kb_path=meta_kb_path,
    )
    assert len(metrics) == 1
    assert metrics[0]["run_id"] == "test-run"
    assert metrics[0]["total_iterations"] == 1
    assert metrics[0]["wins"] == 1
    assert len(kb["entries"]) == 1
    assert meta_kb["entries"] == []


def test_collect_all_metrics_db_exception(tmp_path: Path) -> None:
    """Broken DB file should not crash — warning logged, empty list returned."""
    db_path = tmp_path / "corrupt.sqlite3"
    db_path.write_text("not a real sqlite file")
    kb_path = tmp_path / "kb.yaml"
    kb_path.write_text("entries: []\n")
    meta_kb_path = tmp_path / "meta.yaml"
    meta_kb_path.write_text("entries: []\n")

    import warnings

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        metrics, kb, meta_kb = collect_all_metrics(
            db_path=db_path,
            kb_path=kb_path,
            meta_kb_path=meta_kb_path,
        )
        assert len(metrics) == 0
        assert len([x for x in w if "Failed to read autopilot database" in str(x.message)]) >= 1


def test_collect_all_metrics_empty_db_no_runs(tmp_path: Path) -> None:
    """DB with schema but no rows should return empty metrics."""
    db_path = tmp_path / "empty.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE runs (run_id TEXT PRIMARY KEY, goal_text TEXT, status TEXT, created_at TEXT, updated_at TEXT, iteration_count INTEGER, latest_summary TEXT);
        CREATE TABLE iterations (id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, iteration_number INTEGER, started_at TEXT, finished_at TEXT, exit_code INTEGER, status TEXT, summary TEXT, checkpoint_json TEXT, stdout_path TEXT, stderr_path TEXT, UNIQUE(run_id, iteration_number));
    """)
    conn.close()

    metrics, kb, meta_kb = collect_all_metrics(
        db_path=db_path,
        kb_path=tmp_path / "kb.yaml",
        meta_kb_path=tmp_path / "meta.yaml",
    )
    assert metrics == []


# ── main ─────────────────────────────────────────────────────────────────────


def test_main_no_data(tmp_path: Path, capsys) -> None:
    result = main(
        output_dir=tmp_path,
        db_path=tmp_path / "nonexistent.sqlite3",
        kb_path=tmp_path / "nokb.yaml",
        meta_kb_path=tmp_path / "nometa.yaml",
    )
    assert result == 1
    captured = capsys.readouterr()
    assert "No autopilot runs found" in captured.out


def test_main_with_data(tmp_path: Path, capsys) -> None:
    db_path = tmp_path / "test.sqlite3"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, goal_text TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            iteration_count INTEGER NOT NULL DEFAULT 0, latest_summary TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE iterations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
            iteration_number INTEGER NOT NULL, started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL, exit_code INTEGER,
            status TEXT NOT NULL, summary TEXT NOT NULL,
            checkpoint_json TEXT NOT NULL, stdout_path TEXT NOT NULL, stderr_path TEXT NOT NULL,
            UNIQUE(run_id, iteration_number)
        );
        INSERT INTO runs VALUES ('test-run', 'Goal', 'running', '2025-01-01T00:00:00Z', '2025-01-01T01:00:00Z', 1, '');
        INSERT INTO iterations VALUES (1, 'test-run', 1, '2025-01-01T00:00:00Z', '2025-01-01T01:00:00Z', 0,
            'continue', 'It 1', '{"competitiveness":"promising","branch_family":"tests","family_novelty":"moderate"}',
            '/tmp/stdout', '/tmp/stderr');
    """)
    conn.close()

    kb_path = tmp_path / "kb.yaml"
    kb_path.write_text("entries: []\n")
    meta_kb_path = tmp_path / "meta.yaml"
    meta_kb_path.write_text("entries: []\n")

    result = main(
        output_dir=tmp_path,
        db_path=db_path,
        kb_path=kb_path,
        meta_kb_path=meta_kb_path,
    )
    assert result == 0
    captured = capsys.readouterr()
    assert "Dashboard written to" in captured.out
    # Check the dashboard file was created
    html_files = list(tmp_path.glob("dashboard_*.html"))
    assert len(html_files) == 1
    html = html_files[0].read_text(encoding="utf-8")
    assert "AutoOpencode Dashboard" in html
