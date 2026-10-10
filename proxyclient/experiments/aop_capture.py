#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""One cold J616s HQ microphone recording; resources remain until WDT recovery."""

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.append(str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from m1n1.fw.aop.j616s_capture import J616sHPCapture, wave_files
from t6040_iop import probe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "output",
        type=Path,
        help="Local Float32 WAV output; metadata/raw/preview use the same stem",
    )
    parser.add_argument(
        "--device", default=os.environ.get("M1N1DEVICE", "/dev/ttyACM0")
    )
    parser.add_argument(
        "--bytes",
        type=lambda value: int(value, 0),
        choices=(0x200000, 0x600000),
        default=0x600000,
    )
    parser.add_argument("--preview-channel", type=int, choices=(0, 1, 2), default=0)
    args = parser.parse_args()
    if args.output.suffix.lower() != ".wav":
        parser.error("output must use a .wav extension")
    outputs = (
        args.output,
        args.output.with_suffix(".json"),
        args.output.with_suffix(".bin"),
        args.output.with_name(args.output.stem + "-preview.wav"),
    )
    if any(path.exists() or path.is_symlink() for path in outputs):
        parser.error("Recording outputs must not already exist")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    metadata = args.output.with_suffix(".json")
    raw = args.output.with_suffix(".bin")
    from m1n1.proxy import UartInterface, M1N1Proxy
    from m1n1.proxyutils import ProxyUtils, bootstrap_port

    iface = UartInterface(args.device)
    capture = None
    try:
        iface.dev.timeout = iface.dev.write_timeout = 1
        iface.nop()
        p = M1N1Proxy(iface)
        p.nop()
        if p.get_chipid() != 0x6040:
            raise ValueError(
                "Only the qualified T6040/J616s cold RAM setup is supported"
            )
        bootstrap_port(iface, p)
        iface.dev.timeout = iface.dev.write_timeout = 1
        u = ProxyUtils(p)
        if u.adt["/chosen"].board_id != 6:
            raise ValueError("Requires J616s board 6")
        if p.wdt_arm(45) != 0:
            raise RuntimeError("Recent RAM proxy hardware watchdog support required")

        def persist(state):
            metadata.write_text(json.dumps(state, indent=2) + "\n")

        capture = J616sHPCapture(u, size=args.bytes, persist=persist)
        capture.validate_profile()
        probe("aop", proxy=p, utils=u, on_aop_ready=capture)
        raw.write_bytes(capture.data)
        capture.state["wave"] = wave_files(
            capture.data, args.output, preview_channel=args.preview_channel
        )
        persist(capture.state)
        print(
            f"Saved {args.output} and normalized mono preview. Wait for watchdog recovery before another capture."
        )
    except BaseException as error:
        if capture is not None:
            capture.state.update(
                uncertain=True,
                error=str(error),
                recovery="Retain resources until watchdog recovery",
            )
        raise
    finally:
        try:
            if capture is not None:
                if capture.data is not None and not raw.exists():
                    raw.write_bytes(capture.data)
                metadata.write_text(json.dumps(capture.state, indent=2) + "\n")
        finally:
            iface.dev.close()


if __name__ == "__main__":
    main()
