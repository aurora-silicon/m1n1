"""Production DART construction and preallocated-table publication failures."""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index("\n}", source.index("{", start)) + 2] + "\n"


@pytest.mark.parametrize("t8110", [False, True])
def test_partial_root_publication_and_invalidation_keep_domain_owned(tmp_path, t8110):
    source = (ROOT / "src/dart.c").read_text()
    declarations = source[source.index("#define DART_T8020_CONFIG"):source.index("static void dart_t8020_tlb_invalidate")]
    params = source[source.index("const struct dart_params dart_t8020"):
                    source.index("static dart_dev_t *dart_gen3_adopt_adt")]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "types.h"
#include "dart.h"
#define dma_wmb() ((void)0)
#define min(a,b) ((a) < (b) ? (a) : (b))
'''
    harness += declarations
    harness += r'''
static int mode, allocations, writes, polls, frees, publications, adt_calls;
static bool forbidden;
static void *adt;
struct adt_property { unsigned size; u8 value[16]; };
static struct adt_property property;
static u32 read32(u64 address) { assert(!forbidden); return 0; }
static void write32(u64 address, u32 value) {
    assert(!forbidden); writes++;
    if ((address >= 0x1000 + DART_T8020_TTBR_OFF && address < 0x1000 + DART_T8020_TTBR_OFF + 0x80) ||
        (address >= 0x1000 + DART_T8110_TTBR_OFF && address < 0x1000 + DART_T8110_TTBR_OFF + 0x80))
        publications++;
}
static void set32(u64 address, u32 value) { write32(address, value); }
static int poll32(u64 address, u32 mask, u32 value, unsigned timeout) {
    assert(!forbidden); polls++;
    if (mode == 5 || mode == 6) { forbidden = true; return -1; }
    return 0;
}
static void *memalign(size_t align, size_t size) {
    assert(!forbidden); allocations++;
    if (mode >= 1 && mode <= 4 && allocations == mode) { forbidden = true; return NULL; }
    void *p = aligned_alloc(align, size); assert(p); return p;
}
static void mock_free(void *p) { assert(!forbidden); frees++; }
#define free mock_free
static void dart_gen3_tlb_invalidate(dart_dev_t *d) { assert(0); }
static int adt_path_offset(void *a, const char *p) { assert(!forbidden); adt_calls++; return 1; }
static const struct adt_property *adt_get_property(void *a, int n, const char *p) {
    assert(!forbidden); adt_calls++; return &property;
}
static int adt_setprop(void *a, int n, const char *p, const void *d, size_t len) {
    assert(!forbidden); adt_calls++; return 0;
}
'''
    harness += function(source, "static void dart_t8020_tlb_invalidate(")
    harness += function(source, "static void dart_t8110_tlb_invalidate(")
    harness += params
    harness += function(source, "dart_dev_t *dart_init(")
    harness += function(source, "int dart_setup_pt_region(")
    harness += function(source, "bool dart_has_failed(")
    harness += r'''
int main(int argc, char **argv) {
    assert(argc == 2); mode = atoi(argv[1]);
#ifdef TEST_T8110
    enum dart_type_t type = DART_T8110;
#else
    enum dart_type_t type = DART_T8020;
#endif
    if (mode == 6) {
        u64 table[SZ_16K/8] = {0};
        struct dart_params config = type == DART_T8110 ? dart_t8110 : dart_t8020;
        dart_dev_t dart = {.regs=0x1000, .device=1, .params=&config, .l1={table}};
        void *region = aligned_alloc(SZ_16K, SZ_16K*3); assert(region);
        u64 bounds[2] = {(uintptr_t)region, (uintptr_t)region + SZ_16K*3};
        property.size = 16; memcpy(property.value, bounds, 16);
        assert(dart_setup_pt_region(&dart, "/fixture", 1, 0) == -1);
        assert(dart_has_failed(&dart) && table[0] && table[1] && !frees);
        int before = adt_calls + writes + polls;
        assert(dart_setup_pt_region(&dart, "/fixture", 1, 0) == -1);
        assert(before == adt_calls + writes + polls);
        return 0;
    }
    dart_dev_t *dart = dart_init(0x1000, 1, false, type);
    assert(dart && !frees);
    if (!mode) {
        assert(!dart_has_failed(dart) && polls == 1);
        assert(publications == dart->params->ttbr_count);
    } else {
        assert(dart_has_failed(dart));
        if (mode <= 4) assert(publications == mode - 1 && allocations == mode && !polls);
        if (mode == 5) assert(publications == dart->params->ttbr_count && polls == 1);
        int before = writes + polls + allocations;
        assert(dart_setup_pt_region(dart, "/fixture", 1, 0) == -1);
        assert(before == writes + polls + allocations && !frees);
    }
    return 0;
}
'''
    binary = tmp_path / "dart-constructor-fixture"
    subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-function", "-Wno-unused-parameter", "-Wno-unused-const-variable",
                    *( ["-DTEST_T8110"] if t8110 else [] ),
                    "-I", str(ROOT / "src"), "-x", "c", "-", "-o", str(binary)],
                   input=harness, text=True, check=True)
    cases = (0, 1, 5, 6) if t8110 else range(7)
    for case in cases:
        subprocess.run([str(binary), str(case)], check=True)
