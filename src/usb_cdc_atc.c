/* SPDX-License-Identifier: MIT */

#include "usb_cdc_atc.h"
#include "adt.h"
#include "string.h"
#include "tunables.h"
#include "utils.h"

/* T8130 ATC register facts from public Asahi Linux atc.c and the J700 ADT. */
#define USB2_CTL         0x04
#define USB2_SIG         0x08
#define USB2_MISCTUNE    0x1c
#define USB2_USBCTL      0x00
#define ATC_EVT_USB2_CTL 0x00
#define ATC_CFG0         0x08
#define ATC_LANE_MODE    0x60
#define ATC_CROSSBAR     0x64
#define ATC_RCAL         0x804
#define ATC_BIAS         0xa00
#define ATC_POWER_CTRL   0x20000
#define ATC_POWER_STAT   0x20004
#define ATC_MISC         0x20008

#define PIPE_OVERRIDE    0x00
#define PIPE_MUX         0x0c
#define PIPE_AON_GEN     0x1c
#define PIPE_NONSELECTED 0x20

struct atc_tuning_group {
    const char *name;
    u32 base;
    u32 span;
    bool required;
};

static const struct atc_tuning_group common_groups[] = {
    {"tunable_ATC_FABRIC", 0x44000, 0x4000, true},
    {"tunable_CIO3PLL_CORE", 0x2a00, 0x200, true},
    {"tunable_CIO3PLL_TOP", 0x2800, 0x200, true},
    {"tunable_ACIOPHY_LANE_USBC0", 0x5000, 0x1000, true},
    {"tunable_ACIOPHY_PLL_TOP", 0x1000, 0x4000, true},
    {"tunable_ACIOPHY_TOP", 0x0, 0x4000, true},
    {"tunable_AUSCMN_DIG", 0x800, 0x200, true},
    {"tunable_AUSPLL_CORE", 0x2200, 0x4000, true},
    {"tunable_AUX_TOP", 0x16000, 0x4000, true},
    {"tunable_AUSCMN_SHM", 0xa00, 0x200, true},
    {"tunable_CLKMON_CFG", 0x2600, 0x100, false},
};

static const struct atc_tuning_group lane_groups[2][5] = {
    {{"tunable_LN0_RX_TOP_USB_DFLT", 0x9000, 0x1000, true},
     {"tunable_LN0_RX_EQ_USB_EQA", 0xa000, 0x1000, true},
     {"tunable_LN0_RX_SHM_USB_DFLT", 0xb000, 0x1000, true},
     {"tunable_LN0_TX_TOP_USB_DFLT", 0xc000, 0x1000, true},
     {"tunable_LN0_TX_SHM_USB_DFLT", 0xd000, 0x1000, true}},
    {{"tunable_LN1_RX_TOP_USB_DFLT", 0x10000, 0x1000, true},
     {"tunable_LN1_RX_EQ_USB_EQA", 0x11000, 0x1000, true},
     {"tunable_LN1_RX_SHM_USB_DFLT", 0x12000, 0x1000, true},
     {"tunable_LN1_TX_TOP_USB_DFLT", 0x13000, 0x1000, true},
     {"tunable_LN1_TX_SHM_USB_DFLT", 0x14000, 0x1000, true}},
};

static uintptr_t core, pipe;
static bool phy_ready;
static bool force_swapped;
static int atc_node;
static u64 atc_core_size;
static unsigned active_lane;

void usb_cdc_atc_force_swapped(bool enable)
{
    force_swapped = enable;
}

static int apply_group(int node, uintptr_t window, u64 window_size,
                       const struct atc_tuning_group *group)
{
    u32 length;
    const u8 *records = adt_getprop(adt, node, group->name, &length);
    if (!records)
        return group->required ? -1 : 0;
    if (!length || length % 12 || window_size < 4 || group->span < 4 ||
        group->base > window_size - 4)
        return -1;
    return tunables_apply_compact_addr("/arm-io/atc-phy0", group->name, window + group->base,
                                       min((u64)group->span, window_size - group->base));
}

int usb_cdc_atc_power_on(uintptr_t pipehandler)
{
    int path[8];
    int node = adt_path_offset_trace(adt, "/arm-io/atc-phy0", path);
    u64 usb2, usb2_size, core_addr, core_size, axi, axi_size;
    if (node < 0 || adt_get_reg(adt, path, "reg", 0, &usb2, &usb2_size) < 0 ||
        adt_get_reg(adt, path, "reg", 4, &core_addr, &core_size) < 0 ||
        adt_get_reg(adt, path, "reg", 31, &axi, &axi_size) < 0 || usb2_size < 0x20 ||
        core_size < 0x48000 || axi_size < 0x8000)
        return -1;

    core = core_addr;
    pipe = pipehandler;
    phy_ready = false;
    atc_node = node;
    atc_core_size = core_size;

    set32(usb2 + USB2_SIG, 0xf);
    clear32(usb2 + USB2_SIG, 7 << 12); /* device role */
    udelay(10);
    clear32(usb2 + USB2_CTL, BIT(3)); /* SIDDQ */
    udelay(10);
    clear32(usb2 + USB2_CTL, BIT(0)); /* RESET */
    udelay(10);
    clear32(usb2 + USB2_CTL, BIT(1)); /* PORT_RESET */
    udelay(10);
    set32(core + ATC_EVT_USB2_CTL, BIT(0) | BIT(3));
    udelay(10);
    set32(usb2 + USB2_CTL, BIT(2)); /* APB_RESET_N */
    udelay(10);
    clear32(usb2 + USB2_MISCTUNE, BIT(29) | BIT(30));
    write32(usb2 + USB2_USBCTL, 2); /* RUN */

    /* Park upstream m1n1's DWC3 PIPE on the dummy PHY until DWC3 is ready. */
    write32(pipe + PIPE_MUX, 0x22);
    write32(pipe + PIPE_AON_GEN, BIT(0));
    write32(pipe + PIPE_NONSELECTED, 0x9332);

    set32(core + ATC_MISC, BIT(0));
    set32(core + ATC_POWER_CTRL, BIT(0));
    if (poll32(core + ATC_POWER_STAT, BIT(0), BIT(0), 100000))
        return -1;
    set32(core + ATC_POWER_CTRL, BIT(1));
    if (poll32(core + ATC_POWER_STAT, BIT(1), BIT(1), 100000))
        return -1;
    clear32(core + ATC_POWER_CTRL, BIT(2));
    set32(core + ATC_POWER_CTRL, BIT(3));

    struct atc_tuning_group axi_group = {"tunable_ATC0AXI2AF", 0, 0x8000, true};
    if (apply_group(node, axi, axi_size, &axi_group))
        return -1;
    for (size_t i = 0; i < ARRAY_SIZE(common_groups); i++) {
        if (apply_group(node, core, core_size, &common_groups[i]))
            return -1;
    }

    if (force_swapped)
        set32(core + ATC_MISC, BIT(2));
    unsigned lane = !!(read32(core + ATC_MISC) & BIT(2));
    active_lane = lane;
    for (size_t i = 0; i < ARRAY_SIZE(lane_groups[0]); i++) {
        if (apply_group(node, core, core_size, &lane_groups[lane][i]))
            return -1;
    }

    const u32 cfg_bits[] = {BIT(2), BIT(3), BIT(0), BIT(1)};
    for (size_t i = 0; i < ARRAY_SIZE(cfg_bits); i++) {
        set32(core + ATC_CFG0, cfg_bits[i]);
        udelay(10);
    }
    clear32(core + ATC_CFG0, BIT(4));
    udelay(10);
    set32(core + ATC_CFG0, BIT(5));
    udelay(10);
    set32(core + ATC_BIAS, BIT(1));
    udelay(10);

    u32 lane_mode = lane ? 0x252 : 0x489;
    u32 crossbar = 0x110 | lane;
    write32(core + ATC_LANE_MODE, lane_mode);
    write32(core + ATC_CROSSBAR, crossbar);
    set32(core + ATC_POWER_CTRL, BIT(4));
    if (poll32(core + ATC_RCAL, BIT(0), BIT(0), 100000))
        return -1;

    phy_ready = true;
    printf("CDC ATC: lane=%u mode=%x crossbar=%x rcal=%x\n", lane, lane_mode, crossbar,
           read32(core + ATC_RCAL));
    return 0;
}

int usb_cdc_atc_switch_pipe(void)
{
    if (!phy_ready || (read32(core + ATC_POWER_STAT) & 3) != 3 ||
        (read32(core + ATC_POWER_CTRL) & (BIT(3) | BIT(4))) != (BIT(3) | BIT(4)) ||
        !(read32(core + ATC_MISC) & BIT(0)) || !(read32(core + ATC_RCAL) & BIT(0)))
        return -1;

    if (force_swapped)
        set32(core + ATC_MISC, BIT(2));
    unsigned lane = !!(read32(core + ATC_MISC) & BIT(2));
    if (lane != active_lane) {
        /* The HPM may have changed orientation during a cable reconnect. */
        clear32(core + ATC_POWER_CTRL, BIT(4));
        for (size_t i = 0; i < ARRAY_SIZE(lane_groups[0]); i++) {
            if (apply_group(atc_node, core, atc_core_size, &lane_groups[lane][i]))
                return -1;
        }
        write32(core + ATC_LANE_MODE, lane ? 0x252 : 0x489);
        write32(core + ATC_CROSSBAR, 0x110 | lane);
        set32(core + ATC_POWER_CTRL, BIT(4));
        if (poll32(core + ATC_RCAL, BIT(0), BIT(0), 100000))
            return -1;
        active_lane = lane;
        printf("CDC ATC: cable orientation changed, lane=%u\n", lane);
    }

    mask32(pipe + PIPE_MUX, 7 << 3, 0);
    udelay(10);
    mask32(pipe + PIPE_MUX, 7, 0);
    udelay(10);
    mask32(pipe + PIPE_MUX, 7 << 3, BIT(3));
    udelay(10);
    clear32(pipe + PIPE_OVERRIDE, BIT(0) | BIT(2));
    return 0;
}
