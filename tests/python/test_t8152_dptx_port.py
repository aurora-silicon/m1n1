"""Check native lane masks and calibration completion before any device run."""

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]


def test_native_lane_and_calibration_fields(tmp_path):
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
#define UTILS_H
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define UNUSED(x) (void)(x)
static const u64 base = 0x31a740000ULL;
static u32 regs[0xc000 / 4];
static unsigned writes, delays, ready_after;
static unsigned index_of(u64 address)
{
    assert(address >= base && address < base + sizeof(regs));
    assert(!(address & 3));
    return (address - base) / 4;
}
static u32 read32(u64 address) { return regs[index_of(address)]; }
static u32 mask32(u64 address, u32 mask, u32 value)
{
    unsigned index = index_of(address);
    assert(!(value & ~mask));
    writes++;
    return regs[index] = (regs[index] & ~mask) | value;
}
static void udelay(u32 delay)
{
    assert(delay == 10);
    if (++delays == ready_after)
        regs[0x1c04 / 4] |= 1;
}
#include "t8152_dptx_port.c"
static void reset(void)
{
    memset(regs, 0xa5, sizeof(regs));
    writes = delays = ready_after = 0;
}
int main(void)
{
    static const struct { unsigned offset, mask; } fields[] = {
        {0x64, 0x10}, {0x24, 0x10}, {0x24, 0x2}, {0x48, 0x1},
        {0x48, 0x2}, {0x5c, 0x4}, {0x5c, 0x8}, {0x48, 0x10},
    };
    static const unsigned inactive[] = {0, 0xe, 0xc, 0x8, 0};
    for (unsigned lanes = 1; lanes <= 4; lanes++) {
        reset();
        assert(t8152_dptx_set_active_lanes(base, lanes) == 0);
        assert(writes == 35);
        assert(regs[0x2400 / 4] == 0xa5a5a5a5);
        assert(regs[0x2420 / 4] == ((0xa5a5a5a5 & ~0xfU) | inactive[lanes]));
        for (unsigned lane = 0; lane < 4; lane++) {
            unsigned origin = 0x4500 + lane * 0x2800;
            for (unsigned i = 0; i < ARRAY_SIZE(fields); i++) {
                unsigned combined = fields[i].mask;
                for (unsigned j = 0; j < ARRAY_SIZE(fields); j++)
                    if (fields[j].offset == fields[i].offset)
                        combined |= fields[j].mask;
                u32 expected = (0xa5a5a5a5 & ~combined) |
                    ((inactive[lanes] >> lane) & 1 ? combined : 0);
                assert(regs[(origin + fields[i].offset) / 4] == expected);
            }
        }
    }
    const unsigned invalid[] = {5, 8, 0xffffffff};
    reset();
    assert(t8152_dptx_set_active_lanes(base, 0) == 0 && !writes);
    for (unsigned i = 0; i < ARRAY_SIZE(invalid); i++)
        assert(t8152_dptx_set_active_lanes(base, invalid[i]) == -1 && !writes);
    for (unsigned wait = 0; wait < 2; wait++) {
        reset();
        regs[0x1c04 / 4] = wait ? 0 : 1;
        regs[0x1c24 / 4] = 0x55 << 6;
        ready_after = wait ? 3 : 0;
        assert(t8152_dptx_calibrate(base) == 0);
        assert(delays == ready_after && writes == 3);
        assert(regs[0x1c00 / 4] == ((0xa5a5a5a5 & ~0x3f0U) | 0x3f0));
        assert(regs[0x1c18 / 4] == ((0xa5a5a5a5 & ~0x3fc000U) | (0x55 << 15) | 0x4000));
    }
    reset();
    regs[0x1c04 / 4] = 0;
    assert(t8152_dptx_calibrate(base) == -1);
    assert(delays == 100000 && !writes);
    reset();
    regs[0x2400 / 4] = 0xa5a5a5a5;
    assert(t8152_dptx_latch_rate(base, 8100) == 0);
    assert((regs[0x2400 / 4] & 4) == 0);
    assert(t8152_dptx_latch_rate(base, 10000) == -1);
    reset();
    assert(t8152_dptx_apply_rate(base, 10000) == -1 && !writes && !delays);
    return 0;
}
'''
    binary = tmp_path / "t8152-dptx-port"
    subprocess.run(
        ["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-x", "c", "-",
         "-I", str(REPO / "src/dcp"), "-o", str(binary)],
        input=source, text=True, check=True,
    )
    subprocess.run([str(binary)], check=True)
