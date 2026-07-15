"""Cross-PR precision: per-symbol risk attribution + comma-list import extraction.

Regression guard for two bugs found during the 3-PR live demo:
  1. `from x import a, b` only captured `a` (named-import regex stopped at first symbol).
  2. Importing a deserialization-risky symbol inherited a SQL-injection risk that lived
     in a *different* symbol of the same file (file-level over-propagation).
"""

import asyncio
import json
import re
from types import SimpleNamespace

import aiosqlite

from reviewforge.core.database import Database
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.cross_pr_analyzer import (
    _MAX_LLM_USER_PROMPT_CHARS,
    CrossPRAnalyzer,
    CrossPRChain,
)
from reviewforge.engine.symbol_extractor import extract_calls, extract_diff_calls, extract_diff_symbols, extract_imports


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
    lines = body.splitlines()
    return f"--- {file_path} (+{len(lines)} -0)\n@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + line for line in lines)


class _FakeGitHub:
    def __init__(self, contents: dict[tuple[str, str, str], str | Exception] | None = None) -> None:
        self.contents = contents or {}
        self.calls: list[tuple[str, str, str]] = []

    async def get_file_content(self, repo: str, ref: str, file_path: str) -> str:
        key = (repo, ref, file_path)
        self.calls.append(key)
        await asyncio.sleep(0)
        value = self.contents.get(key, FileNotFoundError(file_path))
        if isinstance(value, Exception):
            raise value
        return value


async def _seed_completed_risk(
    db: Database,
    analyzer: CrossPRAnalyzer,
    *,
    run_id: str,
    repo: str,
    pr_number: int,
    head_sha: str,
    file_path: str,
    symbol_name: str = "danger",
    category: str = "sql-injection",
) -> None:
    await db.create_run(run_id, repo, pr_number, head_sha, "seed-base")
    body = f"def {symbol_name}(raw):\n    return eval(raw)\n"
    state = StateStore(
        pr_number=pr_number,
        repo=repo,
        head_sha=head_sha,
        base_sha="seed-base",
        files_changed=[file_path],
        diff_summary=_diff(file_path, body),
    )
    await analyzer.analyze(
        run_id,
        state,
        [
            Finding(
                file=file_path,
                line=2,
                severity="error",
                category=category,
                message=f"{symbol_name} is an unsafe sink",
                confidence=0.95,
                reviewer="security_reviewer",
                status="confirmed",
            )
        ],
    )
    await db.complete_run(run_id, {})


def test_diff_symbol_and_call_lines_use_right_side_coordinates():
    patch = (
        "@@ -40,2 +80,5 @@\n"
        " context\n"
        "+from demo_app.db import run_query\n"
        "+def route(raw):\n"
        "+    return run_query(raw)\n"
        " tail\n"
    )

    symbols, imports = extract_diff_symbols(patch, "demo_app/routes.py")
    calls = extract_diff_calls(patch, "demo_app/routes.py")

    assert [(s.name, s.line) for s in symbols] == [("route", 82)]
    assert [(i.name, i.line) for i in imports] == [("run_query", 81)]
    assert any(c.callee == "run_query" and c.line == 83 for c in calls)


def test_diff_calls_ignore_call_shapes_inside_strings_comments_and_regex_literals():
    cases = {
        "consumer.py": ("text = \"danger(raw)\"\ndoc = '''danger(raw)'''\n# danger(raw)\nvalue = safe(raw)"),
        "consumer.ts": (
            'const text = "danger(raw)";\n'
            "const template = `danger(raw)`;\n"
            "const pattern = /danger(raw)/;\n"
            "// danger(raw)\n"
            "interface Local { danger(raw: string): void; }\n"
            "type Shape = { danger(raw: string): void };\n"
            "const api = { danger(raw: string) { return raw; } };\n"
            "class Service { danger(raw: string) { return raw; } }\n"
            "abstract class AbstractService { abstract danger(\n  raw: string\n): void; }\n"
            "const asserted = <Danger>raw;\n"
            "safe(raw);"
        ),
        "consumer.go": ("package p\nvar text = `danger(raw)`\n// danger(raw)\nfunc run() { safe(raw) }"),
        "Consumer.java": (
            'String text = "danger(raw)";\nString block = """danger(raw)""";\n/* danger(raw) */\nsafe(raw);'
        ),
        "consumer.rs": ('let text = r#"danger(raw)"#;\n// danger(raw)\nsafe(raw);'),
        "consumer.rb": ('text = "danger(raw)"\ndoc = <<~TEXT\ndanger(raw)\nTEXT\n# danger(raw)\nsafe(raw)'),
    }

    for file_path, body in cases.items():
        calls = extract_diff_calls(_diff(file_path, body), file_path)
        assert "danger" not in {call.callee for call in calls}, file_path
        assert "safe" in {call.callee for call in calls}, file_path


def test_javascript_regex_literals_after_arrow_and_yield_are_not_calls():
    body = "const matcher = () => /danger(raw)/gi;\nfunction* patterns() { yield /danger(raw)/; }\nsafe(raw);"

    calls = extract_diff_calls(_diff("consumer.ts", body), "consumer.ts")

    assert "danger" not in {call.callee for call in calls}
    assert "safe" in {call.callee for call in calls}


def test_imports_inside_comments_and_strings_do_not_create_bindings():
    body = (
        "# from pkg.sink import danger\n"
        'doc = """from pkg.sink import danger"""\n'
        "def route(raw):\n"
        "    return danger(raw)\n"
    )

    _symbols, imports = extract_diff_symbols(_diff("consumer.py", body), "consumer.py")

    assert not any(item.source == "pkg.sink" for item in imports)


def test_python_exact_binding_requires_a_complete_unshadowed_runtime_import():
    safe = "from pkg.sink import danger\n\ndef route(raw):\n    return danger(raw)\n"
    safe_call = next(
        call for call in extract_diff_calls(_diff("consumer.py", safe), "consumer.py") if call.callee == "danger"
    )
    assert safe_call.binding_proven

    unsafe_bodies = [
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from pkg.sink import danger\n"
            "def route(raw):\n"
            "    return danger(raw)\n"
        ),
        (
            "from pkg.sink import danger\n"
            "def route(xs, safe_functions):\n"
            "    return [danger(x) for danger in safe_functions for x in xs]\n"
        ),
        ("from pkg.sink import danger\ndef route(raw):\n    danger = lambda value: value\n    return danger(raw)\n"),
        "from pkg.sink import danger\ndel danger\ndef route(raw):\n    return danger(raw)\n",
    ]
    for body in unsafe_bodies:
        danger_calls = [
            call for call in extract_diff_calls(_diff("consumer.py", body), "consumer.py") if call.callee == "danger"
        ]
        assert danger_calls and not any(call.binding_proven for call in danger_calls), body

    modified_patch = "@@ -10,0 +10,4 @@\n+from pkg.sink import danger\n+\n+def route(raw):\n+    return danger(raw)\n"
    modified_call = next(call for call in extract_diff_calls(modified_patch, "consumer.py") if call.callee == "danger")
    assert not modified_call.binding_proven

    multi_hunk_patch = _diff("consumer.py", safe) + "\n@@ -90,0 +91,1 @@\n+# later old-file context"
    multi_hunk_call = next(
        call for call in extract_diff_calls(multi_hunk_patch, "consumer.py") if call.callee == "danger"
    )
    assert not multi_hunk_call.binding_proven


def test_declaration_filter_preserves_real_calls_followed_by_blocks():
    cases = {
        "consumer.ts": "if (danger(raw)) { handle(); }",
        "consumer.go": "if danger(raw) { handle() }",
        "Consumer.java": "Object value = new Danger() { };",
        "consumer.rs": "if danger(raw) { handle(); }",
        "consumer.rb": "danger(raw) { |value| handle(value) }",
    }

    for file_path, body in cases.items():
        calls = extract_diff_calls(_diff(file_path, body), file_path)
        expected = "Danger" if file_path.endswith(".java") else "danger"
        assert expected in {call.callee for call in calls}, file_path


async def test_string_only_import_reference_is_not_an_exact_cross_pr_call(tmp_path):
    db = Database(tmp_path / "string_call.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=None)
    repo = "owner/repo"
    await _seed_completed_risk(
        db,
        analyzer,
        run_id="seed",
        repo=repo,
        pr_number=1,
        head_sha="base-head",
        file_path="pkg/sink.py",
        category="code-injection",
    )

    body = 'from pkg.sink import danger\n\ndef route(raw):\n    text = "danger(raw)"\n    return raw\n'
    state = StateStore(
        pr_number=2,
        repo=repo,
        head_sha="consumer-head",
        base_sha="base-head",
        files_changed=["consumer.py"],
        diff_summary=_diff("consumer.py", body),
    )

    assert await analyzer.analyze("consumer", state, []) == []
    await db.close()


async def test_exact_import_call_requires_semantic_confirmation(tmp_path):
    db = Database(tmp_path / "semantic_cross_gate.db")
    await db.connect()
    llm = _RejectEveryChainLLM()
    analyzer = CrossPRAnalyzer(db, llm=llm)
    repo = "owner/repo"
    await _seed_completed_risk(
        db,
        analyzer,
        run_id="seed",
        repo=repo,
        pr_number=1,
        head_sha="base-head",
        file_path="pkg/sink.py",
        category="sql-injection",
    )
    body = 'from pkg.sink import danger\n\ndef route():\n    return danger("fixed-safe-value")\n'
    state = StateStore(
        pr_number=2,
        repo=repo,
        head_sha="consumer-head",
        base_sha="base-head",
        files_changed=["consumer.py"],
        diff_summary=_diff("consumer.py", body),
    )

    assert await analyzer.analyze("consumer", state, []) == []
    assert llm.invocations == 1
    await db.close()


async def test_go_interface_method_signature_is_not_an_exact_cross_pr_call(tmp_path):
    db = Database(tmp_path / "go_interface_call.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=None)
    repo = "owner/repo"
    await db.create_run("seed", repo, 1, "base-head", "parent")
    await db.upsert_symbol(
        file_path="pkg/sink.go",
        symbol_name="Danger",
        symbol_type="function",
        run_id="seed",
        pr_number=1,
        language="go",
        risk_level="critical",
        risk_categories=["code-injection"],
    )
    await db.upsert_file_risk("pkg/sink.go", "critical", ["code-injection"], 1, "seed")
    await db.complete_run("seed", {})

    body = 'package consumer\nimport sink "pkg/sink"\ntype Local interface {\n  sink.Danger()\n}'
    state = StateStore(
        pr_number=2,
        repo=repo,
        head_sha="consumer-head",
        base_sha="base-head",
        files_changed=["consumer.go"],
        diff_summary=_diff("consumer.go", body),
    )

    assert "Danger" not in {call.callee for call in extract_diff_calls(state.diff_summary, "consumer.go")}
    assert await analyzer.analyze("consumer", state, []) == []
    await db.close()


async def test_typescript_declarations_are_not_exact_cross_pr_calls(tmp_path):
    db = Database(tmp_path / "typescript_declaration_call.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=None)
    repo = "owner/repo"
    await db.create_run("seed", repo, 1, "base-head", "parent")
    await db.upsert_symbol(
        file_path="pkg/sink.ts",
        symbol_name="danger",
        symbol_type="function",
        run_id="seed",
        pr_number=1,
        language="typescript",
        risk_level="critical",
        risk_categories=["code-injection"],
    )
    await db.upsert_file_risk("pkg/sink.ts", "critical", ["code-injection"], 1, "seed")
    await db.complete_run("seed", {})

    body = (
        'import { danger, Danger } from "pkg/sink";\n'
        "interface Local { danger(raw: string): void; }\n"
        "type Shape = { danger(raw: string): void };\n"
        "const api = { danger(raw: string) { return raw; } };\n"
        "class Service { danger(raw: string) { return raw; } }\n"
        "abstract class AbstractService { abstract danger(\n  raw: string\n): void; }\n"
        "const asserted = <Danger>raw;"
    )
    state = StateStore(
        pr_number=2,
        repo=repo,
        head_sha="consumer-head",
        base_sha="base-head",
        files_changed=["consumer.ts"],
        diff_summary=_diff("consumer.ts", body),
    )

    calls = extract_diff_calls(state.diff_summary, "consumer.ts")
    assert {"danger", "Danger"}.isdisjoint(call.callee for call in calls)
    assert await analyzer.analyze("consumer", state, []) == []
    await db.close()


def test_go_block_import_preserves_alias_and_right_side_coordinate():
    patch = (
        "@@ -0,0 +10,8 @@\n"
        "+package consumer\n"
        "+import (\n"
        '+\t"fmt"\n'
        '+\tseed "gauntlet_fullstack/seed_go"\n'
        "+)\n"
        "+func report(id string) {\n"
        "+\tseed.RunAccountQuery(nil, id)\n"
        "+}\n"
    )

    _symbols, imports = extract_diff_symbols(patch, "gauntlet_services/consumer.go")
    calls = extract_diff_calls(patch, "gauntlet_services/consumer.go")

    assert any(
        item.source == "gauntlet_fullstack/seed_go" and item.local_name == "seed" and item.line == 13
        for item in imports
    )
    assert any(item.receiver == "seed" and item.callee == "RunAccountQuery" and item.line == 16 for item in calls)


def test_cross_pr_file_slice_preserves_rename_headers_and_stops_at_next_summary_file():
    summary = (
        "--- new.py (+1 -0)\n"
        "diff --git a/old.py b/new.py\n"
        "rename from old.py\n"
        "rename to new.py\n"
        "--- a/old.py\n"
        "+++ b/new.py\n"
        "@@ -7,2 +7,3 @@\n"
        " context\n"
        "+def renamed_function(): pass\n"
        " tail\n"
        "--- other.py (+1 -0)\n"
        "@@ -0,0 +1,1 @@\n"
        "+def unrelated(): pass\n"
    )
    analyzer = CrossPRAnalyzer.__new__(CrossPRAnalyzer)

    file_diff = analyzer._extract_file_diff(summary, "new.py")
    symbols, _imports = extract_diff_symbols(file_diff, "new.py")

    assert "--- a/old.py" in file_diff
    assert "other.py" not in file_diff
    assert [(s.name, s.line) for s in symbols] == [("renamed_function", 8)]


async def test_cross_pr_propagates_only_imported_symbol_risk(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=_ConfirmEveryChainLLM())

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
    sess_diff = (
        "--- demo_app/session.py (+3 -0)\n"
        "@@ -40,1 +80,4 @@\n"
        " existing_session_marker = True\n"
        "+from demo_app.db import cache_load\n"
        "+def load_session(cookie):\n"
        "+    return cache_load(cookie)\n"
    )
    state_c = StateStore(
        pr_number=3,
        repo="o/r",
        head_sha="C",
        files_changed=["demo_app/session.py"],
        diff_summary=sess_diff,
    )
    cross = await analyzer.analyze("runC", state_c, existing_findings=[])
    cats = {f.category for f in cross}
    assert "cross-pr-insecure-deserialization" in cats  # the real propagation is still detected
    assert "cross-pr-sql-injection" not in cats  # the phantom risk is gone (precision fix)
    assert all(f.line == 83 for f in cross)
    await db.close()


async def test_cross_pr_normalizes_security_category_aliases(tmp_path):
    db = Database(tmp_path / "aliases.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=_ConfirmEveryChainLLM())

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


async def test_ambiguous_historical_import_stays_llm_gated(tmp_path):
    db = Database(tmp_path / "ambiguous_import.db")
    await db.connect()
    llm = _RejectEveryChainLLM()
    analyzer = CrossPRAnalyzer(db, llm=llm)
    repo = "owner/repo"

    await db.create_run("base", repo, 1, "base-head", "parent")
    for file_path in ("app/pkg/sink.py", "vendor/pkg/sink.py"):
        await db.upsert_symbol(
            file_path=file_path,
            symbol_name="danger",
            symbol_type="function",
            run_id="base",
            pr_number=1,
            language="python",
            risk_level="critical",
            risk_categories=["code-injection"],
        )
        await db.upsert_file_risk(file_path, "critical", ["code-injection"], 1, "base")
    await db.complete_run("base", {})

    body = "from pkg.sink import danger\n\ndef route(raw):\n    return danger(raw)\n"
    state = StateStore(
        pr_number=2,
        repo=repo,
        head_sha="consumer-head",
        base_sha="base-head",
        files_changed=["consumer.py"],
        diff_summary=_diff("consumer.py", body),
    )
    findings = await analyzer.analyze("consumer", state, [])
    await db.close()

    assert findings == []
    assert llm.invocations == 1


async def test_ambiguous_historical_import_is_not_confirmed_without_llm(tmp_path):
    db = Database(tmp_path / "ambiguous_import_no_llm.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=None)
    repo = "owner/repo"

    await db.create_run("base", repo, 1, "base-head", "parent")
    for file_path in ("app/pkg/sink.py", "vendor/pkg/sink.py"):
        await db.upsert_symbol(
            file_path=file_path,
            symbol_name="danger",
            symbol_type="function",
            run_id="base",
            pr_number=1,
            language="python",
            risk_level="critical",
            risk_categories=["code-injection"],
        )
        await db.upsert_file_risk(file_path, "critical", ["code-injection"], 1, "base")
    await db.complete_run("base", {})

    body = "from pkg.sink import danger\n\ndef route(raw):\n    return danger(raw)\n"
    state = StateStore(
        pr_number=2,
        repo=repo,
        head_sha="consumer-head",
        base_sha="base-head",
        files_changed=["consumer.py"],
        diff_summary=_diff("consumer.py", body),
    )
    findings = await analyzer.analyze("consumer", state, [])
    await db.close()

    assert findings == []


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


def test_symbol_ranges_cover_decorators_multiline_signatures_and_real_body_ends():
    python_source = """@security_boundary(
    \"operator\",
)
async def run_tool(
    command: str,
) -> int:
    return await execute(command)
"""
    python_symbols, _ = extract_diff_symbols(_diff("seed.py", python_source), "seed.py")
    assert [(item.name, item.line, item.start_line, item.end_line) for item in python_symbols] == [
        ("run_tool", 4, 1, 7)
    ]

    go_source = """package seed

// FetchInternal performs the network request.
func FetchInternal(
    url string,
) (string, error) {
    return url, nil
}


// RunMaintenance executes the selected maintenance tool.
func RunMaintenance(binary string) error {
    return nil
}
"""
    go_symbols, _ = extract_diff_symbols(_diff("seed.go", go_source), "seed.go")
    assert [(item.name, item.line, item.start_line, item.end_line) for item in go_symbols] == [
        ("FetchInternal", 4, 3, 8),
        ("RunMaintenance", 12, 11, 14),
    ]
    unscoped_gap = Finding(file="seed.go", line=9, message="anchor too far from either declaration")
    assert CrossPRAnalyzer._enclosing_symbol_by_line(unscoped_gap, go_symbols) is None

    typescript_source = """export class Runner {
  first(value: string) {
    return value;
  }

  @Audit(
    \"security\",
  )
  run(
    command: string,
  ): string {
    return command;
  }
}
"""
    typescript_symbols, _ = extract_diff_symbols(_diff("runner.ts", typescript_source), "runner.ts")
    assert [(item.name, item.line, item.start_line, item.end_line) for item in typescript_symbols] == [
        ("Runner", 1, 1, 14),
        ("first", 2, 2, 4),
        ("run", 9, 6, 13),
    ]
    between_methods = Finding(file="runner.ts", line=5, message="anchor drift before decorated method")
    assert CrossPRAnalyzer._enclosing_symbol_by_line(between_methods, typescript_symbols).name == "run"

    java_source = """@Guarded(
    value = \"operator\"
)
public static
String execute(
    String command
) throws Exception {
    return command;
}
"""
    java_symbols, _ = extract_diff_symbols(_diff("Runner.java", java_source), "Runner.java")
    assert [(item.name, item.line, item.start_line, item.end_line) for item in java_symbols] == [("execute", 5, 1, 9)]

    destructured_typescript = """export function RawProfileCard({ html }: { html: string }) {
  return <article dangerouslySetInnerHTML={{ __html: html }} />;
}
"""
    destructured_symbols, _ = extract_diff_symbols(
        _diff("seed_frontend.tsx", destructured_typescript),
        "seed_frontend.tsx",
    )
    assert [(item.name, item.line, item.start_line, item.end_line) for item in destructured_symbols] == [
        ("RawProfileCard", 1, 1, 3)
    ]
    online_anchor = Finding(
        file="seed_frontend.tsx",
        line=2,
        category="xss",
        message="dangerouslySetInnerHTML renders potentially unsafe DOM content.",
        status="confirmed",
        verified_by="judge",
    )
    assert CrossPRAnalyzer._enclosing_symbol_by_line(online_anchor, destructured_symbols).name == "RawProfileCard"

    expression_arrow = """export const buildPayload = (value: string) => ({
  raw: value,
  render: () => value,
});
"""
    arrow_symbols, _ = extract_diff_symbols(_diff("payload.ts", expression_arrow), "payload.ts")
    assert [(item.name, item.line, item.start_line, item.end_line) for item in arrow_symbols] == [
        ("buildPayload", 1, 1, 4)
    ]

    object_return_types = {
        "factory.ts": """export function make(): { html: string } {
  return { html: "safe" };
}
""",
        "factory.go": """package factory
func makeValue() struct { X int } {
  return struct { X int }{X: 1}
}
""",
    }
    expected_ranges = {"factory.ts": ("make", 1, 1, 3), "factory.go": ("makeValue", 2, 2, 4)}
    for file_path, source in object_return_types.items():
        symbols, _ = extract_diff_symbols(_diff(file_path, source), file_path)
        assert [(item.name, item.line, item.start_line, item.end_line) for item in symbols] == [
            expected_ranges[file_path]
        ]


def test_braced_symbol_ranges_do_not_borrow_an_adjacent_declaration_body():
    typescript_cases = {
        "multiline-arrow.ts": (
            """export const
build = ({ raw }: { raw: string }): { value: string } => ({
  value: raw,
});

export function next() {
  return 1;
}
""",
            [("build", 2, 1, 4), ("next", 6, 6, 8)],
        ),
        "callback-return.ts": (
            """export function make(): () => { ok: boolean } {
  return () => ({ ok: true });
}
export function next() { return 1; }
""",
            [("make", 1, 1, 3), ("next", 4, 4, 4)],
        ),
        "generic-class.ts": (
            """export class Store<T extends { id: string }> {
  get(value: T) { return value; }
}
export function next() { return 1; }
""",
            [("Store", 1, 1, 3), ("get", 2, 2, 2), ("next", 4, 4, 4)],
        ),
        "expression-arrow.ts": (
            """export const increment = (value: number) =>
  value + 1;
export const payload = () => ({ ok: true });
""",
            [("increment", 1, 1, 2), ("payload", 3, 3, 3)],
        ),
        "comparison-default.ts": (
            """export function choose(value = left < right ? left : right) {
  return value;
}
export const chooseArrow = (value = left < right ? left : right) => ({ value });
""",
            [("choose", 1, 1, 3), ("chooseArrow", 4, 4, 4)],
        ),
        "generic-functions.ts": (
            """export function select<T extends Outer<Middle<Inner<{ id: string }>>>>(value: T): T {
  return value;
}
export const project = <T extends { value: U }, U = string>(value: T) => ({ value: value.value });
export function next() { return 1; }
""",
            [("select", 1, 1, 3), ("project", 4, 4, 4), ("next", 5, 5, 5)],
        ),
        "bodyless-generic.ts": (
            """declare function missing<T extends { id: string }>(value: T): void;
export function next() { return 1; }
""",
            [("missing", 1, 1, 0), ("next", 2, 2, 2)],
        ),
    }
    for file_path, (source, expected) in typescript_cases.items():
        symbols, _ = extract_diff_symbols(_diff(file_path, source), file_path)
        assert [(item.name, item.line, item.start_line, item.end_line) for item in symbols] == expected

    go_source = """package factory
func makeValue() struct {
  X int
  Nested struct { Y string }
} {
  return struct { X int; Nested struct { Y string } }{}
}
func next() {}
"""
    go_symbols, _ = extract_diff_symbols(_diff("factory.go", go_source), "factory.go")
    assert [(item.name, item.line, item.start_line, item.end_line) for item in go_symbols] == [
        ("makeValue", 2, 2, 7),
        ("next", 8, 8, 8),
    ]

    generic_go_source = """package factory
func Build[T any, U interface { Value() T }](value T) interface { Run() U } {
  return nil
}
func bodyless[T any](value T)
func next() {}
"""
    generic_go_symbols, _ = extract_diff_symbols(_diff("generic.go", generic_go_source), "generic.go")
    assert [(item.name, item.line, item.start_line, item.end_line) for item in generic_go_symbols] == [
        ("Build", 2, 2, 4),
        ("bodyless", 5, 5, 0),
        ("next", 6, 6, 6),
    ]

    java_source = """abstract class Runner {
  abstract void missing();
  void next() { Runnable task = new Runnable() { public void run() {} }; }
}
"""
    java_symbols, _ = extract_diff_symbols(_diff("Runner.java", java_source), "Runner.java")
    assert [(item.name, item.line, item.start_line, item.end_line) for item in java_symbols] == [
        ("Runner", 1, 1, 4),
        ("missing", 2, 2, 0),
        ("next", 3, 3, 3),
    ]

    generic_java_source = """abstract class Factory {
  public static <T extends Comparable<? super T>>
  java.util.Map<String, java.util.List<java.util.Map<String, java.util.Set<T>>>>
  make(
      T value
  ) {
    return java.util.Map.of();
  }
  abstract <T extends Comparable<? super T>> java.util.List<T> missing(T value);
  void next() {}
}
"""
    generic_java_symbols, _ = extract_diff_symbols(
        _diff("Factory.java", generic_java_source),
        "Factory.java",
    )
    assert [(item.name, item.line, item.start_line, item.end_line) for item in generic_java_symbols] == [
        ("Factory", 1, 1, 11),
        ("make", 4, 2, 8),
        ("missing", 9, 9, 0),
        ("next", 10, 10, 10),
    ]

    rust_source = """extern "C" {
  fn missing();
}
fn next() {
  let config = Config { value: 1 };
  if ready() { work(); }
}
"""
    rust_symbols, _ = extract_diff_symbols(_diff("runner.rs", rust_source), "runner.rs")
    assert [(item.name, item.line, item.start_line, item.end_line) for item in rust_symbols] == [
        ("missing", 2, 2, 0),
        ("next", 4, 4, 7),
    ]

    generic_rust_source = """pub fn select<T: Into<Option<Result<Item, Error>>>, const N: usize>(value: T) -> Item {
  value.into()
}
fn bodyless<T: Copy>(value: T);
fn next() {}
"""
    generic_rust_symbols, _ = extract_diff_symbols(_diff("generic.rs", generic_rust_source), "generic.rs")
    assert [(item.name, item.line, item.start_line, item.end_line) for item in generic_rust_symbols] == [
        ("select", 1, 1, 3),
        ("bodyless", 4, 4, 0),
        ("next", 5, 5, 5),
    ]


async def test_online_detector_finding_in_destructured_typescript_function_seeds_cross_pr_risk(tmp_path):
    db = Database(tmp_path / "destructured-typescript.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=_ConfirmEveryChainLLM())
    repo = "Wayne0607/ReviewForge"
    seed_file = "gauntlet_fullstack/seed_frontend.tsx"
    seed_source = """import React from "react";

export function RawProfileCard({ html }: { html: string }) {
  return <article dangerouslySetInnerHTML={{ __html: html }} />;
}
"""
    seed_state = StateStore(
        pr_number=83,
        repo=repo,
        head_sha="seed-head",
        base_sha="main-head",
        files_changed=[seed_file],
        diff_summary=_diff(seed_file, seed_source),
    )
    await db.create_run("online-seed", repo, 83, "seed-head", "main-head")
    await analyzer.analyze(
        "online-seed",
        seed_state,
        [
            Finding(
                file=seed_file,
                line=4,
                severity="error",
                category="xss",
                message="dangerouslySetInnerHTML renders potentially unsafe DOM content.",
                confidence=0.9,
                reviewer="security_reviewer",
                status="confirmed",
                verified_by="judge",
            )
        ],
    )
    await db.complete_run("online-seed", {})

    symbol = await db.get_symbol(seed_file, "RawProfileCard")
    assert json.loads(symbol["risk_categories"]) == ["xss"]

    consumer_file = "gauntlet_consumers/bridge.ts"
    consumer_source = """import { RawProfileCard } from "gauntlet_fullstack/seed_frontend";

export function bridge(html: string) {
  return RawProfileCard({ html });
}
"""
    consumer_state = StateStore(
        pr_number=84,
        repo=repo,
        head_sha="consumer-head",
        base_sha="seed-head",
        files_changed=[consumer_file],
        diff_summary=_diff(consumer_file, consumer_source),
    )
    cross_findings = await analyzer.analyze("online-consumer", consumer_state, [])

    assert {(finding.file, finding.line, finding.category) for finding in cross_findings} == {
        (consumer_file, 4, "cross-pr-xss")
    }
    await db.close()


async def test_adjacent_symbol_boundary_does_not_leak_next_sink_into_previous_symbol(tmp_path):
    db = Database(tmp_path / "adjacent-symbols.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=_ConfirmEveryChainLLM())

    seed_source = """package gauntlet_fullstack

import (
    \"net/http\"
    \"os/exec\"
)

func FetchInternal(url string) (*http.Response, error) {
    return http.Get(url)
}

func RunMaintenance(binary string) error {
    return exec.Command(binary, \"--repair\").Run()
}
"""
    seed_lines = seed_source.splitlines()
    fetch_line = next(index for index, line in enumerate(seed_lines, 1) if line.startswith("func FetchInternal"))
    maintenance_line = next(index for index, line in enumerate(seed_lines, 1) if line.startswith("func RunMaintenance"))
    seed_state = StateStore(
        pr_number=78,
        repo="o/r",
        head_sha="seed",
        files_changed=["gauntlet_fullstack/seed_go.go"],
        diff_summary=_diff("gauntlet_fullstack/seed_go.go", seed_source),
    )
    seed_findings = [
        Finding(
            file="gauntlet_fullstack/seed_go.go",
            line=fetch_line + 1,
            severity="error",
            category="ssrf",
            message="A dynamic URL is passed to http.Get.",
            status="confirmed",
        ),
        # Mirrors PR #78: the LLM anchored the RunMaintenance issue on the
        # separator line immediately before its declaration.
        Finding(
            file="gauntlet_fullstack/seed_go.go",
            line=maintenance_line - 1,
            severity="error",
            category="command-injection",
            message="binary is passed directly to exec.Command.",
            status="confirmed",
        ),
        Finding(
            file="gauntlet_fullstack/seed_go.go",
            line=maintenance_line + 1,
            severity="error",
            category="command-injection",
            message="Go exec.Command is used.",
            status="confirmed",
        ),
    ]
    await analyzer.analyze("seed-boundary", seed_state, seed_findings)

    fetch = await db.get_symbol("gauntlet_fullstack/seed_go.go", "FetchInternal")
    maintenance = await db.get_symbol("gauntlet_fullstack/seed_go.go", "RunMaintenance")
    assert json.loads(fetch["risk_categories"]) == ["ssrf"]
    assert json.loads(maintenance["risk_categories"]) == ["command-injection"]

    consumer_source = """package gauntlet_services

import seed \"gauntlet_fullstack/seed_go\"

func CrossPRCommand(tool string) error {
    return seed.RunMaintenance(tool)
}

func CrossPRSSRF(url string) (*http.Response, error) {
    return seed.FetchInternal(url)
}
"""
    consumer_state = StateStore(
        pr_number=79,
        repo="o/r",
        head_sha="consumer",
        files_changed=["gauntlet_services/go_consumer.go"],
        diff_summary=_diff("gauntlet_services/go_consumer.go", consumer_source),
    )
    cross_findings = await analyzer.analyze("consumer-boundary", consumer_state, [])
    assert {(finding.line, finding.category) for finding in cross_findings} == {
        (6, "cross-pr-command-injection"),
        (10, "cross-pr-ssrf"),
    }
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


async def test_cross_pr_never_propagates_risk_from_the_current_run(tmp_path):
    db = Database(tmp_path / "same_run.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=None)
    await db.create_run("same-run", "o/r", 71, "self-head", "main-head")

    sink_path = "pkg/sink.py"
    imported_path = "pkg/imported_consumer.py"
    bare_path = "pkg/bare_consumer.py"
    state = StateStore(
        pr_number=71,
        repo="o/r",
        head_sha="self-head",
        base_sha="main-head",
        files_changed=[sink_path, imported_path, bare_path],
        diff_summary="\n".join(
            [
                _diff(sink_path, "def danger(raw):\n    return eval(raw)\n"),
                _diff(
                    imported_path,
                    "from pkg.sink import danger\ndef imported(raw):\n    return danger(raw)\n",
                ),
                _diff(bare_path, "def bare(raw):\n    return danger(raw)\n"),
            ]
        ),
    )
    cross = await analyzer.analyze(
        "same-run",
        state,
        [
            Finding(
                file=sink_path,
                line=2,
                severity="error",
                category="code-injection",
                message="danger evaluates current-PR input",
                confidence=0.95,
                reviewer="security_reviewer",
                status="confirmed",
            )
        ],
    )

    assert cross == []
    await db.close()


async def test_cross_pr_rejects_risk_from_a_different_repository(tmp_path):
    db = Database(tmp_path / "different_repo.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=None)
    await _seed_completed_risk(
        db,
        analyzer,
        run_id="repo-a-risk",
        repo="owner/repo-a",
        pr_number=60,
        head_sha="shared-sha",
        file_path="src/shared/sink.py",
    )

    imported_path = "src/imported_consumer.py"
    bare_path = "src/bare_consumer.py"
    state = StateStore(
        pr_number=72,
        repo="owner/repo-b",
        head_sha="consumer-head",
        base_sha="shared-sha",
        files_changed=[imported_path, bare_path],
        diff_summary="\n".join(
            [
                _diff(
                    imported_path,
                    "from shared.sink import danger\ndef imported(raw):\n    return danger(raw)\n",
                ),
                _diff(bare_path, "def bare(raw):\n    return danger(raw)\n"),
            ]
        ),
    )

    assert await analyzer.analyze("repo-b-run", state, []) == []
    await db.close()


async def test_cross_pr_rejects_unmerged_branch_version_and_caches_comparison(tmp_path):
    db = Database(tmp_path / "branch_contamination.db")
    await db.connect()
    repo = "o/r"
    source_head = "pr70-head"
    base_sha = "main-head"
    sink_path = "src/shared/sink.py"
    github = _FakeGitHub(
        {
            (repo, source_head, sink_path): "def danger(raw): return eval(raw)\n",
            (repo, base_sha, sink_path): "def danger(raw): return validate(raw)\n",
        }
    )
    analyzer = CrossPRAnalyzer(db, llm=None, github_client=github)
    await _seed_completed_risk(
        db,
        analyzer,
        run_id="pr70-risk",
        repo=repo,
        pr_number=70,
        head_sha=source_head,
        file_path=sink_path,
    )

    consumer_path = "src/consumer.py"
    state = StateStore(
        pr_number=71,
        repo=repo,
        head_sha="pr71-head",
        base_sha=base_sha,
        files_changed=[consumer_path],
        diff_summary=_diff(
            consumer_path,
            "from shared.sink import danger\ndef run(raw):\n    return danger(raw)\n",
        ),
    )

    assert await analyzer.analyze("pr71-run", state, []) == []
    assert github.calls.count((repo, source_head, sink_path)) == 1
    assert github.calls.count((repo, base_sha, sink_path)) == 1
    await db.close()


async def test_cross_pr_keeps_stacked_base_and_skips_inapplicable_first_fuzzy_candidate(tmp_path):
    db = Database(tmp_path / "stacked.db")
    await db.connect()
    repo = "o/r"
    stacked_sha = "pr73-head"
    stale_path = "branches/shared/sink_pr70.py"
    stacked_path = "src/shared/sink.py"
    github = _FakeGitHub(
        {
            (repo, "pr70-head", stale_path): "def danger(raw): return eval(raw)\n",
            (repo, stacked_sha, stale_path): "def danger(raw): return raw\n",
        }
    )
    analyzer = CrossPRAnalyzer(db, llm=_ConfirmEveryChainLLM(), github_client=github)
    # Insert the stale fuzzy match first: selection must continue to the
    # applicable stacked-base candidate instead of trusting DB row order.
    await _seed_completed_risk(
        db,
        analyzer,
        run_id="stale-pr70",
        repo=repo,
        pr_number=70,
        head_sha="pr70-head",
        file_path=stale_path,
    )
    await _seed_completed_risk(
        db,
        analyzer,
        run_id="stacked-pr73",
        repo=repo,
        pr_number=73,
        head_sha=stacked_sha,
        file_path=stacked_path,
    )

    consumer_path = "src/stacked_consumer.py"
    state = StateStore(
        pr_number=74,
        repo=repo,
        head_sha="pr74-head",
        base_sha=stacked_sha,
        files_changed=[consumer_path],
        diff_summary=_diff(
            consumer_path,
            "from shared.sink import danger\ndef run(raw):\n    return danger(raw)\n",
        ),
    )
    cross = await analyzer.analyze("pr74-run", state, [])

    assert {finding.category for finding in cross} == {"cross-pr-sql-injection"}
    assert all(stacked_path in finding.message for finding in cross)
    assert all(stale_path not in finding.message for finding in cross)
    await db.close()


async def test_cross_pr_keeps_risk_when_source_and_base_file_contents_match(tmp_path):
    db = Database(tmp_path / "same_content.db")
    await db.connect()
    repo = "o/r"
    source_head = "historical-head"
    base_sha = "current-base"
    sink_path = "pkg/content_sink.py"
    unchanged = "def danger(raw): return eval(raw)\n"
    github = _FakeGitHub(
        {
            (repo, source_head, sink_path): unchanged,
            (repo, base_sha, sink_path): unchanged,
        }
    )
    analyzer = CrossPRAnalyzer(db, llm=_ConfirmEveryChainLLM(), github_client=github)
    await _seed_completed_risk(
        db,
        analyzer,
        run_id="historical-risk",
        repo=repo,
        pr_number=64,
        head_sha=source_head,
        file_path=sink_path,
    )

    consumer_path = "pkg/content_consumer.py"
    state = StateStore(
        pr_number=75,
        repo=repo,
        head_sha="consumer-head",
        base_sha=base_sha,
        files_changed=[consumer_path],
        diff_summary=_diff(
            consumer_path,
            "from pkg.content_sink import danger\ndef run(raw):\n    return danger(raw)\n",
        ),
    )
    cross = await analyzer.analyze("same-content-consumer", state, [])

    assert {finding.category for finding in cross} == {"cross-pr-sql-injection"}
    assert github.calls.count((repo, source_head, sink_path)) == 1
    assert github.calls.count((repo, base_sha, sink_path)) == 1
    await db.close()


async def test_cross_pr_caches_missing_file_and_skips_unverifiable_risk(tmp_path):
    db = Database(tmp_path / "missing_file.db")
    await db.connect()
    repo = "o/r"
    source_head = "historical-head"
    base_sha = "current-base"
    sink_path = "pkg/missing_sink.py"
    github = _FakeGitHub(
        {
            (repo, source_head, sink_path): "def danger(raw): return eval(raw)\n",
            (repo, base_sha, sink_path): FileNotFoundError(sink_path),
        }
    )
    analyzer = CrossPRAnalyzer(db, llm=None, github_client=github)
    await _seed_completed_risk(
        db,
        analyzer,
        run_id="missing-base-risk",
        repo=repo,
        pr_number=63,
        head_sha=source_head,
        file_path=sink_path,
    )

    consumer_path = "pkg/missing_consumer.py"
    state = StateStore(
        pr_number=76,
        repo=repo,
        head_sha="consumer-head",
        base_sha=base_sha,
        files_changed=[consumer_path],
        diff_summary=_diff(
            consumer_path,
            "from pkg.missing_sink import danger\ndef run(raw):\n    return danger(raw)\n",
        ),
    )

    assert await analyzer.analyze("missing-base-consumer", state, []) == []
    assert github.calls.count((repo, source_head, sink_path)) == 1
    assert github.calls.count((repo, base_sha, sink_path)) == 1
    await db.close()


async def test_cross_pr_rejects_running_source_run(tmp_path):
    db = Database(tmp_path / "running_source.db")
    await db.connect()
    analyzer = CrossPRAnalyzer(db, llm=None)
    sink_path = "pkg/running_sink.py"
    await db.create_run("running-source", "o/r", 68, "running-head", "main-head")
    source_state = StateStore(
        pr_number=68,
        repo="o/r",
        head_sha="running-head",
        base_sha="main-head",
        files_changed=[sink_path],
        diff_summary=_diff(sink_path, "def danger(raw):\n    return eval(raw)\n"),
    )
    await analyzer.analyze(
        "running-source",
        source_state,
        [
            Finding(
                file=sink_path,
                line=2,
                severity="error",
                category="code-injection",
                message="danger is unsafe",
                confidence=0.95,
                reviewer="security_reviewer",
                status="confirmed",
            )
        ],
    )

    consumer_path = "pkg/consumer.py"
    consumer = StateStore(
        pr_number=69,
        repo="o/r",
        head_sha="consumer-head",
        base_sha="running-head",
        files_changed=[consumer_path],
        diff_summary=_diff(
            consumer_path,
            "from pkg.running_sink import danger\ndef run(raw):\n    return danger(raw)\n",
        ),
    )
    assert await analyzer.analyze("consumer-run", consumer, []) == []
    await db.close()


async def test_cross_pr_depth_two_rejects_relation_from_unmerged_branch(tmp_path):
    db = Database(tmp_path / "depth_two.db")
    await db.connect()
    repo = "o/r"
    base_sha = "base-head"
    gateway_path = "pkg/gateway.py"
    sub_sink_path = "pkg/sub_sink.py"
    github = _FakeGitHub(
        {
            (repo, "relation-branch", gateway_path): "from pkg.sub_sink import subdanger\n",
            (repo, base_sha, gateway_path): "def gateway(raw): return raw\n",
        }
    )
    analyzer = CrossPRAnalyzer(db, llm=_ConfirmEveryChainLLM(), github_client=github)
    await _seed_completed_risk(
        db,
        analyzer,
        run_id="gateway-base-risk",
        repo=repo,
        pr_number=65,
        head_sha=base_sha,
        file_path=gateway_path,
        symbol_name="gateway",
        category="xss",
    )
    await _seed_completed_risk(
        db,
        analyzer,
        run_id="sub-base-risk",
        repo=repo,
        pr_number=66,
        head_sha=base_sha,
        file_path=sub_sink_path,
        symbol_name="subdanger",
        category="sql-injection",
    )
    await db.create_run("branch-relation", repo, 70, "relation-branch", "old-base")
    await db.upsert_relation(
        "branch-relation",
        gateway_path,
        sub_sink_path,
        "subdanger",
        "call",
        source_symbol="gateway",
    )
    await db.complete_run("branch-relation", {})

    consumer_path = "pkg/depth_consumer.py"
    state = StateStore(
        pr_number=74,
        repo=repo,
        head_sha="consumer-head",
        base_sha=base_sha,
        files_changed=[consumer_path],
        diff_summary=_diff(
            consumer_path,
            "from pkg.gateway import gateway\ndef run(raw):\n    return gateway(raw)\n",
        ),
    )
    cross = await analyzer.analyze("depth-consumer", state, [])

    assert {finding.category for finding in cross} == {"cross-pr-xss"}
    await db.close()


async def test_cross_pr_content_cache_is_bounded_and_coalesces_same_key_reads(tmp_path):
    db = Database(tmp_path / "bounded_cache.db")
    repo = "o/r"
    shared_key = (repo, "shared-ref", "pkg/shared.py")
    contents = {shared_key: "shared"}
    contents.update({(repo, f"ref-{index}", f"pkg/file-{index}.py"): f"content-{index}" for index in range(260)})
    github = _FakeGitHub(contents)
    analyzer = CrossPRAnalyzer(db, llm=None, github_client=github)

    values = await asyncio.gather(*(analyzer._get_cached_file_content(*shared_key) for _ in range(12)))
    assert values == ["shared"] * 12
    assert github.calls.count(shared_key) == 1

    for index in range(260):
        await analyzer._get_cached_file_content(repo, f"ref-{index}", f"pkg/file-{index}.py")

    assert len(analyzer._content_cache) == 256
    assert (repo, "ref-0", "pkg/file-0.py") not in analyzer._content_cache
    assert (repo, "ref-259", "pkg/file-259.py") in analyzer._content_cache


def test_alias_and_member_call_extraction_across_stacked_languages():
    cases = [
        ("x.py", "from pkg.sink import danger as d\ndef run():\n    return d()\n", "danger", "d", "d", ""),
        (
            "x.ts",
            'import { danger as d } from "pkg/sink";\nexport function run() { return d(); }\n',
            "danger",
            "d",
            "d",
            "",
        ),
        (
            "x.vue",
            '<script lang="ts">\nimport { danger as d } from "pkg/sink";\nfunction run() { return d(); }\n</script>\n',
            "danger",
            "d",
            "d",
            "",
        ),
        (
            "x.svelte",
            '<script lang="ts">\nimport { danger as d } from "pkg/sink";\nfunction run() { return d(); }\n</script>\n',
            "danger",
            "d",
            "d",
            "",
        ),
        (
            "X.java",
            "import pkg.Seed;\nclass X {\n  private final Seed seed = new Seed();\n"
            "  Object run(Object raw) { return seed.danger(raw); }\n}\n",
            "Seed",
            "Seed",
            "danger",
            "seed",
        ),
        (
            "x.go",
            'package x\nimport seed "pkg/sink"\nfunc run(raw string) string { return seed.Danger(raw) }\n',
            "",
            "seed",
            "Danger",
            "seed",
        ),
    ]

    for file_path, content, exported, local, callee, receiver in cases:
        imports = extract_imports(content, file_path)
        calls = extract_calls(content, file_path)
        assert any(item.name == exported and item.local_name == local for item in imports), file_path
        assert any(item.callee == callee and item.receiver == receiver for item in calls), file_path


class _ConfirmEveryChainLLM:
    def __init__(self) -> None:
        self.batch_sizes: list[int] = []

    async def ainvoke(self, messages):
        chain_ids = [int(value) for value in re.findall(r"^Chain (\d+):", messages[-1].content, re.MULTILINE)]
        self.batch_sizes.append(len(chain_ids))
        return SimpleNamespace(
            content=json.dumps(
                [
                    {"chain_id": chain_id, "exploitable": True, "confidence": 0.99, "reason": "confirmed"}
                    for chain_id in chain_ids
                ]
            )
        )


class _FailMiddleBatchLLM(_ConfirmEveryChainLLM):
    def __init__(self) -> None:
        super().__init__()
        self.attempts = 0

    async def ainvoke(self, messages):
        self.attempts += 1
        if self.attempts == 2:
            raise RuntimeError("transient middle-batch failure")
        return await super().ainvoke(messages)


class _RejectEveryChainLLM:
    def __init__(self) -> None:
        self.invocations = 0

    async def ainvoke(self, messages):
        self.invocations += 1
        chain_ids = [int(value) for value in re.findall(r"^Chain (\d+):", messages[-1].content, re.MULTILINE)]
        return SimpleNamespace(
            content=json.dumps(
                [
                    {"chain_id": chain_id, "exploitable": False, "confidence": 0.99, "reason": "rejected"}
                    for chain_id in chain_ids
                ]
            )
        )


class _CaptureRejectLLM(_RejectEveryChainLLM):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    async def ainvoke(self, messages):
        self.prompts.append(messages[-1].content)
        return await super().ainvoke(messages)


class _CaptureConfirmLLM(_ConfirmEveryChainLLM):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    async def ainvoke(self, messages):
        self.prompts.append(messages[-1].content)
        return await super().ainvoke(messages)


class _PoisonChainLLM(_ConfirmEveryChainLLM):
    async def ainvoke(self, messages):
        if "Source: consumer_2.py:" in messages[-1].content:
            raise RuntimeError("poison chain")
        return await super().ainvoke(messages)


def _confirmation_chain(index: int, *, evidence_kind: str = "call") -> CrossPRChain:
    return CrossPRChain(
        source_file=f"consumer_{index}.py",
        source_symbol=f"caller_{index}",
        source_line=100 + index,
        target_file=f"sink_{index}.py",
        target_symbol=f"sink_{index}",
        risk_category="sql-injection",
        risk_level="critical",
        depth=1,
        path=[
            {"file": f"consumer_{index}.py", "symbol": f"caller_{index}"},
            {"file": f"sink_{index}.py", "symbol": f"sink_{index}", "risk": "sql-injection"},
        ],
        evidence_kind=evidence_kind,
        source_column=12,
        call_callee=f"sink_{index}",
    )


async def test_cross_pr_without_llm_never_publishes_even_exact_structural_edges(tmp_path):
    analyzer = CrossPRAnalyzer(Database(tmp_path / "no_semantic_judge.db"), llm=None)

    findings = await analyzer._confirm_suspicious_chains(
        [_confirmation_chain(1, evidence_kind="exact-import-call")],
        "",
        StateStore(repo="o/r", head_sha="head"),
    )

    assert findings == []


async def test_cross_pr_prompt_keeps_exact_call_arguments_in_long_multifile_diff(tmp_path):
    first_lines = ["def caller_1():", *[f"    filler_{index} = {index}" for index in range(180)]]
    first_lines.append('    return sink_1("fixed-safe-value")')
    first_body = "\n".join(first_lines)
    second_body = "def caller_2(request):\n    clean = allowlist(request.value)\n    return sink_2(clean)"
    first_line = len(first_lines)
    second_line = 3

    chains = [_confirmation_chain(1), _confirmation_chain(2)]
    chains[0].source_line = first_line
    chains[1].source_line = second_line
    github = _FakeGitHub(
        {
            ("o/r", "head", "consumer_1.py"): first_body,
            ("o/r", "head", "consumer_2.py"): second_body,
            ("o/r", "base", "sink_1.py"): "def sink_1(raw):\n    return eval(raw)",
            ("o/r", "base", "sink_2.py"): "def sink_2(raw):\n    return eval(raw)",
        }
    )
    llm = _CaptureRejectLLM()
    analyzer = CrossPRAnalyzer(Database(tmp_path / "prompt_context.db"), llm=llm, github_client=github)
    diff_summary = "\n".join([_diff("consumer_1.py", first_body), _diff("consumer_2.py", second_body)])

    findings = await analyzer._llm_confirm_chain_batch(
        chains,
        diff_summary,
        StateStore(repo="o/r", head_sha="head", base_sha="base"),
    )

    assert findings == []
    assert len(llm.prompts) == 1
    prompt = llm.prompts[0]
    assert 'sink_1("fixed-safe-value")' in prompt
    assert "clean = allowlist(request.value)" in prompt
    assert "return sink_2(clean)" in prompt
    assert "def sink_1(raw)" in prompt
    assert "def sink_2(raw)" in prompt
    assert prompt.index("Chain 1:") < prompt.index("Chain 2:")


def test_cross_pr_confirmation_parser_rejects_truthy_types_low_confidence_and_duplicates(tmp_path):
    analyzer = CrossPRAnalyzer(Database(tmp_path / "strict_json.db"), llm=None)
    chains = [_confirmation_chain(index) for index in range(1, 6)]
    payload = json.dumps(
        [
            {"chain_id": 1, "exploitable": "false", "confidence": 0.99, "reason": "string bool"},
            {"chain_id": 2, "exploitable": True, "confidence": "0.99", "reason": "string confidence"},
            {"chain_id": 3, "exploitable": True, "confidence": 0.64, "reason": "below threshold"},
            {"chain_id": 4, "exploitable": True, "confidence": 0.99, "reason": "first accepted"},
            {"chain_id": 4, "exploitable": True, "confidence": 1.0, "reason": "duplicate"},
            {"chain_id": 5, "exploitable": True, "confidence": 0.9, "reason": "accepted"},
        ]
    )

    findings = analyzer._parse_confirmation(payload, chains)

    assert [(finding.file, finding.confidence) for finding in findings] == [
        ("consumer_4.py", 0.99),
        ("consumer_5.py", 0.9),
    ]


def test_cross_pr_confirmation_parser_bounds_long_reason_without_dropping_sibling(tmp_path):
    analyzer = CrossPRAnalyzer(Database(tmp_path / "long_reason.db"), llm=None)
    payload = json.dumps(
        [
            {"chain_id": 1, "exploitable": True, "confidence": 0.99, "reason": "长" * 10_000},
            {"chain_id": 2, "exploitable": True, "confidence": 0.99, "reason": "正常理由"},
        ]
    )

    findings = analyzer._parse_confirmation(payload, [_confirmation_chain(1), _confirmation_chain(2)])

    assert [finding.file for finding in findings] == ["consumer_1.py", "consumer_2.py"]
    assert len(findings[0].verify_reason) <= 500
    assert findings[1].verify_reason == "正常理由"


async def test_same_file_safe_then_unsafe_calls_are_both_semantic_gated(tmp_path):
    db = Database(tmp_path / "distinct_calls.db")
    await db.connect()
    repo = "owner/repo"
    llm = _CaptureRejectLLM()
    analyzer = CrossPRAnalyzer(db, llm=llm)
    await _seed_completed_risk(
        db,
        analyzer,
        run_id="seed",
        repo=repo,
        pr_number=1,
        head_sha="seed-head",
        file_path="pkg/sink.py",
    )
    body = (
        "from pkg.sink import danger\n\n"
        "def safe_path():\n"
        '    return danger("fixed-safe-value")\n\n'
        "def unsafe_path(request):\n"
        '    return danger(request.args["q"])\n'
    )
    await db.create_run("consumer", repo, 2, "consumer-head", "seed-head")
    state = StateStore(
        pr_number=2,
        repo=repo,
        head_sha="consumer-head",
        base_sha="seed-head",
        files_changed=["consumer.py"],
        diff_summary=_diff("consumer.py", body),
    )

    findings = await analyzer.analyze("consumer", state, [])
    await db.close()

    assert findings == []
    prompt = llm.prompts[-1]
    assert len(re.findall(r"^Chain \d+:", prompt, re.MULTILINE)) == 2
    assert 'danger("fixed-safe-value")' in prompt
    assert 'danger(request.args["q"])' in prompt
    assert "Source: consumer.py:L4" in prompt
    assert "Source: consumer.py:L7" in prompt


async def test_depth_two_filters_relation_by_source_symbol_and_keeps_intermediate_context(tmp_path):
    db = Database(tmp_path / "depth_two_symbol.db")
    await db.connect()
    repo = "owner/repo"
    await db.create_run("seed", repo, 1, "seed-head", "parent")
    for file_path, symbol in (
        ("pkg/gateway.py", "gateway"),
        ("pkg/intended.py", "danger"),
        ("pkg/unrelated.py", "other"),
    ):
        await db.upsert_symbol(
            file_path=file_path,
            symbol_name=symbol,
            symbol_type="function",
            run_id="seed",
            pr_number=1,
            language="python",
            risk_level="critical",
            risk_categories=["sql-injection"],
        )
        await db.upsert_file_risk(file_path, "critical", ["sql-injection"], 1, "seed")
    await db.upsert_relation(
        run_id="seed",
        source_file="pkg/gateway.py",
        source_symbol="gateway",
        target_file="pkg/intended.py",
        target_symbol="danger",
        relation_type="call",
    )
    await db.upsert_relation(
        run_id="seed",
        source_file="pkg/gateway.py",
        source_symbol="unrelated",
        target_file="pkg/unrelated.py",
        target_symbol="other",
        relation_type="call",
    )
    await db.complete_run("seed", {})

    body = "from pkg.gateway import gateway\n\ndef route(raw):\n    return gateway(raw)\n"
    patch = _diff("consumer.py", body)
    _symbols, imports = extract_diff_symbols(patch, "consumer.py")
    calls = extract_diff_calls(patch, "consumer.py")
    state = StateStore(
        pr_number=2,
        repo=repo,
        head_sha="consumer-head",
        base_sha="seed-head",
        files_changed=["consumer.py"],
        diff_summary=patch,
    )
    analyzer = CrossPRAnalyzer(db, llm=_RejectEveryChainLLM())
    chains = await analyzer._find_suspicious_chains(imports, calls, ["consumer.py"], "consumer", state)
    depth_two = [chain for chain in chains if chain.depth == 2]

    assert len(depth_two) == 1
    assert depth_two[0].path[1] == {"file": "pkg/gateway.py", "symbol": "gateway"}
    assert depth_two[0].target_file == "pkg/intended.py"
    assert all(chain.target_file != "pkg/unrelated.py" for chain in depth_two)

    github = _FakeGitHub(
        {
            (repo, "seed-head", "pkg/gateway.py"): (
                "def gateway(raw):\n    checked = allowlist(raw)\n    return danger(checked)\n\n"
                "def unrelated(raw):\n    return other(raw)\n"
            ),
            (repo, "seed-head", "pkg/intended.py"): "def danger(raw):\n    return eval(raw)\n",
        }
    )
    context_analyzer = CrossPRAnalyzer(db, llm=_RejectEveryChainLLM(), github_client=github)
    context = await context_analyzer._chain_confirmation_context(1, depth_two[0], patch, state)
    await db.close()

    assert "Intermediate function pkg/gateway.py:gateway" in context
    assert "checked = allowlist(raw)" in context
    assert "def unrelated" not in context


def test_symbol_context_and_callers_cover_rust_ruby_and_multiline_ts_java():
    analyzer = CrossPRAnalyzer.__new__(CrossPRAnalyzer)
    cases = {
        "review.rs": (
            "pub async fn review(\n    request: Request,\n) -> Result<String, Error>\nwhere Request: Send\n"
            "{\n    danger(request.body())\n}\nfn next_marker() {}\n",
            "danger(request.body())",
        ),
        "review.ts": (
            "class Service {\n  async review(\n    request: Request,\n    options: Options,\n"
            "  ): Promise<Result> {\n    return danger(request.body);\n  }\n}\nfunction next_marker() {}\n",
            "danger(request.body)",
        ),
        "Review.java": (
            "class Review {\n  public Result review(\n"
            "    HttpServletRequest request,\n    Map<String, String> options\n"
            "  ) throws IOException {\n    return danger(request.getBody());\n  }\n}\nclass NextMarker {}\n",
            "danger(request.getBody())",
        ),
        "review.rb": (
            "def review(request)\n  danger(request.body)\nend\ndef next_marker\nend\n",
            "danger(request.body)",
        ),
    }

    for file_path, (source, evidence) in cases.items():
        context = analyzer._extract_function(source, "review", file_path)
        calls = extract_calls(source, file_path)
        assert evidence in context, file_path
        assert "next_marker" not in context, file_path
        assert any(call.callee == "danger" and call.caller == "review" for call in calls), file_path


async def test_multiline_call_keeps_last_argument_and_minified_line_has_hard_budget(tmp_path):
    before = ["def caller(request):", *[f"    filler_{index} = {index}" for index in range(80)]]
    call = ["    return sink_1(", *[f"        arg_{index}," for index in range(30)]]
    call.extend(["        request.ATTACKER_CONTROLLED,", "    )"])
    body = "\n".join([*before, *call])
    chain = _confirmation_chain(1)
    chain.source_file = "consumer_1.py"
    chain.source_symbol = "caller"
    chain.source_line = len(before) + 1
    chain.source_column = 12
    github = _FakeGitHub(
        {
            ("o/r", "head", "consumer_1.py"): body,
            ("o/r", "base", "sink_1.py"): "def sink_1(raw):\n    return eval(raw)",
        }
    )
    llm = _CaptureRejectLLM()
    analyzer = CrossPRAnalyzer(Database(tmp_path / "multiline_call.db"), llm=llm, github_client=github)
    await analyzer._llm_confirm_chain_batch(
        [chain],
        _diff("consumer_1.py", body),
        StateStore(repo="o/r", head_sha="head", base_sha="base"),
    )

    assert "arg_29" in llm.prompts[0]
    assert "request.ATTACKER_CONTROLLED" in llm.prompts[0]
    assert "Exact complete call expression" in llm.prompts[0]

    huge = "A" * 200_000
    huge_body = f'def caller():\n    return sink_1("{huge}")'
    chain.source_line = 2
    huge_llm = _CaptureRejectLLM()
    huge_analyzer = CrossPRAnalyzer(Database(tmp_path / "huge_call.db"), llm=huge_llm, github_client=github)
    await huge_analyzer._llm_confirm_chain_batch(
        [chain],
        _diff("consumer_1.py", huge_body),
        StateStore(repo="o/r", head_sha="head", base_sha="base"),
    )
    assert len(huge_llm.prompts[0]) <= _MAX_LLM_USER_PROMPT_CHARS
    assert "TRUNCATED" in huge_llm.prompts[0]


async def test_poison_chain_does_not_drop_healthy_batch_siblings(tmp_path):
    analyzer = CrossPRAnalyzer(Database(tmp_path / "poison_chain.db"), llm=_PoisonChainLLM())
    chains = [_confirmation_chain(index) for index in range(5)]

    findings = await analyzer._llm_confirm_chains(chains, "", StateStore(repo="o/r", head_sha="head"))

    assert {finding.file for finding in findings} == {
        "consumer_0.py",
        "consumer_1.py",
        "consumer_3.py",
        "consumer_4.py",
    }


async def test_test_path_variants_are_marked_nonproduction(tmp_path):
    analyzer = CrossPRAnalyzer(Database(tmp_path / "test_paths.db"), llm=_RejectEveryChainLLM())
    for file_path in ("test_foo.py", "foo.spec.ts", "__tests__/x.ts", "spec/x.rb"):
        chain = _confirmation_chain(1)
        chain.source_file = file_path
        chain.path[0]["file"] = file_path
        context = await analyzer._chain_confirmation_context(1, chain, "", StateStore(repo="o/r"))
        assert "Path signal:" in context, file_path


async def test_cross_pr_llm_batches_every_chain_and_maps_local_chain_ids(tmp_path):
    db = Database(tmp_path / "llm_batches.db")
    llm = _ConfirmEveryChainLLM()
    analyzer = CrossPRAnalyzer(db, llm=llm)
    chains = [
        CrossPRChain(
            source_file=f"consumer_{index}.py",
            source_symbol=f"caller_{index}",
            source_line=100 + index,
            target_file=f"sink_{index}.py",
            target_symbol=f"sink_{index}",
            risk_category="sql-injection",
            risk_level="critical",
            depth=1,
            path=[
                {"file": f"consumer_{index}.py", "symbol": f"caller_{index}"},
                {"file": f"sink_{index}.py", "symbol": f"sink_{index}", "risk": "sql-injection"},
            ],
            evidence_kind="call",
        )
        for index in range(12)
    ]

    findings = await analyzer._llm_confirm_chains(chains, "", StateStore(repo="o/r", head_sha="head"))

    assert llm.batch_sizes == [5, 5, 2]
    assert [finding.line for finding in findings] == list(range(100, 112))
    assert [finding.file for finding in findings] == [f"consumer_{index}.py" for index in range(12)]


async def test_cross_pr_llm_batch_failure_isolates_and_recovers_healthy_chains(tmp_path):
    db = Database(tmp_path / "llm_batch_failure.db")
    llm = _FailMiddleBatchLLM()
    analyzer = CrossPRAnalyzer(db, llm=llm)
    chains = [
        CrossPRChain(
            source_file=f"consumer_{index}.py",
            source_symbol=f"caller_{index}",
            source_line=100 + index,
            target_file=f"sink_{index}.py",
            target_symbol=f"sink_{index}",
            risk_category="sql-injection",
            risk_level="critical",
            depth=1,
            path=[
                {"file": f"consumer_{index}.py", "symbol": f"caller_{index}"},
                {"file": f"sink_{index}.py", "symbol": f"sink_{index}", "risk": "sql-injection"},
            ],
            evidence_kind="call",
        )
        for index in range(12)
    ]

    findings = await analyzer._llm_confirm_chains(chains, "", StateStore(repo="o/r", head_sha="head"))

    assert llm.attempts > 3
    assert [finding.line for finding in findings] == list(range(100, 112))
    assert all(finding.verified_by == "cross-pr-analysis" for finding in findings)


# Exact cross-PR-relevant files from live stacked PRs #73 (c8a3d8a) and
# #74 (9661b76). Keeping these fixtures local makes the regression deterministic
# in shallow CI clones while preserving the real module paths and RIGHT lines.
_REAL_PR73_SEED = {
    "gauntlet_fullstack/SeedJava.java": """package gauntlet_fullstack;

import java.io.ByteArrayInputStream;
import java.io.ObjectInputStream;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;

public class SeedJava {
    public String normalizeTenant(String tenant) {
        return tenant == null ? "public" : tenant.replaceAll("[^a-z0-9_-]", "");
    }

    public ResultSet runTenantQuery(Connection conn, String tenant) throws Exception {
        Statement stmt = conn.createStatement();
        return stmt.executeQuery("SELECT * FROM tenants WHERE name = '" + tenant + "'");
    }

    public Object restoreJob(byte[] body) throws Exception {
        ObjectInputStream input = new ObjectInputStream(new ByteArrayInputStream(body));
        return input.readObject();
    }

    public Process launchTool(String toolName) throws Exception {
        return Runtime.getRuntime().exec(toolName + " --verbose");
    }
}
""",
}

_REAL_PR74_CONSUMER = {
    "gauntlet_consumers/admin_view.vue": """<template>
  <main>
    <img :src="profile.avatar">
    <div v-html="profile.bio"></div>
    <button @click="go(profile.next)">Continue</button>
  </main>
</template>

<script lang="ts">
import { AccountLabel, RawProfileCard } from "gauntlet_fullstack/seed_frontend";

export default {
  props: {
    profile: { type: Object, required: true },
  },
  methods: {
    go(next: string) {
      window.location.href = next;
    },
    renderBridge(html: string) {
      return RawProfileCard({ html });
    },
    labelText(text: string) {
      return AccountLabel({ text });
    },
  },
};
</script>
""",
    "gauntlet_consumers/angular_bridge.ts": """import { AdminComponent } from "gauntlet_fullstack/admin.component";

export class AngularBridge {
  constructor(private admin: AdminComponent) {}

  showMessage(rawHtml: string) {
    return this.admin.trustOperatorHtml(rawHtml);
  }

  renderTemplate(rawHtml: string) {
    const holder = document.createElement("div");
    holder.innerHTML = rawHtml;
    return holder;
  }
}
""",
    "gauntlet_consumers/live_summary.svelte": """<script lang="ts">
  import { RawProfileCard, storeSessionToken } from "gauntlet_fullstack/seed_frontend";

  export let summaryHtml: string;
  export let token: string;

  function remember() {
    storeSessionToken(token);
  }

  function bridge() {
    return RawProfileCard({ html: summaryHtml });
  }
</script>

<button on:click={remember}>remember</button>
<img src="/summary.png">
{@html summaryHtml}
""",
    "gauntlet_consumers/python_entry.py": """from gauntlet_fullstack.seed_sinks import (
    fetch_metadata,
    load_session_blob,
    normalize_account_id,
    read_user_file,
    run_report_query,
)


def report_endpoint(request, conn):
    account_id = request.args["account_id"]
    return run_report_query(conn, account_id)


def session_endpoint(request):
    return load_session_blob(request.body)


def file_endpoint(request):
    return read_user_file("/srv/reports", request.args["name"])


def metadata_endpoint(request):
    return fetch_metadata(request.args["url"])


def direct_search(request, conn):
    term = request.args["q"]
    return conn.execute(f"SELECT * FROM users WHERE email LIKE '%{term}%'")


def normalized_id_endpoint(request):
    return normalize_account_id(request.args.get("account_id", ""))
""",
    "gauntlet_consumers/report_panel.tsx": """import React from "react";
import {
  RawProfileCard,
  AccountLabel,
  runClientHook,
  spawnReport,
  storeSessionToken,
} from "gauntlet_fullstack/seed_frontend";

export function ReportPanel({
  html,
  token,
  script,
  command,
}: {
  html: string;
  token: string;
  script: string;
  command: string;
}) {
  storeSessionToken(token);
  runClientHook(script);
  spawnReport(command);

  return (
    <section>
      <img src="/report.png" />
      <RawProfileCard html={html} />
      <AccountLabel text={html} />
      <button onClick={() => (window.location.href = html)}>Open</button>
    </section>
  );
}
""",
    "gauntlet_services/CrossPrConsumer.java": """package gauntlet_services;

import gauntlet_fullstack.SeedJava;
import java.io.ByteArrayInputStream;
import java.io.ObjectInputStream;
import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.Statement;

public class CrossPrConsumer {
    private final SeedJava seed = new SeedJava();

    public ResultSet lookupTenant(Connection conn, String tenant) throws Exception {
        return seed.runTenantQuery(conn, tenant);
    }

    public Object restore(byte[] body) throws Exception {
        return seed.restoreJob(body);
    }

    public Process startTool(String tool) throws Exception {
        return seed.launchTool(tool);
    }

    public ResultSet directSearch(Connection conn, String email) throws Exception {
        Statement stmt = conn.createStatement();
        return stmt.executeQuery("SELECT * FROM users WHERE email = '" + email + "'");
    }

    public Object directRestore(byte[] payload) throws Exception {
        ObjectInputStream input = new ObjectInputStream(new ByteArrayInputStream(payload));
        return input.readObject();
    }
}
""",
    "gauntlet_services/go_consumer.go": """package gauntlet_services

import (
	"database/sql"
	"fmt"
	"html/template"
	"net/http"
	"os/exec"

	seed "gauntlet_fullstack/seed_go"
)

func CrossPRReport(db *sql.DB, accountID string) (*sql.Rows, error) {
	return seed.RunAccountQuery(db, accountID)
}

func CrossPRHTML(raw string) template.HTML {
	return seed.RenderTrustedHTML(raw)
}

func CrossPRCommand(tool string) error {
	return seed.RunMaintenance(tool)
}

func CrossPRSSRF(url string) (*http.Response, error) {
	return seed.FetchInternal(url)
}

func DirectReport(db *sql.DB, accountID string) (*sql.Rows, error) {
	query := fmt.Sprintf("SELECT * FROM reports WHERE account_id = '%s'", accountID)
	return db.Query(query)
}

func DirectCommand(name string) error {
	return exec.Command(name, "--sync").Run()
}
""",
}

_REAL_PR73_RISKS = [
    ("gauntlet_fullstack/SeedJava.java", 16, "sql-injection", "runTenantQuery"),
    ("gauntlet_fullstack/SeedJava.java", 21, "insecure-deserialization", "restoreJob"),
    ("gauntlet_fullstack/SeedJava.java", 25, "command-injection", "launchTool"),
    ("gauntlet_fullstack/admin.component.ts", 20, "xss-bypass", "trustOperatorHtml"),
    ("gauntlet_fullstack/seed_frontend.tsx", 9, "xss", "RawProfileCard"),
    ("gauntlet_fullstack/seed_frontend.tsx", 13, "data-leak", "storeSessionToken"),
    ("gauntlet_fullstack/seed_frontend.tsx", 17, "code-injection", "runClientHook"),
    ("gauntlet_fullstack/seed_frontend.tsx", 21, "command-injection", "spawnReport"),
    ("gauntlet_fullstack/seed_go.go", 20, "sql-injection", "RunAccountQuery"),
    ("gauntlet_fullstack/seed_go.go", 24, "xss", "RenderTrustedHTML"),
    ("gauntlet_fullstack/seed_go.go", 28, "ssrf", "FetchInternal"),
    ("gauntlet_fullstack/seed_go.go", 32, "command-injection", "RunMaintenance"),
    ("gauntlet_fullstack/seed_sinks.py", 17, "sql-injection", "run_report_query"),
    ("gauntlet_fullstack/seed_sinks.py", 21, "insecure-deserialization", "load_session_blob"),
    ("gauntlet_fullstack/seed_sinks.py", 25, "path-traversal", "read_user_file"),
    ("gauntlet_fullstack/seed_sinks.py", 38, "ssrf", "fetch_metadata"),
]

_REAL_PR74_EXPECTED = {
    ("gauntlet_consumers/admin_view.vue", 21, "cross-pr-xss"),
    ("gauntlet_consumers/angular_bridge.ts", 7, "cross-pr-xss-bypass"),
    ("gauntlet_consumers/live_summary.svelte", 8, "cross-pr-data-leak"),
    ("gauntlet_consumers/live_summary.svelte", 12, "cross-pr-xss"),
    ("gauntlet_consumers/python_entry.py", 12, "cross-pr-sql-injection"),
    ("gauntlet_consumers/python_entry.py", 16, "cross-pr-insecure-deserialization"),
    ("gauntlet_consumers/python_entry.py", 20, "cross-pr-path-traversal"),
    ("gauntlet_consumers/python_entry.py", 24, "cross-pr-ssrf"),
    ("gauntlet_consumers/report_panel.tsx", 21, "cross-pr-data-leak"),
    ("gauntlet_consumers/report_panel.tsx", 22, "cross-pr-code-injection"),
    ("gauntlet_consumers/report_panel.tsx", 23, "cross-pr-command-injection"),
    ("gauntlet_consumers/report_panel.tsx", 28, "cross-pr-xss"),
    ("gauntlet_services/CrossPrConsumer.java", 14, "cross-pr-sql-injection"),
    ("gauntlet_services/CrossPrConsumer.java", 18, "cross-pr-insecure-deserialization"),
    ("gauntlet_services/CrossPrConsumer.java", 22, "cross-pr-command-injection"),
    ("gauntlet_services/go_consumer.go", 14, "cross-pr-sql-injection"),
    ("gauntlet_services/go_consumer.go", 18, "cross-pr-xss"),
    ("gauntlet_services/go_consumer.go", 22, "cross-pr-command-injection"),
    ("gauntlet_services/go_consumer.go", 26, "cross-pr-ssrf"),
}


async def test_real_pr73_to_pr74_stacked_fixture_hits_all_manifest_cross_pr_truth(tmp_path):
    db = Database(tmp_path / "real_stacked.db")
    await db.connect()
    # Python's complete-file AST binding proof is deterministic. Other
    # languages remain semantic-confirmation gated until their package and
    # lexical scopes can be resolved with equal rigor.
    llm = _ConfirmEveryChainLLM()
    analyzer = CrossPRAnalyzer(db, llm=llm)
    repo = "Wayne0607/ReviewForge"
    seed_sha = "c8a3d8ad11529c348a8565a771345e201529c680"

    await db.create_run("real-pr73", repo, 73, seed_sha, "470aa1f63a17491c265452d2e85c9a10763eb16c")
    seed_state = StateStore(
        pr_number=73,
        repo=repo,
        head_sha=seed_sha,
        base_sha="470aa1f63a17491c265452d2e85c9a10763eb16c",
        files_changed=list(_REAL_PR73_SEED),
        diff_summary="\n".join(_diff(path, body) for path, body in _REAL_PR73_SEED.items()),
    )
    seed_findings = [
        Finding(
            file=file_path,
            line=line,
            severity="error",
            category=category,
            # RawProfileCard mirrors the live detector row: its message names
            # the concrete DOM sink but not the enclosing symbol.  This prevents
            # the fixture from accidentally bypassing line-range attribution via
            # `_match_symbol_by_finding_text`.
            message=(
                "dangerouslySetInnerHTML renders potentially unsafe DOM content."
                if symbol == "RawProfileCard"
                else f"{symbol} contains the manifest security risk"
            ),
            confidence=0.99,
            reviewer="manifest_truth",
            status="confirmed",
            verified_by="judge" if symbol == "RawProfileCard" else "",
        )
        for file_path, line, category, symbol in _REAL_PR73_RISKS
    ]
    await analyzer.analyze("real-pr73", seed_state, seed_findings)
    await db.complete_run("real-pr73", {})

    consumer_state = StateStore(
        pr_number=74,
        repo=repo,
        head_sha="9661b7676ac1697813ede53c3c31d3385d469213",
        base_sha=seed_sha,
        files_changed=list(_REAL_PR74_CONSUMER),
        diff_summary="\n".join(_diff(path, body) for path, body in _REAL_PR74_CONSUMER.items()),
    )
    findings = await analyzer.analyze("real-pr74", consumer_state, [])
    actual = {(finding.file, finding.line, finding.category) for finding in findings}
    await db.close()

    assert actual == _REAL_PR74_EXPECTED, {
        "missing": sorted(_REAL_PR74_EXPECTED - actual),
        "extra": sorted(actual - _REAL_PR74_EXPECTED),
    }
    assert sum(llm.batch_sizes) > 0
    assert {finding.verified_by for finding in findings} == {"cross-pr-analysis"}


_REAL_PR73_SEED.update(
    {
        "gauntlet_fullstack/admin.component.ts": """import { Component } from "@angular/core";
import { DomSanitizer } from "@angular/platform-browser";

@Component({
  selector: "gauntlet-admin",
  template: `
    <section>
      <img [src]="avatarUrl">
      <div [innerHTML]="announcementHtml"></div>
    </section>
  `,
})
export class AdminComponent {
  announcementHtml = "";
  avatarUrl = "";

  constructor(private sanitizer: DomSanitizer) {}

  trustOperatorHtml(value: string) {
    return this.sanitizer.bypassSecurityTrustHtml(value);
  }
}
""",
        "gauntlet_fullstack/seed_frontend.tsx": """import React from "react";
import { exec } from "child_process";

export function AccountLabel({ text }: { text: string }) {
  return <span>{text.replace(/[<>]/g, "")}</span>;
}

export function RawProfileCard({ html }: { html: string }) {
  return <article dangerouslySetInnerHTML={{ __html: html }} />;
}

export function storeSessionToken(token: string) {
  localStorage.setItem("token", token);
}

export function runClientHook(script: string) {
  return eval(script);
}

export function spawnReport(command: string) {
  exec(command);
}

export function LoginForm() {
  return (
    <form>
      <img src="/avatar.png" />
      <input name="email" onChange={() => storeSessionToken("debug-token")} />
      <button onClick={() => window.location.assign("/next")}>Go</button>
    </form>
  );
}
""",
        "gauntlet_fullstack/seed_go.go": """package gauntlet_fullstack

import (
	"database/sql"
	"fmt"
	"html/template"
	"net/http"
	"os/exec"
)

func NormalizeAccountID(id string) string {
	if id == "" {
		return "anonymous"
	}
	return id
}

func RunAccountQuery(db *sql.DB, accountID string) (*sql.Rows, error) {
	query := fmt.Sprintf("SELECT * FROM accounts WHERE id = '%s'", accountID)
	return db.Query(query)
}

func RenderTrustedHTML(raw string) template.HTML {
	return template.HTML(raw)
}

func FetchInternal(url string) (*http.Response, error) {
	return http.Get(url)
}

func RunMaintenance(binary string) error {
	return exec.Command(binary, "--repair").Run()
}
""",
        "gauntlet_fullstack/seed_sinks.py": """import hashlib
import os
import pickle
import subprocess
import urllib.request
from pathlib import Path

SECRET_TOKEN = "rf_seed_token_123456789"


def normalize_account_id(account_id: str) -> str:
    return "".join(ch for ch in account_id if ch.isalnum() or ch in {"_", "-"})


def run_report_query(conn, account_id: str):
    sql = f"SELECT * FROM reports WHERE account_id = '{account_id}'"
    return conn.execute(sql)


def load_session_blob(blob: bytes):
    return pickle.loads(blob)


def read_user_file(root: str, filename: str) -> str:
    with open(root + "/" + filename, "r") as handle:
        return handle.read()


def run_admin_command(command: str) -> int:
    return subprocess.run(command, shell=True).returncode


def build_redirect_url(next_url: str) -> str:
    return next_url


def fetch_metadata(url: str) -> bytes:
    return urllib.request.urlopen(url).read()


def verify_password(password: str, expected: str) -> bool:
    digest = hashlib.md5(password.encode()).hexdigest()
    return digest == expected


def write_debug_dump(path: Path, body: str) -> None:
    os.makedirs(path.parent, exist_ok=True)
    path.write_text(body)
""",
    }
)


def test_extract_function_returns_bounded_real_seed_context_for_supported_languages():
    analyzer = CrossPRAnalyzer.__new__(CrossPRAnalyzer)
    cases = [
        (
            "gauntlet_fullstack/seed_sinks.py",
            "run_report_query",
            "conn.execute(sql)",
        ),
        (
            "gauntlet_fullstack/seed_frontend.tsx",
            "RawProfileCard",
            "dangerouslySetInnerHTML",
        ),
        (
            "gauntlet_fullstack/admin.component.ts",
            "trustOperatorHtml",
            "bypassSecurityTrustHtml",
        ),
        (
            "gauntlet_fullstack/seed_go.go",
            "RunAccountQuery",
            "db.Query(query)",
        ),
        (
            "gauntlet_fullstack/SeedJava.java",
            "restoreJob",
            "input.readObject()",
        ),
    ]

    for file_path, symbol, evidence in cases:
        context = analyzer._extract_function(_REAL_PR73_SEED[file_path], symbol)
        assert symbol in context, file_path
        assert evidence in context, file_path
        assert 1 <= len(context.splitlines()) <= 30, file_path


def test_extract_function_ignores_signature_decoys_inside_multiline_strings():
    analyzer = CrossPRAnalyzer.__new__(CrossPRAnalyzer)
    content = '''PROMPT = """
def dangerous(value):
    return fake(value)
"""

def dangerous(value):
    return eval(value)

def after():
    return "not part of dangerous"
'''

    context = analyzer._extract_function(content, "dangerous")

    assert "eval(value)" in context
    assert "fake(value)" not in context
    assert "def after" not in context
