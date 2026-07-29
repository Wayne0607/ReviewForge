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


def test_cross_file_stored_xss_source_and_sink_cluster_on_exact_render_contract() -> None:
    sink = _finding(
        "sink",
        file="app/models/topic_embed.rb",
        line=10,
        category="xss",
        message=(
            "RSS contents reach PostCreator with `cook_method: Post.cook_methods[:raw_html]`, "
            "so untrusted HTML is stored without sanitization."
        ),
        confidence=0.9,
    )
    source = _finding(
        "source",
        file="app/jobs/scheduled/poll_feed.rb",
        line=31,
        category="xss",
        message=(
            "RSS feed content is forwarded to TopicEmbed.import and rendered through "
            "`cook_method: Post.cook_methods[:raw_html]` without sanitization."
        ),
        confidence=0.85,
        reviewer="security_reviewer",
    )

    result = cluster_root_causes([sink, source])

    assert result.kept == (sink,)
    assert result.absorbed == (source,)
    assert result.clusters[0].causal_family == "stored-xss-flow"


def test_unrelated_cross_file_xss_findings_do_not_cluster() -> None:
    first = _finding(
        "first",
        file="feed.rb",
        category="xss",
        message="RSS is rendered using `cook_method: Post.cook_methods[:raw_html]`.",
    )
    second = _finding(
        "second",
        file="profile.rb",
        category="xss",
        message="Profile bio reaches UserHtml.render without sanitization.",
    )

    result = cluster_root_causes([first, second])

    assert result.kept == (first, second)
    assert result.absorbed == ()


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


@pytest.mark.parametrize(
    ("left_category", "left_message", "left_line", "right_category", "right_message", "right_line", "family"),
    [
        (
            "smtp-no-tls",
            "deliver_email uses smtplib.SMTP without starttls, exposing credentials in plaintext",
            44,
            "insecure-transport",
            "deliver_email calls client.login over smtplib.SMTP without TLS",
            53,
            "smtp-plaintext",
        ),
        (
            "ssrf",
            "deliver_webhook does not call _is_internal_host and permits metadata hosts",
            67,
            "ssrf-resource-exhaustion",
            "deliver_webhook accepts internal metadata URLs without host validation",
            63,
            "ssrf-host-validation",
        ),
        (
            "sql-injection",
            "deliver_inbox interpolates user_id into the INSERT statement",
            85,
            "sql_injection",
            "deliver_inbox builds the same INSERT with an f-string",
            87,
            "sql-injection",
        ),
        (
            "crypto",
            "_sign_webhook_payload uses hashlib.md5 instead of HMAC",
            103,
            "wrong-argument-contract",
            "_sign_webhook_payload computes md5(secret + body), not hmac.new",
            108,
            "weak-webhook-signature",
        ),
        (
            "weak-password-hashing",
            "hash_password stores an unsalted MD5 password hash",
            119,
            "weak-authentication",
            "hash_password uses MD5 for password storage",
            127,
            "weak-password-hash",
        ),
        (
            "wrong-callee-name",
            "deliver_inbox calls os.time(), which raises AttributeError",
            93,
            "name-error",
            "os.time does not exist in deliver_inbox",
            95,
            "undefined-symbol",
        ),
    ],
)
def test_security_aliases_with_same_code_identity_cluster(
    left_category: str,
    left_message: str,
    left_line: int,
    right_category: str,
    right_message: str,
    right_line: int,
    family: str,
) -> None:
    left = _finding(
        "left",
        file="backend/src/reviewforge/notify/channels.py",
        category=left_category,
        message=left_message,
        line=left_line,
        reviewer="security_reviewer",
    )
    right = _finding(
        "right",
        file="backend/src/reviewforge/notify/channels.py",
        category=right_category,
        message=right_message,
        line=right_line,
        reviewer="correctness_reviewer",
    )

    result = cluster_root_causes([left, right])

    assert [finding.id for finding in result.kept] == ["left"]
    assert [finding.id for finding in result.absorbed] == ["right"]
    assert result.clusters[0].causal_family == family


def test_same_security_family_with_distinct_code_identity_does_not_cluster() -> None:
    first = _finding(
        "first",
        file="channels.py",
        category="sql-injection",
        message="load_preferences interpolates user_id into a SELECT",
        line=20,
    )
    second = _finding(
        "second",
        file="channels.py",
        category="sql-injection",
        message="save_preferences interpolates email into an INSERT",
        line=80,
    )

    result = cluster_root_causes([first, second])

    assert result.kept == (first, second)
    assert result.absorbed == ()


def test_extended_security_families_can_be_disabled() -> None:
    first = _finding(
        "first",
        category="smtp-no-tls",
        message="deliver_email uses smtplib.SMTP without starttls",
        line=44,
    )
    second = _finding(
        "second",
        category="insecure-smtp",
        message="deliver_email calls client.login without TLS",
        line=53,
    )

    result = cluster_root_causes([first, second], extended_families=False)

    assert result.kept == (first, second)
    assert result.absorbed == ()


def test_md5_password_verification_does_not_merge_with_webhook_signature() -> None:
    webhook = _finding(
        "webhook",
        file="channels.py",
        category="crypto",
        message="_sign_webhook_payload uses md5(secret + body) instead of a webhook HMAC signature",
        line=105,
    )
    password = _finding(
        "password",
        file="channels.py",
        category="wrong-comparison",
        message="verify_password calls hmac.compare_digest on plaintext and an MD5 password hash",
        line=126,
    )

    result = cluster_root_causes([webhook, password])

    assert result.kept == (webhook, password)
    assert result.absorbed == ()


def test_password_verification_aliases_cluster_separately_from_hash_storage() -> None:
    comparison = _finding(
        "comparison",
        file="channels.py",
        category="wrong-comparison",
        message="verify_password compares plaintext password with password_hash using hmac.compare_digest",
        line=126,
    )
    broken_auth = _finding(
        "broken-auth",
        file="channels.py",
        category="broken-auth",
        message="verify_password passes raw password to compare_digest and always returns false",
        line=128,
    )

    result = cluster_root_causes([comparison, broken_auth])

    assert result.kept == (comparison,)
    assert result.absorbed == (broken_auth,)
    assert result.clusters[0].causal_family == "password-verification"


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
    assert events[-1].data["phase"] == "pre-evidence"


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
    assert events[-1].data["phase"] == "pre-evidence"
