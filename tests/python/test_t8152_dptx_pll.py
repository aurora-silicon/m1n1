"""Compare native T8152 PLL field writes, rejection, and unrelated register bits."""

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def test_native_pll_fields(tmp_path):
    source = r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
#define TYPES_H
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
static u32 registers[4], expected[4];
static unsigned writes;
static const u64 base = 0x31a740000ULL;
static const struct { u16 offset; u32 mask; u16 shift; } fields[] = {
    {0x6c0, 0x7ff, 0}, {0x6c0, 0x800, 11}, {0x6c0, 0x2000, 13},
    {0x6c0, 0x3c0000, 18}, {0x6c4, 0xfffc000, 14}, {0x6c4, 0x3fff, 0},
    {0x6c8, 0xc000, 14}, {0x6c8, 0x100, 8}, {0x6c8, 0xff, 0},
    {0x6c8, 0x3e00, 9}, {0x6c8, 0x10000, 16}, {0x6c8, 0xfc000000, 26},
    {0x6cc, 0x7f, 0}, {0x6cc, 0x600, 9}, {0x6cc, 0x7800, 11},
    {0x6cc, 0x38000, 15}, {0x6cc, 0x100000, 20}, {0x6cc, 0x200000, 21},
    {0x6cc, 0xc00000, 22}, {0x6cc, 0x3000000, 24}, {0x6cc, 0xc000000, 26},
};
static const struct { u32 rate, values[21]; } native[] = {
    {1620, {0x21c, 1, 1, 3, 0, 0, 1, 0, 0, 13, 0, 0x1a, 0x3c, 1, 0, 0, 1, 0, 0, 1, 1}},
    {2700, {0x1c2, 1, 1, 3, 0x1fff, 0x3ffe, 1, 0, 0, 13, 0, 0x1a, 0x32, 1, 0, 0, 1, 0, 0, 1, 1}},
    {5400, {0x1c2, 1, 1, 3, 0x1fff, 0x3ffe, 0, 0, 0, 9, 0, 0x1a, 0x32, 0, 0, 1, 0, 0, 1, 1, 1}},
    {8100, {0x2a3, 1, 1, 3, 0x2ffd, 0x3ffc, 0, 0, 0, 9, 0, 0x1a, 0x5a, 0, 0, 1, 0, 0, 1, 1, 1}},
};
static const u32 *values;
static u32 mask32(u64 address, u32 mask, u32 value)
{
    assert(writes < ARRAY_SIZE(fields));
    assert(address == base + fields[writes].offset);
    assert(mask == fields[writes].mask);
    assert(value == ((values[writes] << fields[writes].shift) & mask));
    unsigned index = (address - base - 0x6c0) / 4;
    u32 before = registers[index];
    registers[index] = (before & ~mask) | value;
    assert(((before ^ registers[index]) & ~mask) == 0);
    writes++;
    return registers[index];
}
#include "t8152_dptx_pll.h"
int main(void)
{
    const u32 seeds[] = {0, 0xffffffff, 0xa5c39e71};
    for (unsigned r = 0; r < ARRAY_SIZE(native); r++) {
        values = native[r].values;
        for (unsigned s = 0; s < ARRAY_SIZE(seeds); s++) {
            for (unsigned i = 0; i < ARRAY_SIZE(registers); i++)
                registers[i] = expected[i] = seeds[s];
            for (unsigned i = 0; i < ARRAY_SIZE(fields); i++) {
                unsigned index = (fields[i].offset - 0x6c0) / 4;
                expected[index] = (expected[index] & ~fields[i].mask) |
                    ((values[i] << fields[i].shift) & fields[i].mask);
            }
            writes = 0;
            assert(t8152_dptx_pll_configure_fields(base, native[r].rate) == 0);
            assert(writes == ARRAY_SIZE(fields));
            assert(!memcmp(registers, expected, sizeof(registers)));
        }
    }
    const u32 invalid[] = {0, 1, 6, 10, 20, 30, 1619, 1621, 2699, 5401, 8101,
                           5600, 10000, 13500, 1620000000U, 0xffffffff};
    for (unsigned i = 0; i < ARRAY_SIZE(invalid); i++) {
        writes = 0;
        assert(t8152_dptx_pll_configure_fields(base, invalid[i]) == -1);
        assert(writes == 0);
    }
    return 0;
}
'''
    binary = tmp_path / "t8152-dptx-pll"
    subprocess.run(
        ["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-x", "c", "-",
         "-I", str(REPO / "src/dcp"), "-o", str(binary)],
        input=source, text=True, check=True,
    )
    subprocess.run([str(binary)], check=True)
