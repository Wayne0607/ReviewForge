from reviewforge.core.state import Finding
from reviewforge.engine.finding_anchors import reanchor_accessibility_findings


def _summary(file_path: str, body: str) -> str:
    lines = body.splitlines()
    patch = "\n".join(f"+{line}" for line in lines)
    return f"--- {file_path} (+{len(lines)} -0)\n@@ -0,0 +1,{len(lines)} @@\n{patch}"


def test_reanchors_react_alt_and_label_to_concrete_elements():
    file_path = "gauntlet_fullstack/seed_frontend.tsx"
    diff = _summary(
        file_path,
        """export function LoginForm() {
  return (
    <form>
      <img src="/avatar.png" />
      <input name="email" onChange={() => save()} />
    </form>
  );
}""",
    )
    findings = [
        Finding(file=file_path, line=1, category="missing-alt", message="image lacks alt"),
        Finding(file=file_path, line=2, category="missing-label", message="input lacks a label"),
    ]

    changed = reanchor_accessibility_findings(findings, diff)

    assert {finding.line for finding in changed} == {4, 5}
    assert [finding.line for finding in findings] == [4, 5]


def test_reanchors_alias_and_multiline_image_tag():
    file_path = "src/Profile.vue"
    diff = _summary(
        file_path,
        """<template>
  <img
    :src="avatar"
    class="avatar"
  />
</template>""",
    )
    finding = Finding(file=file_path, line=1, category="alt-text", message="missing alt")

    changed = reanchor_accessibility_findings([finding], diff)

    assert changed == [finding]
    assert finding.category == "missing-alt"
    assert finding.line == 2


def test_does_not_reanchor_images_with_accessible_or_uncertain_props():
    file_path = "src/Safe.tsx"
    diff = _summary(
        file_path,
        """export const Safe = () => <>
  <img src="/decorative.png" alt="" />
  <img {...imageProps} />
  <Image src="/logo.png" aria-label="Company" />
</>;""",
    )
    finding = Finding(file=file_path, line=1, category="missing-alt", message="missing alt")

    assert reanchor_accessibility_findings([finding], diff) == []
    assert finding.line == 1


def test_does_not_reanchor_controls_with_real_labels_or_dynamic_props():
    file_path = "src/SafeForm.tsx"
    diff = _summary(
        file_path,
        """export const SafeForm = () => <form>
  <label htmlFor="email">Email</label>
  <input id="email" />
  <input aria-label="Search" />
  <input {...fieldProps} />
</form>;""",
    )
    finding = Finding(file=file_path, line=1, category="missing-form-label", message="missing label")

    assert reanchor_accessibility_findings([finding], diff) == []
    assert finding.category == "missing-label"
    assert finding.line == 1


def test_does_not_guess_between_equidistant_sinks():
    file_path = "src/Gallery.tsx"
    diff = _summary(
        file_path,
        """<>
  <img src="/one.png" />
  <div />
  <img src="/two.png" />
</>""",
    )
    finding = Finding(file=file_path, line=3, category="missing-alt", message="missing alt")

    assert reanchor_accessibility_findings([finding], diff) == []
    assert finding.line == 3
