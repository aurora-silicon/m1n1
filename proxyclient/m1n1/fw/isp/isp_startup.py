# SPDX-License-Identifier: MIT
"""Pinned T6040 firmware launch; no automatic teardown after mutation."""
import time


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
