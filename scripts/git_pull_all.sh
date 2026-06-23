#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  scripts/git_pull_all.sh

Fast-forwards the current worktree and updates submodules to the commits
recorded by the current branch.

Rules:
  - refuses to run with local uncommitted changes
  - uses fast-forward only; no merge commits and no rebase
  - syncs submodule URLs from this branch's .gitmodules
  - updates submodules to recorded commits by default

Environment:
  GIT_PULL_REMOTE_SUBMODULES=1  after updating recorded commits, also fast-
                                forward submodules that have branch= configured
                                in .gitmodules
  GIT_PULL_DRY_RUN=1            print actions without changing repositories
EOF
}

log() {
    printf '[git-pull-all] %s\n' "$*" >&2
}

die() {
    printf '[git-pull-all] ERROR: %s\n' "$*" >&2
    exit 1
}

run() {
    if [[ "${GIT_PULL_DRY_RUN:-0}" == "1" ]]; then
        printf '[git-pull-all] DRY-RUN:'
        printf ' %q' "$@"
        printf '\n'
    else
        "$@"
    fi
}

repo_root() {
    git rev-parse --show-toplevel
}

has_changes() {
    local repo=$1
    [[ -n "$(git -C "$repo" status --porcelain)" ]]
}

current_branch() {
    local repo=$1
    git -C "$repo" symbolic-ref --quiet --short HEAD 2>/dev/null || true
}

submodule_paths() {
    git -C "$ROOT_DIR" config -f .gitmodules --get-regexp '^submodule\..*\.path$' |
        awk '{print $2}'
}

configured_submodule_branch() {
    local path=$1
    git -C "$ROOT_DIR" config -f .gitmodules --get "submodule.${path}.branch" 2>/dev/null || true
}

ensure_clean_repository() {
    local repo=$1
    local label=$2

    if has_changes "$repo"; then
        die "$label has local changes; run ./update.sh first or commit/stash them"
    fi
}

pull_current_branch() {
    local repo=$1
    local label=$2
    local branch

    branch=$(current_branch "$repo")
    [[ -n "$branch" ]] || die "$label is on detached HEAD"

    log "fast-forwarding $label on $branch"
    run git -C "$repo" pull --ff-only
}

pull_configured_submodule_branches() {
    local path repo branch

    while IFS= read -r path; do
        [[ -z "$path" ]] && continue
        repo="$ROOT_DIR/$path"
        [[ -d "$repo/.git" || -f "$repo/.git" ]] || continue

        branch=$(configured_submodule_branch "$path")
        [[ -n "$branch" ]] || continue

        ensure_clean_repository "$repo" "submodule $path"
        log "fast-forwarding submodule $path on $branch"

        if git -C "$repo" show-ref --verify --quiet "refs/heads/$branch"; then
            run git -C "$repo" switch "$branch"
        else
            run git -C "$repo" switch -c "$branch"
        fi
        run git -C "$repo" pull --ff-only
    done < <(submodule_paths)
}

main() {
    if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
        usage
        exit 0
    fi

    ROOT_DIR=$(repo_root)
    readonly ROOT_DIR
    cd "$ROOT_DIR"

    log "repository: $ROOT_DIR"
    ensure_clean_repository "$ROOT_DIR" "main repository"
    pull_current_branch "$ROOT_DIR" "main repository"

    log "syncing submodule URLs"
    run git submodule sync --recursive

    log "updating submodules to recorded commits"
    run git submodule update --init --recursive

    if [[ "${GIT_PULL_REMOTE_SUBMODULES:-0}" == "1" ]]; then
        pull_configured_submodule_branches
    fi

    log "done"
}

main "$@"
