import struct

import pytest
np = pytest.importorskip("numpy")
from m1n1.fw.isp.isp_pixels import p010_to_rgb


def packed(y, u=128, v=128, stride=4, luma_size=8):
    data = bytearray(luma_size + stride)
    for row in range(2):
        struct.pack_into('<2H', data, row * stride, y * 256, y * 256)
    struct.pack_into('<2H', data, luma_size, u * 256, v * 256)
    return data


@pytest.mark.parametrize('y,expected', [(16, 0), (235, 255)])
def test_video_range_endpoints(y, expected):
    rgb = p010_to_rgb(packed(y), 2, 2, 4, 8)
    assert rgb.shape == (2, 2, 3)
    assert rgb.dtype == np.uint8
    assert np.all(rgb == expected)


def test_cbcr_order_and_bt709_red():
    rgb = p010_to_rgb(packed(63, 102, 240), 2, 2, 4, 8)
    assert np.all(rgb == [255, 1, 0])


def test_quarter_level_luma_precision():
    data = struct.pack("<6H", *([66 << 6] * 4 + [512 << 6] * 2))
    assert np.all(p010_to_rgb(data, 2, 2, 4, 8) == 1)


def test_row_and_plane_padding_are_not_pixels():
    data = packed(16, stride=8, luma_size=32)
    data[4:8] = b'\xff' * 4
    data[12:32] = b'\xff' * 20
    assert np.all(p010_to_rgb(data, 2, 2, 8, 32) == 0)


def test_truncated_or_misaligned_samples_rejected():
    with pytest.raises(ValueError):
        p010_to_rgb(packed(16)[:-1], 2, 2, 4, 8)
    data = packed(16)
    data[0] |= 1
    with pytest.raises(ValueError):
        p010_to_rgb(data, 2, 2, 4, 8)


@pytest.mark.parametrize('geometry', [(0, 2, 4, 8), (3, 2, 6, 12),
                                     (2, 3, 4, 12), (2, 2, 2, 4),
                                     (2, 2, 5, 10), (2, 2, 4, 4), (2, 2, 4, 9)])
def test_invalid_geometry(geometry):
    with pytest.raises(ValueError):
        p010_to_rgb(packed(16), *geometry)
