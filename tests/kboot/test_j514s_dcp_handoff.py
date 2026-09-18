#!/usr/bin/env python3
"""Exercise the production handoff validator with host-side ADT/FDT stubs."""
from pathlib import Path
import subprocess
import tempfile


def test_handoff():
    source = (Path(__file__).resolve().parents[2] / "src/kboot.c").read_text()
    start = source.index("static int dt_set_j514s_dcp_handoff(void)")
    end = source.index("static int dt_set_display(void)", start)
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint64_t u64;
typedef uint32_t u32;
#define T6030 0x8132
#define SZ_16K 0x4000ULL
#define ALIGN_UP(x, a) (((x) + (a) - 1) & ~((a) - 1))
#define bail(...) do { return -1; } while (0)
struct adt_segment_ranges { u64 phys, iova, remap; u32 size, flags; };
static struct adt_segment_ranges segments[2];
static u32 seglen = sizeof(segments);
static u64 dram_base = 0x10000000000ULL, dram_size = 0x400000000ULL;
/* These exclude both firmware carveouts, as on the captured J514S boot. */
static struct { u64 phys_base, mem_size; } cur_boot_args = {
    0x10002770000ULL, 0x3daa6c000ULL
};
static void *dt, *adt;
static int chip_id = T6030, mutations, missing_bounds, missing_alias;
static int fdt_path_offset(void *p, const char *path) {
    return missing_alias ? -1 : 1;
}
static int fdt_node_check_compatible(void *p, int n, const char *s) { return 0; }
static int adt_path_offset(void *p, const char *path) { return 1; }
static inline int get_bound(const char *name, u64 *value) {
    if (missing_bounds) return -1;
    *value = !strcmp(name, "dram-base") ? dram_base : dram_size;
    return 0;
}
#define ADT_GETPROP(a, n, name, dest) get_bound(name, dest)
static const void *adt_getprop(void *p, int n, const char *s, u32 *len) {
    if (!strcmp(s, "segment-ranges")) { *len = seglen; return segments; }
    *len = 37;
    return "00000000-0000-0000-0000-000000000000";
}
static int fdt_setprop_string(void *p, int n, const char *k, const char *v) {
    mutations++; return 0;
}
static const void *fdt_getprop(void *p, int n, const char *name, int *length) { return NULL; }
static int dt_set_j514s_display_maps(const struct adt_segment_ranges *s, u32 count,
                                    u64 base, u64 size) { return -1; }
static int dt_set_dcp_firmware(const char *s) { mutations++; return 0; }
static int dt_reserve_asc_firmware(const char *a, const char *b,
                                 const char *c, bool remap, u64 base) {
    assert(remap && !base); mutations++; return 0;
}
'''
    harness += source[start:end]
    harness += r'''
static void reset(void) {
    segments[0] = (struct adt_segment_ranges) {
        .phys = 0x10000298000ULL, .remap = 0x10005b8c000ULL, .size = 0x806000
    };
    segments[1] = (struct adt_segment_ranges) {
        .phys = 0x103e187c000ULL, .remap = 0x10006394000ULL, .size = 0x325000
    };
    mutations = 0;
}
static void rejected(void) {
    assert(dt_set_j514s_dcp_handoff() < 0);
    assert(!mutations);
}
int main(void) {
    reset();
    assert(segments[0].phys < cur_boot_args.phys_base);
    assert(segments[1].phys > cur_boot_args.phys_base + cur_boot_args.mem_size);
    assert(dt_set_j514s_dcp_handoff() == 0 && mutations == 3);
    reset(); segments[1].phys = dram_base + dram_size; rejected();
    reset(); segments[0].phys = dram_base - SZ_16K; rejected();
    reset(); segments[1].phys++; rejected();
    reset(); segments[1].remap++; rejected();
    reset(); segments[1].size = 0; rejected();
    reset(); segments[1].remap = UINT64_MAX - SZ_16K + 1; rejected();
    reset(); missing_bounds = 1; rejected(); missing_bounds = 0;
    reset(); dram_size = UINT64_MAX; rejected(); dram_size = 0x400000000ULL;
    reset(); seglen--; rejected(); seglen++;
    reset(); missing_alias = 1;
    assert(dt_set_j514s_dcp_handoff() == 0 && !mutations);
    puts("J514S DCP handoff validation passed");
}
'''
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        (path / "test.c").write_text(harness)
        subprocess.run(["cc", "-std=gnu11", "-Wall", "-Werror", "-Wno-parentheses",
                        str(path / "test.c"), "-o", str(path / "test")], check=True)
        subprocess.run([str(path / "test")], check=True)


if __name__ == "__main__":
    test_handoff()
