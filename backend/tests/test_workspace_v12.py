"""Protocol 12 compact queries and consumer-local immutable evidence reuse."""

from __future__ import annotations

import copy
import json

import pytest
from test_discovery_workspace_v11 import source_observation, worker
from test_investigation_v3 import candidate, source_executor, state

from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.discovery import SourceLine
from reviewforge.engine.discovery_session import extend_sources, restore_observation
from reviewforge.engine.discovery_workflow import save_observations
from reviewforge.engine.discovery_workspace import (
    publish_review_progress,
    remember_evidence,
    workspace_action,
    workspace_tools,
    workspace_view,
)
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.investigation_workspace import run_action, tools
from reviewforge.engine.investigator import Investigator
from reviewforge.engine.review_session import EvidenceStore
from reviewforge.engine.review_workspace import SharedReviewWorkspace


def work(protocol=12, task="consumer", ledger=None):
    book = ledger or HypothesisLedger("run", "head", "digest", protocol_version=protocol)
    review, store = worker(book, task=task)
    review.protocol_version = protocol
    return review, store


@pytest.mark.parametrize("protocol", [11, 12])
def test_default_query_directory_is_versioned_and_explicit_references_preserve_all_558_records(protocol):
    review, store = work(protocol)
    originals = []
    for line in range(1, 559):
        identity = store.source(SourceLine("context", "file0.py", line, f"original source line {line}"))
        originals.append(identity)
    automatic = workspace_view(review)
    assert len(automatic["evidence_directory"]) == 1
    if protocol == 11:
        assert automatic["evidence_catalog"] == []
    else:
        assert "evidence_catalog" not in automatic
    default = workspace_action(review, "query_review_workspace", {"paths": ["file0.py"], "unit_ids": []}, store)
    if protocol == 11:
        assert len(default["evidence_catalog"]) == 558
        return
    assert "evidence_catalog" not in default
    assert default["evidence_directory"][0]["record_count"] == 558
    assert default["evidence_directory"][0]["line_ranges"] == [[1, 558]]
    expanded = workspace_action(
        review,
        "query_review_workspace",
        {"paths": ["file0.py"], "unit_ids": [], "catalog": "references"},
        store,
    )
    assert {row["evidence_id"] for row in expanded["evidence_catalog"]} == set(originals)
    assert len(json.dumps(default)) < len(json.dumps(expanded))
    assert json.dumps(default).count('"path": "file0.py"') == 1
    records = review.workspace.read_evidence(row["evidence_id"] for row in expanded["evidence_catalog"])
    assert {row["excerpt"] for row in records} == {f"original source line {line}" for line in range(1, 559)}


@pytest.mark.parametrize("role", ["discovery", "investigation", "proof", "source_audit"])
def test_directory_never_suggests_an_empty_reference_catalog_and_blind_roles_remain_private(role):
    review, store = work()
    identity = store.observation(source_observation())
    review.workspace.publish_progress(
        task_id="source", actor="discovery", paths=["consumer.py"], summary="PRIVATE_NOTE"
    )
    view = review.workspace.view(paths=["consumer.py"], role=role, catalog="directory")
    assert "evidence_catalog" not in view
    assert view["evidence_directory"][0]["record_count"] == 1
    assert identity not in json.dumps(view["evidence_directory"])
    if role in {"proof", "source_audit"}:
        assert set(view) == {"binding", "role", "evidence_directory"}
        assert "PRIVATE_NOTE" not in json.dumps(view)


def test_query_catalog_schema_is_explicit_only_in_v12_and_unknown_modes_fail_closed():
    old = next(tool for tool in workspace_tools(11) if tool.name == "query_review_workspace")
    new = next(tool for tool in workspace_tools(12) if tool.name == "query_review_workspace")
    assert "catalog" not in old.args_schema.model_fields
    assert new.args_schema.model_fields["catalog"].default == "directory"
    review, store = work()
    with pytest.raises(ValueError):
        workspace_action(review, "query_review_workspace", {"paths": [], "unit_ids": [], "catalog": "all"}, store)


@pytest.mark.parametrize("kind", ["tool", "head", "before", "long"])
def test_discovery_repeated_import_reuses_original_local_observations_and_aliases(kind):
    review, store = work()
    if kind == "tool":
        identity = store.observation(source_observation())
    else:
        identity = store.source(
            SourceLine(
                "before" if kind == "before" else "context",
                "consumer.py",
                7,
                "return stored_value\n" * 150 if kind == "long" else "return stored_value",
            ),
            base_sha="base",
        )
    first = workspace_action(review, "read_shared_evidence", {"evidence_ids": [identity]}, store)["evidence"][0]
    snapshot = (
        len(review.broker._observations),
        len(review.block.facts),
        copy.deepcopy(review.aliases),
        len(store.records),
    )
    second = workspace_action(review, "read_shared_evidence", {"evidence_ids": [identity]}, store)["evidence"][0]
    assert second["reused"] is True
    assert second["observation_ids"] == first["observation_ids"]
    assert second["source_refs"] == first["source_refs"]
    assert snapshot == (len(review.broker._observations), len(review.block.facts), review.aliases, len(store.records))
    assert second["source_refs"]
    if kind == "before":
        assert not review.broker._observations and second["source_reference_only"]
    elif kind == "long":
        assert (
            "".join(observation.excerpt for observation in review.broker._observations) == "return stored_value\n" * 150
        )
        assert review.broker._observations[-1].excerpt_line_range[1] == 156


def test_native_local_evidence_is_reused_without_first_creating_a_duplicate_import():
    review, store = work()
    observation = source_observation(identity="obs_6")
    review.broker._observations.append(observation)
    review.broker._obs_counter = 7
    extend_sources(review.block, observation, review.aliases)
    identity = store.observation(observation)
    remember_evidence(review, identity, observation)
    snapshot = len(review.block.facts), len(review.aliases)
    imported = workspace_action(review, "read_shared_evidence", {"evidence_ids": [identity]}, store)["evidence"][0]
    assert imported["reused"] and imported["observation_ids"] == ["obs_6"] and imported["source_refs"]
    assert len(review.broker._observations) == 1 and (len(review.block.facts), len(review.aliases)) == snapshot
    source = next(iter(review.block.source_refs.values()))
    source_id = store.source(source, base_sha="base")
    imported_source = workspace_action(review, "read_shared_evidence", {"evidence_ids": [source_id]}, store)[
        "evidence"
    ][0]
    assert imported_source["reused"] and imported_source["source_refs"]
    assert len(review.broker._observations) == 1 and (len(review.block.facts), len(review.aliases)) == snapshot


def test_consumer_import_mapping_and_long_source_aliases_survive_real_checkpoint_shape():
    review, store = work()
    text = "long_source_segment_" * 180
    identity = store.source(SourceLine("context", "dependency.py", 9, text))
    first = workspace_action(review, "read_shared_evidence", {"evidence_ids": [identity]}, store)["evidence"][0]
    review.record["source_ref_aliases"] = review.aliases
    save_observations(review)
    store.ledger.discovery_blocks["consumer"] = review.record
    restored = HypothesisLedger.from_dict(json.loads(json.dumps(store.ledger.to_dict(), sort_keys=True)))
    resumed, resumed_store = work(ledger=restored)
    resumed.record = restored.discovery_blocks["consumer"]
    resumed.aliases = resumed.record["source_ref_aliases"]
    resumed.block.source_refs.update({key: SourceLine(**raw) for key, raw in resumed.record["source_registry"].items()})
    resumed.block.facts.extend(resumed.block.source_refs.values())
    resumed.broker._observations = [restore_observation(raw) for raw in resumed.record["observations"]]
    resumed.broker._obs_counter = len(resumed.broker._observations)
    before = len(resumed.broker._observations), len(resumed.block.facts), len(resumed.aliases)
    result = workspace_action(resumed, "read_shared_evidence", {"evidence_ids": [identity]}, resumed_store)["evidence"][
        0
    ]
    assert result["reused"] and result["observation_ids"] == first["observation_ids"]
    assert result["source_refs"] == first["source_refs"]
    assert before == (len(resumed.broker._observations), len(resumed.block.facts), len(resumed.aliases))
    assert "".join(observation.excerpt for observation in resumed.broker._observations) == text


@pytest.mark.parametrize("damage", ["consumer_task_id", "evidence_fingerprint", "observation_ids", "source_refs"])
def test_restored_mapping_tampering_rejects_whole_batch_before_importing_any_other_record(damage):
    review, store = work()
    identity = store.observation(source_observation())
    workspace_action(review, "read_shared_evidence", {"evidence_ids": [identity]}, store)
    mapping = review.record["workspace_imports"][identity]
    mapping[damage] = ["unknown"] if damage in {"observation_ids", "source_refs"} else "other"
    other = store.observation(source_observation(identity="fresh", path="different.py"))
    before = len(review.broker._observations), len(review.block.facts), len(review.aliases)
    with pytest.raises(ValueError):
        workspace_action(review, "read_shared_evidence", {"evidence_ids": [other, identity]}, store)
    assert before == (len(review.broker._observations), len(review.block.facts), len(review.aliases))


def test_progress_hook_uses_controller_dependency_scope_and_keeps_notes_out_of_raw_evidence():
    review, store = work()
    observation = source_observation(identity="obs_0", path="real_dependency.py")
    review.broker._observations.append(observation)
    identity = store.observation(observation)
    note = publish_review_progress(
        review,
        {
            "summary": "Follow the real dependency",
            "open_questions": ["Does it reject missing input?"],
            "resolved_questions": [],
            "next_action": "Read its contract",
            "evidence_ids": [identity],
        },
    )
    assert note["paths"] == ["file0.py", "real_dependency.py"]
    assert note["task_id"] == "consumer" and note["trust"] == "untrusted-navigation-only"
    assert len(store.records) == 1
    with pytest.raises(TypeError):
        publish_review_progress(
            review,
            {
                "task_id": "other-consumer",
                "summary": "untrusted scope",
                "evidence_ids": [],
            },
        )


@pytest.mark.asyncio
async def test_investigation_v12_queries_default_to_directory_and_reuse_native_local_observation():
    book = HypothesisLedger("run", "head", "digest", protocol_version=12)
    store = EvidenceStore(book)
    observation = source_observation(identity="obs_0")
    identity = store.observation(observation)
    worker = Investigator(
        None,
        source_executor([]),
        protocol_version=12,
        shared_workspace=SharedReviewWorkspace(book),
        evidence_store=store,
    )
    worker._prepare_investigation(candidate(), state(), ContextPack(workspace_digest="digest"))
    worker._observations = [observation]
    worker._obs_counter = 1
    query_schema = next(tool for tool in tools(worker) if tool.name == "query_review_workspace").args_schema
    assert query_schema.model_fields["catalog"].default == "directory"
    default = json.loads(await run_action(worker, "query_review_workspace", {"paths": ["consumer.py"], "unit_ids": []}))
    assert "evidence_catalog" not in default["result"] and default["result"]["evidence_directory"]
    expanded = json.loads(
        await run_action(
            worker,
            "query_review_workspace",
            {
                "paths": ["consumer.py"],
                "unit_ids": [],
                "catalog": "references",
            },
        )
    )
    assert expanded["result"]["evidence_catalog"][0]["evidence_id"] == identity
    first = json.loads(await run_action(worker, "read_shared_evidence", {"evidence_ids": [identity]}))
    second = json.loads(await run_action(worker, "read_shared_evidence", {"evidence_ids": [identity]}))
    assert (
        first["result"]["evidence"][0]["observation_id"] == second["result"]["evidence"][0]["observation_id"] == "obs_0"
    )
    assert len(worker._observations) == 1 and worker._obs_counter == 1
