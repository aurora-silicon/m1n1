"""Exercise the production J700 ATC exporter with synthetic ADT records."""

import json
from pathlib import Path
import struct
import subprocess


FIXTURE = Path(__file__).resolve().parents[1] / "data/j700-atc-dp.json"
GROUPS = (
    "tunable_ATC0AXI2AF", "tunable_ATC_FABRIC", "tunable_ATC_COMMON_CFG",
    "tunable_AUSCMN_DIG", "tunable_AUSPLL_CORE", "tunable_AUX_TOP",
)
TRAINING = (
    "dp-training-table", "dp-training-table-rbr", "dp-training-table-hbr",
    "dp-training-table-hbr2", "dp-training-table-hbr3",
)


def test_j700_atc_dp_bytes_and_rejection(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/kboot_atc.c").read_text()
    helper = source[source.index("/* J700's DP lists"):source.index("static void dt_copy_atc_tunables")]
    fixture = json.loads(FIXTURE.read_text())
    props = {name: bytes.fromhex(raw) for name, raw in fixture["properties"].items()}
    ranges = list(struct.iter_unpack("<QQQ", bytes.fromhex(fixture["ranges_le_hex"])))
    regs = list(struct.iter_unpack("<QQ", bytes.fromhex(fixture["reg_le_hex"])))
    assert len(ranges) == 7 and len(regs) == 33
    translated = []
    for base, size in regs:
        matches = [parent + base - child for child, parent, length in ranges
                   if size and child <= base and size <= length and base - child <= length - size]
        assert len(matches) == 1
        translated.append((matches[0], size))
    names = [name for name in (*GROUPS, *TRAINING) if name in props]
    declarations = "\n".join(
        f"static const u8 input_{i}[] = {{{','.join(str(b) for b in props[name])}}};"
        for i, name in enumerate(names)
    )
    declarations += "\n" + "\n".join(
        f"static const u8 {name}[] = {{{','.join(str(b) for b in bytes.fromhex(fixture[key]))}}};"
        for name, key in (("reg_bytes", "reg_le_hex"), ("range_bytes", "ranges_le_hex"))
    )
    entries = "\n".join(
        f'    {{"{name}", input_{i}, sizeof(input_{i})}},' for i, name in enumerate(names)
    )

    expected = {name: bytes.fromhex(raw)
                for name, raw in fixture["expected_be_hex"].items()}
    checks = "\n".join(
        f'    expect("{name}", (const u8[]){{{",".join(str(b) for b in data)}}}, {len(data)});'
        for name, data in expected.items()
    )
    bank_bases = ", ".join(f"[{i}]={base}ULL" for i, (base, _) in enumerate(translated))
    bank_sizes = ", ".join(f"[{i}]={size}ULL" for i, (_, size) in enumerate(translated))
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libfdt.h"
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define T8140 0x8140
u32 chip_id = T8140;
void *adt;
static const char *omit, *replace;
static const u8 *replacement;
static u32 replacement_len;
static int bad_bank;
static const u8 *range_override;
struct property { const char *name; const u8 *data; u32 len; };
''' + declarations + r'''
static const struct property properties[] = {
''' + entries + r'''
};
const void *adt_getprop(const void *tree, int node, const char *name, u32 *len)
{
    (void)tree;
    if (node == 42 && !strcmp(name, "reg")) {
        *len = sizeof(reg_bytes); return reg_bytes;
    }
    if (node == 2 && !strcmp(name, "ranges")) {
        *len = sizeof(range_bytes); return range_override ? range_override : range_bytes;
    }
    if (omit && !strcmp(name, omit)) return NULL;
    if (replace && !strcmp(name, replace)) {
        *len = replacement_len;
        return replacement;
    }
    for (size_t i = 0; i < ARRAY_SIZE(properties); i++)
        if (!strcmp(name, properties[i].name)) {
            *len = properties[i].len;
            return properties[i].data;
        }
    return NULL;
}
int adt_path_offset(const void *tree, const char *path)
{
    (void)tree;
    return !strcmp(path, "/arm-io") ? 2 : -1;
}
int adt_get_reg(const void *tree, int *path, const char *name, int index,
                u64 *base, u64 *size)
{
    (void)tree; (void)path; (void)name;
        const u64 bases[33] = {''' + bank_bases + r'''};
        const u64 sizes[33] = {''' + bank_sizes + r'''};
    if (index < 0 || index >= 33 || !bases[index]) return -1;
    *base = bases[index] + (index == bad_bank ? 4 : 0);
    *size = sizes[index];
    return 0;
}
bool adt_is_compatible_at(const void *tree, int node, const char *name, size_t index)
{
    (void)tree; (void)node;
    return index == 0 && !strcmp(name, "atc-phy,t8130");
}
int adt_path_offset_trace(const void *tree, const char *path, int *offsets)
{
    (void)tree;
    assert(!strcmp(path, "/arm-io/atc-phy0"));
    offsets[0] = 1; offsets[1] = 2; offsets[2] = 42; offsets[3] = 0;
    return 42;
}
''' + helper + r'''
static u8 tree[8192];
static int phy;
static void init_tree(void)
{
    assert(fdt_create_empty_tree(tree, sizeof(tree)) == 0);
    assert(fdt_setprop_string(tree, 0, "compatible", "apple,j700") == 0);
    phy = fdt_add_subnode(tree, 0, "phy@40b000000");
    assert(phy >= 0);
    assert(fdt_setprop_string(tree, phy, "compatible", "apple,t8140-atcphy") == 0);
    const char names[] = "core\0lpdptx\0axi2af";
    assert(fdt_setprop(tree, phy, "reg-names", names, sizeof(names)) == 0);
    fdt64_t reg[] = {cpu_to_fdt64(0x40b000000ULL), cpu_to_fdt64(0x4c000),
        cpu_to_fdt64(0x40b050000ULL), cpu_to_fdt64(0x4000),
        cpu_to_fdt64(0x408000000ULL), cpu_to_fdt64(0x8000)};
    assert(fdt_setprop(tree, phy, "reg", reg, sizeof(reg)) == 0);
    assert(fdt_setprop_string(tree, phy, "apple,tunable-usb2phy-reg-dflt", "usb") == 0);
}
static void run(void) { dt_export_j700_atc(tree, 42, phy); }
static void expect(const char *name, const u8 *bytes, int len)
{
    int actual_len;
    const void *actual = fdt_getprop(tree, phy, name, &actual_len);
    assert(actual && actual_len == len && !memcmp(actual, bytes, len));
}
static void absent(const char *name) { assert(!fdt_getprop(tree, phy, name, NULL)); }
static void retained(void)
{
    assert(fdt_getprop(tree, phy, "apple,tunable-axi2af", NULL));
    assert(fdt_getprop(tree, phy, "apple,tunable-usb2phy-reg-dflt", NULL));
}
int main(void)
{
    init_tree(); run();
''' + checks + r'''
    absent("apple,dp-training-table-rbr");
    absent("apple,dp-training-table-hbr");

    /* Required native group absent: remove stale DP, keep independent data. */
    omit = "tunable_AUSPLL_CORE";
    run(); retained(); absent("apple,tunable-dp-common-pre");
    absent("apple,dp-training-table"); omit = NULL;

    /* Present empty group is valid; a wholly empty output remains present. */
    replace = "tunable_ATC_FABRIC"; replacement = (const u8 *)""; replacement_len = 0;
    run();
    int len = -1;
    assert(fdt_getprop(tree, phy, "apple,tunable-dp-common-pre", &len) && len == 0);
    replace = NULL;

    /* Present empty training table is invalid; optional absence is benign. */
    replace = "dp-training-table"; replacement = (const u8 *)""; replacement_len = 0;
    run(); retained(); absent("apple,tunable-dp-common-post");
    replace = NULL;

    u8 malformed[12] = {0};
    replace = "tunable_ATC_FABRIC"; replacement = malformed; replacement_len = 12;
    run(); retained(); absent("apple,tunable-dp-common-pre");
    /* A 32-bit record outside its source bank is rejected. */
    malformed[3] = 32; malformed[2] = 0x40;
    run(); retained(); absent("apple,tunable-dp-common-pre");
    replace = NULL;
    /* AXI2AF's source bank is larger than its Linux resource. */
    u8 axi_outside[12] = {0, 0x80, 0, 32};
    replace = "tunable_ATC0AXI2AF"; replacement = axi_outside; replacement_len = 12;
    run(); absent("apple,tunable-axi2af");
    assert(fdt_getprop(tree, phy, "apple,tunable-dp-common-pre", NULL));
    replace = NULL;
    bad_bank = 26; run(); retained(); absent("apple,tunable-dp-common-pre");
    bad_bank = 0;
    u8 uncovered[sizeof(range_bytes)];
    memcpy(uncovered, range_bytes, sizeof(uncovered));
    memset(uncovered + 16, 0, 8);
    range_override = uncovered;
    run(); absent("apple,tunable-axi2af");
    assert(fdt_getprop(tree, phy, "apple,tunable-dp-common-pre", NULL));
    memcpy(uncovered, range_bytes, sizeof(uncovered));
    memset(uncovered + 24 + 16, 0, 8);
    run(); absent("apple,tunable-dp-common-pre");
    assert(fdt_getprop(tree, phy, "apple,tunable-usb2phy-reg-dflt", NULL));
    range_override = NULL;
    init_tree(); run();
    assert(fdt_getprop(tree, phy, "apple,tunable-dp-common-post", NULL));
    return 0;
}
'''
    libfdt = repo / "src/libfdt"
    sources = [libfdt / name for name in (
        "fdt.c", "fdt_addresses.c", "fdt_check.c", "fdt_empty_tree.c",
        "fdt_ro.c", "fdt_rw.c", "fdt_strerror.c", "fdt_sw.c", "fdt_wip.c",
    )]
    binary = tmp_path / "j700-atc-dp"
    subprocess.run(
        ["cc", "-D_GNU_SOURCE", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(libfdt),
         "-x", "c", "-", *map(str, sources), "-o", str(binary)],
        input=harness, text=True, check=True,
    )
    subprocess.run([str(binary)], check=True)
