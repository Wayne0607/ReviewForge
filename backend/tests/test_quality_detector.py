from reviewforge.engine.detectors.quality import detect_quality_findings


def _patch(content: str) -> str:
    lines = content.splitlines()
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)


def _keys(findings):
    return {(finding.file, finding.line, finding.category) for finding in findings}


def test_quality_detector_covers_high_signal_multilanguage_shapes():
    findings = detect_quality_findings(
        {
            "src/UserService.java": _patch(
                "import java.util.Optional;\n"
                "class UserService {\n"
                "  String load(Optional<String> value) {\n"
                "    try { work(); }\n"
                "    catch (Exception error) {\n"
                "    }\n"
                "    return value.get();\n"
                "  }\n"
                "}"
            ),
            "src/UserCard.vue": _patch(
                "<script setup>\n"
                "import { computed } from 'vue'\n"
                "const props = defineProps<{ name: string }>()\n"
                "const title = computed(() => {\n"
                "  fetchUser(props.name)\n"
                "  return props.name\n"
                "})\n"
                "function reset() { props.name = '' }\n"
                "try { save() } catch (error) {\n"
                "}\n"
                "</script>\n"
                "<template>\n"
                '  <li v-for="item in items" v-if="item.active">{{ item.name }}</li>\n'
                "</template>"
            ),
            "src/importer.py": _patch("try:\n    load()\nexcept:\n    pass"),
            "src/processor.rb": _patch(
                "class Processor\n"
                "  def run\n"
                "    call\n"
                "  rescue Exception => error\n"
                "    warn error\n"
                "  end\n"
                "\n"
                "  def method_missing(name, *args)\n"
                "    return dispatch(name, args) if dynamic?(name)\n"
                "    super\n"
                "  end\n"
                "end"
            ),
            "src/worker.go": _patch(
                "package worker\n"
                "func run(db *sql.DB) {\n"
                '  cmd := exec.Command("job")\n'
                "  cmd.Run()\n"
                "  go func() {\n"
                "    for {\n"
                '      db.Query("SELECT 1")\n'
                "    }\n"
                "  }()\n"
                "  for i := 0; i < 3; i++ {\n"
                '    rows, _ := db.Query("SELECT 1")\n'
                "    defer rows.Close()\n"
                "  }\n"
                "  _ = db.Ping()\n"
                "}"
            ),
            "src/parser.rs": _patch(
                'pub fn parse(raw: &str) -> u32 { raw.parse::<u32>().unwrap() }\npub fn fail() { panic!("bad state") }'
            ),
            "src/BrowserPanel.tsx": _patch(
                'import { exec } from "child_process";\n'
                "export function run(command: string) {\n"
                "  localStorage.setItem('last', command);\n"
                "  exec(command);\n"
                "}"
            ),
        }
    )

    keys = _keys(findings)
    expected_categories = {
        "api-contract",
        "computed-side-effect",
        "correctness",
        "exception-handling",
        "ignored-error",
        "import-error",
        "lifecycle",
        "null-safety",
        "panic-risk",
        "resource-leak",
        "state-management",
    }
    assert {category for _file, _line, category in keys} == expected_categories
    assert ("src/UserService.java", 5, "exception-handling") in keys
    assert ("src/UserService.java", 7, "null-safety") in keys
    assert ("src/UserCard.vue", 5, "computed-side-effect") in keys
    assert ("src/UserCard.vue", 8, "state-management") in keys
    assert ("src/UserCard.vue", 9, "exception-handling") in keys
    assert ("src/UserCard.vue", 13, "correctness") in keys
    assert ("src/importer.py", 3, "exception-handling") in keys
    assert ("src/processor.rb", 4, "exception-handling") in keys
    assert ("src/processor.rb", 8, "api-contract") in keys
    assert ("src/worker.go", 4, "ignored-error") in keys
    assert ("src/worker.go", 6, "lifecycle") in keys
    assert ("src/worker.go", 7, "ignored-error") in keys
    assert ("src/worker.go", 12, "resource-leak") in keys
    assert ("src/worker.go", 14, "ignored-error") in keys
    assert ("src/parser.rs", 1, "panic-risk") in keys
    assert ("src/parser.rs", 2, "panic-risk") in keys
    assert ("src/BrowserPanel.tsx", 1, "import-error") in keys
    # The rows,_ query is represented by the stronger loop-defer finding, not
    # duplicated as a second warning on the same operation.
    assert ("src/worker.go", 11, "ignored-error") not in keys


def test_quality_detector_avoids_guarded_handled_and_server_only_controls():
    findings = detect_quality_findings(
        {
            "src/Safe.java": _patch(
                "Optional<String> value = lookup();\n"
                "if (value.isPresent()) { return value.get(); }\n"
                "try { work(); } catch (Exception error) { log(error); }"
            ),
            "src/Safe.vue": _patch(
                "const props = defineProps<{ name: string }>()\n"
                "const title = computed(() => props.name)\n"
                "try { save() } catch (error) { report(error) }\n"
                '<li v-for="item in items">{{ item }}</li>\n'
                '<p v-if="visible">Shown</p>'
            ),
            "src/safe.py": _patch("try:\n    load()\nexcept ValueError:\n    recover()"),
            "src/safe.rb": _patch(
                "class Safe\n"
                "  def method_missing(name, *args)\n"
                "    super\n"
                "  end\n"
                "\n"
                "  def respond_to_missing?(name, include_private = false)\n"
                "    super\n"
                "  end\n"
                "end"
            ),
            "src/safe.go": _patch(
                "if err := cmd.Run(); err != nil { return err }\n"
                "if err := db.Ping(); err != nil { return err }\n"
                'rows, err := db.Query("SELECT 1")\n'
                "go func() { for { select { case <-ctx.Done(): return } } }()\n"
                "for i := 0; i < 3; i++ { func() { defer rows.Close() }() }"
            ),
            "src/safe.rs": _patch("pub fn parse(raw: &str) -> Result<u32, Error> { Ok(raw.parse()?) }"),
            "src/ServerPanel.tsx": _patch(
                "'use server';\n"
                'import { execFile } from "node:child_process";\n'
                "export async function render() { return execFile('/bin/date') }"
            ),
        }
    )

    assert findings == []


def test_quality_detector_finds_provable_resource_and_log_continue_failures():
    findings = detect_quality_findings(
        {
            "src/Repository.java": _patch(
                "import java.sql.Statement;\n"
                "class Repository {\n"
                "  void delete() throws Exception {\n"
                "    Statement stmt = db.createStatement();\n"
                '    stmt.executeUpdate("DELETE FROM jobs");\n'
                "    stmt.close();\n"
                "  }\n"
                "}"
            ),
            "src/MixedRepository.java": _patch(
                "class MixedRepository {\n"
                "  void safe() throws Exception {\n"
                "    Statement stmt = db.createStatement();\n"
                '    try { stmt.executeUpdate("DELETE FROM jobs"); }\n'
                "    finally { stmt.close(); }\n"
                "  }\n"
                "  void unsafe() throws Exception {\n"
                "    Statement stmt = db.createStatement();\n"
                '    stmt.executeUpdate("DELETE FROM jobs");\n'
                "  }\n"
                "}"
            ),
            "src/Poller.vue": _patch("<script setup>\nsetInterval(() => refresh(), 1000)\n</script>"),
            "src/importer.py": _patch(
                "import sqlite3\n"
                "def load():\n"
                "    conn = sqlite3.connect('data.db')\n"
                "    rows = conn.execute('SELECT 1').fetchall()\n"
                "    return rows"
            ),
            "src/direct_result.py": _patch(
                "import sqlite3\n"
                "def load():\n"
                "    conn = sqlite3.connect('data.db')\n"
                "    return conn.execute('SELECT 1').fetchall()"
            ),
            "src/conditional_close.py": _patch(
                "import sqlite3\n"
                "def load(close_now):\n"
                "    conn = sqlite3.connect('data.db')\n"
                "    if close_now:\n"
                "        conn.close()\n"
                "    return []"
            ),
            "src/service.go": _patch(
                "package service\n"
                "func run(db *sql.DB) error {\n"
                '  _, err := db.Exec("DELETE FROM jobs")\n'
                "  if err != nil {\n"
                '    fmt.Println("delete failed", err)\n'
                "  }\n"
                "  return nil\n"
                "}"
            ),
        }
    )

    keys = _keys(findings)
    assert ("src/Repository.java", 4, "resource-leak") in keys
    assert ("src/MixedRepository.java", 8, "resource-leak") in keys
    assert ("src/Poller.vue", 2, "resource-leak") in keys
    assert ("src/importer.py", 3, "resource-leak") in keys
    assert ("src/direct_result.py", 3, "resource-leak") in keys
    assert ("src/conditional_close.py", 3, "resource-leak") in keys
    assert ("src/service.go", 4, "error-handling") in keys


def test_quality_detector_accepts_explicit_resource_ownership_and_error_exit_paths():
    findings = detect_quality_findings(
        {
            "src/SafeRepository.java": _patch(
                "import java.sql.Statement;\n"
                "class SafeRepository {\n"
                "  void first() throws Exception {\n"
                "    try (Statement stmt = db.createStatement()) {\n"
                '      stmt.executeUpdate("DELETE FROM jobs");\n'
                "    }\n"
                "  }\n"
                "  void second() throws Exception {\n"
                "    Statement stmt = db.createStatement();\n"
                '    try { stmt.executeUpdate("DELETE FROM jobs"); }\n'
                "    finally { stmt.close(); }\n"
                "  }\n"
                "  Statement transfer() throws Exception {\n"
                "    Statement stmt = db.createStatement();\n"
                "    return stmt;\n"
                "  }\n"
                "  void javaNine() throws Exception {\n"
                "    Statement stmt = db.createStatement();\n"
                '    try (stmt) { stmt.executeUpdate("DELETE FROM jobs"); }\n'
                "  }\n"
                "}"
            ),
            "src/SafePoller.vue": _patch(
                "<script setup>\n"
                "const timer = setInterval(() => refresh(), 1000)\n"
                "onUnmounted(() => clearInterval(timer))\n"
                "</script>"
            ),
            "src/safe_importer.py": _patch(
                "import sqlite3\n"
                "def load():\n"
                "    conn = sqlite3.connect('data.db')\n"
                "    try:\n"
                "        return conn.execute('SELECT 1').fetchall()\n"
                "    finally:\n"
                "        conn.close()\n"
                "def connect():\n"
                "    conn = sqlite3.connect('data.db')\n"
                "    return conn"
            ),
            "src/safe_service.go": _patch(
                "package service\n"
                "func run(db *sql.DB) error {\n"
                '  _, err := db.Exec("DELETE FROM jobs")\n'
                "  if err != nil {\n"
                '    return fmt.Errorf("delete: %w", err)\n'
                "  }\n"
                "  return nil\n"
                "}"
            ),
        }
    )

    assert findings == []


def test_quality_detector_skips_tests_fixtures_examples_and_unanchored_text():
    risky = _patch("def test():\n    try:\n        run()\n    except:\n        pass")

    assert (
        detect_quality_findings(
            {
                "tests/test_bad.py": risky,
                "fixtures/bad.py": risky,
                "examples/bad.py": risky,
                "src/test_bad.py": risky,
                "src/bad.py": "+except:\n+    pass",
            }
        )
        == []
    )


def test_quality_detector_skips_jest_and_go_testdata_paths():
    risky = _patch("pub fn parse(value: Option<u32>) { value.unwrap(); }")

    assert (
        detect_quality_findings(
            {
                "src/__tests__/parse.rs": risky,
                "pkg/testdata/parse.rs": risky,
                "src/integration-tests/parse.rs": risky,
                "src/testFixtures/parse.rs": risky,
                "src/integration_tests/parse.rs": risky,
                "src/testing/parse.rs": risky,
                "src/__fixtures__/parse.rs": risky,
            }
        )
        == []
    )


def test_go_defer_inside_returning_iteration_helper_is_scoped_safely():
    findings = detect_quality_findings(
        {
            "src/helper.go": _patch(
                "package p\n"
                "func run(items []Item) error {\n"
                "  for _, item := range items {\n"
                "    if err := func() error {\n"
                "      defer item.Close()\n"
                "      return nil\n"
                "    }(); err != nil { return err }\n"
                "  }\n"
                "  return nil\n"
                "}"
            )
        }
    )

    assert ("src/helper.go", 5, "resource-leak") not in _keys(findings)


def test_go_defer_inside_map_returning_iteration_helper_is_scoped_safely():
    findings = detect_quality_findings(
        {
            "src/helper.go": _patch(
                "package p\n"
                "func run(items []Item) {\n"
                "  for _, item := range items {\n"
                "    _ = func() map[string]error {\n"
                "      defer item.Close()\n"
                "      return nil\n"
                "    }()\n"
                "  }\n"
                "}"
            )
        }
    )

    assert ("src/helper.go", 5, "resource-leak") not in _keys(findings)


def test_go_defer_inside_multiline_and_channel_returning_helpers_is_scoped_safely():
    findings = detect_quality_findings(
        {
            "src/helper.go": _patch(
                "package p\n"
                "func run(items []Item) {\n"
                "  for _, item := range items {\n"
                "    _ = func(\n"
                "      value Item,\n"
                "    ) error {\n"
                "      defer item.Close()\n"
                "      return nil\n"
                "    }(item)\n"
                "    _ = func() <-chan Item {\n"
                "      defer item.Close()\n"
                "      return nil\n"
                "    }()\n"
                "  }\n"
                "}"
            )
        }
    )

    assert ("src/helper.go", 7, "resource-leak") not in _keys(findings)
    assert ("src/helper.go", 11, "resource-leak") not in _keys(findings)


def test_go_defer_inside_anonymous_struct_and_interface_result_helpers_is_scoped_safely():
    findings = detect_quality_findings(
        {
            "src/helper.go": _patch(
                "package p\n"
                "func run(items []Item) {\n"
                "  for _, item := range items {\n"
                "    _ = func() interface{ Error() string } {\n"
                "      defer item.Close()\n"
                "      return nil\n"
                "    }()\n"
                "    _ = func() struct{ Value int } {\n"
                "      defer item.Close()\n"
                "      return struct{ Value int }{}\n"
                "    }()\n"
                "  }\n"
                "}"
            )
        }
    )

    assert ("src/helper.go", 5, "resource-leak") not in _keys(findings)
    assert ("src/helper.go", 9, "resource-leak") not in _keys(findings)


def test_go_defer_inside_commented_anonymous_result_type_is_scoped_safely():
    findings = detect_quality_findings(
        {
            "src/helper.go": _patch(
                "package p\n"
                "func run(items []Item) {\n"
                "  for _, item := range items {\n"
                "    _ = func() struct /* result */ { Value int } {\n"
                "      defer item.Close()\n"
                "      return struct{ Value int }{}\n"
                "    }()\n"
                "  }\n"
                "}"
            )
        }
    )

    assert ("src/helper.go", 5, "resource-leak") not in _keys(findings)


def test_go_defer_after_closed_helper_is_still_reported():
    findings = detect_quality_findings(
        {
            "src/helper.go": _patch(
                "package p\n"
                "func run(items []Item) {\n"
                "  for _, item := range items {\n"
                "    func() { consume(item) }()\n"
                "    defer item.Close()\n"
                "  }\n"
                "}"
            )
        }
    )

    assert ("src/helper.go", 5, "resource-leak") in _keys(findings)


def test_truncated_new_ruby_file_does_not_prove_missing_reflection_contract():
    truncated = "@@ -0,0 +1,100 @@\n+class Proxy\n+  def method_missing(name, *args)\n+    dispatch(name, args)\n+  end"

    findings = detect_quality_findings({"src/proxy.rb": truncated})

    assert not any(finding.category == "api-contract" for finding in findings)


def test_static_valid_regex_unwrap_is_not_reported_as_runtime_panic():
    findings = detect_quality_findings(
        {"src/pattern.rs": _patch('pub fn pattern() { regex::Regex::new("^a+$").unwrap(); }')}
    )

    panic = [finding for finding in findings if finding.category == "panic-risk"]
    assert len(panic) == 1
    assert panic[0].confidence < 0.98


def test_statically_invalid_regex_unwrap_remains_a_panic_risk():
    findings = detect_quality_findings(
        {"src/pattern.rs": _patch('pub fn pattern() { regex::Regex::new("[").unwrap(); }')}
    )

    panic = [finding for finding in findings if finding.category == "panic-risk"]
    assert len(panic) == 1
    assert panic[0].confidence < 0.98


def test_rust_specific_static_regex_syntax_remains_contextual():
    findings = detect_quality_findings(
        {
            "src/pattern.rs": _patch(
                'fn a() { Regex::new(r"^(?:foo|bar)$").unwrap(); }\n'
                'fn b() { Regex::new(r"(?i)^hello$").unwrap(); }\n'
                'fn c() { Regex::new(r"^\\p{Greek}+$").unwrap(); }'
            )
        }
    )

    panic = [finding for finding in findings if finding.category == "panic-risk"]
    assert len(panic) == 3
    assert all(finding.confidence < 0.98 for finding in panic)


def test_known_some_and_static_parse_unwraps_are_not_auto_confirmable():
    findings = detect_quality_findings(
        {
            "src/value.rs": _patch(
                "fn known() {\n"
                "  let value = Some(42);\n"
                "  let answer = value.unwrap();\n"
                "}\n"
                'fn static_parse() { let value = "42".parse::<u32>().unwrap(); }'
            )
        }
    )

    panic = [finding for finding in findings if finding.category == "panic-risk"]
    assert len(panic) == 2
    assert all(finding.confidence < 0.98 for finding in panic)


def test_context_dependent_quality_shapes_do_not_cross_auto_confirm_threshold():
    findings = detect_quality_findings(
        {
            "src/doc.py": _patch('"""Example:\nexcept:\n    pass\n"""'),
            "src/Known.java": _patch('Optional<String> value = Optional.of("ok");\nString answer = value.get();'),
            "src/Panel.vue": _patch("const enabled = computed(() => {\n  return prefetchEnabled()\n})"),
            "src/ServerPanel.tsx": _patch(
                'import { exec } from "child_process";\n'
                'const note = "document is described here";\n'
                "export async function run() { return exec('/bin/date') }"
            ),
        }
    )

    assert not [finding for finding in findings if finding.file == "src/doc.py"]
    contextual = [finding for finding in findings if finding.file != "src/doc.py"]
    assert {finding.file for finding in contextual} == {
        "src/Known.java",
        "src/Panel.vue",
        "src/ServerPanel.tsx",
    }
    assert all(finding.confidence < 0.98 for finding in contextual)
