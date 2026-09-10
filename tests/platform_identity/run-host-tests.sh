#!/bin/sh
# SPDX-License-Identifier: MIT

set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/m1n1-platform-identity-tests.XXXXXX")
trap 'rm -rf "$build_dir"' EXIT HUP INT TERM

cc=${CC:-clang}
common_flags="-std=c11 -Wall -Wextra -Werror -Wconversion -Wsign-conversion -pedantic"

build_and_run()
{
    output=$1
    source=$2
    shift 2
    "$cc" $common_flags "$@" \
        -DPLATFORM_IDENTITY_HOST_TEST=1 \
        -I"$repo_dir/src" \
        "$repo_dir/src/platform_identity.c" \
        "$repo_dir/tests/platform_identity/$source" \
        -o "$output"
    "$output"
}

for source in test_j414s_identity.c test_j873g_identity.c; do
    build_and_run "$build_dir/${source%.c}" "$source"
    build_and_run "$build_dir/${source%.c}-sanitized" "$source" \
        -fsanitize=address,undefined -fno-omit-frame-pointer
done
