/* SPDX-License-Identifier: MIT */

#ifndef __PAYLOAD_H__
#define __PAYLOAD_H__

#include "types.h"

bool payload_logo(void **custom_128, void **custom_256);

int payload_run(void);

// Chainload a file from disk ("<partuuid>;<path>"), passing on the chosen vars
int payload_chainload(const char *spec);

#endif
