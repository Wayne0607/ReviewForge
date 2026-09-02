from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext, _find_owner_relations
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, UnitKind


@dataclass
class _Hit:
    path: str
    line: int
    symbol: str = ""
    start_line: int = 0
    end_line: int = 0
    text: str = ""


class _Workspace:
    def __init__(self) -> None:
        self.info = SimpleNamespace(source="tarball", digest="workspace-digest", head_sha="head-123")
        self.files = {
            "src/service.py": "\n".join(
                [
                    "from typing import Protocol",
                    "",
                    "class BaseService:",
                    "    def get_user(self, user_id):",
                    "        return None",
                    "",
                    "class Service(BaseService, Protocol):",
                    "    def get_user(self, user_id):",
                    "        self.mu.lock()",
                    "        self.cache[user_id] = self.repo.fetch(user_id)",
                    "        self.mu.unlock()",
                    "        return self.cache[user_id]",
                    "",
                    "    def set_user(self, user_id, value):",
                    "        self.cache[user_id] = value",
                    "        return value",
                ]
            ),
            "src/caller.py": "\n".join(
                [
                    "def run(service, user_id):",
                    "    value = service.get_user(user_id)",
                    "    return value",
                ]
            ),
            "src/repo.py": "\n".join(
                [
                    "class Repo:",
                    "    MUST_NOT_INCLUDE = 'before callee'",
                    "    def fetch(self, user_id):",
                    '        """Fetch a user by id."""',
                    "        return user_id",
                ]
            ),
            "src/base.ts": "\n".join(
                [
                    "class Base {",
                    "    getUser(): string { return ''; }",
                    "}",
                ]
            ),
            "src/interface.ts": "interface Interface { getUser(): string; }",
            "src/child.ts": "\n".join(
                [
                    "class Child extends Base implements Interface {",
                    "    getUser(): string { return ''; }",
                    "}",
                ]
            ),
            "tests/test_service.py": "\n".join(
                [
                    "def test_get_user(service):",
                    "    assert service.get_user('u-1') == 'u-1'",
                ]
            ),
            "config/users.yaml": "users: {}",
            "config/users.yml": "users: {}\ncache: enabled",
            "schema/users.sql": "create table users (id text primary key);",
        }

    def read(self, path: str, start: int | None = None, end: int | None = None) -> str | None:
        content = self.files.get(path)
        if content is None or start is None:
            return content
        lines = content.splitlines()
        return "\n".join(lines[max(0, start - 1) : min(len(lines), end or len(lines))])

    def find_callers(self, symbol: str, *, language: str, max_hits: int) -> list[_Hit]:
        if symbol == "get_user":
            return [_Hit("src/caller.py", 2, symbol=symbol)]
        return []

    def find_symbol_definitions(self, symbol: str, *, language: str) -> list[_Hit]:
        return {
            "fetch": [_Hit("src/repo.py", 3, symbol="fetch")],
            "BaseService": [_Hit("src/service.py", 3, symbol="BaseService")],
            "Protocol": [_Hit("src/service.py", 1, symbol="Protocol")],
            "Base": [_Hit("src/base.ts", 1, symbol="Base")],
            "Interface": [_Hit("src/interface.ts", 1, symbol="Interface")],
        }.get(symbol, [])

    def grep(self, pattern: str, *, globs: list[str] | None, max_hits: int, context: int = 0) -> list[_Hit]:
        if "users" in pattern:
            return [
                _Hit("schema/users.sql", 1, text="create table users"),
                _Hit("config/users.yml", 1, text="users: {}"),
            ]
        return []


def _changeset() -> SemanticChangeSet:
    return SemanticChangeSet(
        repo="example/repo",
        pr_number=7,
        head_sha="head-123",
        units=[
            SemanticUnit(
                id="unit-service",
                path="src/service.py",
                language="python",
                kind=UnitKind.SYMBOL,
                symbol="get_user",
                start_line=8,
                end_line=13,
                added_lines=[9, 10, 11, 12],
                calls=[{"caller": "get_user", "callee": "fetch", "line": 10}],
                candidate_tests=["tests/test_service.py"],
                risk_score=0.9,
            ),
            SemanticUnit(
                id="unit-resource",
                path="config/users.yaml",
                language="",
                kind=UnitKind.RESOURCE,
                added_lines=[1],
                risk_score=0.1,
            ),
            SemanticUnit(
                id="unit-child",
                path="src/child.ts",
                language="typescript",
                kind=UnitKind.SYMBOL,
                symbol="getUser",
                start_line=2,
                end_line=2,
                added_lines=[2],
                risk_score=0.2,
            ),
        ],
    )


def test_collects_context_kinds_and_pr_intent() -> None:
    pack = ContextPack.build(
        _changeset(),
        _Workspace(),
        max_slices=32,
        pr_title="Fix user lookup",
        pr_body="Keep the cache and repository contract aligned.",
        linked_issues=[{"title": "Issue 7: user lookup"}],
    )

    service = pack.units["unit-service"]
    kinds = {item.kind for item in service.slices}
    assert {"caller", "callee", "base_class", "sibling", "lock_usage", "field_usage", "test"} <= kinds
    assert "Fix user lookup" in service.pr_intent
    assert "Issue 7: user lookup" in pack.pr_intent
    assert any("service.get_user" in item.text for item in service.slices if item.kind == "caller")
    callee = next(item for item in service.slices if item.kind == "callee")
    assert "def fetch" in callee.text
    assert "Fetch a user by id." in callee.text
    assert "return user_id" in callee.text
    assert "MUST_NOT_INCLUDE" not in callee.text
    base = next(item for item in service.slices if item.kind == "base_class")
    assert "class BaseService:" in base.text
    assert "def get_user" in base.text

    resource = pack.units["unit-resource"]
    assert any(item.kind == "schema" and "create table users" in item.text for item in resource.slices)
    config = next(item for item in resource.slices if item.kind == "config")
    assert "users: {}" in config.text
    assert "cache: enabled" in config.text
    child = pack.units["unit-child"]
    child_base = next(item for item in child.slices if item.kind == "base_class")
    child_interface = next(item for item in child.slices if item.kind == "interface")
    assert "class Base" in child_base.text
    assert "getUser" in child_base.text
    assert "interface Interface" in child_interface.text
    assert "getUser" in child_interface.text


def test_rendering_is_deterministic_and_risk_ordered() -> None:
    workspace = _Workspace()
    changeset = _changeset()
    first = ContextPack.build(changeset, workspace)
    second = ContextPack.build(changeset, workspace)

    assert first.render_all(max_chars=40_000) == second.render_all(max_chars=40_000)
    rendered = first.render_all(max_chars=320)
    assert len(rendered) <= 320
    assert rendered.startswith("## Unit unit-service")


def test_render_all_marks_slice_and_omitted_unit_kinds() -> None:
    high_slices = [
        ContextSlice("caller", "src/high.py", 1, 1, "high", "high caller", "caller", "sha"),
        ContextSlice("callee", "src/high.py", 2, 2, "high", "high callee", "callee", "sha"),
    ]
    low_slices = [ContextSlice("test", "tests/low.py", 1, 1, "low", "low test", "test", "sha")]
    pack = ContextPack(
        units={
            "high": UnitContext("high", high_slices),
            "low": UnitContext("low", low_slices),
        }
    )
    pack._unit_risks = {"high": 1.0, "low": 0.1}
    high_first_slice = ContextPack(units={"high": UnitContext("high", high_slices[:1])}).render_for_unit(
        "high", max_chars=10_000
    )

    rendered = pack.render_all(max_chars=len(high_first_slice))
    assert rendered.startswith("## Unit high")
    assert "high callee" not in rendered
    assert "low test" not in rendered
    assert pack.units["high"].truncated_kinds == ["callee"]
    assert pack.units["low"].truncated_kinds == ["test"]


def test_slice_limit_records_kinds_deterministically() -> None:
    pack = ContextPack.build(_changeset(), _Workspace(), max_slices=2)
    service = pack.units["unit-service"]

    assert len(service.slices) == 2
    assert service.truncated_kinds
    assert service.truncated_kinds == list(dict.fromkeys(service.truncated_kinds))


def test_degraded_workspace_returns_only_pr_intent() -> None:
    workspace = _Workspace()
    workspace.info.source = "api-fallback"
    pack = ContextPack.build(_changeset(), workspace, pr_intent="degraded run")

    assert all(not context.slices for context in pack.units.values())
    assert all(context.truncated_kinds == ["all"] for context in pack.units.values())
    assert pack.pr_intent == "degraded run"


@pytest.mark.parametrize(
    ("language", "path", "source", "expected"),
    [
        (
            "python",
            "child.py",
            "class Child(Base, Interface):\n    def run(self):\n        return 1",
            [("base_class", "Base"), ("base_class", "Interface")],
        ),
        (
            "java",
            "Child.java",
            "class Child extends Base implements Interface {\n    void run() {}\n}",
            [("base_class", "Base"), ("interface", "Interface")],
        ),
        (
            "typescript",
            "child.ts",
            "class Child extends Base implements Interface {\n    run(): void {}\n}",
            [("base_class", "Base"), ("interface", "Interface")],
        ),
        (
            "go",
            "child.go",
            "type Child struct {\n    Base\n}\nfunc (c Child) Run() {}",
            [("base_class", "Base")],
        ),
        (
            "ruby",
            "child.rb",
            "class Child < Base\n    def run\n    end\nend",
            [("base_class", "Base")],
        ),
    ],
)
def test_extracts_inheritance_relations_for_supported_languages(
    language: str,
    path: str,
    source: str,
    expected: list[tuple[str, str]],
) -> None:
    owner, relations = _find_owner_relations(
        source,
        path,
        SimpleNamespace(language=language, path=path, symbol="run", start_line=2, added_lines=[2]),
    )

    assert owner == "Child"
    assert relations == expected
