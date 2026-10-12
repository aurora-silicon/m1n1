/* SPDX-License-Identifier: MIT */

/*
 * Host harness for src/stage1_cfg.c: reads one 260 byte block from stdin and
 * prints how the stage 1 classifies it:
 *   factory
 *   valid <window_ms> <spec>
 *   invalid <reason>
 */

#include <stdio.h>
#include <string.h>

#include "stage1_cfg.h"

int main(void)
{
    struct stage1_cfg cfg;
    struct stage1_cfg_info info;
    const char *why = "?";

    memset(&info, 0x55, sizeof(info));

    if (fread(&cfg, 1, sizeof(cfg), stdin) != sizeof(cfg) || fgetc(stdin) != EOF) {
        fprintf(stderr, "need exactly %zu bytes\n", sizeof(cfg));
        return 2;
    }

    switch (stage1_cfg_parse(&cfg, &info, &why)) {
        case STAGE1_CFG_FACTORY:
            printf("factory\n");
            break;
        case STAGE1_CFG_VALID:
            if (info.path != info.spec + STAGE1_CFG_UUID_LEN + 1)
                return 3;
            printf("valid %u %s\n", (unsigned)info.window_ms, info.spec);
            break;
        case STAGE1_CFG_INVALID:
            printf("invalid %s\n", why);
            break;
    }

    return 0;
}
