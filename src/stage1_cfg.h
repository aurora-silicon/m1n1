/* SPDX-License-Identifier: MIT */

#ifndef STAGE1_CFG_H
#define STAGE1_CFG_H

/*
 * In-image configuration of the ESP stage 1 (see src/stage1.c).
 *
 * The image carries exactly one 260 byte block in initialised data. A host
 * tool fills it in for one Mac; m1n1 only reads it. Integers are little
 * endian. Once images are in users' hands this layout must not change.
 *
 *   0   16  magic         "AURORA-S1-CFG01" and a NUL
 *   16  4   version       1
 *   20  4   window_ms     USB proxy window in ms, 0..99999
 *   24  40  esp_partuuid  partition UUID, 36 chars, lowercase, NUL padded
 *   64  192 stage2_path   path on the ESP, optionally ';'-prefixed,
 *                         NUL terminated, zero padded
 *   256 4   crc32         CRC-32 (zlib) of bytes 16..255
 *
 * This file is also built by the host tests, so it only uses the standard
 * freestanding headers.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define STAGE1_CFG_MAGIC      "AURORA-S1-CFG01"
#define STAGE1_CFG_VERSION    1
#define STAGE1_CFG_WINDOW_MAX 99999
#define STAGE1_CFG_UUID_LEN   36

struct stage1_cfg {
    char magic[16];
    uint8_t version[4];
    uint8_t window_ms[4];
    char esp_partuuid[40];
    char stage2_path[192];
    uint8_t crc32[4];
};

_Static_assert(sizeof(struct stage1_cfg) == 260, "stage1_cfg layout");

#define STAGE1_CFG_CRC_START offsetof(struct stage1_cfg, version)
#define STAGE1_CFG_CRC_LEN   (offsetof(struct stage1_cfg, crc32) - STAGE1_CFG_CRC_START)

enum stage1_cfg_state {
    STAGE1_CFG_FACTORY, // never filled in: proxy only
    STAGE1_CFG_VALID,
    STAGE1_CFG_INVALID,
};

struct stage1_cfg_info {
    uint32_t window_ms;
    // chainload spec: "<partuuid>;<path>"
    char spec[STAGE1_CFG_UUID_LEN + 1 + sizeof(((struct stage1_cfg *)0)->stage2_path)];
    // path as passed to the loader (points into spec)
    const char *path;
};

/*
 * Classify a configuration block. For a valid block, fill in info. For an
 * invalid one, set *why (if not NULL) to a short reason.
 */
enum stage1_cfg_state stage1_cfg_parse(const struct stage1_cfg *cfg, struct stage1_cfg_info *info,
                                       const char **why);

#endif
