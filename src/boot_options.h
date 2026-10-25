/* SPDX-License-Identifier: MIT */
#ifndef AURA_BOOT_OPTIONS_H
#define AURA_BOOT_OPTIONS_H
#include <stdbool.h>
#include <stddef.h>

bool boot_option_present(const char *line, size_t capacity, const char *option);
void boot_options_init(const char *line, size_t capacity);
bool aura_debug_enabled(void);
void boot_options_set_stage1(bool stage1);
bool aura_proxy_enabled(void);
#endif
