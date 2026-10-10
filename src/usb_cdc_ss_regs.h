/* SPDX-License-Identifier: MIT */
#ifndef USB_CDC_SS_REGS_H
#define USB_CDC_SS_REGS_H

/* DWC3 device receive capacity fields, defined from the public register map. */
#define CDC_DCFG_NUMP_MASK      (0x1f << 17)
#define CDC_DCFG_NUMP(value)    ((value) << 17)
#define CDC_GRXTHRCFG_PKTCNTSEL (1u << 29)

#endif
