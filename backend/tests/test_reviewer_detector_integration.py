"""Integration tests for the Reviewer ↔ deterministic detector handoff.

The security and dependency reviewers merge zero-token deterministic detector
findings into their LLM output. Those findings MUST survive the per-reviewer
cap that trims verbose LLM nitpick noise, and they MUST dedupe against LLM
findings on the same (file, line, category) so we never post a single issue
twice. These tests pin those two contracts end-to-end through the reviewer.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from reviewforge.core.specs import build_registry
from reviewforge.core.state import ReviewTask, StateStore
from reviewforge.engine.detectors.dependency import detect_dependency_findings
from reviewforge.engine.detectors.security import detect_security_findings
from reviewforge.engine.reviewers import (
    REVIEWER_MAP,
    BaseReviewer,
    DependencyReviewer,
    SecurityReviewer,
)
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient

# ── Test doubles ──────────────────────────────────────────────────────────


class _ScriptedLLM:
    """Returns a fixed findings payload on every call.

    Mirrors the structure of the production single-shot reviewer response so
    tests can pin exact (file, line, category) coordinates against detector
    output without depending on mock heuristics.
    """

    def __init__(self, findings: list[dict]) -> None:
        self._findings = findings
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        return AIMessage(content=json.dumps({"findings": self._findings}))

    def bind_tools(self, _tools, **_kwargs):
        return self


@pytest.fixture
def registry():
    return build_registry()


@pytest.fixture
def gateway(registry):
    return ToolGateway(registry, MockGitHubClient())


@pytest.fixture
def hardcoded_secret_diff() -> str:
    """A diff that the universal hardcoded-secret detector MUST flag."""

    # Format intentionally mimics the GitHub RIGHT-side hunk structure so
    # the detector's iter_right_lines mapping produces the same coordinates
    # the LLM would anchor a comment at.
    return (
        "diff --git a/src/auth.py b/src/auth.py\n"
        "@@ -0,0 +1,4 @@\n"
        "+def login():\n"
        "+    api_key = \"sk-live-4f9b2c8e1d7a6b5c3d2e1f0a9b8c7d6e\"\n"
        "+    token = \"Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sign\"\n"
        "+    return api_key\n"
    )


@pytest.fixture
def unpinned_requirements_diff() -> str:
    """A requirements.txt diff the dependency detector MUST flag."""

    return (
        "diff --git a/requirements.txt b/requirements.txt\n"
        "@@ -1,2 +1,3 @@\n"
        " flask==2.3.2\n"
        "+requests>=2.31.0\n"
        "+django\n"
    )


# ── Direct detector sanity checks ─────────────────────────────────────────
# These pin the detectors the reviewers rely on. If a detector stops
# matching, every reviewer that delegates to it regresses at once.


def test_security_detector_flags_hardcoded_secret(hardcoded_secret_diff):
    findings = detect_security_findings({"src/auth.py": hardcoded_secret_diff})

    flagged = [f for f in findings if f.category == "hardcoded-secrets"]
    # Both the api_key and the Bearer literal are real credentials; the
    # detector must flag each independently rather than deduping them.
    assert len(flagged) == 2, [(f.line, f.confidence, f.message) for f in flagged]
    assert all(f.severity == "error" for f in flagged)
    assert all(f.confidence >= 0.9 for f in flagged)
    assert {f.line for f in flagged} == {2, 3}


def test_dependency_detector_flags_unpinned_requirements(unpinned_requirements_diff):
    findings = detect_dependency_findings({"requirements.txt": unpinned_requirements_diff})

    assert findings, "expected dependency detector to flag unpinned wheels"
    messages = "\n".join(f.message for f in findings)
    # Either the unpinned pin complaint or the unpinned-version complaint is
    # acceptable — both come from the same dependency detector. The point is
    # that the wheel is not silently accepted as deterministic-clean.
    assert "requests" in messages or "django" in messages or "pin" in messages.lower()


# ── Reviewer ↔ detector handoff ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_security_reviewer_preserves_detector_finding_beyond_llm_cap(
    registry, gateway, hardcoded_secret_diff
):
    """Security reviewer cap is 15. Flooding the LLM with 20 findings must
    NOT drop the deterministic hardcoded-secret detector finding.

    Regression guard for the bug where re-capping the merged (LLM + detector)
    set silently dropped every scanner hit when the LLM filled every slot.
    """
    overflow_findings = [
        {
            "file": "src/auth.py",
            "line": line,
            "severity": "warning",
            "category": f"readability-{line}",  # distinct so dedupe does not collapse them
            "message": f"noise finding {line}",
            "confidence": 0.5,
        }
        for line in range(1, 21)
    ]
    llm = _ScriptedLLM(overflow_findings)
    reviewer = SecurityReviewer(llm, registry, gateway, agentic=False)

    task = ReviewTask(reviewer="security_reviewer", files=["src/auth.py"], rationale="eval")
    state = StateStore(
        pr_number=1,
        repo="o/r",
        head_sha="abc",
        files_changed=["src/auth.py"],
        diff_summary=hardcoded_secret_diff,
    )
    # Bypass the MockGitHubClient: feed our own diff directly so the
    # detector sees the hardcoded secret instead of the canned response.
    state.file_diffs = {"src/auth.py": hardcoded_secret_diff}

    findings = await reviewer.execute(task, state)

    by_category: dict[str, list] = {}
    for f in findings:
        by_category.setdefault(f.category, []).append(f)

    # The deterministic detector finding must survive the LLM-noise flood.
    assert "hardcoded-secrets" in by_category, by_category
    detector_finding = by_category["hardcoded-secrets"][0]
    assert detector_finding.reviewer == "security_reviewer"
    assert detector_finding.verified_by == "detector"


@pytest.mark.asyncio
async def test_security_reviewer_dedupes_overlapping_detector_and_llm_finding(
    registry, gateway, hardcoded_secret_diff
):
    """If the LLM ALSO reports the same hardcoded secret the detector caught,
    the higher-confidence survivor wins — only one inline comment is emitted.
    """
    # The LLM spots the same line the detector will catch, but with weaker
    # confidence, to prove the dedupe key is (file, line, category) and the
    # detector finding wins over the LLM finding.
    detector_line = 2  # api_key line in the post-image; the detector fires here
    llm_finding = {
        "file": "src/auth.py",
        "line": detector_line,
        "severity": "warning",
        "category": "hardcoded-secrets",
        "message": "weak LLM duplicate of detector hit",
        "confidence": 0.7,
    }
    llm = _ScriptedLLM([llm_finding])
    reviewer = SecurityReviewer(llm, registry, gateway, agentic=False)

    task = ReviewTask(reviewer="security_reviewer", files=["src/auth.py"], rationale="eval")
    state = StateStore(
        pr_number=1,
        repo="o/r",
        head_sha="abc",
        files_changed=["src/auth.py"],
        diff_summary=hardcoded_secret_diff,
    )
    state.file_diffs = {"src/auth.py": hardcoded_secret_diff}

    findings = await reviewer.execute(task, state)
    matching = [
        f
        for f in findings
        if f.file == "src/auth.py"
        and f.line == detector_line
        and f.category == "hardcoded-secrets"
    ]

    # Exactly one survivor, and it must be the deterministic detector finding.
    assert len(matching) == 1, [(f.confidence, f.verified_by) for f in matching]
    assert matching[0].confidence >= 0.9
    assert matching[0].verified_by == "detector"


@pytest.mark.asyncio
async def test_dependency_reviewer_includes_detector_finding_when_llm_returns_empty(
    registry, gateway, unpinned_requirements_diff
):
    """Even when the LLM returns no findings, the dependency reviewer must
    surface the deterministic unpinned-wheel detector finding."""
    llm = _ScriptedLLM([])
    reviewer = DependencyReviewer(llm, registry, gateway, agentic=False)

    task = ReviewTask(reviewer="dependency_reviewer", files=["requirements.txt"], rationale="eval")
    state = StateStore(
        pr_number=2,
        repo="o/r",
        head_sha="def",
        files_changed=["requirements.txt"],
        diff_summary=unpinned_requirements_diff,
    )
    state.file_diffs = {"requirements.txt": unpinned_requirements_diff}

    findings = await reviewer.execute(task, state)

    assert findings, "dependency reviewer must surface the unpinned-wheel detector finding"
    assert all(f.reviewer == "dependency_reviewer" for f in findings)
    assert any(f.verified_by == "detector" for f in findings)


@pytest.mark.asyncio
async def test_dependency_reviewer_skips_detector_path_for_non_dependency_reviewer(
    registry, gateway, hardcoded_secret_diff
):
    """A non-security, non-dependency reviewer MUST NOT pull in detector
    findings — those are the owning reviewer's responsibility. This pins the
    reviewer-type guard inside ``_merge_detector_findings``."""
    llm = _ScriptedLLM([])
    reviewer = BaseReviewer(
        name="style_reviewer",
        reviewer_type="style",
        llm=llm,
        registry=registry,
        gateway=gateway,
    )

    task = ReviewTask(reviewer="style_reviewer", files=["src/auth.py"], rationale="eval")
    state = StateStore(
        pr_number=3,
        repo="o/r",
        head_sha="ghi",
        files_changed=["src/auth.py"],
        diff_summary=hardcoded_secret_diff,
    )
    state.file_diffs = {"src/auth.py": hardcoded_secret_diff}

    findings = await reviewer.execute(task, state)

    assert findings == [], findings
    # The LLM was called once and the empty-array response was respected —
    # the deterministic detector must not have been consulted.
    assert llm.calls == 1


def test_reviewer_map_matches_expected_reviewer_names():
    """Pin the orchestrator-facing reviewer surface. The orchestrator keys
    task dispatch and detector handoff on these exact names, so an accidental
    rename (or a silently-removed reviewer) must fail this test, not just a
    downstream ``KeyError``."""

    expected_names = {
        "security_reviewer",
        "performance_reviewer",
        "style_reviewer",
        "correctness_reviewer",
        "localization_reviewer",
        "testing_reviewer",
        "doc_reviewer",
        "dependency_reviewer",
        "accessibility_reviewer",
    }
    assert set(REVIEWER_MAP.keys()) == expected_names

    for name, cls in REVIEWER_MAP.items():
        import inspect

        source = inspect.getsource(cls)
        # Each concrete subclass of BaseReviewer fixes its own reviewer_type
        # in its __init__ via super().__init__(..., reviewer_type=...).
        # The detector-routing logic in BaseReviewer._merge_detector_findings
        # only fires for "security" and "dependency", so an unset
        # reviewer_type is a routing hole.
        assert "reviewer_type=" in source, f"{name} ({cls.__name__}) does not pin reviewer_type"
