from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding
from reviewforge.engine.calibrator import DynamicCalibrator
from reviewforge.engine.mock_llm import MockChatLLM


async def test_open_redirect_auto_confirms_as_security():
    calibrator = DynamicCalibrator(MockChatLLM(), build_registry())
    finding = Finding(
        file="AdminPreview.tsx",
        line=9,
        severity="error",
        category="open-redirect",
        message="redirectTo is assigned to window.location.href",
        confidence=0.95,
    )

    result = await calibrator.calibrate([finding], "diff")

    assert result[0].status == "confirmed"
    assert result[0].verified_by == "security-auto"


async def test_security_alias_auto_confirms_and_normalizes():
    calibrator = DynamicCalibrator(MockChatLLM(), build_registry())
    finding = Finding(
        file="settings.py",
        line=3,
        severity="error",
        category="hardcoded-secret",
        message="secret literal",
        confidence=0.9,
    )

    result = await calibrator.calibrate([finding], "diff")

    assert result[0].category == "hardcoded-secrets"
    assert result[0].status == "confirmed"
    assert result[0].verified_by == "security-auto"
