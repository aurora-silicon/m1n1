#!/usr/bin/env python3
"""Fill a J700 Stage 1 target without adding bytes after STACKBOT."""

import argparse
import hashlib
from pathlib import Path
import re
import struct
import zlib


MAGIC = b"AURORA-S1-CFG01\0"
BODY = struct.Struct("<II40s192s")
BLOCK_SIZE = len(MAGIC) + BODY.size + 4
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def valid_path(path: str) -> bool:
    """Printable ASCII FAT-root path with nonempty, non-dot components."""
    return (bool(path) and all(0x20 <= ord(c) < 0x7f and c not in ";\\" for c in path)
            and all(component not in ("", ".", "..") for component in path.split("/")))


def fill(image: bytes, uuid: str, path: str, window_ms: int) -> bytes:
    if not UUID.fullmatch(uuid):
        raise ValueError("ESP PARTUUID must use canonical lowercase 8-4-4-4-12 hex")
    if not valid_path(path):
        raise ValueError("Stage 2 path must be relative to the ESP root")
    encoded = b";" + path.encode("ascii")
    if len(encoded) >= 192:
        raise ValueError("Stage 2 path is too long")
    if not 0 <= window_ms <= 99999:
        raise ValueError("Proxy window must be 0..99999 ms")
    if not image.endswith(b"STACKBOT"):
        raise ValueError("Stage 1 image does not end at STACKBOT")
    if image.count(MAGIC) != 1:
        raise ValueError("Stage 1 config magic must occur exactly once")

    offset = image.index(MAGIC)
    if offset + BLOCK_SIZE > len(image) - 8:
        raise ValueError("Stage 1 config block extends past image data")
    version = struct.unpack_from("<I", image, offset + len(MAGIC))[0]
    if version != 1:
        raise ValueError("Unsupported Stage 1 config version")

    body = BODY.pack(1, window_ms, uuid.encode("ascii").ljust(40, b"\0"),
                     encoded.ljust(192, b"\0"))
    block = MAGIC + body + struct.pack("<I", zlib.crc32(body))
    result = image[:offset] + block + image[offset + BLOCK_SIZE:]
    assert len(result) == len(image) and result.endswith(b"STACKBOT")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--esp-partuuid", required=True)
    parser.add_argument("--stage2-path", default="aurora/stage2.bin")
    parser.add_argument("--window-ms", type=int, default=15000)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("use a separate output path to retain the generic image")

    result = fill(args.input.read_bytes(), args.esp_partuuid, args.stage2_path, args.window_ms)
    args.output.write_bytes(result)
    print(f"{args.output}: {len(result)} bytes sha256 {hashlib.sha256(result).hexdigest()}")


if __name__ == "__main__":
    main()
