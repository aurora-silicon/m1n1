# SPDX-License-Identifier: MIT
"""Bounded command response polling while servicing ISP control traffic."""
import time
import struct
import math


class StartDeadline:
    """Firmware inactivity budget, bounded independently by wall time."""
    def __init__(self, clock=time.monotonic, inactivity=30, wall=40):
        self.clock = clock
        start = clock()
        self.wall_deadline = start + wall
        self.idle_deadline = start + inactivity
        self.inactivity = inactivity
        self.serviced_requests = 0

    def serviced(self, serviced):
        if serviced:
            self.serviced_requests += 1
            self.idle_deadline = min(self.clock() + self.inactivity, self.wall_deadline)

    def expired(self):
        now = self.clock()
        return now >= self.wall_deadline or now >= self.idle_deadline

    def check(self, on_timeout):
        if self.expired():
            on_timeout()
            raise TimeoutError("CH_START deadline")

    def snapshot(self):
        return {'wall_deadline': self.wall_deadline, 'idle_deadline': self.idle_deadline,
                'serviced_requests': self.serviced_requests}


def wait_command(read_response, expected, pump, guard, *, timeout,
                 start_deadline=None, on_timeout=None, clock=time.monotonic,
                 sleep=time.sleep):
    """Return only an on-time matching ACK; retain resources on failure."""
    if timeout <= 0:
        raise ValueError('Invalid ISP command timeout')
    deadline = clock() + timeout
    response = None

    def check():
        if ((start_deadline is not None and start_deadline.expired()) or
                (start_deadline is None and clock() >= deadline)):
            if on_timeout:
                on_timeout(response)
            raise TimeoutError('ISP command response')

    while True:
        guard()
        check()
        response = read_response()
        check()
        if response[0] == expected:
            return response
        serviced = pump()
        if start_deadline is not None:
            start_deadline.serviced(serviced)
        check()
        sleep(.002)


class ISPCommandChannel:
    """Serialized commands over a caller-owned, profile-validated IO ring."""
    def __init__(self, transport, channel, notify, guard):
        channel = dict(channel)
        self.address = int(channel['iova'], 16)
        self.count = channel['num']
        if (channel['type'] != 0 or channel['raw_source'] != '0x1' or
                type(self.count) is not int or not 0 < self.count <= 0x1000 or
                self.address % 64):
            raise ValueError('Invalid ISP command channel geometry')
        transport.pa_for(self.address, self.count * 64)
        self.transport = transport
        self.notify = notify
        self.guard = guard
        self.cursor = 0
        self.failed = False

    def send(self, iova, args, outsize, pump, *, timeout, start_deadline=None,
             on_timeout=None, on_response=None):
        """Send args already written and cleaned at the caller-owned IOVA."""
        if self.failed:
            raise ValueError('Failed ISP command channel requires cold recovery')
        size = len(args)
        if (size < 8 or size > 0x800 or size % 4 or
                type(outsize) is not int or not 0 <= outsize <= size):
            raise ValueError('Invalid ISP command payload geometry')
        if iova % 16 or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Invalid ISP command address/timeout')
        if start_deadline is not None and start_deadline.expired():
            raise TimeoutError('ISP startup command deadline')
        self.transport.pa_for(iova, size)
        slot = self.address + self.cursor * 64
        if iova < self.address + self.count * 64 and self.address < iova + size:
            raise ValueError('ISP command payload overlaps its ring')
        try:
            idle = struct.unpack('<8Q', self.transport.read(slot, 64))[0]
            if idle & 15 != 1:
                raise ValueError('ISP command slot is not idle')
            if self.transport.read(iova, size) != args:
                raise ValueError('Prepared ISP command payload differs')
            if start_deadline is not None and start_deadline.expired():
                raise TimeoutError('ISP startup command deadline')
            self.transport.publish(slot, (iova, size, outsize, 0, 0, 0, 0, 0), idle)
            self.notify(2)
            response = wait_command(
                lambda: struct.unpack('<8Q', self.transport.read(slot, 64)),
                iova | 1, pump, self.guard, timeout=timeout,
                start_deadline=start_deadline, on_timeout=on_timeout)
            payload = self.transport.read(iova, size)
            if on_response:
                on_response(response, payload)
            if response[1:3] != (outsize, 0) or any(response[3:]):
                raise ValueError('Unexpected ISP command reply/status')
            if payload[:8] != args[:8]:
                raise ValueError('ISP command reply header/status differs')
            self.cursor = (self.cursor + 1) % self.count
            return payload
        except Exception:
            self.failed = True
            raise
