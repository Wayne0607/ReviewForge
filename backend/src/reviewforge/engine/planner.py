"""Planner Agent — single-shot LLM decision maker with deterministic security detection.

Reads PR diff summary, outputs task proposals for reviewers.
Patterns are language-aware: each pattern carries a language marker so Go code
triggers Go-specific security checks, Rust code triggers Rust-specific ones, etc.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from reviewforge.core.specs import SpecRegistry
from reviewforge.core.state import TASK_RATIONALE_MAX_LENGTH, ReviewTask, StateStore
from reviewforge.engine.context_engine import render_impact_manifest
from reviewforge.engine.prompt import build_planner_prompt
from reviewforge.engine.symbol_extractor import detect_language

logger = logging.getLogger(__name__)

_MAX_LLM_TASKS = 6
_MAX_REVIEWER_NAME_LENGTH = 100
_MAX_FILE_PATH_LENGTH = 1024
_MAX_RATIONALE_INPUT_LENGTH = TASK_RATIONALE_MAX_LENGTH * 10
_MAX_LOCALIZATION_FILES = 16

# ── 通用安全模式（不限语言）────────────────────────────────────────────
_UNIVERSAL_SECURITY = [
    (r"(?:password|secret|api_key|token)\s*=\s*['\"\"][^'\"]{8,}['\"\"]", "hardcoded-secrets"),
    (r"(?:SELECT|INSERT|UPDATE|DELETE).*\+\s*(?:str\(|f['\"])", "sql-injection"),
    (r"f['\"].*(?:SELECT|INSERT|UPDATE|DELETE).*\{", "sql-injection"),
    (r"open\s*\([^)]*\+.*['\"]r['\"]", "path-traversal"),
]

# ── 语言特定安全模式 ──────────────────────────────────────────────────
_SECURITY_BY_LANG = {
    "python": [
        (r"os\.system\s*\(", "command-injection"),
        (r"subprocess\.\w+\(.*shell\s*=\s*True", "command-injection"),
        (r"os\.popen\s*\(", "command-injection"),
        (r"subprocess\.(?:call|run|Popen)\s*\(", "command-injection"),
        (r"pickle\.loads?\s*\(", "insecure-deserialization"),
        (r"yaml\.load\s*\([^)]*\)", "insecure-deserialization"),
        (r"eval\s*\(", "code-injection"),
        (r"exec\s*\(", "code-injection"),
    ],
    "go": [
        (r"exec\.Command\(", "command-injection"),
        (r"os/exec", "command-injection"),
        (r"template\.HTML\(", "xss"),
        (r"unsafe\b", "unsafe-usage"),
        (r"db\.Query\(.*\+", "sql-injection"),
        (r"fmt\.Sprintf\(.*SELECT", "sql-injection"),
    ],
    "java": [
        (r"Runtime\.getRuntime\(\)\.exec", "command-injection"),
        (r"ProcessBuilder", "command-injection"),
        (r"ObjectInputStream", "insecure-deserialization"),
        (r"ScriptEngine", "code-injection"),
        (r"Statement\.executeQuery\(.*\+", "sql-injection"),
    ],
    "rust": [
        (r"unsafe\s*\{", "unsafe-block"),
        (r"std::process::Command", "command-injection"),
        (r"Command::new", "command-injection"),
        (r"unwrap\(\)", "unchecked-unwrap"),
        (r"panic!\(", "production-panic"),
        (r"transmute\s*(::)?\s*<", "unsafe-transmute"),
        (r"\.unwrap\(\)", "unchecked-unwrap"),
    ],
    "ruby": [
        (r"system\s*\(", "command-injection"),
        (r"exec\s*\(", "code-injection"),
        (r"`.*#\{.*\}.*`", "command-injection"),
        (r"%x\(", "command-injection"),
        (r"eval\s*\(", "code-injection"),
        (r"YAML\.load", "insecure-deserialization"),
        (r"Marshal\.load", "insecure-deserialization"),
        (r"rescue\s+Exception", "broad-rescue"),
        (r"send\s*\(", "dynamic-dispatch"),
        (r"instance_eval", "code-injection"),
        (r"class_eval", "code-injection"),
        (r"Open3\.", "command-injection"),
    ],
    "javascript": [
        (r"eval\s*\(", "code-injection"),
        (r"innerHTML\s*=", "xss"),
        (r"child_process", "command-injection"),
        (r"document\.write\(", "xss"),
        (r"v-html", "xss"),
        (r"\{@html", "xss"),
        (r"localStorage\.setItem\(.*token", "data-leak"),
    ],
    "typescript": [
        (r"eval\s*\(", "code-injection"),
        (r"innerHTML\s*=", "xss"),
        (r"dangerouslySetInnerHTML", "xss"),
        (r"v-html", "xss"),
        (r"\[innerHTML\]", "xss"),
        (r"bypassSecurityTrust", "xss-bypass"),
        (r"localStorage\.setItem\(.*token", "data-leak"),
    ],
}

# ── 通用依赖模式 ──────────────────────────────────────────────────────
_UNIVERSAL_DEPENDENCY = [
    (
        r"(?:^|/)(?:package\.json|requirements\.txt|pyproject\.toml|pom\.xml|Gemfile|go\.mod|Cargo\.toml)\b",
        "dep-change",
    ),
    (
        r"(?:package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock|go\.sum|Gemfile\.lock)",
        "dep-file-change",
    ),
    (r"\.github/workflows/.+\.ya?ml", "ci-change"),
]

_DEPENDENCY_BY_LANG = {
    "python": [
        (r"(?:pip install|requirements.*\.txt|pyproject\.toml|setup\.py|Pipfile|poetry\.lock)", "dep-change"),
    ],
    "go": [
        (r"(?:go\.mod|go\.sum|go\s+(?:get|mod)\s)", "dep-change"),
    ],
    "java": [
        (r"(?:pom\.xml|build\.gradle|mvn\s|gradle\s)", "dep-change"),
    ],
    "rust": [
        (r"(?:Cargo\.toml|Cargo\.lock|cargo\s+(?:add|install))", "dep-change"),
    ],
    "ruby": [
        (r"(?:Gemfile|\.gemspec|bundle\s+install|gem\s+install)", "dep-change"),
    ],
}

# ── 性能模式（通用）───────────────────────────────────────────────────
_PERFORMANCE_PATTERNS = [
    (r"for\s+\w+\s+in\s+range.*\n.*for\s+\w+\s+in\s+range", "nested-loop"),
    (r"(?:urllib\.request\.urlopen|requests\.get)\s*\(.*\n.*for\s+", "blocking-io-in-loop"),
    (r"sqlite3\.connect\s*\(.*\n.*for\s+", "db-in-loop"),
]

# ── 测试模式（通用）───────────────────────────────────────────────────
# ── 复杂可访问性模式（明确 alt/label 由 Phase0 零 token detector 负责）──
_COMPLEX_A11Y_PATTERNS = [
    (r"<(?:div|span|li|a)\b[^>]*(?:onClick|onKeyDown|onKeyPress|tabIndex)", "custom-interactive"),
    (r"<(?:button|video|audio|canvas|dialog)\b", "semantic-interactive"),
    (r"\b(?:onKeyDown|onKeyPress|tabIndex|contentEditable|autoFocus)\b", "keyboard-focus"),
    (r"\b(?:aria-|role\s*=)", "aria-contract"),
    (r"(?:\bfocus\s*\(|\.focus\s*\(|\bmodal\b|\bdialog\b)", "focus-management"),
    (r"(?:@keyframes|animation\s*:|transition\s*:)", "motion"),
]


class Planner:
    """Single-shot planner with deterministic, language-aware pattern detection."""

    def __init__(self, llm: ChatOpenAI, registry: SpecRegistry) -> None:
        self._llm = llm
        self._registry = registry

    async def plan(self, state: StateStore, notes: list | None = None) -> list[ReviewTask]:
        """Analyze the PR and return task proposals (re-planning aware).

        Reviewers already dispatched this run are excluded, so repeat rounds
        converge to empty (and the loop detector catches genuine repeats). Notes
        from prior rounds (e.g. loop-detector rescue hints) are fed to the LLM.
        """
        done_reviewers = {t.reviewer for t in state.list_tasks() if t.status in ("completed", "claimed", "failed")}
        first_round = not done_reviewers

        # Step 1: Deterministic pattern detection (skip already-dispatched reviewers)
        cross_pr_wrapper = _looks_like_cross_pr_wrapper(state.files_changed, state.diff_summary)
        if cross_pr_wrapper:
            logger.info("Cross-PR wrapper change detected; skipping normal reviewers")
            return []

        forced_reviewers = {
            r
            for r in (self._detect_patterns(state.files_changed, state.diff_summary) - done_reviewers)
            if not _skip_reviewer_for_change(r, state.files_changed, state.diff_summary)
        }
        if _localization_files(state.files_changed) and "localization_reviewer" not in done_reviewers:
            forced_reviewers.add("localization_reviewer")

        # Detect language summary for the planner prompt
        file_langs = self._detect_file_languages(state.files_changed)

        # Step 2: LLM decision for additional reviewers
        ctx = {
            "registry": self._registry,
            "repo": state.repo,
            "pr_number": state.pr_number,
            "pr_title": "",
            "files_changed": state.files_changed,
            "diff_summary": state.diff_summary,
            "language_summary": file_langs,
            "done_reviewers": sorted(done_reviewers),
            "notes": [{"from": n.from_agent, "type": n.type, "content": n.content} for n in (notes or [])],
            "impact_manifest_text": render_impact_manifest(state.impact_manifest, max_chars=4_500),
        }
        messages = build_planner_prompt(ctx)

        response = await self._llm.ainvoke(
            [SystemMessage(content=messages[0]["content"]), HumanMessage(content=messages[1]["content"])]
        )

        llm_tasks = [
            t
            for t in self._parse_response(response.content, allowed_files=state.files_changed)
            if t.reviewer not in done_reviewers
            and not _skip_reviewer_for_change(t.reviewer, t.files or state.files_changed, state.diff_summary)
            and not (cross_pr_wrapper and _is_low_signal_reviewer(t.reviewer))
        ]

        # Step 3: Merge — include forced reviewers; default style only on the first round
        return self._merge_tasks(
            forced_reviewers,
            llm_tasks,
            state.files_changed,
            first_round,
            style_fallback=not cross_pr_wrapper,
        )

    @staticmethod
    def _detect_file_languages(files: list[str]) -> str:
        """Return a human-readable language summary of the changed files."""
        langs = [detect_language(f) for f in files]
        known = [lang for lang in langs if lang and lang != "unknown"]
        if not known:
            return "未识别"
        counts = Counter(known)
        parts = [f"{lang}({count})" if count > 1 else lang for lang, count in counts.most_common()]
        return ", ".join(parts)

    def _detect_patterns(self, files: list[str], diff: str) -> set[str]:
        """Language-aware deterministic pattern detection."""
        forced: set[str] = set()
        file_set = set(files)
        file_langs = {detect_language(f) for f in file_set}
        is_frontend = any(f.endswith((".tsx", ".jsx", ".vue", ".html", ".svelte")) for f in file_set)

        # --- Security ---
        security_hit = False
        # 1. Universal patterns
        for pattern, label in _UNIVERSAL_SECURITY:
            if re.search(pattern, diff, re.IGNORECASE | re.MULTILINE):
                forced.add("security_reviewer")
                security_hit = True
                logger.info(f"Universal security pattern: {label}")
                break
        # 2. Language-specific patterns
        if not security_hit:
            for lang in file_langs:
                for pattern, label in _SECURITY_BY_LANG.get(lang, []):
                    if re.search(pattern, diff, re.IGNORECASE | re.MULTILINE):
                        forced.add("security_reviewer")
                        logger.info(f"[{lang}] Security pattern: {label}")
                        break

        # --- Performance ---
        for pattern, label in _PERFORMANCE_PATTERNS:
            if re.search(pattern, diff, re.IGNORECASE | re.MULTILINE):
                forced.add("performance_reviewer")
                logger.info(f"Performance pattern: {label}")
                break

        # --- Testing (only when changed evidence can support a concrete defect) ---
        if any(_is_test_file(file_path) for file_path in file_set) or _looks_like_security_test_regression(diff):
            forced.add("testing_reviewer")
            logger.info("Testing evidence changed")

        # --- Dependency ---
        dep_hit = False
        # 1. Universal
        for pattern, label in _UNIVERSAL_DEPENDENCY:
            if re.search(pattern, diff, re.IGNORECASE | re.MULTILINE):
                forced.add("dependency_reviewer")
                dep_hit = True
                logger.info(f"Universal dependency pattern: {label}")
                break
        # 2. Language-specific
        if not dep_hit:
            for lang in file_langs:
                for pattern, label in _DEPENDENCY_BY_LANG.get(lang, []):
                    if re.search(pattern, diff, re.IGNORECASE | re.MULTILINE):
                        forced.add("dependency_reviewer")
                        logger.info(f"[{lang}] Dependency pattern: {label}")
                        break

        # --- Accessibility (frontend only) ---
        if is_frontend:
            for pattern, label in _COMPLEX_A11Y_PATTERNS:
                if re.search(pattern, diff, re.IGNORECASE | re.MULTILINE):
                    forced.add("accessibility_reviewer")
                    logger.info(f"Accessibility pattern: {label}")
                    break

        return forced

    def _merge_tasks(
        self,
        forced: set[str],
        llm_tasks: list[ReviewTask],
        files: list[str],
        first_round: bool = True,
        style_fallback: bool = True,
    ) -> list[ReviewTask]:
        """Merge forced reviewers with LLM decisions.

        On the first round, style_reviewer is always added as a default and a
        fallback guarantees at least one task. On re-planning rounds an empty
        result is valid (it signals convergence — nothing more to dispatch).
        """
        llm_reviewers = {t.reviewer for t in llm_tasks}
        merged = list(llm_tasks)

        for reviewer in forced:
            if reviewer not in llm_reviewers:
                task_files = _localization_files(files) if reviewer == "localization_reviewer" else files
                merged.append(
                    ReviewTask(
                        reviewer=reviewer,
                        files=task_files,
                        rationale="自动检测到安全/性能模式",
                    )
                )
                logger.info(f"Forced reviewer added: {reviewer}")

        if first_round and style_fallback and not merged:
            merged.append(
                ReviewTask(
                    reviewer="style_reviewer",
                    files=files,
                    rationale="fallback style review",
                )
            )

        if first_round and style_fallback:
            return merged or [ReviewTask(reviewer="style_reviewer", files=files, rationale="fallback")]
        return merged

    def _parse_response(self, content: str, allowed_files: list[str] | None = None) -> list[ReviewTask]:
        """Parse untrusted LLM JSON into bounded, schema-valid tasks.

        Validation is deliberately per item: one malformed proposal must not
        discard valid sibling tasks or fail the entire review round.
        """
        if not isinstance(content, str):
            logger.warning("Planner returned non-text content, ignoring LLM tasks")
            return []

        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content[3:]
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            logger.warning("Planner returned invalid JSON, falling back to style-only review")
            return []

        if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
            logger.warning("Planner returned an invalid task envelope, ignoring LLM tasks")
            return []

        allowed_by_normalized_path = (
            {_normalize_file_path(path): path for path in allowed_files if _normalize_file_path(path)}
            if allowed_files is not None
            else None
        )
        tasks: list[ReviewTask] = []
        for index, item in enumerate(data["tasks"]):
            if len(tasks) >= _MAX_LLM_TASKS:
                logger.warning("Planner returned more than %d valid tasks; extras were ignored", _MAX_LLM_TASKS)
                break
            if not isinstance(item, dict):
                logger.warning("Planner task %d is not an object; skipping", index)
                continue

            raw_reviewer = item.get("reviewer")
            if not isinstance(raw_reviewer, str) or not raw_reviewer.strip():
                logger.warning("Planner task %d has an invalid reviewer; skipping", index)
                continue
            if len(raw_reviewer) > _MAX_REVIEWER_NAME_LENGTH:
                logger.warning("Planner task %d has an overlong reviewer; skipping", index)
                continue

            reviewer = raw_reviewer.strip()
            reviewer = reviewer.lower().replace(" ", "_").replace("-", "_")
            reviewer_map = {
                "security": "security_reviewer",
                "security_reviewer": "security_reviewer",
                "performance": "performance_reviewer",
                "performance_reviewer": "performance_reviewer",
                "style": "style_reviewer",
                "style_reviewer": "style_reviewer",
                "architecture": "style_reviewer",
                "readability": "style_reviewer",
                "testing": "testing_reviewer",
                "testing_reviewer": "testing_reviewer",
                "test": "testing_reviewer",
                "documentation": "doc_reviewer",
                "documentation_reviewer": "doc_reviewer",
                "doc": "doc_reviewer",
                "doc_reviewer": "doc_reviewer",
                "dependency": "dependency_reviewer",
                "dependency_reviewer": "dependency_reviewer",
                "deps": "dependency_reviewer",
                "accessibility": "accessibility_reviewer",
                "accessibility_reviewer": "accessibility_reviewer",
                "a11y": "accessibility_reviewer",
                "localization": "localization_reviewer",
                "localisation": "localization_reviewer",
                "i18n": "localization_reviewer",
                "l10n": "localization_reviewer",
                "localization_reviewer": "localization_reviewer",
            }
            reviewer = reviewer_map.get(reviewer, reviewer)

            if reviewer not in self._registry.agents:
                logger.warning("Planner task %d proposed an unknown reviewer; skipping", index)
                continue

            raw_files = item.get("files")
            if not isinstance(raw_files, list):
                logger.warning("Planner task %d has an invalid files field; skipping", index)
                continue

            files: list[str] = []
            seen_files: set[str] = set()
            for raw_path in raw_files:
                path = _normalize_file_path(raw_path)
                if not path or path in seen_files:
                    continue
                if allowed_by_normalized_path is not None:
                    canonical_path = allowed_by_normalized_path.get(path)
                    if canonical_path is None:
                        continue
                    path = canonical_path
                seen_files.add(path)
                files.append(path)

            if not files:
                logger.warning("Planner task %d has no valid changed files; skipping", index)
                continue

            try:
                tasks.append(
                    ReviewTask(
                        reviewer=reviewer,
                        files=files,
                        rationale=_normalize_rationale(item.get("rationale", "")),
                    )
                )
            except (TypeError, ValueError):
                logger.warning("Planner task %d failed schema validation; skipping", index)

        return tasks


def _normalize_rationale(value: object) -> str:
    """Normalize optional prose without coercing structured data into logs/state."""
    if not isinstance(value, str):
        return ""
    bounded = value[:_MAX_RATIONALE_INPUT_LENGTH]
    printable = "".join(char if char.isprintable() else " " for char in bounded)
    return " ".join(printable.split())[:TASK_RATIONALE_MAX_LENGTH]


def _normalize_file_path(value: object) -> str:
    """Return a safe repository-relative path, or an empty string when invalid."""
    if not isinstance(value, str):
        return ""
    path = value.strip().replace("\\", "/")
    if not path or len(path) > _MAX_FILE_PATH_LENGTH or not path.isprintable():
        return ""
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return ""
    if any(part == ".." for part in path.split("/")):
        return ""
    return path


def _is_test_file(file_path: str) -> bool:
    """Check if a file is a test file by common naming conventions."""
    name = file_path.lower()
    return any(
        name.endswith(suffix)
        for suffix in (
            "_test.py",
            "test_.py",
            "_test.go",
            "test.go",
            "test.java",
            "tests.java",
            "test.rs",
            "_test.rs",
            "test.rb",
            "_test.rb",
            "spec.rb",
            "_spec.rb",
            "test.ts",
            "spec.ts",
            "test.tsx",
            "spec.tsx",
            "test.js",
            "spec.js",
            "test.jsx",
            "spec.jsx",
        )
    ) or name.startswith(("test_", "spec/", "tests/", "__tests__/", "test/"))


def _localization_files(files: list[str]) -> list[str]:
    """Select bounded production locale resources for dedicated semantic review."""

    selected: list[str] = []
    locale_suffixes = (".properties", ".po", ".pot", ".arb", ".strings", ".resx", ".ftl")
    locale_directories = ("/i18n/", "/l10n/", "/locale/", "/locales/", "/translations/")
    excluded_directories = ("/src/test/", "/test/", "/tests/", "/testdata/", "/fixtures/")
    for file_path in files:
        normalized = "/" + file_path.replace("\\", "/").lower().lstrip("/")
        if any(marker in normalized for marker in excluded_directories):
            continue
        is_locale_resource = normalized.endswith(locale_suffixes) or (
            normalized.endswith((".json", ".yaml", ".yml"))
            and any(marker in normalized for marker in locale_directories)
        )
        if not is_locale_resource:
            continue
        selected.append(file_path)
        if len(selected) >= _MAX_LOCALIZATION_FILES:
            break
    return selected


def _skip_reviewer_for_files(reviewer: str, files: list[str]) -> bool:
    """Skip low-signal reviewers for fixtures/examples where product UX/tests do not apply."""
    if reviewer in {"security_reviewer", "dependency_reviewer"}:
        return False
    if not files:
        return False
    fixture_prefixes = ("test_fixtures/", "examples/", "docs/")
    return all(f.replace("\\", "/").startswith(fixture_prefixes) for f in files)


def _skip_reviewer_for_change(reviewer: str, files: list[str], diff: str) -> bool:
    """Apply evidence-aware routing for reviewers whose mission needs changed artifacts."""

    if reviewer == "doc_reviewer":
        normalized = [file_path.replace("\\", "/").lower() for file_path in files]
        docs_changed = any(
            path.startswith("docs/")
            or path.rsplit("/", 1)[-1].startswith(("readme", "changelog", "contributing"))
            or path.endswith((".md", ".mdx", ".rst", ".adoc"))
            for path in normalized
        )
        return not docs_changed
    if _skip_reviewer_for_files(reviewer, files):
        return True
    if reviewer == "testing_reviewer":
        return not any(_is_test_file(file_path) for file_path in files) and not _looks_like_security_test_regression(
            diff
        )
    if reviewer == "accessibility_reviewer":
        return not _has_complex_accessibility_evidence(diff)
    return False


def _has_complex_accessibility_evidence(diff: str) -> bool:
    """Whether semantic/keyboard/focus analysis is needed beyond Phase0 sinks."""

    return any(
        re.search(pattern, diff or "", re.IGNORECASE | re.MULTILINE) for pattern, _label in _COMPLEX_A11Y_PATTERNS
    )


def _looks_like_security_test_regression(diff: str) -> bool:
    """Whether a security fix changed an existing guard and merits test review."""

    removed_behavior = any(
        line.startswith("-")
        and not line.startswith("---")
        and re.search(
            r"(?:eval\s*\(|exec\s*\(|shell\s*=\s*true|innerhtml|yaml\.load|pickle\.load|"
            r"authori[sz]|authenticat|permission|saniti[sz]|allow.?list)",
            line,
            re.IGNORECASE,
        )
        for line in (diff or "").splitlines()
    )
    added_guard = any(
        line.startswith("+")
        and not line.startswith("+++")
        and re.search(
            r"(?:saniti[sz]|escape|allow.?list|authori[sz]|authenticat|permission|"
            r"preparedstatement|is_relative_to|safe_load)",
            line,
            re.IGNORECASE,
        )
        for line in (diff or "").splitlines()
    )
    return removed_behavior and added_guard


def _is_low_signal_reviewer(reviewer: str) -> bool:
    return reviewer in {
        "style_reviewer",
        "testing_reviewer",
        "doc_reviewer",
        "performance_reviewer",
        "accessibility_reviewer",
    }


def _looks_like_cross_pr_wrapper(files: list[str], diff: str) -> bool:
    """Detect tiny import/wrapper PRs where cross-PR graph analysis is higher value.

    These changes are commonly follow-up PRs that wire an existing helper into a
    new call site. Running style/doc/testing reviewers on them mostly produces
    low-value comments; the cross-PR analyzer can still inspect imports later.
    """
    if not files:
        return False
    source_exts = (".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".rb", ".rs")
    if not all(f.endswith(source_exts) for f in files):
        return False

    added = [
        line[1:].strip()
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++") and line[1:].strip()
    ]
    code = [line for line in added if not line.startswith(("#", "//", "*", '"""', "'''"))]
    if not code or len(code) > 14:
        return False

    has_import = any(
        re.match(r"(?:from\s+[\w.]+\s+import\s+\w|import\s+[\w.{]|import\s*\{|const\s+\w+\s*=\s*require)", line)
        for line in code
    )
    if not has_import:
        return False

    dangerous_patterns = [p for p, _ in _UNIVERSAL_SECURITY]
    for patterns in _SECURITY_BY_LANG.values():
        dangerous_patterns.extend(p for p, _ in patterns)
    return not any(re.search(pattern, diff, re.IGNORECASE | re.MULTILINE) for pattern in dangerous_patterns)
