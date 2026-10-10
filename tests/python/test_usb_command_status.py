"""The actual DWC3 command helpers decode all status bits in both builds."""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index("\n}", source.index("{", start)) + 2] + "\n"


@pytest.mark.parametrize("j700", [False, True])
def test_command_status_and_timeout(tmp_path, j700):
    source = (ROOT / "src/usb_dwc3.c").read_text()
    harness = r'''
#include "types.h"
#include "usb_dwc3_regs.h"
#include <assert.h>
#define usb_debug_printf(...) ((void)0)
typedef struct { uintptr_t regs; bool shutting_down; } dwc3_dev_t;
static u64 addresses[4];
static u32 values[4], raw;
static unsigned writes, reads, polls;
static bool timeout;
static void write32(u64 address, u32 value)
{
    assert(writes < 4);
    addresses[writes] = address;
    values[writes++] = value;
}
static int poll32(u64 address, u32 mask, u32 value, unsigned usec)
{
    assert(address == addresses[writes - 1]);
    assert(mask == BIT(10) && value == 0 && usec == 1000);
    polls++;
    return timeout ? -1 : 0;
}
static u32 read32(u64 address)
{
    assert(address == addresses[writes - 1]);
    reads++;
    return raw;
}
'''
    harness += function(source, "static int usb_dwc3_command(")
    harness += function(source, "static int usb_dwc3_ep_command(")
    harness += r'''
int main(void)
{
    dwc3_dev_t dev = {.regs = 0x100000};
    for (int shutdown = 0; shutdown < 2; shutdown++) {
        dev.shutting_down = shutdown;
        for (unsigned status = 0; status < 16; status++) {
            raw = (0x55 << 16) | (status << 12);
            writes = reads = polls = 0;
            assert(usb_dwc3_command(&dev, 6, 0x1234) == (int)status);
            assert(writes == 2 && reads == 1 && polls == 1);
            assert(addresses[0] == dev.regs + DWC3_DGCMDPAR && values[0] == 0x1234);
            assert(values[1] == (6 | DWC3_DGCMD_CMDACT));
            writes = reads = polls = 0;
            assert(usb_dwc3_ep_command(&dev, 3, 6, 11, 22, 33) == (int)status);
            assert(writes == 4 && reads == 1 && polls == 1);
            assert(addresses[0] == dev.regs + DWC3_DEPCMDPAR0(3) && values[0] == 11);
            assert(addresses[1] == dev.regs + DWC3_DEPCMDPAR1(3) && values[1] == 22);
            assert(addresses[2] == dev.regs + DWC3_DEPCMDPAR2(3) && values[2] == 33);
            assert(values[3] == (6 | DWC3_DEPCMD_CMDACT));
        }
        timeout = true;
        writes = reads = polls = 0;
        assert(usb_dwc3_command(&dev, 6, 0) == -1);
        assert(writes == 2 && polls == 1 && !reads);
        writes = reads = polls = 0;
        assert(usb_dwc3_ep_command(&dev, 3, 6, 0, 0, 0) == -1);
        assert(writes == 4 && polls == 1 && !reads);
        timeout = false;
    }
    return 0;
}
'''
    binary = tmp_path / "command-status"
    subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                    *( ["-DJ700_CDC_PROXY"] if j700 else [] ),
                    "-I", str(ROOT / "src"), "-x", "c", "-", "-o", str(binary)],
                   input=harness, text=True, check=True)
    subprocess.run([str(binary)], check=True, timeout=5)
