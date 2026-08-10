#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run ANS initialization on a secondary CPU and poll its live breadcrumb."""

import argparse
import hashlib
import pathlib
import subprocess
import sys
import time

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

from m1n1.setup import *  # noqa: F403


def symbol_offset(elf: pathlib.Path, name: str) -> int:
    output = subprocess.check_output(["nm", "-n", str(elf)], text=True)
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] == name:
            return int(fields[0], 16)
    raise RuntimeError(f"symbol {name!r} not found in {elf}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=pathlib.Path)
    parser.add_argument("--cpu", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--nsid", type=int, default=1)
    parser.add_argument("--mode", type=int, default=1)
    parser.add_argument("--skip-smp-start", action="store_true")
    parser.add_argument(
        "--inspect-only",
        action="store_true",
        help="print queue/NVMMU/SART state without issuing an I/O command",
    )
    args = parser.parse_args()

    init = u.base + symbol_offset(args.elf, "nvme_init")  # noqa: F405
    progress = u.base + symbol_offset(args.elf, "nvme_progress")  # noqa: F405
    last_cc = u.base + symbol_offset(args.elf, "nvme_last_cc")  # noqa: F405
    last_csts = u.base + symbol_offset(args.elf, "nvme_last_csts")  # noqa: F405
    last_mode = u.base + symbol_offset(args.elf, "nvme_last_mode")  # noqa: F405
    timerless_mode = u.base + symbol_offset(args.elf, "nvme_timerless_mode")  # noqa: F405
    diag_asq = u.base + symbol_offset(args.elf, "nvme_diag_asq")  # noqa: F405
    diag_acq = u.base + symbol_offset(args.elf, "nvme_diag_acq")  # noqa: F405
    diag_aqa = u.base + symbol_offset(args.elf, "nvme_diag_aqa")  # noqa: F405
    diag_admin_tcb = u.base + symbol_offset(args.elf, "nvme_diag_admin_tcb")  # noqa: F405
    diag_io_sq = u.base + symbol_offset(args.elf, "nvme_diag_io_sq")  # noqa: F405
    diag_io_cq = u.base + symbol_offset(args.elf, "nvme_diag_io_cq")  # noqa: F405
    diag_io_tcb = u.base + symbol_offset(args.elf, "nvme_diag_io_tcb")  # noqa: F405
    diag_io_sq_sart = u.base + symbol_offset(args.elf, "nvme_diag_io_sq_sart")  # noqa: F405
    diag_create_cq_seen = u.base + symbol_offset(args.elf, "nvme_diag_create_cq_seen")  # noqa: F405
    diag_create_cq_status = u.base + symbol_offset(args.elf, "nvme_diag_create_cq_status")  # noqa: F405
    diag_create_cq_result = u.base + symbol_offset(args.elf, "nvme_diag_create_cq_result")  # noqa: F405
    diag_create_cq_dma_flags = u.base + symbol_offset(args.elf, "nvme_diag_create_cq_dma_flags")  # noqa: F405
    diag_create_sq_seen = u.base + symbol_offset(args.elf, "nvme_diag_create_sq_seen")  # noqa: F405
    diag_create_sq_status = u.base + symbol_offset(args.elf, "nvme_diag_create_sq_status")  # noqa: F405
    diag_create_sq_result = u.base + symbol_offset(args.elf, "nvme_diag_create_sq_result")  # noqa: F405
    diag_create_sq_dma_flags = u.base + symbol_offset(args.elf, "nvme_diag_create_sq_dma_flags")  # noqa: F405
    diag_nvmmu_num_readback = u.base + symbol_offset(args.elf, "nvme_diag_nvmmu_num_readback")  # noqa: F405
    diag_nvmmu_asq_readback = u.base + symbol_offset(args.elf, "nvme_diag_nvmmu_asq_readback")  # noqa: F405
    diag_nvmmu_iosq_readback = u.base + symbol_offset(args.elf, "nvme_diag_nvmmu_iosq_readback")  # noqa: F405
    diag_io_submit_opcode = u.base + symbol_offset(args.elf, "nvme_diag_io_submit_opcode")  # noqa: F405
    diag_io_submit_dma_flags = u.base + symbol_offset(args.elf, "nvme_diag_io_submit_dma_flags")  # noqa: F405
    diag_io_submit_len = u.base + symbol_offset(args.elf, "nvme_diag_io_submit_len")  # noqa: F405
    diag_io_submit_prp1 = u.base + symbol_offset(args.elf, "nvme_diag_io_submit_prp1")  # noqa: F405
    base_ptr = u.base + symbol_offset(args.elf, "nvme_base")  # noqa: F405
    ctrl_base_ptr = u.base + symbol_offset(args.elf, "nvme_ctrl_base")  # noqa: F405
    print(
        f"NVME_ASYNC init=0x{init:x} progress=0x{progress:x} "
        f"last_cc=0x{last_cc:x} last_csts=0x{last_csts:x}",
        flush=True,
    )

    if not args.skip_smp_start:
        p.smp_start_secondaries()  # noqa: F405
    if not p.smp_is_alive(args.cpu):  # noqa: F405
        print(f"NVME_ASYNC cpu={args.cpu} alive=0", flush=True)
        return 2

    # T8142 secondaries deliberately park in architectural WFI during initial
    # bring-up. Switch them to m1n1's event-driven idle loop before dispatching
    # the asynchronous call; otherwise P_SMP_CALL can wait forever even though
    # the target core itself started successfully.
    p.smp_set_wfe_mode(True)  # noqa: F405

    p.write32(progress, 0)  # noqa: F405
    # Pass diagnostic mode in x0 so the worker CPU selects timerless polling
    # itself. A shared flag written here can remain stale in the secondary's
    # cache during early T8142 bring-up.
    p.smp_call(args.cpu, init, args.mode)  # noqa: F405

    deadline = time.monotonic() + args.timeout
    last = None
    state = 0
    while time.monotonic() < deadline:
        state = int(p.read32(progress))  # noqa: F405
        if state != last:
            asq = int(p.read32(diag_asq)) | (int(p.read32(diag_asq + 4)) << 32)  # noqa: F405
            acq = int(p.read32(diag_acq)) | (int(p.read32(diag_acq + 4)) << 32)  # noqa: F405
            print(
                f"NVME_ASYNC progress=0x{state:08x} "
                f"mode=0x{int(p.read32(last_mode)):08x} "  # noqa: F405
                f"cc=0x{int(p.read32(last_cc)):08x} "  # noqa: F405
                f"csts=0x{int(p.read32(last_csts)):08x} "  # noqa: F405
                f"asq=0x{asq:016x} acq=0x{acq:016x} "
                f"aqa=0x{int(p.read32(diag_aqa)):08x}",  # noqa: F405
                flush=True,
            )
            last = state
        if state in (0x100, 0x7100) or state & 0x80000000:
            break
        time.sleep(0.1)

    if state == 0x7100:
        result = int(p.smp_wait(args.cpu))  # noqa: F405
        base = int(p.read32(base_ptr)) | (int(p.read32(base_ptr + 4)) << 32)  # noqa: F405
        ctrl_base = int(p.read32(ctrl_base_ptr)) | (  # noqa: F405
            int(p.read32(ctrl_base_ptr + 4)) << 32  # noqa: F405
        )

        def read64(offset: int) -> int:
            return int(p.read32(ctrl_base + offset)) | (  # noqa: F405
                int(p.read32(ctrl_base + offset + 4)) << 32  # noqa: F405
            )

        print(
            f"NVME_ASYNC aperture result={result} vendor_base=0x{base:016x} "
            f"ctrl_base=0x{ctrl_base:016x} "
            f"cap=0x{read64(0x00):016x} vs=0x{int(p.read32(ctrl_base + 0x08)):08x} "  # noqa: F405
            f"cc=0x{int(p.read32(ctrl_base + 0x14)):08x} "  # noqa: F405
            f"csts=0x{int(p.read32(ctrl_base + 0x1c)):08x} "  # noqa: F405
            f"aqa=0x{int(p.read32(ctrl_base + 0x24)):08x} "  # noqa: F405
            f"asq=0x{read64(0x28):016x} acq=0x{read64(0x30):016x}",
            flush=True,
        )
        return 0

    if state != 0x100:
        print(f"NVME_ASYNC stalled=0x{state:08x}", flush=True)
        return 3

    result = int(p.smp_wait(args.cpu))  # noqa: F405
    p.write32(timerless_mode, 0)  # noqa: F405
    print(f"NVME_ASYNC initialized={result}", flush=True)
    if not result:
        return 4

    def read_ptr(address: int) -> int:
        return int(p.read32(address)) | (int(p.read32(address + 4)) << 32)  # noqa: F405

    base = read_ptr(base_ptr)
    # The queue-state offsets recovered from ansf are firmware-private virtual
    # addresses, not host-visible BAR registers.  Direct reads through either
    # ANS aperture external-abort on J813, so keep live inspection limited to
    # the explicitly exported m1n1 diagnostic variables below.

    print(
        "NVME_DIAG "
        f"admin_tcb=0x{read_ptr(diag_admin_tcb):016x} "
        f"io_sq=0x{read_ptr(diag_io_sq):016x} "
        f"io_cq=0x{read_ptr(diag_io_cq):016x} "
        f"io_tcb=0x{read_ptr(diag_io_tcb):016x} "
        f"io_sq_sart={int(p.read32(diag_io_sq_sart))}",  # noqa: F405
        flush=True,
    )
    print(
        "NVME_QUEUE_DIAG "
        f"create_cq_seen={int(p.read32(diag_create_cq_seen))} "  # noqa: F405
        f"create_cq_status=0x{int(p.read32(diag_create_cq_status)):08x} "  # noqa: F405
        f"create_cq_result=0x{read_ptr(diag_create_cq_result):016x} "
        f"create_cq_dma_flags=0x{int(p.read32(diag_create_cq_dma_flags)):08x} "  # noqa: F405
        f"create_sq_seen={int(p.read32(diag_create_sq_seen))} "  # noqa: F405
        f"create_sq_status=0x{int(p.read32(diag_create_sq_status)):08x} "  # noqa: F405
        f"create_sq_result=0x{read_ptr(diag_create_sq_result):016x} "
        f"create_sq_dma_flags=0x{int(p.read32(diag_create_sq_dma_flags)):08x}",  # noqa: F405
        flush=True,
    )
    print(
        "NVME_NVMMU_DIAG "
        f"num=0x{int(p.read32(diag_nvmmu_num_readback)):08x} "  # noqa: F405
        f"asq_tcb_readback=0x{read_ptr(diag_nvmmu_asq_readback):016x} "
        f"iosq_tcb_readback=0x{read_ptr(diag_nvmmu_iosq_readback):016x}",
        flush=True,
    )

    # SARTv3 entries are three parallel 16-element register arrays:
    # config @ +0x00, physical page @ +0x40, and page count @ +0x80.
    # Read them back without changing the table so the host can verify that
    # the temporary I/O-SQ admission actually reached CoastGuard.
    sart_base = u.adt["/arm-io/sart-ans"].get_reg(0)[0]  # noqa: F405
    for entry in range(16):
        flags = int(p.read32(sart_base + 0x00 + 4 * entry))  # noqa: F405
        page = int(p.read32(sart_base + 0x40 + 4 * entry))  # noqa: F405
        pages = int(p.read32(sart_base + 0x80 + 4 * entry))  # noqa: F405
        if flags:
            print(
                f"NVME_SART entry={entry} flags=0x{flags:08x} "
                f"base=0x{page << 12:016x} size=0x{pages << 12:x}",
                flush=True,
            )

    if args.inspect_only:
        print("NVME_ASYNC inspect-only=1 read-issued=0", flush=True)
        return 0

    buffer = u.memalign(0x1000, 0x1000)  # noqa: F405
    try:
        for lba in (0, 1):
            ok = bool(p.nvme_read(args.nsid, lba, buffer))  # noqa: F405
            block = iface.readmem(buffer, 0x1000) if ok else b""  # noqa: F405
            print(
                f"NVME_ASYNC read lba={lba} ok={int(ok)} "
                f"sha256={hashlib.sha256(block).hexdigest() if ok else '-'} "
                f"head={block[:64].hex() if ok else '-'}",
                flush=True,
            )
            print(
                "NVME_IO_TCB_DIAG "
                f"opcode=0x{int(p.read32(diag_io_submit_opcode)):08x} "  # noqa: F405
                f"dma_flags=0x{int(p.read32(diag_io_submit_dma_flags)):08x} "  # noqa: F405
                f"len=0x{int(p.read32(diag_io_submit_len)):08x} "  # noqa: F405
                f"prp1=0x{read_ptr(diag_io_submit_prp1):016x}",
                flush=True,
            )
            if not ok:
                return 5
    finally:
        u.free(buffer)  # noqa: F405
        try:
            p.nvme_shutdown()  # noqa: F405
            print("NVME_ASYNC shutdown=1", flush=True)
        except Exception as exc:
            print(f"NVME_ASYNC shutdown=0 error={exc!r}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
