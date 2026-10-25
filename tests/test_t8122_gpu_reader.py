import importlib.util
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    'reader', Path(__file__).resolve().parents[1] / 'tools/read-t8122-gpu-diagnostics.py')
reader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reader)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in {
            'schema-version': b'\0\0\0\1',
            'encoding': b'raw-adt-little-endian\0',
            'complete': b'',
        }.items():
            (self.root / name).write_bytes(value)
        for name in ('sgx', 'gfx-asc'):
            node = self.root / name
            node.mkdir()
            (node / 'adt-path').write_bytes(f'/arm-io/{name}\0'.encode())

    def test_little_endian_and_opaque_tail(self):
        (self.root / 'sgx/adt,reg').write_bytes(b'\x01\0\0\0\x02\0\0\0')
        (self.root / 'sgx/adt,device').write_bytes(b'\x01\x02\x03')
        props = reader.read_export(self.root)['nodes']['sgx']['properties']
        self.assertEqual(props['reg']['u64_le'], [0x200000001])
        self.assertEqual(props['reg']['u32_le'], [1, 2])
        self.assertEqual(props['device'], {'bytes': 3, 'hex': '010203'})

    def test_unknown_schema(self):
        (self.root / 'schema-version').write_bytes(b'\0\0\0\2')
        with self.assertRaises(ValueError):
            reader.read_export(self.root)

    def test_incomplete_export(self):
        (self.root / 'complete').unlink()
        with self.assertRaises(FileNotFoundError):
            reader.read_export(self.root)

    def test_oversized_property(self):
        (self.root / 'sgx/adt,reg').write_bytes(bytes(32769))
        with self.assertRaises(ValueError):
            reader.read_export(self.root)

    def test_revision_two_memory_is_big_endian(self):
        (self.root / 'exporter-revision').write_bytes((2).to_bytes(4, 'big'))
        (self.root / 'usable-memory').write_bytes(
            (0x800000000).to_bytes(8, 'big') + (0x100000).to_bytes(8, 'big'))
        result = reader.read_export(self.root)
        self.assertEqual(result['usable_memory'], {'base': 0x800000000, 'size': 0x100000})
        self.assertEqual(result['exporter_revision'], 2)

    def test_reject_bad_revision_or_memory(self):
        for revision, memory in [(5, bytes(16)), (2, bytes(15)), (2, bytes(16)),
                                 (2, bytes([255]) * 16)]:
            with self.subTest(revision=revision, memory=memory):
                (self.root / 'exporter-revision').write_bytes(revision.to_bytes(4, 'big'))
                (self.root / 'usable-memory').write_bytes(memory)
                with self.assertRaises(ValueError):
                    reader.read_export(self.root)

    def test_revision_three_image_identity_is_not_abi_support(self):
        (self.root / 'exporter-revision').write_bytes((3).to_bytes(4, 'big'))
        (self.root / 'usable-memory').write_bytes(
            (0x800000000).to_bytes(8, 'big') + (0x100000).to_bytes(8, 'big'))
        identity = {
            'image-uuid': bytes(range(16)),
            'image-patchbay-offset': (0x1000).to_bytes(4, 'big'),
            'image-patchbay-size': (0x200).to_bytes(4, 'big'),
            'image-text-size': (0x64000).to_bytes(4, 'big'),
        }
        for name, value in identity.items():
            (self.root / name).write_bytes(value)
        result = reader.read_export(self.root)
        self.assertEqual(result['exporter_revision'], 3)
        self.assertEqual(result['firmware_image']['uuid'], bytes(range(16)).hex())
        self.assertFalse(result['firmware_image']['abi_identified'])

        (self.root / 'image-text-size').unlink()
        with self.assertRaises(ValueError):
            reader.read_export(self.root)

    def test_revision_four_version_is_validated_and_not_abi_support(self):
        (self.root / 'exporter-revision').write_bytes((4).to_bytes(4, 'big'))
        (self.root / 'usable-memory').write_bytes(
            (0x800000000).to_bytes(8, 'big') + (0x100000).to_bytes(8, 'big'))
        identity = {
            'image-uuid': bytes(range(16)),
            'image-patchbay-offset': (0x1000).to_bytes(4, 'big'),
            'image-patchbay-size': (0x200).to_bytes(4, 'big'),
            'image-text-size': (0x64000).to_bytes(4, 'big'),
        }
        for name, value in identity.items():
            (self.root / name).write_bytes(value)
        version = self.root / 'image-rtkit-version'
        version.write_bytes(b'RTKit-2419.140.12\0')
        result = reader.read_export(self.root)
        self.assertEqual(result['firmware_image']['rtkit_version'], 'RTKit-2419.140.12')
        self.assertFalse(result['firmware_image']['abi_identified'])
        for raw in (b'RTKit-\0', b'RTKit-1\n\0', b'RTKit-1', b'RTKit-' + b'x' * 64 + b'\0'):
            with self.subTest(raw=raw):
                version.write_bytes(raw)
                with self.assertRaises(ValueError):
                    reader.read_export(self.root)


if __name__ == '__main__':
    unittest.main()
