/* SPDX-License-Identifier: MIT */

#ifndef HV_AIC_ALIAS_H
#define HV_AIC_ALIAS_H

#include "types.h"

/*
 * Published-GSIV <-> physical-AIC-line aliasing for the vGIC carrier.
 *
 * Windows' ARM64 PnP interrupt arbiter only accepts GSIVs inside the GIC
 * architectural SPI range.  Measured on J813 against the live arbiter:
 *
 *     type=2 lines=[32,1024) gsi=32 states=992
 *     IRQ GSIV 1277: unmapped
 *
 * so a device whose physical AIC line is >= 1020 cannot be published under its
 * real number.  A devnode that tries comes up
 * `state=DriversAdded problem=12` (CM_PROB_NO_VALID_LOG_CONFIG) and its driver
 * is never loaded.  Note 1020..1023 are architecturally special and
 * 1024..4095 are reserved, so no widening of the vGIC makes such a line legal
 * -- it has to be renumbered.
 *
 * On J414s that renumbering is done in firmware, by the "ALI2" tail of the
 * AIC2 CSRT, and applied by the Windows HAL extension after it takes over the
 * real AIC.  J813 has neither: its CSRT.aslc is an unbuilt stub, so
 * HalExtAppleInterruptController never loads and the guest stays on m1n1's
 * emulated GICv3 carrier for its whole life.  The boot log shows the carrier
 * coming up and never shows "Windows enabled AIC2 CONFIG".
 *
 * While the guest is on that carrier, m1n1 is the only thing that translates
 * between AIC lines and guest INTIDs -- it acks a physical AIC event and
 * injects the same number as a vINTID -- so m1n1 is where the renumbering
 * belongs.  This is deliberately the same shape as the firmware table
 * (published <-> physical, both directions) so the two cannot disagree about
 * direction if J813 ever grows a real CSRT.
 */

/* Guest INTID -> physical AIC line.  Identity when no alias covers it. */
u32 hv_aic_alias_to_physical(u32 published);

/* Physical AIC line -> guest INTID.  Identity when no alias covers it. */
u32 hv_aic_alias_to_published(u32 physical);

#endif
