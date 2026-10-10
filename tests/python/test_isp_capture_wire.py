import pytest

from m1n1.fw.isp.isp_capture import descriptor_batch, output_geometry, output_report_index, pool_parameters


def test_single_plane_wire_fixture():
    wire = descriptor_batch([{'iova': 0x10009000000}], 3, 0x200)
    expected = bytes.fromhex(
        '01000000000000000100000000000000'
        '0000000900010000000000000000000000000000000000000000000000000000'
        '0000004000000000000000000000000001000000030000000002000000000000')
    assert wire == expected + bytes(0x280 - len(expected))


@pytest.mark.parametrize('args', [
    ([{'iova': 1}], 3, (1 << 64)),
    ([{'iova': 1}, {'iova': 2}], 3, (1 << 64) - 1),
    ([{'iova': True}], 3, 0),
    ([{'iova': 1 << 42}], 3, 0),
    ([{'iova': 1}], 9, 0),
    ([{'iova': 1, 'uv_iova': 2}], 3, 0),
])
def test_invalid_descriptor_fields(args):
    with pytest.raises(ValueError):
        descriptor_batch(*args)


@pytest.mark.parametrize('offset', [-64, True, 1, 64])
def test_report_offset_must_identify_full_descriptor(offset):
    with pytest.raises(ValueError):
        output_report_index(bytes(64), offset, [])


@pytest.mark.parametrize('args', [(0, 65536, 1, 1), (9, 2, 1, 1), (0, 2, 1, 1, 2), (3, True, 1, 1)])
def test_pool_fields_do_not_overlap_or_invent_planes(args):
    with pytest.raises(ValueError):
        pool_parameters(*args)


def test_dimensions_are_exact_integers():
    with pytest.raises(ValueError):
        output_geometry(1280.0, 720)


def test_explicit_zero_uv_stays_single_plane():
    assert descriptor_batch([{'iova': 1, 'uv_iova': 0}], 3, 0) == descriptor_batch([{'iova': 1}], 3, 0)
