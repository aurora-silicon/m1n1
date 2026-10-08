# SPDX-License-Identifier: MIT
"""Bounded transport for caller-owned modern ISP allocations."""
import struct


def publish_slot(iface, p, pa, words, expected):
    if type(pa) is not int or pa < 0 or pa % 64 or pa + 64 > 1 << 64:
        raise ValueError('Invalid ISP physical slot')
    if (len(words) != 8 or
            any(type(word) is not int or not 0 <= word < 1 << 64
                for word in (*words, expected))):
        raise ValueError('Invalid ISP slot words')
    if struct.unpack('<Q', iface.readmem(pa, 8))[0] != expected:
        raise ValueError('Slot ownership changed before publication')
    payload = struct.pack('<7Q', *words[1:])
    iface.writemem(pa + 8, payload)
    p.dc_cvac(pa & ~127, 128)
    if iface.readmem(pa, 64) != struct.pack('<Q', expected) + payload:
        raise ValueError('Owned slot payload readback differs')
    # The consumer may reuse the slot immediately after validity is published.
    p.write64(pa, words[0])
    p.dc_cvac(pa & ~127, 128)


def poll_control(service_one, drain_terminal, poll, ack_pending):
    serviced = service_one()
    drain_terminal()
    if poll:
        poll()
    if not serviced:
        ack_pending()
    return serviced


class ISPOwnedTransport:
    def __init__(self, iface, proxy, guard, allocations):
        self.iface = iface
        self.proxy = proxy
        self.guard = guard
        self.allocations = allocations

    def pa_for(self, iova, size):
        if (type(iova) is not int or type(size) is not int or iova < 0 or
                size <= 0 or iova + size > 1 << 64):
            raise ValueError('Invalid owned ISP access extent')
        matches = [a for a in self.allocations()
                   if a['iova'] <= iova < iova + size <= a['iova'] + a['size']]
        if len(matches) != 1:
            raise ValueError('Access outside one owned mapped surface')
        a = matches[0]
        if (type(a['pa']) is not int or a['pa'] < 0 or a['pa'] % 128 or
                a['size'] % 128):
            raise ValueError('Owned allocation does not cover complete cache lines')
        pa = a['pa'] + iova - a['iova']
        if pa < 0 or pa + size > 1 << 64:
            raise ValueError('Invalid owned ISP physical extent')
        return pa

    def invalidate(self, pa, size):
        self.proxy.dc_ivac(pa & ~127, ((pa + size + 127) & ~127) - (pa & ~127))

    def read(self, iova, size):
        self.guard()
        pa = self.pa_for(iova, size)
        self.invalidate(pa, size)
        return self.iface.readmem(pa, size)

    def write(self, iova, data):
        self.guard()
        pa = self.pa_for(iova, len(data))
        self.iface.writemem(pa, data)
        start = pa & ~127
        end = (pa + len(data) + 127) & ~127
        self.proxy.dc_cvac(start, end - start)
        if self.iface.readmem(pa, len(data)) != data:
            raise ValueError('Owned ISP payload readback differs')

    def publish(self, iova, words, expected):
        self.guard()
        if iova % 64 or len(words) != 8:
            raise ValueError('Invalid ISP slot geometry')
        pa = self.pa_for(iova, 64)
        if pa % 64:
            raise ValueError('Unaligned owned ISP slot')
        self.invalidate(pa, 64)
        publish_slot(self.iface, self.proxy, pa, words, expected)
