/* SPDX-License-Identifier: MIT */

#include "stage1_cfg.h"

#include "tinf/tinf.h"

static uint32_t le32(const uint8_t *p)
{
    return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24);
}

static bool is_zero(const char *p, size_t len)
{
    while (len--)
        if (*p++)
            return false;

    return true;
}

static bool uuid_valid(const char *s, size_t field)
{
    for (size_t i = 0; i < STAGE1_CFG_UUID_LEN; i++) {
        char c = s[i];

        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (c != '-')
                return false;
        } else if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) {
            return false;
        }
    }

    return is_zero(s + STAGE1_CFG_UUID_LEN, field - STAGE1_CFG_UUID_LEN);
}

static const char *path_problem(const char *path, size_t len)
{
    size_t start = 0;

    if (!len)
        return "empty stage 2 path";
    if (path[0] == '/')
        return "stage 2 path is absolute";

    for (size_t i = 0; i <= len; i++) {
        if (i == len || path[i] == '/') {
            size_t n = i - start;

            if (!n)
                return "empty component in stage 2 path";
            if (path[start] == '.' && (n == 1 || (n == 2 && path[start + 1] == '.')))
                return "dot component in stage 2 path";

            start = i + 1;
            continue;
        }

        unsigned char c = path[i];
        if (c < 0x20 || c > 0x7e || c == ';' || c == '\\')
            return "bad character in stage 2 path";
    }

    return NULL;
}

enum stage1_cfg_state stage1_cfg_parse(const struct stage1_cfg *cfg, struct stage1_cfg_info *info,
                                       const char **why)
{
    const char *problem = NULL;
    const char *path = cfg->stage2_path;
    size_t field = sizeof(cfg->stage2_path);
    size_t len;
    uint32_t crc = le32(cfg->crc32);

    if (!cfg->esp_partuuid[0] && !cfg->stage2_path[0] && !crc)
        return STAGE1_CFG_FACTORY;

    for (len = 0; len < field && path[len]; len++)
        ;

    if (crc != tinf_crc32((const uint8_t *)cfg + STAGE1_CFG_CRC_START, STAGE1_CFG_CRC_LEN))
        problem = "checksum mismatch";
    else if (le32(cfg->version) != STAGE1_CFG_VERSION)
        problem = "unknown version";
    else if (le32(cfg->window_ms) > STAGE1_CFG_WINDOW_MAX)
        problem = "USB window too long";
    else if (!uuid_valid(cfg->esp_partuuid, sizeof(cfg->esp_partuuid)))
        problem = "malformed partition UUID";
    else if (len == field)
        problem = "stage 2 path not terminated";
    else if (!is_zero(path + len, field - len))
        problem = "data after the stage 2 path";

    if (!problem) {
        // Older fillers stored the bare path, newer ones prefix it with ';'
        if (path[0] == ';') {
            path++;
            len--;
        }
        problem = path_problem(path, len);
    }

    if (problem) {
        if (why)
            *why = problem;
        return STAGE1_CFG_INVALID;
    }

    char *p = info->spec;
    for (size_t i = 0; i < STAGE1_CFG_UUID_LEN; i++)
        *p++ = cfg->esp_partuuid[i];
    *p++ = ';';
    info->path = p;
    for (size_t i = 0; i < len; i++)
        *p++ = path[i];
    *p = 0;

    info->window_ms = le32(cfg->window_ms);

    return STAGE1_CFG_VALID;
}
