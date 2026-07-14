from reviewforge.core.state import Finding
from reviewforge.engine.verifier import Verifier


def _finding(
    finding_id: str,
    *,
    file: str = "app.py",
    line: int,
    category: str,
    message: str,
    confidence: float = 0.9,
    reviewer: str = "security_reviewer",
    verified_by: str = "",
) -> Finding:
    return Finding(
        id=finding_id,
        file=file,
        line=line,
        category=category,
        message=message,
        confidence=confidence,
        reviewer=reviewer,
        verified_by=verified_by,
    )


def test_exact_alias_duplicate_is_canonicalized_and_detector_wins():
    llm = _finding(
        "llm",
        line=12,
        category="malicious-dependency",
        message="A malicious dependency is installed.",
        confidence=0.99,
        reviewer="security_reviewer",
    )
    detector = _finding(
        "detector",
        line=12,
        category="supply-chain-risk",
        message="A malicious dependency is installed.",
        confidence=0.91,
        reviewer="dependency_reviewer",
        verified_by="detector",
    )

    survivors, dropped = Verifier().verify([llm, detector])

    assert [finding.id for finding in survivors] == ["detector"]
    assert survivors[0].category == "supply-chain-risk"
    assert survivors[0].verified_by == "detector"
    assert survivors[0].reviewer == "dependency_reviewer,security_reviewer"
    assert dropped == ["llm"]


def test_nearby_detector_and_llm_same_sink_are_merged():
    detector = _finding(
        "detector",
        file="workflow.yml",
        line=15,
        category="supply-chain-risk",
        message="Workflow executes remote script via piped shell.",
        verified_by="detector",
    )
    llm = _finding(
        "llm",
        file="workflow.yml",
        line=12,
        category="insecure-download",
        message="An external URL download is executed by the shell.",
        confidence=0.97,
    )

    survivors, dropped = Verifier().verify([llm, detector])

    assert [finding.id for finding in survivors] == ["detector"]
    assert dropped == ["llm"]


def test_insecure_download_keeps_tls_semantics_outside_matching_workflow_sink():
    tls = _finding(
        "tls",
        line=4,
        category="insecure-download",
        message="requests.get disables TLS certificate verification.",
    )

    survivors, dropped = Verifier().verify([tls])

    assert survivors[0].category == "insecure-download"
    assert dropped == []


def test_workflow_category_drift_merges_only_with_shared_specific_sink():
    title_detector = _finding(
        "title-detector",
        file=".github/workflows/review.yml",
        line=18,
        category="ci-security",
        message="Pull-request title is interpolated directly into a workflow shell command.",
        verified_by="detector",
    )
    title_llm = _finding(
        "title-llm",
        file=".github/workflows/review.yml",
        line=16,
        category="command-injection",
        message="The PR title reaches a shell command without safe data binding.",
    )
    unrelated_command = _finding(
        "other-command",
        file=".github/workflows/review.yml",
        line=17,
        category="command-injection",
        message="subprocess.run(user_input, shell=True) is dynamically constructed.",
    )

    survivors, dropped = Verifier().verify([title_detector, title_llm, unrelated_command])

    assert {finding.id for finding in survivors} == {"title-detector", "other-command"}
    assert dropped == ["title-llm"]


def test_same_line_secret_output_category_drift_prefers_detector():
    detector = _finding(
        "detector",
        file=".github/workflows/review.yml",
        line=14,
        category="data-leak",
        message="Workflow prints a secret value to command output.",
        verified_by="detector",
    )
    llm = _finding(
        "llm",
        file=".github/workflows/review.yml",
        line=14,
        category="hardcoded-secrets",
        message="The job echoes a secret token into its log output.",
    )

    survivors, dropped = Verifier().verify([llm, detector])

    assert [finding.id for finding in survivors] == ["detector"]
    assert dropped == ["llm"]


def test_mixed_language_message_keeps_code_sink_identity():
    detector = _finding(
        "detector",
        file="account.go",
        line=16,
        category="sql-injection",
        message="SQL string is built with fmt.Sprintf.",
        verified_by="detector",
    )
    llm = _finding(
        "llm",
        file="account.go",
        line=18,
        category="sql-injection",
        message=(
            "\u4f7f\u7528 fmt.Sprintf \u6784\u5efa SQL \u67e5\u8be2\uff0c\u5b58\u5728\u6ce8\u5165\u98ce\u9669\u3002"
        ),
    )

    survivors, dropped = Verifier().verify([detector, llm])

    assert [finding.id for finding in survivors] == ["detector"]
    assert dropped == ["llm"]


def test_nearby_distinct_sinks_are_not_merged():
    detector = _finding(
        "detector",
        line=10,
        category="command-injection",
        message="os.popen(user_input) passes dynamic data to a command.",
        verified_by="detector",
    )
    independent = _finding(
        "independent",
        line=11,
        category="command-injection",
        message="subprocess.run(user_value, shell=True) executes another command.",
    )

    survivors, dropped = Verifier().verify([detector, independent])

    assert {finding.id for finding in survivors} == {"detector", "independent"}
    assert dropped == []


def test_nearby_detector_hits_remain_distinct_even_for_same_api():
    first = _finding(
        "first",
        line=10,
        category="command-injection",
        message="os.popen(first_input) passes dynamic data to a command.",
        verified_by="detector",
    )
    second = _finding(
        "second",
        line=11,
        category="command-injection",
        message="os.popen(second_input) passes dynamic data to a command.",
        verified_by="detector",
    )

    survivors, dropped = Verifier().verify([first, second])

    assert {finding.id for finding in survivors} == {"first", "second"}
    assert dropped == []


def test_nearby_dependency_manifest_entries_are_never_fuzzy_merged():
    detector = _finding(
        "detector",
        file="requirements.txt",
        line=4,
        category="dependency-vulnerability",
        message="jinja2 2.10 matches an advisory.",
        verified_by="detector",
    )
    adjacent = _finding(
        "adjacent",
        file="requirements.txt",
        line=5,
        category="dependency-vulnerability",
        message="jinja2 plugin 1.0 matches a separate advisory.",
    )

    survivors, dropped = Verifier().verify([detector, adjacent])

    assert {finding.id for finding in survivors} == {"detector", "adjacent"}
    assert dropped == []


def test_detector_does_not_absorb_same_sink_outside_line_tolerance():
    detector = _finding(
        "detector",
        line=10,
        category="xss",
        message="Vue v-html binding can inject HTML.",
        verified_by="detector",
    )
    distant = _finding(
        "distant",
        line=14,
        category="unsafe-html",
        message="v-html renders untrusted content.",
    )

    survivors, dropped = Verifier().verify([detector, distant])

    assert {finding.id for finding in survivors} == {"detector", "distant"}
    assert dropped == []


def test_nonduplicate_categories_are_still_canonicalized():
    finding = _finding(
        "alt",
        file="avatar.vue",
        line=8,
        category="missing-alt-text",
        message="Image is missing alt text.",
        reviewer="accessibility_reviewer",
    )

    survivors, dropped = Verifier().verify([finding])

    assert survivors[0].category == "missing-alt"
    assert dropped == []
