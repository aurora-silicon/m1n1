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
#define ACE3_CMD_TIMEOUT_US    500000
#define ACE3_WAKE_TIMEOUT_US   500000
#define ACE3_SELECT_POLL_US    50
#define ACE3_CMD_POLL_US       100
#define ACE3_WAKE_POLL_US      2000

/* CMD1 reads back as "!CMD" when the controller does not know the 4CC. */
#define ACE3_CMD_INVALID "!CMD"

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

/*
 * Wake the AP-side slave and wait until it actually answers.
 *
 * A sleeping ACE3 ACKs SPMI commands and silently discards writes, and reads
 * come back as zeros -- which surfaces as "logical register 0x20 is not
 * implemented", because the size byte reads 0 like everything else.  The wake
 * is not instant: measured on J813, sending the wakeup and reading immediately
 * (as this function first did) fails on every cold boot, while it appears to
 * work after a chainload only because the controller was already awake.
 *
 * Poll for a real answer rather than sleeping a guessed interval: a non-zero
 * size for MODE, which every ACE3 implements, means the register interface is
 * live.  Deliberately quiet -- the probe fails by design until the part wakes.
 */
static bool ace3_awake(spmi_dev_t *dev, u8 sid)
{
    if (ace3_select(dev, sid, ACE3_REG_MODE) < 0)
        return false;

    u8 size = 0;
    if (ace3_selected_size(dev, sid, &size) < 0)
        return false;

    return size != 0;
}

static int ace3_wake(spmi_dev_t *dev, u8 sid)
{
    if (spmi_send_wakeup(dev, sid) < 0)
        return -1;

    u64 timeout = timeout_calculate(ACE3_WAKE_TIMEOUT_US);
    for (;;) {
        if (ace3_awake(dev, sid))
            return 0;
        if (timeout_expired(timeout))
            return -1;
        udelay(ACE3_WAKE_POLL_US);
    }
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

int ace3_write(spmi_dev_t *dev, u8 sid, u8 lreg, const u8 *bfr, size_t len)
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
        if (spmi_ext_write(dev, sid, ACE3_SPMI_DATA + done, bfr + done, chunk) < 0)
            return -1;
        done += chunk;
    }

    return (int)len;
}

int ace3_exec(spmi_dev_t *dev, u8 sid, const char cmd[4], const u8 *in, size_t in_len, u8 *rc)
{
    if (!dev || !cmd)
        return -1;

    if (in_len && ace3_write(dev, sid, ACE3_REG_DATA1, in, in_len) < 0)
        return -1;

    /*
     * The 4CC goes on the wire in literal order.  macOS builds SSPS as the
     * constant 0x53505353, whose little-endian in-memory bytes are 'S','S','P',
     * 'S' -- the same order Linux's tipd writes -- so the characters are passed
     * straight through rather than byte-swapped.
     */
    u8 payload[8] = {0};
    memcpy(payload, cmd, 4);
    if (ace3_write(dev, sid, ACE3_REG_CMD1, payload, sizeof(payload)) < 0)
        return -1;

    u64 timeout = timeout_calculate(ACE3_CMD_TIMEOUT_US);
    for (;;) {
        u8 status[4] = {0};
        if (ace3_read(dev, sid, ACE3_REG_CMD1, status, sizeof(status)) < 0)
            return -1;
        if (!memcmp(status, ACE3_CMD_INVALID, sizeof(status))) {
            printf("ace3: sid %u: command %.4s not recognised (!CMD)\n", sid, cmd);
            return -1;
        }
        if (!status[0] && !status[1] && !status[2] && !status[3])
            break;
        if (timeout_expired(timeout)) {
            printf("ace3: sid %u: command %.4s timed out\n", sid, cmd);
            return -1;
        }
        udelay(ACE3_CMD_POLL_US);
    }

    if (rc) {
        u8 data = 0;
        if (ace3_read(dev, sid, ACE3_REG_DATA1, &data, 1) < 0)
            return -1;
        *rc = data;
    }

    return 0;
}

int ace3_set_system_power_state(spmi_dev_t *dev, u8 sid, u8 state)
{
    u8 current = 0;

    if (ace3_read(dev, sid, ACE3_REG_SYSTEM_POWER_STATE, &current, 1) < 0)
        return -1;

    /*
     * Skip the command when the controller already agrees, mirroring macOS'
     * AppleHPMInterface::setPowerStateHPM, which reads this register first and
     * only issues SSPS when it differs.  Say so rather than returning quietly:
     * a chainload leaves the ACE3 running, so an already-correct port is the
     * normal case on a warm restart and an unexplained gap in the boot log
     * reads like a port that was never visited at all.
     */
    if (current == state) {
        printf("ace3: sid %u: system power state already %u\n", sid, state);
        return 0;
    }

    u8 rc = 0xff;
    if (ace3_exec(dev, sid, "SSPS", &state, 1, &rc) < 0)
        return -1;
    if (rc != ACE3_TASK_SUCCESS) {
        printf("ace3: sid %u: SSPS(%u) returned %u\n", sid, state, rc);
        return -1;
    }

    u8 readback = 0;
    if (ace3_read(dev, sid, ACE3_REG_SYSTEM_POWER_STATE, &readback, 1) < 0)
        return -1;
    if (readback != state) {
        printf("ace3: sid %u: SSPS(%u) did not stick (still %u)\n", sid, state, readback);
        return -1;
    }

    printf("ace3: sid %u: system power state %u -> %u\n", sid, current, state);
    return 0;
}

int ace3_power_on_ports(const char *spmi_path)
{
    int bus = adt_path_offset(adt, spmi_path);
    if (bus < 0)
        return 0;

    spmi_dev_t *spmi = spmi_init(spmi_path);
    if (!spmi) {
        printf("ace3: spmi init failed for %s\n", spmi_path);
        return 0;
    }

    int done = 0;

    ADT_FOREACH_CHILD(adt, bus)
    {
        const char *name = adt_get_name(adt, bus);
        if (!name || memcmp(name, "hpm", 3))
            continue;

        u32 len = 0;
        const u32 *reg = adt_getprop(adt, bus, "reg", &len);
        if (!reg || len < sizeof(u32)) {
            printf("ace3: %s has no usable reg property\n", name);
            continue;
        }

        /*
         * reg word 0 is the SPMI slave address.  It is always even: the ACE3
         * exposes a second slave at address+1 for the SMC, whose selection and
         * interrupt state must not be disturbed.
         */
        u8 sid = reg[0] & 0xf;

        /*
         * The AP-side slave is asleep at boot, and while asleep SPMI register
         * writes are ACKed and silently ignored -- so every access below would
         * read back as zero without this.
         */
        if (ace3_wake(spmi, sid) < 0) {
            printf("ace3: %s (sid %u): never woke up\n", name, sid);
            continue;
        }

        /*
         * All three hpm nodes on J813 -- both Type-C ports and MagSafe --
         * implement SYSTEM_POWER_STATE and accept S0.  An earlier version of
         * this code reported MagSafe as not implementing it; that was the wake
         * race above reading zeros, not a property of the part.  Treat a
         * failure here as a real failure of that port, not as an expected quirk.
         */
        if (ace3_set_system_power_state(spmi, sid, ACE3_SYSTEM_POWER_STATE_S0) < 0) {
            printf("ace3: %s (sid %u): could not enter S0; port will not source\n", name, sid);
            continue;
        }

        done++;
    }

    spmi_shutdown(spmi);
    return done;
}
