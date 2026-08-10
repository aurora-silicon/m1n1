#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Initialize Apple ANS/NVMe and read selected blocks without writing media."""

import argparse
import hashlib
import pathlib
import sys

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

from m1n1.setup import *  # noqa: F403


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nsid", type=int, default=1)
    parser.add_argument(
        "--lba",
        dest="lbas",
        type=lambda value: int(value, 0),
        action="append",
        default=None,
        help="4 KiB logical block to read; repeat as needed (default: 0 and 1)",
    )
    args = parser.parse_args()
    lbas = args.lbas if args.lbas is not None else [0, 1]

    buffer = None
    initialized = False
    try:
        print(f"NVME_PROBE init nsid={args.nsid}", flush=True)
        initialized = bool(p.nvme_init())  # noqa: F405
        print(f"NVME_PROBE initialized={int(initialized)}", flush=True)
        if not initialized:
            return 2

        buffer = u.memalign(0x1000, 0x1000)  # noqa: F405
        for lba in lbas:
            ok = bool(p.nvme_read(args.nsid, lba, buffer))  # noqa: F405
            print(f"NVME_PROBE read lba={lba} ok={int(ok)}", flush=True)
            if not ok:
                return 3
            block = iface.readmem(buffer, 0x1000)  # noqa: F405
            print(
                "NVME_PROBE data "
                f"lba={lba} sha256={hashlib.sha256(block).hexdigest()} "
                f"head={block[:64].hex()}",
                flush=True,
            )
        return 0
    finally:
        if buffer is not None:
            u.free(buffer)  # noqa: F405
        if initialized:
            p.nvme_shutdown()  # noqa: F405
            print("NVME_PROBE shutdown=1", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
