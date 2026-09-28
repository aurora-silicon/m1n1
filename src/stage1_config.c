/* SPDX-License-Identifier: MIT */

#include "stage1_config.h"
#include "string.h"
#include "utils.h"

#include "tinf/tinf.h"

struct stage1_config_block {
    char magic[16];
    u32 version;
    u32 window_ms;
    char esp_uuid[40];
    char stage2_path[192];
    u32 crc32;
};

static struct stage1_config_block config __attribute__((section(".data.stage1_config"), used)) = {
    .magic = "AURORA-S1-CFG01",
    .version = 1,
};

static int config_valid = -1;

static bool valid_uuid(const char *uuid)
{
    if (strnlen(uuid, sizeof(config.esp_uuid)) != 36)
        return false;
    for (int i = 0; i < 36; i++) {
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (uuid[i] != '-')
                return false;
        } else if (!((uuid[i] >= '0' && uuid[i] <= '9') || (uuid[i] >= 'a' && uuid[i] <= 'f'))) {
            return false;
        }
    }
    return true;
}

static bool valid_path(const char *path)
{
    size_t len = strnlen(path, sizeof(config.stage2_path));
    if (!len || len == sizeof(config.stage2_path) || path[0] == '/')
        return false;
    /* The fixed-size field has one terminator and only zero padding. */
    for (size_t i = len + 1; i < sizeof(config.stage2_path); i++) {
        if (path[i])
            return false;
    }
    size_t component = 0;
    for (size_t i = 0; i < len; i++) {
        unsigned char c = path[i];
        if (c < 0x20 || c >= 0x7f || c == ';' || c == '\\')
            return false;
        if (c == '/') {
            size_t length = i - component;
            if (!length || (length == 1 && path[component] == '.') ||
                (length == 2 && path[component] == '.' && path[component + 1] == '.'))
                return false;
            component = i + 1;
        }
    }
    size_t length = len - component;
    return length && !(length == 1 && path[component] == '.') &&
           !(length == 2 && path[component] == '.' && path[component + 1] == '.');
}

static bool stage1_config_valid(void)
{
    if (config_valid >= 0)
        return config_valid;
    config_valid = 0;

    /* A factory image has no target and remains a proxy-only image. */
    if (!config.esp_uuid[0] && !config.stage2_path[0] && !config.crc32)
        return false;

    u32 crc =
        tinf_crc32(&config.version, sizeof(config) - sizeof(config.magic) - sizeof(config.crc32));
    if (config.version != 1 || config.window_ms > 99999 || !valid_uuid(config.esp_uuid) ||
        !valid_path(config.stage2_path) || crc != config.crc32) {
        printf("Stage 1: invalid in-image config; using proxy fallback\n");
        return false;
    }
    config_valid = 1;
    return true;
}

u32 stage1_config_window_ms(void)
{
    return stage1_config_valid() ? config.window_ms : 0;
}

const char *stage1_config_esp_uuid(void)
{
    return stage1_config_valid() ? config.esp_uuid : NULL;
}

const char *stage1_config_target(void)
{
    static char target[sizeof(config.esp_uuid) + sizeof(config.stage2_path) + 1];
    if (!stage1_config_valid())
        return NULL;
    snprintf(target, sizeof(target), "%s;%s", config.esp_uuid, config.stage2_path);
    return target;
}
