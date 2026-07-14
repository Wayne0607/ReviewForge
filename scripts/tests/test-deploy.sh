#!/bin/bash
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEPLOY_SCRIPT="$REPO_ROOT/scripts/deploy.sh"
TEST_ROOT="$(mktemp -d)"
APP_DIR="$TEST_ROOT/app"
MOCK_BIN="$TEST_ROOT/bin"
COMMAND_LOG="$TEST_ROOT/commands.log"

cleanup() {
    rm -rf -- "$TEST_ROOT"
}
trap cleanup EXIT

fail() {
    printf 'FAIL: %s\n' "$*" >&2
    exit 1
}

assert_eq() {
    local expected="$1"
    local actual="$2"
    local message="$3"
    [[ "$expected" == "$actual" ]] || fail "$message (expected=$expected actual=$actual)"
}

mkdir -p "$APP_DIR/frontend" "$APP_DIR/backend" "$MOCK_BIN"
git -C "$APP_DIR" init -q
git -C "$APP_DIR" config user.name "Deploy Test"
git -C "$APP_DIR" config user.email "deploy-test@example.invalid"

printf 'old\n' > "$APP_DIR/VERSION"
printf '{}\n' > "$APP_DIR/frontend/package.json"
printf '{}\n' > "$APP_DIR/frontend/package-lock.json"
printf '[project]\nname = "deploy-test"\nversion = "0.0.0"\n' > "$APP_DIR/backend/pyproject.toml"
git -C "$APP_DIR" add .
git -C "$APP_DIR" commit -qm "old release"
PREVIOUS_SHA="$(git -C "$APP_DIR" rev-parse HEAD)"

printf 'broken-health\n' > "$APP_DIR/VERSION"
touch "$APP_DIR/backend/FAIL_HEALTH"
git -C "$APP_DIR" add .
git -C "$APP_DIR" commit -qm "release with failing health check"
BROKEN_SHA="$(git -C "$APP_DIR" rev-parse HEAD)"

git -C "$APP_DIR" rm -q backend/FAIL_HEALTH
printf 'healthy\n' > "$APP_DIR/VERSION"
git -C "$APP_DIR" add .
git -C "$APP_DIR" commit -qm "healthy release"
HEALTHY_SHA="$(git -C "$APP_DIR" rev-parse HEAD)"
git -C "$APP_DIR" reset -q --hard "$PREVIOUS_SHA"

cat > "$MOCK_BIN/node" <<'EOF'
#!/bin/bash
exit 0
EOF

cat > "$MOCK_BIN/npm" <<'EOF'
#!/bin/bash
set -euo pipefail
printf 'npm %s\n' "$*" >> "$TEST_COMMAND_LOG"
if [[ "${1:-}" == "run" && "${2:-}" == "build" ]]; then
    mkdir -p "$TEST_APP_DIR/backend/src/reviewforge/static"
    cp "$TEST_APP_DIR/VERSION" "$TEST_APP_DIR/backend/src/reviewforge/static/version.txt"
fi
EOF

cat > "$MOCK_BIN/uv" <<'EOF'
#!/bin/bash
set -euo pipefail
printf 'uv %s\n' "$*" >> "$TEST_COMMAND_LOG"
exit 0
EOF

cat > "$MOCK_BIN/systemctl" <<'EOF'
#!/bin/bash
set -euo pipefail
printf 'systemctl %s\n' "$*" >> "$TEST_COMMAND_LOG"
exit 0
EOF

cat > "$MOCK_BIN/sudo" <<'EOF'
#!/bin/bash
set -euo pipefail
if [[ "${1:-}" == "-n" ]]; then
    shift
fi
exec "$@"
EOF

cat > "$MOCK_BIN/curl" <<'EOF'
#!/bin/bash
set -euo pipefail
printf 'curl %s\n' "$*" >> "$TEST_COMMAND_LOG"
[[ ! -f "$TEST_APP_DIR/backend/FAIL_HEALTH" ]]
EOF

cat > "$MOCK_BIN/journalctl" <<'EOF'
#!/bin/bash
set -euo pipefail
printf 'journalctl %s\n' "$*" >> "$TEST_COMMAND_LOG"
printf 'bounded mock journal output\n' >&2
EOF

chmod +x "$MOCK_BIN"/*
export PATH="$MOCK_BIN:$PATH"
export TEST_APP_DIR="$APP_DIR"
export TEST_COMMAND_LOG="$COMMAND_LOG"
hash -r
assert_eq "$MOCK_BIN/systemctl" "$(command -v systemctl)" "mock systemctl is not first on PATH"

run_deploy() {
    local target_sha="$1"
    local bundle_path="$2"
    REVIEWFORGE_APP_DIR="$APP_DIR" \
      REVIEWFORGE_DEPLOY_LOCK="$TEST_ROOT/deploy.lock" \
      REVIEWFORGE_UV_BIN="$MOCK_BIN/uv" \
      REVIEWFORGE_TARGET_SHA="$target_sha" \
      REVIEWFORGE_BUNDLE_PATH="$bundle_path" \
      REVIEWFORGE_HEALTH_ATTEMPTS=1 \
      REVIEWFORGE_HEALTH_INTERVAL_SECONDS=1 \
      bash "$DEPLOY_SCRIPT"
}

run_retry_deploy() {
    local target_sha="$1"
    local previous_sha="$2"
    local bundle_path="$3"
    REVIEWFORGE_APP_DIR="$APP_DIR" \
      REVIEWFORGE_DEPLOY_LOCK="$TEST_ROOT/deploy.lock" \
      REVIEWFORGE_UV_BIN="$MOCK_BIN/uv" \
      REVIEWFORGE_TARGET_SHA="$target_sha" \
      REVIEWFORGE_PREVIOUS_SHA="$previous_sha" \
      REVIEWFORGE_BUNDLE_PATH="$bundle_path" \
      REVIEWFORGE_HEALTH_ATTEMPTS=1 \
      REVIEWFORGE_HEALTH_INTERVAL_SECONDS=1 \
      bash "$DEPLOY_SCRIPT"
}

run_legacy_deploy() {
    local bundle_path="$1"
    REVIEWFORGE_APP_DIR="$APP_DIR" \
      REVIEWFORGE_DEPLOY_LOCK="$TEST_ROOT/deploy.lock" \
      REVIEWFORGE_UV_BIN="$MOCK_BIN/uv" \
      REVIEWFORGE_BUNDLE_PATH="$bundle_path" \
      REVIEWFORGE_HEALTH_ATTEMPTS=1 \
      REVIEWFORGE_HEALTH_INTERVAL_SECONDS=1 \
      bash "$DEPLOY_SCRIPT"
}

# A failed health check must restore, rebuild, restart, and verify the old SHA.
BROKEN_BUNDLE="$TEST_ROOT/broken.bundle"
touch "$BROKEN_BUNDLE"
set +e
run_deploy "$BROKEN_SHA" "$BROKEN_BUNDLE" > "$TEST_ROOT/broken.out" 2>&1
broken_status=$?
set -e
[[ $broken_status -ne 0 ]] || fail "broken deployment unexpectedly succeeded"
assert_eq "$PREVIOUS_SHA" "$(git -C "$APP_DIR" rev-parse HEAD)" "rollback did not restore the previous SHA"
assert_eq "old" "$(tr -d '\r\n' < "$APP_DIR/backend/src/reviewforge/static/version.txt")" \
  "rollback did not rebuild the previous frontend"
[[ ! -e "$BROKEN_BUNDLE" ]] || fail "failed deployment bundle was not removed"
if ! grep -q "Rollback completed; the previous release is healthy" "$TEST_ROOT/broken.out"; then
    sed -n '1,240p' "$TEST_ROOT/broken.out" >&2
    fail "successful rollback was not reported"
fi
assert_eq "2" "$(grep -c '^systemctl restart reviewforge$' "$COMMAND_LOG")" \
  "failed release and rollback should each restart the service"

# A healthy target should become HEAD and remove its uploaded bundle.
: > "$COMMAND_LOG"
HEALTHY_BUNDLE="$TEST_ROOT/healthy.bundle"
touch "$HEALTHY_BUNDLE"
run_deploy "$HEALTHY_SHA" "$HEALTHY_BUNDLE" > "$TEST_ROOT/healthy.out" 2>&1
assert_eq "$HEALTHY_SHA" "$(git -C "$APP_DIR" rev-parse HEAD)" "healthy release was not deployed"
[[ ! -e "$HEALTHY_BUNDLE" ]] || fail "successful deployment bundle was not removed"
grep -q "Deployment completed successfully" "$TEST_ROOT/healthy.out" || \
  fail "successful deployment was not reported"

# If an earlier attempt switched HEAD but died before producing a healthy
# service, retrying the same target must still roll back to the explicit
# previous release when the health check fails.
git -C "$APP_DIR" reset -q --hard "$BROKEN_SHA"
RETRY_BUNDLE="$TEST_ROOT/retry.bundle"
touch "$RETRY_BUNDLE"
set +e
run_retry_deploy "$BROKEN_SHA" "$PREVIOUS_SHA" "$RETRY_BUNDLE" > "$TEST_ROOT/retry.out" 2>&1
retry_status=$?
set -e
[[ $retry_status -ne 0 ]] || fail "interrupted-release retry unexpectedly succeeded"
assert_eq "$PREVIOUS_SHA" "$(git -C "$APP_DIR" rev-parse HEAD)" \
  "same-target retry did not restore the explicit previous SHA"
[[ ! -e "$RETRY_BUNDLE" ]] || fail "retry deployment bundle was not removed"
grep -q "Rollback completed; the previous release is healthy" "$TEST_ROOT/retry.out" || \
  fail "same-target retry rollback was not reported"

# Restore the known healthy descendant for the stale-run assertion below.
git -C "$APP_DIR" reset -q --hard "$HEALTHY_SHA"

# An older queued run must not roll the healthy descendant back.
: > "$COMMAND_LOG"
STALE_BUNDLE="$TEST_ROOT/stale.bundle"
touch "$STALE_BUNDLE"
run_deploy "$PREVIOUS_SHA" "$STALE_BUNDLE" > "$TEST_ROOT/stale.out" 2>&1
assert_eq "$HEALTHY_SHA" "$(git -C "$APP_DIR" rev-parse HEAD)" "stale run rolled production back"
[[ ! -s "$COMMAND_LOG" ]] || fail "stale deployment executed build or service commands"
[[ ! -e "$STALE_BUNDLE" ]] || fail "stale deployment bundle was not removed"
grep -q "Skipping stale deployment" "$TEST_ROOT/stale.out" || fail "stale deployment was not reported"

# The legacy workflow supplies only a fixed-name bundle. The script must read
# its main SHA and deploy it even when the workflow did not pass TARGET_SHA.
git -C "$APP_DIR" update-ref refs/heads/main "$HEALTHY_SHA"
git -C "$APP_DIR" reset -q --hard "$PREVIOUS_SHA"
LEGACY_BUNDLE="$TEST_ROOT/legacy.bundle"
git -C "$APP_DIR" bundle create "$LEGACY_BUNDLE" main
run_legacy_deploy "$LEGACY_BUNDLE" > "$TEST_ROOT/legacy.out" 2>&1
assert_eq "$HEALTHY_SHA" "$(git -C "$APP_DIR" rev-parse HEAD)" "legacy bundle target was not deployed"
[[ ! -e "$LEGACY_BUNDLE" ]] || fail "legacy deployment bundle was not removed"
grep -q "Deployment completed successfully" "$TEST_ROOT/legacy.out" || \
  fail "legacy deployment success was not reported"

printf 'deploy tests: OK\n'
