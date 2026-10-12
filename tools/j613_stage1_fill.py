#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fill in, or check, the configuration block of an m1n1 ESP stage 1 image.

The ESP stage 1 (m1n1 built with CHAINLOADING=1 and J613_STAGE1=1 or
J615_STAGE1=1) loads one stage 2 file from the internal EFI system partition
(ESP). Which partition and which file is stored in a 260 byte block inside the
image, described in src/stage1_cfg.h. A freshly built ("factory") image has an
empty block and only offers the USB proxy.

Each image is built for one board, which this tool reports:
    j613  MacBook Air 13" M3 (J613)
    j615  MacBook Air 15-inch M3 (J615), experimental and untested on hardware

    j613_stage1_fill.py IMAGE OUTPUT [--esp-partuuid UUID] [--target PATH]
                        [--window-ms MS] [--esp-mount DIR] [--board BOARD]
                        [--allow-missing-target]
    j613_stage1_fill.py --check IMAGE

Filling writes a new file and never modifies IMAGE. With --board, an image
built for another board is refused. On Linux, when --esp-partuuid is not given,
the ESP PARTUUID is read from the mount at --esp-mount and the image must be for
the Mac the tool runs on; elsewhere --esp-partuuid is required.

Only the Python standard library is used.
"""

import argparse
import hashlib
import os
import re
import struct
import subprocess
import sys
import zlib

MAGIC = b"AURORA-S1-CFG01\0"
BLOCK_SIZE = 260
CFG_VERSION = 1
WINDOW_MAX = 99999

OFF_VERSION = 16
OFF_WINDOW = 20
OFF_UUID = 24
UUID_FIELD = 40
OFF_PATH = 64
PATH_FIELD = 192
OFF_CRC = 256

END_MARKER = b"STACKBOT"
TAG_PREFIX = b"chosen.asahi,m1n1-stage1-version="

# Boards: the identity string the image carries, and the model it names
BOARDS = {
    "j613": {"ident": b"##j613_stage1##", "model": 'MacBook Air 13" M3 (J613)',
             "experimental": False},
    "j615": {"ident": b"##j615_stage1##", "model": "MacBook Air 15-inch M3 (J615)",
             "experimental": True},
}
DT_COMPATIBLE = "/proc/device-tree/compatible"

DEFAULT_MOUNT = "/boot/efi"
DEFAULT_TARGET = "m1n1/boot.bin"

TAG_RE = re.compile(rb"[A-Za-z0-9._+-]+")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class FillError(Exception):
    pass


# Configuration block


def block_crc(block):
    """CRC-32 (zlib) over version .. stage2_path."""
    return zlib.crc32(bytes(block[OFF_VERSION:OFF_CRC])) & 0xFFFFFFFF


def path_problem(path):
    """Return why a stage 2 path (without the ';' prefix) is unusable, or None."""
    if not path:
        return "empty stage 2 path"
    if path.startswith("/"):
        return "stage 2 path is absolute"
    for ch in path:
        if not 0x20 <= ord(ch) <= 0x7E or ch in ";\\":
            return "bad character in stage 2 path"
    for comp in path.split("/"):
        if comp == "":
            return "empty component in stage 2 path"
        if comp in (".", ".."):
            return "dot component in stage 2 path"
    return None


def pack_block(uuid, stored_path, window_ms, version=CFG_VERSION, crc=None):
    """Build a block from raw field values, without validating them.

    stored_path is the exact bytes for the path field (including any ';'
    prefix); it is NUL padded to the field size. crc defaults to the correct
    value.
    """
    if isinstance(uuid, str):
        uuid = uuid.encode("ascii")
    if isinstance(stored_path, str):
        stored_path = stored_path.encode("ascii")
    if len(uuid) > UUID_FIELD or len(stored_path) > PATH_FIELD:
        raise FillError("field too long")

    block = bytearray(BLOCK_SIZE)
    block[0:len(MAGIC)] = MAGIC
    struct.pack_into("<II", block, OFF_VERSION, version, window_ms)
    block[OFF_UUID:OFF_UUID + len(uuid)] = uuid
    block[OFF_PATH:OFF_PATH + len(stored_path)] = stored_path
    if crc is None:
        crc = block_crc(block)
    struct.pack_into("<I", block, OFF_CRC, crc)
    return bytes(block)


def encode_block(uuid, target, window_ms):
    """Validate the user's settings and build a block for them."""
    uuid = uuid.strip().lower()
    if not UUID_RE.fullmatch(uuid):
        raise FillError(f"not a partition UUID (8-4-4-4-12 hex digits): {uuid!r}")
    problem = path_problem(target)
    if problem:
        raise FillError(f"{problem}: {target!r}")
    stored = ";" + target
    if len(stored) >= PATH_FIELD:
        raise FillError(f"stage 2 path is too long ({len(target)} characters, at most "
                        f"{PATH_FIELD - 2})")
    if not 0 <= window_ms <= WINDOW_MAX:
        raise FillError(f"--window-ms must be 0..{WINDOW_MAX}")
    return pack_block(uuid, stored, window_ms)


def decode_block(block):
    """Classify a block the same way the stage 1 does.

    Returns a dict with "state" ("factory", "valid" or "invalid") and, for a
    valid block, "uuid", "target" and "window_ms", or for an invalid one "why".
    """
    block = bytes(block)
    version, window = struct.unpack_from("<II", block, OFF_VERSION)
    uuid_raw = block[OFF_UUID:OFF_UUID + UUID_FIELD]
    path_raw = block[OFF_PATH:OFF_PATH + PATH_FIELD]
    (crc,) = struct.unpack_from("<I", block, OFF_CRC)

    if uuid_raw[0] == 0 and path_raw[0] == 0 and crc == 0:
        return {"state": "factory"}

    def invalid(why):
        return {"state": "invalid", "why": why}

    if crc != block_crc(block):
        return invalid("checksum mismatch")
    if version != CFG_VERSION:
        return invalid("unknown version")
    if window > WINDOW_MAX:
        return invalid("USB window too long")
    uuid_text = uuid_raw[:36].decode("latin-1")
    if not UUID_RE.fullmatch(uuid_text) or any(uuid_raw[36:]):
        return invalid("malformed partition UUID")
    end = path_raw.find(b"\0")
    if end < 0:
        return invalid("stage 2 path not terminated")
    if any(path_raw[end:]):
        return invalid("data after the stage 2 path")
    path = path_raw[:end].decode("latin-1")
    if path.startswith(";"):
        path = path[1:]
    problem = path_problem(path)
    if problem:
        return invalid(problem)
    return {"state": "valid", "uuid": uuid_text, "target": path, "window_ms": window}


# Image


def board_of(image):
    """Return the board ("j613", "j615") a stage 1 image was built for."""
    found = [(name, image.count(b["ident"])) for name, b in BOARDS.items()]
    found = [(name, n) for name, n in found if n]
    if not found:
        idents = " or ".join(b["ident"].decode() for b in BOARDS.values())
        raise FillError(f"no {idents} identity string; this file is some other m1n1")
    if len(found) > 1 or found[0][1] > 1:
        raise FillError("the stage 1 identity string is not unique; refusing an image that "
                        "claims more than one board")
    return found[0][0]


def board_text(board):
    info = BOARDS[board]
    text = info["model"]
    if info["experimental"]:
        text += ", experimental: not yet tested on hardware"
    return text


def running_board():
    """The stage 1 board this Linux system runs on, or None if it is not one."""
    try:
        with open(DT_COMPATIBLE, "rb") as f:
            compatible = f.read().split(b"\0")
    except OSError:
        return None
    for name in BOARDS:
        if b"apple," + name.encode() in compatible:
            return name
    return None


def locate(image):
    """Find the configuration block in a stage 1 image.

    Returns (offset, tag).
    """
    if not image.endswith(END_MARKER):
        raise FillError("the file does not finish with the 8-byte STACKBOT trailer; "
                        "pass the raw m1n1.bin output of the build")

    board_of(image)

    if image.count(TAG_PREFIX) != 1:
        raise FillError("the stage1-version chosen string is absent or appears twice; "
                        "this file is some other m1n1")
    start = image.index(TAG_PREFIX) + len(TAG_PREFIX)
    end = image.find(b"\0", start)
    tag = image[start:end] if end >= 0 else b""
    if not TAG_RE.fullmatch(tag):
        raise FillError("stage 1 version tag is malformed")

    count = image.count(MAGIC)
    if count != 1:
        raise FillError(f"found {count} configuration blocks where exactly one belongs")
    off = image.index(MAGIC)
    if off % 4:
        raise FillError("configuration block is not 4-byte aligned")
    if off + BLOCK_SIZE > len(image) - len(END_MARKER):
        raise FillError("configuration block runs into the end marker")
    (version,) = struct.unpack_from("<I", image, off + OFF_VERSION)
    if version != CFG_VERSION:
        raise FillError(f"configuration block has layout version {version}; "
                        f"this tool only knows layout {CFG_VERSION}")

    return off, tag.decode("ascii")


_CKSUM_TABLE = []
for _i in range(256):
    _c = _i << 24
    for _ in range(8):
        _c = ((_c << 1) ^ 0x04C11DB7) if _c & 0x80000000 else (_c << 1)
    _CKSUM_TABLE.append(_c & 0xFFFFFFFF)


def posix_cksum(data):
    """CRC-32 as cksum(1) reports it: MSB-first, length appended, inverted.

    recoveryOS ships cksum and md5 but no shasum, so users can compare this one there.
    """
    crc = 0
    table = _CKSUM_TABLE
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[(crc >> 24) ^ b]
    n = len(data)
    while n:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[(crc >> 24) ^ (n & 0xFF)]
        n >>= 8
    return ~crc & 0xFFFFFFFF


def read_image(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise FillError(f"cannot read {path}: {e.strerror}")


# ESP detection (Linux)


def _run(cmd):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
    except OSError as e:
        raise FillError(f"cannot run {cmd[0]}: {e.strerror}")
    if res.returncode != 0:
        raise FillError(f"{' '.join(cmd)} failed: {res.stderr.strip()}")
    return res.stdout


def esp_partuuid_from_mount(mount):
    # Touch the mount point first, so that an automount is triggered
    try:
        os.listdir(mount)
    except OSError:
        pass  # findmnt below reports whether it is mounted

    out = _run(["findmnt", "-n", "-r", "-o", "FSTYPE,SOURCE", "--mountpoint", mount])
    sources = []
    for line in out.splitlines():
        fields = line.split(None, 1)
        if len(fields) == 2 and fields[0] == "vfat":
            sources.append(fields[1].encode().decode("unicode_escape"))
    if not sources:
        raise FillError(f"{mount} is not a vfat mount; is the ESP mounted there?")

    uuid = _run(["lsblk", "-dno", "PARTUUID", sources[-1]]).strip().lower()
    if not UUID_RE.fullmatch(uuid):
        raise FillError(f"could not read the PARTUUID of {sources[-1]} (got {uuid!r})")
    return uuid


# Commands


def print_check(path, out=None):
    out = out or sys.stdout
    image = read_image(path)
    off, tag = locate(image)
    info = decode_block(image[off:off + BLOCK_SIZE])

    print(f"image: {path}", file=out)
    print(f"bytes: {len(image)}", file=out)
    print(f"sha256: {hashlib.sha256(image).hexdigest()}", file=out)
    print(f"md5: {hashlib.md5(image).hexdigest()}", file=out)
    print(f"cksum: {posix_cksum(image)} {len(image)}", file=out)
    board = board_of(image)
    print(f"tag: {tag}", file=out)
    print(f"board: {board} ({board_text(board)})", file=out)

    if info["state"] == "factory":
        print("configuration: factory (not filled in; boots to the USB proxy only)", file=out)
        return 0
    if info["state"] == "valid":
        print("configuration: valid", file=out)
        print(f"  esp_partuuid: {info['uuid']}", file=out)
        print(f"  target: {info['target']}", file=out)
        print(f"  window_ms: {info['window_ms']}", file=out)
        return 0
    print(f"configuration: INVALID ({info['why']})", file=out)
    return 1


def fill(args):
    image = read_image(args.image)
    off, tag = locate(image)
    board = board_of(image)
    old = decode_block(image[off:off + BLOCK_SIZE])

    if args.board and args.board != board:
        raise FillError(f"{args.image} is a stage 1 for the {BOARDS[board]['model']}, not the "
                        f"{BOARDS[args.board]['model']}; use the {args.board.upper()} image")

    problem = path_problem(args.target)
    if problem:
        raise FillError(f"{problem}: {args.target!r}")
    if not 0 <= args.window_ms <= WINDOW_MAX:
        raise FillError(f"--window-ms must be 0..{WINDOW_MAX}")

    if args.esp_partuuid:
        uuid = args.esp_partuuid
    elif sys.platform.startswith("linux"):
        here = running_board()
        if here and here != board:
            raise FillError(f"this Mac is a {BOARDS[here]['model']}, but {args.image} is a "
                            f"stage 1 for the {BOARDS[board]['model']}; use the "
                            f"{here.upper()} image")
        mount = args.esp_mount
        uuid = esp_partuuid_from_mount(mount)
        try:
            os.listdir(mount)
            listable = True
        except PermissionError:
            listable = False
        if not listable:
            print(f"warning: no permission to list {mount}, so whether {args.target} is "
                  "there was not verified", file=sys.stderr)
        elif not os.path.isfile(os.path.join(mount, args.target)):
            if not args.allow_missing_target:
                raise FillError(
                    f"{os.path.join(mount, args.target)} does not exist. Without it the stage 1 "
                    "shows its failure screen and falls back to the USB proxy. Install the "
                    "stage 2 first, or pass --allow-missing-target.")
            print(f"warning: {os.path.join(mount, args.target)} does not exist (yet)",
                  file=sys.stderr)
    else:
        raise FillError("--esp-partuuid is required when not running on Linux")

    block = encode_block(uuid, args.target, args.window_ms)
    new = image[:off] + block + image[off + BLOCK_SIZE:]

    check = decode_block(new[off:off + BLOCK_SIZE])
    if (len(new) != len(image) or not new.endswith(END_MARKER) or locate(new) != (off, tag)
            or check["state"] != "valid"):
        raise FillError("internal error: filled image does not verify")

    if old["state"] != "factory":
        print(f"note: {args.image} was already filled in ({old['state']}); "
              "replacing its configuration", file=sys.stderr)

    try:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        raise FillError(f"{args.output} already exists; choose a new output file")
    except OSError as e:
        raise FillError(f"cannot create {args.output}: {e.strerror}")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(new)
    except OSError as e:
        os.unlink(args.output)
        raise FillError(f"cannot write {args.output}: {e.strerror}")

    print(f"wrote {args.output} (stage 1 {tag})")
    print(f"  board: {board} ({board_text(board)})")
    print(f"  esp_partuuid: {check['uuid']}")
    print(f"  target: {check['target']}")
    print(f"  window_ms: {check['window_ms']}")
    return 0


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        print(f"{self.prog}: error: {message}", file=sys.stderr)
        sys.exit(1)


def main(argv=None):
    ap = _Parser(description="Fill in or check an m1n1 ESP stage 1 image (J613 or J615).")
    ap.add_argument("images", nargs="*", metavar="IMAGE OUTPUT",
                    help="factory (or filled) stage 1 image, and the new file to write")
    ap.add_argument("--check", metavar="IMAGE", help="print hashes and configuration of IMAGE")
    ap.add_argument("--esp-partuuid", "--esp-uuid", dest="esp_partuuid", metavar="UUID",
                    help="PARTUUID of the internal EFI system partition")
    ap.add_argument("--esp-mount", default=DEFAULT_MOUNT, metavar="DIR",
                    help=f"where the ESP is mounted (Linux, default {DEFAULT_MOUNT})")
    ap.add_argument("--target", "--stage2-path", dest="target", default=DEFAULT_TARGET,
                    metavar="PATH", help=f"stage 2 file on the ESP (default {DEFAULT_TARGET})")
    ap.add_argument("--window-ms", type=int, default=0, metavar="MS",
                    help=f"USB proxy window before loading stage 2, 0..{WINDOW_MAX} (default 0)")
    ap.add_argument("--board", type=str.lower, choices=sorted(BOARDS),
                    help="the Mac the image is for; refuse an image built for another board")
    ap.add_argument("--allow-missing-target", action="store_true",
                    help="write the image even though TARGET is absent from the mounted ESP")
    args = ap.parse_args(argv)

    images = list(args.images)
    if len(images) == 3 and images[0] == "fill":
        images = images[1:]

    try:
        if args.check:
            if images:
                ap.error("--check takes no other files")
            return print_check(args.check)
        if len(images) != 2:
            ap.error("expected IMAGE OUTPUT (or --check IMAGE)")
        args.image, args.output = images
        return fill(args)
    except FillError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
