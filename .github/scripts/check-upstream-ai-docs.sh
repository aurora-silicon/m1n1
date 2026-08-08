#!/usr/bin/env bash
set -euo pipefail

readonly provenance_ref="refs/remotes/origin/asahilinux/main"
readonly protected_paths=(AGENTS.md CLAUDE.md GEMINI.md)
readonly candidate_ref="${CANDIDATE_REF:-HEAD}"

git fetch --no-tags origin \
    "refs/heads/asahilinux/main:${provenance_ref}"

resolved_blob() {
    local commit="$1"
    local path="$2"
    local entry mode blob target

    entry="$(git ls-tree "$commit" -- "$path")"
    [[ -n "$entry" ]] || return 1

    read -r mode _ blob _ <<<"$entry"
    if [[ "$mode" == "120000" ]]; then
        target="$(git cat-file blob "$blob")"
        [[ "$target" != /* && "$target" != *".."* ]] || return 1
        git rev-parse "$commit:$target"
    else
        printf '%s\n' "$blob"
    fi
}

blocked=0
for path in "${protected_paths[@]}"; do
    head_blob="$(resolved_blob "$candidate_ref" "$path" || true)"
    upstream_blob="$(resolved_blob "$provenance_ref" "$path" || true)"

    if [[ -n "$head_blob" && "$head_blob" == "$upstream_blob" ]]; then
        echo "::error file=$path::$path resolves to content inherited from asahilinux/main"
        blocked=1
    fi
done

if (( blocked )); then
    echo "Replace the inherited documents with Aurora-authored guidance or remove them."
    exit 1
fi

echo "No protected document content was inherited from asahilinux/main."
