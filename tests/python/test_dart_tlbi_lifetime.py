"""Production DART invalidation/map/unmap/release failure ownership."""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index("\n}", source.index("{", start)) + 2] + "\n"


@pytest.mark.parametrize("t8110", [False, True])
def test_failed_tlbi_retains_tables_buffers_and_blocks_further_operations(tmp_path, t8110):
    source = (ROOT / "src/dart.c").read_text()
    definitions = source[source.index("#define DART_T8020_STREAM_SELECT"):source.index("struct dart_params")]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include "types.h"
typedef struct dart_dev dart_dev_t;
#define dma_wmb() ((void)0)
'''
    harness += definitions
    harness += r'''
struct params { void (*tlb_invalidate)(dart_dev_t *); u64 tcr_disabled; unsigned tcr_off;
                unsigned ttbr_off; int ttbr_count; };
struct dart_dev { bool failed, locked, keep; uintptr_t regs; unsigned device;
                  const struct params *params; u64 *l1[4]; };
static int failure, writes, polls, frees, pages, unmap_pages;
static bool forbidden;
static u64 l2[SZ_16K/8];
static void write32(u64 addr, u32 value) { assert(!forbidden); writes++; }
static int poll32(u64 addr, u32 mask, u32 value, unsigned us) {
    assert(!forbidden); polls++;
    if (failure) { forbidden = true; return -1; }
    return 0;
}
static bool is_heap(const void *p) { return true; }
static u64 *dart_get_l2(dart_dev_t *d, unsigned i) { assert(!forbidden); return l2; }
static int dart_map_page(dart_dev_t *d, uintptr_t iova, uintptr_t paddr, u32 flags) {
    assert(!forbidden); pages++; return 0;
}
static void dart_unmap_page(dart_dev_t *d, uintptr_t iova) { assert(!forbidden); unmap_pages++; }
static void mock_free(void *p) { assert(!forbidden); frees++; }
#define free mock_free
void dart_unmap(dart_dev_t *, uintptr_t, size_t);
'''
    for signature in ("static void dart_t8020_tlb_invalidate(", "static void dart_t8110_tlb_invalidate(",
                      "int dart_map_flags(", "void dart_unmap(", "void dart_free_l2(",
                      "bool dart_shutdown_checked(", "bool dart_has_failed("):
        harness += function(source, signature)
    harness += r'''
int main(int argc, char **argv) {
    assert(argc == 2); int mode = atoi(argv[1]);
    u64 table[SZ_16K/8] = {DART_PTE_VALID};
    struct params params = {.tcr_off=0x1000, .ttbr_off=0x1400, .ttbr_count=1};
#ifdef TEST_T8110
    params.tlb_invalidate = dart_t8110_tlb_invalidate;
#else
    params.tlb_invalidate = dart_t8020_tlb_invalidate;
#endif
    dart_dev_t dart = {.regs=0x1000, .device=1, .params=&params, .l1={table}};
    failure = mode != 0;
    if (mode == 1) {
        assert(dart_map_flags(&dart, 0x4000, (void *)0x80000, SZ_16K*2, 0) == -1);
        assert(pages == 2 && dart_has_failed(&dart) && !frees);
    } else if (mode == 2) {
        dart_unmap(&dart, 0x4000, SZ_16K);
        assert(unmap_pages == 1 && dart_has_failed(&dart) && !frees);
    } else if (mode == 3) {
        assert(!dart_shutdown_checked(&dart));
        assert(dart_has_failed(&dart) && !frees && table[0] == DART_PTE_VALID);
    } else {
        assert(dart_map_flags(&dart, 0x4000, (void *)0x80000, SZ_16K, 0) == 0);
        assert(!dart_has_failed(&dart));
        dart_unmap(&dart, 0x4000, SZ_16K);
        assert(dart_shutdown_checked(&dart));
        assert(frees == 3 && table[0] == 0);
        return 0;
    }
    int before = writes + polls + frees + pages + unmap_pages;
    params.tlb_invalidate(&dart);
    assert(dart_map_flags(&dart, 0x4000, (void *)0x80000, SZ_16K, 0) == -1);
    dart_unmap(&dart, 0x4000, SZ_16K);
    dart_free_l2(&dart, 0);
    assert(!dart_shutdown_checked(&dart));
    assert(before == writes + polls + frees + pages + unmap_pages && !frees);
    return 0;
}
'''
    binary = tmp_path / "dart-tlbi-fixture"
    subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-parameter", "-Wno-unused-function",
                    *( ["-DTEST_T8110"] if t8110 else [] ),
                    "-I", str(ROOT / "src"), "-x", "c", "-", "-o", str(binary)],
                   input=harness, text=True, check=True)
    for case in range(4):
        subprocess.run([str(binary), str(case)], check=True)
