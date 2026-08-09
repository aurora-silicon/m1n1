#
# Walk the stuck guest's call stack from m1n1's hypervisor shell.
#
# Usage, from the `>>>` prompt after breaking in with ^C:
#
#     hv.run_script("/path/to/hv_backtrace.py")
#
# or just paste the file in. Prints the frame chain as raw guest addresses;
# feed the output to tools/uefi_symbolize.py to turn it into module+symbol.
#
# Reads only -- nothing here writes to the guest or to hardware.
#
# Why a frame walk and not the built-in context dump: `print_context()` shows the
# faulting frame only. When UEFI is *stuck* rather than crashed (spinning on MMIO,
# waiting on a device that never answers) the interesting information is who
# called whom to get there, and that lives in the FP chain.
#
# AArch64 AAPCS frame layout, which is what makes this possible:
#
#     [x29 + 0] = caller's x29   (previous frame pointer)
#     [x29 + 8] = caller's x30   (return address)
#
# EDK2 builds keep frame pointers, so the chain is walkable. It is still a
# heuristic: a leaf function that has not set up its frame yet, or anything built
# with -fomit-frame-pointer, will produce a short or skewed trace. Treat the first
# entry (PC) and the last few as the reliable parts.
#

MAX_FRAMES = 48


def _ram_bounds():
    #
    # Only follow pointers into real DRAM. m1n1 will happily read MMIO, and a
    # garbage frame pointer landing on a device register can hang the machine or
    # change hardware state -- exactly what we do not want while diagnosing.
    #
    base = u.ba.phys_base
    top = base + u.ba.mem_size
    return base, top


def _plausible_frame(addr, lo, hi):
    if addr == 0:
        return False
    if addr & 7:                       # frames are 8-byte aligned
        return False
    return lo <= addr < hi - 16


def _read64(addr):
    try:
        return p.read64(addr)
    except Exception as e:
        print(f"    <read failed at {addr:#x}: {e!r}>")
        return None


def backtrace(ctx=None):
    ctx = ctx or hv.ctx
    if ctx is None:
        print("No guest context. Break in with ^C while the guest is running.")
        return []

    lo, hi = _ram_bounds()

    pc = ctx.elr
    lr = ctx.regs[30]
    fp = ctx.regs[29]

    print(f"guest cpu {ctx.cpu_id} mpidr {ctx.mpidr:#x}")
    print(f"  PC  = {pc:#x}")
    print(f"  LR  = {lr:#x}")
    print(f"  FP  = {fp:#x}")
    print(f"  SP  = " + " ".join(f"{s:#x}" for s in ctx.sp))
    print(f"  ESR = {ctx.esr.value:#x}   FAR = {ctx.far:#x}")
    print(f"  DRAM window for frame validation: {lo:#x} .. {hi:#x}")
    print()

    # PC first, then LR: LR is the return address of whatever PC is inside, so it
    # is a real frame even before the chain starts.
    chain = [pc, lr]
    print("frames:")
    print(f"  #0  {pc:#018x}   (PC)")
    print(f"  #1  {lr:#018x}   (LR)")

    n = 2
    seen = set()
    while n < MAX_FRAMES:
        if not _plausible_frame(fp, lo, hi):
            print(f"  --  stop: FP {fp:#x} outside {lo:#x}..{hi:#x} or misaligned")
            break
        if fp in seen:
            print(f"  --  stop: FP {fp:#x} repeats, chain is looping")
            break
        seen.add(fp)

        next_fp = _read64(fp)
        ret = _read64(fp + 8)
        if next_fp is None or ret is None:
            break
        if ret == 0:
            print("  --  stop: return address 0")
            break

        print(f"  #{n}  {ret:#018x}   (fp {fp:#x})")
        chain.append(ret)
        fp = next_fp
        n += 1

    print()
    print("Paste this line into tools/uefi_symbolize.py:")
    print("  " + " ".join(hex(a) for a in chain))
    return chain


backtrace()
