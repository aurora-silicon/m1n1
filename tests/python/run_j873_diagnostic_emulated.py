#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Execute the real diagnostic ELF with synthetic RAM, ADT, CPU IDs and UART.

This is an instruction/control-flow test, not an M6 hardware model. It does not
emulate PMGR2, AIC3, SPTM, DMA, caches or interrupt delivery. Requires unicorn,
pyelftools and construct. Every unmapped access and unexpected system register
read fails the test. Wrong-board tests must halt without touching the UART.
"""
import argparse
from pathlib import Path
import struct
import sys

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UcError, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_INVALID
from unicorn.arm64_const import UC_ARM64_REG_X0, UC_ARM64_REG_PC

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "proxyclient"))
from m1n1.adt import ADTNodeStruct, load_adt

BASE = 0x800000000
ADT = BASE + 0x3000000
ARGS = ADT + 0x200000
UART = 0x331200000


def node(name, props=None, children=None):
    props = {"name": name.encode() + b"\0", **(props or {})}
    return dict(property_count=len(props), child_count=len(children or []),
                properties=[dict(name=k, size=len(v), value=v) for k, v in props.items()],
                children=children or [])


def fixture(chip, board, compatible):
    return ADTNodeStruct.build(node("device-tree", {
        "model": b"Mac18,5\0", "target-type": b"J873g\0", "compatible": compatible,
        "#address-cells": struct.pack("<I", 2), "#size-cells": struct.pack("<I", 2),
    }, [node("chosen", {"chip-id": struct.pack("<I", chip), "board-id": struct.pack("<I", board)}),
        node("arm-io", {"#address-cells": struct.pack("<I", 2), "#size-cells": struct.pack("<I", 2),
                        "ranges": struct.pack("<QQQ", 0, 0x200000000, 0x10000000000)}, [
            node("uart0", {"reg": struct.pack("<QQ", UART - 0x200000000, 0x4000)})]),
        node("cpus", children=[node("cpu0", {"cpu-id": struct.pack("<I", 0), "state": b"running\0"})])]))


def run(elf_path, chip, board, compatible, expected, adt_path=None, el=1):
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    uc.mem_map(BASE, 0x4000000)
    uc.mem_map(UART, 0x4000)
    with elf_path.open("rb") as stream:
        elf = ELFFile(stream)
        for segment in elf.iter_segments():
            if segment["p_type"] == "PT_LOAD":
                uc.mem_write(BASE + segment["p_vaddr"], segment.data())
        symbols = {s.name: s["st_value"] for s in elf.get_section_by_name(".symtab").iter_symbols()}
    adt = fixture(chip, board, compatible)
    if adt_path:
        tree = load_adt(adt_path.read_bytes())
        assert tree.model == "Mac18,5" and tree.target_type == "J873g"
        # Static firmware /chosen IDs are placeholders. This models a runtime
        # handoff explicitly; it does not convert them into measured evidence.
        tree["/chosen"].chip_id = chip
        tree["/chosen"].board_id = board
        # iBoot materializes template properties before handing over a live ADT.
        # The runtime Rust parser expects ordinary lengths, not bit-31 template
        # flags. Retain static values but clear those flags in this fixture only.
        for entry in tree.walk_tree():
            for name, (kind, _) in list(entry._types.items()):
                entry._types[name] = (kind, False)
        adt = tree.build()
    assert len(adt) < ARGS - ADT
    uc.mem_write(ADT, adt)
    args = bytearray(0x4000)
    struct.pack_into("<HH", args, 0, 3, 2)
    struct.pack_into("<QQQQ", args, 8, BASE, BASE, 0x4000000, ARGS + len(args))
    struct.pack_into("<QI", args, 96, ADT, len(adt))
    uc.mem_write(ARGS, bytes(args))
    uc.reg_write(UC_ARM64_REG_X0, ARGS)
    output = bytearray()
    state = {"halted": False, "uart_accesses": 0}
    entries = {BASE + value: name for name, value in symbols.items() if name}
    recent = []
    forbidden = {BASE + symbols[name] for name in ["init_cpu", "pmgr_init", "aic_init",
                 "wdt_disable", "usb_init", "dockchannel_uart_init", "payload_run",
                 "mmu_init", "m1n1_main", "uartproxy_run"] if name in symbols}
    # System-register encodings from the assembled MRS instructions. Values are
    # explicitly synthetic: do not publish them as observed M6 identification.
    mrs_values = {0xd5384240: el << 2, # synthetic CurrentEL
                  0xd5380000: 0x410fd000,  # synthetic MIDR
                  0xd53800a0: 0x80000000,  # synthetic MPIDR
                  0xd53be000: 24000000,    # CNTFRQ
                  0xd5380700: 0, 0xd5380600: 0, 0xd5380400: 0,
                  0xd5381000: 0}  # SCTLR_EL1, synthetic MMU-off entry
    def code(uc, address, size, _):
        assert address not in forbidden, f"diagnostic reached forbidden initializer {entries[address]}"
        if address in entries:
            recent.append(entries[address])
            del recent[:-16]
        if address == BASE + symbols["iodev_console_write"]:
            message = bytes(uc.mem_read(uc.reg_read(UC_ARM64_REG_X0), 128))
            raise AssertionError(f"unexpected generic console path: {message!r}; calls={recent}")
        word = int.from_bytes(uc.mem_read(address, 4), "little")
        if word == 0xd503205f:  # WFE, the explicit diagnostic halt
            state["halted"] = True
            uc.emu_stop()
        elif word & 0xfff00000 == 0xd5300000:  # MRS
            key = word & ~31
            if key not in mrs_values:
                raise AssertionError(f"unexpected MRS {word:#x} at {address:#x}")
            rt = word & 31
            if rt < 29:
                uc.reg_write(UC_ARM64_REG_X0 + rt, mrs_values[key])
            else:
                raise AssertionError("unexpected system-register destination")
            uc.reg_write(UC_ARM64_REG_PC, address + 4)
    def read(uc, access, address, size, value, _):
        if UART <= address < UART + 0x4000:
            state["uart_accesses"] += 1
            assert address == UART + 0x10 and size == 4
            uc.mem_write(address, struct.pack("<I", 6))
    def write(uc, access, address, size, value, _):
        if UART <= address < UART + 0x4000:
            state["uart_accesses"] += 1
            assert address == UART + 0x20 and size == 4
            output.append(value & 255)
    uc.hook_add(UC_HOOK_CODE, code)
    uc.hook_add(UC_HOOK_MEM_READ, read)
    uc.hook_add(UC_HOOK_MEM_WRITE, write)
    def invalid(uc, access, address, size, value, _):
        raise AssertionError(f"unexpected memory access {address:#x} size={size}; calls={recent}")
    uc.hook_add(UC_HOOK_MEM_INVALID, invalid)
    try:
        uc.emu_start(BASE + symbols["_start"], BASE + 0x4000000, count=3000000)
    except UcError as error:
        raise AssertionError(f"{error}; PC={uc.reg_read(UC_ARM64_REG_PC):#x}; calls={recent}; output={output[-160:]!r}") from error
    assert state["halted"], "diagnostic exceeded the instruction budget"
    if expected:
        assert b"J873_DIAGNOSTIC_COMPLETE" in output, output.decode(errors="replace")
        assert b"chip=8152 board=24" in output
        assert b"cpu-id=0 state=" in output
    else:
        assert not output and state["uart_accesses"] == 0
    return len(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=Path)
    parser.add_argument("--adt", type=Path, help="optional real firmware ADT with synthetic runtime IDs")
    args = parser.parse_args()
    compatible = b"J873gAP\0Mac18,5\0AppleARM\0"
    scenarios = [(0x8152, 0x24, compatible, True),
                 (0x8142, 0x24, compatible, False),
                 (0x8152, 0x02, compatible, False),
                 (0x8152, 0x24, compatible[:-1], False)]
    for chip, board, value, expected in scenarios:
        count = run(args.elf, chip, board, value, expected)
        print(f"PASS synthetic chip={chip:#x} board={board:#x} compatible_bytes={len(value)} output_bytes={count}")
    count = run(args.elf, 0x8152, 0x24, compatible, True, el=2)
    print(f"PASS synthetic EL2 entry: output_bytes={count}")
    if args.adt:
        count = run(args.elf, 0x8152, 0x24, compatible, True, args.adt)
        print(f"PASS firmware ADT with synthetic runtime IDs and CPU registers: output_bytes={count}")


if __name__ == "__main__":
    main()
