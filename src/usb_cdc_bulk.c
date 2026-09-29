/* SPDX-License-Identifier: MIT */

#include "usb_cdc_bulk.h"

int usb_cdc_nump(u32 hwparams0, u32 hwparams7)
{
    u32 width_bits = (hwparams0 >> 8) & 0xff;
    u32 ram2 = hwparams7 >> 16;
    if (!width_bits || (width_bits & 7) || !ram2)
        return -1;
    u32 capacity = ram2 * (width_bits / 8);
    if (capacity <= 40)
        return -1;
    u32 nump = (capacity - 40) / 1024;
    if (!nump)
        return -1;
    return nump > 16 ? 16 : nump;
}

size_t usb_cdc_bulk_plan(u32 length, u32 *segments, size_t capacity)
{
    if (!segments || !capacity || length > CDC_BULK_CHAIN_BYTES)
        return 0;
    if (!length) {
        segments[0] = 0;
        return 1;
    }
    size_t count = (length + CDC_BULK_TRB_BYTES - 1) / CDC_BULK_TRB_BYTES;
    if (count > capacity)
        return 0;
    for (size_t i = 0; i < count; i++) {
        u32 remaining = length - i * CDC_BULK_TRB_BYTES;
        segments[i] = remaining < CDC_BULK_TRB_BYTES ? remaining : CDC_BULK_TRB_BYTES;
    }
    return count;
}

int usb_cdc_bulk_retired_bytes(u32 expected, u32 remaining, u32 status, bool owned)
{
    if (owned)
        return -1;
    if (remaining > expected || status)
        return -2;
    return expected - remaining;
}
