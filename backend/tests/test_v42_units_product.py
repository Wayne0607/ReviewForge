"""Product-level checks for unit ownership and integration responsibility."""

import io
import json
import tarfile
from collections import defaultdict

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from test_v42_integration import NativeModel, Workspace, product, tool

from reviewforge.core.state import StateStore


class TwoFileWorkspace(Workspace):
    content = {"a.py": "value = 0\n", "b.py": "from a import value\n"}

    async def read_async(self, path):
        return self.content.get(path)

    def read_bytes(self, path):
        return self.content[path].encode()

    def list_paths(self):
        return sorted(self.content)


class FixedBaseGitHub:
    async def get_comparison_base(self, repo, base, head_repo, head):
        assert (base, head) == ("base", "head")
        return "base"

    async def get_repo_tarball(self, repo, sha):
        assert sha == "base"
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as output:
            for path, content in {"a.py": b"value = 1\n", "b.py": b"from a import old_value\n"}.items():
                entry = tarfile.TarInfo("repo/" + path)
                entry.size = len(content)
                output.addfile(entry, io.BytesIO(content))
        return archive.getvalue()


def two_file_state():
    state = StateStore(
        repo="example/repo",
        pr_number=2,
        head_sha="head",
        base_sha="base",
        files_changed=["a.py", "b.py"],
        file_diffs={
            "a.py": "@@ -1 +1 @@\n-value = 1\n+value = 0",
            "b.py": "@@ -1 +1 @@\n-from a import old_value\n+from a import value",
        },
        pr_title="Change a value and its consumer",
        pr_body="Review both changed files and their interaction.",
    )
    state.linked_issue_context = {
        "schema": "review-v42-no-external-context",
        "repo": state.repo,
        "pr_number": state.pr_number,
        "head_sha": state.head_sha,
        "issues": [],
        "status": "excluded-historical-leakage",
    }
    return state


def unit_context(messages):
    for message in messages:
        if isinstance(message, HumanMessage):
            try:
                state = json.loads(message.content)
            except (TypeError, ValueError):
                continue
            if isinstance(state, dict) and state.get("unit_role") in {"primary", "integration"}:
                return state
    raise AssertionError("the model did not receive a unit-bound context")


def last_receipt(messages):
    return json.loads(next(message.content for message in reversed(messages) if isinstance(message, ToolMessage)))


@pytest.mark.asyncio
async def test_product_reviews_each_file_and_requires_source_backed_integration(tmp_path, monkeypatch):
    """Coverage and the cross-file check are separate responsibilities."""
    phases = defaultdict(int)
    refs = {}
    primary_units = set()
    integration_calls = []

    def review(messages, _turn):
        state = unit_context(messages)
        scope = state["scope"]
        scope_id = scope["id"]
        role = state["unit_role"]
        phase = phases[scope_id]
        phases[scope_id] += 1
        if role == "primary":
            primary_units.add(scope_id)
            regions = scope["regions"]
            assert regions and len({region["path"] for region in regions}) == 1
            path = regions[0]["path"]
            if phase == 0:
                return tool("read_source", {"path": path, "side": "head"})
            receipt = last_receipt(messages)
            assert receipt["content"] == TwoFileWorkspace.content[path]
            ref = receipt["evidence_ref"]
            return tool(
                "submit_unit_decision",
                {
                    "checked_region_ids": [region["id"] for region in regions],
                    "observations": [
                        {
                            "statement": "Inspected the changed file and its fixed source.",
                            "evidence_refs": [ref],
                            "region_ids": [region["id"] for region in regions],
                        }
                    ],
                    "unresolved_refs": [],
                    "finish": True,
                },
            )

        assert role == "integration"
        if phase == 0:
            return tool("read_source", {"path": "a.py", "side": "head"})
        if phase == 1:
            refs["a.py"] = last_receipt(messages)["evidence_ref"]
            return tool("read_source", {"path": "b.py", "side": "head"})
        if phase == 2:
            refs["b.py"] = last_receipt(messages)["evidence_ref"]
        obligations = state["integration_obligations"]
        assert obligations, "an empty integration plan must not count as a completed cross-file check"
        outstanding = [item for item in obligations if item["status"] != "checked"]
        if outstanding:
            obligation = outstanding[0]
            integration_calls.append(obligation["id"])
            return tool(
                "record_integration_receipt",
                {
                    "obligation_id": obligation["id"],
                    "disposition": "checked",
                    "evidence_refs": [refs["a.py"], refs["b.py"]],
                    "reason": "Inspected both the changed value and its consumer.",
                },
            )
        return tool("finish_integration", {})

    reviewer = NativeModel(review)
    verifier = NativeModel(lambda *_: pytest.fail("no candidate requires verification"))
    obj, _ = product(tmp_path, monkeypatch, reviewer, verifier, TwoFileWorkspace(), policy=None)
    obj._gateway._github = FixedBaseGitHub()
    result = await obj.run(two_file_state())
    assert result["status"] == "completed", result
    assert len(primary_units) == 2 and integration_calls
    report = result["review_v42_report"]
    assert report["metrics"]["coverage"]["regions"] == 2
    assert report["metrics"]["coverage"]["checked_regions"] == 2
    assert report["comments"] == [] and result["comments_sent"] == 0

    calls = len(reviewer._inputs)
    again = await obj.run(two_file_state())
    assert again["status"] == "completed" and again["run_id"] == result["run_id"]
    assert len(reviewer._inputs) == calls
