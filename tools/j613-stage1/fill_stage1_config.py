#!/usr/bin/env python3
"""Fill (or check) the in-image configuration of a J613 stage 1 for this Mac's ESP.

A J613 stage 1 (built with CHAINLOADING=1 J613_ESP_STAGE1=1) carries one
260-byte, CRC-checked configuration block. A factory image has an empty block
and stays proxy-only. Filling it names the EFI system partition (by PARTUUID)
and the file on it that stage 1 loads as stage 2:

  m1n1/boot.bin                  the Asahi/Aurora boot.bin (m1n1 + DTBs + U-Boot),
                                 what iconidentify/aurora-linux's installer maintains
  aurora/j613-25g83-g54/stage2.bin  a single-file m1n1 + DTB + kernel + initramfs

On Linux, run it on the Mac itself and it reads the PARTUUID of the mounted
ESP (default /boot/efi) and checks that the stage-2 file is there:

  fill_stage1_config.py m1n1-j613-stage1.bin m1n1-j613-stage1-filled.bin

Elsewhere, give --esp-partuuid. --check prints what an image contains, with
the size, MD5 and POSIX cksum that recoveryOS can verify (it has no shasum).

The output is never the input, is never overwritten, and has the same size as
the input. The block format is the one Aurora m1n1 (aurora-silicon/m1n1
aed999725) and the m1n1-aurora 1.6.1.aurora15 recipe (patch 0054) use.
"""

import argparse
import hashlib
import os
from pathlib import Path
import re
import struct
import subprocess
import sys
import zlib

MAGIC = b"AURORA-S1-CFG01\0"
BODY = struct.Struct("<II40s192s")
BLOCK_SIZE = len(MAGIC) + BODY.size + 4  # 260
TAIL = b"STACKBOT"
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")
TAG_VAR = re.compile(rb"chosen\.asahi,m1n1-stage1-version=([A-Za-z0-9._+-]+)\0")
J613_MARK = b"J613 Stage 1: window expired; loading ESP candidate"
DEFAULT_TARGET = "m1n1/boot.bin"
DEFAULT_WINDOW_MS = 15000


class ConfigError(ValueError):
    pass


def posix_cksum(data: bytes) -> int:
    """The CRC printed by POSIX cksum (CRC-32/CKSUM over the data and its length)."""
    crc = 0
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    length = len(data)
    while length:
        crc ^= (length & 0xFF) << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
        length >>= 8
    return ~crc & 0xFFFFFFFF


def valid_target(path: str) -> bool:
    """Printable ASCII path relative to the ESP root, with no empty, . or .. components."""
    return (bool(path) and not path.startswith("/")
            and all(0x20 <= ord(c) < 0x7F and c not in ";\\" for c in path)
            and all(part not in ("", ".", "..") for part in path.split("/")))


def locate(image: bytes) -> int:
    """Offset of the configuration block in a J613 stage-1 image."""
    if not image.endswith(TAIL):
        raise ConfigError("not a raw m1n1 image (it does not end at STACKBOT)")
    if image.count(MAGIC) != 1:
        raise ConfigError("not a J613 stage 1: the configuration block must occur exactly once")
    if J613_MARK not in image:
        raise ConfigError("not a J613 stage 1 build (CHAINLOADING=1 J613_ESP_STAGE1=1)")
    offset = image.index(MAGIC)
    if offset + BLOCK_SIZE > len(image) - len(TAIL):
        raise ConfigError("configuration block runs past the image data")
    if struct.unpack_from("<I", image, offset + len(MAGIC))[0] != 1:
        raise ConfigError("unsupported configuration block version")
    return offset


def stage1_tag(image: bytes) -> str:
    tags = TAG_VAR.findall(image)
    if len(tags) != 1:
        raise ConfigError("cannot find exactly one stage-1 version tag in the image")
    return tags[0].decode()


def decode(image: bytes) -> dict:
    offset = locate(image)
    version, window_ms, uuid, path = BODY.unpack_from(image, offset + len(MAGIC))
    (crc,) = struct.unpack_from("<I", image, offset + len(MAGIC) + BODY.size)
    body = image[offset + len(MAGIC):offset + len(MAGIC) + BODY.size]
    uuid_s = uuid.rstrip(b"\0").decode("ascii", "replace")
    path_s = path.rstrip(b"\0").decode("ascii", "replace")
    factory = not uuid_s and not path_s and crc == 0
    valid = (not factory and crc == zlib.crc32(body) and window_ms <= 99999
             and bool(UUID.fullmatch(uuid_s)) and valid_target(path_s.removeprefix(";"))
             and len(path.rstrip(b"\0")) < len(path))
    return dict(offset=offset, factory=factory, valid=valid, esp_partuuid=uuid_s,
                target=path_s.removeprefix(";"), window_ms=window_ms)


def fill(image: bytes, esp_partuuid: str, target: str, window_ms: int) -> bytes:
    esp_partuuid = esp_partuuid.lower()
    if not UUID.fullmatch(esp_partuuid):
        raise ConfigError("ESP PARTUUID must be 8-4-4-4-12 hex, e.g. 05f43e5f-01ac-4e4d-abcb-0c844b7e0abf")
    target = target.removeprefix(";")
    if not valid_target(target):
        raise ConfigError("the stage-2 target must be a plain path relative to the ESP root, e.g. m1n1/boot.bin")
    encoded = b";" + target.encode("ascii")
    if len(encoded) >= 192:
        raise ConfigError("the stage-2 target path is too long (190 characters at most)")
    if not 0 <= window_ms <= 99999:
        raise ConfigError("the USB proxy window must be 0..99999 ms")
    offset = locate(image)
    body = BODY.pack(1, window_ms, esp_partuuid.encode("ascii").ljust(40, b"\0"), encoded.ljust(192, b"\0"))
    block = MAGIC + body + struct.pack("<I", zlib.crc32(body))
    result = image[:offset] + block + image[offset + BLOCK_SIZE:]
    assert len(result) == len(image) and result.endswith(TAIL)
    assert decode(result)["valid"]
    return result


def linux_esp(mount: Path) -> tuple[str, str]:
    """(PARTUUID, filesystem type) of the partition mounted at mount, on Linux."""
    out = subprocess.run(["findmnt", "-no", "SOURCE,FSTYPE", "--mountpoint", str(mount)],
                         capture_output=True, text=True)
    if out.returncode or not out.stdout.split():
        raise ConfigError(f"{mount} is not a mount point; mount the ESP or pass --esp-partuuid")
    source, fstype = out.stdout.split()[:2]
    uuid = subprocess.run(["lsblk", "-dno", "PARTUUID", source], capture_output=True, text=True).stdout.strip()
    if not UUID.fullmatch(uuid.lower()):
        raise ConfigError(f"cannot read the PARTUUID of {source}; pass --esp-partuuid")
    return uuid.lower(), fstype


def describe(name: str, data: bytes) -> str:
    return (f"{name}: {len(data)} bytes\n  sha256 {hashlib.sha256(data).hexdigest()}\n"
            f"  md5    {hashlib.md5(data).hexdigest()}\n  cksum  {posix_cksum(data)} {len(data)}")


def check(path: Path) -> int:
    image = path.read_bytes()
    info = decode(image)
    print(describe(str(path), image))
    print(f"  stage-1 version tag: {stage1_tag(image)}")
    if info["factory"]:
        print("  configuration: factory (empty): this image is proxy-only")
    elif info["valid"]:
        print(f"  configuration: valid; ESP PARTUUID {info['esp_partuuid']}, stage 2 "
              f"'{info['target']}', USB proxy window {info['window_ms']} ms")
    else:
        print("  configuration: INVALID (bad CRC or field); stage 1 would stay proxy-only")
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("image", nargs="?", type=Path, help="factory J613 stage-1 image (input)")
    p.add_argument("output", nargs="?", type=Path, help="filled image to write (must not exist)")
    p.add_argument("--input", dest="input_flag", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--output", dest="output_flag", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--esp-partuuid", "--esp-uuid", dest="esp_partuuid",
                   help="PARTUUID of the ESP (default on Linux: read from --esp-mount)")
    p.add_argument("--esp-mount", type=Path, default=Path("/boot/efi"),
                   help="where the ESP is mounted, on Linux (default /boot/efi)")
    p.add_argument("--target", "--stage2-path", dest="target", default=DEFAULT_TARGET,
                   help=f"stage-2 file, relative to the ESP root (default {DEFAULT_TARGET})")
    p.add_argument("--window-ms", type=int, default=DEFAULT_WINDOW_MS,
                   help=f"USB proxy window before loading stage 2 (default {DEFAULT_WINDOW_MS})")
    p.add_argument("--allow-missing-target", action="store_true",
                   help="fill even though the stage-2 file is not on the mounted ESP yet")
    p.add_argument("--check", type=Path, metavar="IMAGE", help="print what IMAGE contains and exit")
    a = p.parse_args()
    try:
        if a.check:
            return check(a.check)
        src, dst = a.input_flag or a.image, a.output_flag or a.output
        if not src or not dst:
            p.error("give the factory image and the output path (or --check IMAGE)")
        if dst.exists() or src.resolve() == dst.resolve():
            p.error(f"{dst} exists; the output must be a new file")
        image = src.read_bytes()
        if not decode(image)["factory"]:
            print(f"note: {src} is already filled; its configuration will be replaced", file=sys.stderr)
        uuid = a.esp_partuuid
        if uuid is None:
            if sys.platform != "linux":
                p.error("--esp-partuuid is required off Linux")
            uuid, fstype = linux_esp(a.esp_mount)
            if fstype != "vfat":
                raise ConfigError(f"{a.esp_mount} is {fstype}, not the FAT EFI system partition")
            print(f"ESP at {a.esp_mount}: PARTUUID {uuid}")
            stage2 = a.esp_mount / a.target
            if not os.access(a.esp_mount, os.R_OK | os.X_OK):
                print(f"warning: cannot read {a.esp_mount} as this user; not checking {stage2}", file=sys.stderr)
            elif not stage2.is_file() and not a.allow_missing_target:
                raise ConfigError(f"{stage2} does not exist. With no stage 2 this stage 1 stops in the "
                                  "USB proxy (a black screen). Put it there first, or --allow-missing-target")
        result = fill(image, uuid, a.target, a.window_ms)
        with dst.open("xb") as out:
            out.write(result)
        print(describe(str(dst), result))
        print(f"  stage-1 version tag: {stage1_tag(result)}")
        print(f"  configuration: ESP PARTUUID {uuid}, stage 2 '{a.target}', window {a.window_ms} ms")
        return 0
    except (ConfigError, OSError, UnicodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
