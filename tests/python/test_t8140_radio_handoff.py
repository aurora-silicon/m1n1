"""Exercise the production radio handoff with real libfdt and modeled hardware."""
from pathlib import Path
import subprocess


def test_radio_handoff(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/pcie_t8140.c").read_text()
    source = "\n".join(line for line in source.splitlines() if not line.startswith("#include"))
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "libfdt.h"
typedef uint32_t u32;
typedef uint64_t u64;
#define BIT(n) (1UL << (n))
#define T8140 0x8140
#define sysop(x) ((void)0)
#define ADT_GETPROP(a,n,p,v) adt_copy(n,p,v)
struct adt_property { char name[32]; };
static struct adt_property properties[] = {{"bypass-16"}, {"bypass-18"}};
#define ADT_FOREACH_PROPERTY(a,n,p) \
    for (struct adt_property *p = properties; p < properties + 2; p++)
enum exc_guard_t { GUARD_OFF, GUARD_SKIP, GUARD_MARK };
static enum exc_guard_t exc_guard;
static int exc_count, chip_id = T8140, board_id = 0x64;
static void *adt;
static int bad_policy, accesses, writes, fault;
static int smc_writes, fail_link, fail_smc, fail_route, sequence;
static u32 buses, identity = 0x793214c3, tcr, ttbr, locked, link = 1;
static u32 sid_count = 19;
static int adt_path_offset_trace(void *a, const char *path, int *trace)
{
    (void)a;
    trace[0] = strstr(path, "dart") ? 1 : strstr(path, "gpio") ? 3 : 2;
    return trace[0];
}
static bool adt_is_compatible(void *a, int node, const char *compat)
{ (void)a; (void)node; (void)compat; return true; }
static int adt_copy(int node, const char *prop, u32 *out)
{ (void)node; (void)prop; *out = sid_count; return 4; }
static const void *adt_get_property(void *a, int node, const char *prop)
{
    (void)a; (void)node;
    return !strncmp(prop, "bypass-", 7) && !bad_policy ? properties : NULL;
}
static int adt_get_reg(void *a, int *trace, const char *prop, int idx, u64 *base, u64 *size)
{
    (void)a; (void)prop;
    *base = trace[0] == 1 ? 0x390000000ULL : idx ? 0x390028000ULL : 0x1cb0000000ULL;
    *size = trace[0] == 1 ? 0x20000 : idx ? 0x8000 : 0x10000000;
    if (trace[0] == 3) *base = 0x31a000000ULL;
    if (idx == 10) *base = 0x390024000ULL;
    return 0;
}
static u32 read32(u64 addr)
{
    accesses++;
    if (addr == 0x390000200ULL) return locked;
    if (addr == 0x390001040ULL || addr == 0x390001048ULL) return tcr;
    if (addr == 0x390001440ULL || addr == 0x390001448ULL) return ttbr;
    if (addr == 0x390028804ULL) return 1;
    if (addr == 0x390028208ULL) return link;
    if (addr == 0x1cb0000018ULL) return fail_route ? ~buses : buses;
    assert(addr == 0x1cb0100000ULL && buses == 0x10100);
    assert(exc_guard == GUARD_MARK);
    if (fault) exc_count++;
    return identity;
}
static void write32(u64 addr, u32 value)
{ assert(addr == 0x1cb0000018ULL); writes++; buses = value; }
typedef int smc_dev_t;
static int pmgr_power_on(int die, const char *name)
{ assert(!die && !strcmp(name, "GPIO")); return 0; }
static smc_dev_t smc;
static smc_dev_t *smc_init(void) { return fail_smc == 1 ? NULL : &smc; }
static void smc_shutdown(smc_dev_t *dev) { assert(dev == &smc); }
static int smc_write_u64(smc_dev_t *dev, u32 key, u64 value)
{
    assert(dev == &smc && key == 0x7063494f);
    assert(value == 0x0008000000800000ULL + smc_writes);
    assert(sequence == (smc_writes ? 5 : 3));
    sequence++;
    smc_writes++; return fail_smc == 2 ? -1 : 0;
}
static void udelay(unsigned int delay)
{
    assert((sequence == 4 && delay == 2000) || (sequence == 6 && delay == 150000) ||
           (sequence == 10 && delay == 100000));
    sequence++;
}
static void mask32(u64 addr, u32 mask, u32 value)
{
    assert(addr == 0x31a000000ULL + 114 * 4 || addr == 0x31a000000ULL + 101 * 4);
    assert((value & mask) == value);
    assert((sequence == 0 && addr == 0x31a0001c8ULL && mask == 0x7f && value == 2) ||
           (sequence == 1 && addr == 0x31a000194ULL && mask == 0x260 && value == 0x220) ||
           (sequence == 9 && addr == 0x31a0001c8ULL && mask == 0x7f && value == 3));
    sequence++;
}
static void clear32(u64 addr, u32 mask)
{ assert(addr == 0x39002882cULL && mask == 1 && sequence == 2); sequence++; }
static void set32(u64 addr, u32 mask)
{
    assert(addr == 0x390024080ULL || addr == 0x39002882cULL || addr == 0x390028080ULL);
    assert(mask == 1);
    assert((sequence == 7 && addr == 0x390024080ULL) ||
           (sequence == 8 && addr == 0x39002882cULL) ||
           (sequence == 11 && addr == 0x390028080ULL));
    sequence++;
}
static int poll32(u64 addr, u32 mask, u32 value, unsigned int timeout)
{
    assert(addr == 0x390028208ULL && mask == 1 && value == 1 && timeout == 250000 && sequence == 12);
    link = !fail_link; return fail_link ? -1 : 0;
}
''' + source + r'''
static void *dt;
static unsigned char blob[8192];
static void add(const char *parent, const char *name)
{ assert(fdt_add_subnode(dt, fdt_path_offset(dt, parent), name) >= 0); }
static void reset(void)
{
    dt = blob;
    assert(!fdt_create_empty_tree(dt, sizeof(blob)));
    assert(!fdt_setprop_string(dt, 0, "compatible", "apple,j700"));
    add("/", "chosen"); add("/", "soc");
    add("/soc", "pcie@1cb0000000"); add("/soc", "iommu@390000000");
    add(RADIO_HOST, "pci@0,0"); add(RADIO_PORT, "wifi@0,0");
    assert(!fdt_setprop_string(dt, fdt_path_offset(dt, RADIO_HOST), "compatible", "apple,t8140-pcie"));
    assert(!fdt_setprop_string(dt, fdt_path_offset(dt, RADIO_HOST), "apple,j700-radio-handoff", RADIO_CONTRACT));
    assert(!fdt_setprop_string(dt, fdt_path_offset(dt, RADIO_DART), "compatible", "apple,t8140-dart"));
    assert(!fdt_setprop_u32(dt, fdt_path_offset(dt, RADIO_DART), "phandle", 7));
    fdt32_t map[] = {cpu_to_fdt32(0x100), cpu_to_fdt32(7), cpu_to_fdt32(1), cpu_to_fdt32(2)};
    assert(!fdt_setprop(dt, fdt_path_offset(dt, RADIO_HOST), "iommu-map", map, sizeof(map)));
    assert(!fdt_setprop_u32(dt, fdt_path_offset(dt, RADIO_HOST), "apple,piodma", 8));
    assert(!fdt_setprop_u32(dt, fdt_path_offset(dt, RADIO_PORT), "pwren-gpios", 9));
    assert(!fdt_setprop_u32(dt, fdt_path_offset(dt, RADIO_HOST), "pinctrl-0", 10));
    assert(!fdt_setprop_string(dt, fdt_path_offset(dt, RADIO_HOST), "pinctrl-names", "default"));
    add("/", "dma-controller@390030000");
    assert(!fdt_setprop_string(dt, fdt_path_offset(dt, "/dma-controller@390030000"),
                              "compatible", "apple,t8140-piodma-diagnostic"));
    accesses = writes = buses = bad_policy = fault = locked = tcr = ttbr = smc_writes = fail_link = 0;
    fail_smc = fail_route = sequence = 0;
    identity = 0x793214c3; link = 0; chip_id = T8140; board_id = 0x64; sid_count = 19;
    strcpy(properties[0].name, "bypass-16"); strcpy(properties[1].name, "bypass-18");
    exc_guard = GUARD_OFF;
}
static void rejected(const char *reason)
{
    assert(pcie_t8140_handoff(dt) == 1);
    assert(!strcmp(fdt_getprop(dt, fdt_path_offset(dt, "/chosen"), "apple,radio-handoff-status", NULL), reason));
    assert(!fdt_getprop(dt, fdt_path_offset(dt, RADIO_HOST), "apple,firmware-initialized", NULL));
    assert(fdt_getprop(dt, fdt_path_offset(dt, RADIO_HOST), "apple,piodma", NULL));
    assert(!fdt_getprop(dt, fdt_path_offset(dt, RADIO_DART), "linux-enablement-mac,owned-streams", NULL));
    assert(exc_guard == GUARD_OFF);
}
int main(void)
{
    reset(); assert(!pcie_t8140_handoff(dt));
    assert(writes == 1 && smc_writes == 2 && exc_guard == GUARD_OFF);
    const fdt32_t *map = fdt_getprop(dt, fdt_path_offset(dt, RADIO_HOST), "iommu-map", NULL);
    assert(fdt32_ld(map + 1) == 7 && fdt32_ld(map + 2) == 1 && fdt32_ld(map + 3) == 1);
    assert(fdt_getprop(dt, fdt_path_offset(dt, RADIO_HOST), "apple,firmware-initialized", NULL));
    assert(!fdt_getprop(dt, fdt_path_offset(dt, RADIO_HOST), "apple,piodma", NULL));
    assert(!fdt_getprop(dt, fdt_path_offset(dt, RADIO_PORT), "pwren-gpios", NULL));
    assert(!fdt_getprop(dt, fdt_path_offset(dt, RADIO_HOST), "pinctrl-0", NULL));
    assert(!fdt_getprop(dt, fdt_path_offset(dt, RADIO_HOST), "pinctrl-names", NULL));
    assert(!strcmp(fdt_getprop(dt, fdt_path_offset(dt, "/dma-controller@390030000"), "status", NULL), "disabled"));
    const fdt32_t *mask = fdt_getprop(dt, fdt_path_offset(dt, RADIO_DART), "linux-enablement-mac,owned-streams", NULL);
    assert(fdt32_ld(mask) == BIT(1));
    mask = fdt_getprop(dt, fdt_path_offset(dt, RADIO_DART), "linux-enablement-mac,static-dart-bypass-test", NULL);
    assert(fdt32_ld(mask) == (BIT(16) | BIT(18)));
    reset(); bad_policy = 1; rejected("unsupported-policy"); assert(!accesses);
    reset(); locked = 1; rejected("dart-locked"); assert(!writes);
    reset(); tcr = 0x82; rejected("firmware-translation-active"); assert(!writes);
    reset(); ttbr = 1; rejected("firmware-translation-active"); assert(!writes);
    reset(); link = 0; assert(!pcie_t8140_handoff(dt) && smc_writes == 2);
    reset(); link = 0; fail_link = 1; rejected("link-not-up"); assert(!writes);
    reset(); identity = ~0U; rejected("ecam-identity-failed");
    reset(); fault = 1; rejected("ecam-identity-failed");
    reset(); chip_id = 0x8103; assert(!pcie_t8140_handoff(dt) && !accesses);
    reset(); board_id = 0x65; assert(!pcie_t8140_handoff(dt) && !accesses);
    /* Public DTBs retain their original path until they explicitly opt in. */
    reset(); assert(!radio_delprop(dt, RADIO_HOST, "apple,j700-radio-handoff"));
    unsigned char original[sizeof(blob)]; memcpy(original, blob, sizeof(blob));
    assert(!pcie_t8140_handoff(dt) && !accesses && !writes && !smc_writes);
    assert(!memcmp(original, blob, sizeof(blob)));
    reset(); assert(!fdt_setprop_string(dt, fdt_path_offset(dt, RADIO_HOST), "apple,j700-radio-handoff", "unknown-v2"));
    rejected("unsupported-contract"); assert(!accesses);
    reset(); assert(!fdt_setprop_string(dt, 0, "compatible", "apple,other"));
    assert(!pcie_t8140_handoff(dt) && !accesses);
    /* Missing experimental properties and already-retained maps are valid. */
    reset(); assert(!radio_delprop(dt, RADIO_HOST, "apple,piodma"));
    assert(!pcie_t8140_handoff(dt));
    reset(); assert(!radio_delprop(dt, RADIO_PORT, "pwren-gpios"));
    assert(!pcie_t8140_handoff(dt));
    reset(); assert(!radio_delprop(dt, RADIO_HOST, "pinctrl-0"));
    assert(!radio_delprop(dt, RADIO_HOST, "pinctrl-names"));
    assert(!pcie_t8140_handoff(dt));
    reset(); fdt32_t retained[] = {cpu_to_fdt32(0x100), cpu_to_fdt32(7), cpu_to_fdt32(1), cpu_to_fdt32(1)};
    assert(!fdt_setprop(dt, fdt_path_offset(dt, RADIO_HOST), "iommu-map", retained, sizeof(retained)));
    assert(!pcie_t8140_handoff(dt));
    reset(); link = 0; fail_smc = 1; rejected("link-not-up"); assert(!writes && !smc_writes);
    reset(); link = 0; fail_smc = 2; rejected("link-not-up"); assert(!writes && smc_writes == 1);
    reset(); fail_route = 1; rejected("bus-routing-failed");
    reset(); link = 1; assert(!pcie_t8140_handoff(dt) && !smc_writes);
    reset(); sid_count = 18; rejected("unsupported-policy"); assert(!accesses);
    reset(); strcpy(properties[0].name, "remap-16"); rejected("unsupported-policy"); assert(!accesses);
    reset(); strcpy(properties[0].name, "exclave"); rejected("unsupported-policy"); assert(!accesses);
    return 0;
}
'''
    binary = tmp_path / "radio-handoff"
    libfdt = repo / "src/libfdt"
    sources = [libfdt / name for name in (
        "fdt.c", "fdt_addresses.c", "fdt_check.c", "fdt_empty_tree.c",
        "fdt_ro.c", "fdt_rw.c", "fdt_strerror.c", "fdt_sw.c", "fdt_wip.c",
    )]
    subprocess.run(["cc", "-D_GNU_SOURCE", "-std=c11", "-Wall", "-Wextra", "-Werror",
                    "-I", str(libfdt), "-x", "c", "-", *map(str, sources), "-o", str(binary)],
                   input=harness, text=True, check=True)
    subprocess.run([str(binary)], check=True)
