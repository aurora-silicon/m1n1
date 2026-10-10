/* SPDX-License-Identifier: MIT */
#ifndef USB_CDC_SS_DESC_H
#define USB_CDC_SS_DESC_H

#ifdef CDC_DESC_HOST_TEST
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef uint8_t u8;
#else
#include "types.h"
#endif

/* Two CDC ACM functions have six endpoints, each with one SS companion. */
#define CDC_SS_COMPANION_BYTES 36
#define CDC_SS_BOS_BYTES       22

size_t usb_cdc_ss_config(const u8 *source, size_t source_len, u8 *dest, size_t capacity);
const u8 *usb_cdc_ss_bos(size_t *length);

#endif
