#!/usr/bin/env python3
"""Fill a source-built J613 stage-1 image for its own ESP; never install it."""
import argparse
import struct
from pathlib import Path
import uuid
import zlib

MAGIC = b'AURORA-S1-CFG01'.ljust(16, b'\0')
SIZE = 260

def fill(image, esp_uuid, target, window_ms):
    esp_uuid = str(uuid.UUID(esp_uuid))
    if not 0 <= window_ms <= 99999:
        raise ValueError('proxy window must be 0..99999 ms')
    target = target.removeprefix(';')
    if not target or target.startswith('/') or any(c in target for c in (';', '\\')):
        raise ValueError('target must be a relative ESP path')
    if any(c in ('', '.', '..') for c in target.split('/')):
        raise ValueError('target path contains an invalid component')
    raw = (';' + target).encode('ascii')
    if len(raw) >= 192 or any(c < 0x20 or c >= 0x7f for c in raw):
        raise ValueError('target must fit the ASCII config field')
    if image.count(MAGIC) != 1:
        raise ValueError('exactly one source-built stage-1 config block is required')
    offset = image.index(MAGIC)
    if offset + SIZE > len(image): raise ValueError('truncated config block')
    body = struct.pack('<II40s192s', 1, window_ms, esp_uuid.encode(), raw)
    block = MAGIC + body + struct.pack('<I', zlib.crc32(body))
    return image[:offset] + block + image[offset + SIZE:]

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('image', type=Path)
    p.add_argument('output', type=Path)
    p.add_argument('--esp-uuid', required=True)
    p.add_argument('--target', default='m1n1/boot.bin')
    p.add_argument('--window-ms', type=int, default=0, help='USB proxy window; 0 (default) boots stage 2 at once')
    a = p.parse_args()
    if a.image.resolve() == a.output.resolve(): p.error('output must differ from source image')
    try: result = fill(a.image.read_bytes(), a.esp_uuid, a.target, a.window_ms)
    except (ValueError, UnicodeError) as e: p.error(str(e))
    with a.output.open('xb') as out: out.write(result)

if __name__ == '__main__': main()
