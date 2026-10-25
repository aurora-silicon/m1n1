#!/usr/bin/env python3
"""Decode local T8122 diagnostic metadata; never access hardware or enable it.

Layouts follow proxyclient/m1n1/adt.py and src/pmgr.c. The arm-io ranges
use the Apple ADT 64-bit bus/parent/size layout. Output is evidence for
bring-up, not a Linux DT binding or a usable firmware handoff.
"""
import argparse
import json
import struct
from pathlib import Path


class DecodeError(ValueError):
    pass


THERMAL_PROPERTIES = (
    'gpu-fast-die0-sensor-mask', 'gpu-fast-die0-alarm-threshold',
    'gpu-fast-die0-target', 'gpu-fast-die0-integral-gain',
    'gpu-fast-die0-proportional-gain', 'gpu-sochot-temp',
)
TOPOLOGY_PROPERTIES = (
    'gpu-core-count', 'gpu-core-mask', 'gpu-count', 'gpu-num-mgpus',
    'gpu-num-cores', 'gpu-num-frags',
)


def decode(export):
    if export.get('schema_version') != 1:
        raise DecodeError('Unsupported diagnostic schema')
    nodes = export['nodes']

    def raw(node, prop):
        item = nodes[node]['properties'][prop]
        value = bytes.fromhex(item['hex'])
        if len(value) != item['bytes']:
            raise DecodeError(f'{node}/{prop}: length mismatch')
        return value

    def records(node, prop, fmt):
        value = raw(node, prop)
        if not value or len(value) % struct.calcsize(fmt):
            raise DecodeError(f'{node}/{prop}: invalid record length')
        return list(struct.iter_unpack(fmt, value))

    def scalar(node, prop):
        rows = records(node, prop, '<I')
        if len(rows) != 1:
            raise DecodeError(f'{node}/{prop}: expected one cell')
        return rows[0][0]

    if raw('arm-io', 'compatible') != b'arm-io,t8122\0':
        raise DecodeError('Expected T8122 arm-io')
    if any(scalar('arm-io', p) != 2 for p in ('#address-cells', '#size-cells')):
        raise DecodeError('Unsupported arm-io cell widths')
    if 'root' in nodes and scalar('root', '#address-cells') != 2:
        raise DecodeError('Unsupported parent address width')
    ranges = records('arm-io', 'ranges', '<QQQ')

    def translate(address, size):
        if size <= 0 or address + size > 1 << 64:
            raise DecodeError('Invalid register extent')
        matches = [parent + address - bus for bus, parent, length in ranges
                   if bus <= address and address + size <= bus + length]
        # ADT can contain nested ranges with identical identity translations.
        if not matches or len(set(matches)) != 1 or matches[0] + size > 1 << 64:
            raise DecodeError('Unmapped or ambiguous register extent')
        return matches[0]

    def registers(node):
        return [{'bus_address': a, 'physical_address': translate(a, s), 'size': s}
                for a, s in records(node, 'reg', '<QQ')]

    result = {'schema_version': 1, 'hardware_activation': False, 'nodes': {}}
    gates = set()
    for node in ('sgx', 'gfx-asc'):
        irqs = [x[0] for x in records(node, 'interrupts', '<I')]
        power = [x[0] for x in records(node, 'power-gates', '<I')]
        gates.update(power)
        info = {'registers': registers(node), 'interrupts_adt_order': irqs,
                'power_gate_ids': power}
        if 'interrupts-valid' in nodes[node]['properties']:
            mask = scalar(node, 'interrupts-valid')
            if mask >> len(irqs):
                raise DecodeError('Interrupt mask exceeds IRQ array')
            info['interrupt_valid'] = [bool(mask & (1 << i)) for i in range(len(irqs))]
        result['nodes'][node] = info

    pmgr_regs = registers('pmgr')
    ps_regs = records('pmgr', 'ps-regs', '<III')
    devices_raw = raw('pmgr', 'devices')
    if len(devices_raw) < 96 or len(devices_raw) % 48:
        raise DecodeError('Invalid PMGR devices table')
    devices = [devices_raw[i:i + 48] for i in range(0, len(devices_raw), 48)]
    # Same ID-width detection as pmgr_init(). Virtual devices have no PS address.
    u8ids = devices[0][3] != devices[1][3]
    by_id = {}
    for row in devices:
        ident = row[3] if u8ids else struct.unpack_from('<H', row, 26)[0]
        if ident in by_id:
            raise DecodeError('Duplicate PMGR device ID')
        by_id[ident] = row
    visited = {}

    def visit(ident, chain):
        if ident in chain:
            raise DecodeError('Cyclic PMGR parent chain')
        if ident in visited:
            return
        if ident not in by_id:
            raise DecodeError(f'Missing PMGR device {ident}')
        row = by_id[ident]
        parents = list(row[4:6] if u8ids else struct.unpack_from('<HH', row, 4))
        parents = [p for p in parents if p]
        item = {'id': ident, 'name': row[32:].split(b'\0')[0].decode('ascii'),
                'parents': parents, 'virtual': bool(row[0] & 0x10)}
        if not item['virtual']:
            if row[11] >= len(ps_regs):
                raise DecodeError('PMGR ps-reg index out of bounds')
            reg_index, offset, _mask = ps_regs[row[11]]
            if reg_index >= len(pmgr_regs):
                raise DecodeError('PMGR register index out of bounds')
            offset += row[10] * 8
            reg = pmgr_regs[reg_index]
            if offset + 4 > reg['size']:
                raise DecodeError('PMGR state register outside extent')
            item['state_register'] = reg['physical_address'] + offset
        for parent in parents:
            visit(parent, chain | {ident})
        visited[ident] = item

    for ident in sorted(gates):
        visit(ident, set())
    result['power_dependencies'] = [visited[i] for i in sorted(visited)]
    count = scalar('sgx', 'perf-state-count')
    tables = scalar('sgx', 'perf-state-table-count')
    if not count or not tables:
        raise DecodeError('Empty performance table dimensions')
    if 'gpu-num-perf-states' in nodes['sgx']['properties']:
        if count != scalar('sgx', 'gpu-num-perf-states') + 1:
            raise DecodeError('GPU pstate count must exclude one off-state entry')
    result['performance_tables'] = {}
    for prop in ('perf-states', 'perf-states-sram'):
        entries = records('sgx', prop, '<II')
        if len(entries) != count * tables:
            raise DecodeError(f'{prop}: count mismatch')
        result['performance_tables'][prop] = [
            {'frequency_raw': freq, 'voltage_raw': volt} for freq, volt in entries]
    gpu = result['performance_tables']['perf-states']
    sram = result['performance_tables']['perf-states-sram']
    if any(g['frequency_raw'] != s['frequency_raw'] for g, s in zip(gpu, sram)):
        raise DecodeError('GPU/SRAM performance frequencies differ')
    result['performance_state_count'] = count
    result['performance_table_count'] = tables
    for label, properties in (('thermal_metadata', THERMAL_PROPERTIES),
                              ('topology_metadata', TOPOLOGY_PROPERTIES)):
        present = nodes['sgx']['properties']
        # Preserve bit patterns: gains may encode floats, and a sensor mask
        # alone does not identify the sensors or establish a thermal model.
        result[label] = {
            'raw_u32': {p: scalar('sgx', p) for p in properties if p in present},
            'missing_properties': [p for p in properties if p not in present],
            'interpretation_validated': False,
        }
    result['firmware_layout'] = {'available': False, 'abi_identified': False}
    if 'segment-ranges' in nodes['gfx-asc']['properties']:
        if export.get('exporter_revision') not in (2, 3, 4):
            raise DecodeError('Segment validation requires revision 2, 3 or 4 memory bounds')

        def extent(base, size):
            if base <= 0 or size <= 0 or base + size > 1 << 64:
                raise DecodeError('Invalid memory extent')
            return base, base + size

        def u64(node, prop):
            rows = records(node, prop, '<Q')
            if len(rows) != 1:
                raise DecodeError(f'{node}/{prop}: expected one u64')
            return rows[0][0]

        dram = extent(u64('chosen', 'dram-base'), u64('chosen', 'dram-size'))
        usable = extent(export['usable_memory']['base'], export['usable_memory']['size'])
        if not dram[0] <= usable[0] < usable[1] <= dram[1]:
            raise DecodeError('Usable memory is outside DRAM')
        occupied = []

        def carveout(label, base, size):
            bounds = extent(base, size)
            if not dram[0] <= bounds[0] < bounds[1] <= dram[1]:
                raise DecodeError(f'{label}: outside DRAM')
            if bounds[0] < usable[1] and usable[0] < bounds[1]:
                raise DecodeError(f'{label}: overlaps usable RAM')
            for name, previous in occupied:
                if bounds[0] < previous[1] and previous[0] < bounds[1]:
                    raise DecodeError(f'{label}: overlaps {name}')
            occupied.append((label, bounds))

        uat = []
        for prefix in ('gfx-handoff', 'gfx-shared-region', 'gfx-shared-l2-region', 'gpu-region'):
            base, size = u64('sgx', prefix + '-base'), u64('sgx', prefix + '-size')
            carveout(prefix, base, size)
            uat.append({'name': prefix, 'physical_address': base, 'size': size})
        names_raw = raw('gfx-asc', 'segment-names')
        if not names_raw.endswith(b'\0') or b'\0' in names_raw[:-1]:
            raise DecodeError('Malformed segment names')
        names = names_raw[:-1].decode('ascii').split(';')
        segments = records('gfx-asc', 'segment-ranges', '<QQQII')
        if len(names) != len(segments) or len(set(names)) != len(names) or not all(names):
            raise DecodeError('Segment name/count mismatch')
        nub = nodes.get('gfx-nub', {}).get('properties', {})
        for prop in ('segment-names', 'segment-ranges'):
            if prop in nub and raw('gfx-nub', prop) != raw('gfx-asc', prop):
                raise DecodeError(f'ASC/nub {prop} mismatch')
        if 'pre-loaded' in nub and scalar('gfx-nub', 'pre-loaded') != 1:
            raise DecodeError('Firmware nub is not preloaded')
        decoded = []
        virtual = []
        for name, (phys, va, remap, size, flags) in zip(names, segments):
            carveout(name, phys, size)
            va_extent = extent(va, size)
            if any(va_extent[0] < end and start < va_extent[1] for start, end in virtual):
                raise DecodeError('Overlapping firmware virtual extents')
            virtual.append(va_extent)
            if remap != phys:
                raise DecodeError('Unsupported firmware remap')
            decoded.append({'name': name, 'physical_address': phys,
                            'virtual_address': va, 'size': size, 'flags_raw': flags})
        result['firmware_layout'] = {
            'available': True, 'abi_identified': False, 'segments': decoded,
            'uat_regions': uat, 'dram': {'base': dram[0], 'size': dram[1] - dram[0]},
            'limits': 'Segment geometry only; no TZ carveout check or reservations.',
        }
    if 'firmware_image' in export:
        result['firmware_layout']['image_identity'] = export['firmware_image']
    result['limitations'] = [
        'Mailbox subrange and IRQ roles are not established by ADT node ranges.',
        'No core topology, fuse values, thermal model or firmware ABI identification.',
        'Performance entries retain raw units; no operating points are installed.',
        'Runtime RAM regions are not reserved or validated as a firmware handoff.',
    ]
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('export', type=Path)
    args = parser.parse_args()
    try:
        result = decode(json.loads(args.export.read_text()))
    except (OSError, ValueError, KeyError, TypeError, struct.error) as error:
        parser.exit(1, f'T8122 resource decode failed: {error}\n')
    print(json.dumps(result, indent=2))
