"""Actual display helpers and bounded modeset branches, not a full DCP model."""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index("\n}", source.index("{", start)) + 2] + "\n"


@pytest.fixture(scope="module")
def display_binary(tmp_path_factory):
    source = (ROOT / "src/display.c").read_text()
    dart = (ROOT / "src/dart.c").read_text()
    configure = function(source, "int display_configure(")
    retry = configure[configure.index("    int ret = display_start_dcp();"):
                      configure.index("    // connect dptx")]
    allocation = configure[configure.index("        tmp_dva = iova_alloc("):
                           configure.index("        // Swap!")]
    old_fb = configure[configure.index("        if (!display_unmap_fb(fb_dva"):
                       configure.index("        fb_size = size;")]
    wait = configure.index("    mdelay(150);")
    cleanup = configure.index("    if (tmp_dva)", wait)
    assert wait < cleanup < configure.index("    bool reinit", wait)
    assert cleanup < configure.index("        cur_boot_args.video.base = fb_pa;", wait)
    assert cleanup < configure.index("        fb_reinit();", wait)
    tail = configure[cleanup:configure.rindex("\n}")]
    harness = r'''
#include "types.h"
#include "dart.h"
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
struct params { void (*tlb_invalidate)(dart_dev_t *); };
struct dart_dev { bool failed, locked; int id; const struct params *params; };
static dart_dev_t disp, domain;
static struct { dart_dev_t *dart_disp, *dart_dcp; void *iovad_dcp; } device;
static __typeof__(device) *dcp = &device;
static unsigned page_ops[2], tlbis[2], maps[2], searches, releases, continued, reinit_calls;
static int tlbi_failure, map_failure, start_result;
static bool uncertain_map, no_iova;
static u64 fb_dva, fb_size;
static void invalidate(dart_dev_t *d)
{
    assert(!disp.failed && !domain.failed);
    tlbis[d->id]++;
    if (tlbi_failure == d->id + 1)
        d->failed = true;
}
static const struct params params = {.tlb_invalidate = invalidate};
bool dart_has_failed(dart_dev_t *d) { return !d || d->failed; }
static void dart_unmap_page(dart_dev_t *d, uintptr_t iova)
{
    assert(!disp.failed && !domain.failed);
    page_ops[d->id]++;
}
int dart_map(dart_dev_t *d, uintptr_t iova, void *pa, size_t size)
{
    assert(!disp.failed && !domain.failed);
    maps[d->id]++;
    if (map_failure == d->id + 1) {
        d->failed = uncertain_map;
        return -1;
    }
    return 0;
}
u64 dart_find_iova(dart_dev_t *d, s64 start, size_t size)
{
    searches++;
    return start;
}
u64 dart_vm_base(dart_dev_t *d) { return 0x10000000; }
static u64 iova_alloc(void *owner, size_t size) { return no_iova ? 0 : 0x8000; }
static void iova_free(void *owner, u64 iova, size_t size)
{
    assert(!disp.failed && !domain.failed);
    releases++;
}
static int display_start_dcp(void) { return start_result; }
#define FB_DEPTH_MASK 0xff
#define FB_DEPTH_FLAG_RETINA 0x100
#define T8152 0x8152
static unsigned chip_id;
static bool display_is_external;
static struct { u64 width, height; } tbest = {1920, 1080};
static struct { bool retina; } opts;
static struct {
    struct { u64 base, stride, width, height, depth; } video;
} cur_boot_args, saved_boot_args;
static uintptr_t boot_args_addr = (uintptr_t)&saved_boot_args;
static void fb_reinit(void) { reinit_calls++; }
static u64 get_ticks(void) { return 1; }
static u64 ticks_to_msecs(u64 value) { return value; }
static void reset(void)
{
    disp = (dart_dev_t){.id = 0, .params = &params};
    domain = (dart_dev_t){.id = 1, .params = &params};
    device.dart_disp = &disp;
    device.dart_dcp = &domain;
    memset(page_ops, 0, sizeof(page_ops));
    memset(tlbis, 0, sizeof(tlbis));
    memset(maps, 0, sizeof(maps));
    searches = releases = continued = reinit_calls = 0;
    tlbi_failure = map_failure = start_result = 0;
    uncertain_map = no_iova = false;
    fb_dva = 0x4000;
    fb_size = SZ_16K;
    memset(&cur_boot_args, 0, sizeof(cur_boot_args));
    memset(&saved_boot_args, 0x5a, sizeof(saved_boot_args));
}
'''
    harness += function(dart, "bool dart_unmap_checked(")
    harness += function(source, "static bool display_unmap_fb(")
    harness += function(source, "static uintptr_t display_map_fb(")
    harness += "static int retry_branch(void)\n{\n" + retry + "continued++; return 0;\n}\n"
    harness += "static int allocation_branch(void)\n{\nu64 tmp_dva, fb_pa = 0x80000, size = SZ_16K;\n"
    harness += allocation + "continued++; return 0;\n}\n"
    harness += "static int old_fb_branch(void)\n{\nu64 fb_pa = 0x80000, size = SZ_16K;\n"
    harness += old_fb + "continued++; return 0;\n}\n"
    harness += "static int completion_branch(u64 tmp_dva)\n{\nu64 fb_pa = 0x80000, size = SZ_16K;\n"
    harness += "u64 stride = 1920 * 4, start_time = 0;\n" + tail + "\n}\n"
    harness += r'''
int main(int argc, char **argv)
{
    assert(argc == 2);
    int test = atoi(argv[1]);
    if (test == 0) {
        for (int fault = 0; fault <= 10; fault++) {
            reset();
            tlbi_failure = fault <= 2 ? fault : (fault == 10 ? 1 : 0);
            disp.failed = fault == 3;
            domain.failed = fault == 4;
            disp.locked = fault == 5;
            domain.locked = fault == 6;
            u64 iova = fault == 7 ? 0x4001 : 0x4000;
            size_t size = fault >= 9 ? 0 : (fault == 8 ? SZ_16K - 1 : SZ_16K);
            assert(display_unmap_fb(iova, size) == (fault == 0 || fault == 9));
            assert(!releases && !maps[0] && !maps[1]);
            if (fault >= 9)
                assert(!page_ops[0] && !page_ops[1]);
            if (fault == 0 || fault == 2 || fault == 9)
                assert(tlbis[0] == 1 && tlbis[1] == 1);
            else if (fault == 1 || fault == 6 || fault == 10)
                assert(tlbis[0] == 1 && tlbis[1] == 0);
            else
                assert(!page_ops[0] && !page_ops[1] && !tlbis[0] && !tlbis[1]);
        }
    } else if (test == 1 || test == 2) {
        for (int fault = 0; fault <= 6; fault++) {
            reset();
            tlbi_failure = fault <= 2 ? fault : 0;
            disp.failed = fault == 3;
            domain.failed = fault == 4;
            disp.locked = fault == 5;
            domain.locked = fault == 6;
            __typeof__(saved_boot_args) before = saved_boot_args;
            int ret = test == 1 ? old_fb_branch() : completion_branch(0x8000);
            if (fault) {
                assert(ret == -1 && !continued && !releases && !reinit_calls);
                assert(!maps[0] && !maps[1] && fb_dva == 0x4000);
                assert(!cur_boot_args.video.base);
                assert(!memcmp(&saved_boot_args, &before, sizeof(before)));
            } else if (test == 1) {
                assert(ret == 0 && maps[0] == 1 && maps[1] == 1 && continued == 1);
            } else {
                assert(ret == 1 && releases == 1 && reinit_calls == 1);
                assert(cur_boot_args.video.base == 0x80000);
                assert(!memcmp(&saved_boot_args, &cur_boot_args, sizeof(cur_boot_args)));
            }
        }
    } else if (test == 3) {
        reset(); no_iova = true;
        assert(allocation_branch() == -1);
        assert(!searches && !maps[0] && !maps[1] && !continued && !releases);
        reset();
        assert(allocation_branch() == 0 && maps[0] == 1 && maps[1] == 1);
    } else if (test == 4) {
        for (int target = 1; target <= 2; target++) {
            for (int uncertain = 0; uncertain <= 1; uncertain++) {
                reset(); map_failure = target; uncertain_map = uncertain;
                assert(DART_IS_ERR(display_map_fb(0x4000, 0x80000, SZ_16K)));
                assert(maps[0] == 1 && maps[1] == (target == 2));
                assert(tlbis[0] == (target == 2 && !uncertain) && !tlbis[1]);
                assert(!releases);
            }
        }
    } else {
        for (int failed = 0; failed < 2; failed++) {
            reset();
            (failed ? &domain : &disp)->failed = true;
            assert(DART_IS_ERR(display_map_fb(0, 0x80000, SZ_16K)));
            assert(!searches && !maps[0] && !maps[1]);
            assert(retry_branch() == -1 && !continued);
        }
        reset(); start_result = -7;
        assert(retry_branch() == -7 && !continued);
        reset(); assert(retry_branch() == 0 && continued == 1);
    }
    return 0;
}
'''
    binary = tmp_path_factory.mktemp("display-lifetime") / "fixture"
    subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-parameter", "-I", str(ROOT / "src"),
                    "-x", "c", "-", "-o", str(binary)],
                   input=harness, text=True, check=True)
    return binary


@pytest.mark.parametrize("case", range(6), ids=[
    "checked-pair", "old-fb-remap", "temp-release-before-publication",
    "iova-exhaustion", "map-rollback", "failed-domain-retry",
])
def test_display_mapping_branches(display_binary, case):
    subprocess.run([str(display_binary), str(case)], check=True, timeout=5)
