#!/usr/bin/env python3
"""ReviewForge Comprehensive Test Suite Runner.

Creates multiple test PRs targeting different dimensions:
  1. Multi-Language Detection Accuracy
  2. Security Vulnerability Coverage
  3. False Positive Resistance
  4. Cross-PR Risk Propagation (2-phase)
  5. Token Scaling Benchmark

Then collects all metrics and generates a comprehensive statistical report.

Usage:
  python scripts/run_test_suite.py create           # Create all test PRs
  python scripts/run_test_suite.py analyze [PR_NUM]  # Analyze a specific PR
  python scripts/run_test_suite.py report            # Full report from DB
  python scripts/run_test_suite.py all               # Create + wait + report
"""

from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

# ── Config ──────────────────────────────────────────────────────────────
REPO = "Wayne0607/ReviewForge"
BASE_BRANCH = "main"
REVIEW_FORGE_USER = "Wayne0607"
COPILOT_USER = "copilot-pull-request-reviewer[bot]"

# Remote DB config (set via env or .reviewforge_remote file)
REMOTE_DB_HOST = os.environ.get("REVIEWFORGE_DB_HOST", "")
REMOTE_DB_USER = os.environ.get("REVIEWFORGE_DB_USER", "root")
REMOTE_DB_PASS = os.environ.get("REVIEWFORGE_DB_PASS", "")
REMOTE_DB_PATH = os.environ.get("REVIEWFORGE_DB_PATH", "/opt/reviewforge/backend/.reviewforge/reviewforge.db")

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def get_token() -> str:
    token = os.environ.get("GH_PAT", "").strip()
    if not token:
        token_file = Path(__file__).parent / ".gh_token"
        if token_file.exists():
            token = token_file.read_text().strip()
    if not token:
        print("Set GH_PAT env var or create scripts/.gh_token", file=sys.stderr)
        sys.exit(1)
    return token


TOKEN = get_token()
HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {TOKEN}",
}


# ── Remote DB Access ───────────────────────────────────────────────────


_remote_conn = None


def get_remote_db_conn():
    """Get or create a paramiko connection to the remote DB server."""
    global _remote_conn
    if _remote_conn is not None:
        return _remote_conn
    if not REMOTE_DB_HOST:
        return None
    try:
        import paramiko

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(REMOTE_DB_HOST, username=REMOTE_DB_USER, password=REMOTE_DB_PASS, timeout=10)
        _remote_conn = ssh
        return ssh
    except Exception as e:
        print(f"  Warning: Could not connect to remote DB: {e}")
        return None


def query_remote_db(sql: str) -> list[tuple]:
    """Execute SQL on the remote DB and return rows."""
    ssh = get_remote_db_conn()
    if not ssh:
        return []
    import shlex

    cmd = f"python3 -c \"import sqlite3,json; conn=sqlite3.connect('{REMOTE_DB_PATH}'); cur=conn.cursor(); cur.execute({repr(sql)}); print(json.dumps(cur.fetchall()))\""
    _, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    if err and not out:
        print(f"  DB query error: {err[:200]}")
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def get_findings_from_db(pr_number: int) -> list[dict] | None:
    """Get findings for a PR from the remote DB. Returns None if unavailable."""
    rows = query_remote_db(
        "SELECT f.file, f.line, f.category, f.severity, f.confidence, f.status, f.message, f.reviewer "
        f"FROM review_findings f JOIN review_runs r ON f.run_id = r.run_id "
        f"WHERE r.pr_number = {pr_number} ORDER BY f.file, f.line"
    )
    if not rows:
        return None
    findings = []
    for row in rows:
        findings.append({
            "file": row[0],
            "line": row[1],
            "category": row[2],
            "severity": row[3],
            "confidence": row[4],
            "status": row[5],
            "message": row[6][:200] if row[6] else "",
            "reviewer": row[7] or "",
        })
    return findings


# ── Test Suite Definitions ─────────────────────────────────────────────


@dataclass
class TestPR:
    """Definition of a single test PR."""

    branch: str
    title: str
    fixture_dir: str  # relative to repo root
    description: str
    metrics: dict = field(default_factory=dict)  # expected bug counts by file
    expected_skills: dict = field(default_factory=dict)  # file -> expected skill
    planted_bugs: int = 0  # total known bugs planted
    test_focus: str = ""  # what this PR tests


# ---- PR 1: Multi-Language Detection Accuracy ----
PR1_LANG_DETECT = TestPR(
    branch="test/lang-detection-accuracy",
    title="[TEST] Multi-Language Detection & Skill Routing Accuracy",
    fixture_dir="test_fixtures/lang_detect",
    test_focus="language_detection",
    planted_bugs=49,  # actual count from BUG: markers in fixtures
    expected_skills={
        "test_fixtures/lang_detect/go_handler.go": "go_best_practices",
        "test_fixtures/lang_detect/rs_validator.rs": "rust_best_practices",
        "test_fixtures/lang_detect/rb_worker.rb": "ruby_best_practices",
        "test_fixtures/lang_detect/py_importer.py": "python_best_practices",
        "test_fixtures/lang_detect/java_repo.java": "java_best_practices",
        "test_fixtures/lang_detect/LoginForm.vue": "vue_patterns",
        "test_fixtures/lang_detect/Dashboard.tsx": "react_patterns",
        "test_fixtures/lang_detect/Widget.svelte": "svelte_patterns",
    },
    description="""## Multi-Language Detection & Skill Routing Accuracy

This PR adds 8 files across 8 language/framework combinations. Each file contains
language-specific bugs that should trigger the correct skill assignment.

### Test Matrix

| # | File | Language | Framework | Expected Skill | Planted Bugs |
|---|------|----------|-----------|----------------|-------------|
| 1 | go_handler.go | Go | - | go_best_practices | SQLi, cmd injection, hardcoded secret, goroutine leak, error ignored |
| 2 | rs_validator.rs | Rust | - | rust_best_practices | path traversal, cmd injection, unsafe, unwrap, panic, hardcoded secret |
| 3 | rb_worker.rb | Ruby | - | ruby_best_practices | eval, cmd injection (backticks/system/Open3), YAML.load, rescue Exception, method_missing |
| 4 | py_importer.py | Python | - | python_best_practices | SQLi, pickle, yaml.load, os.popen, eval, hardcoded secret |
| 5 | java_repo.java | Java | - | java_best_practices | SQLi, Runtime.exec, insecure deserialization, path traversal, resource leak, hardcoded secret |
| 6 | LoginForm.vue | TS | vue | vue_patterns | v-html XSS, open redirect, hardcoded secret, computed side-effect, memory leak |
| 7 | Dashboard.tsx | TS | react | react_patterns | dangerouslySetInnerHTML, eval, open redirect, hardcoded secret |
| 8 | Widget.svelte | TS | svelte | svelte_patterns | @html XSS, eval, hardcoded secret |

### Success Criteria
- [ ] Language detection routes correct skill for each file
- [ ] All 8 skills (6 language + 2 framework) are invoked
- [ ] Each file gets language-specific findings (not generic ones)
- [ ] No language misidentification errors
""",
)

# ---- PR 2: Security Vulnerability Coverage ----
PR2_SECURITY = TestPR(
    branch="test/security-vuln-coverage",
    title="[TEST] Security Vulnerability Spectrum Coverage",
    fixture_dir="test_fixtures/security_vuln",
    test_focus="security_coverage",
    planted_bugs=33,  # actual count from BUG: markers in fixtures
    description="""## Security Vulnerability Coverage Test

This PR contains files with focused security vulnerabilities across all major categories.
The goal is to verify the security_reviewer catches every type of vulnerability.

### Vulnerability Matrix

| Category | Python | Go | Java | TS/React | Total |
|----------|--------|----|------|----------|-------|
| SQL Injection | 4 variants | 3 variants | 3 variants | - | 10 |
| XSS | - | - | - | 6 variants | 6 |
| Command Injection | 5 variants | - | - | - | 5 |
| Deserialization | 4 variants | - | 1 variant | - | 5 |
| Path Traversal | 2 variants | - | - | - | 2 |
| SSRF | 1 variant | - | - | - | 1 |
| Hardcoded Secrets | 6 instances | - | - | - | 6 |
| Weak Cryptography | 2 variants | - | - | - | 2 |
| Code Injection | 2 variants | - | - | - | 2 |
| Open Redirect | - | - | - | 2 variants | 2 |

### Success Criteria
- [ ] All 10 vulnerability categories detected
- [ ] No category completely missed
- [ ] SQL injection detection includes f-string, .format(), concat, %-formatting
- [ ] XSS detection covers React, Vue, vanilla JS, Svelte patterns
- [ ] Command injection covers os.system, os.popen, subprocess(shell=True), subprocess.Popen
- [ ] Deserialization covers pickle, yaml.load, Marshal.load, ObjectInputStream
""",
)

# ---- PR 3: False Positive Resistance ----
PR3_FALSE_POS = TestPR(
    branch="test/false-positive-resistance",
    title="[TEST] False Positive Resistance & Edge Cases",
    fixture_dir="test_fixtures/false_pos_ctrl",
    test_focus="false_positive_control",
    planted_bugs=0,  # Should be ZERO - all code is safe
    description="""## False Positive Resistance Test

This PR contains code that LOOKS dangerous but is actually SAFE.
The goal is to measure the false positive rate — NONE of these should produce findings.

### Safe Patterns Tested

| File | Safe Pattern | Why It Could Be Misflagged |
|------|-------------|---------------------------|
| safe_sql.py | Parameterized queries (?, :name) | Uses "SELECT" strings that regex might match |
| safe_subprocess.py | List-arg subprocess (no shell=True) | Contains "subprocess.run" which is flagged when shell=True |
| safe_frontend.tsx | textContent, React default escaping, whitelist redirect | Contains "innerHTML" and "window.location" in safe contexts |
| test_code.py | eval/exec in test functions, test secrets | Contains "eval()" and "exec()" and "sk-test-" keys |
| safe_rust.rs | unwrap in #[test], SAFETY-commented unsafe, expect() | Contains "unwrap()" and "unsafe" blocks |
| config_template.yaml | Environment variable placeholders | Contains "password:" and "secret:" keys |

### Success Criteria
- [ ] ZERO findings on safe_sql.py (parameterized queries)
- [ ] ZERO findings on safe_subprocess.py (list-arg subprocess)
- [ ] ZERO findings on test_code.py (test-only code)
- [ ] ZERO findings on safe_rust.rs (idiomatic safe Rust)
- [ ] ZERO findings on config_template.yaml (template values)
- [ ] Overall FP rate: 0%
""",
)

# ---- PR 4: Cross-PR Risk Propagation ----
PR4_CROSS_PR_PHASE1 = TestPR(
    branch="test/cross-pr-phase-1",
    title="[TEST] Cross-PR Detection Phase 1 — Risky Auth Module",
    fixture_dir="test_fixtures/cross_pr/phase1",
    test_focus="cross_pr_phase1",
    planted_bugs=6,
    description="""## Cross-PR Detection — Phase 1: Risky Auth Module

This PR introduces a new `auth_provider.py` module with intentionally vulnerable code.
Phase 2 will import and use this module, and the cross-PR analyzer should detect the risk chain.

### Planted Risks in auth_provider.py
1. Hardcoded SECRET_KEY
2. Weak hashing (MD5 for passwords)
3. Insecure pickle deserialization of sessions
4. Predictable token generation
5. Path traversal in SessionStore

### Success Criteria (Phase 1)
- [ ] All 5 bugs detected by security_reviewer
- [ ] Symbols registered in code_symbols table
- [ ] File risk summary created for auth_provider.py
""",
)

PR4_CROSS_PR_PHASE2 = TestPR(
    branch="test/cross-pr-phase-2",
    title="[TEST] Cross-PR Detection Phase 2 — Import Risky Module",
    fixture_dir="test_fixtures/cross_pr/phase2",
    test_focus="cross_pr_phase2",
    planted_bugs=1,
    description="""## Cross-PR Detection — Phase 2: Import Risky Module

This PR adds `login_handler.py` which imports `LegacyAuthProvider` from the module
introduced in Phase 1. The cross-PR analyzer should detect:
1. The import chain: login_handler.py → auth_provider.LegacyAuthProvider
2. Risk propagation: insecure deserialization, weak crypto, hardcoded secret

### Planted Risks in login_handler.py
1. Uses LegacyAuthProvider.deserialize_session() — inherits insecure pickle risk
2. Uses LegacyAuthProvider.generate_token() — inherits weak crypto risk
3. Additionally: pickle.dumps of session data

### Success Criteria (Phase 2)
- [ ] Cross-PR analyzer detects import chain
- [ ] At least 1 cross-pr-* category finding generated
- [ ] Risk propagation correctly attributed to auth_provider
- [ ] Cross-PR findings have category = "cross-pr-insecure-deserialization" or similar
""",
)

# ---- PR 5: Token Scaling Benchmark ----
PR5_TOKEN_SMALL = TestPR(
    branch="test/token-benchmark-small",
    title="[TEST] Token Benchmark — Small (1 file)",
    fixture_dir="test_fixtures/token_scale/small",
    test_focus="token_scaling",
    planted_bugs=0,  # No bugs planted — measures baseline token cost
    description="""## Token Scaling Benchmark — Small PR (1 file)

Baseline measurement: a PR with 1 file, 0 planted bugs.
Measures the minimum token cost of running the review pipeline.

### Success Criteria
- [ ] Review completes successfully
- [ ] Token usage recorded per agent (planner, reviewer, calibrator)
- [ ] Baseline tokens per file established
""",
)

PR5_TOKEN_MEDIUM = TestPR(
    branch="test/token-benchmark-medium",
    title="[TEST] Token Benchmark — Medium (3 files, Python)",
    fixture_dir="test_fixtures/token_scale/medium",
    test_focus="token_scaling",
    planted_bugs=9,  # across 3 files
    description="""## Token Scaling Benchmark — Medium PR (3 files)

Measures token cost scaling: 3 Python files with 7 total bugs.

### Success Criteria
- [ ] Token consumption < 3× the small PR baseline
- [ ] Cost per finding calculated
- [ ] Per-file overhead measured
""",
)

PR5_TOKEN_LARGE = TestPR(
    branch="test/token-benchmark-large",
    title="[TEST] Token Benchmark — Large (8 files, 6 languages)",
    fixture_dir="test_fixtures/token_scale/large",
    test_focus="token_scaling",
    planted_bugs=35,  # across 8 files in 6 languages
    description="""## Token Scaling Benchmark — Large PR (8 files, 6 languages)

Measures token cost at scale with mixed languages.

### Files
| # | File | Language | Bugs |
|---|------|----------|------|
| 1 | api_router.py | Python | 2 |
| 2 | data_exporter.py | Python | 3 |
| 3 | email_service.go | Go | 2 |
| 4 | cache_manager.rs | Rust | 4 |
| 5 | logger_service.rb | Ruby | 4 |
| 6 | AdminPanel.vue | Vue | 3 |
| 7 | notification_handler.java | Java | 3 |
| 8 | report_generator.py | Python | 4 |

### Success Criteria
- [ ] All 6 languages detected
- [ ] Token consumption scales sub-linearly with file count
- [ ] Cost per file decreases with more files (shared context)
- [ ] Multi-language overhead measured
""",
)

# ── All Test PRs in order ──────────────────────────────────────────────
ALL_TEST_PRS = [
    PR1_LANG_DETECT,
    PR2_SECURITY,
    PR3_FALSE_POS,
    PR4_CROSS_PR_PHASE1,
    PR4_CROSS_PR_PHASE2,
    PR5_TOKEN_SMALL,
    PR5_TOKEN_MEDIUM,
    PR5_TOKEN_LARGE,
]


# ── GitHub API Helpers ─────────────────────────────────────────────────


def gh_api(method: str, path: str, **kwargs) -> Any:
    """Call GitHub API."""
    url = f"https://api.github.com{path}"
    r = requests.request(method, url, headers=HEADERS, timeout=30, **kwargs)
    if r.status_code >= 400:
        print(f"  GitHub API error {r.status_code}: {r.text[:200]}")
        return None
    return r.json() if r.text else {}


def get_default_branch_sha() -> str:
    """Get the SHA of the default branch."""
    data = gh_api("GET", f"/repos/{REPO}/git/refs/heads/{BASE_BRANCH}")
    if data:
        return data["object"]["sha"]
    return ""


def create_branch(branch_name: str, base_sha: str) -> bool:
    """Create a new branch."""
    result = gh_api(
        "POST",
        f"/repos/{REPO}/git/refs",
        json={"ref": f"refs/heads/{branch_name}", "sha": base_sha},
    )
    return result is not None


def delete_branch(branch_name: str) -> bool:
    """Delete a branch (for cleanup)."""
    result = gh_api("DELETE", f"/repos/{REPO}/git/refs/heads/{branch_name}")
    return result is not None or True  # 204 returns no content


def create_or_update_file(
    branch: str, file_path: str, content: str, message: str = ""
) -> bool:
    """Create or update a file on a branch."""
    # Check if file exists
    existing = gh_api(
        "GET",
        f"/repos/{REPO}/contents/{file_path}?ref={branch}",
    )
    payload = {
        "message": message or f"test: add {file_path}",
        "content": content,
        "branch": branch,
    }
    if existing and "sha" in existing:
        payload["sha"] = existing["sha"]
        payload["message"] = f"test: update {file_path}"

    result = gh_api("PUT", f"/repos/{REPO}/contents/{file_path}", json=payload)
    return result is not None


def create_pr(pr: TestPR) -> int | None:
    """Create a PR from a test definition. Returns PR number or None."""
    data = {
        "title": pr.title,
        "head": pr.branch,
        "base": BASE_BRANCH,
        "body": pr.description,
    }
    result = gh_api("POST", f"/repos/{REPO}/pulls", json=data)
    if result:
        pr_num = result["number"]
        print(f"  Created PR #{pr_num}: {result['html_url']}")
        return pr_num
    return None


def get_pr_comments(pr_number: int) -> list[dict]:
    """Get all inline review comments on a PR."""
    return gh_api("GET", f"/repos/{REPO}/pulls/{pr_number}/comments") or []


def get_pr_reviews(pr_number: int) -> list[dict]:
    """Get all reviews on a PR."""
    return gh_api("GET", f"/repos/{REPO}/pulls/{pr_number}/reviews") or []


# ── PR Creation ────────────────────────────────────────────────────────


def create_all_test_prs(dry_run: bool = False) -> dict[str, int]:
    """Create all test PRs. Returns mapping of branch -> PR number."""
    base_sha = get_default_branch_sha()
    if not base_sha:
        print("ERROR: Could not get base branch SHA")
        return {}

    print(f"Base SHA: {base_sha[:8]}")
    results: dict[str, int] = {}

    for i, pr_def in enumerate(ALL_TEST_PRS):
        print(f"\n{'=' * 60}")
        print(f"[{i + 1}/{len(ALL_TEST_PRS)}] {pr_def.title}")
        print(f"  Branch: {pr_def.branch}")
        print(f"  Fixtures: {pr_def.fixture_dir}")
        print(f"  Planted bugs: {pr_def.planted_bugs}")

        if dry_run:
            print("  [DRY RUN] Would create branch and PR")
            continue

        # 1. Create branch
        if not create_branch(pr_def.branch, base_sha):
            print(f"  Branch may already exist, continuing...")

        # 2. Upload fixture files
        fixture_dir = Path(pr_def.fixture_dir)
        if fixture_dir.exists():
            files = list(fixture_dir.rglob("*"))
            code_files = [
                f for f in files
                if f.is_file()
                and f.suffix in (
                    ".py", ".go", ".java", ".rs", ".rb", ".vue",
                    ".tsx", ".jsx", ".ts", ".js", ".svelte", ".yaml", ".yml",
                )
            ]
            print(f"  Uploading {len(code_files)} files...")
            for fpath in code_files:
                fpath_resolved = fpath.resolve()
                cwd_resolved = Path.cwd().resolve()
                rel_path = str(fpath_resolved.relative_to(cwd_resolved)).replace("\\", "/")
                content_b64 = fpath_resolved.read_bytes()
                import base64
                encoded = base64.b64encode(content_b64).decode()
                create_or_update_file(pr_def.branch, rel_path, encoded)
                print(f"    ✓ {rel_path}")
        else:
            print(f"  WARNING: Fixture dir {pr_def.fixture_dir} not found!")

        # 3. Create PR
        pr_num = create_pr(pr_def)
        if pr_num:
            results[pr_def.branch] = pr_num

    return results


# ── Analysis ───────────────────────────────────────────────────────────


@dataclass
class FindingSummary:
    """Parsed finding from a review comment."""

    file: str
    line: int
    severity: str  # 🔴/🟡/🔵
    category: str  # [category]
    confidence: float
    reviewer: str
    message: str
    suggestion: str


def parse_finding_comment(comment: dict) -> FindingSummary | None:
    """Parse a ReviewForge comment into a FindingSummary."""
    body = comment.get("body", "")
    author = comment.get("user", {}).get("login", "")
    if author != REVIEW_FORGE_USER:
        return None

    sev_match = re.search(r"(🔴|🟡|🔵|⚪)", body)
    cat_match = re.search(r"\[(.*?)\]", body)
    conf_match = re.search(r"置信度:\s*(\d+)%", body)
    # Message is between the first line and "**建议:**"
    msg_match = re.search(r"\)\n\n(.*?)\n\n\*\*建议", body, re.DOTALL)
    sug_match = re.search(r"\*\*建议:\*\*\s*(.*?)(?:\n\n|$)", body, re.DOTALL)
    reviewer_match = re.search(r"ReviewForge • (.*?)</sub>", body)

    return FindingSummary(
        file=comment["path"],
        line=comment.get("line", comment.get("start_line", 0)),
        severity=sev_match.group(0) if sev_match else "?",
        category=cat_match.group(1) if cat_match else "?",
        confidence=int(conf_match.group(1)) / 100.0 if conf_match else 0.0,
        reviewer=reviewer_match.group(1) if reviewer_match else "",
        message=msg_match.group(1).strip() if msg_match else body[:200],
        suggestion=sug_match.group(1).strip() if sug_match else "",
    )


def analyze_pr(pr_number: int) -> dict:
    """Analyze a single PR's review results.

    Priority: remote DB → local DB → GitHub comments.
    """
    # Try remote DB first
    db_findings = get_findings_from_db(pr_number)
    if db_findings is not None:
        # Build FindingSummary-like dicts from DB rows
        class _DBFinding:
            __slots__ = ("file", "line", "severity", "category", "confidence", "reviewer", "message", "status")
            def __init__(self, d):
                self.file = d["file"]
                self.line = d["line"]
                self.severity = d["severity"]
                self.category = d["category"]
                self.confidence = d["confidence"]
                self.reviewer = d["reviewer"]
                self.message = d["message"]
                self.status = d["status"]

        findings = [_DBFinding(d) for d in db_findings]
        confirmed = [f for f in findings if f.status == "confirmed"]
        fp = [f for f in findings if f.status == "false_positive"]

        sev_counts = Counter(f.severity for f in confirmed)
        cat_counts = Counter(f.category for f in confirmed)
        files_covered = set(f.file for f in confirmed)
        avg_confidence = sum(f.confidence for f in confirmed) / max(len(confirmed), 1)

        by_file = defaultdict(list)
        for f in confirmed:
            by_file[f.file].append(f)

        comments = get_pr_comments(pr_number)
        cp_comments = [c for c in comments if c.get("user", {}).get("login", "") == COPILOT_USER]

        return {
            "pr_number": pr_number,
            "total_comments": len(comments),
            "rf_findings": len(confirmed),
            "rf_total": len(findings),
            "rf_fp": len(fp),
            "cp_findings": len(cp_comments),
            "severity_dist": dict(sev_counts),
            "category_dist": dict(cat_counts),
            "files_covered": len(files_covered),
            "avg_confidence": avg_confidence,
            "by_file": {k: len(v) for k, v in by_file.items()},
            "findings": confirmed,
            "source": "database",
        }

    # Fallback: GitHub comments
    comments = get_pr_comments(pr_number)
    reviews = get_pr_reviews(pr_number)
    if not comments:
        print(f"  No comments found on PR #{pr_number}")
        return {}

    # Group by author
    by_author = defaultdict(list)
    for c in comments:
        by_author[c.get("user", {}).get("login", "?")].append(c)

    rf_comments = by_author.get(REVIEW_FORGE_USER, [])
    cp_comments = by_author.get(COPILOT_USER, [])

    # Parse ReviewForge findings
    findings = [f for c in rf_comments if (f := parse_finding_comment(c))]

    # Stats
    sev_counts = Counter(f.severity for f in findings)
    cat_counts = Counter(f.category for f in findings)
    files_covered = set(f.file for f in findings)
    avg_confidence = sum(f.confidence for f in findings) / max(len(findings), 1)

    # Per-file breakdown
    by_file = defaultdict(list)
    for f in findings:
        by_file[f.file].append(f)

    return {
        "pr_number": pr_number,
        "total_comments": len(comments),
        "rf_findings": len(findings),
        "rf_total": len(findings),
        "rf_fp": 0,
        "cp_findings": len(cp_comments),
        "severity_dist": dict(sev_counts),
        "category_dist": dict(cat_counts),
        "files_covered": len(files_covered),
        "avg_confidence": avg_confidence,
        "by_file": {k: len(v) for k, v in by_file.items()},
        "findings": findings,
        "source": "github_comments",
    }


@dataclass
class TestResult:
    """Aggregated test result for one test PR."""

    pr_def: TestPR
    pr_number: int | None = None
    analysis: dict = field(default_factory=dict)
    bugs_detected: int = 0
    bugs_missed: int = 0
    false_positives: int = 0
    detection_rate: float = 0.0
    tokens_used: dict = field(default_factory=dict)
    skill_accuracy: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


def calculate_tokens_from_db(db_path: str | None = None) -> dict:
    """Read token usage from ReviewForge SQLite database."""
    if db_path is None:
        db_path = ".reviewforge/reviewforge.db"
    if not Path(db_path).exists():
        return {"error": f"DB not found at {db_path}"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    result = {
        "by_agent": {},
        "by_run": {},
        "total_prompt": 0,
        "total_completion": 0,
        "total_tokens": 0,
    }

    # Token usage by agent
    for row in conn.execute(
        """
        SELECT agent_name,
               SUM(prompt_tokens) as prompt,
               SUM(completion_tokens) as completion,
               SUM(total_tokens) as total,
               COUNT(*) as calls,
               AVG(total_tokens) as avg_per_call
        FROM token_usage
        GROUP BY agent_name
        ORDER BY total DESC
    """
    ).fetchall():
        result["by_agent"][row["agent_name"]] = {
            "prompt_tokens": row["prompt"],
            "completion_tokens": row["completion"],
            "total_tokens": row["total"],
            "call_count": row["calls"],
            "avg_per_call": round(row["avg_per_call"], 1),
        }
        result["total_prompt"] += row["prompt"]
        result["total_completion"] += row["completion"]
        result["total_tokens"] += row["total"]

    # Token by run
    for row in conn.execute(
        """
        SELECT t.run_id, r.repo, r.pr_number,
               SUM(t.total_tokens) as total,
               COUNT(*) as calls
        FROM token_usage t
        JOIN review_runs r ON t.run_id = r.run_id
        GROUP BY t.run_id
        ORDER BY total DESC
        LIMIT 20
    """
    ).fetchall():
        result["by_run"][f"PR#{row['pr_number']}"] = {
            "run_id": row["run_id"],
            "total_tokens": row["total"],
            "call_count": row["calls"],
        }

    # Summary stats
    summary = conn.execute(
        """
        SELECT COUNT(DISTINCT run_id) as total_runs,
               COUNT(*) as total_findings,
               SUM(CASE WHEN status='confirmed' THEN 1 ELSE 0 END) as confirmed,
               SUM(CASE WHEN status='false_positive' THEN 1 ELSE 0 END) as fp,
               AVG(confidence) as avg_conf
        FROM review_findings
    """
    ).fetchone()
    if summary:
        result["summary"] = {k: summary[k] for k in summary.keys()}

    conn.close()
    return result


# ── Report Generation ──────────────────────────────────────────────────


def print_separator(char: str = "=", width: int = 80) -> None:
    print(char * width)


def print_header(title: str) -> None:
    print_separator()
    print(f"  {title}")
    print_separator()


def generate_full_report(
    pr_results: dict[str, TestResult],
    db_tokens: dict | None = None,
) -> str:
    """Generate a comprehensive test report."""
    lines = []
    w = lines.append

    w("╔" + "═" * 78 + "╗")
    w("║" + " ReviewForge Comprehensive Test Suite Report".center(76) + "║")
    w("║" + f" Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}".center(76) + "║")
    w("╚" + "═" * 78 + "╝")
    w("")

    # ── 1. Executive Summary ──
    w("─" * 80)
    w("  1. EXECUTIVE SUMMARY")
    w("─" * 80)
    w("")

    total_planted = sum(r.pr_def.planted_bugs for r in pr_results.values())
    total_detected = sum(r.bugs_detected for r in pr_results.values())
    total_fp = sum(r.false_positives for r in pr_results.values())

    w(f"  Total Test PRs:        {len(pr_results)}")
    w(f"  Total Bugs Planted:    {total_planted}")
    w(f"  Total Bugs Detected:   {total_detected}")
    w(f"  Overall Detection Rate: {total_detected / max(total_planted, 1) * 100:.1f}%")
    w(f"  Total False Positives: {total_fp}")
    w(f"  Precision: {total_detected / max(total_detected + total_fp, 1) * 100:.1f}%")
    w("")

    # ── 2. Per-PR Results ──
    w("─" * 80)
    w("  2. PER-PR RESULTS")
    w("─" * 80)
    w("")

    for pr_def in ALL_TEST_PRS:
        result = pr_results.get(pr_def.branch)
        if not result or not result.pr_number:
            w(f"  {pr_def.branch}: NOT RUN")
            continue

        a = result.analysis
        source = a.get("source", "?")
        w(f"  PR #{result.pr_number}: {pr_def.test_focus}")
        w(f"    Branch: {pr_def.branch}")
        w(f"    Planted Bugs: {pr_def.planted_bugs}  |  Detected: {result.bugs_detected}  |  "
          f"Missed: {result.bugs_missed}  |  FP: {result.false_positives}")
        w(f"    RF Findings: {a.get('rf_findings', '?')}  |  "
          f"Copilot Findings: {a.get('cp_findings', '?')}  |  Source: {source}")
        w(f"    Avg Confidence: {a.get('avg_confidence', 0):.1%}")
        w(f"    Files Covered: {a.get('files_covered', '?')}")
        if a.get("by_file"):
            w(f"    Per-File Findings:")
            for fpath, count in sorted(a["by_file"].items()):
                short = fpath.replace("test_fixtures/", "")
                w(f"      {short}: {count}")
        if result.skill_accuracy:
            w(f"    Skill Routing:")
            for file_path, skill in result.skill_accuracy.items():
                short = file_path.replace("test_fixtures/", "")
                w(f"      {short} → {skill}")
        if result.notes:
            for note in result.notes:
                w(f"    ⚠ {note}")
        w("")

    # ── 3. Security Coverage Matrix ──
    w("─" * 80)
    w("  3. SECURITY VULNERABILITY COVERAGE")
    w("─" * 80)
    w("")

    # Gather all security findings across PRs
    all_categories = Counter()
    for result in pr_results.values():
        cats = result.analysis.get("category_dist", {})
        for cat, count in cats.items():
            all_categories[cat] += count

    w("  Detected Categories:")
    for cat, count in all_categories.most_common():
        bar = "█" * min(count, 20)
        w(f"    {cat:<30} {bar} {count}")

    # Expected security categories
    expected_cats = {
        "sql-injection", "command-injection", "xss", "code-injection",
        "insecure-deserialization", "hardcoded-secrets", "path-traversal",
        "unsafe-usage", "cross-pr-insecure-deserialization",
        "cross-pr-hardcoded-secrets",
    }
    detected_cats = set(all_categories.keys())
    missed_cats = expected_cats - detected_cats
    if missed_cats:
        w(f"\n  ⚠ MISSED CATEGORIES: {', '.join(sorted(missed_cats))}")
    else:
        w(f"\n  ✓ All expected security categories detected")
    w("")

    # ── 4. Language Detection Accuracy ──
    w("─" * 80)
    w("  4. LANGUAGE DETECTION & SKILL ROUTING")
    w("─" * 80)
    w("")

    lang_test = pr_results.get(PR1_LANG_DETECT.branch)
    if lang_test:
        w(f"  PR #{lang_test.pr_number}: {PR1_LANG_DETECT.branch}")
        expected = PR1_LANG_DETECT.expected_skills
        actual = lang_test.skill_accuracy
        w(f"  {'File':<45} {'Expected':<25} {'Actual':<25} {'Match'}")
        w(f"  {'─' * 43}  {'─' * 23}  {'─' * 23}  {'─────'}")
        match_count = 0
        for fpath, exp_skill in sorted(expected.items()):
            act_skill = actual.get(fpath, "?")
            match = "✓" if act_skill == exp_skill else "✗"
            if match == "✓":
                match_count += 1
            w(f"  {fpath:<45} {exp_skill:<25} {act_skill:<25} {match}")
        w(f"\n  Skill Routing Accuracy: {match_count}/{len(expected)} "
          f"({match_count / max(len(expected), 1) * 100:.0f}%)")
    w("")

    # ── 5. False Positive Analysis ──
    w("─" * 80)
    w("  5. FALSE POSITIVE ANALYSIS")
    w("─" * 80)
    w("")

    fp_test = pr_results.get(PR3_FALSE_POS.branch)
    if fp_test:
        a = fp_test.analysis
        w(f"  PR #{fp_test.pr_number}: {PR3_FALSE_POS.branch}")
        w(f"  Safe files tested: 6")
        w(f"  False positives found: {fp_test.false_positives}")
        if fp_test.false_positives == 0:
            w(f"  ✓ PERFECT — No false positives on safe code!")
        elif fp_test.false_positives <= 2:
            w(f"  ⚠ ACCEPTABLE — {fp_test.false_positives} false positives (within tolerance)")
        else:
            w(f"  ✗ NEEDS WORK — {fp_test.false_positives} false positives is too many")
        if a.get("by_file"):
            w(f"\n  False positives by file:")
            for fpath, count in sorted(a["by_file"].items()):
                short = fpath.replace("test_fixtures/false_pos_ctrl/", "")
                w(f"    {short}: {count} finding(s)")
    w("")

    # ── 6. Cross-PR Analysis ──
    w("─" * 80)
    w("  6. CROSS-PR DETECTION")
    w("─" * 80)
    w("")

    pr4_p1 = pr_results.get(PR4_CROSS_PR_PHASE1.branch)
    pr4_p2 = pr_results.get(PR4_CROSS_PR_PHASE2.branch)

    if pr4_p1:
        w(f"  Phase 1 PR #{pr4_p1.pr_number}: Risky auth module introduced")
        w(f"    Findings: {pr4_p1.bugs_detected} (expected {PR4_CROSS_PR_PHASE1.planted_bugs})")

    if pr4_p2:
        w(f"  Phase 2 PR #{pr4_p2.pr_number}: Import of risky module")
        w(f"    Findings: {pr4_p2.bugs_detected} (expected {PR4_CROSS_PR_PHASE2.planted_bugs})")

        # Check for cross-pr specific findings
        cross_pr_findings = [
            f for f in pr4_p2.analysis.get("findings", [])
            if f.category.startswith("cross-pr")
        ]
        if cross_pr_findings:
            w(f"    ✓ Cross-PR findings detected: {len(cross_pr_findings)}")
            for f in cross_pr_findings:
                w(f"      [{f.category}] {f.file}:{f.line} — {f.message[:100]}...")
        else:
            w(f"    ✗ No cross-PR specific findings detected!")
            w(f"    NOTE: Cross-PR analysis requires both PRs to be reviewed sequentially")
            w(f"    and the DB to persist code_graph data between runs.")
    w("")

    # ── 7. Token Consumption ──
    w("─" * 80)
    w("  7. TOKEN CONSUMPTION ANALYSIS")
    w("─" * 80)
    w("")

    if db_tokens and "error" not in db_tokens:
        w("  Token Usage by Agent:")
        for agent, data in sorted(db_tokens.get("by_agent", {}).items()):
            w(f"    {agent:<25} {data['total_tokens']:>8,} tokens  "
              f"({data['call_count']} calls, avg {data['avg_per_call']:,.0f}/call)")
        w(f"\n  Total Tokens: {db_tokens.get('total_tokens', 0):,}")
        w(f"  Total Prompt: {db_tokens.get('total_prompt', 0):,}")
        w(f"  Total Completion: {db_tokens.get('total_completion', 0):,}")

        # Per-run breakdown
        w(f"\n  Token Usage by PR:")
        for run_name, data in sorted(
            db_tokens.get("by_run", {}).items(),
            key=lambda x: x[1].get("total_tokens", 0),
            reverse=True,
        ):
            w(f"    {run_name:<20} {data['total_tokens']:>8,} tokens  "
              f"({data['call_count']} calls)")

        # Scaling analysis
        token_runs = db_tokens.get("by_run", {})
        small_tokens = 0
        medium_tokens = 0
        large_tokens = 0
        for name, data in token_runs.items():
            if "small" in name.lower() or "token-benchmark-small" in name.lower():
                small_tokens = data.get("total_tokens", 0)
            elif "medium" in name.lower() or "token-benchmark-medium" in name.lower():
                medium_tokens = data.get("total_tokens", 0)
            elif "large" in name.lower() or "token-benchmark-large" in name.lower():
                large_tokens = data.get("total_tokens", 0)

        if small_tokens and medium_tokens and large_tokens:
            w(f"\n  Token Scaling (Small → Medium → Large):")
            w(f"    Small  (1 file):  {small_tokens:>8,} tokens")
            w(f"    Medium (3 files): {medium_tokens:>8,} tokens  "
              f"({medium_tokens / max(small_tokens, 1):.1f}× small)")
            w(f"    Large  (8 files): {large_tokens:>8,} tokens  "
              f"({large_tokens / max(small_tokens, 1):.1f}× small)")
            w(f"    Per-file cost (small):  {small_tokens:,} tokens")
            w(f"    Per-file cost (medium): {medium_tokens / 3:,.0f} tokens")
            w(f"    Per-file cost (large):  {large_tokens / 8:,.0f} tokens")
    else:
        w("  Token data not available (DB not accessible)")
        if db_tokens and "error" in db_tokens:
            w(f"  Error: {db_tokens['error']}")
    w("")

    # ── 8. Comparison with Copilot ──
    w("─" * 80)
    w("  8. COMPARISON WITH GITHUB COPILOT")
    w("─" * 80)
    w("")

    total_rf = 0
    total_cp = 0
    for result in pr_results.values():
        a = result.analysis
        total_rf += a.get("rf_findings", 0)
        total_cp += a.get("cp_findings", 0)

    w(f"  ReviewForge total findings: {total_rf}")
    w(f"  Copilot total findings:     {total_cp}")
    ratio = total_rf / max(total_cp, 1)
    w(f"  RF:CP Ratio: {total_rf}:{total_cp} ({ratio:.1f}×)")
    w("")

    # ── 9. Recommendations ──
    w("─" * 80)
    w("  9. RECOMMENDATIONS & NEXT STEPS")
    w("─" * 80)
    w("")

    issues = []
    for result in pr_results.values():
        if result.detection_rate < 0.5 and result.pr_def.planted_bugs > 0:
            issues.append(
                f"Low detection rate on {result.pr_def.test_focus} "
                f"({result.detection_rate:.0%})"
            )
        if result.false_positives > 2:
            issues.append(
                f"High FP count on {result.pr_def.test_focus} "
                f"({result.false_positives} false positives)"
            )

    if issues:
        w("  Issues Found:")
        for i, issue in enumerate(issues, 1):
            w(f"    {i}. {issue}")
    else:
        w("  ✓ No critical issues found. All tests passed within expectations.")

    w("")
    w("  Suggested Follow-up Tests:")
    w("    1. Concurrency stress test (multiple simultaneous PRs)")
    w("    2. Large diff test (100+ line changes)")
    w("    3. Non-English code review test (中文注释, 日本語)")
    w("    4. Framework version upgrade impact test")
    w("    5. Model comparison (gpt-4o vs claude vs others)")
    w("")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────


def cmd_create(dry_run: bool = False) -> None:
    """Create all test PRs."""
    print_header("CREATING TEST PRs")
    results = create_all_test_prs(dry_run)
    if results:
        print(f"\nCreated {len(results)} PRs:")
        for branch, num in results.items():
            print(f"  {branch} → PR #{num}")


def cmd_analyze(args: list[str]) -> None:
    """Analyze a specific PR."""
    pr_num = int(args[0]) if args else None
    if not pr_num:
        # Find latest test PR
        prs = gh_api("GET", f"/repos/{REPO}/pulls?state=open&per_page=20") or []
        test_prs = [
            p for p in prs
            if p.get("head", {}).get("ref", "").startswith("test/")
        ]
        if not test_prs:
            print("No open test PRs found.")
            return
        pr_num = test_prs[0]["number"]
        print(f"Analyzing latest test PR: #{pr_num}")

    print_header(f"ANALYZING PR #{pr_num}")
    result = analyze_pr(pr_num)

    if not result:
        print("No data to analyze.")
        return

    print(f"\n  Total Comments: {result['total_comments']}")
    print(f"  ReviewForge Findings: {result['rf_findings']}")
    print(f"  Copilot Findings: {result['cp_findings']}")
    print(f"  Avg Confidence: {result['avg_confidence']:.1%}")
    print(f"  Files Covered: {result['files_covered']}")

    print(f"\n  Severity Distribution:")
    for sev, count in result.get("severity_dist", {}).items():
        print(f"    {sev}: {count}")

    print(f"\n  Category Distribution:")
    for cat, count in sorted(
        result.get("category_dist", {}).items(), key=lambda x: -x[1]
    ):
        print(f"    [{cat}]: {count}")

    print(f"\n  Per-File Findings:")
    for fpath, count in sorted(result.get("by_file", {}).items()):
        short = fpath.replace("test_fixtures/", "")
        print(f"    {short}: {count}")


def cmd_report() -> None:
    """Generate comprehensive report."""
    print_header("GENERATING COMPREHENSIVE REPORT")

    # Collect PR results
    pr_results: dict[str, TestResult] = {}

    # Find all open test PRs
    prs = gh_api("GET", f"/repos/{REPO}/pulls?state=open&per_page=30") or []
    test_pr_map = {}
    for p in prs:
        branch = p.get("head", {}).get("ref", "")
        if branch.startswith("test/"):
            test_pr_map[branch] = p["number"]

    print(f"Found {len(test_pr_map)} test PRs open")

    for pr_def in ALL_TEST_PRS:
        pr_num = test_pr_map.get(pr_def.branch)
        result = TestResult(pr_def=pr_def, pr_number=pr_num)

        if pr_num:
            print(f"  Analyzing PR #{pr_num} ({pr_def.test_focus})...")
            analysis = analyze_pr(pr_num)
            result.analysis = analysis

            # Calculate detection metrics — use DB FP count when available
            rf_count = analysis.get("rf_findings", 0)
            rf_fp = analysis.get("rf_fp", 0)
            if pr_def.planted_bugs > 0:
                result.bugs_detected = min(rf_count, pr_def.planted_bugs)
                result.bugs_missed = max(0, pr_def.planted_bugs - rf_count)
                result.detection_rate = rf_count / pr_def.planted_bugs
                result.false_positives = rf_fp
            else:
                # This PR should have ZERO findings (false positive test)
                result.false_positives = rf_count if rf_fp == 0 else rf_fp
                result.detection_rate = 1.0 if rf_count == 0 else 0.0
                if rf_count > 0:
                    result.notes.append(
                        f"Expected 0 findings, got {rf_count} findings ({rf_fp} FP)!"
                    )

            # Check if findings exist for each expected file
            files_found = set(analysis.get("by_file", {}).keys())
            expected_files = set(pr_def.expected_skills.keys())
            missing_files = expected_files - files_found
            if missing_files:
                result.notes.append(
                    f"No findings for: {', '.join(missing_files)}"
                )

        pr_results[pr_def.branch] = result

    # Read token data from DB
    db_tokens = calculate_tokens_from_db()

    # Generate report
    report = generate_full_report(pr_results, db_tokens)
    print("\n" + report)

    # Save report to file
    report_path = Path("test_report.md")
    report_path.write_text(report, encoding="utf-8")
    print(f"\nReport saved to {report_path}")


def cmd_all() -> None:
    """Create PRs and generate report."""
    cmd_create()
    print("\n" + "=" * 80)
    print("  PRs created! Waiting for review to complete...")
    print("  Run 'python scripts/run_test_suite.py report' after reviews finish.")
    print("=" * 80)


# ── CLI ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("Commands: create, analyze [PR_NUM], report, all")
        sys.exit(1)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "create":
        cmd_create(dry_run="--dry-run" in args)
    elif cmd == "analyze":
        cmd_analyze(args)
    elif cmd == "report":
        cmd_report()
    elif cmd == "all":
        cmd_all()
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: create, analyze [PR_NUM], report, all")
        sys.exit(1)
