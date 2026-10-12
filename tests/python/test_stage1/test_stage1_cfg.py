# SPDX-License-Identifier: MIT
"""Host tests for the ESP stage 1 configuration block and its filler.

The C parser (src/stage1_cfg.c, with the real src/tinf/crc32.c) is built for
the host and fed blocks made by tools/j613_stage1_fill.py, and the filler's own
decoder must agree with it on every case.

Run with "python3 -m pytest tests/python/test_stage1" or
"python3 -m unittest discover -s tests/python/test_stage1". Set CC to pick the
host compiler. If STAGE1_IMAGE (J613) or STAGE1_J615_IMAGE names a real factory
build, the filler is also run against it.
"""

import contextlib
import hashlib
import importlib.util
import io
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
SRC = os.path.join(ROOT, "src")

_spec = importlib.util.spec_from_file_location(
    "j613_stage1_fill", os.path.join(ROOT, "tools", "j613_stage1_fill.py"))
fill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fill)

UUID = "0f3c9a2e-61b4-4c8d-9e7a-5b2d81c4f6a0"
TAG = "v1.6.1-m3air.stage1"

_harness = None
_tmp = tempfile.TemporaryDirectory(prefix="stage1-test-")


def harness():
    global _harness
    if _harness is None:
        cc = os.environ.get("CC", "cc")
        exe = os.path.join(_tmp.name, "cfg_harness")
        subprocess.run(
            [cc, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-I", SRC, "-o", exe,
             os.path.join(SRC, "stage1_cfg.c"), os.path.join(SRC, "tinf", "crc32.c"),
             os.path.join(HERE, "cfg_harness.c")],
            check=True)
        _harness = exe
    return _harness


def c_parse(block):
    assert len(block) == fill.BLOCK_SIZE
    res = subprocess.run([harness()], input=block, capture_output=True, check=True)
    return res.stdout.decode().strip()


def py_parse(block):
    info = fill.decode_block(block)
    if info["state"] == "factory":
        return "factory"
    if info["state"] == "valid":
        return f"valid {info['window_ms']} {info['uuid']};{info['target']}"
    return f"invalid {info['why']}"


def factory_block():
    block = bytearray(fill.BLOCK_SIZE)
    block[:16] = fill.MAGIC
    struct.pack_into("<I", block, fill.OFF_VERSION, 1)
    return bytes(block)


def fake_image(block=None, tag=TAG, ident=True, end=True, extra=b"", board="j613"):
    """A stand-in for a raw factory build: code, rodata strings, data, stack."""
    if block is None:
        block = factory_block()
    img = bytearray(b"\x1f\x20\x03\xd5" * 0x400)
    if ident:
        img += fill.BOARDS[board]["ident"] + board.upper().encode() + b"\0"
    img += fill.TAG_PREFIX + tag.encode() + b"\0"
    img += b"##m1n1_ver##" + tag.encode() + b"\0"
    img += extra
    img += bytes(-len(img) % 0x4000)
    img += block
    img += bytes(0x8000)
    img += b"STACKBOT" if end else b"\0" * 8
    return bytes(img)


class ParserTest(unittest.TestCase):
    """Spec acceptance cases, run through the C parser and the filler's decoder."""

    def check(self, block, expect):
        c = c_parse(block)
        self.assertEqual(c, py_parse(block))
        if expect.endswith(" "):
            self.assertTrue(c.startswith(expect), c)
        else:
            self.assertEqual(c, expect)

    def test_factory_is_proxy_only(self):
        self.check(factory_block(), "factory")

    def test_round_trip(self):
        block = fill.encode_block(UUID, "m1n1/boot.bin", 0)
        self.check(block, f"valid 0 {UUID};m1n1/boot.bin")
        block = fill.encode_block(UUID.upper(), "EFI/aurora/stage2.bin", 15000)
        self.check(block, f"valid 15000 {UUID};EFI/aurora/stage2.bin")
        self.check(fill.encode_block(UUID, "a", 99999), f"valid 99999 {UUID};a")

    def test_prefixed_and_bare_paths(self):
        self.assertEqual(fill.encode_block(UUID, "m1n1/boot.bin", 0)[64:79], b";m1n1/boot.bin\0")
        self.check(fill.pack_block(UUID, ";m1n1/boot.bin", 7), f"valid 7 {UUID};m1n1/boot.bin")
        self.check(fill.pack_block(UUID, "m1n1/boot.bin", 7), f"valid 7 {UUID};m1n1/boot.bin")

    def test_longest_paths(self):
        p190 = "d/" + "x" * 188
        self.check(fill.pack_block(UUID, ";" + p190, 0), f"valid 0 {UUID};{p190}")
        p191 = "x" * 191
        self.check(fill.pack_block(UUID, p191, 0), f"valid 0 {UUID};{p191}")
        self.assertEqual(py_parse(fill.encode_block(UUID, p190, 0)), f"valid 0 {UUID};{p190}")
        with self.assertRaises(fill.FillError):
            fill.encode_block(UUID, "x" * 191, 0)

    def test_rejects(self):
        good = fill.encode_block(UUID, "m1n1/boot.bin", 0)
        bad_crc = bytearray(good)
        bad_crc[fill.OFF_CRC] ^= 1
        bit_flip = bytearray(good)
        bit_flip[fill.OFF_PATH + 3] ^= 0x20
        trailing = bytearray(fill.pack_block(UUID, ";m1n1/boot.bin", 0))
        trailing[fill.OFF_PATH + 100] = 0x41
        struct.pack_into("<I", trailing, fill.OFF_CRC, fill.block_crc(trailing))
        uuid_pad = bytearray(fill.pack_block(UUID, ";a", 0))
        uuid_pad[fill.OFF_UUID + 38] = 0x30
        struct.pack_into("<I", uuid_pad, fill.OFF_CRC, fill.block_crc(uuid_pad))

        cases = {
            "bad crc": (bytes(bad_crc), "checksum mismatch"),
            "corrupt path": (bytes(bit_flip), "checksum mismatch"),
            "zero crc": (fill.pack_block(UUID, ";a", 0, crc=0), "checksum mismatch"),
            "uppercase uuid": (fill.pack_block(UUID.upper(), ";a", 0), "malformed partition UUID"),
            "short uuid": (fill.pack_block(UUID[:-1], ";a", 0), "malformed partition UUID"),
            "uuid dash": (fill.pack_block(UUID.replace("-", "x", 1), ";a", 0),
                          "malformed partition UUID"),
            "uuid padding": (bytes(uuid_pad), "malformed partition UUID"),
            "empty uuid": (fill.pack_block("", ";a", 0), "malformed partition UUID"),
            "dotdot": (fill.pack_block(UUID, ";m1n1/../boot.bin", 0),
                       "dot component in stage 2 path"),
            "dot": (fill.pack_block(UUID, ";./boot.bin", 0), "dot component in stage 2 path"),
            "absolute": (fill.pack_block(UUID, ";/m1n1/boot.bin", 0), "stage 2 path is absolute"),
            "absolute bare": (fill.pack_block(UUID, "/boot.bin", 0), "stage 2 path is absolute"),
            "backslash": (fill.pack_block(UUID, ";m1n1\\boot.bin", 0),
                          "bad character in stage 2 path"),
            "control": (fill.pack_block(UUID, ";m1n1/boot\x01.bin", 0),
                        "bad character in stage 2 path"),
            "tab": (fill.pack_block(UUID, ";m1n1/boot\t.bin", 0), "bad character in stage 2 path"),
            "del": (fill.pack_block(UUID, ";boot\x7f", 0), "bad character in stage 2 path"),
            "high": (fill.pack_block(UUID, b";boot\xc3\xa9", 0), "bad character in stage 2 path"),
            "second ;": (fill.pack_block(UUID, ";;boot.bin", 0), "bad character in stage 2 path"),
            "empty comp": (fill.pack_block(UUID, ";m1n1//boot.bin", 0),
                           "empty component in stage 2 path"),
            "trailing slash": (fill.pack_block(UUID, ";m1n1/", 0),
                               "empty component in stage 2 path"),
            "only ;": (fill.pack_block(UUID, ";", 0), "empty stage 2 path"),
            "no path": (fill.pack_block(UUID, "", 0), "empty stage 2 path"),
            "after nul": (bytes(trailing), "data after the stage 2 path"),
            "version 0": (fill.pack_block(UUID, ";a", 0, version=0), "unknown version"),
            "version 2": (fill.pack_block(UUID, ";a", 0, version=2), "unknown version"),
            "window 100000": (fill.pack_block(UUID, ";a", 100000), "USB window too long"),
            "window max": (fill.pack_block(UUID, ";a", 0xFFFFFFFF), "USB window too long"),
            "191 with ;": (fill.pack_block(UUID, ";" + "x" * 191, 0),
                           "stage 2 path not terminated"),
            "192 bare": (fill.pack_block(UUID, "x" * 192, 0), "stage 2 path not terminated"),
        }
        for name, (block, why) in cases.items():
            with self.subTest(name):
                self.check(block, "invalid " + why)

        # 192 characters with ';' do not even fit in the field
        with self.assertRaises(fill.FillError):
            fill.pack_block(UUID, ";" + "x" * 192, 0)

    def test_factory_needs_all_three_empty(self):
        # A partly cleared block is not factory
        only_crc = fill.pack_block("", "", 0, crc=1)
        self.check(only_crc, "invalid checksum mismatch")
        all_empty = fill.pack_block("", "", 0, crc=0)
        self.check(all_empty, "factory")
        window_only = fill.pack_block("", "", 1234, crc=0)
        self.check(window_only, "factory")


class FillerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix="stage1-fill-")
        self.addCleanup(self.dir.cleanup)

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def write(self, name, data):
        with open(self.path(name), "wb") as f:
            f.write(data)
        return self.path(name)

    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = fill.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_fill_and_check(self):
        image = fake_image()
        src = self.write("factory.bin", image)
        out = self.path("filled.bin")

        rc, so, se = self.run_main(src, out, "--esp-partuuid", UUID.upper())
        self.assertEqual(rc, 0, se)
        with open(src, "rb") as f:
            self.assertEqual(f.read(), image)
        with open(out, "rb") as f:
            filled = f.read()
        self.assertEqual(len(filled), len(image))
        self.assertTrue(filled.endswith(b"STACKBOT"))
        off, tag = fill.locate(filled)
        self.assertEqual(tag, TAG)
        self.assertEqual(filled[:off], image[:off])
        self.assertEqual(filled[off + 260:], image[off + 260:])
        block = filled[off:off + 260]
        self.assertEqual(c_parse(block), f"valid 0 {UUID};m1n1/boot.bin")

        rc, so, se = self.run_main("--check", out)
        self.assertEqual(rc, 0, se)
        self.assertIn(f"bytes: {len(filled)}", so)
        self.assertIn(f"sha256: {hashlib.sha256(filled).hexdigest()}", so)
        self.assertIn(f"md5: {hashlib.md5(filled).hexdigest()}", so)
        self.assertIn(f"tag: {TAG}", so)
        self.assertIn("configuration: valid", so)
        self.assertIn(f"esp_partuuid: {UUID}", so)
        self.assertIn("target: m1n1/boot.bin", so)
        self.assertIn("window_ms: 0", so)

        rc, so, se = self.run_main("--check", src)
        self.assertEqual(rc, 0, se)
        self.assertIn("configuration: factory", so)

    def test_fill_word_and_options(self):
        src = self.write("factory.bin", fake_image())
        out = self.path("filled.bin")
        rc, so, se = self.run_main("fill", src, out, "--esp-uuid", UUID, "--stage2-path",
                                   "EFI/x/s2.bin", "--window-ms", "15000")
        self.assertEqual(rc, 0, se)
        with open(out, "rb") as f:
            filled = f.read()
        off, _ = fill.locate(filled)
        self.assertEqual(c_parse(filled[off:off + 260]), f"valid 15000 {UUID};EFI/x/s2.bin")

    def test_boards(self):
        for board, model in [("j613", 'MacBook Air 13" M3 (J613)'),
                             ("j615", "MacBook Air 15-inch M3 (J615)")]:
            with self.subTest(board):
                image = fake_image(board=board)
                self.assertEqual(fill.board_of(image), board)
                src = self.write(f"{board}.bin", image)
                out = self.path(f"{board}-filled.bin")
                rc, so, se = self.run_main(src, out, "--esp-partuuid", UUID)
                self.assertEqual(rc, 0, se)
                self.assertIn(f"board: {board} ({model}", so)
                with open(out, "rb") as f:
                    filled = f.read()
                self.assertEqual(fill.board_of(filled), board)
                off, _ = fill.locate(filled)
                self.assertEqual(c_parse(filled[off:off + 260]), f"valid 0 {UUID};m1n1/boot.bin")

                rc, so, se = self.run_main("--check", out)
                self.assertEqual(rc, 0, se)
                self.assertIn(f"board: {board} ({model}", so)
                self.assertEqual("experimental" in so, board == "j615", so)

    def test_board_option(self):
        for board, other in [("j613", "j615"), ("j615", "j613")]:
            with self.subTest(board):
                src = self.write(f"{board}.bin", fake_image(board=board))
                out = self.path(f"{board}-out.bin")
                rc, so, se = self.run_main(src, out, "--esp-partuuid", UUID, "--board", other)
                self.assertEqual(rc, 1)
                self.assertIn(f"use the {other.upper()} image", se)
                self.assertFalse(os.path.exists(out))

                rc, so, se = self.run_main(src, out, "--esp-partuuid", UUID, "--board",
                                           board.upper())
                self.assertEqual(rc, 0, se)
        rc, so, se = self.run_main(self.path("j613.bin"), self.path("x.bin"), "--esp-partuuid",
                                   UUID, "--board", "j614")
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.path("x.bin")))

    def test_rejects_two_boards(self):
        for extra in (fill.BOARDS["j615"]["ident"] + b"J615\0",
                      fill.BOARDS["j613"]["ident"] + b"J613\0"):
            with self.subTest(extra):
                src = self.write("both.bin", fake_image(extra=extra))
                out = self.path("out.bin")
                rc, so, se = self.run_main(src, out, "--esp-partuuid", UUID)
                self.assertEqual(rc, 1)
                self.assertIn("not unique", se)
                self.assertFalse(os.path.exists(out))
                rc, so, se = self.run_main("--check", src)
                self.assertEqual(rc, 1)

    def test_cksum_matches_posix_tool(self):
        img = fake_image()
        for data in (b"", b"a", b"123456789", img):
            path = self.write("ck.bin", data)
            tool = shutil.which("cksum")
            if not tool:
                self.skipTest("no cksum")
            res = subprocess.run([tool, path], capture_output=True, text=True, check=True,
                                 env=dict(os.environ, POSIXLY_CORRECT="1"))
            crc, size = res.stdout.split()[:2]
            self.assertEqual((int(crc), int(size)), (fill.posix_cksum(data), len(data)))

    def test_output_is_new_file(self):
        src = self.write("factory.bin", fake_image())
        existing = self.write("exists.bin", b"keep")
        rc, so, se = self.run_main(src, existing, "--esp-partuuid", UUID)
        self.assertEqual(rc, 1)
        self.assertIn("already exists", se)
        with open(existing, "rb") as f:
            self.assertEqual(f.read(), b"keep")
        rc, so, se = self.run_main(src, src, "--esp-partuuid", UUID)
        self.assertEqual(rc, 1)
        with open(src, "rb") as f:
            self.assertEqual(f.read(), fake_image())

    def test_refill_is_noted(self):
        src = self.write("factory.bin", fake_image())
        one, two = self.path("one.bin"), self.path("two.bin")
        self.assertEqual(self.run_main(src, one, "--esp-partuuid", UUID)[0], 0)
        other = "11111111-2222-4333-8444-555555555555"
        rc, so, se = self.run_main(one, two, "--esp-partuuid", other, "--window-ms", "5")
        self.assertEqual(rc, 0, se)
        self.assertIn("already filled", se)
        with open(two, "rb") as f:
            filled = f.read()
        off, _ = fill.locate(filled)
        self.assertEqual(c_parse(filled[off:off + 260]), f"valid 5 {other};m1n1/boot.bin")

    def test_bad_arguments(self):
        src = self.write("factory.bin", fake_image())
        for args, msg in [
            (["--esp-partuuid", "not-a-uuid"], "not a partition UUID"),
            (["--esp-partuuid", UUID, "--window-ms", "100000"], "--window-ms"),
            (["--esp-partuuid", UUID, "--window-ms", "-1"], "--window-ms"),
            (["--esp-partuuid", UUID, "--target", "/m1n1/boot.bin"], "absolute"),
            (["--esp-partuuid", UUID, "--target", "../boot.bin"], "dot component"),
            (["--esp-partuuid", UUID, "--target", "a\\b"], "bad character"),
            (["--esp-partuuid", UUID, "--target", ";a"], "bad character"),
            (["--esp-partuuid", UUID, "--target", "x" * 191], "too long"),
        ]:
            with self.subTest(args=args):
                out = self.path("out.bin")
                rc, so, se = self.run_main(src, out, *args)
                self.assertEqual(rc, 1)
                self.assertIn(msg, se)
                self.assertFalse(os.path.exists(out))
        rc, so, se = self.run_main(src)
        self.assertEqual(rc, 1)
        rc, so, se = self.run_main("--check", src, src)
        self.assertEqual(rc, 1)

    def test_rejects_wrong_images(self):
        good = fill.encode_block(UUID, "a", 0)
        cases = {
            "no end marker": fake_image(end=False),
            "no stage 1 marker": fake_image(ident=False),
            "two blocks": fake_image(extra=bytes(-len(fake_image()) % 4) + factory_block()),
            "two tags": fake_image(extra=fill.TAG_PREFIX + b"x\0"),
            "bad tag": fake_image(tag="bad tag"),
            "no block": fake_image(block=b"\0" * 260),
            "version 2": fake_image(block=fill.pack_block("", "", 0, version=2, crc=0)),
        }
        for name, image in cases.items():
            with self.subTest(name):
                src = self.write("img.bin", image)
                out = self.path("out.bin")
                rc, so, se = self.run_main(src, out, "--esp-partuuid", UUID)
                self.assertEqual(rc, 1, name)
                self.assertTrue(se.startswith("error:"), se)
                self.assertFalse(os.path.exists(out))
                rc, so, se = self.run_main("--check", src)
                self.assertEqual(rc, 1, name)
        # misaligned block
        img = bytearray(fake_image())
        off = img.index(fill.MAGIC)
        img[off + 2:off + 262] = img[off:off + 260]
        img[off:off + 2] = b"\0\0"
        with self.assertRaises(fill.FillError):
            fill.locate(bytes(img))
        # a valid image with a filled block still locates
        self.assertEqual(fill.locate(fake_image(block=good))[1], TAG)

    def test_check_invalid_exits_1(self):
        block = bytearray(fill.encode_block(UUID, "a", 0))
        block[fill.OFF_CRC] ^= 0xFF
        src = self.write("bad.bin", fake_image(block=bytes(block)))
        rc, so, se = self.run_main("--check", src)
        self.assertEqual(rc, 1)
        self.assertIn("configuration: INVALID (checksum mismatch)", so)

    def test_needs_uuid_off_linux(self):
        src = self.write("factory.bin", fake_image())
        saved = fill.sys.platform
        fill.sys.platform = "darwin"
        try:
            rc, so, se = self.run_main(src, self.path("o.bin"))
        finally:
            fill.sys.platform = saved
        self.assertEqual(rc, 1)
        self.assertIn("--esp-partuuid is required", se)

    def _fake_tools(self, fstype, partuuid):
        bindir = self.path("bin")
        os.mkdir(bindir)
        tools = {
            "findmnt": ("#!/bin/sh\nprintf '%s\\n' 'autofs systemd-1' "
                        f"'{fstype} /dev/fake\\x20p1'\n"),
            "lsblk": f'#!/bin/sh\n[ "$3" = "/dev/fake p1" ] || exit 3\necho "{partuuid}"\n',
        }
        for name, body in tools.items():
            p = os.path.join(bindir, name)
            with open(p, "w") as f:
                f.write(body)
            os.chmod(p, stat.S_IRWXU)
        return bindir

    def _run_linux(self, bindir, *argv, compatible=None):
        saved = fill.sys.platform, os.environ["PATH"], fill.DT_COMPATIBLE
        fill.sys.platform = "linux"
        os.environ["PATH"] = bindir + os.pathsep + saved[1]
        fill.DT_COMPATIBLE = self.path("compatible")
        if compatible is not None:
            self.write("compatible", compatible)
        try:
            return self.run_main(*argv)
        finally:
            fill.sys.platform, os.environ["PATH"], fill.DT_COMPATIBLE = saved

    def test_linux_esp_detection(self):
        src = self.write("factory.bin", fake_image())
        esp = self.path("esp")
        os.makedirs(os.path.join(esp, "m1n1"))
        bindir = self._fake_tools("vfat", UUID.upper())

        rc, so, se = self._run_linux(bindir, src, self.path("a.bin"), "--esp-mount", esp)
        self.assertEqual(rc, 1)
        self.assertIn("does not exist", se)
        self.assertFalse(os.path.exists(self.path("a.bin")))

        rc, so, se = self._run_linux(bindir, src, self.path("a.bin"), "--esp-mount", esp,
                                     "--allow-missing-target")
        self.assertEqual(rc, 0, se)
        self.assertIn(UUID, so)

        with open(os.path.join(esp, "m1n1", "boot.bin"), "wb") as f:
            f.write(b"stage2")
        rc, so, se = self._run_linux(bindir, src, self.path("b.bin"), "--esp-mount", esp)
        self.assertEqual(rc, 0, se)
        with open(self.path("b.bin"), "rb") as f:
            filled = f.read()
        off, _ = fill.locate(filled)
        self.assertEqual(c_parse(filled[off:off + 260]), f"valid 0 {UUID};m1n1/boot.bin")

    def test_linux_refuses_other_board(self):
        esp = self.path("esp")
        os.makedirs(os.path.join(esp, "m1n1"))
        with open(os.path.join(esp, "m1n1", "boot.bin"), "wb") as f:
            f.write(b"stage2")
        bindir = self._fake_tools("vfat", UUID)
        dt = {board: b"apple,%s\0apple,t8122\0apple,arm-platform\0" % board.encode()
              for board in ("j613", "j615")}
        for image, here, ok in [("j613", "j613", True), ("j615", "j615", True),
                                ("j613", "j615", False), ("j615", "j613", False)]:
            with self.subTest(image=image, here=here):
                src = self.write(f"{image}.bin", fake_image(board=image))
                out = self.path(f"{image}-on-{here}.bin")
                rc, so, se = self._run_linux(bindir, src, out, "--esp-mount", esp,
                                             compatible=dt[here])
                if ok:
                    self.assertEqual(rc, 0, se)
                else:
                    self.assertEqual(rc, 1)
                    self.assertIn(f"use the {here.upper()} image", se)
                    self.assertFalse(os.path.exists(out))
        # Another machine, or no device tree: no board to compare with
        src = self.write("j615.bin", fake_image(board="j615"))
        rc, so, se = self._run_linux(bindir, src, self.path("j615-elsewhere.bin"), "--esp-mount",
                                     esp, compatible=b"apple,j313\0apple,t8103\0")
        self.assertEqual(rc, 0, se)
        # With --esp-partuuid the image may be for another Mac
        rc, so, se = self._run_linux(bindir, src, self.path("j615-uuid.bin"), "--esp-partuuid",
                                     UUID, compatible=dt["j613"])
        self.assertEqual(rc, 0, se)

    def test_linux_needs_vfat(self):
        src = self.write("factory.bin", fake_image())
        bindir = self._fake_tools("ext4", UUID)
        rc, so, se = self._run_linux(bindir, src, self.path("a.bin"), "--esp-mount",
                                     self.dir.name)
        self.assertEqual(rc, 1)
        self.assertIn("vfat", se)


class BuiltImageTest(unittest.TestCase):
    """Run the filler against real factory builds."""

    def check_built_image(self, var, board, other):
        path = os.environ.get(var)
        if not path:
            self.skipTest(f"{var} not set")
        with open(path, "rb") as f:
            image = f.read()
        off, tag = fill.locate(image)
        self.assertEqual(fill.board_of(image), board)
        self.assertEqual(off % 4, 0)
        self.assertEqual(c_parse(image[off:off + 260]), "factory")
        expect_tag = os.environ.get("STAGE1_TAG")
        if expect_tag:
            self.assertEqual(tag, expect_tag)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "filled.bin")
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(fill.main([path, out, "--esp-partuuid", UUID, "--board", other]),
                                 1)
            self.assertFalse(os.path.exists(out))
            with contextlib.redirect_stdout(io.StringIO()):
                rc = fill.main([path, out, "--esp-partuuid", UUID, "--board", board])
            self.assertEqual(rc, 0)
            with open(out, "rb") as f:
                filled = f.read()
            self.assertEqual(c_parse(filled[off:off + 260]), f"valid 0 {UUID};m1n1/boot.bin")

    def test_built_j613_image(self):
        self.check_built_image("STAGE1_IMAGE", "j613", "j615")

    def test_built_j615_image(self):
        self.check_built_image("STAGE1_J615_IMAGE", "j615", "j613")


if __name__ == "__main__":
    unittest.main()
