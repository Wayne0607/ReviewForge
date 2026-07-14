#!/bin/bash
# ReviewForge production deployment. The workflow executes this file from the
# incoming commit so rollback protection is available on the first fixed run.
set -Eeuo pipefail

APP_DIR="${REVIEWFORGE_APP_DIR:-/opt/reviewforge}"
BACKEND_DIR="$APP_DIR/backend"
FRONTEND_DIR="$APP_DIR/frontend"
SERVICE_NAME="${REVIEWFORGE_SERVICE_NAME:-reviewforge}"
LOCK_FILE="${REVIEWFORGE_DEPLOY_LOCK:-/var/lock/reviewforge-deploy.lock}"
LOCK_WAIT_SECONDS="${REVIEWFORGE_LOCK_WAIT_SECONDS:-600}"
HEALTH_URL="${REVIEWFORGE_HEALTH_URL:-http://127.0.0.1:8000/health}"
HEALTH_ATTEMPTS="${REVIEWFORGE_HEALTH_ATTEMPTS:-20}"
HEALTH_INTERVAL_SECONDS="${REVIEWFORGE_HEALTH_INTERVAL_SECONDS:-2}"
TARGET_SHA="${REVIEWFORGE_TARGET_SHA:-}"
PREVIOUS_SHA="${REVIEWFORGE_PREVIOUS_SHA:-}"
BUNDLE_PATH="${REVIEWFORGE_BUNDLE_PATH:-}"
UV_BIN=""
ROLLBACK_ARMED=0
ROLLBACK_RUNNING=0

log() {
    printf '%s\n' "$*"
}

die() {
    log "ERROR: $*" >&2
    exit 1
}

require_command() {
    command -v "$1" > /dev/null 2>&1 || die "Required command not found: $1"
}

git_app() {
    # setup-server.sh assigns /opt/reviewforge to the service user while the
    # current deployment connection may be root. Trust this one explicit path
    # without weakening Git's ownership check globally.
    git -c safe.directory="$APP_DIR" -C "$APP_DIR" "$@"
}

validate_positive_integer() {
    local name="$1"
    local value="$2"
    [[ "$value" =~ ^[1-9][0-9]*$ ]] || die "$name must be a positive integer"
}

resolve_uv() {
    if [[ -n "${REVIEWFORGE_UV_BIN:-}" ]]; then
        [[ -x "$REVIEWFORGE_UV_BIN" ]] || die "REVIEWFORGE_UV_BIN is not executable"
        UV_BIN="$REVIEWFORGE_UV_BIN"
    elif [[ -n "${HOME:-}" && -x "$HOME/.local/bin/uv" ]]; then
        # setup-server.sh currently installs uv here when run as root.
        UV_BIN="$HOME/.local/bin/uv"
    elif command -v uv > /dev/null 2>&1; then
        UV_BIN="$(command -v uv)"
    else
        die "uv is required (checked REVIEWFORGE_UV_BIN, ~/.local/bin/uv, and PATH)"
    fi
}

run_systemctl() {
    if (( EUID == 0 )); then
        systemctl "$@"
    else
        sudo -n systemctl "$@"
    fi
}

print_service_logs() {
    log "--- Last 80 journal lines for ${SERVICE_NAME} ---" >&2
    if ! command -v journalctl > /dev/null 2>&1; then
        log "journalctl is unavailable" >&2
        return 0
    fi

    if (( EUID == 0 )); then
        journalctl -u "$SERVICE_NAME" --no-pager -n 80 >&2 || true
    elif command -v sudo > /dev/null 2>&1; then
        sudo -n journalctl -u "$SERVICE_NAME" --no-pager -n 80 >&2 || true
    else
        log "Cannot read service journal without root or sudo" >&2
    fi
}

wait_for_health() {
    local attempt
    log "--- Health check (${HEALTH_ATTEMPTS} attempts, ${HEALTH_INTERVAL_SECONDS}s interval) ---"
    for ((attempt = 1; attempt <= HEALTH_ATTEMPTS; attempt++)); do
        # 20 one-second probes plus 19 two-second intervals cap the default
        # failure window below one minute, while allowing normal warm-up time.
        if curl --fail --silent --show-error --max-time 1 "$HEALTH_URL" > /dev/null 2>&1; then
            log "Health check passed on attempt ${attempt}"
            return 0
        fi
        if (( attempt < HEALTH_ATTEMPTS )); then
            sleep "$HEALTH_INTERVAL_SECONDS"
        fi
    done

    log "ERROR: Health check failed after ${HEALTH_ATTEMPTS} attempts" >&2
    print_service_logs
    return 1
}

load_service_environment() {
    if [[ -f "$APP_DIR/.env" ]]; then
        set -a
        # shellcheck disable=SC1091
        . "$APP_DIR/.env"
        set +a
    fi
}

deploy_checked_out_release() {
    local label="${1:-Deploying}"

    log "=== ${label} ReviewForge ==="
    log "Commit: $(git_app rev-parse --short HEAD)"

    [[ -d "$FRONTEND_DIR" ]] || die "Frontend directory not found: $FRONTEND_DIR"
    [[ -d "$BACKEND_DIR" ]] || die "Backend directory not found: $BACKEND_DIR"

    log "--- Building frontend ---"
    cd "$FRONTEND_DIR"
    npm ci --ignore-scripts
    npm run build

    log "--- Syncing Python dependencies ---"
    cd "$BACKEND_DIR"
    "$UV_BIN" sync --frozen

    log "--- Running spec-check ---"
    "$UV_BIN" run reviewforge spec-check

    log "--- Restarting ${SERVICE_NAME} service ---"
    run_systemctl restart "$SERVICE_NAME"
    run_systemctl is-active --quiet "$SERVICE_NAME"
    wait_for_health
}

rollback_to_previous() {
    log "=== Rolling back to ${PREVIOUS_SHA} ===" >&2
    git_app reset --hard "$PREVIOUS_SHA" || return 1
    deploy_checked_out_release "Restoring"
}

on_exit() {
    local status=$?
    local rollback_status

    trap - EXIT INT TERM
    if (( status != 0 && ROLLBACK_ARMED == 1 && ROLLBACK_RUNNING == 0 )); then
        ROLLBACK_RUNNING=1
        print_service_logs
        set +e
        (
            set -Eeuo pipefail
            rollback_to_previous
        )
        rollback_status=$?
        set -e
        if (( rollback_status == 0 )); then
            log "Rollback completed; the previous release is healthy" >&2
        else
            log "CRITICAL: rollback failed with status ${rollback_status}" >&2
            print_service_logs
        fi
    fi

    if [[ -n "$BUNDLE_PATH" ]]; then
        rm -f -- "$BUNDLE_PATH" || log "WARNING: failed to remove bundle $BUNDLE_PATH" >&2
    fi

    exit "$status"
}

main() {
    local current_sha

    validate_positive_integer REVIEWFORGE_LOCK_WAIT_SECONDS "$LOCK_WAIT_SECONDS"
    validate_positive_integer REVIEWFORGE_HEALTH_ATTEMPTS "$HEALTH_ATTEMPTS"
    validate_positive_integer REVIEWFORGE_HEALTH_INTERVAL_SECONDS "$HEALTH_INTERVAL_SECONDS"

    require_command git
    require_command flock
    require_command node
    require_command npm
    require_command curl
    require_command systemctl
    if (( EUID != 0 )); then
        require_command sudo
    fi
    resolve_uv

    [[ -d "$APP_DIR/.git" ]] || die "Not a git repository: $APP_DIR"
    load_service_environment

    # FD 9 remains open for the whole process, including rollback.
    exec 9> "$LOCK_FILE" || die "Cannot open deployment lock: $LOCK_FILE"
    flock -w "$LOCK_WAIT_SECONDS" 9 || die "Timed out waiting for deployment lock"
    log "Acquired deployment lock: $LOCK_FILE"

    cd "$APP_DIR"
    current_sha="$(git_app rev-parse HEAD)"
    if [[ -z "$PREVIOUS_SHA" ]]; then
        PREVIOUS_SHA="$current_sha"
    fi
    git_app cat-file -e "${PREVIOUS_SHA}^{commit}" || die "Invalid previous commit: $PREVIOUS_SHA"

    if [[ -n "$TARGET_SHA" ]]; then
        [[ "$TARGET_SHA" =~ ^[0-9a-fA-F]{40}$ ]] || die "REVIEWFORGE_TARGET_SHA must be a full commit SHA"
        TARGET_SHA="${TARGET_SHA,,}"
        git_app cat-file -e "${TARGET_SHA}^{commit}" || die "Target commit is not available: $TARGET_SHA"

        # Do not let an older queued workflow replace an already deployed
        # descendant. Equal SHAs are deliberately redeployed for manual retries.
        if [[ "$TARGET_SHA" != "$current_sha" ]] && git_app merge-base --is-ancestor "$TARGET_SHA" "$current_sha"; then
            log "Skipping stale deployment ${TARGET_SHA}; current ${current_sha} already contains it"
            return 0
        fi

        if [[ "$TARGET_SHA" != "$current_sha" ]]; then
            ROLLBACK_ARMED=1
            git_app reset --hard "$TARGET_SHA"
        fi
    elif [[ "$PREVIOUS_SHA" != "$current_sha" ]]; then
        # Supports a manual deployment that supplies its pre-pull SHA.
        ROLLBACK_ARMED=1
    fi

    deploy_checked_out_release "Deploying"
    ROLLBACK_ARMED=0
    log "Deployment completed successfully"
}

trap on_exit EXIT
trap 'exit 130' INT TERM
main "$@"
