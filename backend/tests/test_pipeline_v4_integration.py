from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from reviewforge.core.config import PipelineV4Config
from reviewforge.core.events import EventBus
from reviewforge.core.state import StateStore
from reviewforge.engine.detectors.base import DetectorFinding
from reviewforge.engine.editor import Publication, PublicationComment
from reviewforge.engine.hypothesis import HypothesisLedger, HypothesisStatus
from reviewforge.engine.pipeline_v4 import _seed_detector_hypotheses, deliver_publication, run_hypothesis_pipeline
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, UnitKind
from reviewforge.tools.workspace import WorkspaceInfo


def _diff(*lines: str) -> str:
    body = "\n".join(f"+{line}" for line in lines)
    return f"diff --git a/app.py b/app.py\n--- /dev/null\n+++ b/app.py\n@@ -0,0 +1,{len(lines)} @@\n{body}\n"


def _unit(path: str = "app.py", symbol: str = "f") -> SemanticUnit:
    return SemanticUnit(
        id=f"su_test_{symbol}",
        path=path,
        language="python",
        kind=UnitKind.SYMBOL,
        symbol=symbol,
        start_line=1,
        end_line=2,
        added_lines=[1, 2],
    )


def test_seed_detector_hypotheses_maps_security_and_a11y() -> None:
    state = StateStore(
        repo="o/r", pr_number=1, head_sha="abc", file_diffs={"app.py": _diff("def f()", "    return x.name")}
    )
    changeset = SemanticChangeSet(repo="o/r", pr_number=1, head_sha="abc", units=[_unit()])
    ledger = HypothesisLedger("run", "abc", "digest")
    findings = [
        DetectorFinding(
            file="app.py",
            line=2,
            severity="high",
            category="command-injection",
            message="untrusted input reaches shell",
            suggestion="validate input",
            confidence=0.95,
        ),
        DetectorFinding(
            file="app.py",
            line=2,
            severity="medium",
            category="code-quality",
            message="weak variable name",
            suggestion="rename",
            confidence=0.9,
        ),
    ]

    seeded = _seed_detector_hypotheses(state, changeset, ledger, findings)

    assert seeded == 1  # code-quality has no Mechanism mapping, so it is skipped
    hypothesis = next(iter(ledger.items.values()))
    assert hypothesis.status == HypothesisStatus.CONFIRMED
    assert hypothesis.evidence_strength == "strong"
    assert hypothesis.source == "detector:command-injection"
    assert hypothesis.mechanism.value == "security-sink"
    assert len(hypothesis.sites) == 1


class _ScriptedLLM(BaseChatModel):
    responses: list = Field(default_factory=list)
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        content = self.responses.pop(0) if self.responses else "{}"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    @property
    def _llm_type(self):
        return "scripted"

    @property
    def _identifying_params(self):
        return {}


@pytest.mark.asyncio
async def test_llm_stages_wire_generator_and_investigator(tmp_path) -> None:
    diff = _diff("def f(x):", "    return x.name")
    state = StateStore(
        repo="owner/repo",
        pr_number=1,
        head_sha="abc",
        files_changed=[],
        file_diffs={"app.py": diff},
        impact_manifest={
            "version": 1,
            "files": [
                {
                    "path": "app.py",
                    "changed_symbols": [
                        {"name": "f", "type": "function", "start_line": 1, "end_line": 2, "added_lines": [1, 2]}
                    ],
                }
            ],
        },
    )
    workspace = SimpleNamespace(
        info=WorkspaceInfo(
            repo="owner/repo",
            head_repo="owner/repo",
            head_sha="abc",
            root=tmp_path,
            file_count=1,
            byte_size=10,
            digest="d",
            truncated=False,
            source="api-fallback",
        ),
        source="api-fallback",
        digest="d",
    )
    events = EventBus()
    seen: list = []
    events.subscribe(seen.append)
    events.set_run_id("run")

    generator_llm = _ScriptedLLM(
        responses=[
            json.dumps(
                {
                    "hypotheses": [
                        {
                            "unit_id": "su_test_f",
                            "mechanism": "null-path",
                            "anchor_symbol": "f",
                            "claim": "f dereferences x without a null check",
                            "trigger": "x is None",
                            "impact": "AttributeError",
                            "open_question": "can x be None here?",
                            "refutation": "if x is validated upstream it is fine",
                            "severity": "warning",
                            "sites": [{"path": "app.py", "line": 2, "excerpt": "return x.name"}],
                        }
                    ],
                    "no_issue_units": [],
                }
            )
        ]
    )
    investigator_llm = _ScriptedLLM(
        responses=[
            json.dumps(
                {
                    "verdict": "unknown",
                    "answer": "",
                    "evidence_ids": [],
                    "evidence_quote": "",
                    "severity": "warning",
                    "additional_sites": [],
                    "reason": "could not confirm within budget",
                }
            )
        ]
    )

    def get_llm(name: str):
        return {"hypothesis_generator": generator_llm, "investigator": investigator_llm}.get(
            name, _ScriptedLLM(responses=["{}"])
        )

    fake = SimpleNamespace(
        _gateway=SimpleNamespace(workspace_for=AsyncMock(return_value=workspace)),
        _events=events,
        _pipeline_v4_config=PipelineV4Config(),
        _model_router=SimpleNamespace(get_llm=get_llm),
        _db=None,
    )

    await run_hypothesis_pipeline(fake, state)

    event_types = {event.event_type for event in seen}
    assert "hypothesis.generated" in event_types
    assert "lens.selected" in event_types
    assert "investigation.completed" in event_types
    assert "editor.completed" in event_types
    assert "pipeline_v4.completed" in event_types

    generated = next(event for event in seen if event.event_type == "hypothesis.generated")
    assert generated.data["accepted"] == 1
    assert generated.data["source"] == "generator"

    hypothesis = state.ledger.items["su_test_f::null-path::f"]
    assert hypothesis.status == HypothesisStatus.UNKNOWN
    assert hypothesis.attempts == 1


@pytest.mark.asyncio
async def test_deliver_publication_validates_right_side_coordinates() -> None:
    diff = _diff("def f(x):", "    return x.name")
    state = StateStore(repo="o/r", pr_number=1, head_sha="abc", file_diffs={"app.py": diff})
    publication = Publication(
        comments=[
            PublicationComment(hypothesis_ids=["h_1"], path="app.py", line=2, title="t", body="visible"),
            PublicationComment(hypothesis_ids=["h_2"], path="app.py", line=999, title="t", body="off-diff"),
            PublicationComment(hypothesis_ids=["h_3"], path="missing.py", line=1, title="t", body="no-patch"),
        ],
        summary_items=[],
        merged=[],
    )
    calls: list = []

    async def invoke(name, params, state_, agent_name=""):
        calls.append((name, params))
        return {"ok": True}

    gateway = SimpleNamespace(invoke=invoke)

    delivered, rejected = await deliver_publication(gateway, state, publication)

    assert delivered == 1
    assert rejected == 2
    assert calls[0][0] == "post_review"
    assert [comment["file_path"] for comment in calls[0][1]["comments"]] == ["app.py"]
    assert calls[0][1]["comments"][0]["line"] == 2
