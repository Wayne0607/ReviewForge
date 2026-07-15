from reviewforge.engine.detectors.quality import detect_quality_findings


def _new_file_diff(source: str) -> str:
    lines = source.splitlines()
    return "\n".join(
        [
            "diff --git a/src/Panel.tsx b/src/Panel.tsx",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/src/Panel.tsx",
            f"@@ -0,0 +1,{len(lines)} @@",
            *(f"+{line}" for line in lines),
        ]
    )


def test_detects_effectful_import_called_directly_during_react_render():
    source = """import { formatLabel, storeSessionToken, spawnReport } from './service';

export function ReportPanel({ token, command }: { token: string; command: string }) {
  formatLabel(token);
  storeSessionToken(token);
  spawnReport(command);
  return <section>{token}</section>;
}
"""

    findings = detect_quality_findings({"src/Panel.tsx": _new_file_diff(source)})
    render_effects = [finding for finding in findings if finding.category == "side-effect-in-render"]

    assert len(render_effects) == 1
    assert render_effects[0].line == 5
    assert "storeSessionToken" in render_effects[0].message
    assert render_effects[0].confidence == 0.85


def test_ignores_pure_calls_hooks_and_event_handler_effects():
    source = """import { formatLabel, storeSessionToken } from './service';
import { useEffect } from 'react';

export function SafePanel({ token }: { token: string }) {
  const label = formatLabel(token);
  useEffect(() => {
    storeSessionToken(token);
  }, [token]);
  return <button onClick={() => storeSessionToken(token)}>{label}</button>;
}
"""

    findings = detect_quality_findings({"src/Panel.tsx": _new_file_diff(source)})

    assert all(finding.category != "side-effect-in-render" for finding in findings)


def test_partial_hunk_never_proves_render_time_side_effect():
    patch = """@@ -10,2 +10,3 @@ export function Panel({ token }) {
+  storeSessionToken(token);
   return <section>{token}</section>;
 }"""

    findings = detect_quality_findings({"src/Panel.tsx": patch})

    assert all(finding.category != "side-effect-in-render" for finding in findings)


def test_ignores_fake_imports_inside_comments_and_string_literals():
    source = """// import { saveData } from "./commented-api";
const documentation = 'import { saveData } from "./string-api"';
function saveData(value: string) { return value; }

export function Panel({ value }: { value: string }) {
  saveData(value);
  return <span>{value}</span>;
}
"""

    findings = detect_quality_findings({"src/Panel.tsx": _new_file_diff(source)})

    assert all(finding.category != "side-effect-in-render" for finding in findings)


def test_ignores_import_name_shadowed_by_component_parameter_or_local_binding():
    parameter_shadow = """import { saveData } from "./api";

export function Panel({ saveData, value }: Props) {
  saveData(value);
  return <span>{value}</span>;
}
"""
    local_shadow = """import { saveData } from "./api";

export function Panel({ value }: Props) {
  const saveData = (nextValue: string) => nextValue;
  saveData(value);
  return <span>{value}</span>;
}
"""

    for source in (parameter_shadow, local_shadow):
        findings = detect_quality_findings({"src/Panel.tsx": _new_file_diff(source)})
        assert all(finding.category != "side-effect-in-render" for finding in findings)
