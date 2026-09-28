/* SPDX-License-Identifier: MIT */
#ifndef USB_CDC_STATE_H
#define USB_CDC_STATE_H

#ifdef CDC_STATE_HOST_TEST
#include <stdbool.h>
#include <stdint.h>
typedef uint32_t u32;
typedef uint64_t u64;
#else
#include "types.h"
#endif

enum usb_cdc_state_id {
    USB_CDC_IDLE,
    USB_CDC_SCHEDULED,
    USB_CDC_DWC_READY,
    USB_CDC_FAILED,
};

struct usb_cdc_state {
    enum usb_cdc_state_id state;
    u32 flags;
    u32 step;
    u64 deadline_ms;
};

int usb_cdc_state_schedule(struct usb_cdc_state *cdc, u32 delay_ms, u32 reserved, u32 flags,
                           u64 now_ms);
bool usb_cdc_state_due(const struct usb_cdc_state *cdc, u64 now_ms);
u32 usb_cdc_state_status(const struct usb_cdc_state *cdc);

#endif
