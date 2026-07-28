from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from reviewforge.core.events import EventBus, ReviewEvent
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.root_cause import cluster_root_causes


def _finding(
    finding_id: str,
    *,
    category: str,
    message: str,
    line: int = 2,
    file: str = "src/service.ts",
    reviewer: str = "correctness_reviewer",
    confidence: float = 0.9,
    verified_by: str = "",
) -> Finding:
    return Finding(
        id=finding_id,
        file=file,
        line=line,
        severity="warning",
        category=category,
        message=message,
        confidence=confidence,
        reviewer=reviewer,
        verified_by=verified_by,
    )


def _patch(*lines: str) -> str:
    count = len(lines)
    return f"@@ -1,{count} +1,{count} @@\n" + "\n".join(f" {line}" for line in lines)


@pytest.mark.parametrize(
    ("left_category", "right_category", "message", "code"),
    [
        (
            "wrong-metric-recorder",
            "wrong-metric-recorder-and-label",
            "recordStorageDuration passes options.Kind to the wrong metric label",
            "d.recordStorageDuration(false, mode, options.Kind, method, start)",
        ),
        (
            "context-loss",
            "log-field-name",
            "klog.NewContext(ctx, d.Log) discards the enriched logger",
            "ctx = klog.NewContext(ctx, d.Log)",
        ),
        (
            "missing-action",
            "missing-side-effect",
            "@embedding is saved without calling invalidateEmbedding",
            "await invalidateEmbedding(@embedding)",
        ),
        (
            "wrong-boolean-logic",
            "wrong-permission-check",
            "isTeamAdminOrOwner uses && instead of || for the authorization check",
            "if (isTeamAdminOrOwner(user) && canWrite(user)) {",
        ),
    ],
)
def test_alias_categories_with_same_code_identity_cluster(
    left_category: str,
    right_category: str,
    message: str,
    code: str,
) -> None:
    left = _finding("left", category=left_category, message=message, reviewer="security_reviewer")
    right = _finding(
        "right",
        category=right_category,
        message=f"{message}; the same root cause is reported again",
        reviewer="correctness_reviewer",
    )

    result = cluster_root_causes([left, right], file_diffs={"src/service.ts": _patch("const before = 1", code)})

    assert [finding.id for finding in result.kept] == ["left"]
    assert [finding.id for finding in result.absorbed] == ["right"]
    assert result.absorbed_to_representative == (("right", "left"),)
    assert result.stats["cross_reviewer_merged"] == 1


def test_category_and_proximity_without_concrete_identity_do_not_cluster() -> None:
    left = _finding("left", category="missing-action", message="invalidateCache is never invoked", line=2)
    right = _finding("right", category="missing-side-effect", message="sendNotification is never invoked", line=3)

    result = cluster_root_causes([left, right])

    assert result.kept == (left, right)
    assert result.absorbed == ()


def test_same_identifier_in_different_files_does_not_cluster() -> None:
    left = _finding("left", category="context-loss", message="klog.NewContext loses fields", file="a.go")
    right = _finding("right", category="lost-logger", message="klog.NewContext loses fields", file="b.go")

    result = cluster_root_causes([left, right])

    assert result.kept == (left, right)


def test_distinct_calls_of_same_function_do_not_cluster() -> None:
    left = _finding("left", category="missing-action", message="save(user) omits auditUser", line=1)
    right = _finding("right", category="missing-side-effect", message="save(order) omits auditOrder", line=2)
    patch = _patch("save(user)", "save(order)")

    result = cluster_root_causes([left, right], file_diffs={"src/service.ts": patch})

    assert result.kept == (left, right)


def test_auth_compound_variable_and_underlying_checks_share_one_root() -> None:
    findings = [
        _finding(
            "roles",
            category="auth-logic",
            message="isTeamAdmin && isTeamOwner rejects users with either valid role",
            line=34,
        ),
        _finding(
            "condition",
            category="wrong-logic",
            message="isTeamAdmin(user) && isTeamOwner(user) should use ||",
            line=39,
        ),
        _finding(
            "compound",
            category="wrong-boolean-logic",
            message="isTeamAdminOrOwner contains && rather than ||",
            line=45,
        ),
        _finding(
            "permission",
            category="wrong-permission-check",
            message="isTeamAdminOrOwner uses && and rejects admin or owner",
            line=49,
        ),
    ]

    result = cluster_root_causes(findings)

    assert len(result.kept) == 1
    assert len(result.absorbed) == 3
    assert result.clusters[0].causal_family == "auth-logic"


def test_detector_wins_over_llm_duplicate() -> None:
    llm = _finding(
        "llm",
        category="undefined-variable",
        message="missingConfig is undefined",
        confidence=0.99,
    )
    detector = _finding(
        "detector",
        category="undefined-symbol",
        message="missingConfig is undefined",
        confidence=0.7,
        reviewer="quality_reviewer",
        verified_by="detector",
    )

    result = cluster_root_causes([llm, detector])

    assert result.kept == (detector,)
    assert result.absorbed == (llm,)


def test_independent_detector_findings_never_cluster() -> None:
    left = _finding(
        "left",
        category="undefined-symbol",
        message="missingConfig is undefined",
        verified_by="detector",
    )
    right = _finding(
        "right",
        category="undefined-variable",
        message="missingConfig is undefined",
        verified_by="detector",
    )

    result = cluster_root_causes([left, right])

    assert result.kept == (left, right)


def test_equal_quality_uses_stable_input_order() -> None:
    first = _finding("first", category="context-loss", message="klog.NewContext loses fields")
    second = _finding("second", category="lost-logger", message="klog.NewContext loses fields")

    result = cluster_root_causes([first, second])

    assert result.kept == (first,)


def test_result_ir_is_frozen() -> None:
    finding = _finding("only", category="context-loss", message="klog.NewContext loses fields")
    result = cluster_root_causes([finding])

    with pytest.raises(FrozenInstanceError):
        result.input_count = 2  # type: ignore[misc]


def _orchestrator_with_events(events: list[ReviewEvent]) -> Orchestrator:
    orchestrator = object.__new__(Orchestrator)
    event_bus = EventBus()
    event_bus.subscribe(events.append)
    orchestrator._events = event_bus
    return orchestrator


def test_orchestrator_marks_absorbed_finding_and_emits_stats() -> None:
    events: list[ReviewEvent] = []
    orchestrator = _orchestrator_with_events(events)
    left = _finding("left", category="context-loss", message="klog.NewContext loses fields")
    right = _finding("right", category="lost-logger", message="klog.NewContext loses fields")
    state = StateStore()
    state.add_finding(left)
    state.add_finding(right)

    kept = orchestrator._apply_root_cause_clustering([left, right], state)

    assert kept == [left]
    absorbed = state.get_finding("right")
    assert absorbed.status == "false_positive"
    assert absorbed.verified_by == "root-cause-cluster"
    assert "representative left" in absorbed.verify_reason
    assert events[-1].event_type == "root_cause_cluster.completed"
    assert events[-1].data["absorbed"] == 1


def test_orchestrator_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[ReviewEvent] = []
    orchestrator = _orchestrator_with_events(events)
    finding = _finding("only", category="context-loss", message="klog.NewContext loses fields")
    state = StateStore()
    state.add_finding(finding)

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr("reviewforge.engine.orchestrator.cluster_root_causes", _explode)
    kept = orchestrator._apply_root_cause_clustering([finding], state)

    assert kept == [finding]
    assert state.get_finding("only").status == "candidate"
    assert events[-1].event_type == "root_cause_cluster.failed"
