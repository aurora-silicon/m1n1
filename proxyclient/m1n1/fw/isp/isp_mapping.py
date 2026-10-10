# SPDX-License-Identifier: MIT
"""Qualified 25G76 firmware mappings; caller retains resources on failure."""
import hashlib
import struct
import time

from ...hw.dart8110 import DART8110, DART8110Regs, PTE, R_PARAMS_8
from .isp_profile import T6040_25G76


def map_firmware_darts(*, u, p, iface, dart_node, isp_base, segments,
                       guard, persist, result, dart_restore):
    """Caller must qualify firmware residency, power and physical ownership."""
    validated = T6040_25G76.validate_segments(
        firmware_sha256=T6040_25G76.firmware_sha256,
        segment_names=';'.join(s.name for s in segments),
        segment_ranges=b''.join(struct.pack('<QQQII', s.phys, s.virtual,
            s.remap, s.size, s.flags) for s in segments))
    plan = [dict(name=s.name, phys=s.phys, iova=s.virtual, remap=s.remap, size=s.size, flags=s.flags) for s in validated]
    guard()
    if isp_base != 0x580000000:
        raise ValueError("Unexpected ISP aperture")
    if p.read32(isp_base + 0x1600044):
        raise ValueError("ISP processor already running")
    result['firmware_mapping_plan'] = plan
    result['mapper_sid'] = 0
    result['dart_mappings'] = []
    darts = []

    def invalidate(dart_base):
        guard()
        p.write32(dart_base + 0x80, 0x100)
        deadline = time.monotonic() + 1
        while p.read32(dart_base + 0x80) & 1 << 31:
            if time.monotonic() > deadline:
                raise TimeoutError('ISP DART SID0 invalidation timed out')
            guard()

    for instance, expected in enumerate((0x5828c0000, 0x5828e0000, 0x582900000)):
        dart_base, dart_size = dart_node.get_reg(instance)
        if not (dart_base == expected and dart_size >= 0x1800):
            raise ValueError('Unexpected ISP DART aperture')
        guard()
        params = R_PARAMS_8(p.read32(dart_base + 8))
        if not (params.PA_WIDTH == 42 and params.VA_WIDTH == 42 and (params.VERS_MAJ == 2)):
            raise ValueError('Unsupported ISP DART address widths or revision')
        if not (p.read32(dart_base + 0x100) == 0 and p.read32(dart_base + 0xc00) == 0):
            raise ValueError('ISP DART is active or faulted')
        protect = p.read32(dart_base + 0x200)
        if protect & 1:
            raise ValueError('ISP DART is locked')
        saved_tcr = p.read32(dart_base + 0x1000)
        saved_ttbr = p.read32(dart_base + 0x1400)
        if not (saved_tcr == 1 and (not saved_ttbr & 1)):
            raise ValueError('ISP DART stream is already configured')
        regs = DART8110Regs(u, dart_base)
        dart = DART8110(iface, regs, u)
        darts.append((dart_base, dart))
        if dart.enabled_streams != 0:
            raise ValueError('ISP DART has enabled streams')
        state = {'instance': instance, 'base': hex(dart_base), 'saved_tcr': hex(saved_tcr), 'saved_ttbr': hex(saved_ttbr), 'protect': hex(protect), 'translations': []}
        result['dart_mappings'].append(state)
        dart_restore.append((dart_base, saved_tcr, saved_ttbr))
        persist('before DART' + str(instance) + ' SID0 four-level mode')
        if p.get_exc_count():
            raise ValueError('CPU exception during ISP mapping')
        guard()
        p.write32(dart_base + 0x1000, 9)
        if p.read32(dart_base + 0x1000) != 9:
            raise ValueError('ISP DART four-level mode readback differs')
        for seg in plan:
            guard()
            dart.iomap_at(0, seg['remap'], seg['phys'], seg['size'], enable=False)
            if p.read32(dart_base + 0xc00):
                raise ValueError('ISP DART enabled before mapping completed')
        text = plan[0]
        root = regs.TTBR[0].reg.ADDR << 14
        if not u.heap_base <= root < root + 0x4000 <= u.heap_top:
            raise ValueError('ISP DART root outside owned heap')
        for page in range(text['remap'], text['remap'] + text['size'], 0x4000):
            l0 = dart.pt_cache[root]
            l1addr = PTE(l0[page >> 36 & 0x7ff]).OFFSET << 14
            l1 = dart.pt_cache[l1addr]
            l2addr = PTE(l1[page >> 25 & 0x7ff]).OFFSET << 14
            l2 = dart.pt_cache[l2addr]
            index = page >> 14 & 0x7ff
            pte = PTE(l2[index])
            if not pte.VALID:
                raise ValueError('Missing ISP TEXT page mapping')
            pte.WRPROT = 1
            l2[index] = pte.value
        for table in dart.pt_cache:
            guard()
            if not u.heap_base <= table < table + 0x4000 <= u.heap_top:
                raise ValueError('ISP DART table outside owned heap')
            dart.flush_pt(table)
            p.dc_cvac(table, 0x4000)
        guard()
        invalidate(dart_base)
        state['table_readbacks'] = []
        for table, values in dart.pt_cache.items():
            guard()
            expected_bytes = struct.pack('<2048Q', *values)
            actual_bytes = iface.readmem(table, 0x4000)
            state['table_readbacks'].append({'address': hex(table), 'expected_sha256': hashlib.sha256(expected_bytes).hexdigest(), 'observed_sha256': hashlib.sha256(actual_bytes).hexdigest(), 'matched': expected_bytes == actual_bytes})
            if expected_bytes != actual_bytes:
                raise ValueError('ISP DART table readback differs')
        if p.get_exc_count():
            raise ValueError('CPU exception during ISP mapping')
        aliases = (1, 6, 7, 10, 11)
        state['aliases'] = []
        for sid in aliases:
            guard()
            if p.read32(isp_base + 0x1600044):
                raise ValueError('ISP processor already running')
            if p.read32(dart_base + 0xc00):
                raise ValueError('ISP DART enabled before mapping completed')
            saved = p.read32(dart_base + 0x1000 + 4 * sid)
            saved_alias_ttbr = p.read32(dart_base + 0x1400 + 4 * sid)
            if not (saved == 1 and (not saved_alias_ttbr & 1)):
                raise ValueError('ISP DART alias stream already configured')
            entry = {'sid': sid, 'before': hex(saved), 'ttbr_before': hex(saved_alias_ttbr), 'target_sid': 0}
            state['aliases'].append(entry)
            persist('before native SID alias ' + str(sid))
            p.write32(dart_base + 0x1000 + 4 * sid, 0x80)
            entry['after'] = hex(p.read32(dart_base + 0x1000 + 4 * sid))
            if entry['after'] != '0x80':
                raise ValueError("ISP DART alias readback differs")
        guard()
        invalidate(dart_base)
        if p.get_exc_count():
            raise ValueError('CPU exception during ISP mapping')
        guard()
        p.write32(dart_base + 0xc00, 1)
        dart.enabled_streams = p.read32(dart_base + 0xc00)
        if dart.enabled_streams != 1:
            raise ValueError('ISP DART SID0 enable readback differs')
        state['write_protected_text_pages'] = text['size'] // 0x4000
        state['ttbr'] = hex(regs.TTBR[0].val)
        state['tcr'] = hex(regs.TCR[0].val)
        state['enabled_mask'] = hex(p.read32(dart_base + 0xc00))
        state['tables'] = [hex(x) for x in dart.pt_cache]
        if not (state['enabled_mask'] == '0x1' and state['tcr'] == '0x9' and int(state['ttbr'], 16) & 1):
            raise ValueError("ISP DART active configuration differs")
        dart.invalidate_cache()
        state['software_cache_discarded_before_walk'] = True
        for seg in plan:
            for offset in (0, seg['size'] - 0x4000):
                guard()
                address = seg['remap'] + offset
                ranges = dart.iotranslate(0, address, 0x4000)
                if ranges != [(seg['phys'] + offset, 0x4000)]:
                    raise ValueError("ISP firmware translation differs")
                entry = {'segment': seg['name'], 'offset': hex(offset), 'iova': hex(address), 'software_translation': [(hex(pa), size) for pa, size in ranges]}
                state['translations'].append(entry)
        state['exceptions'] = p.get_exc_count()
        if state['exceptions']:
            raise ValueError("CPU exception after ISP translation")
        state['error'] = hex(p.read32(dart_base + 0x100))
        if state['error'] != '0x0':
            raise ValueError("ISP DART fault after mapping")
        persist('DART' + str(instance) + ' firmware mappings verified')
    return darts
