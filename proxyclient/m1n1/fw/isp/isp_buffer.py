# SPDX-License-Identifier: MIT
import struct


class ISPBufferLeases:
    """Track owned buffers for the T6040 rendered-completion format.

    Firmware exports zero-extended plane addresses. Match them to one owned
    submission and its opaque tag; never use a report address for memory access.
    The caller acknowledges the report, copies the returned buffer, then requeues.
    """
    def __init__(self, pools):
        if not pools or any(pool not in (0, 2, 9) for pool in pools):
            raise ValueError('Unqualified buffer pool')
        self.pools = {pool: [dict(surface) for surface in surfaces]
                      for pool, surfaces in pools.items()}
        for pool, surfaces in self.pools.items():
            for surface in surfaces:
                addresses = (surface['iova'], surface.get('uv_iova', 0))
                if not 0 < addresses[0] < 1 << 42 or not 0 <= addresses[1] < 1 << 42:
                    raise ValueError('Surface address outside the DART window')
                if bool(addresses[1]) != (pool == 9):
                    raise ValueError('Surface plane count differs from pool')
        self.records = {(pool, i): {'generation': 0, 'state': 'host', 'tag': None}
                        for pool, surfaces in self.pools.items() for i in range(len(surfaces))}
        self.next_tag = 0x1000

    def prepare(self, pool, indices):
        if not indices or len(set(indices)) != len(indices):
            raise ValueError('Duplicate submission lease')
        if self.next_tag + len(indices) > 1 << 64:
            raise ValueError('Buffer tag space exhausted')
        tags = []
        for index in indices:
            record = self.records[pool, index]
            if record['state'] != 'host':
                raise ValueError('Surface still owned by firmware or unacknowledged report')
        for index in indices:
            record = self.records[pool, index]
            record.update(generation=record['generation'] + 1, state='submitted', tag=self.next_tag)
            tags.append(self.next_tag)
            self.next_tag += 1
        return tags

    def returned(self, report, offset):
        if not isinstance(offset, int) or offset < 0 or offset + 0x40 > len(report):
            raise ValueError('Truncated returned surface descriptor')
        addresses = struct.unpack_from('<4Q', report, offset)
        planes, pool, tag = struct.unpack_from('<2IQ', report, offset + 0x30)
        if pool not in self.pools or any(address >> 32 for address in addresses):
            raise ValueError('Unexpected returned surface descriptor')
        matches = [i for i, surface in enumerate(self.pools[pool])
                   if addresses == (surface['iova'] & 0xffffffff,
                                    surface.get('uv_iova', 0) & 0xffffffff, 0, 0)]
        if len(matches) != 1 or planes != (2 if pool == 9 else 1):
            raise ValueError('Returned descriptor does not identify one owned lease')
        key = pool, matches[0]
        record = self.records[key]
        if record['state'] != 'submitted' or record['tag'] != tag:
            raise ValueError('Stale or duplicate returned surface lease')
        record['state'] = 'awaiting_report_ack'
        return key

    def acknowledged(self, keys):
        if any(self.records[key]['state'] != 'awaiting_report_ack' for key in keys):
            raise ValueError('Report lease ACK ordering differs')
        for key in keys:
            self.records[key]['state'] = 'returned'

    def copied(self, key):
        if self.records[key]['state'] != 'returned':
            raise ValueError('Surface read/reuse before completion report ACK')
        self.records[key]['state'] = 'host'
