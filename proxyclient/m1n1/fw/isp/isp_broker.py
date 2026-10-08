# SPDX-License-Identifier: MIT
"""Shared allocation and terminal channels for the qualified T6040 profile."""
import struct


class ISPControlBroker:
    def __init__(self, transport, channels, allocate, allocations, notify, persist, state):
        self.transport = transport
        self.allocate = allocate
        self.allocations = allocations
        self.notify = notify
        self.persist = persist
        self.state = state
        self.channels = {}
        self.cursor = 0
        self.terminal_cursor = 0
        self.failed = False
        rings = []
        for name, kind, source in (("SHAREDMALLOC", 1, "0x3"), ("TERMINAL", 2, "0x0")):
            channel = dict(channels[name])
            addr = int(channel['iova'], 16)
            count = channel['num']
            if (channel['type'] != kind or channel['raw_source'] != source or
                    type(count) is not int or not 0 < count <= 0x1000 or addr % 64):
                raise ValueError('Invalid ISP control channel geometry')
            size = count * 64
            transport.pa_for(addr, size)
            if any(addr < old + length and old < addr + size for old, length in rings):
                raise ValueError('ISP control rings overlap')
            rings.append((addr, size))
            self.channels[name] = (addr, count)

    def _check(self):
        if self.failed:
            raise ValueError('Failed ISP broker requires cold recovery')

    def service_one(self):
        self._check()
        try:
            return self._service_one()
        except Exception:
            self.failed = True
            raise

    def _service_one(self):
        addr, count = self.channels['SHAREDMALLOC']
        slot = addr + self.cursor * 64
        req = struct.unpack('<8Q', self.transport.read(slot, 64))
        if req[0] & 15 in (1, 3):
            return False
        self.state['requests'].append({'slot': self.cursor, 'words': [hex(x) for x in req]})
        self.persist('owned shared allocation request captured')
        if req[0]:
            matches = [a for a in self.allocations()
                       if a['iova'] == req[0] and not a.get('retired')]
            if len(matches) != 1 or any(req[1:]):
                raise ValueError('Free request does not identify one owned surface')
            matches[0]['retired'] = True
            words = (req[0] | 1, 0, 0, 0, 0, 0, 0, 0)
            stage = 'owned free acknowledged; mapping/storage retained until cold reset'
        else:
            if any(req[3:]):
                raise ValueError('Unexpected allocation request flags')
            allocation = self.allocate(req[2], req[1])
            words = (allocation['iova'] | 1, 0, allocation['index'], 0, 0, 0, 0, 0)
            stage = 'owned ' + allocation['name'] + ' response published'
        self.transport.publish(slot, words, req[0])
        self.notify(8)
        if not req[0] and allocation['name'] == 'LOG':
            self.state['log_response_published'] = True
        self.cursor = (self.cursor + 1) % count
        self.persist(stage)
        return True

    def drain_terminal(self, limit=None):
        self._check()
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError('Invalid terminal drain limit')
        try:
            self._drain_terminal(limit)
        except Exception:
            self.failed = True
            raise

    def _drain_terminal(self, limit):
        addr, count = self.channels['TERMINAL']
        for _ in range(count if limit is None else min(limit, count)):
            slot = addr + self.terminal_cursor * 64
            req = struct.unpack('<8Q', self.transport.read(slot, 64))
            if req[0] & 15 in (1, 3):
                return
            if not 0 < req[1] <= 512:
                self.state['terminal_unhandled'] = [hex(x) for x in req]
                raise ValueError('Unexpected terminal report')
            content = self.transport.read(req[0] & ~3, req[1])
            self.state['terminal'].append({'words': [hex(x) for x in req],
                'text': content.decode('utf-8', errors='replace')})
            self.persist('owned firmware terminal report captured')
            if b'Exception:' in content or b'ASSERT:' in content:
                raise ValueError('ISP firmware reported an exception/assertion')
            self.transport.publish(slot, (3, 0, 0, 0, 0, 0, 0, 0), req[0])
            self.notify(1)
            self.terminal_cursor = (self.terminal_cursor + 1) % count
        if limit is None:
            raise TimeoutError('Terminal did not drain')
