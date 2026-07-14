from reviewforge.engine.detectors import detect_dependency_findings


def _diff(content: str) -> str:
    lines = content.splitlines()
    return f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join("+" + line for line in lines)


def _advisory_findings(diffs: dict[str, str]):
    return [
        finding
        for finding in detect_dependency_findings(diffs)
        if finding.category in {"dependency-vulnerability", "dependency-deprecated"}
    ]


def test_pr72_dependency_fixture_has_all_nine_advisory_findings_at_right_lines():
    findings = _advisory_findings(
        {
            "gauntlet_deps/go.mod": _diff(
                "module example.invalid/fixture\n"
                "\n"
                "go 1.22\n"
                "\n"
                "require (\n"
                "  github.com/dgrijalva/jwt-go v3.2.0+incompatible\n"
                "  gopkg.in/yaml.v2 v2.2.1\n"
                "  github.com/gin-gonic/gin v1.3.0\n"
                ")"
            ),
            "gauntlet_deps/package.json": _diff(
                "{\n"
                '  "name": "fixture",\n'
                '  "version": "1.0.0",\n'
                '  "scripts": {},\n'
                '  "dependencies": {\n'
                '    "react": "18.3.1",\n'
                '    "lodash": "4.17.21",\n'
                '    "axios": "1.7.9",\n'
                '    "express": "4.21.2",\n'
                '    "serialize-javascript": "1.7.0"\n'
                "  }\n"
                "}"
            ),
            "gauntlet_deps/pom.xml": _diff(
                "<project>\n"
                "  <modelVersion>4.0.0</modelVersion>\n"
                "  <dependencies>\n"
                "\n"
                "\n"
                "    <dependency>\n"
                "      <groupId>org.apache.logging.log4j</groupId>\n"
                "      <artifactId>log4j-core</artifactId>\n"
                "      <scope>runtime</scope>\n"
                "      <version>2.14.1</version>\n"
                "    </dependency>\n"
                "  </dependencies>\n"
                "</project>"
            ),
            "gauntlet_deps/requirements.txt": _diff("django==1.2\nrequests==2.32.4\nmarkupsafe==3.0.2\njinja2==2.10"),
            "gauntlet_deps/Cargo.toml": _diff(
                '[package]\nname = "fixture"\nversion = "1.0.0"\n\n[dependencies]\nserde = "1.0"\n\nserde_yaml = "0.8"'
            ),
            "gauntlet_deps/Gemfile": _diff('source "https://rubygems.org"\nruby "3.3.0"\ngem "rails", "4.2.0"'),
        }
    )

    assert {(finding.file, finding.line, finding.category) for finding in findings} == {
        ("gauntlet_deps/go.mod", 6, "dependency-vulnerability"),
        ("gauntlet_deps/go.mod", 7, "dependency-vulnerability"),
        ("gauntlet_deps/go.mod", 8, "dependency-vulnerability"),
        ("gauntlet_deps/package.json", 10, "dependency-vulnerability"),
        ("gauntlet_deps/pom.xml", 10, "dependency-vulnerability"),
        ("gauntlet_deps/requirements.txt", 1, "dependency-vulnerability"),
        ("gauntlet_deps/requirements.txt", 4, "dependency-vulnerability"),
        ("gauntlet_deps/Cargo.toml", 8, "dependency-deprecated"),
        ("gauntlet_deps/Gemfile", 3, "dependency-deprecated"),
    }
    assert len(findings) == 9
    assert all(finding.confidence >= 0.97 for finding in findings)


def test_multiple_advisories_on_one_coordinate_are_aggregated_with_snapshot_metadata():
    findings = _advisory_findings({"go.mod": _diff("require gopkg.in/yaml.v2 v2.2.1")})

    assert len(findings) == 1
    assert findings[0].line == 1
    assert "GHSA-r88r-gmrh-7j83" in findings[0].message
    assert "GHSA-6q6q-88xp-6f2r" in findings[0].message
    assert "GHSA-wxc4-f4m6-wwqv" in findings[0].message
    assert "2026-07-14" in findings[0].message


def test_modern_versions_and_maintained_replacements_are_clean():
    findings = _advisory_findings(
        {
            "go.mod": _diff(
                "require (\n"
                "  github.com/golang-jwt/jwt/v5 v5.2.2\n"
                "  gopkg.in/yaml.v2 v2.4.0\n"
                "  github.com/gin-gonic/gin v1.9.1\n"
                ")"
            ),
            "package.json": _diff('{"dependencies":{"serialize-javascript":"7.0.3"}}'),
            "pom.xml": _diff(
                "<dependency><groupId>org.apache.logging.log4j</groupId>"
                "<artifactId>log4j-core</artifactId><version>2.25.4</version></dependency>"
            ),
            "requirements.txt": _diff("django==5.2.4\njinja2==3.1.6"),
            "Cargo.toml": _diff('[dependencies]\nserde_saphyr = "0.0.17"'),
            "Gemfile": _diff('gem "rails", "8.1.0"'),
        }
    )

    assert findings == []


def test_mutable_ranges_are_never_reported_as_known_vulnerabilities():
    findings = detect_dependency_findings(
        {
            "requirements.txt": _diff("django>=1.2\njinja2~=2.10"),
            "package.json": _diff('{"dependencies":{"serialize-javascript":"^1.7.0"}}'),
        }
    )

    assert "dependency-vulnerability" not in {finding.category for finding in findings}
    assert "dependency-version-range" in {finding.category for finding in findings}


def test_non_manifest_pr71_files_and_deletion_only_changes_are_clean():
    pr71_like = {
        "service_config.yaml": _diff("enabled: true"),
        "src/service.py": _diff("def run():\n    return True"),
        "README.md": _diff("safe documentation"),
    }
    deletion = "@@ -1,1 +1,0 @@\n-django==1.2"

    assert detect_dependency_findings(pr71_like) == []
    assert _advisory_findings({"requirements.txt": deletion}) == []


def test_advisory_anchor_uses_right_side_line_across_context_and_hunks():
    patch = (
        "@@ -40,2 +80,3 @@\n"
        " requests==2.32.4\n"
        "+django==1.2\n"
        " keep-context\n"
        "@@ -100,1 +200,2 @@\n"
        " context\n"
        "+jinja2==2.10\n"
    )

    findings = _advisory_findings({"requirements.txt": patch})

    assert {(finding.line, finding.category) for finding in findings} == {
        (81, "dependency-vulnerability"),
        (201, "dependency-vulnerability"),
    }


def test_maven_partial_hunk_uses_dependency_context_and_added_version_line():
    patch = (
        "@@ -20,5 +20,5 @@\n"
        "     <dependency>\n"
        "       <groupId>org.apache.logging.log4j</groupId>\n"
        "       <artifactId>log4j-core</artifactId>\n"
        "-      <version>2.25.4</version>\n"
        "+      <version>2.14.1</version>\n"
        "     </dependency>\n"
    )

    findings = _advisory_findings({"pom.xml": patch})

    assert [(finding.line, finding.category) for finding in findings] == [(23, "dependency-vulnerability")]


def test_package_json_does_not_scan_scripts_or_top_level_metadata():
    findings = _advisory_findings(
        {
            "package.json": _diff(
                '{"name":"serialize-javascript","version":"1.7.0",'
                '"scripts":{"test":"serialize-javascript 1.7.0"},'
                '"dependencies":{"safe-package":"1.7.0"}}'
            )
        }
    )

    assert findings == []
