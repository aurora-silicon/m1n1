/* SPDX-License-Identifier: MIT */
#include "adt.h"
#include "exception.h"
#include "pcie.h"
#include "pmgr.h"
#include "smc.h"
#include "string.h"
#include "utils.h"

#include "libfdt/libfdt.h"

/* The qualified J700 radio contract exposes only function 0 to Linux. */
#define RADIO_HOST     "/soc/pcie@1cb0000000"
#define RADIO_DART     "/soc/iommu@390000000"
#define RADIO_PORT     RADIO_HOST "/pci@0,0"
#define RADIO_CONTRACT "retained-sid1-v1"

int pcie_t8140_disable_piodma(void *dt)
{
    static const char *const compatible[] = {
        "apple,t8140-piodma-diagnostic",
        "apple,t8140-pcie-piodma",
    };
    for (size_t i = 0; i < sizeof(compatible) / sizeof(*compatible); i++) {
        int node = -1;
        while ((node = fdt_node_offset_by_compatible(dt, node, compatible[i])) >= 0) {
            if (fdt_setprop_string(dt, node, "status", "disabled"))
                return -1;
        }
        if (node != -FDT_ERR_NOTFOUND)
            return -1;
    }
    return 0;
}

static int radio_delprop(void *dt, const char *path, const char *name)
{
    int ret = fdt_delprop(dt, fdt_path_offset(dt, path), name);
    return ret == -FDT_ERR_NOTFOUND ? 0 : ret;
}

static int radio_status(void *dt, const char *status)
{
    int chosen = fdt_path_offset(dt, "/chosen");
    return chosen < 0 ? -1 : fdt_setprop_string(dt, chosen, "apple,radio-handoff-status", status);
}

static bool radio_adt_policy(u64 *config, u64 *port, u64 *dart)
{
    int trace[8];
    u64 size;
    u32 streams;
    int node = adt_path_offset_trace(adt, "/arm-io/dart-apcie0", trace);
    if (node < 0 || !adt_is_compatible(adt, node, "dart,t8110") ||
        ADT_GETPROP(adt, node, "sid-count", &streams) < 0 || streams != 19 ||
        adt_get_reg(adt, trace, "reg", 0, dart, &size) || *dart != 0x390000000ULL ||
        size != 0x20000 || !adt_get_property(adt, node, "bypass-16") ||
        !adt_get_property(adt, node, "bypass-18") || adt_get_property(adt, node, "apf-bypass-16") ||
        adt_get_property(adt, node, "apf-bypass-18"))
        return false;
    ADT_FOREACH_PROPERTY(adt, node, prop)
    {
        if (!memchr(prop->name, 0, sizeof(prop->name)) || strstr(prop->name, "remap") ||
            strstr(prop->name, "exclave"))
            return false;
    }
    node = adt_path_offset_trace(adt, "/arm-io/apcie", trace);
    return node >= 0 && adt_is_compatible(adt, node, "apcie,t8140") &&
           !adt_get_reg(adt, trace, "reg", 0, config, &size) && *config == 0x1cb0000000ULL &&
           size >= 0x100004 && !adt_get_reg(adt, trace, "reg", 7, port, &size) &&
           *port == 0x390028000ULL && size >= 0x304c;
}

static int radio_enable_link(u64 port)
{
    int trace[8];
    u64 gpio, gpio_size, intr, intr_size;
    /* Match the board-qualified PERST#, CLKREQ# and Intr2AXI resources. */
    if (adt_path_offset_trace(adt, "/arm-io/gpio0", trace) < 0 ||
        adt_get_reg(adt, trace, "reg", 0, &gpio, &gpio_size) || gpio != 0x31a000000ULL ||
        gpio_size < 115 * 4 || adt_path_offset_trace(adt, "/arm-io/apcie", trace) < 0 ||
        adt_get_reg(adt, trace, "reg", 10, &intr, &intr_size) || intr != 0x390024000ULL ||
        intr_size < 0x84 || pmgr_power_on(0, "GPIO"))
        return -1;

    smc_dev_t *smc = smc_init();
    if (!smc)
        return -1;
    /* Assert active-low PERST# before the radio's pcIO power cycle. */
    mask32(gpio + 114 * 4, 0x7f, 2);
    mask32(gpio + 101 * 4, 0x260, 0x220);
    clear32(port + 0x82c, BIT(0));
    int ret = smc_write_u64(smc, 0x7063494f, 0x0008000000800000ULL);
    if (!ret) {
        udelay(2000);
        ret = smc_write_u64(smc, 0x7063494f, 0x0008000000800001ULL);
    }
    smc_shutdown(smc);
    if (ret)
        return -1;
    udelay(150000);
    set32(intr + 0x80, BIT(0));
    set32(port + 0x82c, BIT(0));
    mask32(gpio + 114 * 4, 0x7f, 3);
    udelay(100000);
    set32(port + 0x80, BIT(0));
    return poll32(port + 0x208, BIT(0), BIT(0), 250000);
}

int pcie_t8140_handoff(void *dt)
{
    u64 config, port, dart;
    int host, provider, len;
    const fdt32_t *map;
    u32 phandle;

    if (chip_id != T8140 || board_id != 0x64 || fdt_node_check_compatible(dt, 0, "apple,j700"))
        return 0;
    host = fdt_path_offset(dt, RADIO_HOST);
    if (host < 0)
        return 0;
    /* Only a kernel DTB implementing this experimental ownership contract opts in. */
    const char *contract = fdt_getprop(dt, host, "apple,j700-radio-handoff", &len);
    if (!contract && len == -FDT_ERR_NOTFOUND)
        return 0;
    if (!contract || len != sizeof(RADIO_CONTRACT) ||
        memcmp(contract, RADIO_CONTRACT, sizeof(RADIO_CONTRACT)))
        return radio_status(dt, "unsupported-contract") < 0 ? -1 : 1;
    provider = fdt_path_offset(dt, RADIO_DART);
    if (host < 0 || provider < 0 || fdt_node_check_compatible(dt, host, "apple,t8140-pcie") ||
        fdt_node_check_compatible(dt, provider, "apple,t8140-dart") ||
        fdt_path_offset(dt, RADIO_PORT "/wifi@0,0") < 0 || !radio_adt_policy(&config, &port, &dart))
        return radio_status(dt, "unsupported-policy") < 0 ? -1 : 1;
    phandle = fdt_get_phandle(dt, provider);
    map = fdt_getprop(dt, host, "iommu-map", &len);
    if (!phandle || !map || len != 4 * sizeof(*map) || fdt32_ld(&map[0]) != 0x100 ||
        fdt32_ld(&map[1]) != phandle || fdt32_ld(&map[2]) != 1 ||
        (fdt32_ld(&map[3]) != 1 && fdt32_ld(&map[3]) != 2))
        return radio_status(dt, "unsupported-map") < 0 ? -1 : 1;

    /* SID17 diagnostics must never share this SID1-only kernel handoff. */
    if (pcie_t8140_disable_piodma(dt))
        return -1;

    /* No broad DART reset: SID16/18 are firmware-owned, SID1 is Linux-owned. */
    if (read32(dart + 0x200) & BIT(0))
        return radio_status(dt, "dart-locked") < 0 ? -1 : 1;
    for (int sid = 16; sid <= 18; sid += 2) {
        if (read32(dart + 0x1000 + 4 * sid) > 2 || (read32(dart + 0x1400 + 4 * sid) & BIT(0)))
            return radio_status(dt, "firmware-translation-active") < 0 ? -1 : 1;
    }
    if (!(read32(port + 0x804) & BIT(0)))
        return radio_status(dt, "port-not-ready") < 0 ? -1 : 1;
    if (!(read32(port + 0x208) & BIT(0)) && radio_enable_link(port))
        return radio_status(dt, "link-not-up") < 0 ? -1 : 1;

    /* Route bus 1, then qualify its one typed ECAM read before exposing it. */
    write32(config + 0x18, 0x00010100);
    if (read32(config + 0x18) != 0x00010100)
        return radio_status(dt, "bus-routing-failed") < 0 ? -1 : 1;
    enum exc_guard_t previous = exc_guard;
    int before = exc_count;
    exc_guard = GUARD_MARK;
    sysop("dsb sy");
    u32 identity = read32(config + 0x100000);
    sysop("dsb sy");
    sysop("isb");
    exc_guard = previous;
    if (exc_count != before || identity != 0x793214c3 || !(read32(port + 0x208) & BIT(0)))
        return radio_status(dt, "ecam-identity-failed") < 0 ? -1 : 1;

    /* FDT edits move nodes: resolve their paths for every mutation. */
    fdt32_t range[] = {cpu_to_fdt32(0x100), cpu_to_fdt32(phandle), cpu_to_fdt32(1),
                       cpu_to_fdt32(1)};
    fdt32_t dma[] = {0, cpu_to_fdt32(0x4000), 0, cpu_to_fdt32(0xffff0000)};
    if (fdt_setprop(dt, fdt_path_offset(dt, RADIO_HOST), "iommu-map", range, sizeof(range)) ||
        fdt_setprop_u32(dt, fdt_path_offset(dt, RADIO_DART), "linux-enablement-mac,owned-streams",
                        BIT(1)) ||
        fdt_setprop_u32(dt, fdt_path_offset(dt, RADIO_DART),
                        "linux-enablement-mac,static-dart-bypass-test", BIT(16) | BIT(18)) ||
        fdt_setprop(dt, fdt_path_offset(dt, RADIO_DART), "apple,dma-range", dma, sizeof(dma)) ||
        radio_delprop(dt, RADIO_HOST, "apple,piodma") ||
        radio_delprop(dt, RADIO_HOST, "pinctrl-0") ||
        radio_delprop(dt, RADIO_HOST, "pinctrl-names") ||
        radio_delprop(dt, RADIO_PORT, "pwren-gpios") ||
        fdt_setprop(dt, fdt_path_offset(dt, RADIO_HOST), "apple,firmware-initialized", NULL, 0) ||
        radio_status(dt, "ready"))
        return -1;
    printf("pcie: J700 radio ECAM verified; Linux SID1, retained firmware SID16/18\n");
    return 0;
}
