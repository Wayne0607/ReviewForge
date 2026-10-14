"""Discovery locations are leads, with coverage and proof checked separately."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from test_discovery_submission import call, fixture, hypothesis

from reviewforge.core.state import StateStore
from reviewforge.engine.discovery import DiscoveryBlock, SourceLine
from reviewforge.engine.discovery_aliases import (
    expand_response_refs,
    render_source_aliases,
    source_alias_legend,
    source_aliases,
)
from reviewforge.engine.discovery_submission import discovery_submission_tools, parse_discovery_submission
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import (
    HypothesisGenerationResult,
    HypothesisGenerator,
    _right_lines_by_path,
)
from reviewforge.engine.semantic_diff import SemanticUnit


def test_scoped_aliases_preserve_file_side_line_segments_and_source_bodies():
    block = DiscoveryBlock("block", [], "", "", "", "", "")
    block.facts = [
        SourceLine("before", "a.py", 47, "old_value"),
        SourceLine("after", "a.py", 47, "日本語 " + "a" * 230 + "b" * 230 + "c" * 230),
        SourceLine("context", "a.py", 47, "old_value"),
        SourceLine("after", "b.py", 47, "old_value"),
    ]
    block.register_source_refs()
    original = copy.deepcopy(block)
    aliases = source_aliases(block.source_refs, scoped=True)
    assert aliases == source_aliases(dict(reversed(list(block.source_refs.items()))), scoped=True)
    assert len(aliases) == len(set(aliases.values())) == len(block.source_refs)
    assert {"f0.B47", "f0.C47", "f1.R47"} <= set(aliases)
    assert len([alias for alias in aliases if alias.startswith("f0.R47.s")]) == 3
    assert source_alias_legend(block.source_refs) == "f0 = a.py\nf1 = b.py"
    reference = aliases["f0.B47"]
    source = f'+47 [{reference}] | print("{reference}", "日本語")'
    assert render_source_aliases(source, aliases) == f'+47 [f0.B47] | print("{reference}", "日本語")'
    assert block == original


def test_scoped_native_schema_selects_one_real_primary_site_and_keeps_context_out():
    _, _, block, aliases = fixture(2, scoped=True)
    raw = hypothesis(block, aliases)
    schema = discovery_submission_tools(block, aliases)[0].args_schema
    schema.model_validate(raw)
    sites = schema.model_json_schema()["properties"]["site_refs"]
    assert sites["minItems"] == sites["maxItems"] == 1
    assert all(".R" in ref for ref in sites["items"]["enum"])
    before = next(alias for alias in aliases if ".B" in alias)
    for invalid in ([before], [raw["site_refs"][0]] * 2, [], ["f999.R47"]):
        with pytest.raises(ValidationError):
            schema.model_validate(raw | {"site_refs": invalid})
    # A real other-target RIGHT location is still rejected by unit ownership.
    foreign = hypothesis(block, aliases, block.units[1])["site_refs"]
    framed, errors = parse_discovery_submission(
        AIMessage(content="", tool_calls=[call("submit_hypothesis", raw | {"site_refs": foreign})]),
        discovery_submission_tools(block, aliases),
        block,
        aliases,
    )
    assert errors and not framed.complete and not framed.payload["hypotheses"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_ref", ["before", "other-file", "unknown"])
async def test_complete_legacy_lead_keeps_only_valid_sites_and_never_establishes_complete_coverage(bad_ref):
    state, _, block, aliases = fixture(2)
    raw = hypothesis(block, aliases)
    rejected = {
        "before": next(alias for alias, ref in aliases.items() if block.source_refs[ref].side == "before"),
        "other-file": hypothesis(block, aliases, block.units[1])["site_refs"][0],
        "unknown": "r999",
    }[bad_ref]
    raw["site_refs"].append(rejected)
    response = AIMessage(content="", tool_calls=[call("submit_hypothesis", raw)])
    original = response.model_dump()
    framed, errors = parse_discovery_submission(response, discovery_submission_tools(block, aliases), block, aliases)
    assert errors == ["rejected-auxiliary-discovery-sites"] and not framed.complete
    assert framed.rejected_sites == (
        {"unit_id": raw["unit_id"], "source_ref": rejected, "reason": "not-target-right-site"},
    )
    assert len(framed.payload["hypotheses"][0]["sites"]) == 1
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=4)
    result = HypothesisGenerationResult()
    await HypothesisGenerator(None)._consume_discovery(
        expand_response_refs(framed.payload, aliases),
        block,
        ledger,
        result,
        _right_lines_by_path(state),
        12,
        {"units": {}, "output_framing": {"complete": framed.complete}},
    )
    assert result.accepted == 1
    assert next(iter(ledger.items.values())).status.value == "open"
    assert set(ledger.unresolved_units) == {unit.id for unit in block.units}
    assert not ledger.no_issue_units and not ledger.publication_reviews
    assert response.model_dump() == original


@pytest.mark.asyncio
async def test_salvaged_optional_location_cannot_repair_mandatory_behavior_sources():
    state, _, block, aliases = fixture(2)
    raw = hypothesis(block, aliases)
    raw["site_refs"].append("r999")
    # Existing references from another file remain invalid behavior evidence.
    raw["source_refs"][:2] = hypothesis(block, aliases, block.units[1])["source_refs"][:2]
    framed, _ = parse_discovery_submission(
        AIMessage(content="", tool_calls=[call("submit_hypothesis", raw)]),
        discovery_submission_tools(block, aliases),
        block,
        aliases,
    )
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=4)
    result = HypothesisGenerationResult()
    await HypothesisGenerator(None)._consume_discovery(
        expand_response_refs(framed.payload, aliases),
        block,
        ledger,
        result,
        _right_lines_by_path(state),
        12,
        {"units": {}, "output_framing": {"complete": framed.complete}},
    )
    assert not ledger.items and not ledger.no_issue_units
    assert result.accepted == 0 and ledger.unresolved_units


def test_wrong_required_reference_is_not_salvaged_or_remapped():
    _, _, block, aliases = fixture(2)
    raw = hypothesis(block, aliases)
    raw["source_refs"][0] = "r999"
    raw["site_refs"].append("r999")
    framed, errors = parse_discovery_submission(
        AIMessage(content="", tool_calls=[call("submit_hypothesis", raw)]),
        discovery_submission_tools(block, aliases),
        block,
        aliases,
    )
    assert errors and not framed.complete and not framed.payload["hypotheses"]
    assert not framed.rejected_sites


def test_new_delivery_explicitly_rejects_opaque_aliases_instead_of_guessing_a_migration():
    _, _, block, old_aliases = fixture(1)
    new_aliases = source_aliases(block.source_refs, scoped=True)
    raw = hypothesis(block, old_aliases)
    framed, errors = parse_discovery_submission(
        AIMessage(content="", tool_calls=[call("submit_hypothesis", raw)]),
        discovery_submission_tools(block, new_aliases),
        block,
        new_aliases,
    )
    assert errors and not framed.payload["hypotheses"]
    assert json.dumps(raw) != json.dumps(framed.payload)


@pytest.mark.asyncio
async def test_calcom_saved_complete_native_calls_keep_two_open_leads_with_explicit_rejections():
    """Replay the actual failed receipt, not an improved or re-asked response."""
    data = json.loads((Path(__file__).parent / "fixtures/v4_calcom_14740_native_sites.json").read_text("utf-8"))
    assert data["origin"]["output_sha256"] == "7c2ff0ac05732ab432b27bc7d2e9a75c075509e7e4ae7bd17a170075b53c65e3"
    original = copy.deepcopy(data)
    info = data["unit"]
    unit = SemanticUnit(**{key: info[key] for key in ("id", "path", "symbol", "start_line", "end_line")})
    block = DiscoveryBlock(data["origin"]["block_id"], [unit], "", "", "", "", "")
    aliases = {}
    for alias, raw in data["source_refs"].items():
        source = SourceLine(**{key: value for key, value in raw.items() if key != "canonical"})
        identity = json.dumps([block.id, source.side, source.path, source.line, source.text], ensure_ascii=False)
        assert "sr_" + hashlib.sha256(identity.encode()).hexdigest()[:12] == raw["canonical"]
        block.facts.append(source)
        block.source_refs[raw["canonical"]] = source
        aliases[alias] = raw["canonical"]
    block.unit_facts[unit.id] = [
        source for source in block.facts if source.path == unit.path and source.side == "after"
    ]
    block.unit_before_states[unit.id] = info["before_state"]
    response = AIMessage(
        content=data["response"]["content"],
        tool_calls=data["response"]["tool_calls"],
        response_metadata={"finish_reason": data["response"]["finish_reason"]},
    )
    framed, errors = parse_discovery_submission(response, discovery_submission_tools(block, aliases), block, aliases)
    assert errors == ["rejected-auxiliary-discovery-sites"] and not framed.complete
    assert {item["source_ref"] for item in framed.rejected_sites} == {"r54", "r168"}
    state = StateStore(head_sha=data["origin"]["head_sha"], file_diffs={unit.path: data["patch"]})
    ledger = HypothesisLedger("offline-replay", state.head_sha, "source-subset", protocol_version=4)
    result = HypothesisGenerationResult()
    await HypothesisGenerator(None)._consume_discovery(
        expand_response_refs(framed.payload, aliases),
        block,
        ledger,
        result,
        _right_lines_by_path(state),
        12,
        {"units": {}, "output_framing": {"complete": framed.complete}},
    )
    assert result.accepted == len(ledger.items) == 2
    assert all(item.status.value == "open" for item in ledger.items.values())
    assert set(ledger.unresolved_units) == {unit.id}
    assert not ledger.no_issue_units and not ledger.publication_reviews
    assert [item.claim for item in ledger.items.values()] == [
        call["args"]["claim"] for call in data["response"]["tool_calls"]
    ]
    assert data == original
