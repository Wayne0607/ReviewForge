import json
from types import SimpleNamespace

from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding
from reviewforge.engine.calibrator import DynamicCalibrator


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
