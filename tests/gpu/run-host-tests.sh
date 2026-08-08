#!/bin/sh
# SPDX-License-Identifier: MIT

set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/m1n1-gpu-handoff-tests.XXXXXX")
trap 'rm -rf "$build_dir"' EXIT HUP INT TERM

cc=${CC:-clang}
common_flags="-std=c11 -Wall -Wextra -Werror -pedantic"

build_and_run()
{
    output=$1
    shift
    "$cc" $common_flags "$@" \
        -DGPU_HANDOFF_ABI_HOST_TEST=1 \
        -I"$repo_dir/src" \
        "$repo_dir/src/gpu_handoff_abi.c" \
        "$repo_dir/tests/gpu/test_gpu_handoff_abi.c" \
        -o "$output"
    "$output"
}

build_and_run "$build_dir/test_gpu_handoff_abi"
build_and_run "$build_dir/test_gpu_handoff_abi-sanitized" \
    -fsanitize=address,undefined -fno-omit-frame-pointer
