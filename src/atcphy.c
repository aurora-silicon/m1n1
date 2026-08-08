/* SPDX-License-Identifier: MIT */

#include "atcphy.h"
#include "adt.h"
#include "pmgr.h"
#include "string.h"
#include "utils.h"

#define FMT_ATCPHY_ATC_PATH "/arm-io/atc-phy%u"
#define FMT_ATCPHY_DRD_PATH "/arm-io/usb-drd%u"

/* ADT reg[] indices, confirmed against a live j414s ADT capture -- see
 * docs/j414s-atcphy.md sec 2 for the full table and provenance. */
#define ATCPHY_ATC_REG_USB2PHY 0u
#define ATCPHY_ATC_REG_CORE    3u
#define ATCPHY_ATC_REG_LPDPTX  20u
#define ATCPHY_ATC_REG_AXI2AF  24u
#define ATCPHY_DRD_REG_PIPEHANDLER 3u

#define ATCPHY_TUNABLE_RECORD_MAX 64u
#define ATCPHY_MAX_PORTS 4u

typedef struct {
    bool prepared;
    atcphy_mode_t mode;
    bool flipped;
} atcphy_routed_state_t;

static atcphy_routed_state_t atcphy_routed_state[ATCPHY_MAX_PORTS];

int atcphy_get_regs(u32 idx, atcphy_regs_t *regs)
{
    int atc_path[8];
    int drd_path[8];
    char path[24];

    if (!regs)
        return -1;
    memset(regs, 0, sizeof(*regs));

    snprintf(path, sizeof(path), FMT_ATCPHY_ATC_PATH, idx);
    if (adt_path_offset_trace(adt, path, atc_path) < 0) {
        printf("atcphy%u: %s not found\n", idx, path);
        return -1;
    }

    if (adt_get_reg(adt, atc_path, "reg", ATCPHY_ATC_REG_USB2PHY, &regs->usb2phy, NULL) < 0 ||
        adt_get_reg(adt, atc_path, "reg", ATCPHY_ATC_REG_CORE, &regs->core, NULL) < 0 ||
        adt_get_reg(adt, atc_path, "reg", ATCPHY_ATC_REG_LPDPTX, &regs->lpdptx, NULL) < 0 ||
        adt_get_reg(adt, atc_path, "reg", ATCPHY_ATC_REG_AXI2AF, &regs->axi2af, NULL) < 0) {
        printf("atcphy%u: %s missing an expected reg[] entry (usb2phy/core/lpdptx/axi2af)\n", idx,
               path);
        return -1;
    }

    snprintf(path, sizeof(path), FMT_ATCPHY_DRD_PATH, idx);
    if (adt_path_offset_trace(adt, path, drd_path) < 0) {
        printf("atcphy%u: %s not found\n", idx, path);
        return -1;
    }
    if (adt_get_reg(adt, drd_path, "reg", ATCPHY_DRD_REG_PIPEHANDLER, &regs->pipehandler, NULL) <
        0) {
        printf("atcphy%u: %s missing pipehandler reg[3]\n", idx, path);
        return -1;
    }

    return 0;
}

static int atcphy_atc_node_offset(u32 idx)
{
    char path[24];
    snprintf(path, sizeof(path), FMT_ATCPHY_ATC_PATH, idx);
    return adt_path_offset(adt, path);
}

/*
 * SoC-level PMGR power-domain enable for the atc-phy and usb-drd nodes.
 * m1n1's usb_init()/usb_phy_bringup() already does this for every port at
 * boot (usb.c:151-161), so under a normal boot this is a no-op; it is
 * repeated here so proxy-driven calls stay safe even if usb_init was
 * skipped or a domain was later dropped. This is the *SoC-wide* PMGR
 * mechanism, distinct from the ATCPHY-internal POWER_CTRL/POWER_STAT
 * "small"/"big" state machine the sequences below drive.
 */
static int atcphy_ensure_pmgr_power(u32 idx)
{
    char path[24];

    snprintf(path, sizeof(path), FMT_ATCPHY_ATC_PATH, idx);
    if (pmgr_adt_power_enable(path) < 0) {
        printf("atcphy%u: pmgr power enable failed for %s\n", idx, path);
        return -1;
    }
    snprintf(path, sizeof(path), FMT_ATCPHY_DRD_PATH, idx);
    if (pmgr_adt_power_enable(path) < 0) {
        printf("atcphy%u: pmgr power enable failed for %s\n", idx, path);
        return -1;
    }
    return 0;
}

static u64 atcphy_block_base(const atcphy_regs_t *regs, atcphy_block_t block)
{
    switch (block) {
        case ATCPHY_BLOCK_USB2PHY:
            return regs->usb2phy;
        case ATCPHY_BLOCK_CORE:
            return regs->core;
        case ATCPHY_BLOCK_PIPEHANDLER:
            return regs->pipehandler;
        case ATCPHY_BLOCK_AXI2AF:
            return regs->axi2af;
        case ATCPHY_BLOCK_LPDPTX:
            return regs->lpdptx;
        default:
            return 0;
    }
}

static u32 atcphy_mmio_read(void *ctx, atcphy_block_t block, u32 offset)
{
    const atcphy_regs_t *regs = ctx;
    u64 base = atcphy_block_base(regs, block);
    if (!base)
        return 0;
    return read32(base + offset);
}

static void atcphy_mmio_write(void *ctx, atcphy_block_t block, u32 offset, u32 value)
{
    const atcphy_regs_t *regs = ctx;
    u64 base = atcphy_block_base(regs, block);
    if (!base)
        return;
    write32(base + offset, value);
}

static void atcphy_mmio_delay(void *ctx, u32 us)
{
    UNUSED(ctx);
    udelay(us);
}

static int atcphy_run(const char *what, u32 idx, const atcphy_seq_op_t *ops, size_t n,
                      atcphy_regs_t *regs)
{
    size_t fail = 0;
    if (atcphy_seq_apply(ops, n, atcphy_mmio_read, atcphy_mmio_write, atcphy_mmio_delay, regs,
                         &fail) < 0) {
        printf("atcphy%u: %s failed at op %zu/%zu (%s)\n", idx, what, fail, n,
               fail < n ? ops[fail].cite : "?");
        return -1;
    }
    return 0;
}

/* Applies one ADT tunable property (by vocab entry) to its resolved
 * absolute offset, mirroring apple_tunable_apply's read-modify-write
 * (tunable.c:62-75) exactly. Returns 0 on success (including "property not
 * present and not required"), -1 on a required-but-missing/invalid blob. */
static int atcphy_apply_one_tunable(u32 idx, int atc_node, const atcphy_regs_t *regs,
                                    const atcphy_tunable_vocab_t *vocab)
{
    u32 len = 0;
    const u8 *blob = adt_getprop(adt, atc_node, vocab->adt_name, &len);

    if (!blob) {
        if (vocab->required) {
            printf("atcphy%u: required tunable %s not present in ADT\n", idx, vocab->adt_name);
            return -1;
        }
        return 0;
    }

    atcphy_tunable_record_t records[ATCPHY_TUNABLE_RECORD_MAX];
    size_t count = 0, bad_index = 0;
    atcphy_tunable_blob_verdict_t verdict = atcphy_tunable_validate(
        vocab, blob, len, records, ATCPHY_TUNABLE_RECORD_MAX, &count, &bad_index);

    if (verdict == ATCPHY_TUNABLE_BLOB_EMPTY)
        return 0;
    if (verdict != ATCPHY_TUNABLE_BLOB_OK) {
        printf("atcphy%u: tunable %s invalid (verdict=%d, record %zu)\n", idx, vocab->adt_name,
               (int)verdict, bad_index);
        return -1;
    }

    u64 base = (vocab->target == ATCPHY_TUNABLE_TARGET_CORE) ? regs->core : regs->axi2af;
    for (size_t i = 0; i < count; i++) {
        u32 old = read32(base + records[i].offset);
        u32 next = atcphy_tunable_apply_one(old, records[i].mask, records[i].value);
        if (next != old)
            write32(base + records[i].offset, next);
    }
    return 0;
}

/* Applies every vocab entry matching scope (GLOBAL entries always match
 * regardless of the scope/lane requested; lane-scoped entries must match
 * both scope and lane). Mirrors the two call sites in atcphy_apply_tunables
 * (atc.c:882-884 for GLOBAL, atc.c:892-911 for the per-mode switch). */
static int atcphy_apply_tunables_for(u32 idx, int atc_node, const atcphy_regs_t *regs,
                                     atcphy_tunable_scope_t wanted_scope, u8 wanted_lane)
{
    size_t vocab_count;
    const atcphy_tunable_vocab_t *vocab = atcphy_t6020_tunable_vocab(&vocab_count);

    for (size_t i = 0; i < vocab_count; i++) {
        const atcphy_tunable_vocab_t *v = &vocab[i];
        bool want =
            (v->scope == ATCPHY_TUNABLE_SCOPE_GLOBAL) || (v->scope == wanted_scope && v->lane == wanted_lane);
        if (!want)
            continue;
        if (atcphy_apply_one_tunable(idx, atc_node, regs, v) < 0)
            return -1;
    }

    return 0;
}

static int atcphy_apply_mode_owned(u32 idx, atcphy_mode_t mode, bool flipped,
                                   atcphy_pipe_policy_t pipe_policy, bool dp_rate_valid,
                                   atcphy_dp_rate_t dp_rate, bool routed_owner)
{
    if (pipe_policy < ATCPHY_PIPE_POLICY_REFUSE || pipe_policy > ATCPHY_PIPE_POLICY_DEFER) {
        printf("atcphy%u: invalid PIPE policy %d; refusing (valid: REFUSE, SWITCH, DEFER)\n",
               idx, (int)pipe_policy);
        return -1;
    }

    const atcphy_mode_config_t *cfg = atcphy_mode_config(mode, flipped);
    if (!cfg) {
        printf("atcphy%u: invalid mode %d\n", idx, (int)mode);
        return -1;
    }

    /* USB4/TBT is not a PHY-only mode. It requires the matching ACIO power
     * domains, M3/RTKit boot, DART, NHI rings, Apple root-router VSE cable
     * information, and an established tunnel before any consumer can use
     * the lanes. Until that complete owner calls a dedicated routed-mode
     * entry point, fail before the first PMGR or MMIO write. */
    if ((mode == ATCPHY_MODE_USB4 || mode == ATCPHY_MODE_TBT) && !routed_owner) {
        printf("atcphy%u: refusing mode %d: ACIO/NHI router bring-up is not implemented; "
               "a PHY-only USB4/TBT transition would strand the link\n",
               idx, (int)mode);
        return -1;
    }

    if (cfg->pipe_state == ATCPHY_PIPE_STATE_USB3 &&
        pipe_policy == ATCPHY_PIPE_POLICY_REFUSE) {
        printf("atcphy%u: mode %d needs a pipehandler PIPE-mux switch; refusing under "
               "ATCPHY_PIPE_POLICY_REFUSE (this would reprogram the interface a running guest's "
               "xHCI/dwc3 driver depends on -- see docs/j414s-atcphy.md sec 8/10)\n",
               idx, (int)mode);
        return -1;
    }

    atcphy_regs_t regs;
    if (atcphy_get_regs(idx, &regs) < 0)
        return -1;

    if (atcphy_ensure_pmgr_power(idx) < 0)
        return -1;

    int atc_node = atcphy_atc_node_offset(idx);
    if (atc_node < 0) {
        printf("atcphy%u: ADT node vanished between atcphy_get_regs and tunable lookup\n", idx);
        return -1;
    }

    size_t n;
    const atcphy_seq_op_t *ops;

    ops = atcphy_seq_usb2_power_on(&n);
    if (atcphy_run("usb2 power-on", idx, ops, n, &regs) < 0)
        return -1;

    ops = atcphy_seq_core_power_on(&n);
    if (atcphy_run("core power-on (small/big domain wake)", idx, ops, n, &regs) < 0)
        return -1;

    /* Global tunables always apply, atc.c:882-884. Fail closed: without
     * these the analog calibration is whatever silicon reset left behind. */
    if (atcphy_apply_tunables_for(idx, atc_node, &regs, ATCPHY_TUNABLE_SCOPE_GLOBAL, 0) < 0) {
        printf("atcphy%u: aborting -- required global tunable(s) missing/invalid\n", idx);
        return -1;
    }

    /* Lane tunables are mode-dependent, atc.c:886-917. OFF/USB2 apply none. */
    u8 lane0 = flipped ? 1u : 0u;
    u8 lane1 = flipped ? 0u : 1u;
    switch (mode) {
        case ATCPHY_MODE_USB3:
        case ATCPHY_MODE_USB3_DP:
            if (atcphy_apply_tunables_for(idx, atc_node, &regs, ATCPHY_TUNABLE_SCOPE_LANE_USB3,
                                          lane0) < 0 ||
                atcphy_apply_tunables_for(idx, atc_node, &regs, ATCPHY_TUNABLE_SCOPE_LANE_DP,
                                          lane1) < 0)
                return -1;
            break;
        case ATCPHY_MODE_DP:
            if (atcphy_apply_tunables_for(idx, atc_node, &regs, ATCPHY_TUNABLE_SCOPE_LANE_DP,
                                          lane0) < 0 ||
                atcphy_apply_tunables_for(idx, atc_node, &regs, ATCPHY_TUNABLE_SCOPE_LANE_DP,
                                          lane1) < 0)
                return -1;
            break;
        case ATCPHY_MODE_TBT:
        case ATCPHY_MODE_USB4:
            if (atcphy_apply_tunables_for(idx, atc_node, &regs, ATCPHY_TUNABLE_SCOPE_LANE_CIO,
                                          lane0) < 0 ||
                atcphy_apply_tunables_for(idx, atc_node, &regs, ATCPHY_TUNABLE_SCOPE_LANE_CIO,
                                          lane1) < 0)
                return -1;
            break;
        case ATCPHY_MODE_OFF:
        case ATCPHY_MODE_USB2:
        default:
            break;
    }

    ops = atcphy_seq_auspll_fsm_override(&n);
    if (atcphy_run("AUSPLL FSM override", idx, ops, n, &regs) < 0)
        return -1;

    ops = atcphy_seq_cfg0_sleep_override(&n);
    if (atcphy_run("CFG0/SLEEP_CTRL override", idx, ops, n, &regs) < 0)
        return -1;

    if (cfg->enable_dp_aux) {
        ops = atcphy_seq_dp_aux_enable(&n);
        if (atcphy_run("DP AUX enable", idx, ops, n, &regs) < 0)
            return -1;
    }

    ops = atcphy_seq_cio3pll_enable(&n);
    if (atcphy_run("CIO3PLL enable", idx, ops, n, &regs) < 0)
        return -1;

    atcphy_seq_op_t lane_ops[ATCPHY_LANE_CONFIG_MAX_OPS];
    size_t lane_n = atcphy_build_lane_config_ops(mode, flipped, lane_ops,
                                                 ATCPHY_LANE_CONFIG_MAX_OPS);
    if (lane_n == 0 || atcphy_run("lane/crossbar config", idx, lane_ops, lane_n, &regs) < 0)
        return -1;

    ops = atcphy_seq_release_phy_reset(&n);
    if (atcphy_run("PHY reset release", idx, ops, n, &regs) < 0)
        return -1;

    switch (cfg->pipe_state) {
        case ATCPHY_PIPE_STATE_DUMMY:
            ops = atcphy_seq_pipehandler_dummy(&n);
            if (atcphy_run("pipehandler -> dummy", idx, ops, n, &regs) < 0)
                return -1;
            break;
        case ATCPHY_PIPE_STATE_USB3:
            if (pipe_policy == ATCPHY_PIPE_POLICY_DEFER) {
                /*
                 * The PHY above is fully configured and out of reset; only the
                 * mux is left alone, still parked on DUMMY by our caller.
                 * Whoever runs next (Mu's AppleUsbTypeCBringupDxe) is expected
                 * to finish this after its own dwc3 core init -- see the
                 * ATCPHY_PIPE_POLICY_DEFER comment in atcphy.h for the
                 * ordering rule that forces the split.
                 *
                 * Not finishing it is a safe outcome, not a broken one: the
                 * port stays USB2-only.
                 */
                printf("atcphy%u: PHY configured, PIPE mux left on DUMMY (deferred switch); "
                       "SuperSpeed comes up only if the next stage finishes it after dwc3 "
                       "core init\n",
                       idx);
                break;
            }
            /* atcphy_pipehandler_check, atc.c:956-973: if a previous attempt
             * left the lock held, release it before starting a new one. */
            if (read32(regs.pipehandler + ATCPHY_PIPEHANDLER_LOCK_ACK) &
                ATCPHY_PIPEHANDLER_LOCK_EN) {
                printf("atcphy%u: pipehandler lock already held, clearing before BIST\n", idx);
                clear32(regs.pipehandler + ATCPHY_PIPEHANDLER_LOCK_REQ,
                        ATCPHY_PIPEHANDLER_LOCK_EN);
                poll32(regs.pipehandler + ATCPHY_PIPEHANDLER_LOCK_ACK, ATCPHY_PIPEHANDLER_LOCK_EN,
                       0, ATCPHY_PIPEHANDLER_LOCK_ACK_TIMEOUT_US);
            }
            ops = atcphy_seq_pipehandler_usb3_host_bist(&n);
            if (atcphy_run("pipehandler -> USB3 (host BIST)", idx, ops, n, &regs) < 0) {
                printf("atcphy%u: USB3 pipehandler switch failed partway -- PIPE state is now "
                       "UNDEFINED, do not assume a dummy fallback happened\n",
                       idx);
                return -1;
            }
            break;
        case ATCPHY_PIPE_STATE_USB4:
            if (!routed_owner) {
                printf("atcphy%u: refusing unowned USB4 PIPE state\n", idx);
                return -1;
            }
            /* Routed modes are prepared with their PIPE deliberately parked
             * on DUMMY.  The ACIO owner commits the USB4 mux only after its
             * firmware, NHI control rings, root router and USB3 tunnel have
             * all reached READY. */
            ops = atcphy_seq_pipehandler_dummy(&n);
            if (atcphy_run("pipehandler -> dummy (routed prepare)", idx, ops, n, &regs) < 0)
                return -1;
            break;
        default:
            return -1;
    }

    if (dp_rate_valid && (mode == ATCPHY_MODE_DP || mode == ATCPHY_MODE_USB3_DP)) {
        atcphy_seq_op_t dp_ops[ATCPHY_DP_RATE_MAX_OPS];
        size_t dp_n = atcphy_build_dp_rate_ops(dp_rate, dp_ops, ATCPHY_DP_RATE_MAX_OPS);
        if (dp_n == 0 || atcphy_run("DP AUSPLL rate", idx, dp_ops, dp_n, &regs) < 0) {
            printf("atcphy%u: DP rate programming failed\n", idx);
            return -1;
        }
        printf("atcphy%u: DP AUSPLL programmed for rate %u kHz -- note per-lane DP analog "
               "calibration is NOT implemented, the link is not expected to train\n",
               idx, atcphy_dp_rate_khz(dp_rate));
    }

    printf("atcphy%u: mode=%d orientation=%s applied (pipe_state=%d)\n", idx, (int)mode,
           flipped ? "flipped" : "normal", (int)cfg->pipe_state);
    return 0;
}

int atcphy_apply_mode(u32 idx, atcphy_mode_t mode, bool flipped,
                      atcphy_pipe_policy_t pipe_policy, bool dp_rate_valid,
                      atcphy_dp_rate_t dp_rate)
{
    return atcphy_apply_mode_owned(idx, mode, flipped, pipe_policy, dp_rate_valid, dp_rate,
                                   false);
}

int atcphy_prepare_routed_mode(u32 idx, atcphy_mode_t mode, bool flipped)
{
    if (idx >= ATCPHY_MAX_PORTS ||
        (mode != ATCPHY_MODE_USB4 && mode != ATCPHY_MODE_TBT)) {
        printf("atcphy%u: invalid routed-mode prepare request (%d)\n", idx, (int)mode);
        return -1;
    }

    atcphy_routed_state[idx].prepared = false;
    if (atcphy_apply_mode_owned(idx, mode, flipped, ATCPHY_PIPE_POLICY_DEFER, false,
                                ATCPHY_DP_RATE_RBR, true) < 0)
        return -1;

    atcphy_routed_state[idx] = (atcphy_routed_state_t){
        .prepared = true,
        .mode = mode,
        .flipped = flipped,
    };
    printf("atcphy%u: routed PHY prepared; PIPE remains on DUMMY\n", idx);
    return 0;
}

int atcphy_commit_routed_pipe(u32 idx)
{
    if (idx >= ATCPHY_MAX_PORTS || !atcphy_routed_state[idx].prepared) {
        printf("atcphy%u: refusing routed PIPE commit without a prepared PHY\n", idx);
        return -1;
    }

    atcphy_regs_t regs;
    if (atcphy_get_regs(idx, &regs) < 0)
        return -1;

    size_t n;
    const atcphy_seq_op_t *ops = atcphy_seq_pipehandler_usb4_routed(&n);
    if (atcphy_run("pipehandler -> routed USB4", idx, ops, n, &regs) < 0) {
        printf("atcphy%u: routed PIPE commit failed; caller must abort the route\n", idx);
        return -1;
    }

    /* Verify by READBACK, not by assuming the write took. The mux sequence
     * only touches the CLK/DATA fields, so a clean commit from the deferred
     * DUMMY (0x22) state leaves MUX_CTRL reading exactly the whole-register
     * routed value 0x11 (CLK_USB4|DATA_USB4). Anything else means the switch
     * did not settle -- refuse to claim a commit that did not take. */
    u32 mux = read32(regs.pipehandler + ATCPHY_PIPEHANDLER_MUX_CTRL);
    if (mux != ATCPHY_PIPEHANDLER_MUX_VALUE_USB4_TUNNEL) {
        printf("atcphy%u: routed PIPE commit wrote the mux but it read back %#x, not %#x "
               "(CLK_USB4|DATA_USB4); refusing to claim a commit that did not take\n",
               idx, mux, ATCPHY_PIPEHANDLER_MUX_VALUE_USB4_TUNNEL);
        return -1;
    }

    printf("atcphy%u: routed USB4 PIPE committed (MUX_CTRL=%#x)\n", idx, mux);
    return 0;
}

int atcphy_read_pipe_mux(u32 idx, u32 *mux)
{
    atcphy_regs_t regs;

    if (!mux || idx >= ATCPHY_MAX_PORTS)
        return -1;
    if (atcphy_get_regs(idx, &regs) < 0)
        return -1;

    *mux = read32(regs.pipehandler + ATCPHY_PIPEHANDLER_MUX_CTRL);
    return 0;
}

int atcphy_abort_routed_mode(u32 idx)
{
    if (idx >= ATCPHY_MAX_PORTS)
        return -1;

    int result = 0;
    atcphy_regs_t regs;
    if (atcphy_get_regs(idx, &regs) == 0) {
        size_t n;
        const atcphy_seq_op_t *ops = atcphy_seq_pipehandler_dummy(&n);
        if (atcphy_run("pipehandler -> dummy (routed abort)", idx, ops, n, &regs) < 0)
            result = -1;
    } else {
        result = -1;
    }

    atcphy_routed_state[idx].prepared = false;
    if (atcphy_power_off(idx) < 0)
        result = -1;
    return result;
}

int atcphy_set_orientation(u32 idx, bool flipped)
{
    return atcphy_apply_mode(idx, ATCPHY_MODE_USB2, flipped, ATCPHY_PIPE_POLICY_REFUSE, false,
                             ATCPHY_DP_RATE_RBR);
}

int atcphy_power_off(u32 idx)
{
    atcphy_regs_t regs;
    size_t n;
    const atcphy_seq_op_t *ops;

    if (atcphy_get_regs(idx, &regs) < 0)
        return -1;

    /* The APB side must be clocked for the power-down writes to land. */
    if (atcphy_ensure_pmgr_power(idx) < 0)
        return -1;

    /* Order matches atcphy_probe_finalize (atc.c:2242-2247): usb2 PHY off
     * first, then atcphy_power_off -- whose own first step is the
     * unconditional DP AUX disable (atc.c:1642). */
    ops = atcphy_seq_usb2_power_off(&n);
    if (atcphy_run("usb2 power-off", idx, ops, n, &regs) < 0)
        return -1;

    ops = atcphy_seq_dp_aux_disable(&n);
    if (atcphy_run("DP AUX disable", idx, ops, n, &regs) < 0)
        return -1;

    ops = atcphy_seq_core_power_off(&n);
    if (atcphy_run("core power-off (big/small domain sleep)", idx, ops, n, &regs) < 0)
        return -1;

    printf("atcphy%u: powered off (usb2 + DP AUX + core domains)\n", idx);
    return 0;
}

u64 atcphy_reg_base(u32 idx, u32 block)
{
    atcphy_regs_t regs;

    if (block >= ATCPHY_BLOCK_COUNT)
        return 0;
    if (atcphy_get_regs(idx, &regs) < 0)
        return 0;

    return atcphy_block_base(&regs, (atcphy_block_t)block);
}

/* Guest-handoff re-apply latch; see atcphy.h for the design rationale. */
static struct {
    bool armed;
    atcphy_mode_t mode;
    bool flipped;
    atcphy_pipe_policy_t pipe_policy;
} atcphy_guest_mode[ATCPHY_MAX_PORTS];

void atcphy_arm_guest_mode(u32 idx, atcphy_mode_t mode, bool flipped, bool armed,
                           atcphy_pipe_policy_t pipe_policy)
{
    if (idx >= ATCPHY_MAX_PORTS) {
        printf("atcphy: arm_guest_mode: invalid port %u\n", idx);
        return;
    }

    atcphy_guest_mode[idx].armed = armed;
    atcphy_guest_mode[idx].mode = mode;
    atcphy_guest_mode[idx].flipped = flipped;
    atcphy_guest_mode[idx].pipe_policy = pipe_policy;

    if (armed)
        printf("atcphy%u: ARMED guest mode %d (orientation=%s, pipe_policy=%s); it will be "
               "re-applied at guest USB handoff\n",
               idx, (int)mode, flipped ? "flipped" : "normal",
               pipe_policy == ATCPHY_PIPE_POLICY_DEFER
                   ? "DEFER (Mu finishes the mux switch)"
                   : (pipe_policy == ATCPHY_PIPE_POLICY_SWITCH ? "SWITCH" : "REFUSE"));
    else
        printf("atcphy%u: guest mode disarmed\n", idx);
}

int atcphy_reapply_guest_mode(u32 idx)
{
    if (idx >= ATCPHY_MAX_PORTS || !atcphy_guest_mode[idx].armed)
        return 0;

    printf("atcphy%u: re-applying armed guest mode %d (orientation=%s) after handoff\n", idx,
           (int)atcphy_guest_mode[idx].mode, atcphy_guest_mode[idx].flipped ? "flipped" : "normal");

    /* ATCPHY_PIPE_POLICY_SWITCH is safe here and only here: the handoff path
     * runs from hv_init, before the guest is entered, so no guest xHCI
     * driver is bound to this port. DEFER stops one step short of the mux
     * and leaves it to Mu, which is the correct order. See atcphy.h. */
    return atcphy_apply_mode(idx, atcphy_guest_mode[idx].mode, atcphy_guest_mode[idx].flipped,
                             atcphy_guest_mode[idx].pipe_policy, false, ATCPHY_DP_RATE_RBR);
}
