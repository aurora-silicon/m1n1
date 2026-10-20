/* SPDX-License-Identifier: MIT */

#ifndef SMC_H
#define SMC_H

#include "asc.h"
#include "rtkit.h"
#include "types.h"

typedef struct smc_dev smc_dev_t;

int smc_write_u32(smc_dev_t *smc, u32 key, u32 value);
int smc_write(smc_dev_t *smc, u32 key, const u32 *words, u32 len);
int smc_read_u32(smc_dev_t *smc, u32 key, u32 *value);
int smc_read_u64(smc_dev_t *smc, u32 key, u32 out[2]);
int smc_key_info(smc_dev_t *smc, u32 key, u32 out[2]);

smc_dev_t *smc_init(void);
smc_dev_t *smc_init_adopt(void);
void smc_shutdown(smc_dev_t *smc);

#endif
