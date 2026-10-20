/* SPDX-License-Identifier: MIT */
#ifndef DCP_T8152_DPTX_PORT_H
#define DCP_T8152_DPTX_PORT_H

#include "../types.h"

/* AppleT8152DPTXPort register block. base is the port MMIO base.
 * Shadow strobe, route, and the DCP output index are not in this file. */

int t8152_dptx_activate_phy(u64 base);
int t8152_dptx_phy_power_down(u64 base);
int t8152_dptx_set_active_lanes(u64 base, u32 lanes);
int t8152_dptx_calibrate(u64 base);
int t8152_dptx_shadow_update(u64 block0);
int t8152_dptx_program_rate(u64 port, u64 block0, u32 link_rate_mbps);

#endif
