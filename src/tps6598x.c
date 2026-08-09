/* SPDX-License-Identifier: MIT */

#include "tps6598x.h"
#include "adt.h"
#include "i2c.h"
#include "iodev.h"
#include "malloc.h"
#include "string.h"
#include "tps6598x_host_policy.h"
#include "types.h"
#include "utils.h"

#define TPS_REG_MODE          0x03
#define TPS_REG_CMD1          0x08
#define TPS_REG_DATA1         0x09
#define TPS_REG_INT_EVENT1    0x14
#define TPS_REG_INT_MASK1     0x16
#define TPS_REG_INT_CLEAR1    0x18
#define TPS_REG_POWER_STATE   0x20
#define TPS_REG_SYSTEM_CONFIG 0x28
/* "!CMD" as it appears after i2c_smbus_read32's little-endian assembly.
 * This was 0x21434d44 (big-endian byte order), which never matches, so a
 * rejected 4CC was invisible and every rejection burned the full command
 * timeout and then reported "timed out" instead of "rejected".  Upstream
 * m1n1 fixed the same bug in commit bc8c54c4. */
#define TPS_CMD_INVALID       0x444d4321 // !CMD
#define TPS_CMD_TIMEOUT_US    (5 * 1000 * 1000)
#define TPS_MODE_DBMA         ((u32)'D' | ((u32)'B' << 8) | ((u32)'M' << 16) | ((u32)'a' << 24))

struct tps6598x_dev {
    i2c_dev_t *i2c;
    u8 addr;
};

tps6598x_dev_t *tps6598x_init(const char *adt_node, i2c_dev_t *i2c)
{
    int adt_offset;
    adt_offset = adt_path_offset(adt, adt_node);
    if (adt_offset < 0) {
        printf("tps6598x: Error getting %s node\n", adt_node);
        return NULL;
    }

    const u8 *iic_addr = adt_getprop(adt, adt_offset, "hpm-iic-addr", NULL);
    if (iic_addr == NULL) {
        printf("tps6598x: Error getting %s hpm-iic-addr\n.", adt_node);
        return NULL;
    }

    tps6598x_dev_t *dev = calloc(1, sizeof(*dev));
    if (!dev)
        return NULL;

    dev->i2c = i2c;
    dev->addr = *iic_addr;
    return dev;
}

void tps6598x_shutdown(tps6598x_dev_t *dev)
{
    free(dev);
}

int tps6598x_command(tps6598x_dev_t *dev, const char *cmd, const u8 *data_in, size_t len_in,
                     u8 *data_out, size_t len_out)
{
    if (len_in) {
        if (i2c_smbus_write(dev->i2c, dev->addr, TPS_REG_DATA1, data_in, len_in) < 0)
            return -1;
    }

    if (i2c_smbus_write(dev->i2c, dev->addr, TPS_REG_CMD1, (const u8 *)cmd, 4) < 0)
        return -1;

    u32 cmd_status;
    u64 timeout = timeout_calculate(TPS_CMD_TIMEOUT_US);
    do {
        if (i2c_smbus_read32(dev->i2c, dev->addr, TPS_REG_CMD1, &cmd_status))
            return -1;
        if (cmd_status == TPS_CMD_INVALID)
            return -1;
        if (cmd_status == 0)
            break;
        if (timeout_expired(timeout)) {
            printf("tps6598x: command %.4s timed out\n", cmd);
            return -1;
        }
        udelay(100);
    } while (true);

    if (len_out) {
        if (i2c_smbus_read(dev->i2c, dev->addr, TPS_REG_DATA1, data_out, len_out) !=
            (ssize_t)len_out)
            return -1;
    }

    return 0;
}

int tps6598x_cmd_status(tps6598x_dev_t *dev, const char *cmd)
{
    u32 cmd_status;

    if (i2c_smbus_read32(dev->i2c, dev->addr, TPS_REG_CMD1, &cmd_status)) {
        printf("tps6598x: i2c_smbus_read32 cmd: %s failed\n", cmd);
        return -1;
    }
    if (cmd_status == TPS_CMD_INVALID) {
        printf("tps6598x: i2c_smbus_read32 cmd: %s status invalid\n", cmd);
        return -1;
    }
    if (cmd_status) {
        printf("tps6598x: i2c_smbus_read32 cmd: %s status 0x%x\n", cmd, cmd_status);
        return -1;
    }

    return 0;
}

int tps6598x_disable_irqs(tps6598x_dev_t *dev, tps6598x_irq_state_t *state)
{
    size_t read;
    int written;
    static const u8 zeros[CD3218B12_IRQ_WIDTH] = {0x00};
    static const u8 ones[CD3218B12_IRQ_WIDTH] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF,
                                                 0xFF, 0xFF, 0xFF, 0xFF};

    // store IntEvent 1 to restore it later
    read = i2c_smbus_read(dev->i2c, dev->addr, TPS_REG_INT_MASK1, state->int_mask1,
                          sizeof(state->int_mask1));
    if (read != CD3218B12_IRQ_WIDTH) {
        printf("tps6598x: reading TPS_REG_INT_MASK1 failed\n");
        return -1;
    }
    state->valid = 1;

    // mask interrupts and ack all interrupt flags
    written = i2c_smbus_write(dev->i2c, dev->addr, TPS_REG_INT_CLEAR1, ones, sizeof(ones));
    if (written != sizeof(zeros)) {
        printf("tps6598x: writing TPS_REG_INT_CLEAR1 failed, written: %d\n", written);
        return -1;
    }
    written = i2c_smbus_write(dev->i2c, dev->addr, TPS_REG_INT_MASK1, zeros, sizeof(zeros));
    if (written != sizeof(ones)) {
        printf("tps6598x: writing TPS_REG_INT_MASK1 failed, written: %d\n", written);
        return -1;
    }

#ifdef DEBUG
    u8 tmp[CD3218B12_IRQ_WIDTH] = {0x00};
    read = i2c_smbus_read(dev->i2c, dev->addr, TPS_REG_INT_MASK1, tmp, CD3218B12_IRQ_WIDTH);
    if (read != CD3218B12_IRQ_WIDTH)
        printf("tps6598x: failed verification, can't read TPS_REG_INT_MASK1\n");
    else {
        printf("tps6598x: verify: TPS_REG_INT_MASK1 vs. saved IntMask1\n");
        hexdump(tmp, sizeof(tmp));
        hexdump(state->int_mask1, sizeof(state->int_mask1));
    }
#endif
    return 0;
}

int tps6598x_restore_irqs(tps6598x_dev_t *dev, tps6598x_irq_state_t *state)
{
    int written;

    written = i2c_smbus_write(dev->i2c, dev->addr, TPS_REG_INT_MASK1, state->int_mask1,
                              sizeof(state->int_mask1));
    if (written != sizeof(state->int_mask1)) {
        printf("tps6598x: restoring TPS_REG_INT_MASK1 failed\n");
        return -1;
    }

#ifdef DEBUG
    int read;
    u8 tmp[CD3218B12_IRQ_WIDTH];
    read = i2c_smbus_read(dev->i2c, dev->addr, TPS_REG_INT_MASK1, tmp, sizeof(tmp));
    if (read != sizeof(tmp))
        printf("tps6598x: failed verification, can't read TPS_REG_INT_MASK1\n");
    else {
        printf("tps6598x: verify saved IntMask1 vs. TPS_REG_INT_MASK1:\n");
        hexdump(state->int_mask1, sizeof(state->int_mask1));
        hexdump(tmp, sizeof(tmp));
    }
#endif

    return 0;
}

u8 tps6598x_i2c_addr(const tps6598x_dev_t *dev)
{
    if (!dev)
        return 0;
    return dev->addr;
}

int tps6598x_read_status(tps6598x_dev_t *dev, u32 *status)
{
    /*
     * STATUS is 8 bytes on this family, so ask for exactly that: a
     * length-matched SMBus block read is what every other read in this
     * file does, and it keeps i2c_smbus_read() from logging a
     * length-mismatch note on every call. Short replies are tolerated
     * because only byte 0 carries the bits anyone here cares about --
     * a wrong register number would show up as a nonsense decode, not as
     * a silent success.
     */
    u8 buf[8] = {0};

    if (!dev || !status)
        return -1;

    int ret = i2c_smbus_read(dev->i2c, dev->addr, TPS6598X_REG_STATUS, buf, sizeof(buf));
    if (ret < 1) {
        printf("tps6598x: STATUS read from addr %#x failed (ret=%d)\n", dev->addr, ret);
        return -1;
    }
    if (ret != (int)sizeof(buf))
        printf("tps6598x: addr %#x returned %d STATUS bytes, expected %zu (using byte 0)\n",
               dev->addr, ret, sizeof(buf));

    u32 value = 0;
    for (int i = 0; i < ret && i < 4; i++)
        value |= (u32)buf[i] << (8 * i);

    *status = value;
    return 0;
}

int tps6598x_read_link_state(tps6598x_dev_t *dev, tps6598x_link_state_t *state)
{
    tps6598x_link_state_t value = {0};

    if (!dev || !state)
        return -1;
    if (tps6598x_read_status(dev, &value.status) < 0)
        return -1;
    if (i2c_smbus_read32(dev->i2c, dev->addr, TPS6598X_REG_DATA_STATUS,
                         &value.data_status)) {
        printf("tps6598x: DATA_STATUS read from addr %#x failed\n", dev->addr);
        return -1;
    }
    if (value.data_status & TPS6598X_DATA_USB4_CONNECTION) {
        u8 usb4[9];
        int ret = i2c_smbus_read(dev->i2c, dev->addr, TPS6598X_REG_USB4_STATUS,
                                 usb4, sizeof(usb4));
        if (ret != (int)sizeof(usb4)) {
            printf("tps6598x: USB4_STATUS read from addr %#x failed (ret=%d)\n",
                   dev->addr, ret);
            return -1;
        }
        value.usb4_mode_status = usb4[0];
        value.usb4_eudo = (u32)usb4[1] | ((u32)usb4[2] << 8) |
                          ((u32)usb4[3] << 16) | ((u32)usb4[4] << 24);
        value.usb4_unknown = (u32)usb4[5] | ((u32)usb4[6] << 8) |
                             ((u32)usb4[7] << 16) | ((u32)usb4[8] << 24);
        /* PRESENT and REVERSE are entailed by registers we actually read:
         * USB4_CONNECTION is set (we are inside that branch), and the
         * orientation bit comes straight from STATUS.
         *
         * ACTIVE is NOT. It used to be OR-ed in unconditionally, right next to
         * a genuinely measured bit, which made a fabricated value inherit the
         * provenance of its neighbour -- a passive 40 Gb/s USB4 cable sets the
         * same USB4_CONNECTION bit and would have been reported as active.
         * Nothing here distinguishes active from passive, so nothing here
         * claims to: the bit is left clear and its absence means "not
         * determined", not "passive". A consumer that needs it must decode the
         * cable VDO rather than read this word. */
        value.apple_cable_info = TPS6598X_APPLE_CABLE_PRESENT;
        if (value.status & TPS6598X_STATUS_PLUG_UPSIDE_DOWN)
            value.apple_cable_info |= TPS6598X_APPLE_CABLE_REVERSE;
    }

    *state = value;
    return 0;
}

/* Apple waits for the HPM to wake before its first access
 * (HALGenericACIO::waitForAppleHPMWake); m1n1 issued a single read and gave
 * up on the first NAK.  Measured on J414s: at boot every one of hpm0/1/2/5
 * failed identically with `i2c: timeout while reading (got 0, expected 1
 * bytes)`, yet the very same registers read cleanly from the proxy seconds
 * later -- MODE returning ASCII "APP " on all three.  Four independent PD
 * chips do not go deaf simultaneously and then recover; they were simply not
 * awake yet.
 *
 * Because powerup is the FIRST access, its failure cascades: hpm_init()
 * returns NULL, tps6598x_disable_irqs() never runs, and m1n1 ends up writing
 * zero bytes to any CD3217 and never telling them the host is in S0.
 *
 * The retry is bounded and costs nothing in the common case -- a controller
 * that answers immediately takes the first branch with no delay.  The
 * attempt count is logged on success so the real settle time becomes an
 * observation rather than a guess. */
#define TPS6598X_WAKE_ATTEMPTS    10
#define TPS6598X_WAKE_INTERVAL_US 5000

/* The attempt count is printed on EVERY call, including the zero-retry case.
 * Printing only when `attempt` is nonzero makes "answered immediately" and
 * "this controller was never probed at all" produce identical (empty) log
 * output -- the absence of a retry line CORRELATES with a fast wake but does
 * not IDENTIFY one.  The count and elapsed time are also the measurement that
 * decides whether the 50 ms ceiling is too generous or too tight, and that
 * decision cannot be made from a line that only appears on the slow path. */
static int tps6598x_read_power_state_waking(tps6598x_dev_t *dev, u8 *power_state)
{
    for (u32 attempt = 0; attempt < TPS6598X_WAKE_ATTEMPTS; attempt++) {
        if (!i2c_smbus_read8(dev->i2c, dev->addr, TPS_REG_POWER_STATE, power_state)) {
            printf("tps6598x: addr %#x woke on attempt %u/%u (%u us), "
                   "power_state=%#x\n",
                   dev->addr, attempt + 1, TPS6598X_WAKE_ATTEMPTS,
                   attempt * TPS6598X_WAKE_INTERVAL_US, *power_state);
            return 0;
        }
        udelay(TPS6598X_WAKE_INTERVAL_US);
    }
    printf("tps6598x: addr %#x did not wake within %u us (%u attempts)\n",
           dev->addr, TPS6598X_WAKE_ATTEMPTS * TPS6598X_WAKE_INTERVAL_US,
           TPS6598X_WAKE_ATTEMPTS);
    return -1;
}

int tps6598x_powerup(tps6598x_dev_t *dev)
{
    u8 power_state;

    if (tps6598x_read_power_state_waking(dev, &power_state) < 0)
        return -1;

    if (power_state == 0)
        return 0;

    /* Tell the controller the host is in S0.  A CD3217 that is never told
     * this can drop an established contract and stop presenting a
     * connection, which reads downstream as "the port is empty". */
    const u8 data = 0;
    if (tps6598x_command(dev, "SSPS", &data, 1, NULL, 0) < 0) {
        printf("tps6598x: addr %#x rejected SSPS (was power_state=%#x)\n",
               dev->addr, power_state);
        return -1;
    }

    /* Poll for the state to settle rather than sampling once.  The original
     * single read failed for two different reasons that it could not tell
     * apart: an I2C NAK while the controller processes the command, and a
     * state that is simply not 0 yet.  Both returned -1 with no message, so
     * `usb: tps6598x_powerup failed` never said which step lost. */
    for (u32 attempt = 0; attempt < TPS6598X_WAKE_ATTEMPTS; attempt++) {
        u8 settled;
        if (!i2c_smbus_read8(dev->i2c, dev->addr, TPS_REG_POWER_STATE, &settled)) {
            if (settled == 0) {
                printf("tps6598x: addr %#x reached S0 after SSPS on attempt "
                       "%u/%u (%u us)\n",
                       dev->addr, attempt + 1, TPS6598X_WAKE_ATTEMPTS,
                       attempt * TPS6598X_WAKE_INTERVAL_US);
                return 0;
            }
            power_state = settled;
        }
        udelay(TPS6598X_WAKE_INTERVAL_US);
    }

    printf("tps6598x: addr %#x still not in S0 %u us after SSPS "
           "(last power_state=%#x)\n",
           dev->addr, TPS6598X_WAKE_ATTEMPTS * TPS6598X_WAKE_INTERVAL_US,
           power_state);
    return -1;
}

/* Type-C attach detection is not complete when the chip starts answering I2C.
 *
 * Measured on this target: the SSD and the Ethernet adapter were physically
 * attached the whole time, yet m1n1 read PLUG_PRESENT clear on BOTH ports at
 * boot, while a probe taken later on the same machine read both populated.
 * The CD3217 answers the bus before it has finished CC debounce, so sampling
 * STATUS once, immediately after wake, samples it too early.
 *
 * The budget is a BOUND, not a measurement -- nobody has measured what this
 * chip actually needs. That is why every call logs the attempt count and the
 * elapsed time on both outcomes: the log is how we learn the real number
 * instead of keeping a guess that happened to work.
 */
#define TPS6598X_PLUG_SETTLE_ATTEMPTS    100
#define TPS6598X_PLUG_SETTLE_INTERVAL_US 5000

static bool tps6598x_wait_plug_settled(tps6598x_dev_t *dev, u32 hpm_index, u32 *status)
{
    for (u32 attempt = 0; attempt < TPS6598X_PLUG_SETTLE_ATTEMPTS; attempt++) {
        if (tps6598x_read_status(dev, status) < 0) {
            printf("tps6598x: hpm%u STATUS unreadable while waiting for plug "
                   "detection (attempt %u/%u)\n",
                   hpm_index, attempt + 1, TPS6598X_PLUG_SETTLE_ATTEMPTS);
            return false;
        }
        if (*status & TPS6598X_STATUS_PLUG_PRESENT) {
            printf("tps6598x: hpm%u plug detected on attempt %u/%u (%u us), "
                   "STATUS=%#x\n",
                   hpm_index, attempt + 1, TPS6598X_PLUG_SETTLE_ATTEMPTS,
                   attempt * TPS6598X_PLUG_SETTLE_INTERVAL_US, *status);
            return true;
        }
        udelay(TPS6598X_PLUG_SETTLE_INTERVAL_US);
    }
    printf("tps6598x: hpm%u no plug after %u us (%u attempts), STATUS=%#x; "
           "treating the port as EMPTY, not as unconfigured\n",
           hpm_index,
           TPS6598X_PLUG_SETTLE_ATTEMPTS * TPS6598X_PLUG_SETTLE_INTERVAL_US,
           TPS6598X_PLUG_SETTLE_ATTEMPTS, *status);
    return false;
}

int tps6598x_prepare_host(tps6598x_dev_t *dev, u32 hpm_index, u32 controller_count,
                          s32 preserved_index)
{
    u32 status;
    u8 current[TPS6598X_SYSTEM_CONFIG_LEN];
    u8 desired[TPS6598X_SYSTEM_CONFIG_LEN];
    u8 readback[TPS6598X_SYSTEM_CONFIG_LEN];

    if (preserved_index >= 0 && hpm_index == (u32)preserved_index)
        return 0;

    status = 0;
    bool plug_settled = tps6598x_wait_plug_settled(dev, hpm_index, &status);
    if (!plug_settled && !status) {
        printf("tps6598x: hpm%u cannot safely determine live Type-C role\n", hpm_index);
        return -1;
    }

    switch (tps6598x_host_policy_port_action(status, plug_settled)) {
        case TPS6598X_HOST_PORT_PRESERVE:
            printf("tps6598x: hpm%u preserving attached Source/DFP contract "
                   "(STATUS=%#x)\n", hpm_index, status);
            return 0;
        case TPS6598X_HOST_PORT_EMPTY:
            /* Nothing attached. Deliberately NOT a SYSTEM_CONFIG rewrite: an
             * empty port is not an unconfigured port, and this machine has
             * already rejected such a rewrite once. Returning success because
             * there is genuinely nothing to prepare -- a port with no cable is
             * not a failure to report. */
            printf("tps6598x: hpm%u empty; leaving System Configuration "
                   "untouched\n", hpm_index);
            return 0;
        case TPS6598X_HOST_PORT_WRONG_ROLE:
            printf("tps6598x: hpm%u attached in non-host role (STATUS=%#x); "
                   "refusing rewrite\n", hpm_index, status);
            return -1;
        default:
            return -1;
    }

    /* The SYSTEM_CONFIG rewrite that used to live here is deleted, not
     * disabled, because after the change above it was unreachable in every
     * legitimate case:
     *
     *   attached + Source/DFP -> preserved (never rewrote)
     *   attached + wrong role -> refused   (never rewrote, by design)
     *   nothing attached      -> EMPTY     (the only route that ever reached
     *                                       the rewrite, and it reached it by
     *                                       reading "no cable" as "port needs
     *                                       configuring")
     *
     * So every SYSTEM_CONFIG write this driver has ever attempted on this
     * machine was issued against a port it could not see a cable on, and the
     * chip rejected all of them. Leaving the block in place as dead code would
     * present a live-looking "prepare" path that nothing can call.
     *
     * `tps6598x_host_policy_prepare`/`_verify` are kept and still host-tested,
     * for a future caller with a PROVEN need to force a role. Reintroducing a
     * call here needs evidence that a rewrite is required and accepted, not
     * just that a port looked idle.
     */
    (void)controller_count;
    (void)preserved_index;
    (void)current;
    (void)desired;
    (void)readback;
    return 0;
}

int tps6598x_enter_kis(tps6598x_dev_t *dev)
{
    u32 target_len = 0;
    const u8 *target = adt_getprop(adt, 0, "target-type", &target_len);
    u8 key[4] = {0};
    const u8 key_null[4] = {0, 0, 0, 0};
    const u8 vdm[] = {0x06, 0x46, 0x82, 0x01};
    u32 mode = 0;
    u8 out = 0;
    u8 en = 1;
    int ret;

    if (target_len < 4)
        return -1;

    // reverse the key
    for (int i = 0; i < 4; i++)
        key[i] = target[3 - i];

    // check status and soft reset if it fails
    if (tps6598x_cmd_status(dev, "LOCK")) {
        tps6598x_command(dev, "Gaid", NULL, 0, NULL, 0);
        mdelay(20);
    }

    ret = tps6598x_command(dev, "LOCK", key, 4, (u8 *)&out, 1);
    if (ret || (out & 0xf)) {
        printf("tps6598x_enter_kis: Failed to unlock using '%.4s': ret=%d result=0x%hhx\n", key,
               ret, out & 0xf);
        return -1;
    }

    ret = tps6598x_command(dev, "DBMa", &en, 1, &out, 1);
    if (ret || (out & 0xf)) {
        printf("tps6598x_enter_kis: DBMa cmd failed: ret=%d result=0x%hhx\n", ret, out & 0xf);
        return -1;
    }

    ret = i2c_smbus_read32(dev->i2c, dev->addr, TPS_REG_MODE, &mode);
    if (mode != TPS_MODE_DBMA) {
        printf("tps6598x_enter_kis: Failed to enter DBMa mode, mode=0x%08x\n", mode);
        return -1;
    }

    ret = tps6598x_command(dev, "DVEn", vdm, sizeof(vdm), &out, 1);
    if (ret || (out & 0xf)) {
        printf("tps6598x_enter_kis: DVEn cmd failed: ret=%d result=0x%hhx\n", ret, out & 0xf);
        return -1;
    }

    en = 0;
    tps6598x_command(dev, "DBMa", &en, 1, NULL, 0);
    tps6598x_command(dev, "LOCK", key_null, 4, NULL, 0);

    return ret;
}

int tps6598x_enable_debugusb(void)
{
    char hpm_path[64] = {0};
    char i2c_path[64] = {0};
    bool found = false;
    int node;
    int ret;

    node = adt_path_offset(adt, "/arm-io");
    if (node < 0)
        return -1;

    ADT_FOREACH_CHILD(adt, node)
    {
        int mngr_node;

        if (!adt_is_compatible(adt, node, "i2c,s5l8940x"))
            continue;

        mngr_node = adt_first_child_offset(adt, node);
        if (mngr_node < 0 || !adt_is_compatible(adt, mngr_node, "usbc,manager"))
            continue;

        int it = mngr_node;
        ADT_FOREACH_CHILD(adt, it)
        {
            if (!adt_is_compatible(adt, it, "usbc,cd3217"))
                continue;

            const char *name = adt_get_name(adt, it);
            if (strcmp(name, "hpm0"))
                continue;

            ret = snprintf(i2c_path, sizeof(i2c_path), "/arm-io/%s", adt_get_name(adt, node));
            if (ret < 0 || (size_t)ret >= sizeof(i2c_path))
                continue;
            ret = snprintf(hpm_path, sizeof(hpm_path), "/arm-io/%s/%s/%s", adt_get_name(adt, node),
                           adt_get_name(adt, mngr_node), name);
            if (ret < 0 || (size_t)ret >= sizeof(hpm_path))
                continue;

            found = true;
        }
        if (found)
            break;
    }
    if (!found) {
        printf("tps6598x_enable_debugusb: i2c / hpm node not found\n");
        return -1;
    }

    printf("tps6598x: enable debugusb for %s\n", hpm_path);

    i2c_dev_t *i2c = i2c_init(i2c_path);
    if (!i2c) {
        printf("tps6598x_enable_debugusb: i2c_init failed for %s.\n", i2c_path);
        return -1;
    }

    tps6598x_dev_t *tps = tps6598x_init(hpm_path, i2c);
    if (!tps) {
        printf("tps6598x_enable_debugusb: tps6598x_init failed for %s.\n", hpm_path);
        return -1;
    }

    if (tps6598x_powerup(tps) < 0) {
        printf("tps6598x_enable_debugusb: tps6598x_powerup failed for %s.\n", hpm_path);
        tps6598x_shutdown(tps);
        return -1;
    }

    tps6598x_enter_kis(tps);

    tps6598x_shutdown(tps);

    i2c_shutdown(i2c);

    return 0;
}
