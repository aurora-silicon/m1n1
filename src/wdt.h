/* SPDX-License-Identifier: MIT */

#ifndef __WDT_H__
#define __WDT_H__

void wdt_disable(void);
void wdt_reboot(void);
int wdt_arm_seconds(unsigned seconds);

#endif
