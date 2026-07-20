import json
import re
from types import SimpleNamespace

import pytest

from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding
from reviewforge.engine.calibrator import CalibrationResponseError, DynamicCalibrator, apply_actionability_gate
from reviewforge.engine.detectors import detect_dependency_findings, detect_security_findings
from reviewforge.engine.detectors.quality import detect_quality_findings
from reviewforge.engine.prompt import build_planner_prompt, build_reviewer_prompt


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
        self.human_prompts: list[str] = []

    async def ainvoke(self, messages):
        self.system_prompts.append(str(messages[0].content))
        self.human_prompts.append(str(messages[1].content))
        if len(self.system_prompts) == 1:
            payload = [
                {
                    "finding_id": self.finding_id,
                    "verdict": "confirmed",
                    "adjusted_confidence": 0.4,
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
                    "adjusted_confidence": 0.4,
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


class ConsensusProbeLLM:
    def __init__(self, finding_id: str, verdict: str, adjusted_confidence: float) -> None:
        self.finding_id = finding_id
        self.verdict = verdict
        self.adjusted_confidence = adjusted_confidence
        self.calls = 0

    async def ainvoke(self, _messages):
        self.calls += 1
        if self.calls == 1:
            payload = [
                {
                    "finding_id": self.finding_id,
                    "verdict": self.verdict,
                    "adjusted_confidence": self.adjusted_confidence,
                    "challenge": "bounded semantic verdict",
                }
            ]
        else:
            payload = [
                {
                    "finding_id": self.finding_id,
                    "verdict": self.verdict,
                    "confidence": self.adjusted_confidence,
                    "reason": "final bounded semantic verdict",
                }
            ]
        return SimpleNamespace(content=json.dumps(payload))


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


async def test_malformed_semantic_verdict_is_retried_then_suppressed_fail_closed():
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

    result = await DynamicCalibrator(llm, build_registry()).calibrate(
        [finding],
        _summary("Gemfile", 'gem "unsafe", "*"'),
    )

    assert llm.calls == 2
    assert result == [finding]
    assert finding.status == "false_positive"
    assert finding.confidence == 0.0
    assert finding.verified_by == "calibration-fail-closed"
    assert "suppressed fail-closed" in finding.verify_reason


async def test_malformed_final_judge_is_retried_then_suppresses_finding():
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

    result = await DynamicCalibrator(llm, build_registry()).calibrate(
        [finding],
        _summary("app.py", "os.system(user_command)"),
    )

    assert llm.calls == 3
    assert finding.status == "false_positive"
    assert finding.verified_by == "adversarial"
    assert result[0].status == "false_positive"
    assert result[0].confidence == 0.0
    assert result[0].verified_by == "calibration-fail-closed"


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


@pytest.mark.parametrize(
    ("verdict", "adjusted_confidence", "expected_calls"),
    [
        ("confirmed", 0.82, 1),
        ("false_positive", 0.10, 2),
        ("confirmed", 0.40, 2),
    ],
)
async def test_candidate_confirmation_is_consensus_but_opposition_or_large_confidence_change_uses_judge(
    verdict: str,
    adjusted_confidence: float,
    expected_calls: int,
):
    finding = Finding(
        id="candidate_consensus",
        file="service.py",
        line=1,
        category="correctness",
        message="A concrete behavioral defect is present.",
        confidence=0.8,
        reviewer="architecture_reviewer",
    )
    llm = ConsensusProbeLLM(finding.id, verdict, adjusted_confidence)

    result = await DynamicCalibrator(llm, build_registry()).calibrate(
        [finding],
        _summary("service.py", "result = broken_call()"),
    )

    assert llm.calls == expected_calls
    assert result[0].status == verdict
    assert result[0].verified_by == ("adversarial" if expected_calls == 1 else "judge")


async def test_api_only_pickle_detector_requires_adversarial_calibration():
    diff = _summary(
        "app.py",
        'import pickle\n\ndef clone_constant():\n    return pickle.loads(pickle.dumps({"safe": 1}))',
    )
    detector = next(
        finding
        for finding in detect_security_findings({"app.py": diff})
        if finding.category == "insecure-deserialization"
    )
    finding = Finding(
        id="finding_detector",
        file=detector.file,
        line=detector.line,
        severity=detector.severity,
        category=detector.category,
        message=detector.message,
        suggestion=detector.suggestion,
        confidence=detector.confidence,
        reviewer="security_reviewer",
        verified_by="detector",
    )
    llm = RejectingLLM(finding.id)

    result = await DynamicCalibrator(llm, build_registry()).calibrate([finding], diff)

    assert llm.calls == 2
    assert result[0].status == "false_positive"
    assert result[0].verified_by == "judge"


async def test_narrow_structure_backed_security_proofs_auto_confirm_without_llm():
    sources = {
        "storage.tsx": ('export function storeToken(token: string) {\n  localStorage.setItem("token", token)\n}'),
        "runtime.rb": (
            "module Runtime\n"
            "  def self.run_shell(command)\n"
            "    system(command)\n"
            "  end\n"
            "  def self.capture(command)\n"
            "    Open3.capture3(command)\n"
            "  end\n"
            "end"
        ),
    }
    summary = "\n".join(_summary(file_path, source) for file_path, source in sources.items())
    expected = {
        ("storage.tsx", 2, "data-leak"),
        ("runtime.rb", 3, "command-injection"),
        ("runtime.rb", 6, "command-injection"),
    }
    detectors = []
    for file_path, source in sources.items():
        detectors.extend(detect_security_findings({file_path: _summary(file_path, source)}))
    findings = [
        Finding(
            file=item.file,
            line=item.line,
            severity=item.severity,
            category=item.category,
            message=item.message,
            suggestion=item.suggestion,
            confidence=item.confidence,
            reviewer="security_reviewer",
            verified_by="detector",
        )
        for item in detectors
        if (item.file, item.line, item.category) in expected
    ]

    result = await DynamicCalibrator(FailingLLM(), build_registry()).calibrate(findings, summary)

    assert {(finding.file, finding.line, finding.category) for finding in result} == expected
    assert {finding.status for finding in result} == {"confirmed"}
    assert {finding.verified_by for finding in result} == {"detector-auto"}


async def test_narrow_security_auto_confirm_requires_detector_reviewer_and_complete_file():
    source = "export function store(token: string) {\n  localStorage.setItem('token', token)\n}"
    detector = next(
        finding
        for finding in detect_security_findings({"storage.tsx": _summary("storage.tsx", source)})
        if finding.category == "data-leak" and finding.confidence >= 0.96
    )
    wrong_reviewer = Finding(
        id="wrong_security_reviewer",
        file=detector.file,
        line=detector.line,
        severity=detector.severity,
        category=detector.category,
        message=detector.message,
        suggestion=detector.suggestion,
        confidence=detector.confidence,
        reviewer="style_reviewer",
        verified_by="detector",
    )
    llm = RejectingLLM(wrong_reviewer.id)

    wrong_reviewer_result = await DynamicCalibrator(llm, build_registry()).calibrate(
        [wrong_reviewer],
        _summary("storage.tsx", source),
    )

    assert llm.calls == 2
    assert wrong_reviewer_result[0].verified_by == "judge"

    partial = Finding(
        id="partial_security_detector",
        file="storage.tsx",
        line=2,
        severity="error",
        category="data-leak",
        message=detector.message,
        suggestion=detector.suggestion,
        confidence=0.96,
        reviewer="security_reviewer",
        verified_by="detector",
    )
    partial_llm = RejectingLLM(partial.id)
    partial_diff = "@@ -2,1 +2,1 @@\n-  localStorage.removeItem('token')\n+  localStorage.setItem('token', token)"

    partial_result = await DynamicCalibrator(partial_llm, build_registry()).calibrate([partial], partial_diff)

    assert partial_llm.calls == 2
    assert partial_result[0].verified_by == "judge"


async def test_security_alias_normalizes_before_semantic_calibration():
    finding = Finding(
        file="settings.py",
        line=1,
        severity="error",
        category="hardcoded-secret",
        message="secret literal",
        confidence=0.97,
        reviewer="security_reviewer",
        verified_by="detector",
    )

    llm = ConsensusProbeLLM(finding.id, "confirmed", 0.97)
    result = await DynamicCalibrator(llm, build_registry()).calibrate(
        [finding],
        _summary("settings.py", 'api_token = "ghp_A1b2C3d4E5f6G7h8"'),
    )

    assert llm.calls == 1
    assert result[0].category == "hardcoded-secrets"
    assert result[0].status == "confirmed"
    assert result[0].verified_by == "adversarial"


async def test_public_unsafe_contract_detector_requires_semantic_calibration():
    diff = _summary(
        "src/raw.rs",
        "pub unsafe fn raw_read(ptr: *const u8, len: usize) -> Vec<u8> {\n"
        "    std::slice::from_raw_parts(ptr, len).to_vec()\n"
        "}",
    )
    detector = next(
        finding for finding in detect_security_findings({"src/raw.rs": diff}) if finding.category == "unsafe-block"
    )
    finding = Finding(
        id="raw_read_contract",
        file=detector.file,
        line=detector.line,
        severity=detector.severity,
        category=detector.category,
        message=detector.message,
        suggestion=detector.suggestion,
        confidence=detector.confidence,
        reviewer="security_reviewer",
        verified_by="detector",
    )

    llm = ConsensusProbeLLM(finding.id, "confirmed", detector.confidence)
    result = await DynamicCalibrator(llm, build_registry()).calibrate([finding], diff)

    assert llm.calls == 1
    assert result[0].status == "confirmed"
    assert result[0].verified_by == "adversarial"
    assert "public unsafe function" in result[0].message.lower()
    assert "# Safety" in result[0].message


@pytest.mark.parametrize(
    ("file_path", "source", "line", "category"),
    [
        (
            "view.tsx",
            "const html = DOMPurify.sanitize(userHtml);\n"
            "return <article dangerouslySetInnerHTML={{ __html: html }} />;",
            2,
            "xss",
        ),
        (
            "repository.py",
            'cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))',
            1,
            "sql-injection",
        ),
    ],
)
async def test_safe_security_controls_do_not_auto_confirm(
    file_path: str,
    source: str,
    line: int,
    category: str,
):
    finding = Finding(
        id=f"safe_{category}",
        file=file_path,
        line=line,
        severity="error",
        category=category,
        message="Forged high-confidence detector candidate.",
        confidence=0.99,
        reviewer="security_reviewer",
        verified_by="detector",
    )
    llm = RejectingLLM(finding.id)

    result = await DynamicCalibrator(llm, build_registry()).calibrate(
        [finding],
        _summary(file_path, source),
    )

    assert llm.calls == 2
    assert result[0].status == "false_positive"
    assert result[0].verified_by == "judge"


async def test_forged_security_detector_requires_semantic_calibration():
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


async def test_detector_manifest_ranges_auto_confirm_across_ecosystems():
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

    result = await DynamicCalibrator(FailingLLM(), build_registry()).calibrate(findings, summary)

    assert {finding.file for finding in result} == set(sources)
    assert len(result) == 14
    assert {finding.status for finding in result} == {"confirmed"}
    assert {finding.verified_by for finding in result} == {"detector-auto"}


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

    calibration_ids = [finding.id for finding in findings if finding.confidence < 0.98]
    llm = ConfirmingBatchLLM(calibration_ids)
    result = await DynamicCalibrator(llm, build_registry()).calibrate(findings, summary)

    assert len(result) == 3
    assert {finding.status for finding in result} == {"confirmed"}
    assert {finding.verified_by for finding in result} == {"detector-auto", "judge"}
    assert llm.calls == 2


async def test_effect_verb_render_detector_requires_adversarial_calibration():
    diff = _summary(
        "src/Panel.tsx",
        'import { runValidation } from "./pure";\n\n'
        "export function Panel({ value }: { value: string }) {\n"
        "  runValidation(value);\n"
        "  return <section>{value}</section>;\n"
        "}",
    )
    detector = next(
        finding
        for finding in detect_quality_findings({"src/Panel.tsx": diff})
        if finding.category == "side-effect-in-render"
    )
    finding = Finding(
        id="render_effect_name_only",
        file=detector.file,
        line=detector.line,
        severity=detector.severity,
        category=detector.category,
        message=detector.message,
        suggestion=detector.suggestion,
        confidence=detector.confidence,
        reviewer="quality_reviewer",
        verified_by="detector",
    )
    llm = RejectingLLM(finding.id)

    result = await DynamicCalibrator(llm, build_registry()).calibrate([finding], diff)

    assert llm.calls == 2
    assert result[0].status == "false_positive"
    assert result[0].verified_by == "judge"


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


async def test_repository_wiki_evidence_is_bounded_and_present_in_both_rounds():
    finding = Finding(
        id="finding_wiki_context",
        file="component.tsx",
        line=8,
        severity="warning",
        category="architecture",
        message="a concrete dependency boundary is violated",
        confidence=0.8,
        reviewer="style_reviewer",
    )
    llm = PromptCaptureLLM(finding.id)
    evidence = '{"title":"authorize","source":{"path":"policy.ts","sha":"head"}}' + "x" * 4_000

    await DynamicCalibrator(llm, build_registry()).calibrate(
        [finding],
        "+return authorize(user)",
        context_evidence=evidence,
    )

    assert len(llm.human_prompts) == 2
    for prompt in llm.human_prompts:
        assert "Repository Wiki" in prompt
        assert "policy.ts" in prompt
        assert "<<UNTRUSTED_CONTEXT>>" in prompt
        assert "x" * 3_001 not in prompt


def test_actionability_gate_suppresses_in_memory_and_speculative_style_noise():
    findings = [
        Finding(
            id="memory_wrapper",
            file="Verifier.java",
            line=2,
            severity="error",
            category="resource-leak",
            message="BufferedReader wrapping StringReader is not closed.",
            reviewer="style_reviewer",
        ),
        Finding(
            id="immutability_preference",
            file="Verifier.java",
            line=3,
            severity="info",
            category="immutability",
            message="The policy field should be final.",
            reviewer="style_reviewer",
        ),
        Finding(
            id="speculative_robustness",
            file="Verifier.java",
            line=4,
            severity="warning",
            category="robustness",
            message="replaceAll may be brittle for a hypothetical directory name.",
            reviewer="style_reviewer",
        ),
    ]
    diff = _summary(
        "Verifier.java",
        "class Verifier {\n"
        "  var reader = new BufferedReader(new StringReader(value));\n"
        "  Policy policy = createPolicy();\n"
        '  String path = source.replaceAll("messages_.*", "messages");\n'
        "}",
    )

    actionable, rejected = apply_actionability_gate(findings, diff)

    assert actionable == []
    assert {finding.id for finding in rejected} == {finding.id for finding in findings}
    assert all(finding.verified_by == "actionability-gate" for finding in rejected)


async def test_dependency_and_duplicate_evidence_contract_is_present_in_both_rounds():
    finding = Finding(
        id="finding_dependency_contract",
        file="Gemfile",
        line=4,
        severity="warning",
        category="dependency-version-range",
        message="Dependency constraint admits multiple versions.",
        confidence=0.9,
        reviewer="dependency_reviewer",
    )
    llm = PromptCaptureLLM(finding.id)

    await DynamicCalibrator(llm, build_registry()).calibrate(
        [finding],
        _summary("Gemfile", 'gem "rack", "~> 1.6"'),
    )

    assert len(llm.human_prompts) == 2
    for prompt in llm.human_prompts:
        assert "dependency-version-range" in prompt
        assert "admits multiple versions" in prompt
        assert "${{ secrets.NAME }}" in prompt
        assert "most" in prompt and "specific" in prompt
        assert "risky value" in prompt
        assert "ordinary native button" in prompt
        assert "Command::new" in prompt
        assert "public path-like" in prompt


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
    stdout, _ = proc.communicate()
    return stdout.decode()


def list_logs() -> str:
    return subprocess.run(
        "ls -la /var/log/", shell=True, capture_output=True, text=True
    ).stdout


def ping_host(host: str = "localhost") -> str:
    return subprocess.check_output(["ping", "-c", "1", host]).decode()
'''
    findings = [
        Finding(
            id="finding_count_exit_status",
            file="helpers.py",
            line=6,
            severity="warning",
            category="error-handling",
            message="subprocess.run return code is not checked before stdout is returned.",
            suggestion="Pass check=True or inspect result.returncode.",
            confidence=0.9,
            reviewer="style_reviewer",
        ),
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
            id="finding_grep_exit_status",
            file="helpers.py",
            line=19,
            severity="warning",
            category="error-handling",
            message="The grep process exit code is ignored after communicate().",
            suggestion="Inspect proc.returncode and distinguish no matches from an execution error.",
            confidence=0.8,
            reviewer="style_reviewer",
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

    assert len(result) == 6
    assert {finding.status for finding in result} == {"false_positive"}
    assert {finding.verified_by for finding in result} == {"code-evidence"}


async def test_python_code_evidence_preserves_non_status_error_for_stdout_wrapper():
    source = """import subprocess


def read_count(path: str) -> str:
    result = subprocess.run(["wc", "-l", path], capture_output=True, text=True)
    return result.stdout
"""
    finding = Finding(
        id="finding_stdout_encoding",
        file="worker.py",
        line=5,
        severity="warning",
        category="error-handling",
        message=(
            "subprocess.run decodes command stdout with the locale default; "
            "non-UTF-8 output can raise UnicodeDecodeError."
        ),
        suggestion="Set an explicit encoding and errors policy for textual stdout.",
        confidence=0.9,
        reviewer="style_reviewer",
    )
    llm = ConfirmingBatchLLM([finding.id])

    result = await DynamicCalibrator(llm, build_registry()).calibrate([finding], _summary("worker.py", source))

    assert llm.calls == 2
    assert result[0].status == "confirmed"
    assert result[0].verified_by == "judge"


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


def publish(package: str) -> str:
    result = subprocess.run(["publishctl", package], capture_output=True, text=True)
    return result.stdout


def parsed_count(path: str) -> int:
    result = subprocess.run(["wc", "-l", path], capture_output=True, text=True)
    return int(result.stdout.split()[0])
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
        Finding(
            id="finding_publish_exit_status",
            file="worker.py",
            line=17,
            severity="error",
            category="error-handling",
            message="The publish command return code is ignored, so a failed deployment is reported as output.",
            suggestion="Check result.returncode before reporting deployment output.",
            confidence=0.9,
            reviewer="style_reviewer",
        ),
        Finding(
            id="finding_count_parse_exit_status",
            file="worker.py",
            line=22,
            severity="warning",
            category="ignored-error",
            message="A non-zero wc exit status is ignored before its stdout is parsed as a count.",
            suggestion="Check result.returncode and raise the command failure before parsing stdout.",
            confidence=0.9,
            reviewer="style_reviewer",
        ),
    ]
    llm = ConfirmingBatchLLM([finding.id for finding in findings])
    calibrator = DynamicCalibrator(llm, build_registry())

    result = await calibrator.calibrate(findings, _summary("worker.py", source))

    assert llm.calls == 2
    assert len(result) == 5
    assert {finding.status for finding in result} == {"confirmed"}
    assert {finding.verified_by for finding in result} == {"judge"}


async def test_python_code_evidence_does_not_suppress_output_wrapper_from_partial_hunk():
    diff = """--- worker.py (+4 -0)
@@ -40,0 +40,4 @@
+def count_lines(path: str) -> str:
+    result = subprocess.run(["wc", "-l", path], capture_output=True, text=True)
+    return result.stdout
+
"""
    finding = Finding(
        id="partial_count_exit_status",
        file="worker.py",
        line=41,
        severity="warning",
        category="error-handling",
        message="The subprocess return code is ignored before stdout is returned.",
        suggestion="Pass check=True or inspect result.returncode.",
        confidence=0.9,
        reviewer="style_reviewer",
    )
    llm = ConfirmingBatchLLM([finding.id])

    result = await DynamicCalibrator(llm, build_registry()).calibrate([finding], diff)

    assert llm.calls == 2
    assert result[0].status == "confirmed"
    assert result[0].verified_by == "judge"


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


def test_actionability_gate_rejects_static_live_noise_pure_naming_and_manual_count_micro_optimization():
    source = """function showStatus(message: string) {
  document.getElementById('status')!.textContent = message;
}

def count_lines(path: str) -> str:
    return run_wc(path)

def ping_localhost(constant_host: str) -> bool:
    return ping(constant_host)

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
            id="ping_name_preference",
            file="mixed.txt",
            line=8,
            category="naming",
            message=(
                "ping_localhost is misleading because constant_host can select any host; "
                "the naming is inconsistent with the implementation."
            ),
            confidence=0.85,
            reviewer="style_reviewer",
        ),
        Finding(
            id="micro_optimization",
            file="mixed.txt",
            line=11,
            category="performance",
            message="The manual count loop can be replaced by len() for O(1) access.",
            confidence=1.0,
            reviewer="performance_reviewer",
        ),
    ]

    actionable, rejected = apply_actionability_gate(findings, _summary("mixed.txt", source))

    assert actionable == []
    assert rejected == findings
    assert {finding.verified_by for finding in rejected} == {"actionability-gate"}


def test_actionability_gate_rejects_go_package_naming_conventions_without_observable_failure():
    findings = [
        Finding(
            id="go_package_name_cn",
            file="gauntlet_decoys/account_store.go",
            line=1,
            category="naming",
            message="包名 gauntlet_decoys 使用了下划线，不符合 Go 惯例。",
            suggestion="改为 gauntletdecoys。",
            reviewer="style_reviewer",
        ),
        Finding(
            id="go_package_name_en",
            file="gauntlet_decoys/account_store.go",
            line=1,
            category="naming",
            message="Package name gauntlet_decoys uses an underscore and violates the Go naming convention.",
            suggestion="Rename the package to gauntletdecoys.",
            reviewer="style_reviewer",
        ),
    ]

    actionable, rejected = apply_actionability_gate(
        findings,
        _summary("gauntlet_decoys/account_store.go", "package gauntlet_decoys"),
    )

    assert actionable == []
    assert rejected == findings


def test_actionability_gate_preserves_go_package_name_with_concrete_compile_failure():
    finding = Finding(
        id="go_package_compile_failure",
        file="client.go",
        line=1,
        category="naming",
        message="包名与生成代码不匹配，导致 API 调用者编译错误。",
        reviewer="style_reviewer",
    )

    actionable, rejected = apply_actionability_gate(
        [finding],
        _summary("client.go", "package generated_client"),
    )

    assert actionable == [finding]
    assert rejected == []


def test_actionability_gate_rejects_optional_parameter_style_advice_but_preserves_unsafe_get():
    source = """import java.util.Optional;
class UserController {
    String getUserName(Optional<String> userId) {
        return userId.get();
    }
}
"""
    chinese_preference = Finding(
        id="optional_parameter_cn",
        file="UserController.java",
        line=3,
        category="optional-misuse",
        message="Optional 作为方法参数是反模式，强制调用方传入 Optional 会增加复杂性且设计意图是用于返回值。",
        suggestion="将参数改为 String，并在方法内部处理空值检查。",
        reviewer="style_reviewer",
    )
    english_preference = Finding(
        id="optional_parameter_en",
        file="UserController.java",
        line=3,
        category="optional-misuse",
        message="Using Optional as a method parameter is an anti-pattern and adds design complexity.",
        suggestion="Use Optional only as a return value.",
        reviewer="style_reviewer",
    )
    unsafe_get = Finding(
        id="optional_unsafe_get",
        file="UserController.java",
        line=4,
        category="optional-misuse",
        message=(
            "Optional is used as a method parameter and userId.get() can throw "
            "NoSuchElementException when the value is empty."
        ),
        reviewer="quality_reviewer",
    )

    actionable, rejected = apply_actionability_gate(
        [chinese_preference, english_preference, unsafe_get],
        _summary("UserController.java", source),
    )

    assert actionable == [unsafe_get]
    assert rejected == [chinese_preference, english_preference]


def test_actionability_gate_routes_unnecessary_linear_count_through_manual_count_gate():
    source = """pub fn count_items(items: &[String]) -> usize {
    let mut count = 0;
    for _item in items { count += 1; }
    count
}
"""
    micro_optimization = Finding(
        id="linear_count_preference",
        file="config_loader.rs",
        line=1,
        category="unnecessary-linear-count",
        message="The manual O(n) count loop can be replaced by Vec::len(), which provides O(1) access.",
        reviewer="performance_reviewer",
    )
    meaningful_impact = Finding(
        id="linear_count_hot_path",
        file="config_loader.rs",
        line=1,
        category="unnecessary-linear-count",
        message=(
            "The manual O(n) count loop runs on every request over an unbounded collection, increasing request latency."
        ),
        reviewer="performance_reviewer",
    )

    actionable, rejected = apply_actionability_gate(
        [micro_optimization, meaningful_impact],
        _summary("config_loader.rs", source),
    )

    assert actionable == [meaningful_impact]
    assert rejected == [micro_optimization]


def test_actionability_gate_rejects_static_vue_and_svelte_raw_html_live_region_guesses():
    diff = "\n".join(
        [
            _summary("StaticBio.vue", '<div class="bio" v-html="bio" />'),
            _summary("StaticBio.svelte", "<article>{@html bio}</article>"),
        ]
    )
    findings = [
        Finding(
            id="vue_static_live",
            file="StaticBio.vue",
            line=1,
            category="missing-aria-live",
            message="v-html content has no aria-live announcement.",
            reviewer="accessibility_reviewer",
        ),
        Finding(
            id="svelte_static_live",
            file="StaticBio.svelte",
            line=1,
            category="missing-live-region",
            message="The {@html} carrier has no live-region role.",
            reviewer="accessibility_reviewer",
        ),
    ]

    actionable, rejected = apply_actionability_gate(findings, diff)

    assert actionable == []
    assert rejected == findings


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
                "window.addEventListener('message', event => {\n"
                "  status.textContent = event.data;\n"
                "});\n"
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
            line=3,
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


async def test_high_confidence_detector_missing_alt_auto_confirms_complete_file():
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


async def test_detector_missing_label_auto_confirms_complete_file_replay():
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
    result = await DynamicCalibrator(FailingLLM(), build_registry()).calibrate(
        [finding],
        _summary("view.tsx", '<input name="email" />'),
    )

    assert result[0].verified_by == "detector-auto"


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


class PoisonedCalibrationLLM:
    """Return malformed output when one designated finding is present."""

    def __init__(self, poisoned_id: str, poisoned_round: str = "adversarial") -> None:
        self.poisoned_id = poisoned_id
        self.poisoned_round = poisoned_round
        self.round_counts = {"adversarial": 0, "judge": 0}
        self.records: list[tuple[str, list[str]]] = []

    async def ainvoke(self, messages):
        human = str(messages[1].content)
        round_name = "adversarial" if '"adjusted_confidence"' in human else "judge"
        finding_ids = re.findall(r"^- \[([^\]]+)\]", human, re.MULTILINE)
        self.round_counts[round_name] += 1
        self.records.append((round_name, finding_ids))
        if round_name == self.poisoned_round and self.poisoned_id in finding_ids:
            return SimpleNamespace(content="not-json")
        if round_name == "adversarial":
            payload = [
                {
                    "finding_id": finding_id,
                    "verdict": "confirmed",
                    "adjusted_confidence": 0.4,
                    "challenge": "concrete evidence",
                }
                for finding_id in finding_ids
            ]
        else:
            payload = [
                {
                    "finding_id": finding_id,
                    "verdict": "confirmed",
                    "confidence": 0.9,
                    "reason": "concrete evidence",
                }
                for finding_id in finding_ids
            ]
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


async def test_large_adversarial_missing_id_recovers_on_bounded_retry():
    findings, diff, _markers = _large_calibration_fixture(padding_lines=4)
    llm = BoundedCalibrationLLM(invalid_round="adversarial", invalid_batch=2)

    result = await DynamicCalibrator(llm, build_registry()).calibrate(findings, diff)

    assert llm.round_counts == {"adversarial": 4, "judge": 3}
    assert all(finding.status == "confirmed" for finding in result)
    assert all(finding.verified_by == "judge" for finding in result)


async def test_large_judge_missing_id_recovers_on_bounded_retry():
    findings, diff, _markers = _large_calibration_fixture(padding_lines=4)
    llm = BoundedCalibrationLLM(invalid_round="judge", invalid_batch=2)

    result = await DynamicCalibrator(llm, build_registry()).calibrate(findings, diff)

    assert llm.round_counts == {"adversarial": 3, "judge": 4}
    assert all(finding.status == "confirmed" for finding in result)
    assert all(finding.verified_by == "judge" for finding in result)


async def test_persistent_poisoned_finding_is_split_and_suppressed_without_failing_peers():
    findings, diff, _markers = _large_calibration_fixture(count=4, padding_lines=4)
    poisoned = findings[0]
    llm = PoisonedCalibrationLLM(poisoned.id)

    result = await DynamicCalibrator(llm, build_registry()).calibrate(findings, diff)

    result_by_id = {finding.id: finding for finding in result}
    assert result_by_id[poisoned.id].status == "false_positive"
    assert result_by_id[poisoned.id].confidence == 0.0
    assert result_by_id[poisoned.id].verified_by == "calibration-fail-closed"
    assert all(
        result_by_id[finding.id].status == "confirmed" and result_by_id[finding.id].verified_by == "judge"
        for finding in findings[1:]
    )
    assert llm.round_counts == {"adversarial": 6, "judge": 1}
    judge_ids = {
        finding_id for round_name, finding_ids in llm.records if round_name == "judge" for finding_id in finding_ids
    }
    assert poisoned.id not in judge_ids


async def test_persistent_poisoned_judgment_is_split_and_suppressed_without_failing_peers():
    findings, diff, _markers = _large_calibration_fixture(count=4, padding_lines=4)
    poisoned = findings[0]
    llm = PoisonedCalibrationLLM(poisoned.id, poisoned_round="judge")

    result = await DynamicCalibrator(llm, build_registry()).calibrate(findings, diff)

    result_by_id = {finding.id: finding for finding in result}
    assert result_by_id[poisoned.id].status == "false_positive"
    assert result_by_id[poisoned.id].confidence == 0.0
    assert result_by_id[poisoned.id].verified_by == "calibration-fail-closed"
    assert all(
        result_by_id[finding.id].status == "confirmed" and result_by_id[finding.id].verified_by == "judge"
        for finding in findings[1:]
    )
    assert llm.round_counts == {"adversarial": 1, "judge": 6}


def test_actionability_rejects_ungrounded_dependency_and_a11y_absence():
    diff = _summary("view.tsx", '<Button icon="more" />')
    findings = [
        Finding(
            id="dependency_memory",
            file="package.json",
            line=2,
            category="dependency-security",
            message="This version may have a known CVE.",
            reviewer="dependency_reviewer",
        ),
        Finding(
            id="custom_button_name",
            file="view.tsx",
            line=1,
            category="missing-aria-label",
            message="The custom Button may lack an accessible name.",
            reviewer="accessibility_reviewer",
        ),
        Finding(
            id="keyboard_contract",
            file="view.tsx",
            line=1,
            category="keyboard-navigation",
            message="The custom control handles click but not keyboard activation.",
            reviewer="accessibility_reviewer",
        ),
    ]

    actionable, rejected = apply_actionability_gate(findings, diff)

    assert {finding.id for finding in actionable} == {"keyboard_contract"}
    assert {finding.id for finding in rejected} == {"dependency_memory", "custom_button_name"}


def test_actionability_requires_local_database_sink_for_n_plus_one():
    diff = _summary(
        "service.ts",
        "for (const item of items) {\n"
        "  await client.request(item);\n"
        "}\n"
        "for (const user of users) {\n"
        "  await prisma.account.findFirst({ where: { userId: user.id } });\n"
        "}",
    )
    network = Finding(
        id="network_loop",
        file="service.ts",
        line=2,
        category="n-plus-one",
        message="The loop makes one network request per item.",
        reviewer="performance_reviewer",
    )
    database = Finding(
        id="database_loop",
        file="service.ts",
        line=5,
        category="n-plus-one",
        message="The loop makes one database query per user.",
        reviewer="performance_reviewer",
    )

    actionable, rejected = apply_actionability_gate([network, database], diff)

    assert actionable == [database]
    assert rejected == [network]


def test_actionability_rejects_performance_nit_without_scale_proof():
    diff = _summary("slots.ts", "for (const slot of slots) {\n  dayjs(slot.start).hour();\n}")
    nit = Finding(
        id="small_allocation",
        file="slots.ts",
        line=2,
        category="performance",
        message="This creates a small object in each iteration.",
        reviewer="performance_reviewer",
    )
    quadratic = Finding(
        id="quadratic",
        file="slots.ts",
        line=2,
        category="performance",
        message="This nested scan is O(n^2) for an unbounded input.",
        reviewer="performance_reviewer",
    )

    actionable, rejected = apply_actionability_gate([nit, quadratic], diff)

    assert actionable == [quadratic]
    assert rejected == [nit]


def test_prompt_diff_evidence_has_hard_fair_budgets():
    registry = build_registry()
    huge = "x" * 50_000
    planner = build_planner_prompt(
        {
            "registry": registry,
            "repo": "owner/repo",
            "pr_number": 1,
            "files_changed": ["a.py"],
            "diff_summary": huge,
        }
    )[1]["content"]
    reviewer = build_reviewer_prompt(
        {
            "registry": registry,
            "reviewer_type": "style",
            "agent_name": "style_reviewer",
            "files_to_review": ["a.py", "b.py", "c.py"],
            "diffs": {"a.py": huge, "b.py": huge, "c.py": huge},
        }
    )[1]["content"]

    assert "diff truncated to prompt budget" in planner
    assert len(planner) < 30_000
    assert all(f"### {path}" in reviewer for path in ("a.py", "b.py", "c.py"))
    assert "diff truncated to prompt budget" in reviewer
    assert len(reviewer) < 42_000


def test_actionability_rejects_specialist_category_misuse_and_preferences():
    diff = "\n".join(
        [
            _summary(
                "service.java",
                'private final Pattern HTML_TAGS = Pattern.compile("<[^>]+>");\n'
                "try (BufferedReader reader = new BufferedReader(new StringReader(content))) {\n"
                "  return reader.readLine();\n"
                "}",
            ),
            _summary(
                "view.tsx",
                'return <div className="grid">{codes.map(code => <div>{code}</div>)}</div>;',
            ),
            _summary(
                "writer.go",
                "go func() {\n  legacy.Update(ctx, value)\n}()",
            ),
        ]
    )
    findings = [
        Finding(
            id="static_preference",
            file="service.java",
            line=1,
            category="naming",
            message="The constant should be static.",
            reviewer="style_reviewer",
        ),
        Finding(
            id="memory_reader",
            file="service.java",
            line=2,
            category="resource-management",
            message="BufferedReader is not explicitly closed.",
            reviewer="style_reviewer",
        ),
        Finding(
            id="semantic_preference",
            file="view.tsx",
            line=1,
            category="semantic-html",
            message="Use a list instead of div elements.",
            reviewer="accessibility_reviewer",
        ),
        Finding(
            id="async_consistency",
            file="writer.go",
            line=1,
            category="goroutine-leak",
            message="The caller cannot observe whether the asynchronous update succeeds.",
            reviewer="performance_reviewer",
        ),
        Finding(
            id="real_leak",
            file="writer.go",
            line=1,
            category="goroutine-leak",
            message="The goroutine waits forever and has no cancellation path.",
            reviewer="performance_reviewer",
        ),
    ]

    actionable, rejected = apply_actionability_gate(findings, diff)

    assert actionable == [findings[-1]]
    assert {finding.id for finding in rejected} == {
        "static_preference",
        "memory_reader",
        "semantic_preference",
        "async_consistency",
    }


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


def test_actionability_gate_rejects_missing_mock_verification_advice():
    finding = Finding(
        id="mock_verification",
        file="pkg/service_test.go",
        line=12,
        category="mock-validation",
        message="mock 预期调用缺少验证，即使实现没有调用也可能通过。",
        suggestion="在测试末尾调用 AssertExpectations(t)。",
        reviewer="testing_reviewer",
    )
    diff = _summary(
        "pkg/service_test.go",
        'func TestService(t *testing.T) {\n    mock.On("Save").Return(nil)\n}',
    )

    actionable, rejected = apply_actionability_gate([finding], diff)

    assert actionable == []
    assert rejected == [finding]
