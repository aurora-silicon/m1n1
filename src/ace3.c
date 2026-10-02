/* SPDX-License-Identifier: MIT */

#include "ace3.h"
#include "adt.h"
#include "spmi.h"
#include "string.h"
#include "types.h"
#include "utils.h"

/*
 * ACE3 SPMI transport, per
 * OtherResources/re-work/asahi-docs/docs/hw/peripherals/ace3.md:
 *
 *   0x00        logical register select -- write 0x80 | reg, MSB self-clears
 *   0x1F  [RO]  size in bytes of the selected logical register
 *   0x20..0x5F  data of the selected logical register
 *
 * Only the first 0x60 SPMI addresses are mapped.  Selecting a register makes
 * the hardware refill the data window with that register's current contents,
 * so a partial write commits the untouched bytes unchanged.
 */
#define ACE3_SPMI_SELECT   0x00
#define ACE3_SPMI_SIZE     0x1f
#define ACE3_SPMI_DATA     0x20
#define ACE3_SPMI_DATA_MAX 0x40
#define ACE3_SPMI_BURST    16 /* SPMI extended transfers cap at 16 bytes */
#define ACE3_SELECT_BUSY   BIT(7)

#define ACE3_SELECT_TIMEOUT_US 20000
#define ACE3_SELECT_POLL_US    50


static size_t ace3_chunk(size_t left)
{
    return left > ACE3_SPMI_BURST ? ACE3_SPMI_BURST : left;
}

/*
 * Select a logical register and wait for the hardware to finish the transfer.
 *
 * The "register 0 write" SPMI command is used rather than a normal write: the
 * ACE3 doc notes selections made this way bypass the per-slave selection cache,
 * and that a cached selection can return stale data -- notably making a command
 * written to CMD1 appear never to complete.
 */
static int ace3_select(spmi_dev_t *dev, u8 sid, u8 lreg)
{
    if (lreg & ACE3_SELECT_BUSY) {
        printf("ace3: logical register %#x does not fit in a reg-0 write\n", lreg);
        return -1;
    }

    if (spmi_reg0_write(dev, sid, lreg) < 0)
        return -1;

    u64 timeout = timeout_calculate(ACE3_SELECT_TIMEOUT_US);
    for (;;) {
        u8 sel;
        if (spmi_ext_read(dev, sid, ACE3_SPMI_SELECT, &sel, 1) < 0)
            return -1;
        if (!(sel & ACE3_SELECT_BUSY))
            return 0;
        if (timeout_expired(timeout)) {
            printf("ace3: sid %u: selection of %#x never completed (sel %#x)\n", sid, lreg, sel);
            return -1;
        }
        udelay(ACE3_SELECT_POLL_US);
    }
}

/* Size of the currently selected logical register.  0 means "not implemented". */
static int ace3_selected_size(spmi_dev_t *dev, u8 sid, u8 *size)
{
    return spmi_ext_read(dev, sid, ACE3_SPMI_SIZE, size, 1);
}

int ace3_read(spmi_dev_t *dev, u8 sid, u8 lreg, u8 *bfr, size_t len)
{
    if (!dev || !bfr || !len || len > ACE3_SPMI_DATA_MAX)
        return -1;

    if (ace3_select(dev, sid, lreg) < 0)
        return -1;

    u8 size = 0;
    if (ace3_selected_size(dev, sid, &size) < 0)
        return -1;
    if (!size) {
        printf("ace3: sid %u: logical register %#x is not implemented\n", sid, lreg);
        return -1;
    }
    if (len > size)
        len = size;

    for (size_t done = 0; done < len;) {
        size_t chunk = ace3_chunk(len - done);
        if (spmi_ext_read(dev, sid, ACE3_SPMI_DATA + done, bfr + done, chunk) < 0)
            return -1;
        done += chunk;
    }

    return (int)len;
}

