from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path

import pytest
from test_discovery_v3 import ScriptedLLM
from test_discovery_v3 import clean as text_clean
from test_discovery_v3 import diff as text_diff

from reviewforge.core.change_manifest import FileChange, file_change_id, is_verified_rename
from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.discovery import DiscoveryLimits, plan_discovery
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.semantic_diff import compile_semantic_changeset

FIXTURES = Path(__file__).parent / "fixtures" / "structural_rename"
HEAD = "a" * 40
BASE = "b" * 40


def rename(**updates) -> FileChange:
    content = (FIXTURES / "resources/new.json").read_bytes()
    blob = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
    change = FileChange(
        repo="owner/repo",
        head_repo="owner/repo",
        path="resources/new.json",
        previous_path="resources/old.json",
        status="renamed",
        base_sha=BASE,
        head_sha=HEAD,
        comparison_base_sha="c" * 40,
        base_blob_sha=blob,
        head_blob_sha=blob,
        base_mode="100644",
        head_mode="100644",
        base_type="blob",
        head_type="blob",
        patch_state="verified-no-text-change",
        source="github-pr-files+git-tree",
    )
    change = replace(change, **updates)
    return replace(change, id=file_change_id(change))


class Workspace:
    def __init__(self, *, updated=False, head=True, consumer=True):
        self.info = {"source": "tarball", "head_sha": HEAD, "digest": "digest"}
        self.files = {}
        if head:
            self.files["resources/new.json"] = (FIXTURES / "resources/new.json").read_text()
        if consumer:
            folder = "updated_consumer" if updated else "stale_consumer"
            self.files["app.py"] = (FIXTURES / folder / "app.py").read_text()

    def read(self, path):
        return self.files.get(path)

    def grep(self, pattern, *, globs=None, max_hits=8, context=0, diverse=False):
        hits = []
        for path, text in sorted(self.files.items()):
            if globs and not any(fnmatch.fnmatch(path, glob) for glob in globs):
                continue
            hits.extend(
                {"path": path, "line": index, "text": line}
                for index, line in enumerate(text.splitlines(), 1)
                if re.search(pattern, line)
            )
        return hits[:max_hits]


def inputs(change=None, workspace=None, protocol=3):
    change = change or rename()
    state = StateStore(
        repo="owner/repo",
        head_repo="owner/repo",
        pr_number=1,
        head_sha=HEAD,
        base_sha=BASE,
        files_changed=[change.path],
        file_diffs={change.path: ""},
        file_changes={change.path: change},
    )
    changeset = compile_semantic_changeset(state)
    pack = ContextPack.build(changeset, workspace or Workspace(), state, protocol_version=protocol)
    return state, changeset, pack, changeset.units[0]


def change_facts(change):
    return [
        {"kind": "file-change", "change_id": change.id, "field": field, "value": getattr(change, field)}
        for field in ("previous_path", "path")
    ]


def behavior(change, *, updated=False):
    filename = "new" if updated else "old"
    return {
        "before": "The consumer loads the resource at the previous path.",
        "after": "The same bytes exist at the new path; the consumer's referenced path is compared below.",
        "observable": "The resource lookup uses the consumer's literal path.",
        "change_facts": change_facts(change),
        "source_facts": [
            {"side": "context", "path": "app.py", "line": 2, "excerpt": f'load_json("resources/{filename}.json")'}
        ],
    }


def candidate(unit, change):
    source = behavior(change)["source_facts"][0]
    return {
        "unit_id": unit.id,
        "mechanism": "contract-mismatch",
        "severity": "warning",
        "sites": [],
        "claim": "The consumer still loads the old resource path after the verified rename.",
        "trigger": "load_configuration is called through the supported resource loader.",
        "impact": "The resource lookup can fail because it requests the old filename.",
        "open_question": "Does load_json resolve the old filename through an explicit compatibility alias?",
        "refutation": "The loader aliases the old path to the new resource.",
        "discrepancy": "The verified path changed while the consumer's literal path did not.",
        "trigger_basis": source,
        "behavior": behavior(change),
        "violation": {
            "required_behavior": "The consumer's configured resource lookup must return the configuration.",
            "requirement_basis": [source],
            "actual_incompatible_behavior": "The renamed resource is unavailable at the consumer's literal old path.",
            "negative_consequence": "The supported configuration load can fail.",
            "supported_input": "The delivered production consumer calls its configured resource loader.",
            "input_basis": [source],
        },
    }


def clean(unit, change):
    return {
        "unit_id": unit.id,
        "checked": "Compared the updated consumer's literal path with the verified new path.",
        "behavior": behavior(change, updated=True),
    }


def test_structural_plan_has_typed_metadata_and_real_material_without_fake_right_lines():
    state, changeset, pack, unit = inputs()
    block = plan_discovery(
        changeset, pack, state.file_diffs, limits=DiscoveryLimits(), file_changes=state.file_changes
    )[0]
    assert block.structural_changes == {unit.id: state.file_changes[unit.path]}
    assert not block.failure and not block.diff_text and block.change_text
    assert not any(f.side in {"before", "after"} for f in block.facts)
    assert {f.kind for f in block.facts} == {"head", "reference"}
    assert unit.path in changeset.unresolved_files  # Compiler/coverage identity remains unchanged.
    assert block.valid_change_facts(unit.id, change_facts(rename()))


@pytest.mark.asyncio
async def test_stale_consumer_produces_program_bound_structural_candidate_with_empty_sites():
    change = rename()
    assert is_verified_rename(change, HEAD)
    state, changeset, pack, unit = inputs(change)
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [candidate(unit, change)], "no_issue_units": []})])
    ledger = HypothesisLedger("run", HEAD, "digest", protocol_version=3)
    result = await HypothesisGenerator(llm).run(state, pack, changeset, ledger)
    assert result.accepted == 1 and not ledger.unresolved_units
    saved = next(iter(ledger.items.values()))
    assert saved.change_anchor == state.file_changes[unit.path] and saved.sites == []
    assert saved.observations == [] and "file-change:" in saved.identity
    assert "sites must be []" in llm.calls[0][1].content
    assert "Structural changes (typed facts" in llm.calls[0][1].content
    restored = HypothesisLedger.from_dict(ledger.to_dict())
    assert next(iter(restored.items.values())).change_anchor == change


@pytest.mark.asyncio
async def test_updated_consumer_allows_source_backed_structural_check():
    change = rename()
    state, changeset, pack, unit = inputs(change, Workspace(updated=True))
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [clean(unit, change)]})])
    ledger = HypothesisLedger("run", HEAD, "digest", protocol_version=3)
    result = await HypothesisGenerator(llm).run(state, pack, changeset, ledger)
    assert not result.failed_blocks and unit.id in ledger.no_issue_units and not ledger.unresolved_units


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "updates",
    [
        {"patch_state": "absent-unverified"},
        {"head_blob_sha": "d" * 40},
        {"head_sha": "e" * 40},
        {"head_mode": "100755"},
        {"head_type": "tree"},
        {"proof_error": "missing-entry"},
        {"repo": "other/repo"},
        {"head_repo": "other/repo"},
        {"patch_state": "unsupported"},
    ],
)
async def test_unverified_or_misbound_metadata_does_not_enable_structural_discovery(updates):
    state, changeset, pack, unit = inputs(rename(**updates))
    llm = ScriptedLLM()
    ledger = HypothesisLedger("run", HEAD, "digest", protocol_version=3)
    await HypothesisGenerator(llm).run(state, pack, changeset, ledger)
    assert not llm.calls and unit.id in ledger.unresolved_units and not ledger.no_issue_units
    assert not any(s.kind in {"head", "reference"} for s in pack.units[unit.id].slices)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["wrong_change_id", "wrong_path", "missing_old_path", "fake_right_site"])
async def test_model_cannot_rewrite_change_fact_or_fabricate_structural_site(damage):
    change = rename()
    state, changeset, pack, unit = inputs(change)
    raw = candidate(unit, change)
    if damage == "wrong_change_id":
        raw["behavior"]["change_facts"][0]["change_id"] = "fc_wrong"
    elif damage == "wrong_path":
        raw["behavior"]["change_facts"][1]["value"] = "invented/path.json"
    elif damage == "missing_old_path":
        raw["behavior"]["change_facts"] = raw["behavior"]["change_facts"][1:]
    else:
        raw["sites"] = [{"path": change.path, "line": 2, "excerpt": '"feature": "enabled"'}]
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [raw], "no_issue_units": []})])
    ledger = HypothesisLedger("run", HEAD, "digest", protocol_version=3)
    result = await HypothesisGenerator(llm).run(state, pack, changeset, ledger)
    assert result.failed_blocks == 1 and not ledger.items and unit.id in ledger.unresolved_units


@pytest.mark.asyncio
async def test_head_content_or_negative_reference_search_cannot_alone_prove_clean():
    change = rename()
    state, changeset, pack, unit = inputs(change, Workspace(consumer=False))
    assert "reference" in pack.units[unit.id].truncated_kinds
    check = clean(unit, change)
    check["behavior"]["source_facts"] = [
        {"side": "context", "path": change.path, "line": 2, "excerpt": '"feature": "enabled"'}
    ]
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [check]})])
    ledger = HypothesisLedger("run", HEAD, "digest", protocol_version=3)
    await HypothesisGenerator(llm).run(state, pack, changeset, ledger)
    assert unit.id in ledger.unresolved_units and not ledger.no_issue_units


@pytest.mark.asyncio
async def test_missing_material_is_not_a_clean_rename():
    change = rename()
    state, changeset, pack, unit = inputs(change, Workspace(head=False, consumer=False))
    assert {"head", "reference"} <= set(pack.units[unit.id].truncated_kinds)
    raw = candidate(unit, change)
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [raw], "no_issue_units": []})])
    ledger = HypothesisLedger("run", HEAD, "digest", protocol_version=3)
    await HypothesisGenerator(llm).run(state, pack, changeset, ledger)
    assert not ledger.items and unit.id in ledger.unresolved_units
    assert not llm.calls and "missing-structural-material" in ledger.unresolved_units[unit.id]


@pytest.mark.parametrize("protocol", [1, 2])
def test_old_protocols_do_not_receive_structural_head_or_reference_slices(protocol):
    _, _, pack, unit = inputs(protocol=protocol)
    assert not any(s.kind in {"head", "reference"} for s in pack.units[unit.id].slices)


def test_raw_grep_claims_are_not_source_material_and_context_bounds_still_apply():
    class BadGrepWorkspace(Workspace):
        def grep(self, pattern, **kwargs):
            return [{"path": "app.py", "line": 2, "text": 'load_json("resources/old.json")'}]

    workspace = BadGrepWorkspace()
    workspace.files["app.py"] = "def run():\n    unrelated_work()\n"
    state, changeset, pack, unit = inputs(workspace=workspace)
    assert not any(s.kind == "reference" for s in pack.units[unit.id].slices)
    block = plan_discovery(
        changeset, pack, state.file_diffs, limits=DiscoveryLimits(context_max_chars=0), file_changes=state.file_changes
    )[0]
    assert block.structural_changes and not block.facts and "Structural" in block.change_text


@pytest.mark.asyncio
async def test_missing_structural_material_does_not_block_complete_text_change():
    change = rename()
    state, _, _, _ = inputs(change, Workspace(head=False, consumer=False))
    state.files_changed.append("service.py")
    state.file_diffs["service.py"] = text_diff()
    changeset = compile_semantic_changeset(state)
    pack = ContextPack.build(changeset, Workspace(head=False, consumer=False), state, protocol_version=3)
    target = next(unit for unit in changeset.units if unit.path == "service.py")
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [text_clean(target)]})])
    ledger = HypothesisLedger("run", HEAD, "digest", protocol_version=3)
    await HypothesisGenerator(llm, discovery_max_blocks=1).run(state, pack, changeset, ledger)
    assert len(llm.calls) == 1 and target.id in ledger.no_issue_units
    assert next(reason for unit_id, reason in ledger.unresolved_units.items() if unit_id != target.id).endswith(
        "missing-structural-material"
    )


def test_shared_consumer_body_is_delivered_once_with_separate_unit_bindings():
    first = rename(path="resources/new-a.json", previous_path="resources/old-a.json")
    second = rename(path="resources/new-b.json", previous_path="resources/old-b.json")
    workspace = Workspace(consumer=False)
    body = workspace.files.pop("resources/new.json")
    workspace.files.update(
        {
            first.path: body,
            second.path: body,
            "app.py": 'def load():\n    load_json("resources/old-a.json")\n    load_json("resources/old-b.json")\n',
        }
    )
    state = StateStore(
        repo="owner/repo",
        head_repo="owner/repo",
        head_sha=HEAD,
        base_sha=BASE,
        files_changed=[first.path, second.path],
        file_diffs={first.path: "", second.path: ""},
        file_changes={first.path: first, second.path: second},
    )
    changeset = compile_semantic_changeset(state)
    pack = ContextPack.build(changeset, workspace, state, protocol_version=3)
    block = plan_discovery(
        changeset, pack, state.file_diffs, limits=DiscoveryLimits(), file_changes=state.file_changes
    )[0]
    units = {unit.path: unit.id for unit in changeset.units}
    quote = {"side": "context", "path": "app.py", "line": 3, "excerpt": 'load_json("resources/old-b.json")'}
    assert block.context_text.count('load_json("resources/old-b.json")') == 1
    assert block.structural_source_matches(units[second.path], quote, consumer=True)
    assert not block.structural_source_matches(units[first.path], quote, consumer=True)


@pytest.mark.asyncio
async def test_binary_or_missing_head_does_not_turn_a_reference_into_clean_material():
    change = rename()
    workspace = Workspace(updated=True)
    workspace.files[change.path] = "\x00binary-content"
    state, changeset, pack, unit = inputs(change, workspace)
    llm = ScriptedLLM()
    ledger = HypothesisLedger("run", HEAD, "digest", protocol_version=3)
    await HypothesisGenerator(llm).run(state, pack, changeset, ledger)
    assert not llm.calls and ledger.unresolved_units[unit.id].endswith("missing-structural-material")


@pytest.mark.asyncio
async def test_model_change_anchor_echo_cannot_replace_the_program_bound_record():
    change = rename()
    state, changeset, pack, unit = inputs(change)
    raw = candidate(unit, change)
    raw["change_anchor"] = rename(repo="other/repo").to_dict()
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [raw], "no_issue_units": []})])
    ledger = HypothesisLedger("run", HEAD, "digest", protocol_version=3)
    await HypothesisGenerator(llm).run(state, pack, changeset, ledger)
    assert next(iter(ledger.items.values())).change_anchor == change
