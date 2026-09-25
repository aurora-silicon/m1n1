#!/usr/bin/env python3
"""Host-test the J613 (T8122) DCP handoff with real libfdt and the production kboot code.

The DT comes from a J613 fixture compiled by dtc, shaped like the kernel's
t8122-j613-dcp.dtsi (set J613_DCP_DTSI to use that file instead, or J613_DTB
to use a complete compiled t8122-j613.dtb). The ADT
values are the J613 os-fw 14.7 capture (set J613_ADT to a saved J613 ADT to
read them from it instead). The locked DART walk is injected;
tests/kboot/test_locked_dart_walk.py covers it.

The contract is the one drm/apple's J613 path and the display gate read:
apple,firmware-uuid, apple,firmware-compat = <14 7 0>, apple,notch-height and a
memory-region per segment on the DCP (iommu-addresses, apple,dcp-os-log on
__OS_LOG); the "framebuffer" memory-region and the SID0 segments on the display
subsystem; the SID4 segment on the PIODMA child; apple,t8122-handoff = <1> on
all three, set last; every status left as the DT has it.
"""
from pathlib import Path
import os
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "src/kboot.c").read_text()

# (phys, iova, remap, size, unk) from /arm-io/dcp/iop-dcp-nub on J613.
SEGMENTS = [
    (0x1000022C000, 0x0, 0x10040000000, 0x601000, 0x1),       # __TEXT
    (0x103E1864000, 0x601000, 0x10080000000, 0x300000, 0x0),  # __DATA
    (0x10002B1C000, 0x901000, 0x10002B1C000, 0x20000, 0xA),   # __OS_LOG
    (0x103E5C28000, 2**64 - 1, 0x1000099C000, 0x1000000, 0x2),
    (0x103E5C24000, 2**64 - 1, 0x100019C4000, 0x4000, 0x2),
    (0x103E5B64000, 2**64 - 1, 0x100019C8000, 0xC0000, 0x2),
    (0x103E1BF4000, 2**64 - 1, 0x10001A88000, 0x3F70000, 0x2),
]
UUID = "90F849E1-B422-367E-B389-50246F8DEC47"
DRAM = (0x10000000000, 0x400000000)
# Measured on J613: disp0 SID0 maps segments 4 and 6, SID4 segment 5, and
# segment 3 is mapped by neither.
SID0, SID4, UNMAPPED = 0x50, 0x20, 0x08
# /vram is read from the ADT at boot; this fixture holds the 2560x1664 boot
# framebuffer (10240-byte stride) with the 2560x1600 notchless part 64 rows in.
VRAM = (0x103F0000000, 0x2000000)
STRIDE, NOTCH, FB_HEIGHT = 10240, 64, 1600

# Shaped like the kernel's t8122-j613-dcp.dtsi.
FIXTURE_DTSI = r"""
/ {
	aliases {
		dcp = &dcp;
		disp0 = &display;
		disp0-piodma = &disp0_piodma;
		disp0_piodma = &disp0_piodma;
	};
};

&{/soc} {
	dcp_mbox: mailbox@28ec08000 {
		compatible = "apple,t8122-asc-mailbox", "apple,asc-mailbox-v4";
		reg = <0x2 0x8ec08000 0x0 0x4000>;
		#mbox-cells = <0>;
		status = "disabled";
	};
	dcp_dart: iommu@28d30c000 {
		compatible = "apple,t8122-dart", "apple,t8110-dart";
		reg = <0x2 0x8d30c000 0x0 0x4000>;
		#iommu-cells = <1>;
		status = "disabled";
	};
	disp0_dart: iommu@28d304000 {
		compatible = "apple,t8122-dart", "apple,t8110-dart";
		reg = <0x2 0x8d304000 0x0 0x4000>;
		#iommu-cells = <1>;
		status = "disabled";
	};
	dcp: dcp@28ec00000 {
		compatible = "apple,t8122-dcp";
		mboxes = <&dcp_mbox>;
		iommus = <&dcp_dart 5>;
		reg = <0x2 0x8ec00000 0 0x4000>;
		power-domains = <&ps_disp_cpu>;
		#address-cells = <2>;
		#size-cells = <2>;
		status = "disabled";
		panel: panel { compatible = "apple,panel-j613", "apple,panel"; };
		disp0_piodma: piodma {
			iommus = <&disp0_dart 4>;
		};
	};
	display: display-subsystem {
		compatible = "apple,t8122-display-subsystem";
		iommus = <&disp0_dart 0>;
		status = "disabled";
	};
};
"""

BOARD_DTS = r"""
/dts-v1/;
#define AIC_IRQ 0
#define IRQ_TYPE_LEVEL_HIGH 4
/ {
	compatible = "apple,j613", "apple,t8122", "apple,arm-platform";
	#address-cells = <2>;
	#size-cells = <2>;
	aliases { };
	chosen {
		#address-cells = <2>;
		#size-cells = <2>;
		ranges;
		framebuffer@%(fb)x {
			compatible = "apple,simple-framebuffer", "simple-framebuffer";
			reg = <0x%(fb_hi)x 0x%(fb_lo)x 0x0 0x%(fb_size)x>;
		};
	};
	reserved-memory {
		#address-cells = <2>;
		#size-cells = <2>;
		ranges;
	};
	aic: interrupt-controller { #interrupt-cells = <3>; interrupt-controller; };
	ps_disp_cpu: power-controller { #power-domain-cells = <0>; };
	soc { #address-cells = <2>; #size-cells = <2>; ranges; };
};
#include "dcp.dtsi"
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
    global SEGMENTS, UUID, DRAM, VRAM
    path = os.environ.get("J613_ADT")
    if not path:
        return "built-in J613 capture values"
    nodes = read_adt(path)
    nub = nodes["/arm-io/dcp/iop-dcp-nub"]
    raw = nub["segment-ranges"]
    SEGMENTS = [struct.unpack_from("<QQQII", raw, i) for i in range(0, len(raw), 32)]
    UUID = nub["uuid"].split(b"\0")[0].decode()
    chosen = nodes["/chosen"]
    DRAM = (struct.unpack("<Q", chosen["dram-base"])[0], struct.unpack("<Q", chosen["dram-size"])[0])
    if "/vram" in nodes:
        VRAM = struct.unpack_from("<QQ", nodes["/vram"]["reg"])
    return path


def extract(name):
    start = SOURCE.index("static int " + name + "(")
    return SOURCE[start:SOURCE.index("\n}", start) + 2]


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
typedef uint8_t u8;
#define T6030 0x6030
#define T8122 0x8122
#define SZ_16K 0x4000ULL
#define ALIGN_UP(x, a) (((x) + (a) - 1) & ~((a) - 1))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define bail(...) do { printf(__VA_ARGS__); return -1; } while (0)
struct adt_segment_ranges { u64 phys, iova, remap; u32 size, unk; } __attribute__((packed));

static char tree[1 << 20];
static void *dt = tree, *adt;
static u32 chip_id = T8122;

/* ---- ADT stub: 1 = iop-dcp-nub, 2 = /chosen, 3 = /vram ---- */
static struct adt_segment_ranges segments[16];
static u32 nsegments;
static char uuid[64];
static u64 dram[2], vram[2];
static int adt_no_nub, adt_no_bounds, adt_no_vram;
static int adt_path_offset(void *p, const char *path) {
    if (!strcmp(path, "/arm-io/dcp/iop-dcp-nub")) return adt_no_nub ? -1 : 1;
    if (!strcmp(path, "/chosen")) return 2;
    return -1;
}
static int adt_path_offset_trace(void *p, const char *path, int *offsets) {
    if (strcmp(path, "/vram") || adt_no_vram) return -1;
    offsets[0] = 3; offsets[1] = 0;
    return 3;
}
static int adt_get_reg(void *p, int *path, const char *prop, int idx, u64 *addr, u64 *size) {
    assert(path[0] == 3 && !strcmp(prop, "reg") && !idx);
    *addr = vram[0]; *size = vram[1];
    return 0;
}
static const void *adt_getprop(void *p, int node, const char *name, u32 *len) {
    if (node == 1 && !strcmp(name, "segment-ranges")) {
        *len = nsegments * sizeof(*segments);
        return segments;
    }
    if (node == 1 && !strcmp(name, "uuid")) {
        *len = strlen(uuid) + 1;
        return uuid;
    }
    return NULL;
}
static int get_bound(int node, const char *name, u64 *value) {
    if (node != 2 || adt_no_bounds) return -1;
    *value = !strcmp(name, "dram-base") ? dram[0] : dram[1];
    return 8;
}
#define ADT_GETPROP(a, n, name, dest) get_bound(n, name, dest)

/* ---- Locked DART walk injection ---- */
static u32 layout[2];       /* segment bitmasks mapped by SID0 / SID4 */
static int fault, walks;
static int dart_visit_locked_t8110(uintptr_t base, u8 sid, u64 dram_base, u64 dram_size,
                                   int (*visit)(u64, u64, void *), void *opaque) {
    assert(base == 0x28d304000ULL && (sid == 0 || sid == 4));
    assert(dram_base == dram[0] && dram_size == dram[1]);
    walks++;
    if (fault == 1) return -1;                       /* unlocked or invalid tables */
    u32 mask = layout[sid == 4];
    for (u32 i = 0; i < nsegments; i++) {
        if (!(mask & (1U << i))) continue;
        for (u64 off = 0; off < ALIGN_UP((u64)segments[i].size, SZ_16K); off += SZ_16K) {
            if (fault == 2 && off) break;              /* partially mapped segment */
            u64 pa = segments[i].phys + off + (fault == 3 ? SZ_16K : 0);
            int ret = visit((segments[i].remap & ((1ULL << 36) - 1)) + off, pa, opaque);
            if (ret) return ret;
        }
    }
    if (fault == 4) return visit(0x7fff0000, dram[0] + 0x10000000, opaque); /* stray page */
    if (fault == 5) return visit(0x7fff0000, vram[0], opaque);               /* /vram page */
    return 0;
}

/* dt_set_dcp_firmware() for os_firmware 14.7 (compat[] can be broken by a test). */
static u32 compat[3];
static int dt_set_dcp_firmware(const char *alias) {
    int node = fdt_path_offset(dt, alias);
    if (node < 0) return 0;
    fdt32_t v[3] = {cpu_to_fdt32(14), cpu_to_fdt32(7), cpu_to_fdt32(0)};
    fdt32_t c[3] = {cpu_to_fdt32(compat[0]), cpu_to_fdt32(compat[1]), cpu_to_fdt32(compat[2])};
    if (fdt_setprop(dt, node, "apple,firmware-version", v, sizeof(v))) return -1;
    return fdt_setprop(dt, node, "apple,firmware-compat", c, sizeof(c)) ? -1 : 0;
}
"""

MAIN = r"""
static unsigned char dtb[1 << 18];
static size_t dtb_len;
static u32 notch_expected;                            /* 0: property absent */

static int node(const char *path) { return fdt_path_offset(dt, path); }
/* The DT as dt_set_fb() leaves it: the notchless boot framebuffer in
 * /chosen/framebuffer and apple,notch-height on the dcp alias.
 */
static void load(int room) {
    assert(!fdt_open_into(dtb, tree, fdt_totalsize(dtb) + room));
    fdt64_t fb[2] = {cpu_to_fdt64(FB_BASE), cpu_to_fdt64(FB_SIZE)};
    assert(!fdt_setprop(dt, node("/chosen/framebuffer"), "reg", fb, sizeof(fb)));
    if (node("dcp") >= 0) assert(!fdt_appendprop_u32(dt, node("dcp"), "apple,notch-height", NOTCH));
}
static void reset_adt(void) {
    nsegments = NSEG;
    memcpy(segments, SEGS, sizeof(SEGS));
    strcpy(uuid, UUID);
    dram[0] = DRAM0; dram[1] = DRAM1;
    vram[0] = VRAM0; vram[1] = VRAM1;
    adt_no_nub = adt_no_bounds = adt_no_vram = 0;
    chip_id = T8122;
    fault = walks = 0;
    layout[0] = LAYOUT0; layout[1] = LAYOUT1;
    compat[0] = 14; compat[1] = 7; compat[2] = 0;
    notch_expected = NOTCH;
}
static const char *str(const char *path, const char *prop) {
    return fdt_getprop(dt, node(path), prop, NULL);
}
static bool has(const char *path, const char *prop) {
    return fdt_getprop(dt, node(path), prop, NULL) != NULL;
}
static int proplen(const char *path, const char *prop) {
    int len = -1;
    return fdt_getprop(dt, node(path), prop, &len) ? len : -1;
}
static int resv(u32 i) {
    char path[96];
    snprintf(path, sizeof(path), "/reserved-memory/asc-firmware@%lx", (unsigned long)segments[i].phys);
    return fdt_path_offset(dt, path);
}
static int fb_resv(void) {
    char path[96];
    snprintf(path, sizeof(path), "/reserved-memory/framebuffer@%lx", (unsigned long)vram[0]);
    return fdt_path_offset(dt, path);
}
static const char *DART = "/soc/iommu@28d304000";
static const char *DCP_DART = "/soc/iommu@28d30c000";
static const char *MBOX = "/soc/mailbox@28ec08000";
static const char *MARKER = "apple,t8122-handoff";

/* The display gate enables these itself and refuses any that is not disabled. */
static void check_statuses(void) {
    const char *nodes[] = {"dcp", "disp0", DART, DCP_DART, MBOX};
    for (u32 i = 0; i < 5; i++) assert(!strcmp(str(nodes[i], "status"), "disabled"));
    assert(!has("disp0_piodma", "status"));          /* the driver creates it */
    assert(node("disp0_piodma") == fdt_subnode_offset(dt, node("dcp"), "piodma"));
}
static void check_no_markers(void) {
    assert(!has("dcp", MARKER) && !has("disp0", MARKER) && !has("disp0_piodma", MARKER));
}
/* Every segment is reserved no-map at its 16K-rounded size; nothing else is linked. */
static void check_reserved_only(void) {
    for (u32 i = 0; i < nsegments; i++) {
        int n = resv(i), len;
        if (segments[i].phys & (SZ_16K - 1)) { assert(n < 0); continue; }
        assert(n >= 0 && fdt_getprop(dt, n, "no-map", NULL));
        assert(!fdt_node_check_compatible(dt, n, "apple,asc-mem"));
        const fdt64_t *reg = fdt_getprop(dt, n, "reg", &len);
        assert(reg && len == 16 && fdt64_ld(reg) == segments[i].phys &&
               fdt64_ld(reg + 1) == ALIGN_UP((u64)segments[i].size, SZ_16K));
        assert(!fdt_getprop(dt, n, "iommu-addresses", NULL));
        assert(!fdt_getprop(dt, n, "apple,dcp-os-log", NULL));
    }
    assert(!has("dcp", "apple,firmware-uuid") && !has("dcp", "memory-region"));
}
static void check_display_untouched(void) {
    check_statuses();
    check_no_markers();
    assert(!has("disp0", "memory-region") && !has("disp0_piodma", "memory-region"));
    assert(!has("disp0", "memory-region-names") && fb_resv() < 0);
    assert(!has("disp0_piodma", "apple,j613-inherited-mappings"));
    assert(!has("disp0", "apple,j613-inherited-mappings"));
}
/* The DCP properties drm/apple reads. */
static void check_dcp_handoff(void) {
    int len;
    assert(!strcmp(str("dcp", "apple,firmware-uuid"), uuid));
    const fdt32_t *c = fdt_getprop(dt, node("dcp"), "apple,firmware-compat", &len);
    assert(c && len == 12 && fdt32_ld(c) == compat[0] && fdt32_ld(c + 1) == compat[1] &&
           fdt32_ld(c + 2) == compat[2]);
    assert(proplen("dcp", "apple,firmware-version") == 12);
    const fdt32_t *notch = fdt_getprop(dt, node("dcp"), "apple,notch-height", &len);
    assert(notch_expected ? notch && len == 4 && fdt32_ld(notch) == notch_expected : !notch);
    u32 dcp_phandle = fdt_get_phandle(dt, node("dcp"));
    const fdt32_t *regions = fdt_getprop(dt, node("dcp"), "memory-region", &len);
    assert(dcp_phandle && regions && len == (int)(4 * nsegments));
    for (u32 i = 0; i < nsegments; i++) {
        int n = resv(i);
        assert(n >= 0 && fdt_get_phandle(dt, n) == fdt32_ld(regions + i));
        assert(fdt_getprop(dt, n, "no-map", NULL));
        const fdt32_t *map = fdt_getprop(dt, n, "iommu-addresses", &len);
        if (segments[i].unk == 0xa) {
            /* __OS_LOG: host physical buffer, marker and no DART tuple. */
            assert(fdt_getprop(dt, n, "apple,dcp-os-log", NULL) && !map);
            assert(segments[i].remap == segments[i].phys);
            continue;
        }
        assert(!fdt_getprop(dt, n, "apple,dcp-os-log", NULL));
        assert(map && len >= 20 && fdt32_ld(map) == dcp_phandle);
        assert(fdt64_ld((const fdt64_t *)(map + 1)) == segments[i].remap);
        assert(fdt64_ld((const fdt64_t *)(map + 3)) == ALIGN_UP((u64)segments[i].size, SZ_16K));
    }
}
static u32 popcount(u32 v) { return __builtin_popcount(v); }
/* The published display handoff: framebuffer, SID0/SID4 links, markers, statuses. */
static void check_display_handoff(void) {
    int len;
    check_statuses();
    const char *targets[] = {"dcp", "disp0", "disp0_piodma"};
    for (u32 i = 0; i < 3; i++) {
        const fdt32_t *m = fdt_getprop(dt, node(targets[i]), MARKER, &len);
        assert(m && len == 4 && fdt32_ld(m) == 1);
    }
    /* framebuffer first, so the kernel's name lookup finds it at its index. */
    int fb = fb_resv();
    assert(fb >= 0 && !fdt_node_check_compatible(dt, fb, "framebuffer"));
    assert(fdt_getprop(dt, fb, "no-map", NULL) && !fdt_getprop(dt, fb, "iommu-addresses", NULL));
    const fdt64_t *reg = fdt_getprop(dt, fb, "reg", &len);
    assert(reg && len == 16 && fdt64_ld(reg) == vram[0] && fdt64_ld(reg + 1) == vram[1]);
    const fdt32_t *regions = fdt_getprop(dt, node("disp0"), "memory-region", &len);
    assert(regions && len == (int)(4 * (1 + popcount(layout[0]))));
    assert(fdt32_ld(regions) == fdt_get_phandle(dt, fb));
    assert(fdt_stringlist_count(dt, node("disp0"), "memory-region-names") == len / 4);
    assert(fdt_stringlist_search(dt, node("disp0"), "memory-region-names", "framebuffer") == 0);
    assert(proplen("disp0_piodma", "memory-region") == (int)(4 * popcount(layout[1])));
    assert(fdt_stringlist_count(dt, node("disp0_piodma"), "memory-region-names") ==
           (int)popcount(layout[1]));
    assert(!has("disp0", "apple,j613-inherited-mappings"));
    const char *aliases[] = {"disp0", "disp0_piodma"};
    for (u32 i = 0; i < nsegments; i++) {
        int n = resv(i);
        const fdt32_t *map = fdt_getprop(dt, n, "iommu-addresses", &len);
        int tuples = 1;
        for (u32 s = 0; s < 2; s++) {
            if (!(layout[s] & (1U << i))) continue;
            u32 ph = fdt_get_phandle(dt, node(aliases[s]));
            assert(map && len >= 20 * (tuples + 1) && fdt32_ld(map + 5 * tuples) == ph);
            assert(fdt64_ld((const fdt64_t *)(map + 5 * tuples + 1)) == segments[i].remap);
            tuples++;
        }
        /* Segment 3 (mapped by neither stream) stays linked to the DCP only. */
        if (segments[i].unk != 0xa) assert(len == 20 * tuples);
    }
}

int main(void) {
    FILE *f = fopen(DTB_PATH, "rb");
    assert(f);
    dtb_len = fread(dtb, 1, sizeof(dtb), f);
    fclose(f);
    assert(dtb_len > 0 && !fdt_check_header(dtb));
    (void)m3_dcp_board_j514s;

    /* A. Current linux-aurora DT: no DCP node. Reservation-only, never fatal. */
    reset_adt(); load(0x10000);
    assert(!fdt_delprop(dt, node("/aliases"), "dcp"));
    assert(!dt_set_t8122_display());
    check_reserved_only();
    assert(walks == 2);                                  /* read-only report ran */

    /* B. The earlier DT (display diagnostics node): dcp ABI only, no markers. */
    reset_adt(); load(0x10000);
    assert(!fdt_setprop_string(dt, node("disp0"), "compatible", "apple,t8122-display-diagnostics"));
    assert(!dt_set_t8122_display());
    check_dcp_handoff();
    assert(!strcmp(str("disp0", "status"), "disabled") && !has("disp0", "memory-region"));
    check_no_markers();
    assert(walks == 2);
    /* ... and a DT without the display subsystem. */
    reset_adt(); load(0x10000);
    assert(!fdt_delprop(dt, node("/aliases"), "disp0"));
    assert(!dt_set_t8122_display());
    check_dcp_handoff(); check_no_markers();

    /* C. The kernel's DT with the measured streams: the whole handoff. */
    reset_adt(); load(0x10000);
    assert(!dt_set_t8122_display());
    check_dcp_handoff(); check_display_handoff();
    assert(walks == 2);

    /* D. Anything off: the dcp ABI stays, no marker, the display untouched. */
    for (int mode = 0; mode < 25; mode++) {
        reset_adt(); load(0x10000);
        printf("-- D%d\n", mode);
        switch (mode) {
        case 0: case 1: case 2: case 3: case 4: fault = mode + 1; break;
        case 5: layout[0] |= 1U << 3; break;             /* segment 3 on SID0 */
        case 6: layout[1] |= 1U << 3; break;             /* segment 3 on SID4 */
        case 7: layout[0] &= ~(1U << 6); break;          /* SID0 misses segment 6 */
        case 8: layout[1] = 0; break;                    /* empty SID4 */
        case 9: layout[0] = LAYOUT1; layout[1] = LAYOUT0; break; /* streams swapped */
        case 10: layout[0] |= LAYOUT1; break;            /* segment 5 on both */
        case 11: strcpy(uuid, "00000000-0000-0000-0000-000000000000"); break;
        case 12: compat[1] = 8; compat[2] = 3; break;    /* not firmware-compat 14.7.0 */
        case 13:
            assert(!fdt_delprop(dt, node("dcp"), "apple,notch-height"));
            notch_expected = 0;
            break;
        case 14:
            assert(!fdt_setprop_u32(dt, node("dcp"), "apple,notch-height", 80));
            notch_expected = 80;
            break;
        case 15: adt_no_vram = 1; break;
        case 16: vram[0] += 2 * SZ_16K * 1024; break;    /* boot framebuffer outside /vram */
        case 17: assert(!fdt_setprop_string(dt, node("disp0"), "status", "okay")); break;
        case 18: assert(!fdt_setprop_string(dt, node(DART), "status", "okay")); break;
        case 19: assert(!fdt_setprop_string(dt, node("disp0_piodma"), "status", "disabled")); break;
        case 20: assert(!fdt_setprop_string(dt, node("/aliases"), "disp0_piodma",
                                             "/soc/display-subsystem")); break;
        case 21: assert(!fdt_setprop_u32(dt, node("disp0"), "iommus",
                                         fdt_get_phandle(dt, node(DART)))); break;
        case 22: {                                       /* display on stream 1 */
            fdt32_t v[2] = {cpu_to_fdt32(fdt_get_phandle(dt, node(DART))), cpu_to_fdt32(1)};
            assert(!fdt_setprop(dt, node("disp0"), "iommus", v, sizeof(v)));
            break;
        }
        case 23: {                                       /* PIODMA on the DCP DART */
            u32 ph = fdt_get_phandle(dt, node(DCP_DART));
            if (!ph) { ph = 0x7777; assert(!fdt_setprop_u32(dt, node(DCP_DART), "phandle", ph)); }
            fdt32_t v[2] = {cpu_to_fdt32(ph), cpu_to_fdt32(4)};
            assert(!fdt_setprop(dt, node("disp0_piodma"), "iommus", v, sizeof(v)));
            break;
        }
        case 24: assert(!fdt_setprop_u32(dt, node("disp0"), "memory-region", 1)); break;
        }
        assert(!dt_set_t8122_display());
        check_dcp_handoff();
        check_no_markers();
        assert(fb_resv() < 0);
        if (mode == 17) assert(!strcmp(str("disp0", "status"), "okay"));
        else if (mode == 18) assert(!strcmp(str(DART, "status"), "okay"));
        else if (mode == 19) assert(!strcmp(str("disp0_piodma", "status"), "disabled"));
        else if (mode == 20) continue;
        else if (mode == 24) assert(proplen("disp0", "memory-region") == 4);
        else check_display_untouched();
    }

    /* F. Rejected handoffs retain the original DT and fall back to reservations. */
    for (int mode = 0; mode < 8; mode++) {
        reset_adt(); load(0x10000);
        if (mode == 0) segments[1].phys += 0x1000;       /* misaligned segment */
        else if (mode == 1) segments[3].phys = dram[0] + dram[1];
        else if (mode == 2) adt_no_bounds = 1;
        else if (mode == 3) uuid[0] = 0;                 /* malformed UUID */
        else if (mode == 4) adt_no_nub = 1;
        else if (mode == 5) chip_id = T6030;
        else if (mode == 6) assert(!fdt_setprop_string(dt, 0, "compatible", "apple,j615"));
        else assert(!fdt_setprop_string(dt, node("dcp"), "compatible", "apple,t6030-dcp"));
        assert(!dt_set_t8122_display());
        check_display_untouched();
        if (mode == 4) { assert(!has("dcp", "apple,firmware-uuid") && resv(0) < 0); continue; }
        check_reserved_only();
    }

    /* G. Transactional: wherever the FDT runs out of space, the dcp ABI and the
     * display handoff (markers included) are each all or nothing.
     */
    int complete = 0, dcp_only = 0, none = 0;
    for (int room = 0; room <= 0x2000; room += 8) {
        reset_adt(); load(0x10000);
        assert(!fdt_pack(dt) && !fdt_open_into(dt, dt, fdt_totalsize(dt) + room));
        assert(!dt_set_t8122_display());
        int len = proplen("dcp", "memory-region");
        if (!has("dcp", "apple,firmware-uuid")) {
            assert(len < 0 && !has("dcp", "apple,firmware-compat"));
            check_display_untouched();
            none++;
            continue;
        }
        assert(len == (int)(4 * nsegments));
        check_dcp_handoff();
        if (has("dcp", MARKER)) {
            check_display_handoff();
            complete++;
        } else {
            check_display_untouched();
            dcp_only++;
        }
    }
    printf("room sweep: %d none, %d dcp only, %d complete\n", none, dcp_only, complete);
    assert(none && dcp_only && complete);

    printf("J613 DCP handoff: %u segments, UUID %s, SID0 0x%x SID4 0x%x, all checks passed\n",
           nsegments, uuid, LAYOUT0, LAYOUT1);
    return 0;
}
"""


def main():
    source_note = adt_values()
    for mask in (SID0, SID4, UNMAPPED):
        assert all(SEGMENTS[i][4] == 2 for i in range(len(SEGMENTS)) if mask >> i & 1), \
            "the measured stream masks name J613 scanout segments"

    fb = VRAM[0] + STRIDE * NOTCH
    board_dts = BOARD_DTS % {"fb": fb, "fb_hi": fb >> 32, "fb_lo": fb & 0xFFFFFFFF,
                             "fb_size": STRIDE * FB_HEIGHT}
    segs = ",\n".join("    {0x%x, 0x%x, 0x%x, 0x%x, 0x%x}" % s for s in SEGMENTS)
    functions = [extract(n) for n in ("dt_device_set_reserved_mem", "dt_get_or_add_reserved_mem",
                                      "dt_device_add_mem_region", "dt_get_iommu_node",
                                      "dt_reserve_asc_firmware", "dt_transaction")]
    start = SOURCE.index("struct m3_dcp_board {")
    functions.append(SOURCE[start:SOURCE.index("static int dt_set_display(void)", start)])

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dtsi = os.environ.get("J613_DCP_DTSI")
        dtb = os.environ.get("J613_DTB")
        (tmp / "dcp.dtsi").write_text(Path(dtsi).read_text() if dtsi else FIXTURE_DTSI)
        (tmp / "board.dts").write_text(board_dts)
        pre = subprocess.run(["cc", "-E", "-P", "-nostdinc", "-undef", "-x", "assembler-with-cpp",
                              "-I", str(tmp), str(tmp / "board.dts")],
                             check=True, capture_output=True, text=True).stdout
        (tmp / "board.pp.dts").write_text(pre)
        subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-@", "-o", str(tmp / "board.dtb"),
                        str(tmp / "board.pp.dts")], check=True)
        if dtb:
            (tmp / "board.dtb").write_bytes(Path(dtb).read_bytes())

        harness = HARNESS + "\n".join(functions) + MAIN
        defines = [
            "#define NSEG %d" % len(SEGMENTS),
            "static const struct adt_segment_ranges SEGS[] = {\n%s\n};" % segs,
            '#define UUID "%s"' % UUID,
            "#define DRAM0 0x%xULL\n#define DRAM1 0x%xULL" % DRAM,
            "#define VRAM0 0x%xULL\n#define VRAM1 0x%xULL" % VRAM,
            "#define NOTCH %d" % NOTCH,
            "#define FB_BASE 0x%xULL\n#define FB_SIZE 0x%xULL" % (fb, STRIDE * FB_HEIGHT),
            "#define LAYOUT0 0x%x\n#define LAYOUT1 0x%x" % (SID0, SID4),
            '#define DTB_PATH "%s"' % (tmp / "board.dtb"),
        ]
        harness = harness.replace("static unsigned char dtb", "\n".join(defines) + "\nstatic unsigned char dtb", 1)
        (tmp / "test.c").write_text(harness)
        subprocess.run(["cc", "-std=gnu11", "-Wall", "-Werror", str(tmp / "test.c"), "-lfdt",
                        "-o", str(tmp / "test")], check=True)
        out = subprocess.run(["stdbuf", "-oL", str(tmp / "test")], capture_output=True, text=True)
        if os.environ.get("J613_TEST_LOG"):
            Path(os.environ["J613_TEST_LOG"]).write_text(out.stdout)
        if out.returncode:
            print(out.stdout[-4000:], out.stderr)
            raise SystemExit("J613 DCP handoff test failed (%d)" % out.returncode)
        print(*[l for l in out.stdout.splitlines() if l.startswith(("room sweep", "J613 DCP handoff:"))],
              "(ADT: %s; DT: %s)" % (source_note, dtb or dtsi or "fixture"))


if __name__ == "__main__":
    main()
