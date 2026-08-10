/* SPDX-License-Identifier: MIT */

#ifndef PMGR_H
#define PMGR_H

#include "types.h"

#define PMGR_DIE_OFFSET 0x2000000000

#define PMGR_DEVICE_ID GENMASK(15, 0)
#define PMGR_DIE_ID    GENMASK(31, 28)

#define PMGR_PS_ACTIVE  0xf
#define PMGR_PS_CLKGATE 0x4
#define PMGR_PS_PWRGATE 0x0

int pmgr_init(void);

int pmgr_power_enable(u32 id);
int pmgr_power_disable(u32 id);

int pmgr_adt_power_enable(const char *path);
int pmgr_adt_power_disable(const char *path);
int pmgr_adt_power_enable_index(const char *path, u32 index);
int pmgr_adt_power_disable_index(const char *path, u32 index);
int pmgr_adt_reset(const char *path);

int pmgr_reset(int die, const char *name);
int pmgr_power_enable_name(int die, const char *name);
/* Copy the ADT name of the PMGR device with this id into `out`. Returns 0 on
 * success. Used to derive meaning from a device id without hardcoding the
 * id itself, e.g. mapping CIO<N>_RECONFIG-V to reconfiguration slot N. */
int pmgr_device_name_by_id(u16 id, char *out, size_t len);
int pmgr_power_on(int die, const char *name);

int pmgr_set_mode(uintptr_t addr, u8 target_mode);

u32 pmgr_get_feature(const char *name);

#endif
