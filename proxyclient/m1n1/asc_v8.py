# SPDX-License-Identifier: MIT
"""Offline ASCWrap-v8 mailbox layout, derived from 26A428 AppleMessageBoxMailbox.

No transport, power-up, IRQ handling, or live register access is implemented.
Callers must supply the aperture selected by the driver; this is not inferred
from an older ASC generation. See docs/j873.md for evidence and limits.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class MailboxLayout:
    base: int
    mailbox: int
    aperture: int
    alias: bool = False

    def __post_init__(self):
        if self.base < 0 or not 0 <= self.mailbox < 256 or self.aperture not in (0, 1):
            raise ValueError("invalid mailbox layout")

    @property
    def read_data(self):
        return self.base + (0x10000 if self.alias else 0) + (2 * self.mailbox + self.aperture) * 0x80

    @property
    def write_data(self):
        return self.read_data + 0x10

    @property
    def status(self):
        return self.read_data + 0x20


def decode_status(value):
    if not 0 <= value <= 0xffffffff:
        raise ValueError("mailbox status must be a 32-bit unsigned value")
    readable = value & 15
    writable = (value >> 8) & 15
    capacity = (value >> 16) & 15
    return dict(readable=readable, writable=writable, capacity=capacity,
                tx_empty=writable == capacity, tx_full=writable == 0,
                rx_empty=readable == 0, rx_full=readable == capacity)
