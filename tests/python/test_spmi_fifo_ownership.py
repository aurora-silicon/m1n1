"""Reject foreign queued replies without consuming them or issuing a command."""
from pathlib import Path
import subprocess


def test_unexpected_fifo_contents_are_retained(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/spmi.c").read_text()
    definitions = source[source.index("#define SPMI_OPC_RESET"):source.index("spmi_dev_t *spmi_init")]
    commands = source[source.index("static int wait_rx_fifo"):source.index("int spmi_send_reset")]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stddef.h>
typedef uint8_t u8; typedef uint16_t u16; typedef uint32_t u32; typedef uint64_t u64;
typedef struct spmi_dev spmi_dev_t;
#define BIT(n) (1U << (n))
#define MASK(n) ((1ULL << (n)) - 1)
#define GENMASK(h,l) (MASK((h) + 1) & ~MASK(l))
#define FIELD_PREP(m,v) (((u32)(v) << __builtin_ctzll(m)) & (m))
#define FIELD_GET(m,v) (((v) & (m)) >> __builtin_ctzll(m))
#define SPMI_ERR_UNKNOWN 1
#define SPMI_ERR_BUS_IO 2
#define SPMI_ERR_INVALID_PARAM 3
static unsigned scenario, writes, replies;
static u32 opcode;
static u32 read32(uintptr_t address) {
    if (address == 0x1000) {
        if (scenario == 1) return BIT(8) | BIT(16);
        if (scenario == 2) return BIT(24);
        return BIT(8) | ((!writes || replies == 2) ? BIT(24) : 0);
    }
    assert(address == 0x1008 && writes);
    if (++replies == 1) return BIT(16) | (12U << 8) | opcode;
    assert(replies == 2); return 0x5a;
}
static void write32(uintptr_t address, u32 value) {
    assert(address == 0x1004); writes++; opcode = value & 0xff;
}
static void udelay(unsigned value) {assert(value == 10);}
''' + definitions + commands + r'''
int main(void) {
    spmi_dev_t dev = {.base = 0x1000, .regs = &regs_gen1};
    u8 output = 0;
    scenario = 1;
    assert(raw_command(&dev, 12, 0x20, 0, NULL, 0, &output, 1) < 0);
    assert(!writes && !replies);
    scenario = 2;
    assert(raw_command(&dev, 12, 0x20, 0, NULL, 0, &output, 1) < 0);
    assert(!writes && !replies);
    scenario = 0;
    assert(raw_command(&dev, 12, 0x20, 0, NULL, 0, &output, 1) == 0);
    assert(writes == 1 && replies == 2 && output == 0x5a);
    return 0;
}
'''
    cfile = tmp_path / "spmi.c"
    binary = tmp_path / "spmi-test"
    cfile.write_text(harness)
    subprocess.run(["cc", "-std=c11", str(cfile), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, capture_output=True)
