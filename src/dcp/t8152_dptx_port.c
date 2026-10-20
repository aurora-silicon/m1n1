/* SPDX-License-Identifier: MIT */
/*
 * AppleT8152DPTXPort PHY sequences from the 26A434 crossbar disassembly.
 * Addresses are the kext runtime addresses in mac18g-t8152-hdmi-disasm.txt.
 * set_field matches vtable+0xa28 (discriminator 0x170e): offset, shift, mask, value.
 */
#include "t8152_dptx_port.h"

#include "../utils.h"

#include "t8152_dptx_pll.h"

#define T8152_LANE0  0x4500
#define T8152_LANE_STRIDE 0x2800
#define T8152_CAL_POLLS 100000

static void set_field(u64 base, u32 offset, u32 shift, u32 mask, u32 value)
{
    mask32(base + offset, mask, (value << shift) & mask);
}

/* AppleDPTXPort::handleActivate 0x91434bc, after handleSetTunables: configurePll 0x9138ed0
 * (0x8d0 bit 4: the M6 ADT has no pclkdrv-select), activatePll (+0x0 bit 1),
 * auxSetupCmnControl (0x1400 bit 0 = 0), activatePhy (0x1c00[9:4] = 0x3c, +0x0 bit 0),
 * IODelay(1). The DCP sets the lane count next; impedance calibration runs once, here.
 * The half-link bits belong to setLinkRate, not activation. */
int t8152_dptx_activate_phy(u64 base)
{
    set_field(base, 0x8d0, 4, 0x10, 1);
    set_field(base, 0, 1, 0x2, 1);
    set_field(base, 0x1400, 0, 0x1, 0);
    set_field(base, 0x1c00, 4, 0x3f0, 0x3c);
    set_field(base, 0, 0, 0x1, 1);
    udelay(1);
    return t8152_dptx_calibrate(base);
}

int t8152_dptx_phy_power_down(u64 base)
{
    /* 0xfffffe000913a354: clear bit 0 of 0x2400. */
    set_field(base, 0x2400, 0, 0x1, 0);
    return 0;
}

static u32 unused_lane_mask(u32 lanes)
{
    /* Const table at 0xfffffe00073cf020/030, simulated for 1..4:
     * 1 -> 0xe, 2 -> 0xc, 3 -> 0x8, 4 -> 0. */
    return (~((1u << lanes) - 1u)) & 0xf;
}

int t8152_dptx_set_active_lanes(u64 base, u32 lanes)
{
    static const struct {
        u16 off;
        u8 shift;
        u32 mask;
    } fields[] = {
        {0x64, 4, 0x10}, {0x24, 4, 0x10}, {0x24, 1, 0x2},  {0x48, 0, 0x1},
        {0x48, 1, 0x2},  {0x5c, 2, 0x4},  {0x5c, 3, 0x8}, {0x48, 4, 0x10},
    };
    u32 mask;
    u32 lane;

    if (lanes == 0)
        return 0;
    if (lanes > 4)
        return -1;

    mask = unused_lane_mask(lanes);
    /* 0xfffffe000913a454: drop bit 0 of 0x2400 before the per-lane writes. */
    set_field(base, 0x2400, 0, 0x1, 0);
    for (lane = 0; lane < 4; lane++) {
        u32 bit = (mask >> lane) & 1;
        u32 origin = T8152_LANE0 + lane * T8152_LANE_STRIDE;
        size_t i;

        for (i = 0; i < ARRAY_SIZE(fields); i++)
            set_field(base, origin + fields[i].off, fields[i].shift, fields[i].mask, bit);
    }
    /* 0xfffffe000913ab80 and 0xfffffe000913abb4. */
    set_field(base, 0x2420, 0, 0xf, mask);
    set_field(base, 0x2400, 0, 0x1, 1);
    return 0;
}

int t8152_dptx_calibrate(u64 base)
{
    u32 polls;

    /* 0xfffffe000913a070. Bit 0 of 0x1c04 is the done flag.
     * 0xfffffe00091561f8 is _IODelay. The poll passes 10, so the wait is 10 us. */
    if (!(read32(base + 0x1c04) & 1)) {
        for (polls = 0; polls < T8152_CAL_POLLS; polls++) {
            udelay(10);
            if (read32(base + 0x1c04) & 1)
                break;
        }
        if (polls == T8152_CAL_POLLS)
            return -1;
    }

    set_field(base, 0x1c00, 4, 0x3f0, 0x3f);
    set_field(base, 0x1c18, 15, 0x3f8000, (read32(base + 0x1c24) >> 6) & 0x7f);
    set_field(base, 0x1c18, 14, 0x4000, 1);
    return 0;
}

int t8152_dptx_shadow_update(u64 block0)
{
    /* 0xfffffe000912041c. Block0+0xa0: set 8, set 4, set/clear 0x40000,
     * set/clear 0x80000, clear 4, clear 8. */
    u64 reg = block0 + 0xa0;

    mask32(reg, 0x8, 0x8);
    mask32(reg, 0x4, 0x4);
    mask32(reg, 0x40000, 0x40000);
    mask32(reg, 0x40000, 0);
    mask32(reg, 0x80000, 0x80000);
    mask32(reg, 0x80000, 0);
    mask32(reg, 0x4, 0);
    mask32(reg, 0x8, 0);
    return 0;
}

/* pllPowerDown 0x9139d68 / pllPowerUp 0x91398e8: request (0x408 bit 6, bits 2:0 = 0 or 7,
 * bit 3), poll bit 19 up to 10 times 16 us apart, drop bits 3 and 6. Power-up then requires
 * 0x438 bit 30. */
static int t8152_dptx_pll_power(u64 base, bool up)
{
    set_field(base, 0x408, 6, 0x40, 1);
    set_field(base, 0x408, 0, 0x7, up ? 7 : 0);
    set_field(base, 0x408, 3, 0x8, 1);
    u32 polls = 0;
    do
        udelay(16);
    while (!(read32(base + 0x408) & BIT(19)) && ++polls < 10);
    bool done = read32(base + 0x408) & BIT(19);
    set_field(base, 0x408, 3, 0x8, 0);
    set_field(base, 0x408, 6, 0x40, 0);
    u32 r438 = read32(base + 0x438);
    if (up)
        done = done && (r438 & BIT(30));
    printf("dptx: pll power-%s %s after %u polls, 0x408 %x 0x438 %x\n", up ? "up" : "down",
           done ? "ok" : "FAILED", polls, read32(base + 0x408), r438);
    return done ? 0 : -1;
}

/* AppleDPTXPort::setLinkRate 0x9154768: phyPowerDown (0x2400 bit 0 = 0), pllPowerDown,
 * pllConfigureFrequency (21 fields, shadow pulse, UHBR bit 0x2400[2]), phy/pllSetHalfLinkRate
 * (0x2400 bit 3 and +0x0 bit 17 = !half, half = rate > 2.5 Gb/s), pllPowerUp. No calibration;
 * the DCP's next SetActiveLaneCount sets 0x2400 bit 0 again. */
int t8152_dptx_program_rate(u64 port, u64 block0, u32 link_rate_mbps)
{
    if (!block0)
        return -1;
    if (link_rate_mbps != 1620 && link_rate_mbps != 2700 && link_rate_mbps != 5400 &&
        link_rate_mbps != 8100)
        return -1;
    u32 full = link_rate_mbps < 2500;
    set_field(port, 0x2400, 0, 0x1, 0);
    t8152_dptx_pll_power(port, false);
    t8152_dptx_pll_configure_fields(port, link_rate_mbps);
    t8152_dptx_shadow_update(block0);
    set_field(port, 0x2400, 2, 0x4, 0);
    set_field(port, 0x2400, 3, 0x8, full);
    set_field(port, 0, 17, 0x20000, full);
    return t8152_dptx_pll_power(port, true);
}
