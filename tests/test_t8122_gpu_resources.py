"""Synthetic data only. Machine captures must stay outside the repository."""
import importlib.util
from pathlib import Path
import struct
import unittest

SPEC = importlib.util.spec_from_file_location(
    'resources', Path(__file__).resolve().parents[1] / 'tools/t8122_gpu_resources.py')
resources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resources)


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.export = {'schema_version': 1, 'nodes': {
            name: {'properties': {}} for name in ('arm-io', 'sgx', 'gfx-asc', 'pmgr')}}
        self.put('arm-io', 'compatible', b'arm-io,t8122\0')
        for prop in ('#address-cells', '#size-cells'):
            self.pack('arm-io', prop, '<I', 2)
        self.pack('arm-io', 'ranges', '<QQQ', 0, 0x100000000, 0x100000)
        for node in ('sgx', 'gfx-asc'):
            self.pack(node, 'reg', '<QQ', 0x2000, 0x1000)
            self.pack(node, 'interrupts', '<II', 7, 8)
            self.pack(node, 'power-gates', '<I', 300)
        self.pack('sgx', 'interrupts-valid', '<I', 1)
        self.pack('pmgr', 'reg', '<QQ', 0x6000, 0x100)
        self.pack('pmgr', 'ps-regs', '<III', 0, 0x10, 0)
        self.devices = bytearray(96)
        # Real parent (ID 100) and virtual gate (ID 300).
        struct.pack_into('<H', self.devices, 26, 100)
        self.devices[10] = 2
        self.devices[32:38] = b'PARENT'
        self.devices[48] = 0x10
        struct.pack_into('<HH', self.devices, 52, 100, 0)
        struct.pack_into('<H', self.devices, 74, 300)
        self.devices[80:84] = b'GATE'
        self.put('pmgr', 'devices', self.devices)
        self.pack('sgx', 'perf-state-count', '<I', 2)
        self.pack('sgx', 'perf-state-table-count', '<I', 1)
        for prop in ('perf-states', 'perf-states-sram'):
            self.pack('sgx', prop, '<IIII', 100, 500, 200, 600)

    def put(self, node, prop, data):
        self.export['nodes'][node]['properties'][prop] = {
            'bytes': len(data), 'hex': data.hex()}

    def pack(self, node, prop, fmt, *values):
        self.put(node, prop, struct.pack(fmt, *values))

    def decode(self):
        return resources.decode(self.export)

    def test_translation_virtual_gate_and_performance(self):
        result = self.decode()
        self.assertEqual(result['nodes']['sgx']['registers'][0]['physical_address'], 0x100002000)
        self.assertEqual(result['nodes']['sgx']['interrupt_valid'], [True, False])
        parent, gate = result['power_dependencies']
        self.assertEqual(parent['state_register'], 0x100006020)
        self.assertNotIn('state_register', gate)
        self.assertEqual(gate['parents'], [100])
        self.assertEqual(result['performance_tables']['perf-states'][1],
                         {'frequency_raw': 200, 'voltage_raw': 600})
        self.assertFalse(result['hardware_activation'])

    def test_reject_register_crossing_range(self):
        self.pack('sgx', 'reg', '<QQ', 0xffff0, 0x20)
        with self.assertRaises(resources.DecodeError): self.decode()

    def test_reject_conflicting_range(self):
        self.pack('arm-io', 'ranges', '<QQQQQQ', 0, 0x100000000, 0x100000,
                  0x2000, 0x200000000, 0x1000)
        with self.assertRaises(resources.DecodeError): self.decode()

    def test_reject_truncated_table(self):
        self.put('pmgr', 'devices', self.devices[:-1])
        with self.assertRaises(resources.DecodeError): self.decode()

    def test_reject_cycle(self):
        struct.pack_into('<H', self.devices, 4, 300)
        self.put('pmgr', 'devices', self.devices)
        with self.assertRaises(resources.DecodeError): self.decode()

    def test_reject_missing_parent(self):
        struct.pack_into('<H', self.devices, 52, 400)
        self.put('pmgr', 'devices', self.devices)
        with self.assertRaises(resources.DecodeError): self.decode()

    def test_reject_ps_index(self):
        self.devices[11] = 1
        self.put('pmgr', 'devices', self.devices)
        with self.assertRaises(resources.DecodeError): self.decode()

    def test_reject_ps_address_outside_extent(self):
        self.devices[10] = 255
        self.put('pmgr', 'devices', self.devices)
        with self.assertRaises(resources.DecodeError): self.decode()

    def test_reject_perf_count_mismatch(self):
        self.pack('sgx', 'perf-state-count', '<I', 3)
        with self.assertRaises(resources.DecodeError): self.decode()

    def test_reject_irq_mask_outside_array(self):
        self.pack('sgx', 'interrupts-valid', '<I', 4)
        with self.assertRaises(resources.DecodeError): self.decode()

    def test_reject_gpu_sram_frequency_mismatch(self):
        self.pack('sgx', 'perf-states-sram', '<IIII', 100, 500, 201, 600)
        with self.assertRaisesRegex(resources.DecodeError, 'frequencies differ'):
            self.decode()

    def test_preserve_nonmonotonic_performance_order(self):
        for prop in ('perf-states', 'perf-states-sram'):
            self.pack('sgx', prop, '<IIII', 200, 500, 100, 600)
        result = self.decode()['performance_tables']['perf-states']
        self.assertEqual([p['frequency_raw'] for p in result], [200, 100])

    def test_thermal_bits_preserved_without_interpretation(self):
        self.pack('sgx', 'gpu-fast-die0-integral-gain', '<I', 0xc1200000)
        result = self.decode()['thermal_metadata']
        self.assertEqual(result['raw_u32'], {'gpu-fast-die0-integral-gain': 0xc1200000})
        self.assertIn('gpu-fast-die0-sensor-mask', result['missing_properties'])
        self.assertFalse(result['interpretation_validated'])

    def test_missing_topology_is_explicit(self):
        result = self.decode()['topology_metadata']
        self.assertEqual(result['raw_u32'], {})
        self.assertEqual(result['missing_properties'], list(resources.TOPOLOGY_PROPERTIES))
        self.assertFalse(result['interpretation_validated'])

    def test_reject_malformed_optional_metadata(self):
        for prop in ('gpu-fast-die0-target', 'gpu-core-count'):
            with self.subTest(prop=prop):
                self.put('sgx', prop, b'\x01\x00')
                with self.assertRaises(resources.DecodeError): self.decode()
                del self.export['nodes']['sgx']['properties'][prop]

    def test_reject_length_mismatch(self):
        self.export['nodes']['sgx']['properties']['reg']['bytes'] = 1
        with self.assertRaises(resources.DecodeError): self.decode()

    def firmware_fixture(self):
        self.export.update(exporter_revision=2, usable_memory={'base': 0x100000, 'size': 0x100000})
        self.export['nodes']['chosen'] = {'properties': {}}
        self.pack('chosen', 'dram-base', '<Q', 0x100000)
        self.pack('chosen', 'dram-size', '<Q', 0x1000000)
        for i, prefix in enumerate(('gfx-handoff', 'gfx-shared-region',
                                     'gfx-shared-l2-region', 'gpu-region')):
            self.pack('sgx', prefix + '-base', '<Q', 0x300000 + i * 0x4000)
            self.pack('sgx', prefix + '-size', '<Q', 0x4000)
        self.put('gfx-asc', 'segment-names', b'__TEXT;__DATA\0')
        self.pack('gfx-asc', 'segment-ranges', '<QQQIIQQQII',
                  0x400000, 0x80000000, 0x400000, 0x4000, 1,
                  0x404000, 0x80004000, 0x404000, 0x4000, 0)

    def test_firmware_geometry_does_not_identify_abi(self):
        self.firmware_fixture()
        result = self.decode()['firmware_layout']
        self.assertTrue(result['available'])
        self.assertFalse(result['abi_identified'])
        self.assertEqual(result['segments'][1]['physical_address'], 0x404000)
        self.assertEqual(len(result['uat_regions']), 4)

    def test_reject_firmware_in_usable_ram(self):
        self.firmware_fixture()
        self.export['usable_memory']['size'] = 0x500000
        with self.assertRaises(resources.DecodeError): self.decode()

    def test_reject_firmware_outside_dram(self):
        self.firmware_fixture()
        self.pack('chosen', 'dram-size', '<Q', 0x210000)
        with self.assertRaises(resources.DecodeError): self.decode()

    def test_reject_overlapping_firmware_and_uat(self):
        self.firmware_fixture()
        self.pack('sgx', 'gpu-region-base', '<Q', 0x400000)
        with self.assertRaises(resources.DecodeError): self.decode()

    def test_reject_nub_mismatch(self):
        self.firmware_fixture()
        self.export['nodes']['gfx-nub'] = {'properties': {}}
        self.put('gfx-nub', 'segment-names', b'OTHER\0')
        with self.assertRaises(resources.DecodeError): self.decode()

    def test_reject_segment_count_mismatch(self):
        self.firmware_fixture()
        self.put('gfx-asc', 'segment-names', b'__TEXT\0')
        with self.assertRaises(resources.DecodeError): self.decode()

    def test_gpu_count_excludes_off_state(self):
        self.pack('sgx', 'gpu-num-perf-states', '<I', 1)
        self.decode()
        self.pack('sgx', 'gpu-num-perf-states', '<I', 2)
        with self.assertRaises(resources.DecodeError): self.decode()


if __name__ == '__main__':
    unittest.main()
