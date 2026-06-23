#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  scripts/git_sync_all.sh [commit message]

Commits and pushes dirty initialized submodules first, then commits and pushes
the main repository, including updated submodule pointers.

Rules:
  - ignored files stay ignored
  - no force push
  - no automatic rebase/merge
  - submodules are pushed before the main repository
  - clean detached submodules are skipped
  - clean branch submodules are pushed only when they have unpushed commits

Environment:
  GIT_SYNC_REMOTE=remote-name   override push remote for repositories without an upstream
  GIT_SYNC_DRY_RUN=1            print actions without committing or pushing
EOF
}

log() {
    printf '[git-sync-all] %s\n' "$*" >&2
}

die() {
    printf '[git-sync-all] ERROR: %s\n' "$*" >&2
    exit 1
}

run() {
    if [[ "${GIT_SYNC_DRY_RUN:-0}" == "1" ]]; then
        printf '[git-sync-all] DRY-RUN:'
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

has_unmerged_paths() {
    local repo=$1
    [[ -n "$(git -C "$repo" diff --name-only --diff-filter=U)" ]]
}

current_branch() {
    local repo=$1
    git -C "$repo" symbolic-ref --quiet --short HEAD 2>/dev/null || true
}

configured_submodule_branch() {
    local repo=$1
    local path=$2
    git -C "$repo" config -f .gitmodules --get "submodule.${path}.branch" 2>/dev/null || true
}

ensure_push_branch() {
    local repo=$1
    local submodule_path=${2:-}
    local branch
    branch=$(current_branch "$repo")

    if [[ -n "$branch" ]]; then
        printf '%s\n' "$branch"
        return
    fi

    if [[ -n "$submodule_path" ]]; then
        branch=$(configured_submodule_branch "$ROOT_DIR" "$submodule_path")
        if [[ -n "$branch" ]]; then
            log "submodule $submodule_path is detached; switching to configured branch $branch"
            if git -C "$repo" show-ref --verify --quiet "refs/heads/$branch"; then
                run git -C "$repo" switch "$branch"
            else
                run git -C "$repo" switch -c "$branch"
            fi
            printf '%s\n' "$branch"
            return
        fi
    fi

    die "$repo is on detached HEAD and no branch is configured"
}

choose_push_remote() {
    local repo=$1
    local branch=$2
    local upstream remote

    upstream=$(git -C "$repo" rev-parse --abbrev-ref --symbolic-full-name "@{upstream}" 2>/dev/null || true)
    if [[ -n "$upstream" && "$upstream" == */* ]]; then
        printf '%s\n' "${upstream%%/*}"
        return
    fi

    if [[ -n "${GIT_SYNC_REMOTE:-}" ]]; then
        git -C "$repo" remote get-url "$GIT_SYNC_REMOTE" >/dev/null 2>&1 ||
            die "remote $GIT_SYNC_REMOTE does not exist in $repo"
        printf '%s\n' "$GIT_SYNC_REMOTE"
        return
    fi

    remote=$(git -C "$repo" config "branch.${branch}.remote" 2>/dev/null || true)
    if [[ -n "$remote" && "$remote" != "." ]]; then
        printf '%s\n' "$remote"
        return
    fi

    if git -C "$repo" remote get-url origin >/dev/null 2>&1; then
        printf 'origin\n'
        return
    fi

    die "no push remote found for $repo"
}

commit_repo_if_dirty() {
    local repo=$1
    local label=$2
    local branch=$3
    local message=$4

    if ! has_changes "$repo"; then
        log "$label clean; skip commit"
        return
    fi

    has_unmerged_paths "$repo" && die "$label has unresolved merge conflicts"

    log "committing $label on $branch"
    run git -C "$repo" add -A

    if [[ "${GIT_SYNC_DRY_RUN:-0}" != "1" ]] && git -C "$repo" diff --cached --quiet; then
        log "$label has no staged changes after git add; skip commit"
        return
    fi

    run git -C "$repo" commit -m "$message"
}

push_repo() {
    local repo=$1
    local label=$2
    local branch=$3
    local remote

    remote=$(choose_push_remote "$repo" "$branch")
    log "pushing $label to $remote/$branch"
    run git -C "$repo" push -u "$remote" "$branch:$branch"
}

has_unpushed_commits() {
    local repo=$1
    local branch=$2
    local upstream remote_ref

    upstream=$(git -C "$repo" rev-parse --abbrev-ref --symbolic-full-name "@{upstream}" 2>/dev/null || true)
    if [[ -n "$upstream" ]]; then
        [[ "$(git -C "$repo" rev-list --count "${upstream}..HEAD")" != "0" ]]
        return
    fi

    remote_ref=$(git -C "$repo" for-each-ref --format='%(refname:short)' "refs/remotes/*/$branch" | head -n 1)
    if [[ -n "$remote_ref" ]]; then
        [[ "$(git -C "$repo" rev-list --count "${remote_ref}..HEAD")" != "0" ]]
        return
    fi

    return 1
}

submodule_paths() {
    git -C "$ROOT_DIR" config -f .gitmodules --get-regexp '^submodule\..*\.path$' |
        awk '{print $2}'
}

main() {
    if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
        usage
        exit 0
    fi

    ROOT_DIR=$(repo_root)
    readonly ROOT_DIR
    cd "$ROOT_DIR"

    local timestamp message_prefix main_branch submodule_path submodule_repo submodule_branch
    timestamp=$(date '+%Y-%m-%d %H:%M:%S %z')
    message_prefix=${*:-"Sync local changes"}

    log "repository: $ROOT_DIR"
    log "syncing submodule URLs"
    run git submodule sync --recursive

    while IFS= read -r submodule_path; do
        [[ -z "$submodule_path" ]] && continue
        submodule_repo="$ROOT_DIR/$submodule_path"

        if [[ ! -d "$submodule_repo/.git" && ! -f "$submodule_repo/.git" ]]; then
            log "submodule $submodule_path not initialized; skip"
            continue
        fi

        if has_changes "$submodule_repo"; then
            submodule_branch=$(ensure_push_branch "$submodule_repo" "$submodule_path")
            commit_repo_if_dirty \
                "$submodule_repo" \
                "submodule $submodule_path" \
                "$submodule_branch" \
                "$message_prefix: $submodule_path ($timestamp)"

            push_repo "$submodule_repo" "submodule $submodule_path" "$submodule_branch"
            continue
        fi

        submodule_branch=$(current_branch "$submodule_repo")
        if [[ -z "$submodule_branch" ]]; then
            log "submodule $submodule_path clean and detached; skip push"
            continue
        fi

        if has_unpushed_commits "$submodule_repo" "$submodule_branch"; then
            push_repo "$submodule_repo" "submodule $submodule_path" "$submodule_branch"
        else
            log "submodule $submodule_path clean with no unpushed commits; skip push"
        fi
    done < <(submodule_paths)

    log "recording dependency state in main repository"

    main_branch=$(ensure_push_branch "$ROOT_DIR")
    commit_repo_if_dirty \
        "$ROOT_DIR" \
        "main repository" \
        "$main_branch" \
        "$message_prefix: main repository ($timestamp)"

    if has_unpushed_commits "$ROOT_DIR" "$main_branch"; then
        push_repo "$ROOT_DIR" "main repository" "$main_branch"
    else
        log "main repository has no unpushed commits; skip push"
    fi
    log "done"
}

main "$@"
