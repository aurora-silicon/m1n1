# SPDX-License-Identifier: MIT
import struct


def check_span(address, size, ram_base, ram_size, regions):
    end = address + size
    ram_end = ram_base + ram_size
    if (size <= 0 or ram_size <= 0 or ram_base < 0 or ram_end > 1 << 64 or
            address < ram_base or end > ram_end):
        raise ValueError("Chainload span is outside inherited RAM")
    for name, start, length in regions:
        if start < 0 or length < 0 or start + length > 1 << 64:
            raise ValueError(f"Invalid preserved region: {name}")
        if length and address < start + length and start < end:
            raise ValueError(f"Chainload span overlaps {name}")
    return end


def preserved_regions(adt):
    regions = []
    for name, value in adt["chosen"]["memory-map"]._properties.items():
        if isinstance(value, (list, tuple)) and len(value) == 2:
            regions.append((name, *value))
    nodes = [adt]
    while nodes:
        node = nodes.pop()
        nodes.extend(node)
        raw = node._properties.get("segment-ranges")
        if raw is not None:
            for phys, _, _, size, _ in struct.iter_unpack("<QQQII", raw):
                regions.append((node._path, phys, size))
    return regions
