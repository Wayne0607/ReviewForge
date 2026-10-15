"""Replay exact saved records for admission/repair classification, not quality."""

from __future__ import annotations

import copy
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from reviewforge.engine.discovery import DiscoveryBlock, SourceLine
from reviewforge.engine.discovery_repair import pending_repairs, remember, repair_request
from reviewforge.engine.discovery_workflow import Work
from reviewforge.engine.discovery_workflow_tools import assessment_tools
from reviewforge.engine.review_session import ReviewSession
from reviewforge.engine.semantic_diff import SemanticUnit


def work_from_receipt(case):
    data = case["unit"]
    unit = SemanticUnit(id=data["unit_id"], path=data["path"], symbol=data["symbol"])
    block = DiscoveryBlock("saved-receipt", [unit], "", "", "", "", "")
    required = {ref for side in data["required_source_sides"] for ref in side}
    for ref, text in case["rows"]:
        file_id, coord = ref.split(".", 1)
        side = {"B": "before", "R": "after", "C": "context"}[coord[0]]
        line = int(coord[1:].split(".")[0])
        block.source_refs[ref] = SourceLine(side, case["files"][file_id], line, text, changed=ref in required)
    block.facts = list(block.source_refs.values())
    block.unit_facts[unit.id] = [block.source_refs[ref] for ref in required]
    record = {
        "head_sha": "92f44dcea7ff19e9123a30c63c167a2938df5a55",
        "workspace_digest": "frozen-failure-fixture",
        "units": {},
        "workflow": {"policy": "fair-assessments-v2"},
    }
    session = ReviewSession(record, task_id="fixture", phase="discovery", max_turns=4)
    return Work(block, record, SimpleNamespace(), {ref: ref for ref in block.source_refs}, session, 0, 7)


def test_exact_twelve_failed_records_are_classified_without_editing_their_meaning():
    fixture = json.loads((Path(__file__).parent / "fixtures/v6_workflow_rejected_records.json").read_text("utf-8"))
    counts = Counter()
    for case in fixture["cases"]:
        original = copy.deepcopy(case["original_record"])
        work = work_from_receipt(case)
        _, old = assessment_tools(work.block, protocol_version=6)
        _, new = assessment_tools(work.block, protocol_version=7)
        with pytest.raises(ValidationError):
            old[original["kind"]].model_validate(original)
        try:
            new[original["kind"]].model_validate(original)
        except ValidationError as exc:
            fields = [{"field": list(error["loc"]), "type": error["type"]} for error in exc.errors(include_input=False)]
            remember(work, original, fields, new)
            if pending_repairs(work):
                counts["narrow_repair"] += 1
                _, chat = repair_request(work, new, {"phase": "field-repair"})
                material = json.loads(chat[1].content)["repair_records"][0]
                assert material["rejected_record_untrusted"] == original
                assert all(ref in original["source_refs"] for ref, _ in material["source_rows"])
            else:
                counts["full_source_required"] += 1
        else:
            counts["duplicate_prose_no_longer_required"] += 1
        assert original == case["original_record"]
    assert counts == {"duplicate_prose_no_longer_required": 1, "narrow_repair": 10, "full_source_required": 1}
