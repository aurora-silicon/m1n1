/* SPDX-License-Identifier: MIT */
#ifndef USB_CDC_BULK_H
#define USB_CDC_BULK_H

#ifdef CDC_BULK_HOST_TEST
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef uint32_t u32;
#else
#include "types.h"
#endif

#define CDC_BULK_TRB_BYTES   (16u * 1024u)
#define CDC_BULK_MAX_TRBS    16u
#define CDC_BULK_CHAIN_BYTES (CDC_BULK_TRB_BYTES * CDC_BULK_MAX_TRBS)

int usb_cdc_nump(u32 hwparams0, u32 hwparams7);
size_t usb_cdc_bulk_plan(u32 length, u32 *segments, size_t capacity);
int usb_cdc_bulk_retired_bytes(u32 expected, u32 remaining, u32 status, bool owned);

#endif
