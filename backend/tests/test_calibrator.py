import json
import re
from types import SimpleNamespace

import pytest

from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding
from reviewforge.engine.calibrator import CalibrationResponseError, DynamicCalibrator, apply_actionability_gate
from reviewforge.engine.detectors import detect_dependency_findings
from reviewforge.engine.detectors.quality import detect_quality_findings
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


class MalformedCalibrationLLM:
    def __init__(self, content: str = "not-json") -> None:
        self.content = content
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        return SimpleNamespace(content=self.content)


class RejectThenMalformedJudgeLLM:
    def __init__(self, finding_id: str) -> None:
        self.finding_id = finding_id
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        if self.calls == 1:
            return SimpleNamespace(
                content=json.dumps(
                    [
                        {
                            "finding_id": self.finding_id,
                            "verdict": "false_positive",
                            "adjusted_confidence": 0.1,
                            "challenge": "检测证据不足。",
                        }
                    ],
                    ensure_ascii=False,
                )
            )
        return SimpleNamespace(content="not-json")


def _summary(file_path: str, content: str) -> str:
    lines = content.splitlines()
    patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)
    return f"--- {file_path} (+{len(lines)} -0)\n{patch}"


async def test_malformed_semantic_verdict_fails_closed_instead_of_confirming_detector_candidate():
    finding = Finding(
        id="manifest_candidate",
        file="Gemfile",
        line=1,
        severity="warning",
        category="dependency-version-range",
        message="Unbounded dependency version range.",
        confidence=0.93,
        verified_by="detector",
    )
    llm = MalformedCalibrationLLM()

    with pytest.raises(CalibrationResponseError, match="Adversarial verifier"):
        await DynamicCalibrator(llm, build_registry()).calibrate(
            [finding],
            _summary("Gemfile", 'gem "unsafe", "*"'),
        )

    assert llm.calls == 1
    assert finding.status == "candidate"
    assert finding.verified_by == "detector"


async def test_malformed_final_judge_never_preserves_an_unverified_confirmation():
    finding = Finding(
        id="judge_candidate",
        file="app.py",
        line=1,
        severity="error",
        category="command-injection",
        message="Dynamic command candidate.",
        confidence=0.93,
        verified_by="detector",
    )
    llm = RejectThenMalformedJudgeLLM(finding.id)

    with pytest.raises(CalibrationResponseError, match="Judge returned invalid JSON"):
        await DynamicCalibrator(llm, build_registry()).calibrate(
            [finding],
            _summary("app.py", "os.system(user_command)"),
        )

    assert llm.calls == 2
    assert finding.status == "false_positive"
    assert finding.verified_by == "adversarial"


def test_semantic_verdict_parser_rejects_truthy_types_and_incomplete_batches():
    findings = [
        Finding(id="finding_one", file="app.py", line=1, category="security", message="first candidate"),
        Finding(id="finding_two", file="app.py", line=2, category="security", message="second candidate"),
    ]
    calibrator = DynamicCalibrator(MalformedCalibrationLLM(), build_registry())
    truthy_payload = json.dumps(
        [
            {
                "finding_id": "finding_one",
                "verdict": True,
                "adjusted_confidence": 0.9,
                "challenge": "wrong type",
            },
            {
                "finding_id": "finding_two",
                "verdict": "confirmed",
                "adjusted_confidence": 0.9,
                "challenge": "valid shape",
            },
        ]
    )
    with pytest.raises(CalibrationResponseError, match="invalid verdict"):
        calibrator._parse_challenges(truthy_payload, findings)

    incomplete_payload = json.dumps(
        [
            {
                "finding_id": "finding_one",
                "verdict": "confirmed",
                "adjusted_confidence": 0.9,
                "challenge": "only one result",
            }
        ]
    )
    with pytest.raises(CalibrationResponseError, match="omitted findings"):
        calibrator._parse_challenges(incomplete_payload, findings)


async def test_high_confidence_detector_security_requires_independent_calibration():
    finding = Finding(
        id="finding_detector",
        file="app.py",
        line=1,
        severity="error",
        category="code-injection",
        message="Dynamic eval input is executed.",
        confidence=0.97,
        verified_by="detector",
    )

    llm = ConfirmingBatchLLM([finding.id])
    result = await DynamicCalibrator(llm, build_registry()).calibrate(
        [finding],
        _summary("app.py", "eval(user_code)"),
    )

    assert result[0].status == "confirmed"
    assert result[0].verified_by == "judge"
    assert llm.calls == 2


async def test_security_alias_normalizes_but_still_requires_calibration():
    finding = Finding(
        file="settings.py",
        line=1,
        severity="error",
        category="hardcoded-secret",
        message="secret literal",
        confidence=0.97,
        verified_by="detector",
    )

    llm = ConfirmingBatchLLM([finding.id])
    result = await DynamicCalibrator(llm, build_registry()).calibrate(
        [finding],
        _summary("settings.py", 'api_token = "ghp_A1b2C3d4E5f6G7h8"'),
    )

    assert result[0].category == "hardcoded-secrets"
    assert result[0].status == "confirmed"
    assert result[0].verified_by == "judge"
    assert llm.calls == 2


async def test_security_auto_confirm_requires_exact_detector_replay():
    finding = Finding(
        id="spoofed_detector",
        file="safe.py",
        line=1,
        severity="error",
        category="code-injection",
        message="A detector supposedly found eval here.",
        confidence=0.99,
        verified_by="detector",
    )
    llm = RejectingLLM(finding.id)

    result = await DynamicCalibrator(llm, build_registry()).calibrate(
        [finding],
        _summary("safe.py", "value = parse_json(raw)"),
    )

    assert result[0].status == "false_positive"
    assert result[0].verified_by == "judge"


async def test_detector_manifest_ranges_require_independent_calibration_across_ecosystems():
    sources = {
        "requirements.txt": "flask>=2.0\nunsafe-lib==*\nunversioned-package\nrequests==2.31.0",
        "package.json": (
            '{\n  "dependencies": {\n'
            '    "react": "^18.0.0",\n'
            '    "floating": "*",\n'
            '    "moving": "latest",\n'
            '    "exact": "1.2.3"\n'
            "  }\n}"
        ),
        "Gemfile": 'gem "rack"\ngem "rails", "~> 7.0"\ngem "json", "3.0.0"',
        "Cargo.toml": '[dependencies]\nserde = "0.8"\nhyper = ">=0.13"\nwild = "*"\nfixed = "=1.2.3"',
        "pom.xml": "<dependency>\n  <version>[2.9,)</version>\n  <version>1.2.3</version>\n</dependency>",
        "pyproject.toml": '[tool.poetry.dependencies]\nrequests = ">=2.0"\nfixed = "==1.2.3"',
        ".github/workflows/build.yml": "jobs:\n  build:\n    steps:\n      - uses: actions/checkout@v4",
    }
    summary = "\n".join(_summary(file_path, source) for file_path, source in sources.items())
    detected = []
    for file_path, source in sources.items():
        detected.extend(detect_dependency_findings({file_path: _summary(file_path, source)}))
    ranges = [item for item in detected if item.category == "dependency-version-range"]
    findings = [
        Finding(
            file=item.file,
            line=item.line,
            severity=item.severity,
            category=item.category,
            message=item.message,
            suggestion=item.suggestion,
            confidence=item.confidence,
            reviewer="dependency_reviewer",
            verified_by="detector",
        )
        for item in ranges
    ]

    llm = ConfirmingBatchLLM([finding.id for finding in findings])
    result = await DynamicCalibrator(llm, build_registry()).calibrate(findings, summary)

    assert {finding.file for finding in result} == set(sources)
    assert len(result) == 14
    assert {finding.status for finding in result} == {"confirmed"}
    assert {finding.verified_by for finding in result} == {"judge"}
    assert llm.calls == 2


async def test_manifest_range_auto_confirm_requires_detector_provenance():
    diff = _summary("package.json", '{"dependencies":{"react":"^18.0.0"}}')
    finding = Finding(
        id="llm_range_guess",
        file="package.json",
        line=1,
        severity="warning",
        category="dependency-version-range",
        message="This dependency may resolve to another compatible release.",
        confidence=0.99,
        reviewer="dependency_reviewer",
    )
    llm = RejectingLLM(finding.id)

    result = await DynamicCalibrator(llm, build_registry()).calibrate([finding], diff)

    assert llm.calls == 2
    assert result[0].status == "false_positive"
    assert result[0].verified_by == "judge"


async def test_detector_provenance_does_not_auto_confirm_an_exact_manifest_pin():
    diff = _summary("package.json", '{"dependencies":{"react":"18.3.1"}}')
    finding = Finding(
        id="detector_exact_pin_guess",
        file="package.json",
        line=1,
        severity="warning",
        category="dependency-version-range",
        message="The dependency uses a mutable version range.",
        confidence=0.99,
        reviewer="dependency_reviewer",
        verified_by="detector",
    )
    llm = RejectingLLM(finding.id)

    result = await DynamicCalibrator(llm, build_registry()).calibrate([finding], diff)

    assert llm.calls == 2
    assert result[0].status == "false_positive"
    assert result[0].verified_by == "judge"


async def test_high_confidence_quality_findings_require_independent_calibration():
    sources = {
        "src/job.py": "def run_job():\n    try:\n        run()\n    except:\n        pass",
        "src/parser.rs": "pub fn parse(raw: &str) -> u32 { raw.parse::<u32>().unwrap() }",
        "src/panel.tsx": (
            'import { exec } from "child_process";\n'
            "export function run(command: string) { localStorage.setItem('last', command); exec(command); }"
        ),
    }
    summary = "\n".join(_summary(file_path, source) for file_path, source in sources.items())
    detected = []
    for file_path, source in sources.items():
        detected.extend(detect_quality_findings({file_path: _summary(file_path, source)}))
    findings = [
        Finding(
            file=item.file,
            line=item.line,
            severity=item.severity,
            category=item.category,
            message=item.message,
            suggestion=item.suggestion,
            confidence=item.confidence,
            reviewer="quality_reviewer",
            verified_by="detector",
        )
        for item in detected
    ]

    llm = ConfirmingBatchLLM([finding.id for finding in findings])
    result = await DynamicCalibrator(llm, build_registry()).calibrate(findings, summary)

    assert len(result) == 3
    assert {finding.status for finding in result} == {"confirmed"}
    assert {finding.verified_by for finding in result} == {"judge"}
    assert llm.calls == 2


async def test_quality_auto_confirm_requires_detector_provenance_and_reproduced_rule():
    risky_diff = _summary("src/job.py", "def run_job():\n    try:\n        run()\n    except:\n        pass")
    llm_finding = Finding(
        id="llm_bare_except",
        file="src/job.py",
        line=4,
        category="exception-handling",
        message="A bare except catches process control exceptions.",
        confidence=0.99,
        reviewer="quality_reviewer",
    )
    llm = RejectingLLM(llm_finding.id)

    llm_result = await DynamicCalibrator(llm, build_registry()).calibrate([llm_finding], risky_diff)

    assert llm.calls == 2
    assert llm_result[0].status == "false_positive"

    safe_diff = _summary("src/job.py", "try:\n    run()\nexcept ValueError:\n    recover()")
    forged = Finding(
        id="forged_quality_detector",
        file="src/job.py",
        line=3,
        category="exception-handling",
        message="The exception is swallowed.",
        confidence=0.99,
        reviewer="quality_reviewer",
        verified_by="detector",
    )
    forged_llm = RejectingLLM(forged.id)

    forged_result = await DynamicCalibrator(forged_llm, build_registry()).calibrate([forged], safe_diff)

    assert forged_llm.calls == 2
    assert forged_result[0].status == "false_positive"


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
        category="architecture",
        message="a concrete dependency boundary is violated",
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


def test_actionability_gate_only_rejects_narrow_missing_test_noise_before_calibration():
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

    actionable, rejected = apply_actionability_gate(findings, diffs)

    assert {finding.id for finding in rejected} == {"missing_query_tests", "missing_java_tests"}
    assert {finding.id for finding in actionable} == {"duplicate_safety_doc", "missing_python_doc"}
    assert {finding.verified_by for finding in rejected} == {"actionability-gate"}


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
            message=("该命名导致 API caller 失败" if category == "naming" else f"具体的 {category} 行为缺陷"),
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


async def test_rust_direct_path_parameters_are_rejected_without_suppressing_dynamic_construction():
    safe_source = """use std::fs;

pub fn load_config(path: &str) -> Result<String, std::io::Error> {
    fs::read_to_string(path)
}

pub fn read_user_data(path: &str) -> Result<String, std::io::Error> {
    fs::read_to_string(path)
}
"""
    safe_findings = [
        Finding(
            id="safe_rust_sink",
            file="binary_reader.rs",
            line=4,
            category="path-traversal",
            message="Filesystem access uses a variable path.",
            confidence=0.8,
            reviewer="security_reviewer",
        ),
        Finding(
            id="safe_rust_wrong_anchor",
            file="binary_reader.rs",
            line=9,
            category="path-traversal",
            message="load_config directly reads a caller-provided path.",
            confidence=0.8,
            reviewer="security_reviewer",
        ),
    ]

    safe = await DynamicCalibrator(FailingLLM(), build_registry()).calibrate(
        safe_findings, _summary("binary_reader.rs", safe_source)
    )

    assert {finding.status for finding in safe} == {"false_positive"}
    assert {finding.verified_by for finding in safe} == {"actionability-gate"}

    dynamic_source = """use std::fs;
pub fn load_config(base_path: &str, filename: &str) -> Result<String, std::io::Error> {
    let path = format!("{}/{}", base_path, filename);
    fs::read_to_string(&path)
}
"""
    dynamic = Finding(
        id="dynamic_rust_path",
        file="config_loader.rs",
        line=4,
        category="path-traversal",
        message="base_path and filename are dynamically combined before the filesystem read.",
        confidence=0.9,
        reviewer="security_reviewer",
    )
    llm = ConfirmingBatchLLM([dynamic.id])

    preserved = await DynamicCalibrator(llm, build_registry()).calibrate(
        [dynamic], _summary("config_loader.rs", dynamic_source)
    )

    assert llm.calls == 2
    assert preserved[0].status == "confirmed"


def test_contextual_live_region_style_and_performance_findings_are_not_irreversibly_filtered():
    source = """function showStatus(message: string) {
  document.getElementById('status')!.textContent = message;
}

def count_lines(path: str) -> str:
    return run_wc(path)

pub fn count_items(items: &[String]) -> usize {
    let mut count = 0;
    for _item in items { count += 1; }
    count
}
"""
    findings = [
        Finding(
            id="live_region",
            file="mixed.txt",
            line=2,
            category="dynamic-content-update",
            message="textContent updates status but no ARIA live region is visible in this diff.",
            confidence=0.95,
            reviewer="accessibility_reviewer",
        ),
        Finding(
            id="naming_preference",
            file="mixed.txt",
            line=5,
            category="naming",
            message="count_lines promises a count but returns raw stdout, so the name is imprecise.",
            confidence=0.85,
            reviewer="style_reviewer",
        ),
        Finding(
            id="micro_optimization",
            file="mixed.txt",
            line=8,
            category="performance",
            message="The manual count loop can be replaced by len() for O(1) access.",
            confidence=1.0,
            reviewer="performance_reviewer",
        ),
    ]

    actionable, rejected = apply_actionability_gate(findings, _summary("mixed.txt", source))

    assert actionable == findings
    assert rejected == []


async def test_actionability_gate_preserves_concrete_style_performance_and_removed_aria_failures():
    source = """func worker(ctx context.Context) {
    for {
        db.Query("SELECT 1")
    }
}

def parse(value):
    raise ValueError(value)
"""
    diff = _summary("worker.txt", source)
    diff += '\n--- view.tsx (+0 -1)\n@@ -1,2 +1,1 @@\n-<div role="status" aria-live="polite" />\n+<div />'
    findings = [
        Finding(
            id="infinite_work",
            file="worker.txt",
            line=2,
            category="performance",
            message="Infinite loop performs an unbounded database query and can exhaust the connection pool.",
            confidence=0.9,
            reviewer="performance_reviewer",
        ),
        Finding(
            id="observable_style_failure",
            file="worker.txt",
            line=7,
            category="naming",
            message="The misleading dispatch name causes the API caller to reject valid values and fail.",
            confidence=0.85,
            reviewer="style_reviewer",
        ),
        Finding(
            id="removed_live_contract",
            file="view.tsx",
            line=1,
            category="missing-live-region",
            message="This change removed aria-live, so status updates are no longer announced.",
            confidence=0.9,
            reviewer="accessibility_reviewer",
        ),
    ]
    llm = ConfirmingBatchLLM([finding.id for finding in findings])

    result = await DynamicCalibrator(llm, build_registry()).calibrate(findings, diff)

    assert llm.calls == 2
    assert {finding.status for finding in result} == {"confirmed"}


def test_actionability_gate_preserves_new_live_carrier_blocking_io_listener_import_and_default_contracts():
    diff = "\n".join(
        [
            _summary(
                "view.tsx",
                "const status = document.getElementById('status');\n"
                "status.textContent = message;\n"
                'return <div id="status" />;',
            ),
            _summary(
                "server.ts",
                "window.addEventListener('resize', onResize);\nconst config = readFileSync(configPath, 'utf8');",
            ),
            _summary("compile.ts", "export const value: Widget = makeWidget();"),
            _summary("README.md", "The request timeout defaults to 5 seconds."),
        ]
    )
    findings = [
        Finding(
            id="new_live_region",
            file="view.tsx",
            line=2,
            category="missing-live-region",
            message="The newly added status carrier is updated dynamically but has no live-region semantics.",
            confidence=0.9,
            reviewer="accessibility_reviewer",
        ),
        Finding(
            id="listener_leak",
            file="server.ts",
            line=1,
            category="performance",
            message="The listener is never removed, causing a memory leak across mounts.",
            confidence=0.9,
            reviewer="performance_reviewer",
        ),
        Finding(
            id="event_loop_block",
            file="server.ts",
            line=2,
            category="performance",
            message="readFileSync blocks the event loop for every request.",
            confidence=0.9,
            reviewer="performance_reviewer",
        ),
        Finding(
            id="missing_import",
            file="compile.ts",
            line=1,
            category="imports",
            message="Widget is used without its import, causing a compilation failure.",
            confidence=0.9,
            reviewer="style_reviewer",
        ),
        Finding(
            id="stale_default",
            file="README.md",
            line=1,
            category="documentation",
            message="This default value is outdated: the implementation now defaults to 30 seconds.",
            confidence=0.9,
            reviewer="documentation_reviewer",
        ),
    ]

    actionable, rejected = apply_actionability_gate(findings, diff)

    assert {finding.id for finding in actionable} == {finding.id for finding in findings}
    assert rejected == []


def test_rust_actionability_uses_extractor_and_fragment_provenance():
    diff = "\n".join(
        [
            _summary(
                "handler.rs",
                "async fn download(Path(filename): Path<String>) -> Result<Vec<u8>, Error> {\n"
                "    fs::read(filename).map_err(Error::from)\n"
                "}",
            ),
            _summary(
                "constant.rs",
                'const FILE_NAME: &str = "config.toml";\n'
                "fn load(base: &Path) -> Result<Vec<u8>, Error> {\n"
                "    let candidate = base.join(FILE_NAME);\n"
                "    fs::read(&candidate).map_err(Error::from)\n"
                "}",
            ),
        ]
    )
    axum = Finding(
        id="axum_path",
        file="handler.rs",
        line=2,
        category="path-traversal",
        message="The Axum Path extractor reaches fs::read without confinement.",
        confidence=0.9,
        reviewer="security_reviewer",
    )
    fixed = Finding(
        id="fixed_fragment",
        file="constant.rs",
        line=4,
        category="path-traversal",
        message="base.join(FILE_NAME) reaches fs::read.",
        confidence=0.9,
        reviewer="security_reviewer",
    )

    actionable, rejected = apply_actionability_gate([axum, fixed], diff)

    assert actionable == [axum]
    assert rejected == [fixed]


def test_rust_actionability_guard_requires_same_candidate_before_sink():
    diff = "\n".join(
        [
            _summary(
                "guarded.rs",
                "fn load(base: &Path, filename: &str) -> Result<Vec<u8>, Error> {\n"
                "    let candidate = base.join(filename).canonicalize()?;\n"
                "    if !candidate.starts_with(base) { return Err(Error::Traversal); }\n"
                "    fs::read(&candidate).map_err(Error::from)\n"
                "}",
            ),
            _summary(
                "late.rs",
                "fn load(base: &Path, filename: &str) -> Result<Vec<u8>, Error> {\n"
                "    let candidate = base.join(filename).canonicalize()?;\n"
                "    let result = fs::read(&candidate)?;\n"
                "    if !candidate.starts_with(base) { return Err(Error::Traversal); }\n"
                "    Ok(result)\n"
                "}",
            ),
            _summary(
                "unrelated.rs",
                "fn load(base: &Path, filename: &str) -> Result<Vec<u8>, Error> {\n"
                "    let candidate = base.join(filename).canonicalize()?;\n"
                '    let safe = base.join("known.txt").canonicalize()?;\n'
                "    if !safe.starts_with(base) { return Err(Error::Traversal); }\n"
                "    fs::read(&candidate).map_err(Error::from)\n"
                "}",
            ),
        ]
    )
    findings = [
        Finding(
            id=file_name,
            file=f"{file_name}.rs",
            line=line,
            category="path-traversal",
            message="A dynamically joined filename reaches fs::read.",
            confidence=0.9,
            reviewer="security_reviewer",
        )
        for file_name, line in (("guarded", 4), ("late", 3), ("unrelated", 5))
    ]

    actionable, rejected = apply_actionability_gate(findings, diff)

    assert {finding.id for finding in actionable} == {"late", "unrelated"}
    assert {finding.id for finding in rejected} == {"guarded"}


async def test_high_confidence_detector_missing_alt_requires_render_context():
    finding = Finding(
        id="detector_alt",
        file="view.tsx",
        line=1,
        category="missing-alt",
        message="img has no alt",
        confidence=0.97,
        reviewer="accessibility_reviewer",
        verified_by="detector",
    )

    llm = ConfirmingBatchLLM([finding.id])
    result = await DynamicCalibrator(llm, build_registry()).calibrate(
        [finding],
        _summary("view.tsx", '<img src="/avatar.png" />'),
    )

    assert {finding.status for finding in result} == {"confirmed"}
    assert {finding.verified_by for finding in result} == {"judge"}
    assert llm.calls == 2


async def test_detector_missing_label_still_requires_contextual_calibration():
    finding = Finding(
        id="detector_label",
        file="view.tsx",
        line=1,
        category="missing-label",
        message="input has no accessible label",
        confidence=0.99,
        reviewer="accessibility_reviewer",
        verified_by="detector",
    )
    llm = ConfirmingBatchLLM([finding.id])

    result = await DynamicCalibrator(llm, build_registry()).calibrate(
        [finding],
        _summary("view.tsx", '<input name="email" />'),
    )

    assert llm.calls == 2
    assert result[0].verified_by == "judge"


class BoundedCalibrationLLM:
    def __init__(
        self,
        *,
        invalid_round: str = "",
        invalid_batch: int = 0,
        reason_length: int = 40,
    ) -> None:
        self.invalid_round = invalid_round
        self.invalid_batch = invalid_batch
        self.reason_length = reason_length
        self.round_counts = {"adversarial": 0, "judge": 0}
        self.records: list[tuple[str, list[str], str]] = []

    async def ainvoke(self, messages):
        human = str(messages[1].content)
        round_name = "adversarial" if '"adjusted_confidence"' in human else "judge"
        self.round_counts[round_name] += 1
        finding_ids = re.findall(r"^- \[([^\]]+)\]", human, re.MULTILINE)
        self.records.append((round_name, finding_ids, human))
        reason = "evidence-" + "x" * self.reason_length
        if round_name == "adversarial":
            payload = [
                {
                    "finding_id": finding_id,
                    "verdict": "confirmed",
                    "adjusted_confidence": 0.4,
                    "challenge": reason,
                }
                for finding_id in finding_ids
            ]
        else:
            payload = [
                {
                    "finding_id": finding_id,
                    "verdict": "confirmed",
                    "confidence": 0.9,
                    "reason": reason,
                }
                for finding_id in finding_ids
            ]
        if round_name == self.invalid_round and self.round_counts[round_name] == self.invalid_batch:
            payload = payload[:-1]
        return SimpleNamespace(content=json.dumps(payload))


def _large_calibration_fixture(count: int = 48, padding_lines: int = 90):
    findings: list[Finding] = []
    summaries: list[str] = []
    markers: dict[str, str] = {}
    target_line = max(1, padding_lines // 2)
    for index in range(count):
        file_path = f"src/generated_{index:02d}.txt"
        marker = f"UNIQUE_CALIBRATION_FILE_{index:02d}"
        markers[file_path] = marker
        source = "\n".join(f"value_{line} = '{marker}_{line}'" for line in range(1, padding_lines + 1))
        summaries.append(_summary(file_path, source))
        findings.append(
            Finding(
                id=f"large_finding_{index:02d}",
                file=file_path,
                line=target_line,
                severity="warning",
                category="correctness",
                message=f"Concrete behavioral defect {index}. " + "m" * 800,
                suggestion="Apply the local correction. " + "s" * 1_500,
                confidence=0.9,
                reviewer="architecture_reviewer",
            )
        )
    return findings, "\n".join(summaries), markers


async def test_large_calibration_is_bounded_file_local_complete_and_reason_limited():
    findings, diff, markers = _large_calibration_fixture()
    llm = BoundedCalibrationLLM(reason_length=2_000)

    result = await DynamicCalibrator(llm, build_registry()).calibrate(findings, diff)

    assert len(result) == 48
    assert llm.round_counts == {"adversarial": 3, "judge": 3}
    assert all(finding.status == "confirmed" and finding.verified_by == "judge" for finding in result)
    assert all(len(finding.verify_reason) == 500 for finding in result)

    expected_ids = {finding.id for finding in findings}
    for round_name in ("adversarial", "judge"):
        batches = [ids for record_round, ids, _human in llm.records if record_round == round_name]
        assert [len(batch) for batch in batches] == [16, 16, 16]
        assert len([finding_id for batch in batches for finding_id in batch]) == len(expected_ids)
        assert {finding_id for batch in batches for finding_id in batch} == expected_ids

    for _round_name, batch_ids, human in llm.records:
        related_files = {findings[int(finding_id.rsplit("_", 1)[1])].file for finding_id in batch_ids}
        for file_path, marker in markers.items():
            assert (marker in human) is (file_path in related_files)
        assert len(human) <= 60_000


async def test_large_adversarial_missing_id_fails_before_any_batch_is_applied():
    findings, diff, _markers = _large_calibration_fixture(padding_lines=4)
    llm = BoundedCalibrationLLM(invalid_round="adversarial", invalid_batch=2)

    with pytest.raises(CalibrationResponseError, match="Adversarial verifier omitted findings"):
        await DynamicCalibrator(llm, build_registry()).calibrate(findings, diff)

    assert llm.round_counts == {"adversarial": 2, "judge": 0}
    assert all(finding.status == "candidate" for finding in findings)
    assert all(finding.verified_by == "" for finding in findings)


async def test_large_judge_missing_id_never_leaves_partially_judged_findings():
    findings, diff, _markers = _large_calibration_fixture(padding_lines=4)
    llm = BoundedCalibrationLLM(invalid_round="judge", invalid_batch=2)

    with pytest.raises(CalibrationResponseError, match="Judge omitted findings"):
        await DynamicCalibrator(llm, build_registry()).calibrate(findings, diff)

    assert llm.round_counts == {"adversarial": 3, "judge": 2}
    assert all(finding.status == "confirmed" for finding in findings)
    assert all(finding.verified_by == "adversarial" for finding in findings)


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
