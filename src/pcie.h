/* SPDX-License-Identifier: MIT */

#ifndef PCIE_H
#define PCIE_H

int pcie_init(void);
int pcie_shutdown(void);
int pcie_t8140_handoff(void *dt);
int pcie_t8140_disable_piodma(void *dt);

#endif
