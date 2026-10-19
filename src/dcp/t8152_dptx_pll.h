/* SPDX-License-Identifier: MIT */
#ifndef DCP_T8152_DPTX_PLL_H
#define DCP_T8152_DPTX_PLL_H

#include "../types.h"

/* T8152 configurePll fields from 26A434; link_rate is in Mbit/s.
 * The caller owns PHY power, calibration, and the subsequent shadow strobe. */
static inline int t8152_dptx_pll_configure_fields(u64 base, u32 link_rate)
{
    static const struct {
        u32 rate;
        u16 values[21];
    } configs[] = {
        {1620, {0x21c, 1, 1, 3, 0, 0, 1, 0, 0, 13, 0, 0x1a, 0x3c, 1, 0, 0, 1, 0, 0, 1, 1}},
        {2700, {0x1c2, 1, 1, 3, 0x1fff, 0x3ffe, 1, 0, 0, 13, 0, 0x1a, 0x32, 1, 0, 0, 1, 0, 0, 1, 1}},
        {5400, {0x1c2, 1, 1, 3, 0x1fff, 0x3ffe, 0, 0, 0, 9, 0, 0x1a, 0x32, 0, 0, 1, 0, 0, 1, 1, 1}},
        {8100, {0x2a3, 1, 1, 3, 0x2ffd, 0x3ffc, 0, 0, 0, 9, 0, 0x1a, 0x5a, 0, 0, 1, 0, 0, 1, 1, 1}},
    };
    static const struct {
        u16 offset;
        u8 shift;
        u32 mask;
    } fields[] = {
        {0x6c0, 0, 0x7ff}, {0x6c0, 11, 0x800}, {0x6c0, 13, 0x2000},
        {0x6c0, 18, 0x3c0000}, {0x6c4, 14, 0xfffc000}, {0x6c4, 0, 0x3fff},
        {0x6c8, 14, 0xc000}, {0x6c8, 8, 0x100}, {0x6c8, 0, 0xff},
        {0x6c8, 9, 0x3e00}, {0x6c8, 16, 0x10000}, {0x6c8, 26, 0xfc000000},
        {0x6cc, 0, 0x7f}, {0x6cc, 9, 0x600}, {0x6cc, 11, 0x7800},
        {0x6cc, 15, 0x38000}, {0x6cc, 20, 0x100000}, {0x6cc, 21, 0x200000},
        {0x6cc, 22, 0xc00000}, {0x6cc, 24, 0x3000000}, {0x6cc, 26, 0xc000000},
    };
    for (size_t rate = 0; rate < ARRAY_SIZE(configs); rate++) {
        if (configs[rate].rate != link_rate)
            continue;
        for (size_t i = 0; i < ARRAY_SIZE(fields); i++)
            mask32(base + fields[i].offset, fields[i].mask,
                   ((u32)configs[rate].values[i] << fields[i].shift) & fields[i].mask);
        return 0;
    }
    return -1;
}

#endif
