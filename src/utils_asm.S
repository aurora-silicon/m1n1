/* SPDX-License-Identifier: MIT */

#include "cpu_regs.h"

.text

.globl memcpy128
.type memcpy128, @function
memcpy128:
    ands    x2, x2, #~15
    beq     2f
1:  ldp     x3, x4, [x1], #16
    stp     x3, x4, [x0], #16
    subs    x2, x2, #16
    bne     1b
2:
    ret

.globl memcpy64
.type memcpy64, @function
memcpy64:
    ands    x2, x2, #~7
    beq     2f
1:  ldr     x3, [x1], #8
    str     x3, [x0], #8
    subs    x2, x2, #8
    bne     1b
2:
    ret

.globl memset64
.type memset64, @function
memset64:
    ands    x2, x2, #~7
    beq     2f
1:  str     x1, [x0], #8
    subs    x2, x2, #8
    bne     1b
2:
    ret

.globl memcpy32
.type memcpy32, @function
memcpy32:
    ands    x2, x2, #~3
    beq     2f
1:  ldr     w3, [x1], #4
    str     w3, [x0], #4
    subs    x2, x2, #4
    bne     1b
2:
    ret

.globl memset32
.type memset32, @function
memset32:
    ands    x2, x2, #~3
    beq     2f
1:  str     w1, [x0], #4
    subs    x2, x2, #4
    bne     1b
2:
    ret

.globl memcpy16
.type memcpy16, @function
memcpy16:
    ands    x2, x2, #~1
    beq     2f
1:  ldrh    w3, [x1], #2
    strh    w3, [x0], #2
    subs    x2, x2, #2
    bne     1b
2:
    ret

.globl memset16
.type memset16, @function
memset16:
    ands    x2, x2, #~1
    beq     2f
1:  strh    w1, [x0], #2
    subs    x2, x2, #2
    bne     1b
2:
    ret

.globl memcpy8
.type memcpy8, @function
memcpy8:
    cmp     x2, #0
    beq     2f
1:  ldrb    w3, [x1], #1
    strb    w3, [x0], #1
    subs    x2, x2, #1
    bne     1b
2:
    ret

.globl memset8
.type memset8, @function
memset8:
    cmp     x2, #0
    beq     2f
1:  strb    w1, [x0], #1
    subs    x2, x2, #1
    bne     1b
2:
    ret

.globl get_simd_state
.type get_simd_state, @function
get_simd_state:
    stp q0, q1, [x0], #32
    stp q2, q3, [x0], #32
    stp q4, q5, [x0], #32
    stp q6, q7, [x0], #32
    stp q8, q9, [x0], #32
    stp q10, q11, [x0], #32
    stp q12, q13, [x0], #32
    stp q14, q15, [x0], #32
    stp q16, q17, [x0], #32
    stp q18, q19, [x0], #32
    stp q20, q21, [x0], #32
    stp q22, q23, [x0], #32
    stp q24, q25, [x0], #32
    stp q26, q27, [x0], #32
    stp q28, q29, [x0], #32
    stp q30, q31, [x0], #32
    ret

.globl put_simd_state
.type put_simd_state, @function
put_simd_state:
    ldp q0, q1, [x0], #32
    ldp q2, q3, [x0], #32
    ldp q4, q5, [x0], #32
    ldp q6, q7, [x0], #32
    ldp q8, q9, [x0], #32
    ldp q10, q11, [x0], #32
    ldp q12, q13, [x0], #32
    ldp q14, q15, [x0], #32
    ldp q16, q17, [x0], #32
    ldp q18, q19, [x0], #32
    ldp q20, q21, [x0], #32
    ldp q22, q23, [x0], #32
    ldp q24, q25, [x0], #32
    ldp q26, q27, [x0], #32
    ldp q28, q29, [x0], #32
    ldp q30, q31, [x0], #32
    ret

.globl _deep_wfi_helper
.type _deep_wfi_helper, @function
_deep_wfi_helper:
    stp x30, x0, [sp, #-16]!
    stp x28, x29, [sp, #-16]!
    stp x26, x27, [sp, #-16]!
    stp x24, x25, [sp, #-16]!
    stp x22, x23, [sp, #-16]!
    stp x20, x21, [sp, #-16]!
    stp x18, x19, [sp, #-16]!

    wfi

    ldp x18, x19, [sp], #16
    ldp x20, x21, [sp], #16
    ldp x22, x23, [sp], #16
    ldp x24, x25, [sp], #16
    ldp x26, x27, [sp], #16
    ldp x28, x29, [sp], #16
    ldp x30, x0, [sp], #16

    ret

/*
 * Waits that do not trust the general-purpose register file to survive them.
 *
 * T8142 gives m1n1 no way to select a state-retaining wait mode -- writing
 * SYS_IMP_APL_CYC_OVRD takes an undefined instruction exception -- so a core can
 * come back from a wait with the register file cleared.  That is not
 * hypothetical: it is what destroys the guest's x26 across HalProcessorIdle
 * (bugcheck 0xA), and m1n1's own C code is exposed to the same thing wherever it
 * waits, because the compiler keeps live values in x19-x28 across the
 * instruction.
 *
 * Both routines spill the whole callee-saved set to their own frame and reload
 * it afterwards, so a caller sees an ordinary AAPCS call whatever the hardware
 * does.  SP and memory do survive -- the Windows idle path's own post-wait
 * reload of x29/x30 from its stack is what lets it return at all.
 *
 * cpu_wfe_stateless() additionally reports the fact: it leaves a canary in x19
 * across the wait and returns 1 if it did not come back.
 */
.globl cpu_wfi_stateless
.type cpu_wfi_stateless, @function
cpu_wfi_stateless:
    stp x29, x30, [sp, #-96]!
    mov x29, sp
    stp x19, x20, [sp, #16]
    stp x21, x22, [sp, #32]
    stp x23, x24, [sp, #48]
    stp x25, x26, [sp, #64]
    stp x27, x28, [sp, #80]

    dsb sy
    isb
    wfi
    isb

    ldp x19, x20, [sp, #16]
    ldp x21, x22, [sp, #32]
    ldp x23, x24, [sp, #48]
    ldp x25, x26, [sp, #64]
    ldp x27, x28, [sp, #80]
    ldp x29, x30, [sp], #96

    ret

.globl cpu_wfe_stateless
.type cpu_wfe_stateless, @function
cpu_wfe_stateless:
    stp x29, x30, [sp, #-96]!
    mov x29, sp
    stp x19, x20, [sp, #16]
    stp x21, x22, [sp, #32]
    stp x23, x24, [sp, #48]
    stp x25, x26, [sp, #64]
    stp x27, x28, [sp, #80]

    movz x19, #0x5a5a, lsl #48
    movk x19, #0xa5a5, lsl #32

    dsb sy
    isb
    wfe
    isb

    movz x1, #0x5a5a, lsl #48
    movk x1, #0xa5a5, lsl #32
    cmp x19, x1
    cset x0, ne

    ldp x19, x20, [sp, #16]
    ldp x21, x22, [sp, #32]
    ldp x23, x24, [sp, #48]
    ldp x25, x26, [sp, #64]
    ldp x27, x28, [sp, #80]
    ldp x29, x30, [sp], #96

    ret
