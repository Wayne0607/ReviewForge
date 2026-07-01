"""Cross-PR precision: per-symbol risk attribution + comma-list import extraction.

Regression guard for two bugs found during the 3-PR live demo:
  1. `from x import a, b` only captured `a` (named-import regex stopped at first symbol).
  2. Importing a deserialization-risky symbol inherited a SQL-injection risk that lived
     in a *different* symbol of the same file (file-level over-propagation).
"""

from reviewforge.core.database import Database
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.cross_pr_analyzer import CrossPRAnalyzer
from reviewforge.engine.symbol_extractor import extract_imports


def test_named_import_list_extracts_all_symbols():
    imps = extract_imports("from demo_app.db import connect, run_query\n", "demo_app/user_routes.py")
    names = sorted(i.name for i in imps if i.source == "demo_app.db")
    assert names == ["connect", "run_query"]


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
    sess_src = "from demo_app.db import cache_load\ndef load_session(cookie):\n    return cache_load(cookie)\n"
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
    await db.close()
