#!/bin/sh
# SPDX-License-Identifier: MIT
#
# Host tests for the framebuffer change detector. No hardware needed.
set -eu
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/m1n1-fb-capture-tests.XXXXXX")
trap 'rm -rf "$build_dir"' EXIT HUP INT TERM
cc=${CC:-clang}
"$cc" -std=c11 -Wall -Wextra -Werror -pedantic \
    -I"$repo_dir/src" \
    "$repo_dir/tests/fb_capture/test_fb_capture.c" \
    -o "$build_dir/test_fb_capture"
"$build_dir/test_fb_capture"
