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


async def test_high_confidence_detector_missing_alt_auto_confirms_without_llm():
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

    result = await DynamicCalibrator(FailingLLM(), build_registry()).calibrate(
        [finding],
        _summary("view.tsx", '<img src="/avatar.png" />'),
    )

    assert {finding.status for finding in result} == {"confirmed"}
    assert {finding.verified_by for finding in result} == {"detector-auto"}


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
