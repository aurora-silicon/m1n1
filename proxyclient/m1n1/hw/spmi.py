# SPDX-License-Identifier: MIT
import struct

from .spmi1 import SPMI1Regs, R_ACTION, R_CMD, R_PEEK_POS, R_REPLY
from .spmi4 import SPMI4Regs

__all__ = ["SPMI", "SPMITimeout"]

# The STATUS poll loops below used to be unbounded. On T8142 under the
# hypervisor the RX FIFO can read as permanently non-empty, which wedged the
# host in an infinite read32 loop with no way out but ^C.
# Each poll is a USB round-trip, so this is already several seconds of patience;
# a healthy transaction settles in one or two iterations.
SPMI_POLL_LIMIT = 1000

class SPMITimeout(Exception):
    pass

OPC_RESET       = 0x10
OPC_SLEEP       = 0x11
OPC_SHUTDOWN    = 0x12
OPC_WAKEUP      = 0x13

OPC_SLAVE_DESC  = 0x1c

OPC_EXT_WRITE   = 0x00
OPC_EXT_READ    = 0x20
OPC_EXT_WRITEL  = 0x30
OPC_EXT_READL   = 0x38
OPC_WRITE       = 0x40
OPC_READ        = 0x60
OPC_ZERO_WRITE  = 0x80

class SPMI:
    def __init__(self, u, adt_path):
        self.u = u
        self.p = u.proxy
        self.iface = u.iface
        self.adt = u.adt[adt_path]
        self.base = self.adt.get_reg(0)[0]
        gen = getattr(self.adt, "gen", -1)
        if gen == 4:
            self.regs = SPMI4Regs(u, self.base)
        else:
            # Includes the case where no "gen" property exists,
            # which is the case on older macOS versions
            self.regs = SPMI1Regs(u, self.base)

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

    def raw_read(self):
        self._await_reply()
        return self.regs.REPLY.val

    def raw_command(self, slave, opc, extra=0, data=b"", size=0, alert=True):
        self._drain(verbose=True)

        assert 0 <= slave < 16 and 0 <= opc < 256 and 0 <= extra < 0x10000
        self.regs.CMD.reg = R_CMD(
            EXTRA=extra, ALERT=alert, SLAVE_ID=slave, OPCODE=opc
        )

        while data:
            blk = (data[:4] + b"\0\0\0")[:4]
            self.regs.CMD.val = struct.unpack("<I", blk)[0]
            data = data[4:]

        reply = R_REPLY(self.raw_read())
        if reply.SLAVE_ID != slave or reply.OPCODE != opc:
            raise RuntimeError(
                f"unexpected SPMI reply: slave {reply.SLAVE_ID}, "
                f"opcode {reply.OPCODE:#x}"
            )

        buf = b""
        left = size
        while left > 0:
            buf += struct.pack("<I", self.raw_read())
            left -= 4

        if reply.FRAME_PARITY != (1 << size) - 1:
            raise RuntimeError(
                "some SPMI response frames were not received correctly: "
                f"{reply.FRAME_PARITY:b}"
            )
        if any(buf[size:]):
            raise RuntimeError("non-zero padding in SPMI response")
        if not size != bool(reply.ACK):
            raise RuntimeError("SPMI command not acknowledged")
        return buf[:size] or None

    def reset(self, slave):
        return self.raw_command(slave, OPC_RESET)

    def sleep(self, slave):
        return self.raw_command(slave, OPC_SLEEP)

    def shutdown(self, slave):
        return self.raw_command(slave, OPC_SHUTDOWN)

    def wakeup(self, slave):
        return self.raw_command(slave, OPC_WAKEUP)

    def get_descriptor(self, slave):
        return self.raw_command(slave, OPC_SLAVE_DESC, size=10)

    def read_reg(self, slave, reg):
        assert 0 <= reg < 32
        return self.raw_command(slave, OPC_READ | reg, reg, size=1)[0]

    def write_reg(self, slave, reg, value):
        assert 0 <= reg < 32 and 0 <= value < 0x100
        return self.raw_command(slave, OPC_WRITE | reg, reg | value << 8)

    def write_zero(self, slave, value):
        assert 0 <= value < 0x80
        return self.raw_command(slave, OPC_ZERO_WRITE | value, value << 8)

    def read_ext(self, slave, reg, size):
        assert 1 <= size <= 16 and 0 <= reg < 0x100
        return self.raw_command(slave, OPC_EXT_READ | (size - 1), reg, size=size)

    def write_ext(self, slave, reg, data):
        size = len(data)
        assert 1 <= size <= 16 and 0 <= reg < 0x100
        return self.raw_command(slave, OPC_EXT_WRITE | (size - 1), reg, data=data)

    def read_extl(self, slave, reg, size):
        assert 1 <= size <= 8 and 0 <= reg < 0x10000
        return self.raw_command(slave, OPC_EXT_READL | (size - 1), reg, size=size)

    def write_extl(self, slave, reg, data):
        size = len(data)
        assert 1 <= size <= 8 and 0 <= reg < 0x10000
        return self.raw_command(slave, OPC_EXT_WRITEL | (size - 1), reg, data=data)

    def read8(self, slave, reg):
        return struct.unpack("<B", self.read_extl(slave, reg, 1))[0]

    def read16(self, slave, reg):
        return struct.unpack("<H", self.read_extl(slave, reg, 2))[0]

    def read32(self, slave, reg):
        return struct.unpack("<I", self.read_extl(slave, reg, 4))[0]

    def read64(self, slave, reg):
        return struct.unpack("<Q", self.read_extl(slave, reg, 8))[0]

    def write8(self, slave, reg, val):
        return self.write_extl(slave, reg, struct.pack("<B", val))

    def write16(self, slave, reg, val):
        return self.write_extl(slave, reg, struct.pack("<H", val))

    def write32(self, slave, reg, val):
        return self.write_extl(slave, reg, struct.pack("<I", val))

    def write64(self, slave, reg, val):
        return self.write_extl(slave, reg, struct.pack("<Q", val))

    # FIFO debug interfaces

    def clear_fifos(self):
        self.regs.ACTION1.val = R_ACTION(CLEAR_FIFOS=1)

    def peek(self, fifo_idx: int, cursor: int) -> int:
        assert 0 <= fifo_idx < 2
        assert 0 <= cursor < 64
        self.regs.PEEK_POS.val = R_PEEK_POS(FIFO_IDX=fifo_idx, BUFFER_POS=cursor)
        return self.regs.PEEK_VALUE.val
