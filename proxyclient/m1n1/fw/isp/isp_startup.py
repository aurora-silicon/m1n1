# SPDX-License-Identifier: MIT
"""Pinned T6040 firmware launch; no automatic teardown after mutation."""
import time
import hashlib
import struct

from .isp_profile import T6040_25G76


class T6040ISPStartup:
    def __init__(self, *, proxy, guard, persist, result, isp_base, plan):
        if isp_base != 0x580000000:
            raise ValueError('Unexpected T6040 ISP register base')
        self.proxy = proxy
        self.guard = guard
        self.persist = persist
        self.result = result
        self.base = isp_base
        self.plan = plan
        self.gpio = isp_base + 0x29241d0
        self.access_ready = False
        self.gpio_ready = False
        self.start_attempted = False

    def configure_firmware_access(self, dart_node):
        self.access_ready = False
        self.gpio_ready = False
        p, guard, persist = self.proxy, self.guard, self.persist
        r, base, plan = self.result, self.base, self.plan
        guard()
        if p.read32(base + 0x1600044) != 0:
            raise ValueError('ISP firmware is already running')
        dapf_base, dapf_size = dart_node.get_reg(5)
        if dapf_base != 0x5828d0000 or dapf_size != 0x4000:
            raise ValueError('Unexpected ISP DAPF aperture')
        records = dart_node.getprop('dapf-instance-0')
        if len(records) != 11:
            raise ValueError('Unexpected ISP DAPF record count')
        r['dapf'] = []
        for index, entry in enumerate(records):
            if entry.start > entry.end or entry.r0h != 3 or entry.r0l != 1:
                raise ValueError('Invalid ISP DAPF record')
            address = dapf_base + index * 0x40
            desired = {0: entry.r0h << 4 | entry.r0l, 4: entry.r4, 8: entry.start, 16: entry.end, 32: entry.r20}
            state = {'index': index, 'address': hex(address), 'before': {}, 'after': {}}
            r['dapf'].append(state)
            for offset, value in desired.items():
                guard()
                read = p.read64 if offset in (8, 16) else p.read32
                state['before'][hex(offset)] = hex(read(address + offset))
            persist('before DAPF record ' + str(index))
            if p.get_exc_count() != 0:
                raise ValueError('Proxy exception before DAPF write')
            for offset in (4, 8, 16, 0, 32):
                guard()
                write = p.write64 if offset in (8, 16) else p.write32
                write(address + offset, desired[offset])
            for offset, value in desired.items():
                guard()
                read = p.read64 if offset in (8, 16) else p.read32
                actual = read(address + offset)
                state['after'][hex(offset)] = hex(actual)
                if actual != (value | 3 if offset == 16 else value):
                    raise ValueError('ISP DAPF readback differs')
            persist('DAPF record verified ' + str(index))
        r['native_contexts'] = []
        for instance, dart_base in enumerate((0x5828c0000, 0x5828e0000, 0x582900000)):
            guard()
            tcrs = [p.read32(dart_base + 0x1000 + 4 * sid) for sid in range(16)]
            expected = [1] * 16
            expected[0] = 9
            for sid in (1, 6, 7, 10, 11):
                expected[sid] = 0x80
            if instance == 0:
                expected[15] = 2
            if instance == 1:
                expected[14] = 2
            if tcrs != expected:
                raise ValueError('ISP native DART context differs')
            if p.read32(dart_base + 0xc00) != 1 or p.read32(dart_base + 0x100) != 0:
                raise ValueError('ISP DART enable or error state differs')
            final_ttbr = p.read32(dart_base + 0x1400)
            if hex(final_ttbr) != r['dart_mappings'][instance]['ttbr']:
                raise ValueError('ISP owned DART root differs')
            r['native_contexts'].append({'instance': instance,
                'tcrs': [hex(x) for x in tcrs], 'ttbr0': hex(final_ttbr),
                'owned_root_match': True, 'enabled_mask': '0x1', 'error': '0x0'})
        persist('native alias policy read back before firmware start')
        if p.get_exc_count() != 0:
            raise ValueError('Proxy exception before firmware start')
        text_entry = next(seg['remap'] | 1 for seg in plan if seg['name'] == '__TEXT')
        if p.read64(base + 0x1050000) != text_entry:
            raise ValueError('ISP firmware entry remap differs')
        gpio = base + 0x29241d0
        r['gpio_before'] = [hex(p.read32(gpio + 4 * x)) for x in range(8)]
        if r['gpio_before'] != ['0x0'] * 8 or p.read32(base + 0x1600044) != 0:
            raise ValueError('ISP GPIO or CONTROL is not reset')
        if p.get_exc_count() != 0:
            raise ValueError('Proxy exception during startup qualification')

        self.access_ready = True

    def initialize_gpio(self):
        self.gpio_ready = False
        if not self.access_ready or self.start_attempted:
            raise ValueError("ISP access phase is not ready for GPIO initialization")
        p, guard, persist = (self.proxy, self.guard, self.persist)
        gpio = self.gpio
        guard()
        if p.read32(self.base + 0x1600044) != 0:
            raise ValueError('Cannot initialize GPIO while ISP firmware is running')
        persist('before firmware GPIO initialization')
        for index in range(8):
            guard()
            p.write32(gpio + 4 * index, 1 if index == 6 else 0)
        if p.get_exc_count() != 0:
            raise ValueError('Proxy exception after GPIO initialization')

        self.gpio_ready = True

    def start_firmware(self, *, armed_at):
        if not self.access_ready or not self.gpio_ready or self.start_attempted:
            raise ValueError("ISP startup phases are not ready or start was already attempted")
        p, guard, persist = self.proxy, self.guard, self.persist
        r, base, gpio = self.result, self.base, self.gpio
        guard()
        if p.read32(base + 0x1600044) != 0:
            raise ValueError('Cannot start ISP firmware while CONTROL is active')
        values = [p.read32(gpio + 4 * index) for index in range(8)]
        if values != [0] * 6 + [1, 0] or p.get_exc_count() != 0:
            raise ValueError('ISP GPIO changed before firmware start')
        self.start_attempted = True
        persist('before modern CONTROL0 to CONTROL10 first start')
        p.write32(base + 0x1600044, 0)
        r['firmware_start_attempted'] = True
        persist('first start write about to issue')
        p.write32(base + 0x1600044, 0x10)
        r['control_after_start'] = hex(p.read32(base + 0x1600044))
        if r['control_after_start'] != '0x10' or p.get_exc_count() != 0:
            raise ValueError('ISP CONTROL did not enter running state')
        deadline = time.monotonic() + 10
        last = None
        r['gpio_transitions'] = []
        while time.monotonic() < deadline:
            guard()
            values = [p.read32(gpio + 4 * x) for x in range(8)]
            if p.get_exc_count() != 0:
                raise ValueError("Proxy exception during firmware handshake polling")
            if values != last:
                r['gpio_transitions'].append({
                    'seconds_since_arm': round(time.monotonic() - armed_at, 3),
                    'values': [hex(x) for x in values]})
                last = values
                persist('first handshake polling')
            if values[7] == 0x8042006:
                r['first_handshake'] = 'passed'
                break
            time.sleep(0.01)
        else:
            r['first_handshake'] = 'no magic within 10 seconds'
            persist('first firmware handshake timed out; resources retained')
            raise TimeoutError('ISP first firmware handshake')

    def complete_boot(self, *, u, iface, darts, segments):
        """Allocate, verify and publish the pinned boot block after launch."""
        if self.result.get('first_handshake') != 'passed' or not self.start_attempted:
            raise ValueError('ISP first handshake is not qualified')
        if getattr(self, 'boot_attempted', False):
            raise ValueError('ISP boot block was already attempted')
        if self.result.get('boot_block_reader_source_match') is not True:
            raise ValueError('ISP resident boot-block reader is not qualified')
        expected_bases = (0x5828c0000, 0x5828e0000, 0x582900000)
        mappings = self.result.get('dart_mappings', [])
        if len(darts) != 3 or len(mappings) != 3:
            raise ValueError('ISP boot requires three qualified DART mappings')
        if tuple(base for base, _ in darts) != expected_bases:
            raise ValueError('ISP boot DART bases or ordering differ')
        if len({id(dart) for _, dart in darts}) != 3:
            raise ValueError('ISP boot DART objects must be distinct')
        for index, (base, dart) in enumerate(darts):
            record = mappings[index]
            try:
                matches = (record['instance'] == index and
                           int(record['base'], 16) == base and
                           int(record['ttbr'], 16) == dart.regs.TTBR[0].val)
            except (KeyError, TypeError, ValueError, AttributeError):
                matches = False
            if not matches:
                raise ValueError('ISP boot DART ownership record differs')
        self.boot_attempted = True
        p, guard, persist = self.proxy, self.guard, self.persist
        result, plan, gpio = self.result, self.plan, self.gpio
        def context_valid(index, dart_base):
            guard()
            expected_root = int(result['dart_mappings'][index]['ttbr'], 16)
            if p.read32(dart_base + 0x1400) != expected_root:
                raise ValueError('Live SID0 root differs from owned root')
            if p.read32(dart_base + 0xc00) != 1 or p.read32(dart_base + 0x1000) != 9:
                raise ValueError('SID0 context changed')
            if any(p.read32(dart_base + 0x1000 + 4 * sid) != 0x80
                   for sid in (1, 6, 7, 10, 11)):
                raise ValueError('Native SID aliases changed')
            if p.read32(dart_base + 0x100) != 0:
                raise ValueError('ISP DART fault before boot publication')
            if p.get_exc_count() != 0:
                raise ValueError('CPU exception during ISP context validation')

        guard()
        reply = [p.read32(gpio + 4 * index) for index in range(8)]
        if reply[7] != 0x8042006 or reply[0] != 7 or reply[2] != 2:
            raise ValueError('Unexpected first handshake')
        extra_size = reply[3]
        if not 0 < extra_size <= 0x7000000 or extra_size % 0x4000:
            raise ValueError('Unexpected firmware heap size')
        node = u.adt['/arm-io/isp0']
        base, size = node.get_reg(0)
        if base != self.base or size < 0x2924468:
            raise ValueError('ISP boot aperture differs from startup context')
        result['isp_register_span'] = {'base': base, 'size': size}
        platform = node.getprop('sensor-type')
        scheme = node.getprop('cam-connections-scheme')
        if isinstance(platform, bytes):
            platform = int.from_bytes(platform, 'little')
        if isinstance(scheme, bytes):
            scheme = int.from_bytes(scheme, 'little')
        if platform != 5 or scheme != 16:
            raise ValueError('Platform/scheme differ from reviewed J616s baseline')
        data = next(segment for segment in plan if segment['name'] == '__DATA')
        text = next(segment for segment in plan if segment['name'] == '__TEXT')
        if text['remap'] != 1 << 40:
            raise ValueError('Unexpected firmware remap base')
        firmware_end = data['remap'] + data['size']
        shared_base = firmware_end - text['remap']
        if not 0 < shared_base < 0x10000000:
            raise ValueError('Unexpected shared range start')
        ipc_iova = firmware_end + 0x4000
        ipc_size = 0x1c000
        extra_iova = ipc_iova + ipc_size + 0x4000
        args_iova, command_iova, block = T6040_25G76.prepare_bootargs(
            ipc_iova=ipc_iova, ipc_size=ipc_size, args_offset=reply[1],
            extra_iova=extra_iova, extra_size=extra_size,
            shared_base=shared_base, shared_size=0x10000000 - shared_base,
            platform_id=platform, camera_scheme=scheme)
        profile_boot = T6040_25G76.boot_layout(segments,
            args_offset=reply[1], extra_size=extra_size)
        if (ipc_iova, ipc_size, args_iova, command_iova, extra_iova, extra_size,
                shared_base, 0x10000000 - shared_base) != (
                profile_boot.ipc_iova, profile_boot.ipc_size, profile_boot.args_iova,
                profile_boot.command_iova, profile_boot.extra_iova, profile_boot.extra_size,
                profile_boot.shared_base, profile_boot.shared_size):
            raise ValueError('Existing boot formula differs from modern profile')
        result['modern_profile_boot_layout_matches'] = True
        allocations = []
        for name, iova, size in (('ipc', ipc_iova, ipc_size),
                                  ('extra', extra_iova, extra_size)):
            guard()
            pa = u.heap.memalign(0x4000, size)
            if not u.heap_base <= pa < pa + size <= u.heap_top:
                raise ValueError('Allocation outside owned host heap')
            if any(iova < seg['remap'] + seg['size'] and seg['remap'] < iova + size
                   for seg in plan):
                raise ValueError('New mapping overlaps firmware')
            allocation = {'name': name, 'iova': iova, 'pa': pa, 'size': size}
            allocations.append(allocation)
            result['boot_allocations'] = allocations
            persist('before zeroing owned ' + name)
            p.memset32(pa, 0, size)
            p.dc_cvac(pa, size)
        if allocations[0]['pa'] < allocations[1]['pa'] + extra_size and allocations[1]['pa'] < allocations[0]['pa'] + ipc_size:
            raise ValueError('Physical allocations overlap')
        result['second_stage_mappings'] = []
        for index, (dart_base, dart) in enumerate(darts):
            context_valid(index, dart_base)
            for allocation in allocations:
                guard()
                dart.iomap_at(0, allocation['iova'], allocation['pa'], allocation['size'],
                              enable=False)
            facts = {'base': hex(dart_base), 'tables': [], 'walks': []}
            result['second_stage_mappings'].append(facts)
            for pa, entries in dart.pt_cache.items():
                guard()
                if not u.heap_base <= pa < pa + 0x4000 <= u.heap_top:
                    raise ValueError('Table outside owned host heap')
                p.dc_cvac(pa, 0x4000)
                expected = struct.pack('<2048Q', *entries)
                observed = iface.readmem(pa, 0x4000)
                if expected != observed:
                    raise ValueError('Published table readback differs')
                facts['tables'].append({'pa': hex(pa), 'sha256': hashlib.sha256(observed).hexdigest()})
            guard()
            p.write32(dart_base + 0x80, 0x100)
            deadline = time.monotonic() + 1
            while p.read32(dart_base + 0x80) & (1 << 31):
                guard()
                if time.monotonic() > deadline:
                    raise TimeoutError('SID0 TLB invalidation')
            dart.invalidate_cache()
            for allocation in allocations:
                for offset in (0, allocation['size'] - 0x4000):
                    guard()
                    ranges = dart.iotranslate(0, allocation['iova'] + offset, 0x4000)
                    if ranges != [(allocation['pa'] + offset, 0x4000)]:
                        raise ValueError('Owned mapping software walk differs')
                    facts['walks'].append({'name': allocation['name'], 'offset': hex(offset),
                                           'pa': hex(allocation['pa'] + offset)})
            if p.read32(dart_base + 0x100) or p.read32(dart_base + 0xc00) != 1:
                raise ValueError('DART error/mask changed')
        ipc = allocations[0]
        args_pa = ipc['pa'] + args_iova - ipc_iova
        guard()
        iface.writemem(args_pa, block)
        p.dc_cvac(args_pa, len(block))
        if iface.readmem(args_pa, len(block)) != block:
            raise ValueError('Boot block readback differs')
        if p.get_exc_count() or p.read32(0x581600044) != 0x10:
            raise ValueError('CPU exception/control change before submission')
        result['boot_block'] = {'args_iova': hex(args_iova), 'command_iova': hex(command_iova),
                                'platform': platform, 'scheme': scheme,
                                'size': len(block), 'shared_base': hex(shared_base),
                                'sha256': hashlib.sha256(block).hexdigest(),
                                'readback_matches': True}
        persist('before second handshake boot block submission')
        for index, (dart_base, _) in enumerate(darts):
            context_valid(index, dart_base)
        guard()
        if p.get_exc_count() != 0:
            raise ValueError('CPU exception before boot-block publication')
        p.write32(gpio, args_iova & 0xffffffff)
        p.write32(gpio + 4, args_iova >> 32)
        p.write32(gpio + 28, 0xf7fbdff9)
        if p.get_exc_count():
            raise ValueError('CPU exception during boot-block publication')
        deadline = time.monotonic() + 5
        while p.read32(gpio + 28) != 0x8042006:
            guard()
            if time.monotonic() > deadline:
                result['second_handshake'] = 'no magic within5seconds'
                result['second_gpio'] = [hex(p.read32(gpio + 4 * index)) for index in range(8)]
                persist('second handshake timed out; resources retained')
                raise TimeoutError('ISP second firmware handshake')
            time.sleep(.01)
        result['second_handshake'] = 'passed'
        guard()
        table_iova = p.read32(gpio) | (p.read32(gpio + 4) << 32)
        table_size = reply[0] * 0x100
        if not ipc_iova <= table_iova < table_iova + table_size <= ipc_iova + ipc_size:
            raise ValueError('Channel table outside owned IPC')
        table_pa = ipc['pa'] + table_iova - ipc_iova
        p.dc_ivac(table_pa & ~127, ((table_pa + table_size + 127) & ~127) - (table_pa & ~127))
        raw = iface.readmem(table_pa, table_size)
        result['channel_table_iova'] = hex(table_iova)
        result['channel_table_sha256'] = hashlib.sha256(raw).hexdigest()
        result['channel_table_hex'] = raw.hex()
        persist('owned channel table captured before parsing')
        entries = T6040_25G76.parse_channels(raw, table_iova=table_iova,
            count=reply[0], boot=profile_boot)
        result['modern_profile_channels_validated'] = True
        # Preserve source words without assigning interrupt semantics.
        channels = [{'name': entry.name, 'type': entry.type,
                     'raw_source': hex(entry.source), 'num': entry.count,
                     'iova': hex(entry.iova)} for entry in entries]
        result['channels'] = channels
        names = {'TERMINAL', 'IO', 'DEBUG', 'BUF_H2T', 'BUF_T2H',
                 'SHAREDMALLOC', 'IO_T2H'}
        if p.get_exc_count():
            raise ValueError('CPU exception after second handshake')
        if {channel['name'] for channel in channels} != names:
            raise ValueError('Channel name set differs from matching firmware')
        result['second_stage_qualification'] = 'Boot block and IPC metadata only; no ring writes, IRQ, sensor or capture commands'
        persist('second handshake/channel metadata qualified; retain watchdog/resources')
