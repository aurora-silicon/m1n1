# SPDX-License-Identifier: MIT
import struct

from ..utils import *

__all__ = ["SPMI", "SPMITimeout"]

# The STATUS poll loops below used to be unbounded. On T8142 under the
# hypervisor the RX FIFO can read as permanently non-empty, which wedged the
# host in an infinite read32 loop with no way out but ^C.
# Each poll is a USB round-trip, so this is already several seconds of patience;
# a healthy transaction settles in one or two iterations.
SPMI_POLL_LIMIT = 1000

class SPMITimeout(Exception):
    pass

CMD_EXT_WRITE   = 0x00
CMD_EXT_READ    = 0x20
CMD_EXT_WRITEL  = 0x30
CMD_EXT_READL   = 0x38
CMD_WRITE       = 0x40
CMD_READ        = 0x60
CMD_ZERO_WRITE  = 0x80

class R_CMD(Register32):
    REG         = 31, 16
    ACTIVE      = 15
    SLAVE_ID    = 14, 8
    CMD         = 7, 0

class R_STATUS(Register32):
    RX_EMPTY    = 24
    RX_COUNT    = 23, 16
    TX_EMPTY    = 8
    TX_COUNT    = 7, 0

class SPMIRegs(RegMap):
    STATUS      = 0x00, R_STATUS
    CMD         = 0x04, R_CMD
    REPLY       = 0x08, Register32
    IRQ_FLAG    = 0x80, Register32

class SPMI:
    def __init__(self, u, adt_path):
        self.u = u
        self.p = u.proxy
        self.iface = u.iface
        self.base = u.adt[adt_path].get_reg(0)[0]
        self.regs = SPMIRegs(u, self.base)

    def _drain(self, verbose=False):
        for _ in range(SPMI_POLL_LIMIT):
            if self.regs.STATUS.reg.RX_EMPTY:
                return
            v = self.regs.REPLY.val
            if verbose:
                print(">", v)
        raise SPMITimeout(f"SPMI @ {self.base:#x}: RX FIFO never drained "
                          f"(STATUS = {self.regs.STATUS.val:#x})")

    def _await_reply(self):
        for _ in range(SPMI_POLL_LIMIT):
            if not self.regs.STATUS.reg.RX_EMPTY:
                return
        raise SPMITimeout(f"SPMI @ {self.base:#x}: no reply "
                          f"(STATUS = {self.regs.STATUS.val:#x})")

    def read(self, slave, reg, size):
        self._drain(verbose=True)

        self.regs.CMD.reg = R_CMD(REG = reg, ACTIVE=1, SLAVE_ID = slave, CMD = CMD_EXT_READL | (size - 1))

        buf = b""

        left = size + 4
        while left > 0:
            self._await_reply()
            v = self.regs.REPLY.val
            buf += struct.pack("<I", v)
            left -= 4

        return buf[4:4+size]

    def write(self, slave, reg, data):
        self._drain()

        size = len(data)
        self.regs.CMD.reg = R_CMD(REG = reg, ACTIVE=1, SLAVE_ID = slave, CMD = CMD_EXT_WRITEL | (size - 1))

        while data:
            blk = (data[:4] + b"\0\0\0")[:4]
            self.regs.CMD.val = struct.unpack("<I", blk)[0]
            data = data[4:]

        self._await_reply()
        return self.regs.REPLY.val

    def read8(self, slave, reg):
        return struct.unpack("<B", self.read(slave, reg, 1))[0]

    def read16(self, slave, reg):
        return struct.unpack("<H", self.read(slave, reg, 2))[0]

    def read32(self, slave, reg):
        return struct.unpack("<I", self.read(slave, reg, 4))[0]

    def read64(self, slave, reg):
        return struct.unpack("<Q", self.read(slave, reg, 8))[0]

    def write8(self, slave, reg, val):
        return self.write(slave, reg, struct.pack("<B", val))

    def write16(self, slave, reg, val):
        return self.write(slave, reg, struct.pack("<H", val))

    def write32(self, slave, reg, val):
        return self.write(slave, reg, struct.pack("<I", val))

    def write64(self, slave, reg, val):
        return self.write(slave, reg, struct.pack("<Q", val))
