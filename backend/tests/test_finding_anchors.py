from reviewforge.core.state import Finding
from reviewforge.engine.finding_anchors import (
    reanchor_accessibility_findings,
    reanchor_security_detector_duplicates,
    unsupported_python_open_redirect_findings,
)


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


def _security_finding(
    finding_id: str,
    file_path: str,
    line: int,
    category: str,
    message: str,
    *,
    detector: bool = False,
) -> Finding:
    return Finding(
        id=finding_id,
        file=file_path,
        line=line,
        category=category,
        message=message,
        verified_by="detector" if detector else "",
    )


def test_reanchors_online_java_security_duplicates_from_unique_message_symbols():
    file_path = "eval_cases/pr1/UserController.java"
    diff = _summary(
        file_path,
        """public class UserController {
    public void deleteUser(String userId) {
        String query = "DELETE FROM users WHERE id = " + userId;
        Statement stmt = dbConnection.createStatement();
        stmt.executeUpdate(query);
    }

    private static final String DB_PASSWORD = "admin123!";

    public void runBackup(String backupPath) throws IOException {
        Runtime.getRuntime().exec("tar -czf " + backupPath);
    }
}""",
    )
    findings = [
        _security_finding("sql-det", file_path, 4, "sql-injection", "Raw JDBC Statement is used.", detector=True),
        _security_finding("secret-det", file_path, 8, "hardcoded-secrets", "Hard-coded credentials.", detector=True),
        _security_finding("command-det", file_path, 11, "command-injection", "Runtime.exec is used.", detector=True),
        _security_finding(
            "sql-llm", file_path, 2, "sql-injection", "deleteUser方法直接拼接SQL查询，userId参数未经校验。"
        ),
        _security_finding("secret-llm", file_path, 6, "hardcoded-secrets", "DB_PASSWORD硬编码在源代码中。"),
        _security_finding(
            "command-llm", file_path, 9, "command-injection", "runBackup方法直接拼接backupPath执行shell命令。"
        ),
    ]

    changed = reanchor_security_detector_duplicates(findings, diff)

    assert {(finding.id, finding.line) for finding in changed} == {
        ("sql-llm", 4),
        ("secret-llm", 8),
        ("command-llm", 11),
    }


def test_reanchors_online_python_duplicates_to_the_matching_detector_only():
    file_path = "eval_cases/pr1/data_importer.py"
    diff = _summary(
        file_path,
        """def load_state(filepath: str):
    with open(filepath, "rb") as handle:
        return pickle.loads(handle.read())


def parse_config(config_str: str):
    return yaml.load(config_str)


def run_command(user_cmd: str):
    return os.popen(user_cmd).read()""",
    )
    findings = [
        _security_finding(
            "pickle-det", file_path, 3, "insecure-deserialization", "Unsafe deserialization API.", detector=True
        ),
        _security_finding(
            "yaml-det", file_path, 7, "insecure-deserialization", "YAML load without safe loader.", detector=True
        ),
        _security_finding("command-det", file_path, 11, "command-injection", "Dynamic shell API.", detector=True),
        _security_finding(
            "pickle-llm", file_path, 1, "insecure-deserialization", "load_state使用pickle.loads反序列化不可信数据。"
        ),
        _security_finding(
            "command-llm", file_path, 9, "command-injection", "run_command直接执行user_cmd，可执行任意命令。"
        ),
    ]

    changed = reanchor_security_detector_duplicates(findings, diff)

    assert {(finding.id, finding.line) for finding in changed} == {("pickle-llm", 3), ("command-llm", 11)}
    assert next(finding for finding in findings if finding.id == "yaml-det").line == 7


def test_security_reanchor_does_not_guess_between_adjacent_independent_sinks():
    file_path = "src/jobs.py"
    diff = _summary(
        file_path,
        """def first(command):
    return os.popen(command).read()

def second(command):
    return os.popen(command).read()""",
    )
    findings = [
        _security_finding("first-det", file_path, 2, "command-injection", "Dynamic shell API.", detector=True),
        _security_finding("second-det", file_path, 5, "command-injection", "Dynamic shell API.", detector=True),
        _security_finding("llm", file_path, 3, "command-injection", "os.popen executes a dynamic command."),
    ]

    assert reanchor_security_detector_duplicates(findings, diff) == []
    assert findings[-1].line == 3


def test_reanchors_workflow_semantic_category_drift_with_unique_diff_evidence():
    file_path = ".github/workflows/gauntlet-deploy.yml"
    diff = _summary(
        file_path,
        """steps:
  - name: unsafe remote installer
    run: curl https://example.invalid/install.sh | bash
  - name: print deployment token
    run: echo ${{ secrets.DEPLOY_TOKEN }}
  - name: deploy pull request
    run: ./deploy.sh "${{ github.event.pull_request.title }}"
""",
    )
    findings = [
        _security_finding(
            "download-det", file_path, 3, "supply-chain-risk", "Remote script is piped to shell.", detector=True
        ),
        _security_finding("secret-det", file_path, 5, "data-leak", "Workflow prints a secret.", detector=True),
        _security_finding("command-det", file_path, 7, "ci-security", "PR title reaches a shell.", detector=True),
        _security_finding("download-llm", file_path, 1, "code-injection", "curl下载脚本后直接pipe到bash执行。"),
        _security_finding("secret-llm", file_path, 2, "hardcoded-secrets", "DEPLOY_TOKEN被直接打印到日志。"),
        _security_finding("command-llm", file_path, 4, "command-injection", "deploy.sh直接使用pull_request.title。"),
    ]

    changed = reanchor_security_detector_duplicates(findings, diff)

    assert {(finding.id, finding.line, finding.category) for finding in changed} == {
        ("download-llm", 3, "supply-chain-risk"),
        ("secret-llm", 5, "data-leak"),
        ("command-llm", 7, "ci-security"),
    }


def test_rejects_python_url_builder_without_redirect_sink_but_keeps_real_redirect():
    file_path = "gauntlet_decoys/prompt_injection.py"
    diff = _summary(
        file_path,
        """def redirect_destination(next_url: str) -> str:
    return next_url


def perform_redirect(next_url: str):
    return RedirectResponse(url=next_url)""",
    )
    builder = _security_finding(
        "builder",
        file_path,
        3,
        "open-redirect",
        "函数 `redirect_destination` 直接返回未经验证的 next_url。",
    )
    real_redirect = _security_finding(
        "real", file_path, 5, "open-redirect", "perform_redirect passes next_url to RedirectResponse."
    )
    detector = _security_finding(
        "detector", file_path, 1, "open-redirect", "Redirect target is dynamic.", detector=True
    )

    rejected = unsupported_python_open_redirect_findings([builder, real_redirect, detector], diff)

    assert rejected == [builder]


def test_python_open_redirect_gate_fails_open_for_truncated_patch():
    file_path = "src/redirects.py"
    patch = "@@ -20,0 +20,2 @@\n+def redirect_destination(next_url):\n+    return next_url"
    finding = _security_finding("builder", file_path, 20, "open-redirect", "redirect_destination returns next_url.")

    assert unsupported_python_open_redirect_findings([finding], patch) == []
