#!/usr/bin/env python3
"""Host-test the J613 (T8122) DCP handoff with real libfdt and the production kboot code.

The DT comes from a J613 fixture compiled by dtc (set J613_DCP_DTSI to use the
real t8122-j613-dcp.dtsi instead). The ADT values are the J613 os-fw 14.7
capture (set J613_ADT to a saved J613 ADT to read them from it instead). The
locked DART walk is injected; tests/kboot/test_locked_dart_walk.py covers it.
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

FIXTURE_DTSI = r"""
/ {
	aliases {
		dcp = &dcp;
		disp0 = &display;
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
	dcp: dcp@28ec00000 {
		compatible = "apple,t8122-dcp";
		mboxes = <&dcp_mbox>;
		iommus = <&dcp_dart 5>;
		reg = <0x2 0x8ec00000 0 0x4000>;
		power-domains = <&ps_disp_cpu>;
		status = "disabled";
		#address-cells = <2>;
		#size-cells = <2>;
		disp0_piodma: piodma {
			iommus = <&disp0_dart 4>;
			status = "disabled";
		};
	};
	display: display-subsystem {
		compatible = "apple,t8122-display-diagnostics";
		iommus = <&disp0_dart 0>;
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
    global SEGMENTS, UUID, DRAM
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
#define bail(...) do { printf(__VA_ARGS__); return -1; } while (0)
struct adt_segment_ranges { u64 phys, iova, remap; u32 size, unk; } __attribute__((packed));

static char tree[1 << 20];
static void *dt = tree, *adt;
static u32 chip_id = T8122;

/* ---- ADT stub: 1 = iop-dcp-nub, 2 = /chosen ---- */
static struct adt_segment_ranges segments[16];
static u32 nsegments;
static char uuid[64];
static u64 dram[2];
static int adt_no_nub, adt_no_bounds;
static int adt_path_offset(void *p, const char *path) {
    if (!strcmp(path, "/arm-io/dcp/iop-dcp-nub")) return adt_no_nub ? -1 : 1;
    if (!strcmp(path, "/chosen")) return 2;
    return -1;
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
    return 0;
}

static int dt_set_dcp_firmware(const char *alias) {
    int node = fdt_path_offset(dt, alias);
    if (node < 0) return 0;
    return fdt_setprop_string(dt, node, "apple,firmware-version", "14.7") ? -1 : 0;
}
"""

MAIN = r"""
static unsigned char dtb[1 << 16];
static size_t dtb_len;

static void load(int room) {
    assert(!fdt_open_into(dtb, tree, fdt_totalsize(dtb) + room));
}
static void reset_adt(void) {
    nsegments = NSEG;
    memcpy(segments, SEGS, sizeof(SEGS));
    strcpy(uuid, UUID);
    dram[0] = DRAM0; dram[1] = DRAM1;
    adt_no_nub = adt_no_bounds = 0;
    chip_id = T8122;
    fault = walks = 0;
    layout[0] = LAYOUT0; layout[1] = LAYOUT1;
}
static int node(const char *path) { return fdt_path_offset(dt, path); }
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
static const char *DART = "/soc/iommu@28d304000";

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
    assert(!strcmp(str("disp0_piodma", "status"), "disabled"));
    assert(!strcmp(str("disp0", "status"), "disabled"));
    assert(!strcmp(str(DART, "status"), "disabled"));
    assert(!has("disp0_piodma", "apple,j613-inherited-mappings"));
    assert(!has("disp0", "apple,j613-inherited-mappings"));
    assert(!has("disp0", "memory-region") && !has("disp0_piodma", "memory-region"));
}
/* The dcp link ABI the J613 m3_dcp patches consume. */
static void check_dcp_handoff(void) {
    assert(!strcmp(str("dcp", "apple,firmware-uuid"), uuid));
    assert(!strcmp(str("dcp", "apple,firmware-version"), "14.7"));
    assert(!strcmp(str("dcp", "status"), "disabled"));   /* status stays with the DT */
    u32 dcp_phandle = fdt_get_phandle(dt, node("dcp"));
    int len;
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
static void check_piodma(bool scanout) {
    assert(!strcmp(str("disp0_piodma", "status"), "okay"));
    assert(!strcmp(str(DART, "status"), "okay"));
    assert(has("disp0_piodma", "apple,j613-inherited-mappings"));
    assert(proplen("disp0_piodma", "memory-region") == (int)(4 * popcount(layout[1])));
    assert(proplen("disp0", "memory-region") == (int)(4 * popcount(layout[0])));
    assert(has("disp0", "apple,j613-inherited-mappings") == scanout);
    assert(!strcmp(str("disp0", "status"), scanout ? "okay" : "disabled"));
    const char *aliases[] = {"disp0", "disp0_piodma"};
    for (u32 i = 0; i < nsegments; i++) {
        int len, n = resv(i);
        const fdt32_t *map = fdt_getprop(dt, n, "iommu-addresses", &len);
        int tuples = 1;
        for (u32 s = 0; s < 2; s++) {
            if (!(layout[s] & (1U << i))) continue;
            u32 ph = fdt_get_phandle(dt, node(aliases[s]));
            assert(map && len >= 20 * (tuples + 1) && fdt32_ld(map + 5 * tuples) == ph);
            assert(fdt64_ld((const fdt64_t *)(map + 5 * tuples + 1)) == segments[i].remap);
            tuples++;
        }
        if (segments[i].unk != 0xa) assert(len == 20 * tuples);
    }
}
static void optin(bool scanout) {
    assert(!fdt_setprop_empty(dt, node("dcp"), "apple,j613-native-piodma"));
    if (scanout) assert(!fdt_setprop_empty(dt, node("dcp"), "apple,j613-native-scanout"));
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

    /* B. J613 DT without opt-in: dcp ABI published, display left alone. */
    reset_adt(); load(0x10000);
    assert(!dt_set_t8122_display());
    check_dcp_handoff(); check_display_untouched();
    assert(walks == 2);

    /* C/D. Opt-in with a qualified walk. */
    for (int scanout = 0; scanout < 2; scanout++) {
        reset_adt(); load(0x10000); optin(scanout);
        assert(!dt_set_t8122_display());
        check_dcp_handoff(); check_piodma(scanout);
    }
    /* Any stream layout covering all scanout segments qualifies on J613. */
    reset_adt(); layout[0] = ALT0; layout[1] = ALT1; load(0x10000); optin(true);
    assert(!dt_set_t8122_display());
    check_dcp_handoff(); check_piodma(true);

    /* E. Unqualified walks keep the DCP handoff but never enable PIODMA/scanout. */
    for (int mode = 0; mode < 8; mode++) {
        reset_adt(); load(0x10000); optin(true);
        if (mode < 4) fault = mode + 1;
        else if (mode == 4) layout[0] &= ~(1U << LAST);  /* uncovered scanout segment */
        else if (mode == 5) layout[1] = 0;               /* empty SID4 */
        else if (mode == 6) layout[0] = 0;               /* empty SID0 */
        else strcpy(uuid, "00000000-0000-0000-0000-000000000000"); /* unqualified firmware */
        assert(!dt_set_t8122_display());
        check_dcp_handoff(); check_display_untouched();
    }

    /* F. Rejected handoffs retain the original DT and fall back to reservations. */
    for (int mode = 0; mode < 8; mode++) {
        reset_adt(); load(0x10000); optin(true);
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
     * PIODMA/scanout publication are each all or nothing.
     */
    int complete = 0, dcp_only = 0, none = 0;
    for (int room = 0; room <= 0x2000; room += 8) {
        reset_adt(); load(0x10000); optin(true);
        assert(!fdt_pack(dt) && !fdt_open_into(dt, dt, fdt_totalsize(dt) + room));
        assert(!dt_set_t8122_display());
        int len = proplen("dcp", "memory-region");
        if (!has("dcp", "apple,firmware-uuid")) {
            assert(len < 0 && !has("dcp", "apple,firmware-version"));
            check_display_untouched();
            none++;
            continue;
        }
        assert(len == (int)(4 * nsegments));
        check_dcp_handoff();
        if (has("disp0_piodma", "apple,j613-inherited-mappings")) {
            check_piodma(true);
            complete++;
        } else {
            check_display_untouched();
            dcp_only++;
        }
    }
    printf("room sweep: %d none, %d dcp only, %d complete\n", none, dcp_only, complete);
    assert(none && dcp_only && complete);

    printf("J613 DCP handoff: %u segments, UUID %s, all checks passed\n", nsegments, uuid);
    return 0;
}
"""


def main():
    source_note = adt_values()
    unk2 = [i for i, s in enumerate(SEGMENTS) if s[4] == 2]
    assert len(unk2) >= 2, "J613 ADT is expected to describe several scanout segments"
    # Hypothetical J514S-shaped layout: the 16K segment on PIODMA, the rest on scanout.
    piodma = min(unk2, key=lambda i: SEGMENTS[i][3])
    layout0 = sum(1 << i for i in unk2 if i != piodma)
    layout1 = 1 << piodma
    alt0 = sum(1 << i for i in unk2[::2])
    alt1 = sum(1 << i for i in unk2[1::2])

    segs = ",\n".join("    {0x%x, 0x%x, 0x%x, 0x%x, 0x%x}" % s for s in SEGMENTS)
    functions = [extract(n) for n in ("dt_device_set_reserved_mem", "dt_get_or_add_reserved_mem",
                                      "dt_device_add_mem_region", "dt_get_iommu_node",
                                      "dt_reserve_asc_firmware", "dt_transaction")]
    start = SOURCE.index("struct m3_dcp_board {")
    functions.append(SOURCE[start:SOURCE.index("static int dt_set_display(void)", start)])

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        dtsi = os.environ.get("J613_DCP_DTSI")
        (tmp / "dcp.dtsi").write_text(Path(dtsi).read_text() if dtsi else FIXTURE_DTSI)
        (tmp / "board.dts").write_text(BOARD_DTS)
        pre = subprocess.run(["cc", "-E", "-P", "-nostdinc", "-undef", "-x", "assembler-with-cpp",
                              "-I", str(tmp), str(tmp / "board.dts")],
                             check=True, capture_output=True, text=True).stdout
        (tmp / "board.pp.dts").write_text(pre)
        subprocess.run(["dtc", "-q", "-I", "dts", "-O", "dtb", "-@", "-o", str(tmp / "board.dtb"),
                        str(tmp / "board.pp.dts")], check=True)

        harness = HARNESS + "\n".join(functions) + MAIN
        defines = [
            "#define NSEG %d" % len(SEGMENTS),
            "static const struct adt_segment_ranges SEGS[] = {\n%s\n};" % segs,
            '#define UUID "%s"' % UUID,
            "#define DRAM0 0x%xULL\n#define DRAM1 0x%xULL" % DRAM,
            "#define LAYOUT0 0x%x\n#define LAYOUT1 0x%x" % (layout0, layout1),
            "#define ALT0 0x%x\n#define ALT1 0x%x" % (alt0, alt1),
            "#define LAST %d" % unk2[-1] if (layout0 >> unk2[-1]) & 1 else "#define LAST %d" % unk2[0],
            '#define DTB_PATH "%s"' % (tmp / "board.dtb"),
        ]
        harness = harness.replace("static unsigned char dtb", "\n".join(defines) + "\nstatic unsigned char dtb", 1)
        (tmp / "test.c").write_text(harness)
        subprocess.run(["cc", "-std=gnu11", "-Wall", "-Werror", str(tmp / "test.c"), "-lfdt",
                        "-o", str(tmp / "test")], check=True)
        out = subprocess.run([str(tmp / "test")], check=True, capture_output=True, text=True).stdout
        print(*[l for l in out.splitlines() if l.startswith(("room sweep", "J613 DCP handoff:"))], "(ADT: %s; DT: %s)" % (source_note, dtsi or "fixture"))


if __name__ == "__main__":
    main()
