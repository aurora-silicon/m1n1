/* SPDX-License-Identifier: MIT */
/*
 * Host-test stand-in for src/utils.h. The G15 GPU host test copies kboot_gpu.c and firmware.c next
 * to this file, so their #include "utils.h" resolves here: register and memory accesses go to the
 * test's simulated bus instead of real loads and stores.
 */
#ifndef UTILS_H
#define UTILS_H

#include <stdio.h>
#include <stdlib.h>

#include "soc.h"
#include "types.h"

#define printf(...) debug_printf(__VA_ARGS__)
#define dprintf(...)                                                                               \
    do {                                                                                           \
    } while (0)

#define ARRAY_SIZE(s) (sizeof(s) / sizeof((s)[0]))

#define ALIGN_UP(x, a)   (((x) + ((a) - 1)) & ~((a) - 1))
#define ALIGN_DOWN(x, a) ((x) & ~((a) - 1))

#define min(a, b) (((a) < (b)) ? (a) : (b))
#define max(a, b) (((a) > (b)) ? (a) : (b))

u32 host_read32(u64 addr);
u8 host_read8(u64 addr);
void host_sysop(const char *op);

static inline u32 read32(u64 addr)
{
    return host_read32(addr);
}

static inline u8 read8(u64 addr)
{
    return host_read8(addr);
}

void host_write32(u64 addr, u32 val);

static inline void write32(u64 addr, u32 val)
{
    host_write32(addr, val);
}

static inline u32 mask32(u64 addr, u32 clear, u32 set)
{
    u32 val = (read32(addr) & ~clear) | set;
    write32(addr, val);
    return val;
}

#define sysop(op) host_sysop(op)

static inline size_t get_page_size(void)
{
    return 16384;
}

extern u64 boot_flags, mem_size_actual;
extern u32 board_id, chip_id;

u64 top_of_memory_alloc(size_t size);

int debug_printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

#endif
