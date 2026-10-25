# SPDX-License-Identifier: MIT
"""Exercise the J514S publication bodies against real libfdt route statuses."""
from pathlib import Path
from test_siliconforest_handoff import definition, run_c

ROOT = Path(__file__).resolve().parents[2]


def test_j514s_preserves_unavailable_routes_and_controllers(tmp_path):
    source = (ROOT / "src/kboot.c").read_text()
    run_c(tmp_path, "j514s-policy", r'''
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "libfdt.h"
static unsigned char blob[16384];
static void *dt = blob;
static int validations, reservations;
static int dt_t6030_dcpext_validate(unsigned n, const char *path, char *uuid)
{ (void)n; (void)path; strcpy(uuid, "test-uuid"); validations++; return 0; }
static int dt_t6030_dcpext_reserve_tables(unsigned n)
{ (void)n; reservations++; return 0; }
static int dt_reserve_asc_firmware(const char *path, const char *alt,
                                  const char *alias, int remap, unsigned long base)
{ (void)path; (void)alt; (void)alias; (void)remap; (void)base; return 0; }
static int dt_set_dcp_firmware(const char *alias) { (void)alias; return 0; }
''' + definition(source, "dt_t6030_dcpext_enable") + '\n' +
        definition(source, "dt_set_t6030_dcpext") + r'''
static int path(const char *name)
{ int n = fdt_path_offset(dt, name); assert(n >= 0); return n; }
static void world(const char *controller, int controller_len)
{
    assert(!fdt_create_empty_tree(dt, sizeof(blob)));
    int n = fdt_add_subnode(dt, 0, "controller"); assert(n >= 0);
    if (controller) assert(!fdt_setprop(dt, n, "status", controller, controller_len));
    assert(fdt_add_subnode(dt, n, "typec-routes") >= 0);
    const char *names[] = {"disabled", "reserved", "failed", "prefix", "legacy", "okay", "unterminated"};
    const char *values[] = {"disabled", "reserved", "fail", "disabledX", NULL, "okay", "disabled"};
    for (unsigned i = 0; i < 7; i++) {
        n = fdt_add_subnode(dt, path("/controller/typec-routes"), names[i]); assert(n >= 0);
        if (values[i]) assert(!fdt_setprop(dt, n, "status", values[i],
                              i == 6 ? 8 : (int)strlen(values[i]) + 1));
    }
    n = fdt_add_subnode(dt, 0, "aliases"); assert(n >= 0);
    assert(!fdt_setprop_string(dt, n, "dcpext0", "/controller"));
    validations = reservations = 0;
}
static void expect(const char *suffix, const char *value, int length)
{
    char name[128]; snprintf(name, sizeof(name), "/controller/typec-routes/%s", suffix);
    int len; const char *actual = fdt_getprop(dt, path(name), "status", &len);
    if (!value) { assert(!actual && len == -FDT_ERR_NOTFOUND); return; }
    assert(actual && len == length && !memcmp(actual, value, len));
}
int main(void)
{
    world("disabled", 9); assert(!dt_set_t6030_dcpext(0));
    assert(validations == 1 && reservations == 1);
    expect("disabled", "okay", 5); expect("reserved", "reserved", 9);
    expect("failed", "fail", 5); expect("prefix", "disabledX", 10);
    expect("legacy", NULL, 0); expect("okay", "okay", 5);
    expect("unterminated", "disabled", 8);
    const char *refused[] = {"reserved", "fail", "disabledX", "disabled"};
    const int lengths[] = {9, 5, 10, 8};
    for (unsigned i = 0; i < 4; i++) {
        world(refused[i], lengths[i]); unsigned char before[sizeof(blob)];
        memcpy(before, blob, sizeof(blob));
        assert(dt_set_t6030_dcpext(0) < 0);
        assert(!validations && !reservations && !memcmp(before, blob, sizeof(blob)));
    }
    world("okay", 5); assert(!dt_set_t6030_dcpext(0));
    assert(!validations && !reservations);
    world(NULL, 0); assert(!dt_set_t6030_dcpext(0));
    assert(validations == 1 && reservations == 1);
    assert(dt_set_t6030_dcpext(2) < 0);
    return 0;
}
''')
