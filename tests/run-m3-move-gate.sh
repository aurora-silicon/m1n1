#!/bin/sh
# Host check of start.S's M3 gate on the move above the chainloaded image.
#
#   sh tests/run-m3-move-gate.sh
#
# Assembles src/start.S as the Makefile does, cuts the gate (the instructions between
# "mrs x1, midr_el1" and the first load of top_of_kernel_data) out of the object, and runs those
# exact bytes under qemu-aarch64 for one boot CPU MIDR_EL1 of every SoC below. Only the M3 cores
# (T8122, T6030, T6031) may take the move; M1, M2, the A18 Pro and other implementers stay put.
# Needs clang, ld.lld, llvm-objdump, llvm-objcopy and qemu-aarch64 (or qemu-aarch64-static).
set -eu
cd "$(dirname "$0")/.."
QEMU=${QEMU:-$(command -v qemu-aarch64 || command -v qemu-aarch64-static || true)}
[ -n "$QEMU" ] || { echo "run-m3-move-gate: no qemu-aarch64, skipped"; exit 77; }
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM

mkdir -p "$tmp/build" "$tmp/src"
cp src/start.S "$tmp/src/start.S"
: > "$tmp/build/build_cfg.h"
clang --target=aarch64-none-elf -c -O2 -ffreestanding -fpic -nostdinc \
    -isystem "$(clang -print-file-name=include)" -isystem sysinc -Isrc -Isrc/hv -I"$tmp" \
    -march=armv8.2-a -o "$tmp/start.o" "$tmp/src/start.S"
llvm-objcopy -O binary -j .init "$tmp/start.o" "$tmp/init.bin"
llvm-objdump -d --no-show-raw-insn -j .init "$tmp/start.o" > "$tmp/init.dis"

# The separate J613 stage1 must stay at the iBoot placement. Check actual
# preprocessed assembly, rather than assuming the build flag reaches start.S.
printf '%s\n' '#define J613_ESP_STAGE1' > "$tmp/build/build_cfg.h"
clang --target=aarch64-none-elf -c -O2 -ffreestanding -fpic -nostdinc \
    -isystem "$(clang -print-file-name=include)" -isystem sysinc -Isrc -Isrc/hv -I"$tmp" \
    -march=armv8.2-a -o "$tmp/stage1.o" "$tmp/src/start.S"
llvm-objdump -d --no-show-raw-insn -j .init "$tmp/stage1.o" > "$tmp/stage1.dis"
# The old move writes top_of_kernel_data after loading it at BootArgs+32.
if grep -Eq 'ldr[[:space:]]+x21, \[x19, #0x20\]' "$tmp/stage1.dis"; then
    echo 'FAIL: optional J613 stage1 still runs the stage2 self-move'
    exit 1
fi

# Gate start (after the mrs), end (the ldr that begins the move) and the skip label it targets
python3 - "$tmp" <<'PY'
import re, sys
tmp = sys.argv[1]
lines = open(f"{tmp}/init.dis").read().splitlines()
start = next(i for i, l in enumerate(lines) if l.endswith("<_start>:"))
ins = []
for l in lines[start + 1:]:
    m = re.match(r"\s*([0-9a-f]+):\s+(.*)", l)
    if not m:
        # The chainload header and local labels can interrupt objdump's listing
        # before the M3 move. They are not the end of the .init section.
        continue
    ins.append((int(m.group(1), 16), m.group(2).split("//")[0].strip()))
mrs = next(i for i, (_, t) in enumerate(ins) if re.fullmatch(r"mrs\s+x1, MIDR_EL1", t))
ldr = next(i for i, (_, t) in enumerate(ins) if i > mrs and re.match(r"ldr\s+x21, \[x19, #0x20\]", t))
gate = ins[mrs + 1:ldr]
targets = {int(re.search(r"0x([0-9a-f]+)", t).group(1), 16) for _, t in gate if t.startswith("b.")}
assert len(gate) == 7 and len(targets) == 1, (gate, targets)
assert all(gate[i][0] == gate[0][0] + 4 * i for i in range(len(gate))), gate
lo, skip = gate[0][0], targets.pop()
assert skip > ins[ldr][0], "the gate must skip the whole move"
blob = open(f"{tmp}/init.bin", "rb").read()
open(f"{tmp}/gate.bin", "wb").write(blob[lo:ins[ldr][0]])
open(f"{tmp}/skip_off", "w").write(str(skip - lo))
PY

# (MIDR_EL1, 1 if it may move) per SoC: implementer 0x61, variant, architecture 0xf, part, revision
cat > "$tmp/cases.S" <<'CASES'
#define MIDR(part) (0x610f0000 | ((part) << 4))
cases:
    .quad MIDR(0x22), 0, MIDR(0x23), 0              /* T8103 (M1) */
    .quad MIDR(0x24), 0, MIDR(0x25), 0              /* T6000 (M1 Pro) */
    .quad MIDR(0x28), 0, MIDR(0x29), 0              /* T6001/T6002 (M1 Max/Ultra) */
    .quad MIDR(0x32), 0, MIDR(0x33), 0              /* T8112 (M2) */
    .quad MIDR(0x34), 0, MIDR(0x35), 0              /* T6020 (M2 Pro) */
    .quad MIDR(0x38), 0, MIDR(0x39), 0              /* T6021/T6022 (M2 Max/Ultra) */
    .quad MIDR(0x42), 1, MIDR(0x43), 1              /* T8122 (M3) */
    .quad MIDR(0x44), 1, MIDR(0x45), 1              /* T6030 (M3 Pro) */
    .quad MIDR(0x48), 1, MIDR(0x49), 1              /* T6031 (M3 Max) */
    .quad MIDR(0x41), 0, MIDR(0x4a), 0              /* just outside the range */
    .quad MIDR(0x52), 0, MIDR(0x60), 0, MIDR(0x61), 0 /* T8132 (M4), T8140 (A18 Pro) */
    .quad 0x611f0230, 0, 0x612f0450, 1              /* other variant and revision bits */
    .quad 0x410f0440, 0, 0x000f0440, 0              /* other implementers, part 0x44 */
    .quad 0xffffffff, 0, 0, 0
cases_end:
CASES

skip_off=$(cat "$tmp/skip_off")
cat > "$tmp/harness.S" <<HARNESS
.text
.globl _start
_start:
    adr x20, cases
    adr x21, cases_end
    mov x22, #0                 /* failures */
1:
    ldp x1, x24, [x20], #16
    bl gate
    cmp x0, x24
    cinc x22, x22, ne
    cmp x20, x21
    b.lo 1b
    mov x0, x22
    mov x8, #93                 /* exit(failures) */
    svc #0

.balign 4
gate:
    .incbin "$tmp/gate.bin"
    mov x0, #1                  /* fell through: start.S would move the image */
    ret
    .space $skip_off - (. - gate)
    mov x0, #0                  /* the gate's skip target: stay put */
    ret

.data
.balign 8
#include "$tmp/cases.S"
HARNESS
clang --target=aarch64-linux-gnu -nostdlib -static -fuse-ld=lld -o "$tmp/harness" "$tmp/harness.S"
if "$QEMU" "$tmp/harness"; then
    echo "run-m3-move-gate: pass ($(($(wc -c < "$tmp/gate.bin") / 4)) gate instructions, skip at +$skip_off)"
else
    echo "run-m3-move-gate: FAIL ($? cases wrong)"
    exit 1
fi
