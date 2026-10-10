"""Capture the native M6 boot console without clearing it or replacing its text.

Boot the matching image first. USB-C initialization now runs in m1n1 itself.
Use --initialize only to retry after connecting a display, or --reinit to
qualify framebuffer history replay. Neither option draws a test banner.
"""
import argparse
import fcntl
import hashlib
import json
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from elftools.elf.elffile import ELFFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "proxyclient"))
from m1n1.proxy import UartInterface, M1N1Proxy
from m1n1.proxyutils import ProxyUtils

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("image", type=Path)
ap.add_argument("sha256")
ap.add_argument("--device", required=True)
ap.add_argument("--output", type=Path, required=True)
ap.add_argument("--nm", default="llvm-nm")
ap.add_argument("--initialize", action="store_true")
ap.add_argument("--reinit", action="store_true")
args = ap.parse_args()
args.image = args.image.resolve()
raw = args.image.read_bytes()
assert hashlib.sha256(raw).hexdigest() == args.sha256
args.output.mkdir(parents=True, exist_ok=False)
lock = open(Path(tempfile.gettempdir()) / ("m1n1-" + Path(args.device).name + ".lock"), "a")
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

elf = args.image.parent / "m1n1-raw.elf"
symbols = {}
for line in subprocess.check_output([args.nm, str(elf)], text=True).splitlines():
    parts = line.split()
    if len(parts) == 3:
        symbols[parts[2]] = int(parts[0], 16)
with elf.open("rb") as stream:
    parsed = ELFFile(stream)
    assert parsed.header["e_entry"] == 0x800
    entries = parsed.get_section_by_name(".symtab").get_symbol_by_name("con_buf")
    assert len(entries) == 1
    console_size = entries[0]["st_size"]
    assert 0 < console_size <= 1024 * 1024

def png(width, height, stride, bgra):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    rows = bytearray()
    for y in range(height):
        row = bgra[y * stride * 4:(y * stride + width) * 4]
        rows.append(0)
        rgb = bytearray(width * 3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::4], row[1::4], row[0::4]
        rows.extend(rgb)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))

f = UartInterface(args.device)
f.dev.timeout = 30
try:
    f.nop()
    p = M1N1Proxy(f)
    base = p.get_base()
    assert p.read32(base + symbols["chip_id"]) == 0x8152
    assert p.read32(base + symbols["board_id"]) == 0x24
    assert not any(p.smp_is_alive(i) for i in range(12) if i != 6), "Fresh boot required"
    for name in ("display_init", "fb_reinit", "fb_set_active", "iodev_console_rewind"):
        off = symbols[name]
        assert f.readmem(base + off, 64) == raw[off:off + 64], name
    u = ProxyUtils(p)
    assert u.adt.model == "Mac18,5"
    assert u.adt["/chosen"].chip_id == 0x8152
    assert u.adt["/chosen"].board_id == 0x24

    if args.initialize:
        ret = p.call(base + symbols["display_init"])
        assert ret in (0, 1), f"Display initialization failed: {ret:#x}"
    before_wp = p.read64(base + symbols["con_wp"])
    if args.reinit:
        p.call(base + symbols["fb_reinit"])

    _, hwptr, stride, depth, width, height, size = struct.unpack(
        "<QQIIIII", f.readmem(base + symbols["fb"], 36))
    assert depth == 32 and 0 < width <= stride <= 8192 and 0 < height <= 8192
    assert (width, height) != (640, 1136), "Only firmware's dummy framebuffer is active"
    assert size == stride * height * 4 and size <= 128 * 1024 * 1024
    wp = p.read64(base + symbols["con_wp"])
    rp = p.read64(base + symbols["con_rp"] + 2 * 8)
    assert rp == wp, "Framebuffer console did not consume retained history"
    ring = f.readmem(base + symbols["con_buf"], console_size)
    start = max(0, wp - console_size)
    history = bytes(ring[i % console_size] for i in range(start, wp))
    assert b"Initialization complete. Running proxy..." in history
    assert b"USB-C DisplayPort framebuffer test" not in history
    (args.output / "boot-console.txt").write_bytes(history)
    pixels = b"".join(f.readmem(hwptr + i, min(0x10000, size - i))
                      for i in range(0, size, 0x10000))
    (args.output / "framebuffer.bgra").write_bytes(pixels)
    (args.output / "framebuffer.png").write_bytes(png(width, height, stride, pixels))
    state = dict(base=hex(base), image_sha256=args.sha256,
                 width=width, height=height, depth=depth, stride=stride,
                 framebuffer=hex(hwptr), framebuffer_sha256=hashlib.sha256(pixels).hexdigest(),
                 console_before_wp=before_wp, console_wp=wp, console_rp=rp,
                 reinitialized=args.reinit, native_auto_boot=not args.initialize,
                 host_heap_base=u.heap_base, host_heap_top=u.heap_top,
                 visible_confirmed=False)
    (args.output / "display-state.json").write_text(json.dumps(state, indent=2) + "\n")
    p.nop()
    print(f"NATIVE_BOOT_CONSOLE_PASS {width}x{height}x{depth} history={len(history)}")
    print("PROXY_VERIFIED")
    print("DISPLAY_LEFT_ACTIVE True")
finally:
    f.dev.close()
