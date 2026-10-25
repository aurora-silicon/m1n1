# SPDX-License-Identifier: MIT
"""Exercise the actual display status reader with bounded FDT properties."""

from pathlib import Path
import subprocess


def test_display_status_is_a_complete_bounded_string(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/kboot.c").read_text()
    start = source.index("static bool dt_t8140_node_enabled(")
    helper = source[start:source.index("static int dt_t8140_audit_iommus(", start)]
    program = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include "libfdt.h"
static unsigned char tree[4096];
static void *dt = tree;
''' + helper + r'''
static const char *test_property;
static int test_length;
static const void *fixture_getprop(const void *blob, int node, const char *name, int *length)
{
    (void)blob;
    (void)node;
    assert(!strcmp(name, "status"));
    if (length)
        *length = test_length;
    return test_property;
}
#define fdt_getprop fixture_getprop
#define dt_t8140_node_enabled fixture_node_enabled
''' + helper + r'''
#undef dt_t8140_node_enabled
#undef fdt_getprop
int main(void)
{
    /* A real four-byte allocation exposes an unbounded strcmp under ASan. */
    char *four = malloc(4);
    assert(four);
    memcpy(four, "okay", 4);
    test_property = four;
    test_length = 4;
    assert(!fixture_node_enabled(0));
    free(four);

    test_property = NULL;
    test_length = -FDT_ERR_NOTFOUND;
    assert(fixture_node_enabled(0));
    test_length = -FDT_ERR_BADOFFSET;
    assert(!fixture_node_enabled(0));
    test_length = 0;
    assert(!fixture_node_enabled(0));

    /* Real FDT properties can be followed by a zero tag byte without the
     * zero being part of the property. That must not validate "okay". */
    assert(fdt_create_empty_tree(dt, sizeof(tree)) == 0);
    int node = fdt_add_subnode(dt, 0, "display");
    assert(node >= 0 && dt_t8140_node_enabled(node));
    assert(!dt_t8140_node_enabled(-1));
    const struct {
        const char *value;
        int length;
        bool enabled;
    } cases[] = {
        {"okay", 5, true}, {"ok", 3, true},
        {"okay", 4, false}, {"ok", 2, false},
        {"okay\0junk", 10, false}, {"ok\0junk", 8, false},
        {"okayX", 5, false}, {"disabled", 9, false}, {"", 0, false},
    };
    for (unsigned i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        assert(fdt_setprop(dt, node, "status", cases[i].value, cases[i].length) == 0);
        assert(dt_t8140_node_enabled(node) == cases[i].enabled);
    }
    return 0;
}
'''
    libfdt = repo / "src/libfdt"
    sources = [libfdt / name for name in (
        "fdt.c", "fdt_addresses.c", "fdt_check.c", "fdt_empty_tree.c",
        "fdt_ro.c", "fdt_rw.c", "fdt_strerror.c", "fdt_sw.c", "fdt_wip.c",
    )]
    binary = tmp_path / "display-status"
    subprocess.run(
        ["cc", "-D_GNU_SOURCE", "-std=c11", "-Wall", "-Wextra", "-Werror",
         "-fsanitize=address,undefined", "-fno-sanitize-recover=undefined",
         "-I", str(libfdt), "-x", "c", "-", *map(str, sources), "-o", str(binary)],
        input=program, text=True, check=True,
    )
    result = subprocess.run([str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
