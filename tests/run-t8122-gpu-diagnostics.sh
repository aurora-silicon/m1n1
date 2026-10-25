#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
test_dir=$(mktemp -d)
trap 'rm -rf "$test_dir"' EXIT HUP INT TERM
${CC:-cc} -std=c11 -D_GNU_SOURCE -Wall -Wextra -Werror \
    -fsanitize=address,undefined -fno-omit-frame-pointer -g \
    tests/t8122_gpu_diagnostics.c src/kboot_gpu_t8122.c \
    src/libfdt/fdt.c src/libfdt/fdt_ro.c src/libfdt/fdt_rw.c \
    src/libfdt/fdt_wip.c src/libfdt/fdt_sw.c src/libfdt/fdt_empty_tree.c \
    -Wl,--wrap=malloc -o "$test_dir/t8122-gpu-diagnostics"
"$test_dir/t8122-gpu-diagnostics"
${CC:-cc} -std=c11 -D_GNU_SOURCE -Wall -Wextra -Werror \
    -fsanitize=address,undefined -fno-omit-frame-pointer -g \
    tests/test_gfx_fw_image_info.c -o "$test_dir/gfx-fw-image-info"
"$test_dir/gfx-fw-image-info"
${CC:-cc} -std=c11 -D_GNU_SOURCE -Wall -Wextra -Werror \
    -fsanitize=address,undefined -fno-omit-frame-pointer -g \
    tests/t8122_powered_identity.c src/kboot_gpu_t8122_power.c \
    src/libfdt/fdt.c src/libfdt/fdt_ro.c src/libfdt/fdt_rw.c \
    src/libfdt/fdt_wip.c src/libfdt/fdt_sw.c src/libfdt/fdt_empty_tree.c \
    -o "$test_dir/t8122-powered-identity"
"$test_dir/t8122-powered-identity"
${CC:-cc} -std=c11 -D_GNU_SOURCE -Wall -Wextra -Werror \
    -fsanitize=address,undefined -fno-omit-frame-pointer -g \
    tests/t8122_full_identity.c src/kboot_gpu_t8122_power.c \
    src/libfdt/fdt.c src/libfdt/fdt_ro.c src/libfdt/fdt_rw.c \
    src/libfdt/fdt_wip.c src/libfdt/fdt_sw.c src/libfdt/fdt_empty_tree.c \
    -o "$test_dir/t8122-full-identity"
"$test_dir/t8122-full-identity" ${IDENTITY_FIXTURE_DIR:+"$IDENTITY_FIXTURE_DIR"}
