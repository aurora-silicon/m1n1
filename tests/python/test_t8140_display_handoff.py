"""Exercise the T8140 hand-off against measured synthetic DART translations."""

from pathlib import Path
import subprocess


def test_t8140_display_handoff(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/kboot.c").read_text()
    start = source.index("struct dt_t8140_dram {")
    end = source.index("static int dt_set_display(void)", start)
    helper = source[start:end]
    asc_source = (repo / "src/asc.c").read_text()
    asc_defs = asc_source[asc_source.index("#define ASC_CPU_CONTROL"):asc_source.index("struct asc_dev {")]
    asc_check = asc_source[asc_source.index("static bool asc_mailbox_empty("):asc_source.index("bool asc_can_recv(")]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "libfdt.h"
typedef unsigned long u64;
typedef uint32_t u32;
#define BIT(n) (1U << (n))
#define GENMASK(msb, lsb) ((BIT((msb) + 1 - (lsb)) - 1) << (lsb))
#define SZ_16K 16384
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define ADT_GETPROP_ARRAY(a, n, p, v) adt_array((n), (p), (v), sizeof(v))
#define ADT_GETPROP(a, n, p, v) adt_getprop_copy((a), (n), (p), (v), sizeof(*(v)))
struct adt_segment_ranges { u64 phys, iova, remap; u32 size, flags; };
struct disp_mapping {
    char region_adt[24], mem_fdt[24];
    bool map_dcp, map_disp, map_piodma;
};
struct mem_region { u64 paddr, size; };
struct run { u64 iova, size, pa; };
''' + asc_defs + r'''
typedef struct asc_dev { uintptr_t cpu_base, base; } asc_dev_t;
struct asc_mock {
    u32 control, status, a2i, i2a;
    bool stop_on_recheck;
    unsigned int cpu_reads;
};
static struct asc_mock asc_state[2];
static asc_dev_t asc_devs[2] = {{0x1000, 0x9000}, {0x3000, 0xb000}};
static unsigned int polls;
static u64 timeout_calculate(u32 usec) { assert(usec == 200000); polls = 0; return 3; }
static bool timeout_expired(u64 deadline) { return ++polls >= deadline; }
static u32 read32(uintptr_t addr)
{
    unsigned int idx = ((addr >= 0x3000 && addr < 0x4000) || addr >= 0xb000) ? 1 : 0;
    struct asc_mock *state = &asc_state[idx];
    uintptr_t cpu = asc_devs[idx].cpu_base, box = asc_devs[idx].base;
    if (addr == cpu + ASC_CPU_CONTROL) {
        state->cpu_reads++;
        return state->stop_on_recheck && state->cpu_reads > 1 ? 0 : state->control;
    }
    if (addr == cpu + ASC_CPU_STATUS) return state->status;
    if (addr == box + ASC_MBOX_A2I_CONTROL) return state->a2i;
    if (addr == box + ASC_MBOX_I2A_CONTROL) return state->i2a;
    abort();
}
static asc_dev_t *asc_init(const char *path)
{
    if (!strcmp(path, "/arm-io/dcp")) return &asc_devs[0];
    if (!strcmp(path, "/arm-io/dcpext")) return &asc_devs[1];
    return NULL;
}
static void asc_free(asc_dev_t *asc) { (void)asc; }
''' + asc_check + r'''
static const struct run dcp[] = {
    {0x11c4000,0xc00000,0x101f0cd4000}, {0x1ddc000,0x4000,0x101f0cd0000},
    {0x1de0000,0xc0000,0x101f0c10000}, {0x1ea0000,0x160000,0x101eb4b8000},
    {0x2000000,0x2000000,0x101eb618000}, {0x4000000,0x2000000,0x101ed618000},
    {0x6000000,0x15f8000,0x101ef618000}, {0x75f8000,0x4d8000,0x10000234000},
    {0x7ad0000,0x530000,0x101ea7d4000}, {0x8000000,0x784000,0x101ead04000},
    {0x8784000,0x30000,0x101ea7a4000}, {0x87b4000,0x8000,0x101eb488000},
};
static const struct run disp[] = {
    {0x1ddc000,0x4000,0x101f0cd0000}, {0x1ea0000,0x160000,0x101eb4b8000},
    {0x2000000,0x2000000,0x101eb618000}, {0x4000000,0x2000000,0x101ed618000},
    {0x6000000,0x15f8000,0x101ef618000},
};
static const struct run ext[] = {
    {0x75f8000,0x4d8000,0x10000234000}, {0x7ad0000,0x530000,0x101e9ae8000},
    {0x8000000,0x784000,0x101ea018000}, {0x8784000,0x20000,0x101e9ac8000},
    {0x87a4000,0x8000,0x101ea79c000}, {0x87ac000,0xc00000,0x101e8ec8000},
};
static struct { const char *id; u64 pa, size; } carveouts[] = {
    {"region-id-14",0x101eb4b8000,0x5758000},
    {"region-id-49",0x10000234000,0x4d8000},
    {"region-id-50",0x101ea7d4000,0xcbc000},
    {"region-id-57",0x101f0cd4000,0xc00000},
    {"region-id-73",0x101e9ae8000,0xcbc000},
    {"region-id-74",0x101e8ec8000,0xc00000},
    {"region-id-94",0x101f0cd0000,0x4000},
    {"region-id-95",0x101f0c10000,0xc0000},
    {"region-id-233",0x101ea7a4000,0x30000},
    {"region-id-234",0x101e9ac8000,0x20000},
};
static unsigned char tree[65536];
static void *dt = tree, *adt;
static int dt_bufsize = sizeof(tree);
static struct {
    u64 phys_base, mem_size;
    struct { u64 base, stride, height; } video;
} cur_boot_args;
static u64 physical_base = 0x10000000000, physical_size = 0x200000000;
static u64 mem_size_actual = 0x200000000;
static bool hole_dcp_vram, hole_dcp_firmware, hole_ext, bad_carveout, bad_segment;
static int reservation_calls;

static u64 translate(const char *device, u64 iova)
{
    const struct run *runs;
    size_t count;
    if (!strcmp(device, "disp0")) { runs = disp; count = ARRAY_SIZE(disp); }
    else if (strstr(device, "ext")) { runs = ext; count = ARRAY_SIZE(ext); }
    else { runs = dcp; count = ARRAY_SIZE(dcp); }
    if (hole_dcp_vram && runs == dcp && iova == 0x4000000) return 0;
    if (hole_dcp_firmware && runs == dcp && iova == 0x11c4000) return 0;
    if (hole_ext && runs == ext && iova == 0x87ac000) return 0;
    for (size_t i = 0; i < count; i++)
        if (iova >= runs[i].iova && iova - runs[i].iova < runs[i].size)
            return runs[i].pa + iova - runs[i].iova;
    return 0;
}
static u64 search_range(const char *device, u64 pa, u64 size)
{
    const struct run *runs;
    size_t count;
    if (!strcmp(device, "disp0")) { runs = disp; count = ARRAY_SIZE(disp); }
    else if (strstr(device, "ext")) { runs = ext; count = ARRAY_SIZE(ext); }
    else { runs = dcp; count = ARRAY_SIZE(dcp); }
    for (size_t i = 0; i < count; i++) {
        if (pa < runs[i].pa || pa - runs[i].pa >= runs[i].size) continue;
        u64 iova = runs[i].iova + pa - runs[i].pa;
        bool complete = true;
        for (u64 off = 0; off < size; off += SZ_16K)
            if (translate(device, iova + off) != pa + off) complete = false;
        if (complete) return iova;
    }
    return UINT64_MAX;
}
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
    *base = carveouts[0].pa; *size = carveouts[0].size;
    return 0;
}
static int adt_path_offset(void *a, const char *path)
{
    (void)a;
    if (!strcmp(path, "/chosen/carveout-memory-map")) return 3;
    if (!strcmp(path, "/arm-io/dcp/iop-dcp-nub")) return 4;
    if (!strcmp(path, "/arm-io/dcpext/iop-dcpext-nub")) return 5;
    if (!strcmp(path, "/chosen")) return 6;
    return -1;
}
static int adt_getprop_copy(void *a, int node, const char *prop, void *out, size_t len)
{
    (void)a;
    if (node != 6 || len != sizeof(u64)) return -1;
    u64 value;
    if (!strcmp(prop, "dram-base")) value = physical_base;
    else if (!strcmp(prop, "dram-size")) value = physical_size;
    else return -1;
    memcpy(out, &value, len);
    return len;
}
static int adt_array(int node, const char *prop, void *out, size_t len)
{
    if (node != 3 || len != 16) return -1;
    for (size_t i = 0; i < ARRAY_SIZE(carveouts); i++) {
        if (strcmp(prop, carveouts[i].id)) continue;
        u64 pair[2] = {carveouts[i].pa, carveouts[i].size};
        if (bad_carveout && !strcmp(prop, "region-id-14")) pair[1] /= 2;
        memcpy(out, pair, len);
        return len;
    }
    return -1;
}
static const void *adt_getprop(void *a, int node, const char *prop, u32 *len)
{
    (void)a;
    static struct adt_segment_ranges seg[7];
    if ((node != 4 && node != 5) || strcmp(prop, "segment-ranges")) return NULL;
    memset(seg, 0, sizeof(seg));
    seg[0].phys = node == 4 ? carveouts[8].pa : carveouts[9].pa;
    seg[0].size = node == 4 ? carveouts[8].size : carveouts[9].size;
    seg[1].phys = node == 4 ? 0x101eb488000 : 0x101ea79c000;
    seg[1].size = bad_segment ? 0x4000 : 0x8000;
    if (node == 4) {
        seg[6].phys = carveouts[0].pa; seg[6].size = carveouts[0].size;
        *len = sizeof(seg);
    } else *len = 4 * sizeof(*seg);
    return seg;
}
static int dt_add_reserved_regions(const char *dcp_path, const char *disp_path,
                                   const char *piodma, const char *compat,
                                   struct disp_mapping *maps, struct mem_region *regions,
                                   u32 count)
{
    (void)piodma;
    assert(dcp_path && !strcmp(compat, "apple,asc-mem"));
    reservation_calls++;
    for (u32 i = 0; i < count; i++) {
        if (search_range(dcp_path, regions[i].paddr, regions[i].size) == UINT64_MAX ||
            (maps[i].map_disp && (!disp_path ||
             search_range(disp_path, regions[i].paddr, regions[i].size) == UINT64_MAX)))
            return -1;
        char name[64];
        snprintf(name, sizeof(name), "%s@%lx", maps[i].mem_fdt, (unsigned long)regions[i].paddr);
        int parent = fdt_path_offset(dt, "/reserved-memory");
        int n = fdt_subnode_offset(dt, parent, name);
        if (n < 0) n = fdt_add_subnode(dt, parent, name);
        assert(n >= 0);
        u64 reg[2] = {cpu_to_fdt64(regions[i].paddr), cpu_to_fdt64(regions[i].size)};
        assert(fdt_setprop(dt, n, "reg", reg, sizeof(reg)) == 0);
        assert(fdt_setprop_string(dt, n, "compatible", compat) == 0);
        assert(fdt_setprop_empty(dt, n, "no-map") == 0);
    }
    return 0;
}
static int dt_vram_reserved_region(const char *dcp_path, const char *disp_path)
{
    assert(!strcmp(dcp_path, "dcp") && !strcmp(disp_path, "disp0"));
    if (search_range(dcp_path, carveouts[0].pa, carveouts[0].size) == UINT64_MAX ||
        search_range(disp_path, carveouts[0].pa, carveouts[0].size) == UINT64_MAX)
        return -1;
    struct disp_mapping map = {0};
    struct mem_region region = {carveouts[0].pa, carveouts[0].size};
    strcpy(map.mem_fdt, "framebuffer");
    map.map_dcp = map.map_disp = true;
    reservation_calls++;
    int n = fdt_add_subnode(dt, fdt_path_offset(dt, "/reserved-memory"),
                            "framebuffer@101eb4b8000");
    assert(n >= 0);
    u64 reg[2] = {cpu_to_fdt64(region.paddr), cpu_to_fdt64(region.size)};
    assert(fdt_setprop(dt, n, "reg", reg, sizeof(reg)) == 0);
    assert(fdt_setprop_string(dt, n, "compatible", "framebuffer") == 0);
    return 0;
}
static int dt_set_dcp_firmware(const char *path)
{
    (void)path;
    return 0;
}
static int dt_get_iommu_node(int node, u32 num)
{
    assert(num == 0);
    return node;
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
        if (i == 0 || i == 2)
            assert(fdt_setprop_string(dt, fdt_path_offset(dt, path), "compatible",
                   i == 0 ? "apple,t8140-dcp" : "apple,t8140-dcpext") == 0);
        if (i == 1)
            assert(fdt_setprop_string(dt, fdt_path_offset(dt, path), "compatible",
                                      "apple,display-subsystem") == 0);
        assert(fdt_setprop_string(dt, fdt_path_offset(dt, path), "status", "okay") == 0);
    }
    assert(fdt_add_subnode(dt, fdt_path_offset(dt, "/chosen"), "framebuffer") >= 0);
    cur_boot_args.phys_base = 0x1000342c000;
    cur_boot_args.mem_size = 0x1e39e8000;
    physical_base = 0x10000000000;
    physical_size = 0x200000000;
    carveouts[3].pa = 0x101f0cd4000;
    carveouts[5].pa = 0x101e8ec8000;
    assert(cur_boot_args.phys_base + cur_boot_args.mem_size < carveouts[0].pa);
    cur_boot_args.video.base = carveouts[0].pa;
    /* The h-499 boot framebuffer is a sub-range of the /vram carveout. */
    cur_boot_args.video.stride = 0x1788;
    cur_boot_args.video.height = 2416;
    assert(cur_boot_args.video.stride * cur_boot_args.video.height == 0xde1380);
    reservation_calls = 0;
    hole_dcp_vram = hole_dcp_firmware = hole_ext = false;
    bad_carveout = bad_segment = false;
    for (size_t i = 0; i < ARRAY_SIZE(asc_state); i++) {
        asc_state[i] = (struct asc_mock){
            .control = 0x10, .status = 0x6d, .a2i = 0x2ff01, .i2a = 0x28801,
        };
    }
}
static void check_claim(bool present)
{
    int len = 0;
    const u32 *cell = fdt_getprop(dt, fdt_path_offset(dt, "/chosen"),
                                  "apple,dcp-rtkit-quiesced", &len);
    if (present) assert(cell && len == sizeof(u32) && fdt32_to_cpu(*cell) == 1);
    else assert(cell == NULL);
}
static void check_status(const char *name, const char *want)
{
    char path[64];
    snprintf(path, sizeof(path), "/soc/%s", name);
    const char *status = fdt_getprop(dt, fdt_path_offset(dt, path), "status", NULL);
    assert(status && !strcmp(status, want));
}
static void check_fallback(const unsigned char *before)
{
    /* Refusal must leave the entire FDT exactly as j700-full-2 did. */
    assert(memcmp(dt, before, sizeof(tree)) == 0);
    check_status("dcp", "okay");
    check_status("disp0", "okay");
    check_status("dcpext", "okay");
    check_status("dispext0", "okay");
    assert(fdt_path_offset(dt, "/chosen/framebuffer") >= 0);
    assert(fdt_path_offset(dt, "/reserved-memory/framebuffer@101eb4b8000") < 0);
    assert(fdt_check_header(dt) == 0);
}
int main(void)
{
    unsigned char before[sizeof(tree)];
    setup();
    assert(dt_set_display_t8140() == 0);
    check_status("dcp", "okay");
    check_status("disp0", "okay");
    check_status("dcpext", "okay");
    check_status("dispext0", "disabled");
    assert(reservation_calls == 11);
    assert(fdt_path_offset(dt, "/reserved-memory/framebuffer@101eb4b8000") >= 0);
    assert(fdt_path_offset(dt, "/reserved-memory/dcp_data_tail@101eb488000") >= 0);
    assert(fdt_path_offset(dt, "/reserved-memory/dcpext0_data_tail@101ea79c000") >= 0);
    assert(fdt_path_offset(dt, "/chosen/framebuffer") >= 0);
    check_claim(true);

    setup();
    physical_size = carveouts[0].pa - physical_base + carveouts[0].size - SZ_16K;
    memcpy(before, dt, sizeof(tree));
    assert(dt_set_display_t8140() == 0);
    assert(reservation_calls == 0);
    check_fallback(before);

    setup();
    assert(fdt_delprop(dt, fdt_path_offset(dt, "/chosen"),
                       "apple,dcp-rtkit-quiesced") == 0);
    asc_state[0].control = 0;
    memcpy(before, dt, sizeof(tree));
    assert(dt_set_display_t8140() == 0);
    check_fallback(before);
    check_claim(false);

    for (int fault = 0; fault < 9; fault++) {
        setup();
        assert(fdt_delprop(dt, fdt_path_offset(dt, "/chosen"),
                           "apple,dcp-rtkit-quiesced") == 0);
        switch (fault) {
        case 0: asc_state[0].status |= BIT(1); break;
        case 1: asc_state[0].a2i |= BIT(16); break;
        case 2: asc_state[0].i2a |= BIT(18); break;
        case 3: asc_state[0].a2i &= ~BIT(17); break;
        case 4: asc_state[0].i2a |= BIT(20); break;
        case 5: asc_state[0].a2i ^= BIT(8); break;
        case 6: asc_state[0].i2a &= ~BIT(0); break;
        case 7: asc_state[0].stop_on_recheck = true; break;
        case 8: asc_state[0].i2a |= BIT(16); break;
        }
        memcpy(before, dt, sizeof(tree));
        assert(dt_set_display_t8140() == 0);
        check_fallback(before);
        check_claim(false);
    }

    setup();
    asc_state[1].i2a |= BIT(18);
    assert(dt_set_display_t8140() == 0);
    check_status("dcpext", "disabled");
    check_claim(true);
    assert(fdt_path_offset(dt, "/reserved-memory/dcpext0_data_tail@101ea79c000") < 0);

    setup();
    carveouts[3].pa = physical_base + physical_size;
    memcpy(before, dt, sizeof(tree));
    assert(dt_set_display_t8140() == 0);
    check_fallback(before);

    setup();
    carveouts[5].pa = physical_base + physical_size;
    assert(dt_set_display_t8140() == 0);
    check_status("dcp", "okay");
    check_status("disp0", "okay");
    check_status("dcpext", "disabled");
    assert(fdt_path_offset(dt, "/reserved-memory/region74@101e8ec8000") < 0);
    check_claim(true);

    setup(); hole_dcp_vram = true;
    memcpy(before, dt, sizeof(tree));
    assert(dt_set_display_t8140() == 0);
    check_fallback(before);

    setup(); hole_dcp_firmware = true;
    memcpy(before, dt, sizeof(tree));
    assert(dt_set_display_t8140() == 0);
    check_fallback(before);

    setup(); hole_ext = true;
    assert(dt_set_display_t8140() == 0);
    check_status("dcp", "okay");
    check_status("disp0", "okay");
    check_status("dcpext", "disabled");
    assert(fdt_path_offset(dt, "/reserved-memory/region74@101e8ec8000") < 0);

    setup(); bad_carveout = true;
    memcpy(before, dt, sizeof(tree));
    assert(dt_set_display_t8140() == 0);
    check_fallback(before);

    setup(); bad_segment = true;
    memcpy(before, dt, sizeof(tree));
    assert(dt_set_display_t8140() == 0);
    check_fallback(before);

    setup();
    assert(fdt_delprop(dt, fdt_path_offset(dt, "/aliases"), "dcp") == 0);
    memcpy(before, dt, sizeof(tree));
    assert(dt_set_display_t8140() == 0);
    check_fallback(before);

    setup();
    assert(fdt_delprop(dt, fdt_path_offset(dt, "/aliases"), "dcpext") == 0);
    assert(dt_set_display_t8140() == 0);
    check_status("dcpext", "okay");

    setup();
    cur_boot_args.video.base = carveouts[0].pa + carveouts[0].size - 4096;
    memcpy(before, dt, sizeof(tree));
    assert(dt_set_display_t8140() == 0);
    check_fallback(before);
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
    result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
    assert "framebuffer: PA 0x101eb4b8000 size 0xde1380; /vram PA 0x101eb4b8000 size 0x5758000" in result.stdout
    assert "framebuffer: sub-range of /vram validated" in result.stdout
    assert "physical DRAM: PA 0x10000000000 size 0x200000000" in result.stdout
    assert "refused at preflight: /vram lies outside physical DRAM" in result.stdout
    assert "region-id-57 PA 0x10200000000 size 0xc00000 outside physical DRAM" in result.stdout
    assert "refused at /vram DCP/disp0 DART reservation" in result.stdout
    assert "refused at preflight: boot framebuffer lies outside /vram" in result.stdout
