# SPDX-License-Identifier: MIT
"""A refused J514S native handoff still records why in /chosen.

Runs kboot.c's dt_set_j514s_native(), dt_t6030_panel() and dt_display_transaction() as they are,
on a real FDT, with the panel and external-controller handoffs replaced by fakes.
"""

from pathlib import Path
from test_siliconforest_handoff import run_c

ROOT = Path(__file__).resolve().parents[2]


def test_refused_j514s_handoff_records_its_result(tmp_path):
    source = (ROOT / "src/kboot.c").read_text()

    def function(name, kind="static int "):
        start = source.index(kind + name + "(")
        return source[start:source.index("\n}", start) + 2] + "\n"

    start = source.index("static char t6030_display_handoff[")
    state = source[start:source.index("\n", start) + 1]
    run_c(tmp_path, "j514s-result", r'''
#include <assert.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libfdt.h"
static void *dt;
static unsigned char blob[8192];
static int fail_alloc;
static void *checked_malloc(size_t size) { return fail_alloc ? NULL : malloc(size); }
''' + state + r'''
/* 0: success; 1: refused at the mappings; 2: refused before them; 3: external refused */
static int outcome, dcpext_calls;
static int dt_set_t6030_dcp_handoff(void)
{
    int node = fdt_path_offset(dt, "/soc/dcp");
    assert(node >= 0 && !fdt_setprop_string(dt, node, "status", "okay"));
    if (outcome == 2)
        return -1;
    if (outcome == 1) {
        snprintf(t6030_display_handoff, sizeof(t6030_display_handoff),
                 "SID4: segment 2 mapped 0x4000 of 0x8000 bytes");
        return -1;
    }
    snprintf(t6030_display_handoff, sizeof(t6030_display_handoff), "PIODMA enabled");
    int chosen = fdt_path_offset(dt, "/chosen");
    assert(!fdt_setprop_string(dt, chosen, "aurora,j514s-display-handoff", t6030_display_handoff));
    return 0;
}
static int dt_set_t6030_dcpext(unsigned int n)
{
    (void)n;
    dcpext_calls++;
    return outcome == 3 ? -1 : 0;
}
#define malloc checked_malloc
''' + function("dt_display_transaction") + r'''
#undef malloc
''' + function("dt_t6030_panel") + function("dt_set_j514s_native", "static void ") + r'''
static void world(int which)
{
    outcome = which;
    dcpext_calls = 0;
    fail_alloc = 0;
    memset(blob, 0, sizeof(blob));
    dt = blob;
    assert(!fdt_create_empty_tree(dt, sizeof(blob)));
    assert(fdt_add_subnode(dt, 0, "chosen") >= 0);
    int soc = fdt_add_subnode(dt, 0, "soc");
    int dcp = fdt_add_subnode(dt, soc, "dcp");
    assert(dcp >= 0 && !fdt_setprop_string(dt, dcp, "status", "disabled"));
}
static const char *result(void)
{
    return fdt_getprop(dt, fdt_path_offset(dt, "/chosen"), "aurora,j514s-display-handoff", NULL);
}
static const char *dcp_status(void)
{
    return fdt_getprop(dt, fdt_path_offset(dt, "/soc/dcp"), "status", NULL);
}
int main(void)
{
    world(0);
    dt_set_j514s_native();
    assert(!strcmp(result(), "PIODMA enabled") && !strcmp(dcp_status(), "okay"));
    assert(dcpext_calls == 2);

    /* A refused mapping keeps its reason; the transaction's other edits are gone. */
    world(1);
    dt_set_j514s_native();
    assert(!strcmp(result(), "SID4: segment 2 mapped 0x4000 of 0x8000 bytes"));
    assert(!strcmp(dcp_status(), "disabled") && dcpext_calls == 0);

    /* Refused before the mappings, for example by its firmware. */
    world(2);
    dt_set_j514s_native();
    assert(!strcmp(result(), "DCP firmware handoff refused"));
    assert(!strcmp(dcp_status(), "disabled") && dcpext_calls == 0);

    /* A stale result from an earlier attempt is not published. */
    world(1);
    dt_set_j514s_native();
    world(2);
    dt_set_j514s_native();
    assert(!strcmp(result(), "DCP firmware handoff refused"));

    /* Without memory for the transaction, the panel step never runs: say so. */
    world(0);
    fail_alloc = 1;
    dt_set_j514s_native();
    assert(!strcmp(result(), "no memory for the handoff transaction"));
    assert(!strcmp(dcp_status(), "disabled") && dcpext_calls == 0);

    /* A refused external controller does not change the panel's result. */
    world(3);
    dt_set_j514s_native();
    assert(!strcmp(result(), "PIODMA enabled") && dcpext_calls == 2);
    puts("PASS j514s handoff result");
    return 0;
}
''')
