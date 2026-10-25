/* SPDX-License-Identifier: MIT */
/*
 * Host stand-in for src/utils.h, for tests/t6031_host.c: register and memory accesses go to the
 * test's simulated bus, and printf() to the test's m1n1 log.
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

#define ARRAY_SIZE(s)    (sizeof(s) / sizeof((s)[0]))
#define ALIGN_UP(x, a)   (((x) + ((a) - 1)) & ~((a) - 1))
#define ALIGN_DOWN(x, a) ((x) & ~((a) - 1))
#define min(a, b)        (((a) < (b)) ? (a) : (b))
#define max(a, b)        (((a) > (b)) ? (a) : (b))

u32 host_read32(u64 addr);
u64 host_read64(u64 addr);
u8 host_read8(u64 addr);
void host_write32(u64 addr, u32 val);

static inline u32 read32(u64 addr)
{
    return host_read32(addr);
}

static inline u64 read64(u64 addr)
{
    return host_read64(addr);
}

static inline u8 read8(u64 addr)
{
    return host_read8(addr);
}

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

static inline u32 set32(u64 addr, u32 set)
{
    return mask32(addr, 0, set);
}

static inline u32 clear32(u64 addr, u32 clear)
{
    return mask32(addr, clear, 0);
}

void udelay(u32 d);

/* As src/utils.h: at most `timeout` reads, a microsecond apart */
static inline int poll32(u64 addr, u32 mask, u32 target, u32 timeout)
{
    while (--timeout > 0) {
        if ((read32(addr) & mask) == target)
            return 0;
        udelay(1);
    }
    return -1;
}

#define sysop(op)                                                                                  \
    do {                                                                                           \
    } while (0)

static inline size_t get_page_size(void)
{
    return 16384;
}

extern u64 boot_flags, mem_size_actual;
extern u32 board_id, chip_id;

u64 top_of_memory_alloc(size_t size);

int debug_printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

#endif
