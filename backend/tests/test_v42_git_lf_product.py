"""Fixed diffs and source reads use Git physical lines in new v4.2 runs."""

import subprocess
from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from test_v42_integration import NativeModel, product
from test_v42_units_product import FixedBaseGitHub, TwoFileWorkspace, two_file_state
from test_v42_verifier import Model

from reviewforge.review_v42.adapter import PinnedToolGateway, changed_regions
from reviewforge.review_v42.contracts import BindingError, Case, EvidenceRef, ReviewScope, Run
from reviewforge.review_v42.publication import Publication
from reviewforge.review_v42.snapshot_diff import pinned_regions
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.tools import EvidenceGateway
from reviewforge.review_v42.verifier import Verifier, _citation


class Workspace:
    source = "tarball"
    truncated = False

    def __init__(self, sha, files):
        self.head_sha, self.files = sha, files

    def list_paths(self):
        return sorted(self.files)

    def read_bytes(self, path):
        return self.files[path]


GIT_LINES = SimpleNamespace(metadata={"source_line_policy": "git-lf-1"})


def git_regions(tmp_path, before, after):
    left, right = tmp_path / "before", tmp_path / "after"
    left.write_bytes(before)
    right.write_bytes(after)
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "diff",
            "--no-index",
            "--no-ext-diff",
            "--no-color",
            "--",
            str(left),
            str(right),
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    patch = result.stdout.decode("utf-8")
    return changed_regions(SimpleNamespace(files_changed=["x"], file_diffs={"x": patch}), run=GIT_LINES), patch


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("first\u2028inside\nold\nlast\n", "first\u2028inside\nnew\nlast\n"),
        ("one\vwithin\rinside\nold\n", "one\vwithin\rinside\nnew\n"),
        ("first\r\nold\r\nlast\r\n", "first\r\nnew\r\nlast\r\n"),
        ("first\nlast", "first\nlast\n"),
        ("first\nlast\n", "first\nlast"),
        ("abc\r\n", "abc\n"),
    ],
)
async def test_generated_hunk_ranges_match_real_git(tmp_path, before, after):
    expected, git_patch = git_regions(tmp_path, before.encode(), after.encode())
    result = await pinned_regions(
        Workspace("base", {"x": before.encode()}), Workspace("head", {"x": after.encode()}), run=GIT_LINES
    )
    actual = result["regions"]
    fields = ("start_line", "end_line", "base_start_line", "base_end_line", "changed_lines", "unsupported")
    assert [[r.get(k) for k in fields] for r in actual] == [[r.get(k) for k in fields] for r in expected]
    assert all(not r["unsupported"] for r in actual)
    if "No newline at end of file" in git_patch:
        assert "\\ No newline at end of file\n" in actual[0]["patch"]


async def test_lf_source_page_search_base_and_legacy_behavior():
    head = Workspace("head", {"x": "a\u2028b\vneedle\rmore\nlast\r\n".encode()})
    base = Workspace("base", {"x": "base\u2028needle\rpart\ntail\n".encode()})
    state = SimpleNamespace(
        repo="org/repo", head_sha="head", base_sha="base", linked_issues=[], files_changed=[], file_diffs={}
    )
    gateway = PinnedToolGateway(head, state, base_workspace=base, run=GIT_LINES)
    first = await gateway("read_source", {"path": "x", "start_line": 1, "end_line": 1})
    assert first["content"].encode() == "a\u2028b\vneedle\rmore\n".encode()
    assert (first["start_line"], first["end_line"], first["metadata"]["file_lines"]) == (1, 1, 2)
    base_first = await gateway("read_source", {"path": "x", "side": "base", "start_line": 1, "end_line": 1})
    assert base_first["content"] == "base\u2028needle\rpart\n"
    second = await gateway("read_source", {"path": "x", "start_line": 2, "end_line": 200})
    assert second["content"] == "last\r\n" and second["end_line"] == 2
    search = await gateway("search_code", {"query": "needle"})
    assert '"line": 1' in search["content"]
    legacy = PinnedToolGateway(head, state, base_workspace=base)
    old_first = await legacy("read_source", {"path": "x", "start_line": 1, "end_line": 1})
    assert old_first["content"] == "a\u2028"


async def test_legacy_line_ending_only_stays_structural_and_new_policy_is_textual():
    base, head = Workspace("base", {"x": b"a\r\n"}), Workspace("head", {"x": b"a\n"})
    old = (await pinned_regions(base, head))["regions"][0]
    new = (await pinned_regions(base, head, run=GIT_LINES))["regions"][0]
    assert old["unsupported"] and not new["unsupported"]
    assert new["changed_lines"] == [1] and "-a\r\n+a\n" in new["patch"]


async def test_deleted_and_binary_regions_remain_explicit():
    base = Workspace("base", {"old": b"a\xe2\x80\xa8b\n", "binary": b"\0old"})
    head = Workspace("head", {"binary": b"\0new"})
    rows = {r["path"]: r for r in (await pinned_regions(base, head, run=GIT_LINES))["regions"]}
    assert rows["old"]["side"] == "base" and (rows["old"]["start_line"], rows["old"]["end_line"]) == (1, 1)
    assert not rows["old"]["unsupported"] and rows["old"]["changed_lines"] == []
    assert rows["binary"]["unsupported"] and rows["binary"]["binary"]


async def test_product_binds_new_policy_and_resumes_same_run(tmp_path, monkeypatch):
    monkeypatch.delenv("REVIEWFORGE_V42_UNIT_PLAN", raising=False)
    monkeypatch.delenv("REVIEWFORGE_V42_UNIT_SCHEDULER", raising=False)
    monkeypatch.delenv("REVIEWFORGE_V42_SHARED_INVESTIGATION", raising=False)
    model = NativeModel(lambda *_: AIMessage(content="No decision yet"))
    obj, _ = product(tmp_path, monkeypatch, model, model, TwoFileWorkspace(), policy=None)
    obj._gateway._github = FixedBaseGitHub()
    result = await obj.run(two_file_state())
    assert result["status"] == "partial"
    assert result["review_v42_report"]["run"]["metadata"]["source_line_policy"] == "git-lf-1"
    resumed = await obj.run(two_file_state())
    assert resumed["run_id"] == result["run_id"]


def test_legacy_checkpoint_cannot_be_rebound_to_lf_semantics(tmp_path):
    legacy = Run("old", "org/repo", "base", "head", policy="v42-units-6", metadata={})
    store = ReviewStore(tmp_path / "legacy.sqlite", legacy)
    with pytest.raises(BindingError, match="bound"):
        ReviewStore(store.path, replace(legacy, metadata={"source_line_policy": "git-lf-1"}))


async def test_git_diff_source_citation_and_comment_share_physical_lines(tmp_path):
    before = "prefix\u2028old\rwithin\nlast\n".encode()
    after = "prefix\u2028new\rwithin\nlast\n".encode()
    expected, _ = git_regions(tmp_path, before, after)
    base, head = Workspace("base", {"x": before}), Workspace("head", {"x": after})
    fixed = await pinned_regions(base, head, run=GIT_LINES)
    region = fixed["regions"][0]
    assert region["changed_lines"] == expected[0]["changed_lines"] == [1]

    run = Run(
        "run",
        "org/repo",
        "base",
        "head",
        metadata={
            "changed_regions": [region],
            "workspace_digest": fixed["head_identity"],
            "source_line_policy": "git-lf-1",
        },
    )
    store = ReviewStore(tmp_path / "chain.sqlite", run)
    state = SimpleNamespace(
        repo=run.repo,
        head_sha=run.head,
        base_sha=run.base,
        linked_issues=[],
        files_changed=["x"],
        file_diffs={"x": region["patch"]},
    )
    gateway = EvidenceGateway(store, PinnedToolGateway(head, state, base_workspace=base, run=run))
    receipt = await gateway.execute("read_source", {"path": "x", "start_line": 1, "end_line": 1})
    ref = store.get(EvidenceRef, receipt["evidence_ref"])
    assert receipt["content"].encode() == after.split(b"\n", 1)[0] + b"\n"
    assert (ref.start_line, ref.end_line) == (1, 1)
    store.add(ReviewScope("scope", run.id, "scope", regions=[region], status="reviewed"))
    store.add(
        Case(
            "case",
            run.id,
            "scope",
            trigger="trigger",
            mechanism="mechanism",
            claims=[{"id": "claim", "statement": "claim", "impact": "impact"}],
            evidence_refs=[ref.id],
        )
    )
    citation = {"ref": ref.id, "quote": "new", "start_line": 1, "end_line": 1}
    result = {
        "verdict": "accept",
        "reason": "verified",
        "gaps": [],
        "facts": {
            key: {"statement": key, "citations": [dict(citation)]}
            for key in ("trigger", "actual", "expected", "change", "impact")
        },
    }
    assert (await Verifier(store, Model(result), None).verify("case"))[0]["verdict"] == "accept"
    assert Publication(store).local_report()["comments"][0]["anchor"] == {"path": "x", "line": 1, "side": "RIGHT"}
    with pytest.raises(ValueError):
        _citation(store, {**citation, "start_line": 2, "end_line": 2})
