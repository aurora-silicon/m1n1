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


