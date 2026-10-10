# SPDX-License-Identifier: MIT
"""T6040 capture wire layouts; callers retain mapping and lease ownership."""
import struct


def output_report_index(report, offset, surfaces):
    """Match a single-capture report; lease/ACK ordering is caller-owned."""
    if type(offset) is not int or offset < 0 or offset + 64 > len(report):
        raise ValueError('Truncated output descriptor')
    addresses = struct.unpack_from('<4Q', report, offset)
    planes, pool, tag = struct.unpack_from('<2IQ', report, offset + 0x30)
    if planes != 2 or pool != 9 or any(address >> 32 for address in addresses):
        raise ValueError('Unexpected modern output descriptor')
    matches = [index for index, surface in enumerate(surfaces)
               if addresses == (surface['iova'] & 0xffffffff,
                                surface['uv_iova'] & 0xffffffff, 0, 0)]
    if len(matches) != 1 or tag != 0x500 + matches[0]:
        raise ValueError('Output descriptor does not identify one submitted surface')
    return matches[0]


def output_geometry(width, height):
    if type(width) is not int or type(height) is not int:
        raise ValueError('Dimensions must be integers')
    if (width, height) not in ((1920, 1080), (1280, 720)):
        raise ValueError('Unqualified preview geometry')
    stride = (width * 2 + 63) & ~63
    luma_size = (stride * height + 0x3fff) & ~0x3fff
    chroma_size = (stride * height // 2 + 0x3fff) & ~0x3fff
    return {'width': width, 'height': height, 'stride': stride,
            'luma_size': luma_size, 'chroma_size': chroma_size,
            'surface_size': luma_size + chroma_size}


def pool_parameters(pool, count, size, stride, chroma_size=0):
    if (type(pool) is not int or pool not in (0, 3, 9)
            or type(count) is not int or not 0 < count < 1 << 16
            or any(type(v) is not int or not 0 < v < 1 << 32 for v in (size, stride))
            or type(chroma_size) is not int or not 0 <= chroma_size < 1 << 32
            or bool(chroma_size) != (pool == 9)):
        raise ValueError('Unqualified pool configuration')
    words = [0] * 37
    words[:4] = [0, pool | (count << 16), size, stride]
    if chroma_size:
        words[10:12] = [chroma_size, stride]
    words[35] = 2 if chroma_size else 1
    return words


def descriptor_batch(surfaces, pool, tag_base, tags=None):
    if not 0 < len(surfaces) < 10:
        raise ValueError('Only small fixed-size batches are qualified')
    if tags is not None and len(tags) != len(surfaces):
        raise ValueError("Descriptor tag count differs")
    if type(pool) is not int or pool not in (0, 2, 3, 9):
        raise ValueError('Unqualified descriptor pool')
    wire_tags = tags
    if tags is None and type(tag_base) is int:
        wire_tags = [tag_base + i for i in range(len(surfaces))]
    if wire_tags is None or any(type(tag) is not int or not 0 <= tag < 1 << 64 for tag in wire_tags):
        raise ValueError('Descriptor tag outside u64 range')
    for surface in surfaces:
        addresses = (surface['iova'], surface.get('uv_iova', 0))
        if (any(type(v) is not int or not 0 <= v < 1 << 42 for v in addresses)
                or not addresses[0] or bool(addresses[1]) != (pool == 9)):
            raise ValueError('Descriptor address or plane count differs from pool')
    data = bytearray(0x280)
    struct.pack_into('<2Q', data, 0, 1, len(surfaces))
    for index, surface in enumerate(surfaces):
        struct.pack_into('<4Q6IQ', data, 0x10 + index * 64,
                         surface['iova'], surface.get('uv_iova', 0), 0, 0,
                         0x40000000, 0x40000000 if pool == 9 else 0,
                         0, 0, 2 if pool == 9 else 1, pool, wire_tags[index])
    return bytes(data)


class T6040PreviewConfiguration:
    """Configure the tested IMX958 P010 route; startup and buffers are caller-owned."""
    def __init__(self, query):
        self.query = query
        self.attempted = False

    def configure(self, info, preset, width=1280, height=720):
        if self.attempted:
            raise ValueError('Preview configuration already attempted; cold recovery required')
        geometry = output_geometry(width, height)
        if (len(info) != 0x190 or len(preset) != 0x120
                or struct.unpack_from('<3I', info) != (0, 0x10d, 0)
                or struct.unpack_from('<4I', preset) != (0, 0x106, 0, 5)
                or struct.unpack_from('<I', info, 0x20)[0] != 0x958
                or not 6 <= struct.unpack_from('<I', info, 0x60)[0] <= 64
                or struct.unpack_from('<2H', preset, 0x10) != (1920, 2160)
                or info[0x138] != 0 or struct.unpack_from('<I', info, 0x68)[0] != 0x4d00):
            raise ValueError('Unexpected IMX958 preset or render route')
        self.attempted = True
        query = self.query
        query('FLICKER_SENSOR_SET', 0x24, 0xc, (0,), outsize=0)
        query('CH_SBS_ENABLE', 0x13b, 0x10, (0, 1), outsize=0)
        query('CH_CAMERA_CONFIG_SELECT', 0x107, 0x10, (0, 5), outsize=0)
        query('CH_BUFFER_RECYCLE_MODE_SET', 0x10e, 0x10, (0, 1), outsize=0)
        query('CH_BUFFER_RECYCLE_START', 0x10f, 0xc, (0,), outsize=0)
        query('CH_CROP_SCL1_SET', 0x80c, 0x1c, (0, 0, 0, width, height), outsize=0)
        query('CH_OUTPUT_CONFIG_SCL1_SET', 0xb09, 0x38,
              (0, width, height, 1, 0x12, geometry['stride'], geometry['stride'],
               0, 0, height, 0, width), outsize=0)
        query('CH_AE_FRAME_RATE_MAX_SET', 0x208, 0x10, (0, 7680), outsize=0)
        query('CH_AE_FRAME_RATE_MIN_SET', 0x20a, 0x10, (0, 3840), outsize=0)
        query('CH_META_POOL_CONFIG_SET', 0x117, 0x9c, pool_parameters(0, 8, 0x8000, 0x8000))
        query('CH_OUTPUT_POOL_CONFIG_SET', 0x117, 0x9c,
              pool_parameters(9, 2, geometry['luma_size'], geometry['stride'], geometry['chroma_size']))
        query('CH_LOCAL_RAW_BUFFER_ENABLE', 0x125, 0x10, (0, 1), outsize=0)
        query('CH_PREVIEW_STREAM_SET', 0xb0d, 0x10, (0, 1), outsize=0)
        query('CH_MASTER_SLAVE_SYNC_MODE_SET', 0x138, 0x10, (0, 0), outsize=0)
        return geometry
