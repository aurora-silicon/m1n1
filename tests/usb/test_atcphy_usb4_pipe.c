/* SPDX-License-Identifier: MIT */
#include <assert.h>
#include <stdio.h>

#include "atcphy_core.h"

int main(void)
{
    size_t count = 0;
    const atcphy_seq_op_t *ops = atcphy_seq_pipehandler_usb4_routed(&count);
    assert(ops != NULL);
    /* Apple's setUSB3Mode USB4 branch: 2 lead-in overrides, lock req + ack
     * poll, three back-to-back MUX_CTRL writes (NO settle delays), two
     * override releases, unlock req + ack poll. Ten ops, no NONSELECTED
     * (+0x20) write. */
    assert(count == 10);

    /* Lock must precede the first mux write; unlock follows the last. */
    assert(ops[2].kind == ATCPHY_OP_SET);
    assert(ops[2].offset == ATCPHY_PIPEHANDLER_LOCK_REQ);
    assert(ops[3].kind == ATCPHY_OP_POLL_SET);
    /* Apple's routed lock-ACK budget is 6 ms, distinct from atc.c's 1 ms. */
    assert(ops[3].arg2 == ATCPHY_PIPEHANDLER_LOCK_ACK_ROUTED_TIMEOUT_US);

    /* The three mux writes are consecutive: CLK_OFF, DATA_USB4, CLK_USB4, with
     * no ATCPHY_OP_DELAY_US between them. */
    assert(ops[4].offset == ATCPHY_PIPEHANDLER_MUX_CTRL && ops[4].arg2 == 0);
    assert(ops[5].offset == ATCPHY_PIPEHANDLER_MUX_CTRL);
    assert(ops[5].arg2 == ATCPHY_PIPEHANDLER_MUX_DATA_USB4);
    assert(ops[6].offset == ATCPHY_PIPEHANDLER_MUX_CTRL);
    assert(ops[6].arg2 == (ATCPHY_PIPEHANDLER_MUX_CLK_USB4 <<
                           ATCPHY_PIPEHANDLER_MUX_CLK_SHIFT));
    for (size_t i = 4; i <= 6; i++)
        assert(ops[i].kind == ATCPHY_OP_MASK);

    assert(ops[8].kind == ATCPHY_OP_CLEAR);
    assert(ops[8].offset == ATCPHY_PIPEHANDLER_LOCK_REQ);
    assert(ops[9].kind == ATCPHY_OP_POLL_CLEAR);

    /* No op in the routed list may touch NONSELECTED_OVERRIDE (+0x20): Apple
     * does not write it in the USB4 branch. */
    for (size_t i = 0; i < count; i++)
        assert(ops[i].offset != ATCPHY_PIPEHANDLER_NONSELECTED_OVERRIDE);

    u32 mux = ATCPHY_PIPEHANDLER_MUX_VALUE_DUMMY;
    for (size_t i = 0; i < count; i++) {
        if (ops[i].offset != ATCPHY_PIPEHANDLER_MUX_CTRL ||
            ops[i].kind != ATCPHY_OP_MASK)
            continue;
        mux = (mux & ~ops[i].arg1) | ops[i].arg2;
    }
    assert(mux == ATCPHY_PIPEHANDLER_MUX_VALUE_USB4_TUNNEL);
    assert(atcphy_pipe_mux_decode(mux) == ATCPHY_PIPE_BACKEND_USB4);

    puts("ATCPHY routed USB4 PIPE tests: PASS");
    return 0;
}
