/* SPDX-License-Identifier: MIT */
#ifndef DCP_ATC_PHY_H
#define DCP_ATC_PHY_H

#include "../types.h"

typedef struct atc_phy atc_phy_t;
struct dptx_drive_settings;

/* T8152/J873g ATC3, direct four-lane DisplayPort only. */
atc_phy_t *atc_phy_init(const char *path);
int atc_phy_activate(atc_phy_t *phy);
int atc_phy_set_link_rate(atc_phy_t *phy, u32 rate);
int atc_phy_set_route(atc_phy_t *phy, bool enable);
int atc_phy_set_drive_settings(atc_phy_t *phy, const struct dptx_drive_settings *settings,
                               u32 count);
int atc_phy_deactivate(atc_phy_t *phy);
void atc_phy_shutdown(atc_phy_t *phy);

#endif
