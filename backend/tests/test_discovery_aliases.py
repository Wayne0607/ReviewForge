import copy
import json

import pytest

from reviewforge.engine.discovery_aliases import expand_response_refs, render_source_aliases, source_aliases


def test_aliases_are_stable_bijection_and_source_text_is_never_rewritten():
    refs = {"sr_bbbbbbbbbbbb": {"side": "context"}, "sr_aaaaaaaaaaaa": {"side": "after"}}
    saved = copy.deepcopy(refs)
    aliases = source_aliases(refs)
    assert aliases == {"r0": "sr_aaaaaaaaaaaa", "r1": "sr_bbbbbbbbbbbb"}
    raw = '+12 [sr_aaaaaaaaaaaa:1-50] | print("sr_aaaaaaaaaaaa")\n 13 [sr_bbbbbbbbbbbb] | 日本語'
    assert render_source_aliases(raw, aliases) == '+12 [r0:1-50] | print("sr_aaaaaaaaaaaa")\n 13 [r1] | 日本語'
    assert refs == saved


def test_before_after_and_context_aliases_keep_separate_source_identity():
    aliases = {"r0": "sr_aaaaaaaaaaaa", "r1": "sr_bbbbbbbbbbbb", "r2": "sr_cccccccccccc"}
    raw = " before= 1 [sr_aaaaaaaaaaaa] RIGHT= 1 [sr_bbbbbbbbbbbb] | return value\n 1 [sr_cccccccccccc] | return value"
    assert render_source_aliases(raw, aliases) == " before= 1 [r0] RIGHT= 1 [r1] | return value\n 1 [r2] | return value"
    payload = {
        "hypotheses": [
            {
                "unit_id": "u",
                "claim": "r0 sr_aaaaaaaaaaaa",
                "sites": [{"source_ref": "r2"}],
                "behavior": {"source_facts": ["r0", {"source_ref": "r1"}]},
                "trigger_basis": {"source_ref": "r1"},
            }
        ],
        "no_issue_units": [{"behavior": {"source_facts": [{"source_ref": "r0", "side": "after"}]}}],
    }
    original = copy.deepcopy(payload)
    expanded = expand_response_refs(payload, aliases)
    item = expanded["hypotheses"][0]
    assert item["sites"] == [{"source_ref": "sr_cccccccccccc"}]
    assert item["claim"] == "r0 sr_aaaaaaaaaaaa"
    assert item["behavior"]["source_facts"] == ["sr_aaaaaaaaaaaa", {"source_ref": "sr_bbbbbbbbbbbb"}]
    # Conflicting overrides remain invalid for the existing strict resolver.
    assert expanded["no_issue_units"][0]["behavior"]["source_facts"][0]["side"] == "after"
    assert payload == original


def test_unknown_aliases_and_malformed_fields_remain_invalid():
    payload = {
        "hypotheses": [
            {"sites": [None, {"source_ref": "r999"}], "trigger_basis": [], "behavior": {"source_facts": "r0"}}
        ],
        "no_issue_units": None,
    }
    assert expand_response_refs(payload, {"r0": "sr_aaaaaaaaaaaa"}) == payload


def test_useful_output_floor_tracks_v4_lead_schema_without_changing_v3_or_output_cap():
    from reviewforge.engine.hypothesis_generator import _discovery_output_floor

    assert _discovery_output_floor(12, 6) == 7168
    assert _discovery_output_floor(12, 6, protocol_version=4) == 4864
    assert _discovery_output_floor(1, 6, protocol_version=4) == 832
    assert _discovery_output_floor(100, 100, protocol_version=4) == 8192


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_site", [False, True])
async def test_v4_generator_decodes_aliases_before_exact_site_validation(bad_site):
    from test_discovery_v3 import ScriptedLLM, candidate, changeset, diff, unit

    from reviewforge.core.state import StateStore
    from reviewforge.engine.context_pack import ContextPack
    from reviewforge.engine.discovery import DiscoveryLimits, plan_discovery
    from reviewforge.engine.hypothesis import HypothesisLedger
    from reviewforge.engine.hypothesis_generator import HypothesisGenerator

    u = unit("app.py")
    pack = ContextPack()
    block = plan_discovery(changeset(u), pack, {u.path: diff()}, limits=DiscoveryLimits())[0]
    aliases = source_aliases(block.source_refs)
    before = next(alias for alias, ref in aliases.items() if block.source_refs[ref].side == "before")
    after = next(alias for alias, ref in aliases.items() if block.source_refs[ref].side == "after")
    raw = candidate(u)
    raw.pop("violation")
    raw["sites"] = [{"source_ref": before if bad_site else after}]
    raw["trigger_basis"] = {"source_ref": before}
    raw["behavior"]["source_facts"] = [{"source_ref": before}, {"source_ref": after}]
    model = ScriptedLLM(responses=[json.dumps({"hypotheses": [raw]})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=4)
    result = await HypothesisGenerator(model).run(StateStore(file_diffs={u.path: diff()}), pack, changeset(u), ledger)
    assert result.accepted == (0 if bad_site else 1)
    assert bool(ledger.unresolved_units) == bad_site
    assert next(iter(ledger.discovery_blocks.values()))["source_ref_aliases"] == aliases
    assert f"[{after}]" in model.calls[0][-1].content
    if not bad_site:
        item = next(iter(ledger.items.values()))
        assert item.sites[0].excerpt == "return new_value"
        assert item.discovery_basis["behavior"]["source_facts"][0]["side"] == "before"
