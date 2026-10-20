/* SPDX-License-Identifier: MIT */

#ifndef PCIE_H
#define PCIE_H

#include "types.h"

int pcie_init(void);
int pcie_shutdown(void);
const char *pcie_log_get(u32 *len);

#endif
