/* SPDX-License-Identifier: MIT */

#ifndef SMC_H
#define SMC_H

#include "asc.h"
#include "rtkit.h"
#include "types.h"

typedef struct smc_dev smc_dev_t;

int smc_write_u32(smc_dev_t *smc, u32 key, u32 value);

/*
 * Keys are FourCCs packed MSB-first, matching how src/gpio.c reads an SMC key
 * out of an ADT function- property ("gP16" == 0x67503136).
 *
 * Note that /arm-io/smctempsensor0's "sensor" property is stored the other way
 * round: its little-endian word 0x4c50546d is the key "mTPL" (0x6d54504c), and
 * SMC_KEY(LPTm) is rejected with 132 (key not found) on J813.  "mTPL" does
 * exist but reads back an si32 zero; the live temperatures on this machine are
 * plain "flt " keys -- SMC_KEY(TB0T) and SMC_KEY(Ts0P) both answer.
 */
#define _SMC_KEY(s) (((u32)(s)[0] << 24) | ((s)[1] << 16) | ((s)[2] << 8) | (s)[3])
#define SMC_KEY(s)  _SMC_KEY(#s)

int smc_read(smc_dev_t *smc, u32 key, void *buf, size_t size);
int smc_read_u32(smc_dev_t *smc, u32 key, u32 *value);
int smc_get_key_info(smc_dev_t *smc, u32 key, u8 *size, u32 *type);

smc_dev_t *smc_init(void);
void smc_shutdown(smc_dev_t *smc);

#endif
