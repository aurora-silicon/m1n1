# SPDX-License-Identifier: MIT
"""Test the runtime PCIe-C DART handoff with synthetic ADT and real libfdt."""
from pathlib import Path
import subprocess


def extract(source, name):
    start = source.index('static int ' + name + '(')
    end = source.index('{', start) + 1
    depth = 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def test_pciec_dart_tunables(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / 'src/kboot.c').read_text()
    structs = source[source.index('struct adt_tunable_info {'):source.index('static const struct adt_tunable_info usb4_nhi_tunables')]
    acio = source[source.index('struct acio_tunable {'):source.index('static_assert(sizeof(struct acio_tunable)')]
    code = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "libfdt.h"
typedef uint32_t u32;
typedef uint64_t u64;
#define PACKED __attribute__((packed))
static void *dt, *adt;
''' + structs + acio + r'''
/* Synthetic values, unrelated to hardware calibration. */
static struct acio_tunable values[] = {
    { .offset = 0x100, .size = 4, .mask = 0xff, .value = 0x42 },
    { .offset = 0x104, .size = 4, .mask = 0xffff, .value = 0x1234 },
};
static bool have_adt = true;
static u32 input_len = sizeof(values);
static int adt_path_offset(void *tree, const char *path)
{
    (void)tree;
    assert(!strcmp(path, "/arm-io/dart-apciec0"));
    return have_adt ? 1 : -1;
}
static const void *adt_getprop(void *tree, int node, const char *name, u32 *len)
{
    (void)tree;
    assert(node == 1 && !strcmp(name, "dart-tunables-instance-0"));
    *len = input_len;
    return values;
}
''' + extract(source, 'dt_append_acio_tunable') + '\n' + extract(source, 'dt_copy_pciec_dart_tunables') + r'''
static int node(const char *path) { int n = fdt_path_offset(dt, path); assert(n >= 0); return n; }
static void reset(void)
{
    static unsigned char blob[8192];
    dt = blob;
    assert(!fdt_create_empty_tree(dt, sizeof(blob)));
    assert(fdt_add_subnode(dt, 0, "aliases") >= 0);
    assert(fdt_add_subnode(dt, 0, "pcie") >= 0);
    assert(fdt_add_subnode(dt, 0, "iommu") >= 0);
    assert(!fdt_setprop_string(dt, node("/aliases"), "usb4_0_pcie", "/pcie"));
    assert(!fdt_setprop_string(dt, node("/iommu"), "compatible", "apple,t8110-dart"));
    assert(!fdt_setprop_u32(dt, node("/iommu"), "phandle", 42));
    fdt32_t map[] = { cpu_to_fdt32(0), cpu_to_fdt32(42), cpu_to_fdt32(0), cpu_to_fdt32(64) };
    assert(!fdt_setprop(dt, node("/pcie"), "iommu-map", map, sizeof(map)));
    have_adt = true;
    input_len = sizeof(values);
    values[1].size = 4;
    values[1].offset = 0x104;
    values[1].mask = 0xffff;
}
static int copy(void) { return dt_copy_pciec_dart_tunables("/arm-io/dart-apciec0", "usb4_0_pcie"); }
static void absent(void) { assert(!fdt_getprop(dt, node("/iommu"), "apple,tunable", NULL)); }
int main(void)
{
    reset();
    assert(!copy());
    int len;
    const fdt32_t *out = fdt_getprop(dt, node("/iommu"), "apple,tunable", &len);
    assert(out && len == 24);
    assert(fdt32_to_cpu(out[0]) == 0x100 && fdt32_to_cpu(out[1]) == 0xff && fdt32_to_cpu(out[2]) == 0x42);
    assert(fdt32_to_cpu(out[3]) == 0x104 && fdt32_to_cpu(out[5]) == 0x1234);
    assert(!copy()); /* Existing DT values are not duplicated. */
    assert(fdt_getprop(dt, node("/iommu"), "apple,tunable", &len) && len == 24);
    have_adt = false;
    assert(!copy()); /* Existing board values do not require a new ADT copy. */
    reset();
    have_adt = false;
    assert(copy() < 0); absent();
    reset();
    assert(!fdt_setprop_string(dt, node("/iommu"), "compatible", "apple,t8103-dart"));
    assert(copy() < 0); absent(); /* Original M1's lock ordering stays unchanged. */
    reset();
    assert(!fdt_setprop_u32(dt, node("/pcie"), "iommu-map", 0));
    assert(copy() < 0); absent();
    reset();
    values[1].size = 8;
    assert(copy() < 0); absent(); /* Roll back a prefix appended before failure. */
    reset();
    values[1].offset = 0x4000;
    assert(copy() < 0); absent();
    reset();
    values[1].mask = 1ULL << 32;
    assert(copy() < 0); absent();
    reset();
    input_len--;
    assert(copy() < 0); absent();
    reset();
    input_len = 0;
    assert(copy() < 0); absent();
    puts("PASS: PCIe-C DART runtime copy, preservation, validation and rollback");
    return 0;
}
'''
    harness = tmp_path / 'pciec-dart.c'
    harness.write_text(code)
    binary = tmp_path / 'pciec-dart'
    lib = repo / 'src/libfdt'
    files = ['fdt.c', 'fdt_ro.c', 'fdt_rw.c', 'fdt_wip.c', 'fdt_sw.c', 'fdt_empty_tree.c', 'fdt_addresses.c']
    subprocess.run(['cc', '-std=gnu11', '-Wall', '-Werror', '-Wno-sign-compare', '-I', str(lib), str(harness),
                    *[str(lib / f) for f in files], '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
