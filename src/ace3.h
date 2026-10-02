/* SPDX-License-Identifier: MIT */
#ifndef ACE3_H
#define ACE3_H
#include "spmi.h"
#include "types.h"
#define ACE3_REG_MODE 0x03
#define ACE3_REG_STATUS 0x1a
int ace3_read(spmi_dev_t *dev, u8 sid, u8 lreg, u8 *bfr, size_t len);
#endif
