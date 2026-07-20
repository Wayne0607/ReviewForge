"""Tests for the pre-planning Context Engine and Impact Manifest."""

from __future__ import annotations

import json

from reviewforge.core.config import ReviewForgeConfig
from reviewforge.core.database import Database
from reviewforge.core.specs import build_registry
from reviewforge.core.state import StateStore
from reviewforge.engine.context_engine import ContextEngine, render_impact_manifest
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.prompt import build_planner_prompt
from reviewforge.tools.gateway import ToolGateway


class _ContextGitHub:
    async def get_file_content(self, repo: str, ref: str, file_path: str) -> str:
        assert ref == "head"
        return "def authorize(user):\n    return user.is_admin\n\ndef process(user):\n    return authorize(user)\n"

    async def search_code(self, repo: str, pattern: str, file_glob: str = "") -> str:
        if pattern == "process":
            return "- src/caller.py\n- tests/test_service.py\n- src/service.py"
        if pattern == "authorize":
            return "- tests/test_service.py\n- src/policy.py"
        return "No results"

    async def get_pr_files(self, repo: str, pr_number: int):
        raise AssertionError("the supplied per-run diff cache should be reused")


async def test_context_engine_builds_symbol_references_tests_and_graph(tmp_path):
    db = Database(tmp_path / "context.db")
    await db.connect()
    await db.upsert_symbol("src/legacy.py", "process", "function", "old-run", pr_number=4)
    await db.upsert_relation(
        "old-run",
        "src/caller.py",
        "src/legacy.py",
        "process",
        "call",
        source_symbol="dispatch",
    )
    await db.upsert_symbol("unrelated/other.py", "process", "function", "other-run", pr_number=99)
    try:
        state = StateStore(
            repo="owner/repo",
            pr_number=9,
            head_sha="head",
            files_changed=["src/service.py"],
            file_diffs={
                "src/service.py": (
                    "@@ -4,2 +4,2 @@\n def process(user):\n-    return True\n+    return authorize(user)\n"
                )
            },
        )
        manifest = await ContextEngine(ToolGateway(build_registry(), _ContextGitHub()), db).build(state)

        assert manifest is state.impact_manifest
        indexed = manifest["files"][0]
        assert indexed["changed_symbols"][0]["name"] == "process"
        assert {item["symbol"] for item in manifest["references"]} >= {"process", "authorize"}
        assert manifest["candidate_tests"] == ["tests/test_service.py"]
        assert any(item["kind"] == "definition" for item in manifest["historical_graph"])
        assert any(item["kind"] == "call" for item in manifest["historical_graph"])
        assert all(item.get("file") != "unrelated/other.py" for item in manifest["historical_graph"])
        assert any(item["type"] == "blast-radius" for item in manifest["risk_signals"])
        assert any(page["title"] == "process" for page in manifest["wiki_pages"])
        process_page = next(page for page in manifest["wiki_pages"] if page["title"] == "process")
        assert process_page["source"]["sha"] == "head"
        assert any(fact["kind"] == "return-or-error" for fact in process_page["facts"])
        assert any(page["source"]["path"] == "src/caller.py" for page in manifest["wiki_pages"])
    finally:
        await db.close()


async def test_context_engine_represents_localization_resources_without_symbols():
    state = StateStore(
        repo="owner/repo",
        pr_number=10,
        head_sha="head",
        files_changed=["themes/messages/messages_zh_CN.properties"],
        file_diffs={"themes/messages/messages_zh_CN.properties": "@@ -1 +1 @@\n-old=phone\n+totpStep1=手機應用程式\n"},
    )

    manifest = await ContextEngine(ToolGateway(build_registry(), _ContextGitHub())).build(state)

    assert manifest["files"] == []
    assert manifest["coverage"]["indexed_resource_files"] == 1
    assert manifest["resource_files"] == [
        {
            "path": "themes/messages/messages_zh_CN.properties",
            "kind": "localization",
            "locale": "zh_CN",
            "added_lines": [1],
            "added_entries": 1,
        }
    ]


async def test_context_tool_is_permission_checked_and_filterable():
    registry = build_registry()
    gateway = ToolGateway(registry, _ContextGitHub())
    state = StateStore(
        impact_manifest={
            "version": 1,
            "files": [
                {"path": "a.py", "changed_symbols": [{"name": "alpha"}]},
                {"path": "b.py", "changed_symbols": [{"name": "beta"}]},
            ],
            "references": [{"symbol": "alpha", "paths": ["tests/test_a.py"]}],
            "historical_graph": [],
            "wiki_pages": [
                {"title": "alpha", "facts": []},
                {"title": "beta", "facts": []},
            ],
        }
    )

    output = await gateway.invoke(
        "get_change_context",
        {"file_path": "a.py", "symbol": "alpha"},
        state,
        agent_name="security_reviewer",
    )
    parsed = json.loads(output)
    assert [item["path"] for item in parsed["files"]] == ["a.py"]
    assert parsed["references"][0]["symbol"] == "alpha"
    assert [item["title"] for item in parsed["wiki_pages"]] == ["alpha"]


def test_planner_prompt_includes_bounded_impact_manifest():
    registry = build_registry()
    messages = build_planner_prompt(
        {
            "registry": registry,
            "repo": "owner/repo",
            "pr_number": 1,
            "files_changed": ["src/service.py"],
            "diff_summary": "+return authorize(user)",
            "impact_manifest_text": '{"changed_symbols":["process"]}',
        }
    )
    assert "Impact Manifest" in messages[1]["content"]
    assert "process" in messages[1]["content"]


def test_yaml_can_select_agentic_reviewers(tmp_path, monkeypatch):
    monkeypatch.delenv("REVIEWFORGE_AGENTIC_REVIEWERS", raising=False)
    monkeypatch.delenv("REVIEWFORGE_AGENTIC_DEFAULT", raising=False)
    config_path = tmp_path / "reviewforge.yaml"
    config_path.write_text(
        "agentic_reviewers:\n  - security_reviewer\nagentic_default: false\n",
        encoding="utf-8",
    )

    config = ReviewForgeConfig.load(config_path)

    assert config.agentic_reviewers == ["security_reviewer"]
    assert config.agentic_default is False


def test_render_manifest_can_filter_without_mutating_original():
    manifest = {
        "version": 1,
        "files": [
            {"path": "a.py", "changed_symbols": [{"name": "alpha"}]},
            {"path": "b.py", "changed_symbols": [{"name": "beta"}]},
        ],
        "references": [],
        "historical_graph": [],
    }
    rendered = json.loads(render_impact_manifest(manifest, files=["b.py"]))
    assert [item["path"] for item in rendered["files"]] == ["b.py"]
    assert len(manifest["files"]) == 2


def test_render_manifest_compacts_low_value_file_details():
    manifest = {
        "version": 2,
        "files": [
            {
                "path": "a.py",
                "language": "python",
                "added_lines": list(range(30)),
                "changed_symbols": [],
                "imports": [],
                "calls": [],
                "content_available": True,
            }
        ],
        "references": [],
        "historical_graph": [],
        "wiki_pages": [],
    }

    rendered = json.loads(render_impact_manifest(manifest))

    assert rendered["files"][0]["added_lines"] == list(range(12))
    assert "content_available" not in rendered["files"][0]
    assert len(manifest["files"][0]["added_lines"]) == 30


def test_render_manifest_truncation_remains_valid_json():
    manifest = {
        "version": 1,
        "files": [{"path": f"src/{index}.py", "calls": ["x" * 100]} for index in range(20)],
        "references": [],
        "historical_graph": [],
        "risk_signals": [],
    }
    rendered = render_impact_manifest(manifest, max_chars=200)
    assert json.loads(rendered)["truncated"] is True


def test_render_manifest_truncation_does_not_erase_shared_evidence():
    manifest = {
        "version": 2,
        "files": [{"path": "src/a.py", "calls": ["x" * 200]}],
        "references": [{"symbol": f"symbol_{index}", "paths": [f"src/{index}.py"]} for index in range(8)],
        "historical_graph": [{"kind": "call", "symbol": "authorize"}],
        "wiki_pages": [{"title": "authorize", "facts": [{"evidence": "x" * 200}]}],
        "risk_signals": [{"type": "blast-radius", "symbol": "authorize"}],
    }

    render_impact_manifest(manifest, max_chars=200)

    assert len(manifest["references"]) == 8
    assert len(manifest["historical_graph"]) == 1
    assert len(manifest["wiki_pages"]) == 1
    assert len(manifest["risk_signals"]) == 1


def test_security_agentic_requires_retrieved_cross_file_evidence():
    task = type("Task", (), {"reviewer": "security_reviewer", "files": ["src/service.py"]})()
    state = StateStore(files_changed=["src/service.py"])
    assert Orchestrator._has_agentic_context(task, state) is False

    state.impact_manifest = {
        "risk_signals": [
            {"type": "blast-radius", "file": "src/service.py", "symbol": "authorize", "reference_count": 2}
        ],
        "historical_graph": [],
    }
    assert Orchestrator._has_agentic_context(task, state) is False

    state.impact_manifest["risk_signals"].append(
        {"type": "security-sensitive-symbol", "file": "src/service.py", "symbol": "authorize"}
    )
    assert Orchestrator._has_agentic_context(task, state) is True


def test_non_security_agentic_configuration_is_not_context_gated():
    task = type("Task", (), {"reviewer": "style_reviewer", "files": ["src/service.py"]})()
    assert Orchestrator._has_agentic_context(task, StateStore()) is True
