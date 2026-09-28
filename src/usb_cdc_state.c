/* SPDX-License-Identifier: MIT */

#include "usb_cdc_state.h"

int usb_cdc_state_schedule(struct usb_cdc_state *cdc, u32 delay_ms, u32 reserved, u32 flags,
                           u64 now_ms)
{
    /* Gen1 is the only qualified descriptor and controller mode in this build. */
    if (!cdc || cdc->state != USB_CDC_IDLE || delay_ms < 1000 || delay_ms > 15000 || reserved ||
        (flags & ~7u) || !(flags & 2u))
        return -1;
    cdc->flags = flags;
    cdc->step = 0;
    cdc->deadline_ms = now_ms + delay_ms;
    cdc->state = USB_CDC_SCHEDULED;
    return 0;
}

bool usb_cdc_state_due(const struct usb_cdc_state *cdc, u64 now_ms)
{
    return cdc && cdc->state == USB_CDC_SCHEDULED && now_ms >= cdc->deadline_ms;
}

u32 usb_cdc_state_status(const struct usb_cdc_state *cdc)
{
    return cdc ? (u32)cdc->state | (cdc->step << 8) : USB_CDC_FAILED;
}

void usb_cdc_recovery_arm(struct usb_cdc_recovery *recovery, u64 now_ms)
{
    if (!recovery || recovery->pending || recovery->attempts >= 3)
        return;
    recovery->pending = true;
    recovery->deadline_ms = now_ms + 25;
}

void usb_cdc_recovery_connected(struct usb_cdc_recovery *recovery)
{
    if (!recovery)
        return;
    recovery->pending = false;
    recovery->attempts = 0;
    recovery->deadline_ms = 0;
}

bool usb_cdc_recovery_due(const struct usb_cdc_recovery *recovery, u64 now_ms)
{
    return recovery && recovery->pending && now_ms >= recovery->deadline_ms;
}

void usb_cdc_recovery_attempted(struct usb_cdc_recovery *recovery, u64 now_ms)
{
    if (!recovery || !recovery->pending)
        return;
    recovery->attempts++;
    if (recovery->attempts >= 3)
        recovery->pending = false;
    else
        recovery->deadline_ms = now_ms + 2000;
}

bool usb_cdc_dma_may_release(bool active, int end_status, bool reset_complete)
{
    return !active || end_status == 0 || reset_complete;
}
