from reviewforge.core.state import Finding
from reviewforge.engine.verifier import Verifier


def _finding(
    finding_id: str,
    *,
    file: str = "app.py",
    line: int,
    category: str,
    message: str,
    confidence: float = 0.9,
    reviewer: str = "security_reviewer",
    verified_by: str = "",
) -> Finding:
    return Finding(
        id=finding_id,
        file=file,
        line=line,
        category=category,
        message=message,
        confidence=confidence,
        reviewer=reviewer,
        verified_by=verified_by,
    )


def test_exact_alias_duplicate_is_canonicalized_and_detector_wins():
    llm = _finding(
        "llm",
        line=12,
        category="malicious-dependency",
        message="A malicious dependency is installed.",
        confidence=0.99,
        reviewer="security_reviewer",
    )
    detector = _finding(
        "detector",
        line=12,
        category="supply-chain-risk",
        message="A malicious dependency is installed.",
        confidence=0.91,
        reviewer="dependency_reviewer",
        verified_by="detector",
    )

    survivors, dropped = Verifier().verify([llm, detector])

    assert [finding.id for finding in survivors] == ["detector"]
    assert survivors[0].category == "supply-chain-risk"
    assert survivors[0].verified_by == "detector"
    assert survivors[0].reviewer == "dependency_reviewer,security_reviewer"
    assert dropped == ["llm"]


def test_vocabulary_aliases_are_canonicalized_before_detector_precedence():
    cases = [
        (
            "eval-injection",
            "code-injection",
            "app.rb",
            "eval(user_input) executes attacker-controlled Ruby code.",
        ),
        (
            "version-not-pinned",
            "dependency-version-range",
            "package.json",
            "react@^18.0.0 uses a mutable dependency version.",
        ),
        (
            "deprecated-dependency",
            "dependency-deprecated",
            "requirements.txt",
            "Dependency legacy-lib==1.0.0 is deprecated.",
        ),
        (
            "empty-catch",
            "exception-handling",
            "UserController.java",
            "The empty catch block silently swallows the exception.",
        ),
        (
            "optional-unsafe-get",
            "null-safety",
            "UserController.java",
            "Optional.get is called without a presence guard.",
        ),
        (
            "unpinned-dependency",
            "dependency-version-range",
            "Cargo.toml",
            "openssl * uses a mutable dependency version.",
        ),
        (
            "unlocked-dependency",
            "dependency-version-range",
            "Gemfile",
            "rack >= 2.0 uses a mutable dependency version.",
        ),
        (
            "missing-accessible-name",
            "missing-label",
            "LoginForm.tsx",
            "The input has no accessible label.",
        ),
        (
            "deserialization",
            "insecure-deserialization",
            "loader.rb",
            "YAML.load may deserialize unsafe objects.",
        ),
        (
            "unsafe-deserialization",
            "insecure-deserialization",
            "seed.py",
            "pickle.loads may deserialize attacker-controlled objects.",
        ),
        (
            "dependency-locked",
            "dependency-version-range",
            "Cargo.toml",
            "openssl * uses a mutable dependency version.",
        ),
    ]

    for index, (alias, canonical, file_path, message) in enumerate(cases):
        llm = _finding(
            f"llm-{index}",
            file=file_path,
            line=7,
            category=alias,
            message=message,
            confidence=0.99,
        )
        detector = _finding(
            f"detector-{index}",
            file=file_path,
            line=7,
            category=canonical,
            message=message,
            confidence=0.9,
            verified_by="detector",
        )

        survivors, dropped = Verifier().verify([llm, detector])

        assert [finding.id for finding in survivors] == [detector.id]
        assert survivors[0].category == canonical
        assert survivors[0].verified_by == "detector"
        assert dropped == [llm.id]


def test_nearby_detector_and_llm_same_sink_are_merged():
    detector = _finding(
        "detector",
        file="workflow.yml",
        line=15,
        category="supply-chain-risk",
        message="Workflow executes remote script via piped shell.",
        verified_by="detector",
    )
    llm = _finding(
        "llm",
        file="workflow.yml",
        line=12,
        category="insecure-download",
        message="An external URL download is executed by the shell.",
        confidence=0.97,
    )

    survivors, dropped = Verifier().verify([llm, detector])

    assert [finding.id for finding in survivors] == ["detector"]
    assert dropped == ["llm"]


def test_insecure_download_keeps_tls_semantics_outside_matching_workflow_sink():
    tls = _finding(
        "tls",
        line=4,
        category="insecure-download",
        message="requests.get disables TLS certificate verification.",
    )

    survivors, dropped = Verifier().verify([tls])

    assert survivors[0].category == "insecure-download"
    assert dropped == []


def test_workflow_category_drift_merges_only_with_shared_specific_sink():
    title_detector = _finding(
        "title-detector",
        file=".github/workflows/review.yml",
        line=18,
        category="ci-security",
        message="Pull-request title is interpolated directly into a workflow shell command.",
        verified_by="detector",
    )
    title_llm = _finding(
        "title-llm",
        file=".github/workflows/review.yml",
        line=16,
        category="command-injection",
        message="The PR title reaches a shell command without safe data binding.",
    )
    unrelated_command = _finding(
        "other-command",
        file=".github/workflows/review.yml",
        line=17,
        category="command-injection",
        message="subprocess.run(user_input, shell=True) is dynamically constructed.",
    )

    survivors, dropped = Verifier().verify([title_detector, title_llm, unrelated_command])

    assert {finding.id for finding in survivors} == {"title-detector", "other-command"}
    assert dropped == ["title-llm"]


def test_workflow_pr_title_fingerprint_recognizes_chinese_reviewer_message():
    detector = _finding(
        "detector",
        file=".github/workflows/gauntlet-deploy.yml",
        line=19,
        category="ci-security",
        message="Pull-request title is interpolated directly into a workflow shell command.",
        verified_by="detector",
    )
    reviewer = _finding(
        "reviewer",
        file=".github/workflows/gauntlet-deploy.yml",
        line=16,
        category="command-injection",
        message="PR标题未经转义就传给 shell 命令，攻击者可注入参数。",
    )

    survivors, dropped = Verifier().verify([reviewer, detector])

    assert [finding.id for finding in survivors] == ["detector"]
    assert dropped == ["reviewer"]


def test_same_line_secret_output_category_drift_prefers_detector():
    detector = _finding(
        "detector",
        file=".github/workflows/review.yml",
        line=14,
        category="data-leak",
        message="Workflow prints a secret value to command output.",
        verified_by="detector",
    )
    llm = _finding(
        "llm",
        file=".github/workflows/review.yml",
        line=14,
        category="hardcoded-secrets",
        message="The job echoes a secret token into its log output.",
    )

    survivors, dropped = Verifier().verify([llm, detector])

    assert [finding.id for finding in survivors] == ["detector"]
    assert dropped == ["llm"]


def test_mixed_language_message_keeps_code_sink_identity():
    detector = _finding(
        "detector",
        file="account.go",
        line=16,
        category="sql-injection",
        message="SQL string is built with fmt.Sprintf.",
        verified_by="detector",
    )
    llm = _finding(
        "llm",
        file="account.go",
        line=18,
        category="sql-injection",
        message=(
            "\u4f7f\u7528 fmt.Sprintf \u6784\u5efa SQL \u67e5\u8be2\uff0c\u5b58\u5728\u6ce8\u5165\u98ce\u9669\u3002"
        ),
    )

    survivors, dropped = Verifier().verify([detector, llm])

    assert [finding.id for finding in survivors] == ["detector"]
    assert dropped == ["llm"]


def test_nearby_distinct_sinks_are_not_merged():
    detector = _finding(
        "detector",
        line=10,
        category="command-injection",
        message="os.popen(user_input) passes dynamic data to a command.",
        verified_by="detector",
    )
    independent = _finding(
        "independent",
        line=11,
        category="command-injection",
        message="subprocess.run(user_value, shell=True) executes another command.",
    )

    survivors, dropped = Verifier().verify([detector, independent])

    assert {finding.id for finding in survivors} == {"detector", "independent"}
    assert dropped == []


def test_nearby_detector_hits_remain_distinct_even_for_same_api():
    first = _finding(
        "first",
        line=10,
        category="command-injection",
        message="os.popen(first_input) passes dynamic data to a command.",
        verified_by="detector",
    )
    second = _finding(
        "second",
        line=11,
        category="command-injection",
        message="os.popen(second_input) passes dynamic data to a command.",
        verified_by="detector",
    )

    survivors, dropped = Verifier().verify([first, second])

    assert {finding.id for finding in survivors} == {"first", "second"}
    assert dropped == []


def test_nearby_dependency_manifest_entries_are_never_fuzzy_merged():
    detector = _finding(
        "detector",
        file="requirements.txt",
        line=4,
        category="dependency-vulnerability",
        message="jinja2 2.10 matches an advisory.",
        verified_by="detector",
    )
    adjacent = _finding(
        "adjacent",
        file="requirements.txt",
        line=5,
        category="dependency-vulnerability",
        message="jinja2-plugin 1.0 matches CVE-2025-12345, a separate advisory.",
    )

    survivors, dropped = Verifier().verify([detector, adjacent])

    assert {finding.id for finding in survivors} == {"detector", "adjacent"}
    assert dropped == []


def test_nearby_same_function_offsets_merge_targeted_security_categories():
    cases = [
        (
            "sql-injection",
            "SQL string is built with f-string interpolation.",
            "The f-string query reaches cursor.execute() in `load_account`.",
        ),
        (
            "path-traversal",
            "Potential path join from user-driven fragments.",
            "os.path.join builds the untrusted path read by open() in `load_account`.",
        ),
        (
            "insecure-deserialization",
            "Unsafe deserialization API used.",
            "pickle.loads() consumes attacker data inside `load_account`.",
        ),
        (
            "command-injection",
            "Dynamic data is passed to a shell command API.",
            "subprocess.run() executes the attacker-controlled command in `load_account`.",
        ),
    ]

    for index, (category, detector_message, llm_message) in enumerate(cases):
        detector = _finding(
            f"detector-{index}",
            line=20,
            category=category,
            message=detector_message,
            verified_by="detector-auto",
        )
        llm = _finding(
            f"llm-{index}",
            line=18,
            category=category,
            message=llm_message,
            confidence=0.99,
        )

        survivors, dropped = Verifier().verify([llm, detector])

        assert [finding.id for finding in survivors] == [detector.id]
        assert dropped == [llm.id]


def test_fuzzy_matching_consumes_each_detector_at_most_once():
    detector = _finding(
        "detector",
        line=10,
        category="command-injection",
        message="os.system(user_input) executes a dynamic command.",
        verified_by="detector",
    )
    closest = _finding(
        "closest",
        line=11,
        category="command-injection",
        message="os.system(user_input) executes unsanitized input.",
    )
    second = _finding(
        "second",
        line=12,
        category="command-injection",
        message="os.system(other_input) is another command execution.",
    )

    survivors, dropped = Verifier().verify([second, closest, detector])

    assert {finding.id for finding in survivors} == {"detector", "second"}
    assert dropped == ["closest"]


def test_exact_match_consumes_detector_before_fuzzy_matching():
    detector = _finding(
        "detector",
        line=10,
        category="command-injection",
        message="os.system(user_input) executes a dynamic command.",
        verified_by="detector",
    )
    exact_duplicate = _finding(
        "exact-duplicate",
        line=10,
        category="command-injection",
        message="os.system(user_input) executes unsanitized input.",
    )
    independent = _finding(
        "independent",
        line=11,
        category="command-injection",
        message="os.system(other_input) executes a separate command.",
    )

    survivors, dropped = Verifier().verify([detector, exact_duplicate, independent])

    assert {finding.id for finding in survivors} == {"detector", "independent"}
    assert dropped == ["exact-duplicate"]


def test_manifest_offset_matches_same_package_and_detector_wins():
    detector = _finding(
        "detector",
        file="requirements.txt",
        line=8,
        category="dependency-vulnerability",
        message="django==1.2 matches a deterministic advisory.",
        reviewer="dependency_reviewer",
        verified_by="detector-auto",
    )
    llm = _finding(
        "llm",
        file="requirements.txt",
        line=6,
        category="security",
        message="Dependency django 1.2 is insecure and should be upgraded.",
    )

    survivors, dropped = Verifier().verify([llm, detector])

    assert [finding.id for finding in survivors] == ["detector"]
    assert survivors[0].reviewer == "dependency_reviewer,security_reviewer"
    assert dropped == ["llm"]


def test_manifest_package_matching_handles_supply_npm_maven_and_go_coordinates():
    cases = [
        (
            "package.json",
            "supply-chain-risk",
            "event-stream has recorded package-compromise history.",
            "Dependency event-stream@3.3.6 has a supply-chain compromise risk.",
        ),
        (
            "package.json",
            "dependency-vulnerability",
            "@scope/widget 1.2.0 matches GHSA-abcd-efgh-ijkl.",
            "Package @scope/widget@1.2.0 is vulnerable.",
        ),
        (
            "pom.xml",
            "dependency-vulnerability",
            "org.yaml:snakeyaml 1.26 matches CVE-2022-1471.",
            "Artifact org.yaml:snakeyaml:1.26 has an unsafe version.",
        ),
        (
            "go.mod",
            "dependency-vulnerability",
            "github.com/dgrijalva/jwt-go v3.2.0 matches GO-2020-0017.",
            "Module github.com/dgrijalva/jwt-go v3.2.0 is vulnerable.",
        ),
    ]

    for index, (file, category, detector_message, llm_message) in enumerate(cases):
        detector = _finding(
            f"detector-{index}",
            file=file,
            line=10,
            category=category,
            message=detector_message,
            verified_by="detector-auto",
        )
        llm = _finding(
            f"llm-{index}",
            file=file,
            line=8,
            category=category,
            message=llm_message,
        )

        survivors, dropped = Verifier().verify([llm, detector])

        assert [finding.id for finding in survivors] == [detector.id]
        assert dropped == [llm.id]


def test_manifest_different_package_is_not_absorbed_by_nearby_detector():
    detector = _finding(
        "detector",
        file="package.json",
        line=10,
        category="dependency-vulnerability",
        message="lodash@4.17.20 matches a deterministic advisory.",
        verified_by="detector",
    )
    independent = _finding(
        "independent",
        file="package.json",
        line=11,
        category="dependency-vulnerability",
        message="Dependency minimist@0.0.8 is affected by CVE-2021-44906.",
    )

    survivors, dropped = Verifier().verify([detector, independent])

    assert {finding.id for finding in survivors} == {"detector", "independent"}
    assert dropped == []


def test_manifest_version_range_detector_does_not_absorb_same_package_cve():
    detector = _finding(
        "range-detector",
        file="package.json",
        line=10,
        category="dependency-version-range",
        message="lodash@^4.17.0 uses a mutable version range.",
        verified_by="detector",
    )
    vulnerability = _finding(
        "cve",
        file="package.json",
        line=9,
        category="dependency-vulnerability",
        message="Dependency lodash@4.17.20 is affected by CVE-2021-23337.",
    )

    survivors, dropped = Verifier().verify([detector, vulnerability])

    assert {finding.id for finding in survivors} == {"range-detector", "cve"}
    assert dropped == []


def test_manifest_different_advisory_ids_for_same_package_remain_independent():
    detector = _finding(
        "cve-one",
        file="requirements.txt",
        line=8,
        category="dependency-vulnerability",
        message="urllib3==1.25.2 is affected by CVE-1.",
        verified_by="detector-auto",
    )
    second_advisory = _finding(
        "cve-two",
        file="requirements.txt",
        line=8,
        category="dependency-vulnerability",
        message="Dependency urllib3==1.25.2 is affected by CVE-2.",
    )
    second_duplicate = _finding(
        "cve-two-copy",
        file="requirements.txt",
        line=8,
        category="dependency-vulnerability",
        message="Dependency urllib3==1.25.2 is affected by CVE-2.",
        confidence=0.8,
    )

    survivors, dropped = Verifier().verify([detector, second_advisory, second_duplicate])

    assert {finding.id for finding in survivors} == {"cve-one", "cve-two"}
    assert dropped == ["cve-two-copy"]


def test_manifest_same_advisory_id_for_same_package_is_merged():
    detector = _finding(
        "detector",
        file="requirements.txt",
        line=8,
        category="dependency-vulnerability",
        message="urllib3==1.25.2 is affected by CVE-2021-33503.",
        verified_by="detector-auto",
    )
    duplicate = _finding(
        "duplicate",
        file="requirements.txt",
        line=6,
        category="security",
        message="Dependency urllib3==1.25.2 has CVE-2021-33503 vulnerability.",
    )

    survivors, dropped = Verifier().verify([duplicate, detector])

    assert [finding.id for finding in survivors] == ["detector"]
    assert dropped == ["duplicate"]


def test_manifest_equivalent_cve_and_ghsa_ids_are_merged():
    cases = [
        ("package.json", "serialize-javascript 1.7.0", "CVE-2019-16769", "GHSA-h9rv-jmmf-4pgx"),
        ("package.json", "serialize-javascript 1.7.0", "CVE-2020-7660", "GHSA-hxcc-f52p-wc94"),
        ("pom.xml", "log4j-core 2.14.1", "CVE-2021-44228", "GHSA-jfh8-c2jp-5v3q"),
    ]

    for index, (file_path, coordinate, cve_id, ghsa_id) in enumerate(cases):
        detector = _finding(
            f"detector-{index}",
            file=file_path,
            line=10,
            category="dependency-vulnerability",
            message=f"{coordinate} matches {ghsa_id}.",
            verified_by="detector-auto",
        )
        duplicate = _finding(
            f"duplicate-{index}",
            file=file_path,
            line=8,
            category="dependency-vulnerability",
            message=f"Dependency {coordinate} is affected by {cve_id}.",
        )

        survivors, dropped = Verifier().verify([duplicate, detector])

        assert [finding.id for finding in survivors] == [detector.id]
        assert dropped == [duplicate.id]


def test_manifest_equivalent_cve_is_absorbed_by_composite_ghsa_detector():
    detector = _finding(
        "detector",
        file="package.json",
        line=10,
        category="dependency-vulnerability",
        message=("serialize-javascript 1.7.0 matches GHSA-h9rv-jmmf-4pgx and GHSA-hxcc-f52p-wc94."),
        verified_by="detector-auto",
    )
    duplicate = _finding(
        "duplicate",
        file="package.json",
        line=10,
        category="dependency-vulnerability",
        message="serialize-javascript 1.7.0 is affected by CVE-2020-7660.",
    )

    survivors, dropped = Verifier().verify([duplicate, detector])

    assert [finding.id for finding in survivors] == ["detector"]
    assert dropped == ["duplicate"]


def test_manifest_equivalence_does_not_merge_distinct_known_advisories():
    detector = _finding(
        "detector",
        file="package.json",
        line=10,
        category="dependency-vulnerability",
        message="serialize-javascript 1.7.0 matches GHSA-h9rv-jmmf-4pgx.",
        verified_by="detector-auto",
    )
    independent = _finding(
        "independent",
        file="package.json",
        line=10,
        category="dependency-vulnerability",
        message="serialize-javascript 1.7.0 is affected by CVE-2020-7660.",
    )

    survivors, dropped = Verifier().verify([detector, independent])

    assert {finding.id for finding in survivors} == {"detector", "independent"}
    assert dropped == []


def test_manifest_generic_vulnerability_is_not_absorbed_without_shared_version_evidence():
    detector = _finding(
        "detector",
        file="requirements.txt",
        line=10,
        category="dependency-vulnerability",
        message="urllib3==1.25.2 is affected by CVE-1.",
        verified_by="detector-auto",
    )
    ambiguous = _finding(
        "ambiguous",
        file="requirements.txt",
        line=8,
        category="dependency-vulnerability",
        message="Dependency urllib3 may have another vulnerability.",
    )

    survivors, dropped = Verifier().verify([ambiguous, detector])

    assert {finding.id for finding in survivors} == {"detector", "ambiguous"}
    assert dropped == []


def test_manifest_generic_vulnerability_merges_when_exact_version_coordinate_matches():
    detector = _finding(
        "detector",
        file="requirements.txt",
        line=10,
        category="dependency-vulnerability",
        message="urllib3==1.25.2 is affected by CVE-1.",
        verified_by="detector-auto",
    )
    duplicate = _finding(
        "duplicate",
        file="requirements.txt",
        line=8,
        category="dependency-vulnerability",
        message="Dependency urllib3 1.25.2 has a known vulnerability.",
    )

    survivors, dropped = Verifier().verify([duplicate, detector])

    assert [finding.id for finding in survivors] == ["detector"]
    assert dropped == ["duplicate"]


def test_composite_detector_absorbs_overlapping_cve_once_but_preserves_independent_cve():
    detector = _finding(
        "detector",
        file="requirements.txt",
        line=10,
        category="dependency-vulnerability",
        message="urllib3==1.25.2 is affected by CVE-1 and CVE-2.",
        verified_by="detector-auto",
    )
    cve_one_duplicate = _finding(
        "cve-one-duplicate",
        file="requirements.txt",
        line=10,
        category="dependency-vulnerability",
        message="Dependency urllib3==1.25.2 is affected by CVE-1.",
    )
    independent_cve_two = _finding(
        "independent-cve-two",
        file="requirements.txt",
        line=10,
        category="dependency-vulnerability",
        message="Dependency urllib3==1.25.2 is affected by CVE-2.",
    )

    survivors, dropped = Verifier().verify([independent_cve_two, cve_one_duplicate, detector])

    assert {finding.id for finding in survivors} == {
        "detector",
        "independent-cve-two",
    }
    assert dropped == ["cve-one-duplicate"]


def test_exact_manifest_match_consumes_detector_before_advisory_fuzzy_matching():
    detector = _finding(
        "detector",
        file="requirements.txt",
        line=10,
        category="dependency-vulnerability",
        message="urllib3==1.25.2 is affected by CVE-1 and CVE-2.",
        verified_by="detector-auto",
    )
    exact_duplicate = _finding(
        "exact-duplicate",
        file="requirements.txt",
        line=10,
        category="dependency-vulnerability",
        message="Dependency urllib3==1.25.2 is affected by CVE-1 and CVE-2.",
    )
    independent_advisory = _finding(
        "independent-cve-two",
        file="requirements.txt",
        line=11,
        category="dependency-vulnerability",
        message="Dependency urllib3==1.25.2 is affected by CVE-2.",
    )

    survivors, dropped = Verifier().verify([detector, exact_duplicate, independent_advisory])

    assert {finding.id for finding in survivors} == {"detector", "independent-cve-two"}
    assert dropped == ["exact-duplicate"]


def test_manifest_detector_absorbs_repeated_generic_same_version_restatement():
    detector = _finding(
        "detector",
        file="package.json",
        line=10,
        category="dependency-vulnerability",
        message="serialize-javascript 1.7.0 matches GHSA-h9rv-jmmf-4pgx.",
        verified_by="detector-auto",
    )
    exact_duplicate = _finding(
        "exact-duplicate",
        file="package.json",
        line=10,
        category="dependency-vulnerability",
        message="serialize-javascript 1.7.0 matches GHSA-h9rv-jmmf-4pgx.",
    )
    offset_restatement = _finding(
        "offset-restatement",
        file="package.json",
        line=8,
        category="vulnerability",
        message="serialize-javascript 1.7.0 has a known vulnerability.",
    )

    survivors, dropped = Verifier().verify([detector, exact_duplicate, offset_restatement])

    assert [finding.id for finding in survivors] == ["detector"]
    assert dropped == ["exact-duplicate", "offset-restatement"]


def test_manifest_detector_does_not_absorb_distinct_same_version_mechanism():
    detector = _finding(
        "detector",
        file="package.json",
        line=10,
        category="dependency-vulnerability",
        message="widget 1.2.3 matches GHSA-aaaa-bbbb-cccc for prototype pollution.",
        verified_by="detector-auto",
    )
    exact_duplicate = _finding(
        "exact-duplicate",
        file="package.json",
        line=10,
        category="dependency-vulnerability",
        message="widget 1.2.3 matches GHSA-aaaa-bbbb-cccc for prototype pollution.",
    )
    independent = _finding(
        "independent",
        file="package.json",
        line=8,
        category="dependency-vulnerability",
        message="widget 1.2.3 allows remote code execution through unsafe deserialization.",
    )

    survivors, dropped = Verifier().verify([detector, exact_duplicate, independent])

    assert [finding.id for finding in survivors] == ["detector", "independent"]
    assert dropped == ["exact-duplicate"]


def test_manifest_unmaintained_alias_requires_lifecycle_evidence():
    unsupported = _finding(
        "unsupported",
        file="go.mod",
        line=7,
        category="unmaintained-dependency",
        message="jwt-go v3.2.0 is unmaintained.",
    )

    survivors, dropped = Verifier().verify([unsupported])

    assert survivors == []
    assert dropped == ["unsupported"]


def test_unsupported_manifest_dependency_guess_is_filtered_but_advisory_evidence_survives():
    guess = _finding(
        "guess",
        file="requirements.txt",
        line=3,
        category="dependency-vulnerability",
        message="requests may be outdated and could have security issues.",
    )
    evidenced = _finding(
        "evidenced",
        file="requirements.txt",
        line=4,
        category="dependency-vulnerability",
        message="urllib3==1.25.2 is affected by PYSEC-2021-108.",
    )

    survivors, dropped = Verifier().verify([guess, evidenced])

    assert [finding.id for finding in survivors] == ["evidenced"]
    assert dropped == ["guess"]


def test_version_range_alias_merges_with_manifest_detector_by_package_coordinate():
    detector = _finding(
        "detector",
        file="package.json",
        line=7,
        category="dependency-version-range",
        message="react@^18.0.0 uses an open version range.",
        verified_by="detector",
    )
    llm = _finding(
        "llm",
        file="package.json",
        line=5,
        category="version-range",
        message="Dependency react@^18.0.0 is not pinned exactly.",
    )

    survivors, dropped = Verifier().verify([llm, detector])

    assert [finding.id for finding in survivors] == ["detector"]
    assert dropped == ["llm"]


def test_manifest_security_guess_alias_is_filtered_but_concrete_script_sink_survives():
    secret_guess = _finding(
        "secret-guess",
        file="package.json",
        line=3,
        category="secret-leakage",
        message="Dependency metadata might expose a secret value.",
    )
    concrete_command = _finding(
        "command",
        file="package.json",
        line=8,
        category="command-injection",
        message="The postinstall hook calls child_process.exec(user_script).",
    )

    survivors, dropped = Verifier().verify([secret_guess, concrete_command])

    assert [finding.id for finding in survivors] == ["command"]
    assert survivors[0].category == "command-injection"
    assert dropped == ["secret-guess"]


def test_detector_does_not_absorb_same_sink_outside_line_tolerance():
    detector = _finding(
        "detector",
        line=10,
        category="xss",
        message="Vue v-html binding can inject HTML.",
        verified_by="detector",
    )
    distant = _finding(
        "distant",
        line=14,
        category="unsafe-html",
        message="v-html renders untrusted content.",
    )

    survivors, dropped = Verifier().verify([detector, distant])

    assert {finding.id for finding in survivors} == {"detector", "distant"}
    assert dropped == []


def test_nonduplicate_categories_are_still_canonicalized():
    finding = _finding(
        "alt",
        file="avatar.vue",
        line=8,
        category="missing-alt-text",
        message="Image is missing alt text.",
        reviewer="accessibility_reviewer",
    )

    survivors, dropped = Verifier().verify([finding])

    assert survivors[0].category == "missing-alt"
    assert dropped == []


def test_verifier_merges_repeated_metric_recorder_swaps_but_keeps_opposite_direction():
    verifier = Verifier()
    legacy_create = Finding(
        id="legacy_create",
        file="writer.go",
        line=45,
        category="metrics-recorder-mismatch",
        message="Storage failure calls recordLegacyDuration instead of recordStorageDuration.",
        suggestion="Call recordStorageDuration.",
        confidence=0.9,
        reviewer="correctness_reviewer",
    )
    legacy_update = Finding(
        id="legacy_update",
        file="writer.go",
        line=128,
        category="wrong-metric-recorder",
        message="Storage update calls recordLegacyDuration; use recordStorageDuration.",
        suggestion="Replace it with recordStorageDuration.",
        confidence=0.8,
        reviewer="performance_reviewer",
    )
    storage_delete = Finding(
        id="storage_delete",
        file="writer.go",
        line=163,
        category="metrics-recorder-mismatch",
        message="Legacy deletion calls recordStorageDuration instead of recordLegacyDuration.",
        suggestion="Call recordLegacyDuration.",
        confidence=0.85,
        reviewer="correctness_reviewer",
    )

    survivors, dropped = verifier.verify([legacy_create, legacy_update, storage_delete])

    assert [finding.id for finding in survivors] == ["legacy_create", "storage_delete"]
    assert dropped == ["legacy_update"]
