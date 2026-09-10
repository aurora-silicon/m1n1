# SPDX-License-Identifier: MIT
"""Read-only PMGR2 ADT records. This module never accesses MMIO or sends PMS requests.

Layouts are derived from AppleT8152PMGR in macOS 27 build 26A428; see
docs/j873.md. Unknown bytes remain available in each record's raw field.
"""

from dataclasses import dataclass
import struct


def records(data, size):
    if not isinstance(data, bytes) or len(data) % size:
        raise ValueError(f"expected a byte table with {size}-byte records")
    return [data[i:i + size] for i in range(0, len(data), size)]


def name_at(raw, offset):
    return raw[offset:offset + 16].split(b"\0", 1)[0].decode("ascii")


@dataclass(frozen=True)
class Device:
    id: int
    name: str
    group: int
    offset: int
    raw: bytes


@dataclass(frozen=True)
class Service:
    id: int
    name: str
    flags: int
    raw: bytes


@dataclass(frozen=True)
class PowerGroup:
    reg_index: int
    offset: int
    raw: bytes


class PMGR2Tables:
    def __init__(self, node):
        if "pmgr2,t8152" not in node.compatible:
            raise ValueError("only the evidenced T8152 PMGR2 layout is supported")
        self.node = node
        self.devices = []
        for raw in records(node.devices, 48):
            packed = struct.unpack_from("<I", raw, 16)[0]
            self.devices.append(Device(struct.unpack_from("<H", raw, 26)[0],
                                       name_at(raw, 32), packed >> 24,
                                       packed & 0xffffff, raw))
        self.services = [Service(struct.unpack_from("<H", raw, 18)[0],
                                 name_at(raw, 20), struct.unpack_from("<I", raw)[0], raw)
                         for raw in records(node.service, 36)]
        self.groups = [PowerGroup(*struct.unpack_from("<II", raw), raw)
                       for raw in records(node.ps_groups, 12)]
        self.implicit_service_count = node.virt_service_start
        if not isinstance(self.implicit_service_count, int) or not 0 <= self.implicit_service_count <= 65536:
            raise ValueError("invalid virt-service-start")
        for values in (self.devices, self.services):
            if len({value.id for value in values}) != len(values):
                raise ValueError("duplicate PMGR2 record id")

    def device_register(self, device):
        """Resolve an evidenced register address; never read or write it."""
        if not 0 <= device.group < len(self.groups):
            raise ValueError(f"{device.name}: invalid power group {device.group}")
        group = self.groups[device.group]
        if not 0 <= group.reg_index < len(self.node.reg):
            raise ValueError(f"{device.name}: invalid register index {group.reg_index}")
        base, size = self.node.get_reg(group.reg_index)
        offset = group.offset + device.offset
        if offset & 3 or offset + 4 > size:
            raise ValueError(f"{device.name}: register outside ADT window or unaligned")
        return base + offset

    def service_description(self, service_id):
        for service in self.services:
            if service.id == service_id:
                return {"id": service_id, "kind": "explicit", "name": service.name}
        if 0 <= service_id < self.implicit_service_count:
            # Runtime PMS supplies these names; device IDs are a separate namespace.
            return {"id": service_id, "kind": "implicit", "name": None}
        raise ValueError(f"service {service_id} is not declared")
