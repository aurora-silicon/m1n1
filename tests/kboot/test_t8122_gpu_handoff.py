#!/usr/bin/env python3
"""Host-test the T8122 GPU firmware handoff with real libfdt and the production kboot_gpu code.

The ADT values are the J613 14.8.3 capture (set J613_ADT to a saved J613 ADT to
read them from it instead). The DT is a J613 fixture compiled by dtc with the
placeholders the Linux side provides (set J613_GPU_DTSI to use a real dtsi that
defines the gpu alias, the GPU node and the uat-* reserved-memory nodes).
The checks mirror what the M3 runtime (drm/asahi m3_board.rs admit() and
g16_resources.rs Resources::validate) requires.
"""
from pathlib import Path
import os
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "src/kboot_gpu.c").read_text()

# /arm-io/sgx {gpu-region, gfx-shared-region, gfx-handoff, gfx-shared-l2-region}-{base,size}
UAT = [(0x103FFFB8000, 0x4000), (0x103FFF78000, 0x40000), (0x103FFF70000, 0x4000),
       (0x103FFF74000, 0x4000)]
# /arm-io/gfx-asc segment-ranges (phys, iova, remap, size, flags)
SEGMENTS = [(0x1000084C000, 0xFFFFFC0000000000, 0x1000084C000, 0x64000, 1),
            (0x10001888000, 0xFFFFFC0000064000, 0x10001888000, 0xB0000, 0)]
DRAM = (0x10000000000, 0x400000000)

FIXTURE_DTSI = r"""
/ {
	aliases {
		gpu = &gpu;
	};
	reserved-memory {
		gpu_hw_cal_a: hw-cal-a { status = "disabled"; };
		uat_handoff: uat-handoff { reg = <0 0 0 0>; status = "disabled"; };
		uat_pagetables: uat-pagetables { reg = <0 0 0 0>; status = "disabled"; };
		uat_ttbs: uat-ttbs { reg = <0 0 0 0>; status = "disabled"; };
		uat_pagetables_l2: uat-pagetables-l2 { reg = <0 0 0 0>; status = "disabled"; };
	};
};

&{/soc} {
	agx_mbox: mailbox@292408000 {
		compatible = "apple,t8122-asc-mailbox", "apple,asc-mailbox-v4";
		reg = <0x2 0x92408000 0x0 0x4000>;
		#mbox-cells = <0>;
	};
	gpu: gpu@290000000 {
		compatible = "apple,agx-t8122";
		reg = <0x2 0x92400000 0 0x4000>, <0x2 0x90000000 0 0x4000000>;
		reg-names = "asc", "sgx";
		mboxes = <&agx_mbox>;
		memory-region = <&gpu_hw_cal_a>, <&uat_ttbs>;
		memory-region-names = "hw-cal-a", "ttbs";
		status = "disabled";
	};
	other: other@200000000 {
		compatible = "apple,t8122-something";
		reg = <0x2 0x0 0x0 0x4000>;
		status = "disabled";
	};
};
"""

BOARD_DTS = r"""
/dts-v1/;
/memreserve/ 0x10010000000 0x100000;
/ {
	compatible = "apple,j613", "apple,t8122", "apple,arm-platform";
	#address-cells = <2>;
	#size-cells = <2>;
	aliases { };
	reserved-memory {
		#address-cells = <2>;
		#size-cells = <2>;
		ranges;
		/* An unrelated ASC reservation made earlier (as the DCP handoff does). */
		asc-firmware@1000022c000 {
			compatible = "apple,asc-mem";
			reg = <0x100 0x0022c000 0x0 0x601000>;
			no-map;
		};
	};
	soc { #address-cells = <2>; #size-cells = <2>; ranges; };
};
#include "gpu.dtsi"
"""


def read_adt(path):
    """Minimal Apple Device Tree reader: returns {path: {prop: bytes}}."""
    data = Path(path).read_bytes()
    nodes = {}

    def node(offset, parent):
        nprops, nchildren = struct.unpack_from("<II", data, offset)
        offset += 8
        props = {}
        for _ in range(nprops):
            name = data[offset:offset + 32].split(b"\0")[0].decode()
            size = struct.unpack_from("<I", data, offset + 32)[0] & 0x7FFFFFFF
            props[name] = data[offset + 36:offset + 36 + size]
            offset += 36 + ((size + 3) & ~3)
        name = props.get("name", b"").split(b"\0")[0].decode()
        here = "/" if parent is None else (parent.rstrip("/") + "/" + name)
        nodes[here] = props
        for _ in range(nchildren):
            offset = node(offset, here)
        return offset

    node(0, None)
    return nodes


def adt_values():
    global UAT, SEGMENTS, DRAM
    path = os.environ.get("J613_ADT")
    if not path:
        return "built-in J613 capture values"
    nodes = read_adt(path)
    sgx = nodes["/arm-io/sgx"]
    assert sgx["compatible"].split(b"\0")[0] == b"gpu,t8122"
    q = lambda b: struct.unpack("<Q", b)[0]
    UAT = [(q(sgx[n + "-base"]), q(sgx[n + "-size"])) for n in
           ("gpu-region", "gfx-shared-region", "gfx-handoff", "gfx-shared-l2-region")]
    asc = nodes["/arm-io/gfx-asc"]
    nub = nodes["/arm-io/gfx-asc/iop-gfx-nub"]
    assert asc["segment-ranges"] == nub["segment-ranges"]
    assert asc["segment-names"] == b"__TEXT;__DATA\0" and nub["pre-loaded"] == struct.pack("<I", 1)
    raw = asc["segment-ranges"]
    SEGMENTS = [struct.unpack_from("<QQQII", raw, i) for i in range(0, len(raw), 32)]
    chosen = nodes["/chosen"]
    DRAM = (q(chosen["dram-base"]), q(chosen["dram-size"]))
    return path


HARNESS = r"""
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libfdt.h>
typedef uint64_t u64;
typedef uint32_t u32;
#define T8122 0x8122
#define SZ_16K 0x4000UL
#define ALIGN_UP(x, a) (((x) + (a) - 1) & ~((a) - 1))
#define UNUSED(x) (void)(x)
#define bail(...) do { printf(__VA_ARGS__); return -1; } while (0)
struct adt_segment_ranges { u64 phys, iova, remap; u32 size, unk; } __attribute__((packed));

static char tree[1 << 20];
static void *adt;
static u32 chip_id = T8122;

/* ---- ADT stub: 1 = sgx, 2 = gfx-asc, 3 = iop-gfx-nub, 4 = /chosen ---- */
struct prop { int node; const char *name; unsigned char data[96]; u32 len; };
static struct prop props[32];
static int nprops;
static int adt_no_node;
static void setp(int node, const char *name, const void *data, u32 len) {
    for (int i = 0; i < nprops; i++)
        if (props[i].node == node && !strcmp(props[i].name, name)) {
            memcpy(props[i].data, data, len); props[i].len = len; return;
        }
    assert(nprops < 32 && len <= 96);
    props[nprops].node = node; props[nprops].name = name;
    memcpy(props[nprops].data, data, len); props[nprops++].len = len;
}
static void delp(int node, const char *name) {
    for (int i = 0; i < nprops; i++)
        if (props[i].node == node && !strcmp(props[i].name, name)) props[i].node = -1;
}
static void setq(int node, const char *name, u64 v) { setp(node, name, &v, 8); }
static int adt_path_offset(void *p, const char *path) {
    const char *paths[] = {"", "/arm-io/sgx", "/arm-io/gfx-asc", "/arm-io/gfx-asc/iop-gfx-nub",
                           "/chosen"};
    for (int i = 1; i < 5; i++)
        if (!strcmp(path, paths[i])) return i == adt_no_node ? -1 : i;
    return -1;
}
static const void *adt_getprop(void *p, int node, const char *name, u32 *len) {
    for (int i = 0; i < nprops; i++)
        if (props[i].node == node && !strcmp(props[i].name, name)) {
            *len = props[i].len;
            return props[i].data;
        }
    return NULL;
}
"""

MAIN = r"""
static unsigned char dtb[1 << 16];
static unsigned char orig_tree[1 << 20];
static void *dt = tree;

static struct adt_segment_ranges segs[2];
static void put_segs(int which) {
    if (which & 1) setp(2, "segment-ranges", segs, sizeof(segs));
    if (which & 2) setp(3, "segment-ranges", segs, sizeof(segs));
}
static void reset_adt(void) {
    nprops = 0; adt_no_node = 0; chip_id = T8122;
    setp(1, "compatible", "gpu,t8122", 10);
    const char *names[] = {"gpu-region", "gfx-shared-region", "gfx-handoff", "gfx-shared-l2-region"};
    for (int i = 0; i < 4; i++) {
        char *b = malloc(64), *s = malloc(64);
        snprintf(b, 64, "%s-base", names[i]); snprintf(s, 64, "%s-size", names[i]);
        setq(1, b, UAT[i][0]); setq(1, s, UAT[i][1]);
    }
    memcpy(segs, SEGS, sizeof(segs));
    put_segs(3);
    setp(2, "segment-names", "__TEXT;__DATA", 14);
    u32 one = 1;
    setp(3, "pre-loaded", &one, 4);
    setq(4, "dram-base", DRAM0); setq(4, "dram-size", DRAM1);
}
static void load(int room) {
    assert(!fdt_open_into(dtb, tree, fdt_totalsize(dtb) + room));
    memcpy(orig_tree, tree, fdt_totalsize(tree));
}
static int node(const char *path) { return fdt_path_offset(dt, path); }
static const char *str(const char *path, const char *prop) {
    return fdt_getprop(dt, node(path), prop, NULL);
}

static const char *NAMES[6] = {"ttbs", "pagetables", "handoff", "shared-l2", "fw-text", "fw-data"};
static const char *STATIC[4] = {"uat-ttbs", "uat-pagetables", "uat-handoff", "uat-pagetables-l2"};
static u64 rbase(int i) { return i < 4 ? UAT[i][0] : segs[i - 4].phys; }
static u64 rsize(int i) { return i < 4 ? UAT[i][1] : ALIGN_UP((u64)segs[i - 4].size, SZ_16K); }
static void rpath(char *p, size_t n, int i) {
    if (i < 4) snprintf(p, n, "/reserved-memory/%s", STATIC[i]);
    else snprintf(p, n, "/reserved-memory/asc-firmware@%lx", (unsigned long)rbase(i));
}

/* Region i is reserved no-map, enabled, at exactly its ADT range. */
static bool reserved(int i) {
    char p[96]; rpath(p, sizeof(p), i);
    int n = node(p), len;
    if (n < 0) return false;
    const fdt64_t *reg = fdt_getprop(dt, n, "reg", &len);
    const char *status = fdt_getprop(dt, n, "status", NULL);
    return reg && len == 16 && fdt64_ld(reg) == rbase(i) && fdt64_ld(reg + 1) == rsize(i) &&
           fdt_getprop(dt, n, "no-map", NULL) && (!status || !strcmp(status, "okay"));
}

/* No two enabled reserved-memory nodes (or /memreserve/ entries) overlap. */
static int resv_count;
static void check_no_overlaps(void) {
    u64 start[64], end[64];
    int n = 0, sub;
    fdt_for_each_subnode(sub, dt, node("/reserved-memory")) {
        const char *status = fdt_getprop(dt, sub, "status", NULL);
        int len;
        const fdt64_t *reg = fdt_getprop(dt, sub, "reg", &len);
        if ((status && strcmp(status, "okay")) || !reg) continue;
        for (int i = 0; i + 16 <= len; i += 16, reg += 2) {
            if (!fdt64_ld(reg + 1)) continue;
            start[n] = fdt64_ld(reg); end[n] = start[n] + fdt64_ld(reg + 1); n++;
        }
    }
    resv_count = n;
    for (int i = 0; i < fdt_num_mem_rsv(dt); i++) {
        u64 a, s;
        assert(!fdt_get_mem_rsv(dt, i, &a, &s));
        start[n] = a; end[n] = a + s; n++;
    }
    for (int i = 0; i < n; i++)
        for (int j = 0; j < i; j++)
            assert(!(start[i] < end[j] && start[j] < end[i]));
}

static const char *gpu_status(void) { return str("gpu", "status"); }
static bool published(void) { return fdt_getprop(dt, node("gpu"), "apple,m3-handoff-version", NULL); }

/* Exactly what m3_board.rs admit() + Resources::validate() accept. */
static void check_published(bool enabled) {
    int gpu = node("gpu"), len, nlen;
    assert(gpu >= 0 && published());
    const fdt32_t *ver = fdt_getprop(dt, gpu, "apple,m3-handoff-version", &len);
    assert(len == 4 && fdt32_ld(ver) == 1);
    const fdt64_t *vas = fdt_getprop(dt, gpu, "apple,firmware-segment-vas", &len);
    assert(vas && len == 16);
    const fdt32_t *flags = fdt_getprop(dt, gpu, "apple,firmware-segment-flags", &len);
    assert(flags && len == 8 && fdt32_ld(flags) == 1 && fdt32_ld(flags + 1) == 0);
    assert(fdt64_ld(vas) == 0xfffffc0000000000UL);
    assert(fdt64_ld(vas) + rsize(4) == fdt64_ld(vas + 1));
    assert(fdt64_ld(vas) == segs[0].iova && fdt64_ld(vas + 1) == segs[1].iova);

    const fdt32_t *mr = fdt_getprop(dt, gpu, "memory-region", &len);
    const char *names = fdt_getprop(dt, gpu, "memory-region-names", &nlen);
    assert(mr && names && len % 4 == 0);
    int count = len / 4, off = 0, found = 0;
    for (int k = 0; k < count; k++) {
        const char *name = names + off;
        assert(off < nlen);
        off += strlen(name) + 1;
        int i = -1;
        for (int j = 0; j < 6; j++) if (!strcmp(name, NAMES[j])) i = j;
        if (i < 0) { assert(!strcmp(name, "hw-cal-a") && k == 0); continue; }
        assert(k == count - 6 + i);                   /* ours, last, in driver order */
        char p[96]; rpath(p, sizeof(p), i);
        assert(fdt32_ld(mr + k) == fdt_get_phandle(dt, node(p)) && fdt32_ld(mr + k));
        assert(reserved(i));
        found++;
    }
    assert(off == nlen && found == 6);
    for (int i = 0; i < 6; i++) {
        assert(!(rbase(i) & 0x3fff) && !(rsize(i) & 0x3fff) && rbase(i) + rsize(i) <= (1UL << 42));
        for (int j = 0; j < i; j++)
            assert(!(rbase(i) < rbase(j) + rsize(j) && rbase(j) < rbase(i) + rsize(i)));
    }
    assert(!strcmp(gpu_status(), enabled ? "okay" : "disabled"));
    check_no_overlaps();
}

/* Handoff refused: GPU node carries no handoff description and stays disabled. */
static void check_refused(void) {
    int gpu = node("gpu");
    if (gpu >= 0 && !fdt_node_check_compatible(dt, gpu, "apple,agx-t8122")) {
        assert(!published());
        assert(!fdt_getprop(dt, gpu, "apple,firmware-segment-vas", NULL));
        assert(!strcmp(gpu_status(), "disabled"));
        int len;
        const char *names = fdt_getprop(dt, gpu, "memory-region-names", &len);
        assert(!names || len == 14);   /* "hw-cal-a\0ttbs\0" untouched */
    }
    check_no_overlaps();
}

static void opt_in(void) { assert(!fdt_setprop_empty(dt, node("gpu"), "apple,j613-native-gpu")); }

int main(void) {
    FILE *f = fopen(DTB_PATH, "rb");
    assert(f);
    size_t dtb_len = fread(dtb, 1, sizeof(dtb), f);
    fclose(f);
    assert(dtb_len > 0 && !fdt_check_header(dtb));

    /* A. linux-aurora DT today: no GPU node, no uat-* placeholders. Reserve only. */
    reset_adt(); load(0x10000);
    assert(!fdt_del_node(dt, node("gpu")));
    for (int i = 0; i < 4; i++) {
        char p[96]; rpath(p, sizeof(p), i);
        assert(!fdt_del_node(dt, node(p)));
    }
    assert(!fdt_delprop(dt, node("/aliases"), "gpu"));
    assert(!dt_set_gpu_t8122(dt));
    for (int i = 0; i < 6; i++) assert(reserved(i));
    check_refused();
    assert(resv_count == 7);                           /* six + the DCP one */

    /* B. Linux DT with placeholders, no opt-in: published, left disabled. */
    reset_adt(); load(0x10000);
    assert(!dt_set_gpu_t8122(dt));
    check_published(false);
    assert(resv_count == 7);                           /* placeholders filled, none added */

    /* Idempotent: a second pass produces the same tree. */
    static unsigned char once[1 << 20];
    memcpy(once, tree, fdt_totalsize(tree));
    assert(!dt_set_gpu_t8122(dt));
    assert(!memcmp(once, tree, fdt_totalsize(tree)));

    /* C. Opt-in on J613 enables; the same opt-in on another board does not. */
    reset_adt(); load(0x10000); opt_in();
    assert(!dt_set_gpu_t8122(dt));
    check_published(true);
    reset_adt(); load(0x10000); opt_in();
    assert(!fdt_setprop_string(dt, 0, "compatible", "apple,j615"));
    assert(!dt_set_gpu_t8122(dt));
    check_published(false);
    /* A DT that enables the node without opting in is overridden. */
    reset_adt(); load(0x10000);
    assert(!fdt_setprop_string(dt, node("gpu"), "status", "okay"));
    assert(!dt_set_gpu_t8122(dt));
    check_published(false);

    /* D. GPU node without the alias: found by its compatible if unique. */
    reset_adt(); load(0x10000);
    assert(!fdt_delprop(dt, node("/aliases"), "gpu"));
    assert(!dt_set_gpu_t8122(dt));
    assert(fdt_getprop(dt, fdt_node_offset_by_compatible(dt, -1, "apple,agx-t8122"),
                       "apple,m3-handoff-version", NULL));

    /* E. Invalid ADT descriptions: refused, every usable range still reserved. */
    for (int mode = 0; mode < 16; mode++) {
        reset_adt(); load(0x10000); opt_in();
        assert(!fdt_setprop_string(dt, node("gpu"), "status", "okay"));
        unsigned expect = 0x3f;                        /* regions still reserved */
        switch (mode) {
        case 0: {                                       /* nub copy differs */
            struct adt_segment_ranges t[2];
            memcpy(t, segs, sizeof(t));
            t[1].size += 0x4000;
            setp(3, "segment-ranges", t, sizeof(t));
            break;
        }
        case 1: setp(2, "segment-names", "__DATA;__TEXT", 14); break;
        case 2: { u32 z = 0; setp(3, "pre-loaded", &z, 4); break; }
        case 3: segs[0].unk = 0; segs[1].unk = 1; put_segs(3); break;
        case 4: segs[0].iova += 0x4000; segs[1].iova += 0x4000; put_segs(3); break;
        case 5: segs[1].iova += 0x4000; put_segs(3); break;
        case 6: segs[0].phys += 0x1000; segs[0].remap = segs[0].phys; put_segs(3);
                expect = 0x2f; break;                  /* misaligned text: not reservable */
        case 7: segs[1].remap += 0x4000; put_segs(3); break;
        case 8: setq(1, "gfx-shared-l2-region-base", UAT[2][0]); expect = 0x37; break;
        case 9: setq(1, "gpu-region-base", DRAM0 + DRAM1); expect = 0x3e; break;
        case 10: delp(1, "gfx-handoff-size"); expect = 0x3b; break;
        case 11: setp(1, "compatible", "gpu,t8112", 10); break;
        case 12: delp(4, "dram-base"); break;          /* reserved without bounds */
        case 13: segs[1].size = 0x300000; put_segs(3); break;
        case 14: adt_no_node = 3; break;               /* no iop-gfx-nub */
        case 15: setq(1, "gpu-region-size", 0x4001); expect = 0x3e; break;
        }
        assert(!dt_set_gpu_t8122(dt));
        check_refused();
        for (int i = 0; i < 6; i++) {
            if (mode == 8 && i == 3) continue;         /* covered by uat-handoff */
            if (!(expect & (1U << i))) continue;
            assert(reserved(i));
        }
    }

    /* F. DT conflicts refuse the handoff but keep what can be reserved. */
    reset_adt(); load(0x10000); opt_in();
    {
        int resv = node("/reserved-memory");
        int n = fdt_add_subnode(dt, resv, "asc-firmware@10001880000");
        fdt64_t reg[2]; fdt64_st(&reg[0], 0x10001880000UL); fdt64_st(&reg[1], 0x10000);
        assert(n >= 0 && !fdt_setprop(dt, n, "reg", reg, 16) && !fdt_setprop_empty(dt, n, "no-map"));
    }
    assert(!dt_set_gpu_t8122(dt));
    check_refused();
    for (int i = 0; i < 5; i++) assert(reserved(i));
    assert(!reserved(5));                              /* partial overlap: left alone */

    reset_adt(); load(0x10000); opt_in();              /* /memreserve/ over fw-text */
    assert(!fdt_add_mem_rsv(dt, segs[0].phys, 0x4000));
    assert(!dt_set_gpu_t8122(dt));
    check_refused();

    reset_adt(); load(0x10000);                        /* alias to a foreign node */
    assert(!fdt_setprop_string(dt, node("/aliases"), "gpu", "/soc/other@200000000"));
    assert(!fdt_setprop_string(dt, node("/soc/other@200000000"), "status", "okay"));
    assert(!dt_set_gpu_t8122(dt));
    assert(!strcmp(str("/soc/other@200000000", "status"), "okay"));   /* not ours: untouched */
    assert(!fdt_getprop(dt, node("/soc/gpu@290000000"), "apple,m3-handoff-version", NULL));
    for (int i = 0; i < 6; i++) assert(reserved(i));
    check_no_overlaps();

    reset_adt(); load(0x10000);                        /* two GPU nodes, no alias */
    assert(!fdt_delprop(dt, node("/aliases"), "gpu"));
    assert(!fdt_setprop_string(dt, node("/soc/other@200000000"), "compatible", "apple,agx-t8122"));
    assert(!dt_set_gpu_t8122(dt));
    assert(!fdt_getprop(dt, node("/soc/gpu@290000000"), "apple,m3-handoff-version", NULL));
    for (int i = 0; i < 6; i++) assert(reserved(i));

    reset_adt(); load(0x10000);                        /* inconsistent memory-region lists */
    assert(!fdt_setprop_string(dt, node("gpu"), "memory-region-names", "hw-cal-a"));
    assert(!dt_set_gpu_t8122(dt));
    assert(!published() && !strcmp(gpu_status(), "disabled"));
    for (int i = 0; i < 6; i++) assert(reserved(i));

    /* G. Transactional: wherever the FDT runs out of space, the handoff is all or nothing. */
    int complete = 0, partial = 0;
    for (int room = 0; room <= 0x1000; room += 8) {
        reset_adt(); load(0x10000); opt_in();
        assert(!fdt_pack(dt) && !fdt_open_into(dt, dt, fdt_totalsize(dt) + room));
        assert(!dt_set_gpu_t8122(dt));
        if (published()) {
            check_published(true);
            complete++;
        } else {
            check_refused();
            partial++;
        }
    }
    printf("room sweep: %d refused (reservations only), %d complete\n", partial, complete);
    assert(partial && complete);

    printf("T8122 GPU handoff: ttbs 0x%lx, fw-text 0x%lx+0x%lx, fw-data 0x%lx+0x%lx, all checks passed\n",
           (unsigned long)UAT[0][0], (unsigned long)segs[0].phys, (unsigned long)rsize(4),
           (unsigned long)segs[1].phys, (unsigned long)rsize(5));
    return 0;
}
"""


def main():
    source_note = adt_values()
    start = SOURCE.index("#define T8122_GPU_REGIONS")
    end = SOURCE.index("int dt_set_gpu(void *dt)", start)
    functions = SOURCE[start:end]

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dtsi = os.environ.get("J613_GPU_DTSI")
        (tmp / "gpu.dtsi").write_text(Path(dtsi).read_text() if dtsi else FIXTURE_DTSI)
        (tmp / "board.dts").write_text(BOARD_DTS)
        pre = subprocess.run(["cc", "-E", "-P", "-nostdinc", "-undef", "-x", "assembler-with-cpp",
                              "-I", str(tmp), str(tmp / "board.dts")],
                             check=True, capture_output=True, text=True).stdout
        (tmp / "board.pp.dts").write_text(pre)
        subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-@", "-o", str(tmp / "board.dtb"),
                        str(tmp / "board.pp.dts")], check=True)

        defines = [
            "static const u64 UAT[4][2] = {%s};" % ", ".join("{0x%xUL, 0x%xUL}" % r for r in UAT),
            "static const struct adt_segment_ranges SEGS[2] = {%s};" %
            ", ".join("{0x%xUL, 0x%xUL, 0x%xUL, 0x%x, %d}" % s for s in SEGMENTS),
            "#define DRAM0 0x%xUL\n#define DRAM1 0x%xUL" % DRAM,
            '#define DTB_PATH "%s"' % (tmp / "board.dtb"),
        ]
        harness = HARNESS + "\n".join(defines) + "\n" + functions + MAIN
        (tmp / "test.c").write_text(harness)
        subprocess.run(["cc", "-std=gnu11", "-Wall", "-Werror", str(tmp / "test.c"), "-lfdt",
                        "-o", str(tmp / "test")], check=True)
        out = subprocess.run([str(tmp / "test")], check=True, capture_output=True, text=True).stdout
        print(*[l for l in out.splitlines() if l.startswith(("room sweep", "T8122 GPU handoff:"))],
              "(ADT: %s; DT: %s)" % (source_note, dtsi or "fixture"))


if __name__ == "__main__":
    main()
