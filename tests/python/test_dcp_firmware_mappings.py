"""Validate static firmware maps without touching firmware-excluded records."""

import subprocess
from pathlib import Path


def test_firmware_segment_reuse(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/dcp.c").read_text()
    start = source.index("static int dcp_create_firmware_mappings(")
    end = source.index("\ndcp_dev_t *dcp_init(", start)
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint64_t u64;
typedef uint32_t u32;
#define SZ_16K 16384UL
#define BIT(n) (1UL << (n))
#define ALIGN_UP(x, a) (((x) + (a) - 1) & ~((a) - 1))
#define ADT_GETPROP(a, n, p, v) adt_getprop_copy(a, n, p, v)
struct adt_segment_ranges { u64 phys, iova, remap; u32 size, unk; };
typedef struct { const char *dcp; } display_config_t;
typedef struct { bool locked; } dart_dev_t;
typedef struct { dart_dev_t *dart_dcp; } dcp_dev_t;
static void *adt;
static struct adt_segment_ranges segment = {
    .phys = 0x10005dac000UL, .iova = 0x1030000,
    .remap = 0x8000010005dac000UL, .size = 3 * SZ_16K
};
static u64 translations[3];
static unsigned int map_calls;
static int adt_path_offset(void *tree, const char *path)
{
    (void)tree;
    assert(!strcmp(path, "/arm-io/dcp"));
    return 1;
}
static int adt_first_child_offset(void *tree, int node)
{
    (void)tree;
    assert(node == 1);
    return 2;
}
static int adt_getprop_copy(void *tree, int node, const char *prop, u64 *out)
{
    (void)tree;
    assert(node == 2 && !strcmp(prop, "asc-dram-mask"));
    *out = 1UL << 63;
    return sizeof(*out);
}
static const void *adt_getprop(void *tree, int node, const char *prop, u32 *size)
{
    (void)tree;
    assert(node == 2 && !strcmp(prop, "segment-ranges"));
    *size = sizeof(segment);
    return &segment;
}
static void *dart_translate_silent(dart_dev_t *dart, uintptr_t iova)
{
    (void)dart;
    assert(iova >= segment.phys && iova < segment.phys + segment.size);
    return (void *)translations[(iova - segment.phys) / SZ_16K];
}
static int dart_map_flags(dart_dev_t *dart, uintptr_t iova, void *backing,
                          size_t size, u32 flags)
{
    assert(iova == segment.phys && (uintptr_t)backing == segment.phys);
    assert(size == segment.size && flags == 4);
    map_calls++;
    return dart->locked ? -1 : 0;
}
''' + source[start:end] + r'''
int main(void)
{
    display_config_t cfg = {"/arm-io/dcp"};
    dart_dev_t dart = {.locked = true};
    dcp_dev_t dcp = {&dart};
    for (size_t i = 0; i < 3; i++)
        translations[i] = segment.phys + i * SZ_16K;
    assert(dcp_create_firmware_mappings(&cfg, &dcp) == 0 && map_calls == 0);
    translations[1] = 0;
    assert(dcp_create_firmware_mappings(&cfg, &dcp) == -1 && map_calls == 0);
    translations[1] = segment.phys + 2 * SZ_16K;
    assert(dcp_create_firmware_mappings(&cfg, &dcp) == -1 && map_calls == 0);
    translations[0] = 0;
    assert(dcp_create_firmware_mappings(&cfg, &dcp) == -1 && map_calls == 0);
    for (size_t i = 0; i < 3; i++)
        translations[i] = segment.phys + (i + 1) * SZ_16K;
    assert(dcp_create_firmware_mappings(&cfg, &dcp) == -1 && map_calls == 0);
    memset(translations, 0, sizeof(translations));
    segment.unk = 0xa;
    assert(dcp_create_firmware_mappings(&cfg, &dcp) == 0 && map_calls == 0);
    segment.unk = 0x2;
    assert(dcp_create_firmware_mappings(&cfg, &dcp) == 0 && map_calls == 0);
    segment.unk = 0x8;
    assert(dcp_create_firmware_mappings(&cfg, &dcp) == -1 && map_calls == 1);
    dart.locked = false;
    assert(dcp_create_firmware_mappings(&cfg, &dcp) == 1 && map_calls == 2);
    puts("full-range DCP mapping checks passed");
}
'''
    program = tmp_path / "firmware-mappings.c"
    binary = tmp_path / "firmware-mappings"
    program.write_text(harness)
    subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", str(program), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
