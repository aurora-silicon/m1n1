# SPDX-License-Identifier: MIT
"""m1n1's C boot_args must match the iBoot layout the proxyclient uses.

iBoot (like XNU's boot_args) puts the command line straight after the
32-bit devtree_size field. Every revision only changes its length.
"""
from pathlib import Path
import os
import shlex
import subprocess
import sys

import construct

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "proxyclient"))
from m1n1.tgtypes import BootArgs_r1, BootArgs_r2, BootArgs_r3  # noqa: E402


def offset(struct, name):
    total = 0
    for sub in struct.subcons:
        if sub.name == name:
            return total
        total += sub.sizeof()
    raise KeyError(name)


def test_boot_args_layout_matches_proxyclient(tmp_path):
    checks = []
    for rev, struct in ((1, BootArgs_r1), (2, BootArgs_r2), (3, BootArgs_r3)):
        for field in ("cmdline", "boot_flags", "mem_size_actual"):
            checks.append(f"_Static_assert(__builtin_offsetof(struct boot_args, rv{rev}.{field}) == "
                          f"{offset(struct, field):#x}, \"rv{rev}.{field}\");")
    checks.append(f"_Static_assert(__builtin_offsetof(struct boot_args, devtree_size) == "
                  f"{offset(BootArgs_r3, 'devtree_size'):#x}, \"devtree_size\");")
    checks.append(f"_Static_assert(sizeof(struct boot_args) == {BootArgs_r3.sizeof():#x}, \"size\");")
    assert offset(BootArgs_r3, "cmdline") == 0x6c
    test = tmp_path / "layout.c"
    test.write_text('#include "types.h"\n#include "xnuboot.h"\n' + "\n".join(checks) + "\n")
    cc = shlex.split(os.environ.get("CC", "clang"))
    subprocess.run(cc + ["--target=aarch64-linux-gnu", "-ffreestanding", "-fsyntax-only",
                         "-I", str(ROOT / "src"), str(test)], check=True)
