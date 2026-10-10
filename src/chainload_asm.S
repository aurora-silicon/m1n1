/* SPDX-License-Identifier: MIT */

.text

.globl _chainload_stub_start
.globl _chainload_stub_end
.type _chainload_stub_start, @function

_chainload_stub_start:
    mov x7, x2
    mov x8, x3
1:
    ldp x5, x6, [x1], #16
    stp x5, x6, [x2]
    add x2, x2, #16
    subs x3, x3, #16
    b.ne 1b

    mov x2, x7
    mov x3, x8
2:
    dc cvac, x2
    add x2, x2, #64
    subs x3, x3, #64
    b.ne 2b
    dsb sy

    mov x2, x7
    mov x3, x8
3:
    ic ivau, x2
    add x2, x2, #64
    subs x3, x3, #64
    b.ne 3b
    dsb sy
    isb

    br x4
_chainload_stub_end:
