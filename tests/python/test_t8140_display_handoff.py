"""Check the T8140 framebuffer reservation boundary with a synthetic FDT/ADT."""

from pathlib import Path
import subprocess


def test_t8140_display_reservation_rolls_back(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/kboot.c").read_text()
    start = source.index("static int dt_set_display_t8140(void)")
    end = source.index("static int dt_set_display(void)", start)
    helper = source[start:end]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libfdt.h"
typedef uint64_t u64;
typedef uint32_t u32;
#define SZ_16K 16384
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define ADT_GETPROP_ARRAY(a, n, p, v) adt_array((n), (p), (v), sizeof(v))
struct adt_segment_ranges { u64 phys, iova, remap; uint32_t size, unk; };
static unsigned char tree[16384];
static void *dt = tree;
static int dt_bufsize = sizeof(tree);
static void *adt;
static struct {
    u64 phys_base, mem_size;
    struct { u64 base, stride, height; } video;
} cur_boot_args;
static u64 vram_base = 0x110000000, vram_size = 0x200000;
static int mapping_ok = 1, reserve_calls, bad_carveout, bad_segment;

static int adt_path_offset_trace(void *a, const char *path, int *trace)
{
    (void)a;
    if (strcmp(path, "/vram")) return -1;
    trace[0] = 1; trace[1] = 2; trace[2] = 0;
    return 2;
}
static int adt_get_reg(void *a, int *trace, const char *prop, int index, u64 *base, u64 *size)
{
    (void)a; (void)trace;
    assert(!strcmp(prop, "reg") && index == 0);
    *base = vram_base; *size = vram_size;
    return 0;
}
static int adt_path_offset(void *a, const char *path)
{
    (void)a;
    if (!strcmp(path, "/chosen/carveout-memory-map")) return 3;
    if (!strcmp(path, "/arm-io/dcp/iop-dcp-nub")) return 4;
    return -1;
}
static int adt_array(int node, const char *prop, void *out, size_t len)
{
    if (node != 3 || strcmp(prop, "region-id-14") || len != 16) return -1;
    u64 pair[2] = {vram_base, bad_carveout ? vram_size / 2 : vram_size};
    memcpy(out, pair, len);
    return (int)len;
}
static const void *adt_getprop(void *a, int node, const char *prop, uint32_t *len)
{
    (void)a;
    static struct adt_segment_ranges seg[2];
    if (node != 4 || strcmp(prop, "segment-ranges")) return NULL;
    seg[0].phys = vram_base;
    seg[0].size = bad_segment ? vram_size / 2 : vram_size;
    seg[1].phys = vram_base + vram_size;
    seg[1].size = 0x4000;
    *len = sizeof(seg);
    return seg;
}
static int dt_vram_reserved_region(const char *dcp, const char *disp)
{
    assert(!strcmp(dcp, "dcp") && !strcmp(disp, "disp0"));
    reserve_calls++;
    int parent = fdt_path_offset(dt, "/reserved-memory");
    int node = fdt_add_subnode(dt, parent, "framebuffer@110000000");
    assert(node >= 0);
    node = fdt_path_offset(dt, "/reserved-memory/framebuffer@110000000");
    assert(fdt_setprop_empty(dt, node, "no-map") == 0);
    return mapping_ok ? 0 : -1;
}
''' + helper + r'''
static void setup(void)
{
    memset(tree, 0, sizeof(tree));
    assert(fdt_create_empty_tree(dt, sizeof(tree)) == 0);
    assert(fdt_add_subnode(dt, 0, "aliases") >= 0);
    assert(fdt_add_subnode(dt, 0, "chosen") >= 0);
    assert(fdt_setprop_u32(dt, fdt_path_offset(dt, "/chosen"),
                           "apple,dcp-rtkit-quiesced", 1) == 0);
    assert(fdt_add_subnode(dt, 0, "reserved-memory") >= 0);
    assert(fdt_add_subnode(dt, 0, "soc") >= 0);
    for (int i = 0; i < 4; i++) {
        const char *names[] = {"dcp", "disp0", "dcpext", "dispext0"};
        int soc = fdt_path_offset(dt, "/soc");
        assert(fdt_add_subnode(dt, soc, names[i]) >= 0);
        char path[64];
        snprintf(path, sizeof(path), "/soc/%s", names[i]);
        assert(fdt_setprop_string(dt, fdt_path_offset(dt, "/aliases"), names[i], path) == 0);
        assert(fdt_setprop_string(dt, fdt_path_offset(dt, path), "status", "okay") == 0);
    }
    assert(fdt_add_subnode(dt, fdt_path_offset(dt, "/chosen"), "framebuffer") >= 0);
    cur_boot_args.phys_base = 0x100000000;
    cur_boot_args.mem_size = 0x20000000;
    cur_boot_args.video.base = vram_base;
    cur_boot_args.video.stride = 4096;
    cur_boot_args.video.height = 256;
    mapping_ok = 1;
    reserve_calls = 0;
    bad_carveout = bad_segment = 0;
}
static void check_fallback(int has_reservation)
{
    const char *names[] = {"dcp", "disp0", "dcpext", "dispext0"};
    for (int i = 0; i < 4; i++) {
        char path[64];
        snprintf(path, sizeof(path), "/soc/%s", names[i]);
        const char *s = fdt_getprop(dt, fdt_path_offset(dt, path), "status", NULL);
        assert(s && !strcmp(s, "disabled"));
    }
    assert(fdt_path_offset(dt, "/chosen/framebuffer") >= 0);
    assert(fdt_getprop(dt, fdt_path_offset(dt, "/chosen"),
                       "apple,dcp-rtkit-quiesced", NULL) == NULL);
    assert((fdt_path_offset(dt, "/reserved-memory/framebuffer@110000000") >= 0) == has_reservation);
    assert(fdt_check_header(dt) == 0);
}
int main(void)
{
    setup();
    assert(dt_set_display_t8140() == 0);
    assert(reserve_calls == 1);
    check_fallback(1);

    setup();
    mapping_ok = 0;
    assert(dt_set_display_t8140() == 0);
    check_fallback(0);

    setup();
    cur_boot_args.video.base = vram_base + vram_size - 4096;
    assert(dt_set_display_t8140() == 0 && reserve_calls == 0);
    check_fallback(0);

    setup();
    vram_size = 0x200001;
    assert(dt_set_display_t8140() == 0 && reserve_calls == 0);
    check_fallback(0);

    vram_size = 0x200000;
    setup();
    bad_carveout = 1;
    assert(dt_set_display_t8140() == 0 && reserve_calls == 0);
    check_fallback(0);

    setup();
    bad_segment = 1;
    assert(dt_set_display_t8140() == 0 && reserve_calls == 0);
    check_fallback(0);
    return 0;
}
'''
    binary = tmp_path / "t8140-display-handoff"
    libfdt = repo / "src/libfdt"
    sources = [libfdt / name for name in (
        "fdt.c", "fdt_addresses.c", "fdt_check.c", "fdt_empty_tree.c",
        "fdt_ro.c", "fdt_rw.c", "fdt_strerror.c", "fdt_sw.c", "fdt_wip.c",
    )]
    subprocess.run(
        ["cc", "-D_GNU_SOURCE", "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(libfdt),
         "-x", "c", "-", *map(str, sources), "-o", str(binary)],
        input=harness, text=True, check=True,
    )
    subprocess.run([str(binary)], check=True)
