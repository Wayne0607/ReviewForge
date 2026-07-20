"""Tests for the pre-planning Context Engine and Impact Manifest."""

from __future__ import annotations

import json

from reviewforge.core.config import ReviewForgeConfig
from reviewforge.core.database import Database
from reviewforge.core.specs import build_registry
from reviewforge.core.state import StateStore
from reviewforge.engine.context_engine import ContextEngine, render_impact_manifest
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
    finally:
        await db.close()


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
