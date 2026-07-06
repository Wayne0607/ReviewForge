"""Cross-PR precision: per-symbol risk attribution + comma-list import extraction.

Regression guard for two bugs found during the 3-PR live demo:
  1. `from x import a, b` only captured `a` (named-import regex stopped at first symbol).
  2. Importing a deserialization-risky symbol inherited a SQL-injection risk that lived
     in a *different* symbol of the same file (file-level over-propagation).
"""

import aiosqlite

from reviewforge.core.database import Database
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.cross_pr_analyzer import CrossPRAnalyzer
from reviewforge.engine.symbol_extractor import extract_imports


def test_named_import_list_extracts_all_symbols():
    imps = extract_imports("from demo_app.db import connect, run_query\n", "demo_app/user_routes.py")
    names = sorted(i.name for i in imps if i.source == "demo_app.db")
    assert names == ["connect", "run_query"]
    assert {i.line for i in imps} == {1}


def test_named_import_handles_aliases():
    imps = extract_imports("from pkg.mod import foo as f, bar\n", "x.py")
    names = sorted(i.name for i in imps if i.source == "pkg.mod")
    assert names == ["bar", "foo"]  # alias target stripped, original name kept


def _diff(file_path: str, body: str) -> str:
    return f"--- {file_path} (+9 -0)\n" + "\n".join("+" + line for line in body.splitlines())


async def test_cross_pr_propagates_only_imported_symbol_risk(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=None)  # no LLM → deterministic structural findings

    # --- PR-A: db.py defines run_query (sql-injection) and cache_load (insecure-deserialization) ---
    db_src = (
        "import pickle\n"
        "def connect(path):\n"
        "    return path\n"
        "def run_query(conn, table, raw):\n"
        "    conn.execute('SELECT * FROM ' + table + raw)\n"
        "def cache_load(blob):\n"
        "    return pickle.loads(blob)\n"
    )
    state_a = StateStore(
        pr_number=1,
        repo="o/r",
        head_sha="A",
        files_changed=["demo_app/db.py"],
        diff_summary=_diff("demo_app/db.py", db_src),
    )
    findings_a = [
        Finding(
            file="demo_app/db.py",
            line=5,
            severity="error",
            category="sql-injection",
            message="string-concat SQL",
            confidence=0.9,
            reviewer="security_reviewer",
            status="confirmed",
        ),
        Finding(
            file="demo_app/db.py",
            line=7,
            severity="error",
            category="insecure-deserialization",
            message="pickle.loads",
            confidence=0.9,
            reviewer="security_reviewer",
            status="confirmed",
        ),
    ]
    await analyzer.analyze("runA", state_a, findings_a)

    # Per-symbol risk is now populated and correctly separated.
    rq = await db.get_symbol("demo_app/db.py", "run_query")
    cl = await db.get_symbol("demo_app/db.py", "cache_load")
    assert "sql-injection" in rq["risk_categories"] and "insecure-deserialization" not in rq["risk_categories"]
    assert "insecure-deserialization" in cl["risk_categories"] and "sql-injection" not in cl["risk_categories"]

    # --- PR-C: session.py imports ONLY cache_load ---
    sess_src = "\nfrom demo_app.db import cache_load\ndef load_session(cookie):\n    return cache_load(cookie)\n"
    state_c = StateStore(
        pr_number=3,
        repo="o/r",
        head_sha="C",
        files_changed=["demo_app/session.py"],
        diff_summary=_diff("demo_app/session.py", sess_src),
    )
    cross = await analyzer.analyze("runC", state_c, existing_findings=[])
    cats = {f.category for f in cross}
    assert "cross-pr-insecure-deserialization" in cats  # the real propagation is still detected
    assert "cross-pr-sql-injection" not in cats  # the phantom risk is gone (precision fix)
    assert all(f.line == 4 for f in cross)
    await db.close()


async def test_cross_pr_normalizes_security_category_aliases(tmp_path):
    db = Database(tmp_path / "aliases.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=None)

    seed_src = "def risky_eval(expr):\n    return eval(expr)\n"
    state_a = StateStore(
        pr_number=10,
        repo="o/r",
        head_sha="A",
        files_changed=["demo_app/eval_sink.py"],
        diff_summary=_diff("demo_app/eval_sink.py", seed_src),
    )
    findings_a = [
        Finding(
            file="demo_app/eval_sink.py",
            line=2,
            severity="error",
            category="client-side-code-execution",
            message="eval executes attacker-controlled code",
            confidence=0.9,
            reviewer="security_reviewer",
            status="confirmed",
        )
    ]
    await analyzer.analyze("aliasA", state_a, findings_a)

    sym = await db.get_symbol("demo_app/eval_sink.py", "risky_eval")
    assert "code-injection" in sym["risk_categories"]

    consumer_src = "from demo_app.eval_sink import risky_eval\ndef run(expr):\n    return risky_eval(expr)\n"
    state_b = StateStore(
        pr_number=11,
        repo="o/r",
        head_sha="B",
        files_changed=["demo_app/eval_consumer.py"],
        diff_summary=_diff("demo_app/eval_consumer.py", consumer_src),
    )
    cross = await analyzer.analyze("aliasB", state_b, existing_findings=[])
    assert {f.category for f in cross} == {"cross-pr-code-injection"}
    relations = await db.get_relations_from_symbol("demo_app/eval_consumer.py", "run")
    assert any(r["relation_type"] == "call" and r["target_symbol"] == "risky_eval" for r in relations)
    await db.close()


async def test_cross_pr_ignores_stdlib_import_fuzzy_matches(tmp_path):
    db = Database(tmp_path / "stdlib.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=None)
    await db.upsert_file_risk("cross_pr_live/risky_ops.py", "critical", ["sql-injection"], 1, "old")

    state = StateStore(
        pr_number=20,
        repo="o/r",
        head_sha="S",
        files_changed=["demo_app/std_import.py"],
        diff_summary=_diff("demo_app/std_import.py", "import os\n\ndef run():\n    return os.getcwd()\n"),
    )

    assert await analyzer.analyze("stdlib", state, existing_findings=[]) == []
    await db.close()


async def test_symbol_risk_prefers_function_name_over_drifted_line(tmp_path):
    db = Database(tmp_path / "symbol_text.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=None)

    seed_src = (
        "def safe_first(value):\n"
        "    return value\n"
        "def risky_second(conn, raw):\n"
        "    return conn.execute(f'SELECT * FROM users WHERE name = {raw}')\n"
    )
    state_a = StateStore(
        pr_number=30,
        repo="o/r",
        head_sha="A",
        files_changed=["demo_app/sinks.py"],
        diff_summary=_diff("demo_app/sinks.py", seed_src),
    )
    await analyzer.analyze(
        "textA",
        state_a,
        [
            Finding(
                file="demo_app/sinks.py",
                line=1,
                severity="error",
                category="sql-injection",
                message="risky_second 函数拼接 SQL，存在注入风险",
                confidence=0.9,
                reviewer="security_reviewer",
                status="confirmed",
            )
        ],
    )

    safe = await db.get_symbol("demo_app/sinks.py", "safe_first")
    risky = await db.get_symbol("demo_app/sinks.py", "risky_second")
    assert safe["risk_categories"] == "[]"
    assert "sql-injection" in risky["risk_categories"]
    await db.close()


async def test_code_relations_migration_preserves_multiple_source_symbols(tmp_path):
    db_path = tmp_path / "old_relations.db"
    old = await aiosqlite.connect(db_path)
    await old.execute(
        """
        CREATE TABLE code_relations (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id        TEXT NOT NULL,
            source_file   TEXT NOT NULL,
            target_file   TEXT NOT NULL DEFAULT '',
            target_symbol TEXT NOT NULL DEFAULT '',
            relation_type TEXT NOT NULL,
            UNIQUE(run_id, source_file, target_file, target_symbol)
        )
        """
    )
    await old.commit()
    await old.close()

    db = Database(db_path)
    await db.connect()
    await db.upsert_relation("r1", "consumer.py", "sink.py", "danger", "call", source_symbol="route_a")
    await db.upsert_relation("r1", "consumer.py", "sink.py", "danger", "call", source_symbol="route_b")

    rows = await db.get_relations_from("consumer.py")
    assert sorted(r["source_symbol"] for r in rows) == ["route_a", "route_b"]
    await db.close()
