# SPDX-License-Identifier: GPL-2.0-only OR MIT
"""Exercise production T8132 handoff against both J713 calibration bundles."""
import json
from pathlib import Path
import subprocess


def test_j713_atc_usb3_bundle_and_atomic_rejection(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/kboot_atc.c").read_text()
    definitions = source[source.index("struct atc_tunable {"):source.index("struct atc_fuse_info {")]
    table = source[source.index("static const struct adt_tunable_info atc_tunables_t8132_usb3[]"):
                   source.index("static const struct adt_tunable_info atc_tunables_t8130[]")]
    start = source.index("static int dt_append_atc_tunable(")
    append = source[start:source.index("\n}", start) + 2] + "\n"
    copy = source[source.index("static void dt_copy_t8132_usb3_tunables("):
                  source.index("static void dt_copy_atc_tunables(")]
    fixture = json.loads((repo / "tests/data/j713-atc-usb3.json").read_text())
    for port, entry in enumerate(fixture["ports"]):
        declarations, properties, checks, usb_checks = [], [], [], []
        for i, (name, raw) in enumerate(entry["properties"].items()):
            data = bytes.fromhex(raw)
            declarations.append(f'static const u8 in{i}[] = {{{",".join(str(x) for x in data)}}};')
            properties.append(f'{{"{name}", in{i}, sizeof(in{i})}}')
        for i, (name, raw) in enumerate(entry["expected_be_hex"].items()):
            data = bytes.fromhex(raw)
            declarations.append(f'static const u8 out{i}[] = {{{",".join(str(x) for x in data)}}};')
            check = f'expect(dt, node, "{name}", out{i}, sizeof(out{i}));'
            checks.append(check)
            if name != "apple,tunable-usb3-dp":
                usb_checks.append(check)
        harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <libfdt.h>
typedef uint8_t u8;
typedef uint32_t u32;
#define PACKED __attribute__((packed))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
void *adt;
static const char *omit, *replace;
static const u8 *replacement;
static u32 replacement_len;
static int dp_append_fail = -1;
static int append_u32(void *dt, int node, const char *name, uint32_t value)
{
    if (!strcmp(name, "apple,tunable-usb3-dp") && dp_append_fail >= 0) {
        if (!dp_append_fail--) return -FDT_ERR_NOSPACE;
    }
    return fdt_appendprop_u32(dt, node, name, value);
}
#define fdt_appendprop_u32 append_u32
struct property { const char *name; const u8 *data; u32 len; };
''' + "\n".join(declarations) + '\nstatic const struct property props[] = {' + ",".join(properties) + r'''};
const void *adt_getprop(const void *tree, int node, const char *name, u32 *len)
{
    if (omit && !strcmp(omit, name)) return NULL;
    if (replace && !strcmp(replace, name)) {
        if (len) *len = replacement_len;
        return replacement;
    }
    for (size_t i = 0; i < ARRAY_SIZE(props); i++)
        if (!strcmp(props[i].name, name)) {
            if (len) *len = props[i].len;
            return props[i].data;
        }
    return NULL;
}
''' + definitions + table + append + copy + r'''
static void expect(void *dt, int node, const char *name, const u8 *want, int count)
{
    int len;
    const void *data = fdt_getprop(dt, node, name, &len);
    assert(data && len == count && !memcmp(data, want, count));
}
static void expect_usb(void *dt, int node)
{
''' + "\n".join(usb_checks) + r'''
}
static void expect_empty(void *dt, int node)
{
    assert(!fdt_getprop(dt, node, "apple,tunable-usb3-dp", NULL));
    for (size_t i = 0; i < ARRAY_SIZE(atc_tunables_t8132_usb3); i++)
        assert(!fdt_getprop(dt, node, atc_tunables_t8132_usb3[i].fdt_name, NULL));
}
int main(void)
{
    char dt[32768];
    assert(!fdt_create_empty_tree(dt, sizeof(dt)));
    int node = fdt_add_subnode(dt, 0, "phy");
    assert(node >= 0);
    /* No reg topology is required in the bootloader's USB2 control DT. */
    dt_copy_t8132_usb3_tunables(dt, 42, node);
''' + "\n".join(checks) + r'''
    /* AUX is an independent optional capability; stale values never survive. */
    omit = "tunable_AUX_TOP";
    dt_copy_t8132_usb3_tunables(dt, 42, node);
    expect_usb(dt, node);
    assert(!fdt_getprop(dt, node, "apple,tunable-usb3-dp", NULL));
    omit = NULL;
    replace = "tunable_AUX_TOP";
    u32 aux_bad[][3] = {
        {0x20000002, 1, 1}, /* unaligned */
        {0x20004000, 1, 1}, /* outside AUX bank */
        {0x10000000, 1, 1}, /* wrong access width */
    };
    for (size_t i = 0; i < ARRAY_SIZE(aux_bad); i++) {
        replacement = (const u8 *)aux_bad[i];
        replacement_len = sizeof(aux_bad[i]);
        dt_copy_t8132_usb3_tunables(dt, 42, node);
        expect_usb(dt, node);
        assert(!fdt_getprop(dt, node, "apple,tunable-usb3-dp", NULL));
    }
    for (unsigned len = 0; len < 12; len += 11) {
        replacement_len = len;
        dt_copy_t8132_usb3_tunables(dt, 42, node);
        expect_usb(dt, node);
        assert(!fdt_getprop(dt, node, "apple,tunable-usb3-dp", NULL));
    }
    replace = NULL;
    /* Every optional append failure clears its partial list, not USB. */
    for (unsigned fail = 0; fail < 6; fail++) {
        dp_append_fail = fail;
        dt_copy_t8132_usb3_tunables(dt, 42, node);
        expect_usb(dt, node);
        assert(!fdt_getprop(dt, node, "apple,tunable-usb3-dp", NULL));
    }
    dp_append_fail = -1;
    dt_copy_t8132_usb3_tunables(dt, 42, node);
    /* Every missing required group removes the entire old/new bundle. */
    for (size_t i = 0; i < ARRAY_SIZE(atc_tunables_t8132_usb3); i++) {
        if (!atc_tunables_t8132_usb3[i].required) continue;
        omit = atc_tunables_t8132_usb3[i].adt_name;
        dt_copy_t8132_usb3_tunables(dt, 42, node);
        expect_empty(dt, node);
        omit = NULL;
        dt_copy_t8132_usb3_tunables(dt, 42, node);
    }
    /* Late failure must roll back earlier bank groups and stale lane data. */
    replace = "tunable_LN1_TX_SHM_USB_DFLT";
    u32 bad[][3] = {
        {0x20000002, 1, 1}, /* unaligned */
        {0x20001000, 1, 1}, /* outside this source bank */
        {0x10000000, 1, 1}, /* wrong access width */
    };
    for (size_t i = 0; i < ARRAY_SIZE(bad); i++) {
        replacement = (const u8 *)bad[i];
        replacement_len = sizeof(bad[i]);
        dt_copy_t8132_usb3_tunables(dt, 42, node);
        expect_empty(dt, node);
    }
    replacement_len = 11;
    dt_copy_t8132_usb3_tunables(dt, 42, node);
    expect_empty(dt, node);
    replacement_len = 0;
    dt_copy_t8132_usb3_tunables(dt, 42, node);
    expect_empty(dt, node);
    replace = NULL;
    /* A short FDT must also leave no partially published bundle. */
    assert(!fdt_create_empty_tree(dt, 1024));
    node = fdt_add_subnode(dt, 0, "phy");
    assert(node >= 0);
    dt_copy_t8132_usb3_tunables(dt, 42, node);
    expect_empty(dt, node);
    return 0;
}
'''
        cfile = tmp_path / f"j713-atc-{port}.c"
        binary = tmp_path / f"j713-atc-{port}"
        cfile.write_text(harness)
        # Link the in-tree libfdt rather than a host-installed one.
        libfdt = repo / "src/libfdt"
        sources = [str(libfdt / name) for name in (
            "fdt.c", "fdt_ro.c", "fdt_rw.c", "fdt_sw.c", "fdt_wip.c", "fdt_empty_tree.c",
            "fdt_strerror.c",
        )]
        subprocess.run(["cc", "-D_GNU_SOURCE", "-Wall", "-Werror", "-Wno-unused-function", "-I",
                        str(libfdt), str(cfile), *sources, "-o", str(binary)], check=True)
        subprocess.run([str(binary)], check=True, capture_output=True)
