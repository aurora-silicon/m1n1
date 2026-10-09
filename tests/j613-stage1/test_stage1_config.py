#!/usr/bin/env python3
"""Host test: the filler's block is what src/stage1_config.c accepts, and nothing else is.

Builds src/stage1_config.c (as J613_ESP_STAGE1) with host stubs, feeds it blocks
written by tools/j613-stage1/fill_stage1_config.py, and checks the C parser's
result. Adapted from tests/run-stage1-config.py and tests/stage1_config_host.c of
the m1n1-aurora 1.6.1.aurora15 recipe (patch 0054). Needs a host C compiler.
"""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zlib

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("fill", ROOT / "tools/j613-stage1/fill_stage1_config.py")
fill = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fill)

UUID = "05f43e5f-01ac-4e4d-abcb-0c844b7e0abf"

HARNESS = r"""
#include <assert.h>
#include <stdio.h>
#include "stage1_config.c"
static void reset(void) { config_valid = -1; }
int main(int argc, char **argv)
{
    assert(argc == 3);
    assert(!stage1_config_target()); /* factory block: proxy only */
    FILE *f = fopen(argv[1], "rb");
    assert(f);
    assert(fread(&config, 1, sizeof(config), f) == sizeof(config));
    fclose(f);
    reset();
    const char *t = stage1_config_target();
    printf("%s|%u\n", t ? t : "(none)", stage1_config_window_ms());
    return t && !strcmp(t, argv[2]) ? 0 : 1;
}
"""


def image(tag=b"v1.6.1-j613s1-gtest"):
    """A synthetic factory stage-1 image with the markers the filler requires."""
    block = fill.MAGIC + fill.BODY.pack(1, 0, b"", b"") + b"\0\0\0\0"
    return (b"\0" * 64 + block + b"\0" * 12 + fill.J613_MARK + b"\0"
            + b"chosen.asahi,m1n1-stage1-version=" + tag + b"\0" + b"\0" * 64 + fill.TAIL)


def block_of(img):
    off = fill.locate(img)
    return img[off:off + fill.BLOCK_SIZE]


def main():
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        print("SKIP: no host C compiler")
        return 0
    factory = image()
    assert fill.decode(factory)["factory"] and fill.stage1_tag(factory) == "v1.6.1-j613s1-gtest"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "src/tinf").mkdir(parents=True)
        (tmp / "build").mkdir()
        for name in ("stage1_config.c", "stage1_config.h"):
            src = (ROOT / "src" / name).read_bytes()
            if sys.platform == "darwin":  # Mach-O sections need "segment,section"; ELF layout is not under test
                src = src.replace(b'section(".data.stage1_config"), ', b"")
            (tmp / "src" / name).write_bytes(src)
        for name in ("crc32.c", "tinf.h"):
            (tmp / "src/tinf" / name).write_bytes((ROOT / "src/tinf" / name).read_bytes())
        (tmp / "build/build_cfg.h").write_text("#define J613_ESP_STAGE1\n")
        (tmp / "src/types.h").write_text("#include <stdbool.h>\n#include <stdint.h>\ntypedef uint32_t u32;\n")
        (tmp / "src/assert.h").write_text("#pragma once\n#include_next <assert.h>\n#define static_assert _Static_assert\n")
        (tmp / "src/string.h").write_text("#include_next <string.h>\n")
        (tmp / "src/utils.h").write_text("#include <stdio.h>\n")
        (tmp / "harness.c").write_text(HARNESS)
        subprocess.run([cc, "-D_GNU_SOURCE", "-std=gnu11", "-Wall", "-Werror", "-I" + str(tmp / "src"),
                        str(tmp / "harness.c"), str(tmp / "src/tinf/crc32.c"), "-o", str(tmp / "t")], check=True)

        def c_accepts(block, expect):
            (tmp / "cfg").write_bytes(block)
            r = subprocess.run([str(tmp / "t"), str(tmp / "cfg"), expect], capture_output=True, text=True)
            return r.returncode == 0, r.stdout.strip()

        for target in ("m1n1/boot.bin", "aurora/j613-25g83-g54/stage2.bin", "aurora/chris-25g83-test/stage2.bin"):
            filled = fill.fill(factory, UUID, target, 15000)
            assert len(filled) == len(factory) and fill.decode(filled)["valid"]
            ok, out = c_accepts(block_of(filled), f"{UUID};{target}")
            assert ok and out == f"{UUID};{target}|15000", out

        zero = fill.fill(factory, UUID, "m1n1/boot.bin", 0)  # user default: no USB window
        assert fill.DEFAULT_WINDOW_MS == 0
        ok, out = c_accepts(block_of(zero), f"{UUID};m1n1/boot.bin")
        assert ok and out == f"{UUID};m1n1/boot.bin|0", out

        # Chris's recipe filler writes the same block for the same inputs.
        good = block_of(fill.fill(factory, UUID, "m1n1/boot.bin", 15000))
        body = good[16:-4]
        assert good[-4:] == zlib.crc32(body).to_bytes(4, "little")

        bad = bytearray(good); bad[-1] ^= 1
        assert not c_accepts(bytes(bad), "")[0], "CRC tamper must fail"
        for target in ("../boot.bin", "/boot.bin", "x//y", "x;y", "x\\y", "./x", ""):
            try:
                fill.fill(factory, UUID, target, 15000)
            except fill.ConfigError:
                pass
            else:
                raise AssertionError(f"filler accepted {target!r}")
        for uuid in ("05f43e5f01ac4e4dabcb0c844b7e0abf", "zzzzzzzz-01ac-4e4d-abcb-0c844b7e0abf"):
            try:
                fill.fill(factory, uuid, "m1n1/boot.bin", 15000)
            except fill.ConfigError:
                pass
            else:
                raise AssertionError(f"filler accepted {uuid!r}")
        assert fill.fill(factory, UUID.upper(), "m1n1/boot.bin", 15000) == fill.fill(factory, UUID, "m1n1/boot.bin", 15000)
        for window in (-1, 100000):
            try:
                fill.fill(factory, UUID, "m1n1/boot.bin", window)
            except fill.ConfigError:
                pass
            else:
                raise AssertionError(window)
        for broken in (factory[:-1], factory + b"\0", factory.replace(fill.J613_MARK, b"x" * len(fill.J613_MARK))):
            try:
                fill.locate(broken)
            except fill.ConfigError:
                pass
            else:
                raise AssertionError("locate accepted a non-stage-1 image")

    cksum = shutil.which("cksum")
    if cksum:
        sample = image() * 3
        r = subprocess.run([cksum], input=sample, capture_output=True, check=True)
        assert int(r.stdout.split()[0]) == fill.posix_cksum(sample)
    print("PASS: J613 stage-1 config: filler <-> stage1_config.c, CRC, path/UUID/window validation, cksum")
    return 0


if __name__ == "__main__":
    sys.exit(main())
