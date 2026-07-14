import os
import subprocess
import sys
from pathlib import Path

from reviewforge.engine.detectors.accessibility import detect_accessibility_findings


def _patch(body: str) -> str:
    lines = body.splitlines()
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)


def test_detects_concrete_missing_alt_and_label_across_framework_markup():
    findings = detect_accessibility_findings(
        {
            "Profile.vue": _patch('<template><img :src="avatar" /></template>'),
            "Form.tsx": _patch('<form><input name="email" /></form>'),
            "admin.component.ts": _patch('template: `<img [src]="avatarUrl">`'),
        }
    )

    assert {(item.file, item.line, item.category) for item in findings} == {
        ("Profile.vue", 1, "missing-alt"),
        ("Form.tsx", 1, "missing-label"),
        ("admin.component.ts", 1, "missing-alt"),
    }
    assert next(item for item in findings if item.category == "missing-alt").confidence == 0.99
    assert next(item for item in findings if item.category == "missing-label").confidence < 0.96


def test_skips_decorative_dynamic_and_explicitly_named_markup():
    findings = detect_accessibility_findings(
        {
            "Safe.tsx": _patch(
                """<>
  <img src="/spacer.png" alt="" />
  <img {...imageProps} />
  <Image src="/logo.png" aria-label="Company" />
  <Image src="/custom-card.png" />
  <label htmlFor="email">Email</label>
  <input id="email" />
  <input aria-label="Search" />
  <input title="Filter results" />
  <input type="hidden" />
</>"""
            )
        }
    )

    assert findings == []


def test_unrelated_material_label_does_not_name_a_native_input():
    findings = detect_accessibility_findings(
        {
            "Search.tsx": _patch(
                """<mat-label>Material-only label</mat-label>
<input id="native-search" />"""
            )
        }
    )

    assert [(item.line, item.category, item.confidence) for item in findings] == [(2, "missing-label", 0.9)]


def test_ignores_non_markup_files_even_when_strings_contain_tags():
    findings = detect_accessibility_findings({"fixture.py": _patch('example = "<img src=x>"')})

    assert findings == []


def test_skips_test_fixture_and_example_markup_instead_of_auto_confirming_it():
    findings = detect_accessibility_findings(
        {
            "tests/Profile.test.tsx": _patch('<img src="avatar.png" /><input />'),
            "fixtures/page.html": _patch('<img src="fixture.png" />'),
            "examples/demo.vue": _patch("<template><input /></template>"),
        }
    )

    assert findings == []


def test_finding_anchors_can_be_imported_in_a_fresh_interpreter():
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(Path(__file__).parents[1] / "src"), env.get("PYTHONPATH", "")])
    )

    result = subprocess.run(
        [sys.executable, "-c", "import reviewforge.engine.finding_anchors"],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert result.returncode == 0, result.stderr
