#!/usr/bin/env python3
"""Check display mapping publication with real libfdt and an injected page walk."""
from pathlib import Path
import subprocess
import tempfile
source = (Path(__file__).resolve().parents[2] / 'src/kboot.c').read_text()
functions = []
for name in ('dt_device_set_reserved_mem', 'dt_get_or_add_reserved_mem',
             'dt_device_add_mem_region', 'dt_get_iommu_node', 'dt_reserve_asc_firmware'):
    start = source.index('static int '+name+'(')
    functions.append(source[start:source.index('\n}', start)+2])
start = source.index('struct j514s_display_maps {')
functions.append(source[start:source.index('/* Publish the inherited J514S', start)])
harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <libfdt.h>
typedef uint64_t u64;
typedef uint32_t u32;
typedef uint8_t u8;
#define T6030 0x8132
#define SZ_16K 0x4000ULL
#define ALIGN_UP(x,a) (((x)+(a)-1)&~((a)-1))
#define bail(...) do { return -1; } while (0)
static char tree[16384];
static void *dt = tree, *adt;
static int chip_id = T6030, fault;
struct adt_segment_ranges { u64 phys, iova, remap; u32 size, unk; };
static const struct adt_segment_ranges segments[] = {
    { 0x103e0000000, 0, 0x10000100000, 0x4000, 2 },
    { 0x103e1000000, 0, 0x10000200000, 0x8000, 2 },
    { 0x103e2000000, 0, 0x10000300000, 0x8000, 2 },
    { 0x103e3000000, 0, 0x10000400000, 0x4000, 2 },
};
static int adt_path_offset(void *p, const char *s) { return 1; }
static const void *adt_getprop(void *p, int n, const char *s, u32 *len) {
    *len = sizeof(segments); return segments;
}
static int dart_visit_locked_t8110(uintptr_t base, u8 sid, u64 dram, u64 size,
                                   int (*visit)(u64,u64,void *), void *opaque) {
    assert(base == 0x28d304000 && (sid == 0 || sid == 4));
    for (unsigned i = 0; i < 4; i++) {
        if ((i == 1) != (sid == 4)) continue;
        for (u32 off = 0; off < segments[i].size; off += SZ_16K) {
            if (fault == 2 && i == 2 && off) continue;
            u64 pa = segments[i].phys + off + (fault == 1 ? SZ_16K : 0);
            int ret = visit((segments[i].remap & ((1ULL<<36)-1))+off,pa,opaque);
            if (ret) return ret;
        }
    }
    if (fault == 3) return visit(0x500000,0x103e4000000,opaque);
    return 0;
}
'''
harness += '\n'.join(functions)
harness += r'''
static void setup(void) {
    assert(!fdt_create_empty_tree(dt,sizeof(tree)));
    const char *nodes[] = {"reserved-memory","dcp","disp0","disp0_piodma","dart","aliases"};
    for (unsigned i=0;i<6;i++) assert(fdt_add_subnode(dt,0,nodes[i])>=0);
    int n=fdt_path_offset(dt,"/dart");
    assert(!fdt_setprop_u32(dt,n,"phandle",1));
    assert(!fdt_setprop_string(dt,n,"status","disabled"));
    n=fdt_path_offset(dt,"/disp0_piodma");
    fdt32_t iommu[]={cpu_to_fdt32(1),cpu_to_fdt32(4)};
    assert(!fdt_setprop(dt,n,"iommus",iommu,sizeof(iommu)));
    assert(!fdt_setprop_string(dt,n,"status","disabled"));
    for (unsigned i=1;i<4;i++) {
        char path[64];snprintf(path,sizeof(path),"/%s",nodes[i]);
        n=fdt_path_offset(dt,"/aliases");
        assert(!fdt_setprop_string(dt,n,nodes[i],path));
    }
    assert(!dt_reserve_asc_firmware("/dcp",NULL,"dcp",true,0));
}
int main(void) {
    for (fault=0;fault<=3;fault++) {
        setup();
        char before[sizeof(tree)];memcpy(before,tree,sizeof(tree));
        int ret=dt_set_j514s_display_maps(segments,4,0x10000000000,0x400000000);
        if (fault) { assert(ret<0 && !memcmp(tree,before,sizeof(tree))); continue; }
        assert(!ret);
        int n=fdt_path_offset(dt,"/dart"),length;
        assert(!strcmp(fdt_getprop(dt,n,"status",NULL),"okay"));
        n=fdt_path_offset(dt,"disp0_piodma");
        assert(!strcmp(fdt_getprop(dt,n,"status",NULL),"okay"));
        assert(fdt_getprop(dt,n,"memory-region",&length) && length==4);
        n=fdt_path_offset(dt,"disp0");
        assert(fdt_getprop(dt,n,"memory-region",&length) && length==12);
        for (unsigned i=0;i<4;i++) {
            char path[128];snprintf(path,sizeof(path),"/reserved-memory/asc-firmware@%lx",segments[i].phys);
            n=fdt_path_offset(dt,path);
            const fdt32_t *map=fdt_getprop(dt,n,"iommu-addresses",&length);
            assert(map && length==40);
            assert(fdt64_ld((const fdt64_t *)(map+1))==segments[i].remap);
            assert(fdt64_ld((const fdt64_t *)(map+6))==segments[i].remap);
            assert(fdt32_ld(map)!=fdt32_ld(map+5));
        }
    }
    fault=0;setup();
    int n=fdt_path_offset(dt,"dcp");
    assert(!fdt_setprop_empty(dt,n,"apple,j514s-native-scanout"));
    n=fdt_path_offset(dt,"disp0");
    assert(!fdt_setprop_string(dt,n,"compatible","apple,t6030-display-diagnostics"));
    assert(!dt_set_j514s_display_maps(segments,4,0x10000000000,0x400000000));
    n=fdt_path_offset(dt,"disp0");
    assert(fdt_getprop(dt,n,"apple,j514s-inherited-mappings",NULL));
    assert(!strcmp(fdt_getprop(dt,n,"status",NULL),"okay"));
    puts("display mapping publication preserves DCP aliases and rejects incomplete/unknown pages");
}
'''
with tempfile.TemporaryDirectory() as tmp:
    p=Path(tmp);(p/'test.c').write_text(harness)
    subprocess.run(['cc','-std=gnu11','-Wall','-Werror',str(p/'test.c'),'-lfdt','-o',str(p/'test')],check=True)
    subprocess.run([str(p/'test')],check=True)
