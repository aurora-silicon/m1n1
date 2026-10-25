#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
${CC:-cc} -std=gnu11 -Wall -Wextra -Werror -Isrc tests/payload_profile_host.c \
 src/libfdt/fdt.c src/libfdt/fdt_ro.c src/libfdt/fdt_rw.c src/libfdt/fdt_wip.c \
 src/libfdt/fdt_sw.c src/libfdt/fdt_empty_tree.c src/libfdt/fdt_addresses.c -o "$tmp/test"
"$tmp/test"
echo 'PASS: exact firmware DT selection, archive order and legacy framebuffer fallback'
