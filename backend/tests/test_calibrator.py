import json
from types import SimpleNamespace

from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding
from reviewforge.engine.calibrator import DynamicCalibrator, apply_actionability_gate
from reviewforge.engine.prompt import build_reviewer_prompt


class FailingLLM:
    async def ainvoke(self, _messages):
        raise AssertionError("high-confidence deterministic finding should not call the LLM")


class RejectingLLM:
    def __init__(self, finding_id: str):
        self.finding_id = finding_id
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        if self.calls == 1:
            payload = [
                {
                    "finding_id": self.finding_id,
                    "verdict": "false_positive",
                    "adjusted_confidence": 0.1,
                    "challenge": "上下文证明输入经过 allow-list。",
                }
            ]
        else:
            payload = [
                {
                    "finding_id": self.finding_id,
                    "verdict": "false_positive",
                    "confidence": 0.1,
                    "reason": "没有攻击者可控输入到危险 sink。",
                }
            ]
        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))


class PromptCaptureLLM:
    def __init__(self, finding_id: str):
        self.finding_id = finding_id
        self.system_prompts: list[str] = []

    async def ainvoke(self, messages):
        self.system_prompts.append(str(messages[0].content))
        if len(self.system_prompts) == 1:
            payload = [
                {
                    "finding_id": self.finding_id,
                    "verdict": "confirmed",
                    "adjusted_confidence": 0.8,
                    "challenge": "存在明确证据。",
                }
            ]
        else:
            payload = [
                {
                    "finding_id": self.finding_id,
                    "verdict": "confirmed",
                    "confidence": 0.8,
                    "reason": "最终确认。",
                }
            ]
        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))


class ConfirmingBatchLLM:
    def __init__(self, finding_ids: list[str]):
        self.finding_ids = finding_ids
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        if self.calls == 1:
            payload = [
                {
                    "finding_id": finding_id,
                    "verdict": "confirmed",
                    "adjusted_confidence": 0.9,
                    "challenge": "存在动态 source 到 shell/path sink。",
                }
                for finding_id in self.finding_ids
            ]
        else:
            payload = [
                {
                    "finding_id": finding_id,
                    "verdict": "confirmed",
                    "confidence": 0.9,
                    "reason": "动态数据进入危险 sink。",
                }
                for finding_id in self.finding_ids
            ]
        return SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))


def _summary(file_path: str, content: str) -> str:
    lines = content.splitlines()
    patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)
    return f"--- {file_path} (+{len(lines)} -0)\n{patch}"


async def test_high_confidence_detector_security_auto_confirms():
    calibrator = DynamicCalibrator(FailingLLM(), build_registry())
    finding = Finding(
        id="finding_detector",
        file="AdminPreview.tsx",
        line=9,
        severity="error",
        category="open-redirect",
        message="redirectTo is assigned to window.location.href",
        confidence=0.97,
        verified_by="detector",
    )

    result = await calibrator.calibrate([finding], "diff")

    assert result[0].status == "confirmed"
    assert result[0].verified_by == "detector-auto"


async def test_security_alias_auto_confirms_and_normalizes_for_detector():
    calibrator = DynamicCalibrator(FailingLLM(), build_registry())
    finding = Finding(
        file="settings.py",
        line=3,
        severity="error",
        category="hardcoded-secret",
        message="secret literal",
        confidence=0.96,
        verified_by="detector",
    )

    result = await calibrator.calibrate([finding], "diff")

    assert result[0].category == "hardcoded-secrets"
    assert result[0].status == "confirmed"
    assert result[0].verified_by == "detector-auto"


async def test_llm_security_finding_is_contextually_calibrated():
    finding = Finding(
        id="finding_llm",
        file="repository.py",
        line=12,
        severity="error",
        category="sql-injection",
        message="dynamic query",
        confidence=0.99,
        reviewer="security_reviewer",
    )
    llm = RejectingLLM(finding.id)
    calibrator = DynamicCalibrator(llm, build_registry())

    result = await calibrator.calibrate([finding], "+query = allowlisted_query")

    assert llm.calls == 2
    assert result[0].status == "false_positive"
    assert result[0].verified_by == "judge"


async def test_low_confidence_detector_security_is_contextually_calibrated():
    finding = Finding(
        id="finding_contextual_detector",
        file="view.tsx",
        line=7,
        severity="warning",
        category="xss",
        message="innerHTML assignment",
        confidence=0.9,
        reviewer="security_reviewer",
        verified_by="detector",
    )
    llm = RejectingLLM(finding.id)
    calibrator = DynamicCalibrator(llm, build_registry())

    result = await calibrator.calibrate([finding], "+el.innerHTML = escapeHtml(raw)")

    assert llm.calls == 2
    assert result[0].status == "false_positive"


async def test_quality_evidence_contract_is_present_in_both_calibration_rounds():
    finding = Finding(
        id="finding_prompt_contract",
        file="component.tsx",
        line=8,
        severity="warning",
        category="naming",
        message="prefer another name",
        confidence=0.8,
        reviewer="style_reviewer",
    )
    llm = PromptCaptureLLM(finding.id)
    calibrator = DynamicCalibrator(llm, build_registry())

    await calibrator.calibrate([finding], "+return <span>static text</span>")

    assert len(llm.system_prompts) == 2
    for prompt in llm.system_prompts:
        assert "不能仅因" in prompt and "测试" in prompt and "文档" in prompt
        assert "命名" in prompt and "风格" in prompt
        assert "普通静态文本" in prompt and "textContent" in prompt
        assert "alt" in prompt and "label" in prompt


async def test_python_code_evidence_rejects_safe_process_and_path_claims_without_llm():
    source = '''"""Process helpers."""
import subprocess


def count_lines(input_file: str) -> str:
    result = subprocess.run(
        ["wc", "-l", input_file],
        capture_output=True,
        text=True,
    )
    return result.stdout


def find_entries(user_data: str) -> str:
    proc = subprocess.Popen(
        ["grep", user_data, "/var/log/app.log"],
        stdout=subprocess.PIPE,
    )
    return proc.communicate()[0].decode()


def list_logs() -> str:
    return subprocess.run(
        "ls -la /var/log/", shell=True, capture_output=True, text=True
    ).stdout


def ping_host(host: str = "localhost") -> str:
    return subprocess.check_output(["ping", "-c", "1", host]).decode()
'''
    findings = [
        Finding(
            id="finding_safe_path",
            file="helpers.py",
            line=4,
            severity="warning",
            category="path-traversal",
            message="path argument may read arbitrary files",
            confidence=0.75,
            reviewer="security_reviewer",
        ),
        Finding(
            id="finding_safe_grep",
            file="helpers.py",
            line=14,
            severity="warning",
            category="command-injection",
            message="grep argument may contain options",
            confidence=0.8,
            reviewer="security_reviewer",
        ),
        Finding(
            id="finding_constant_shell",
            file="helpers.py",
            line=23,
            severity="warning",
            category="design",
            message="shell=True increases command injection risk",
            confidence=0.9,
            reviewer="style_reviewer",
        ),
        Finding(
            id="finding_safe_ping",
            file="helpers.py",
            line=28,
            severity="warning",
            category="command-injection",
            message="host argument could target another host",
            confidence=0.8,
            reviewer="security_reviewer",
        ),
    ]
    calibrator = DynamicCalibrator(FailingLLM(), build_registry())

    result = await calibrator.calibrate(findings, _summary("helpers.py", source))

    assert len(result) == 4
    assert {finding.status for finding in result} == {"false_positive"}
    assert {finding.verified_by for finding in result} == {"code-evidence"}


async def test_python_code_evidence_preserves_dynamic_shell_and_open_path_findings():
    source = """import os
import subprocess


def safe_count(path: str):
    return subprocess.run(["wc", "-l", path], capture_output=True)


def execute(command: str, user_path: str) -> str:
    first = subprocess.run(command, shell=True, capture_output=True, text=True)
    os.system(command)
    with open(os.path.join("/srv/uploads", user_path)) as handle:
        return first.stdout + handle.read()
"""
    findings = [
        Finding(
            id="finding_dynamic_command",
            file="worker.py",
            line=10,
            severity="error",
            category="command-injection",
            message="dynamic command reaches shell=True",
            confidence=0.9,
            reviewer="security_reviewer",
        ),
        Finding(
            id="finding_dynamic_design",
            file="worker.py",
            line=10,
            severity="warning",
            category="design",
            message="dynamic shell=True command",
            confidence=0.85,
            reviewer="style_reviewer",
        ),
        Finding(
            id="finding_dynamic_path",
            file="worker.py",
            line=12,
            severity="error",
            category="path-traversal",
            message="user path is joined into an open call",
            confidence=0.9,
            reviewer="security_reviewer",
        ),
    ]
    llm = ConfirmingBatchLLM([finding.id for finding in findings])
    calibrator = DynamicCalibrator(llm, build_registry())

    result = await calibrator.calibrate(findings, _summary("worker.py", source))

    assert llm.calls == 2
    assert len(result) == 3
    assert {finding.status for finding in result} == {"confirmed"}
    assert {finding.verified_by for finding in result} == {"judge"}


async def test_actionability_gate_rejects_pr77_pr78_style_missing_test_and_doc_noise():
    diffs = "\n".join(
        [
            _summary(
                "gauntlet_decoys/account_store.go",
                "func QueryAccount(accountID string) error { return nil }\n"
                "func RenderSnippet(raw string) template.HTML { return template.HTML(raw) }",
            ),
            _summary(
                "gauntlet_fullstack/seed_sinks.py",
                "def run_admin_command(command: str) -> int:\n"
                "    return subprocess.run(command, shell=True).returncode",
            ),
            _summary(
                "gauntlet_fullstack/SeedJava.java",
                "public class SeedJava {\n"
                "    public void launchTool(String command) { Runtime.getRuntime().exec(command); }\n"
                "}",
            ),
        ]
    )
    findings = [
        Finding(
            id="missing_query_tests",
            file="gauntlet_decoys/account_store.go",
            line=1,
            category="missing-test",
            message="新增公共函数 QueryAccount 缺少测试，需要覆盖合法和非法输入。",
            suggestion="添加正常、边界和数据库失败场景的测试。",
            confidence=0.9,
            reviewer="testing_reviewer",
        ),
        Finding(
            id="duplicate_safety_doc",
            file="gauntlet_decoys/account_store.go",
            line=2,
            category="safety-doc",
            message="RenderSnippet 使用 template.HTML，存在 XSS 风险但缺少安全风险文档。",
            suggestion="添加注释警告调用者只能传入可信 HTML。",
            confidence=0.9,
            reviewer="documentation_reviewer",
        ),
        Finding(
            id="missing_python_doc",
            file="gauntlet_fullstack/seed_sinks.py",
            line=1,
            category="missing-documentation",
            message="公共函数 run_admin_command 缺少 docstring，未说明命令注入风险。",
            suggestion="添加安全警告文档。",
            confidence=0.9,
            reviewer="documentation_reviewer",
        ),
        Finding(
            id="missing_java_tests",
            file="gauntlet_fullstack/SeedJava.java",
            line=1,
            category="test-coverage",
            message="新增公共类及所有方法，但没有对应测试文件。",
            suggestion="创建 SeedJavaTest 并覆盖命令执行的正常和异常场景。",
            confidence=0.9,
            reviewer="testing_reviewer",
        ),
    ]

    result = await DynamicCalibrator(FailingLLM(), build_registry()).calibrate(findings, diffs)

    assert len(result) == 4
    assert {finding.status for finding in result} == {"false_positive"}
    assert {finding.verified_by for finding in result} == {"actionability-gate"}
    assert all("锚点" in finding.verify_reason or "可评论" in finding.verify_reason for finding in result)


async def test_actionability_gate_preserves_concrete_changed_assertion_defect():
    finding = Finding(
        id="wrong_assertion",
        file="tests/test_api.py",
        line=2,
        category="missing-test",
        message="修改后的断言仍预期 200，但新建接口现在返回 201，断言与实际契约不匹配。",
        suggestion="将断言更新为 201，并保留对响应体的验证。",
        confidence=0.85,
        reviewer="testing_reviewer",
    )
    llm = ConfirmingBatchLLM([finding.id])
    diff = _summary("tests/test_api.py", "def test_create_status():\n    assert response.status_code == 200")

    result = await DynamicCalibrator(llm, build_registry()).calibrate([finding], diff)

    assert llm.calls == 2
    assert result[0].status == "confirmed"
    assert result[0].verified_by == "judge"


async def test_actionability_gate_preserves_reliably_removed_test_coverage():
    finding = Finding(
        id="removed_auth_test",
        file="tests/test_auth.py",
        line=1,
        category="test-coverage",
        message="本次删除了拒绝未认证用户的断言，授权回归因此失去保护。",
        suggestion="恢复该测试并断言匿名请求返回 401。",
        confidence=0.9,
        reviewer="testing_reviewer",
    )
    llm = ConfirmingBatchLLM([finding.id])
    diff = """--- tests/test_auth.py (+0 -3)
@@ -1,4 +1,1 @@
 def auth_client():
-    pass
-def test_rejects_anonymous(auth_client):
-    assert auth_client.get(\"/private\").status_code == 401"""

    result = await DynamicCalibrator(llm, build_registry()).calibrate([finding], diff)

    assert llm.calls == 2
    assert result[0].status == "confirmed"


async def test_actionability_gate_preserves_rust_unsafe_safety_contract():
    finding = Finding(
        id="rust_safety_contract",
        file="src/raw.rs",
        line=1,
        category="missing-documentation",
        message="pub unsafe fn read_raw 缺少 # Safety，未定义调用者必须保证的指针有效性前置条件。",
        suggestion="记录指针非空、对齐和可读长度的安全契约。",
        confidence=0.9,
        reviewer="documentation_reviewer",
    )
    llm = ConfirmingBatchLLM([finding.id])
    diff = _summary(
        "src/raw.rs",
        "pub unsafe fn read_raw(ptr: *const u8) -> u8 {\n    *ptr\n}",
    )

    result = await DynamicCalibrator(llm, build_registry()).calibrate([finding], diff)

    assert llm.calls == 2
    assert result[0].status == "confirmed"


async def test_actionability_gate_preserves_documentation_behavior_mismatch():
    finding = Finding(
        id="stale_api_docs",
        file="src/service.py",
        line=2,
        category="documentation",
        message="现有 API 文档仍写失败时返回 None，与本次改为抛出 ValueError 的行为不一致。",
        suggestion="更新文档，说明 ValueError 的触发条件。",
        confidence=0.85,
        reviewer="documentation_reviewer",
    )
    llm = ConfirmingBatchLLM([finding.id])
    diff = _summary("src/service.py", "def parse(value: str):\n    raise ValueError(value)")

    result = await DynamicCalibrator(llm, build_registry()).calibrate([finding], diff)

    assert llm.calls == 2
    assert result[0].status == "confirmed"
    assert result[0].verified_by == "judge"


def test_actionability_gate_is_available_as_zero_token_pre_escalation_filter():
    generic = Finding(
        id="generic_missing_test",
        file="src/service.py",
        line=1,
        category="missing-test",
        message="新增公共函数没有测试。",
        confidence=0.6,
        reviewer="testing_reviewer",
    )

    actionable, rejected = apply_actionability_gate(
        [generic],
        _summary("src/service.py", "def service():\n    return 1"),
    )

    assert actionable == []
    assert rejected == [generic]
    assert generic.status == "false_positive"
    assert generic.verified_by == "actionability-gate"


async def test_actionability_gate_does_not_touch_other_review_dimensions():
    categories = [
        "n-plus-one",
        "architecture",
        "naming",
        "missing-alt",
        "xss",
        "dependency-vulnerability",
        "cross-pr-contract",
    ]
    findings = [
        Finding(
            id=f"finding_{index}",
            file="src/view.tsx",
            line=1,
            category=category,
            message=f"具体的 {category} 行为缺陷",
            confidence=0.85,
            reviewer="relevant_reviewer",
        )
        for index, category in enumerate(categories)
    ]
    llm = ConfirmingBatchLLM([finding.id for finding in findings])

    result = await DynamicCalibrator(llm, build_registry()).calibrate(
        findings,
        _summary("src/view.tsx", "export const View = () => <img />"),
    )

    assert llm.calls == 2
    assert {finding.category for finding in result} == set(categories)
    assert {finding.status for finding in result} == {"confirmed"}
    assert "actionability-gate" not in {finding.verified_by for finding in result}


def test_reviewer_prompts_require_concrete_test_and_documentation_evidence():
    base_ctx = {
        "files_to_review": ["src/service.py"],
        "diffs": {"src/service.py": "@@ -0,0 +1 @@\n+def service(): pass"},
        "target_language": "python",
        "registry": build_registry(),
    }
    testing = build_reviewer_prompt({**base_ctx, "reviewer_type": "testing"})[0]["content"]
    documentation = build_reviewer_prompt({**base_ctx, "reviewer_type": "documentation"})[0]["content"]

    assert "不要仅因新增公共函数" in testing
    assert "具体错误测试行" in testing
    assert "不要仅因公共函数/类缺少 docstring" in documentation
    assert "应直接报告可修复的漏洞" in documentation
