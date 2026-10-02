identity:
    mrs x1, mpidr_el1
    str x1, [x0]
    mrs x1, midr_el1
    str x1, [x0, #8]
    mrs x1, CurrentEL
    str x1, [x0, #16]
    mrs x1, sctlr_el2
    str x1, [x0, #24]
    mrs x1, tcr_el2
    str x1, [x0, #32]
    mrs x1, tpidr_el2
    str x1, [x0, #40]
    ret
math:
    madd x0, x0, x1, x2
    eor x0, x0, x3
    ret
ticks:
    mrs x0, cntpct_el0
    ret
frequency:
    mrs x0, cntfrq_el0
    ret
work:
    isb
    mrs x3, cntpct_el0
    str x3, [x1]
    mov x2, #0
1:
    add x2, x2, #1
    .rept 31
    add x4, x4, #1
    .endr
    cmp x2, x0
    b.lo 1b
    isb
    mrs x3, cntpct_el0
    str x3, [x1, #8]
    mov x0, x2
    ret
atomic:
    mrs x10, cntpct_el0
    cmp x10, x2
    b.lo atomic
    str x10, [x3]
    mov x6, #0
1:
    ldaxr x4, [x0]
    add x4, x4, #1
    stlxr w5, x4, [x0]
    cbnz w5, 1b
    add x6, x6, #1
    cmp x6, x1
    b.lo 1b
    mrs x10, cntpct_el0
    str x10, [x3, #8]
    mov x0, x6
    ret
