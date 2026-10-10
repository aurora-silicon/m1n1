# SPDX-License-Identifier: MIT
import struct
import unittest
import pytest
from m1n1.fw.isp.isp_buffer import ISPBufferLeases


def report(surface, pool, tag, planes=None):
    payload = bytearray(64)
    struct.pack_into('<4Q', payload, 0, surface['iova'] & 0xffffffff,
                     surface.get('uv_iova', 0) & 0xffffffff, 0, 0)
    struct.pack_into('<2IQ', payload, 48, planes if planes is not None else (2 if 'uv_iova' in surface else 1), pool, tag)
    return payload


class LeaseOwnership(unittest.TestCase):
    def setUp(self):
        self.outputs = [{'iova': 0x10009000000, 'uv_iova': 0x100091c4000},
                        {'iova': 0x100092a8000, 'uv_iova': 0x1000946c000}]
        self.meta = [{'iova': 0x1000a100000}]
        self.book = ISPBufferLeases({9: self.outputs, 0: self.meta})

    def test_same_surfaces_three_generations_with_distinct_wire_tags(self):
        all_tags = []
        for generation in range(1, 4):
            tags = self.book.prepare(9, [0, 1])
            all_tags.extend(tags)
            keys = [self.book.returned(report(s, 9, tag), 0) for s, tag in zip(self.outputs, tags)]
            self.book.acknowledged(keys)
            for key in keys:
                self.assertEqual(self.book.records[key]['generation'], generation)
                self.book.copied(key)
        self.assertEqual(len(set(all_tags)), 6)

    def test_report_ack_required_before_copy_or_resubmission(self):
        tag = self.book.prepare(9, [0])[0]
        key = self.book.returned(report(self.outputs[0], 9, tag), 0)
        # Publication/doorbell failure leaves this lease awaiting ACK.
        with self.assertRaisesRegex(ValueError, 'before completion report ACK'):
            self.book.copied(key)
        with self.assertRaisesRegex(ValueError, 'still owned'):
            self.book.prepare(9, [0])
        self.book.acknowledged([key])
        self.book.copied(key)
        self.assertEqual(self.book.records[key]['state'], 'host')

    def test_duplicate_return_before_and_after_ack_rejected(self):
        tag = self.book.prepare(9, [0])[0]
        payload = report(self.outputs[0], 9, tag)
        key = self.book.returned(payload, 0)
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.book.returned(payload, 0)
        self.book.acknowledged([key])
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.book.returned(payload, 0)

    def test_prior_generation_tag_rejected_after_requeue(self):
        old = self.book.prepare(9, [0])[0]
        key = self.book.returned(report(self.outputs[0], 9, old), 0)
        self.book.acknowledged([key])
        self.book.copied(key)
        new = self.book.prepare(9, [0])[0]
        self.assertNotEqual(old, new)
        with self.assertRaisesRegex(ValueError, 'Stale'):
            self.book.returned(report(self.outputs[0], 9, old), 0)
        self.assertEqual(self.book.records[key]['state'], 'submitted')

    def test_unknown_alias_planes_and_high_bits_refused(self):
        tag = self.book.prepare(9, [0])[0]
        for mutate in ('address', 'planes', 'high_bits'):
            payload = report(self.outputs[0], 9, tag)
            if mutate == 'address':
                struct.pack_into('<Q', payload, 8, 0xdead000)
            elif mutate == 'planes':
                struct.pack_into('<I', payload, 48, 1)
            else:
                struct.pack_into('<Q', payload, 0, self.outputs[0]['iova'])
            with self.assertRaises(ValueError):
                self.book.returned(payload, 0)
        with self.assertRaises(ValueError):
            self.book.returned(report(self.outputs[0], 99, tag), 0)
        self.assertEqual(self.book.records[9, 0]['state'], 'submitted')

    def test_metadata_single_plane_and_no_implicit_refill(self):
        tag = self.book.prepare(0, [0])[0]
        key = self.book.returned(report(self.meta[0], 0, tag), 0)
        self.assertEqual(self.book.records[key]['state'], 'awaiting_report_ack')
        self.book.acknowledged([key])
        self.assertEqual(self.book.records[key]['state'], 'returned')
        self.book.copied(key)
        self.assertEqual(self.book.records[key]['state'], 'host')
        self.assertEqual(self.book.records[key]['generation'], 1)



def test_captured_rendered_report():
    # Both descriptors from a completed J616s 720p frame (25G76 firmware).
    surfaces = {0: [{'iova': 0x1000a100000}],
                9: [{'iova': 0x10009b9c000, 'uv_iova': 0x10009d60000}]}
    book = ISPBufferLeases(surfaces)
    book.next_tag = 0x301
    book.prepare(0, [0])
    book.next_tag = 0x500
    book.prepare(9, [0])
    metadata = bytes.fromhex('0000100a00000000' + '00' * 40 +
                             '01000000000000000103000000000000')
    output = bytes.fromhex('00c0b909000000000000d60900000000' + '00' * 32 +
                           '02000000090000000005000000000000')
    keys = [book.returned(metadata, 0), book.returned(output, 0)]
    assert keys == [(0, 0), (9, 0)]
    book.acknowledged(keys)
    for key in keys:
        book.copied(key)
    assert all(record['state'] == 'host' for record in book.records.values())


def test_report_cannot_retarget_mutated_submission():
    surface = {'iova': 0x10009000000, 'uv_iova': 0x100091c4000}
    book = ISPBufferLeases({9: [surface]})
    tag = book.prepare(9, [0])[0]
    surface['iova'] += 0x4000
    with pytest.raises(ValueError):
        book.returned(report(surface, 9, tag), 0)


def test_invalid_bounds_and_exhausted_tags():
    for surface in ({'iova': 1 << 42}, {'iova': 0},
                    {'iova': 0x10009000000, 'uv_iova': 1 << 42}):
        with pytest.raises(ValueError):
            ISPBufferLeases({9: [surface]})
    book = ISPBufferLeases({0: [{'iova': 0x10009000000}]})
    with pytest.raises(ValueError):
        book.returned(bytes(63), 0)
    book.next_tag = 1 << 64
    with pytest.raises(ValueError):
        book.prepare(0, [0])


def test_negative_offset_and_low_address_collision():
    surfaces = [{'iova': 0x10009000000, 'uv_iova': 0x100091c4000},
                {'iova': 0x10109000000, 'uv_iova': 0x101091c4000}]
    book = ISPBufferLeases({9: surfaces})
    tag = book.prepare(9, [0])[0]
    payload = report(surfaces[0], 9, tag)
    with pytest.raises(ValueError, match='Truncated'):
        book.returned(payload, -0x40)
    with pytest.raises(ValueError, match='one owned lease'):
        book.returned(payload, 0)
    assert book.records[9, 0]['state'] == 'submitted'
