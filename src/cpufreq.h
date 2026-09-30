/* SPDX-License-Identifier: MIT */

#ifndef CPUFREQ_H
#define CPUFREQ_H

#include "types.h"

int cpufreq_init(void);
void cpufreq_fixup(void);
void cpufreq_payload_boost(bool enable);

#endif
