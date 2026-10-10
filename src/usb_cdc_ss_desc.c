/* SPDX-License-Identifier: MIT */

#include "usb_cdc_ss_desc.h"

/* USB 3.1 Gen1 only: LPM and SS are advertised; no SSP sublink is claimed. */
static const u8 cdc_ss_bos[CDC_SS_BOS_BYTES] = {
    5,  0x0f, CDC_SS_BOS_BYTES,
    0,  2, /* BOS header */
    7,  0x10, 2,
    2,  0,    0,
    0, /* USB 2.0 extension: LPM */
    10, 0x10, 3,
    0,  0x0f, 0,
    3,  10,   0,
    2, /* SS: LS/FS/HS/5G */
};

const u8 *usb_cdc_ss_bos(size_t *length)
{
    if (length)
        *length = sizeof(cdc_ss_bos);
    return cdc_ss_bos;
}

size_t usb_cdc_ss_config(const u8 *source, size_t source_len, u8 *dest, size_t capacity)
{
    if (!source || !dest || source_len < 9 || source[0] != 9 || source[1] != 2 ||
        ((size_t)source[2] | ((size_t)source[3] << 8)) != source_len || source[4] != 4)
        return 0;

    size_t in = 0, out = 0;
    unsigned endpoints = 0;
    while (in < source_len) {
        size_t length = source[in];
        if (length < 2 || length > source_len - in || length > capacity - out)
            return 0;

        for (size_t i = 0; i < length; i++)
            dest[out + i] = source[in + i];

        if (source[in + 1] == 5) {
            if (length != 7 || capacity - out - length < 6)
                return 0;
            bool interrupt = (source[in + 3] & 3) == 3;
            bool bulk = (source[in + 3] & 3) == 2;
            if (!interrupt && !bulk)
                return 0;
            dest[out + 4] = interrupt ? 64 : 0;
            dest[out + 5] = interrupt ? 0 : 4; /* 1024-byte bulk packet */
            dest[out + 6] = interrupt ? 9 : 0;
            out += length;
            dest[out++] = 6;    /* bLength */
            dest[out++] = 0x30; /* SS endpoint companion */
            dest[out++] = 0;    /* no burst until measured */
            dest[out++] = 0;    /* no streams */
            dest[out++] = interrupt ? 64 : 0;
            dest[out++] = 0;
            endpoints++;
        } else {
            out += length;
        }
        in += length;
    }

    if (endpoints != 6 || out > 0xffff)
        return 0;
    dest[2] = (u8)out;
    dest[3] = (u8)(out >> 8);
    dest[8] = 112; /* 896 mA at 8 mA units */
    return out;
}
