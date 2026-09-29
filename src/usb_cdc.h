/* SPDX-License-Identifier: MIT */
#ifndef USB_CDC_H
#define USB_CDC_H

#include "types.h"

int usb_cdc_schedule(u32 delay_ms, u32 reserved, u32 flags);
u32 usb_cdc_status(void);
void usb_cdc_poll(void);
int usb_cdc_arm_watchdog(void);
void usb_cdc_primary_opened(void);
void usb_cdc_link_failed(void);

#endif
