/* SPDX-License-Identifier: MIT */

#include "acio.h"
#include "adt.h"
#include "atcphy.h"
#include "memory.h"
#include "malloc.h"
#include "dart.h"
#include "gpio.h"
#include "pmgr.h"
#include "string.h"
#include "utils.h"

#define ACIO_RUNTIME_PORT_COUNT 3u
#define ACIO_TYPE5_FW_READY_TIMEOUT_US 500000u
/* Apple's control path uses a 17-entry receive ring with 256-byte frames. */
#define ACIO_CONTROL_RING_SIZE 17u

/* Byte offsets of the control ring's own TX/RX register blocks. */
#define ACIO_CTRL_TX_OFF                                                                           \
    (ACIO_TYPE5_TX_RING_BASE + ACIO_TYPE5_CONTROL_RING * ACIO_TYPE5_RING_STRIDE)
#define ACIO_CTRL_RX_OFF                                                                           \
    (ACIO_TYPE5_RX_RING_BASE + ACIO_TYPE5_CONTROL_RING * ACIO_TYPE5_RING_STRIDE)
#define ACIO_CONTROL_PAGE_SIZE 0x4000u
#define ACIO_CONTROL_TIMEOUT_US 500000u
/* Apple's waitForRingDisableDone polls at IODelay(10) us with a deadline of
 * 0x05F5E100 ns = 100 ms, returning 0xE00002D6 on timeout.  Confirmed
 * against the Type5 ring-transport contract section 7.4 step 3, so this is
 * Apple's value rather than a chosen one. */
#define ACIO_RING_DISABLE_TIMEOUT_US 100000u
/* Apple's config poll cadence/budget are CALLER-supplied, not baked into a
 * helper: configPollDWordWithMask takes (interval, count) and the router
 * paths pass interval 0x3E8 and count 0x3E8, i.e. 1000 us between polls and
 * up to 1000 polls -- a 1 s ceiling.  Sourced, no longer a placeholder. */
#define ACIO_ROUTER_POLL_INTERVAL_US 1000u
#define ACIO_ROUTER_POLL_TIMEOUT_US 1000000u
#define ACIO_ROUTER_MAX_STEPS 16u

typedef struct acio_type5_control_transport {
    dart_dev_t *dart;
    acio_type5_descriptor_t *tx_desc;
    u8 *tx_data;
    acio_type5_descriptor_t *rx_desc;
    u8 *rx_data;
    u64 iova_base;
    u16 tx_head;
    u16 tx_complete;
    u16 rx_complete;
    u16 rx_posted;
    u32 sequences;
    /* Cumulative plug-event acks this control session; zeroed with the rest
     * of the transport on control start/free. */
    u32 acks_sent;
    bool enabled;
} acio_type5_control_transport_t;

typedef struct acio_type5_runtime {
    enum acio_type5_runtime_phase phase;
    u32 index;
    acio_resources_t resources;
    acio_type5_control_transport_t control;
    acio_type5_router_sm_t router;
    u32 last_error;
    u32 last_error_detail;
    u32 error_count;
    u32 config_requests;
    u32 config_failures;
    u32 run_id;
} acio_type5_runtime_t;

static acio_type5_runtime_t acio_runtime[ACIO_RUNTIME_PORT_COUNT];

/* Record exactly one bounded error per failure path.  Every runtime return
 * of -1 goes through here so a diagnostic caller can tell which phase and
 * which hardware condition stopped the ladder without a re-run. */
/* Record a failure only if a more specific one has not already been recorded
 * for this operation.  A router poll that fails because the underlying config
 * transaction timed out must keep `cfg-rx-timeout`, not overwrite it with the
 * generic `router-poll-ready`: the deeper code names the actual hardware
 * condition, the shallower one only names the caller. */
static int acio_fail_keep_specific(acio_type5_runtime_t *runtime, u32 error, u32 detail)
{
    if ((runtime->last_error >> 8) == 0x07)
        return -1;
    runtime->last_error = error;
    runtime->last_error_detail = detail;
    runtime->error_count++;
    printf("acio%u: %s (detail=%#x)\n", runtime->index,
           acio_type5_error_name(error), detail);
    return -1;
}

static int acio_fail(acio_type5_runtime_t *runtime, u32 error, u32 detail)
{
    runtime->last_error = error;
    runtime->last_error_detail = detail;
    runtime->error_count++;
    printf("acio%u: %s (detail=%#x)\n", runtime->index,
           acio_type5_error_name(error), detail);
    return -1;
}

int acio_type5_runtime_status(u32 index, acio_type5_status_t *status)
{
    if (index >= ACIO_RUNTIME_PORT_COUNT || !status)
        return -1;
    const acio_type5_runtime_t *runtime = &acio_runtime[index];
    status->run_id = runtime->run_id;
    status->index = index;
    status->phase = runtime->phase;
    status->last_error = runtime->last_error;
    status->last_error_detail = runtime->last_error_detail;
    status->error_count = runtime->error_count;
    status->config_requests = runtime->config_requests;
    status->config_failures = runtime->config_failures;
    status->router_state = runtime->router.state;
    return 0;
}

static int acio_control_disable(acio_type5_runtime_t *runtime)
{
    acio_type5_control_transport_t *control = &runtime->control;
    if (!control->enabled)
        return 0;
    u64 nhi = runtime->resources.nhi_base;
    write32(nhi + ACIO_CTRL_TX_OFF + ACIO_TYPE5_RING_CONTROL, 0);
    write32(nhi + ACIO_CTRL_RX_OFF + ACIO_TYPE5_RING_CONTROL, 0);
    int result = 0;
    if (poll32(nhi + ACIO_CTRL_TX_OFF + ACIO_TYPE5_TX_DISABLE_DONE,
               1, 1, ACIO_RING_DISABLE_TIMEOUT_US) < 0)
        result = acio_fail(runtime, ACIO_TYPE5_E_CTRL_DISABLE_DONE, 0);
    if (poll32(nhi + ACIO_CTRL_RX_OFF + ACIO_TYPE5_RX_DISABLE_DONE,
               1, 1, ACIO_RING_DISABLE_TIMEOUT_US) < 0)
        result = acio_fail(runtime, ACIO_TYPE5_E_CTRL_DISABLE_DONE, 1);
    control->enabled = false;
    return result;
}

/* Apple-equivalent of the ADT platform function `Fact` on
 * /arm-io/dart-acioN, i.e. `function-dart_force_active`.
 *
 * Decoded: `Fact` is a FourCC selector, not an ADT op-stream.
 * IODART::callPlatformFunction matches 'Fact' (0x46616374) on an
 * exactly-8-byte property and dispatches to
 * AppleT8110DART::_forceAvailable(bool) with the bool taken as
 * `*(u32 *)arg != 0`.  That method sets a software force-available flag and
 * then, through _updateAvailability -> _powerUp/_powerDown, votes the
 * DART's OWN PMGR clock gate: AppleARMIODevice::enableDeviceClock(enable,
 * index 0) against the DART node's `clock-gates[0]`, reaching
 * ApplePMGR::_enableDevice with action (enable, auto=0) -> PMGR target
 * ACTIVE 0xf when enabling and PWRGATE 0 when disabling.
 *
 * It writes NO DART MMIO register.  Its `power-gates` leg is a proven
 * no-op: ApplePMGRFunctionPowerGate::callFunction drops the enable argument
 * and only queries _wasDeviceDisabled, and Apple passes a NULL out-pointer.
 *
 * The software half exists solely to suppress _updateAvailability's
 * mapper-driven auto-gating.  m1n1 has no such policy engine, so nothing
 * here will ever gate the DART underneath a live NHI DMA stream.  The clock
 * vote is therefore the entire equivalent, and it is idempotent when iBoot
 * has already left the gate ACTIVE.
 *
 * The ordering constraint that actually matters is clock-ACTIVE BEFORE
 * TCR/TTBR programming, not Apple's placement at the end of setHWState(2).
 * m1n1 may legally issue it earlier, immediately before dart_init_adt().
 *
 * Rollback is only safe once DMA is quiesced: Apple's force-inactive flag
 * makes _updateAvailability skip the "is any mapper still enabled?" scan
 * and gate unconditionally. */
static int acio_dart_force_active(acio_type5_runtime_t *runtime,
                                  const char *dart_path, bool enable)
{
    int node = adt_path_offset(adt, dart_path);
    u32 gates_size = 0;

    if (node < 0)
        return acio_fail(runtime, ACIO_TYPE5_E_CTRL_DART_FORCE, 0);

    /* MEASURED on the live J414s ADT: /arm-io/dart-acio{0,1,2} carry
     * `manual-availability = 1` but have NO `clock-gates` and NO
     * `power-gates` property at all.  So on this machine Apple's own
     * _powerUp -> enableDeviceClock(1, index 0) fails its bounds check and
     * returns kIOReturnBadArgument, which _powerUp discards.
     *
     * In other words `Fact(1)` performs NO PMGR action here; only the
     * software force-available flag is set, and that flag exists purely to
     * suppress _updateAvailability's mapper-driven auto-gating, which m1n1
     * has no counterpart for.  Absence is therefore the expected case and
     * must NOT fail the bring-up -- an earlier revision treated it as an
     * error and would have aborted the control ring on real hardware.
     *
     * The call is kept for machines whose DART node does declare a clock
     * gate, so the ownership assertion is real where it exists. */
    if (!adt_getprop(adt, node, "clock-gates", &gates_size) ||
        gates_size < sizeof(u32))
        return 0;

    int result = enable ? pmgr_adt_power_enable_index(dart_path, 0)
                        : pmgr_adt_power_disable_index(dart_path, 0);
    if (result < 0)
        return acio_fail(runtime, ACIO_TYPE5_E_CTRL_DART_FORCE, enable ? 1 : 0);
    return 0;
}

static void acio_control_free(acio_type5_control_transport_t *control)
{
    if (control->dart) {
        if (control->iova_base != DART_PTR_ERR)
            dart_unmap(control->dart, control->iova_base,
                       4 * ACIO_CONTROL_PAGE_SIZE);
        dart_shutdown(control->dart);
    }
    free(control->tx_desc);
    free(control->tx_data);
    free(control->rx_desc);
    free(control->rx_data);
    memset(control, 0, sizeof(*control));
}

static int acio_control_start(acio_type5_runtime_t *runtime)
{
    acio_type5_control_transport_t *control = &runtime->control;
    const acio_resources_t *resources = &runtime->resources;
    if (resources->sid_partition_mode != ACIO_SID_PARTITION_SHARED ||
        resources->dart_sid_count != 1)
        return acio_fail(runtime, ACIO_TYPE5_E_CTRL_SID_LAYOUT,
                         resources->dart_sid_count);
    u32 path_count = read32(resources->nhi_base);
    if ((path_count & 0x7ff) != ACIO_NHI_RING_COUNT)
        return acio_fail(runtime, ACIO_TYPE5_E_CTRL_PATH_COUNT, path_count);

    char dart_path[32];
    snprintf(dart_path, sizeof(dart_path), "/arm-io/dart-acio%u", resources->index);
    /* Fact(1) before any DART register access; see acio_dart_force_active. */
    if (acio_dart_force_active(runtime, dart_path, true) < 0)
        return -1;
    control->dart = dart_init_adt(dart_path, 0, resources->dart_sids[0], false);
    control->iova_base = DART_PTR_ERR;
    if (!control->dart) {
        acio_fail(runtime, ACIO_TYPE5_E_CTRL_DART_INIT, 0);
        goto fail;
    }
    control->tx_desc = memalign(ACIO_CONTROL_PAGE_SIZE, ACIO_CONTROL_PAGE_SIZE);
    control->tx_data = memalign(ACIO_CONTROL_PAGE_SIZE, ACIO_CONTROL_PAGE_SIZE);
    control->rx_desc = memalign(ACIO_CONTROL_PAGE_SIZE, ACIO_CONTROL_PAGE_SIZE);
    control->rx_data = memalign(ACIO_CONTROL_PAGE_SIZE, ACIO_CONTROL_PAGE_SIZE);
    if (!control->tx_desc || !control->tx_data || !control->rx_desc ||
        !control->rx_data) {
        acio_fail(runtime, ACIO_TYPE5_E_CTRL_ALLOC, 0);
        goto fail;
    }
    /* NOTE: m1n1's dart_init_adt masks vm_base with (1<<36)-1, so a t8110
     * ACIO DART whose ADT vm-base is 0x10000000000 reports 0 here, and
     * dart_find_iova's search ceiling is likewise a hardcoded 1<<36.  The
     * search therefore runs in the low 64 GiB regardless of the ADT window.
     * Report both values in the failure detail rather than guessing which of
     * the two is responsible. */
    u64 vm_base = dart_vm_base(control->dart);
    control->iova_base = dart_find_iova(
        control->dart, vm_base + ACIO_CONTROL_PAGE_SIZE,
        4 * ACIO_CONTROL_PAGE_SIZE);
    if (control->iova_base == DART_PTR_ERR) {
        printf("acio%u: dart_find_iova failed (vm_base=%#lx start=%#lx len=%#x)\n",
               resources->index, vm_base, vm_base + ACIO_CONTROL_PAGE_SIZE,
               4 * ACIO_CONTROL_PAGE_SIZE);
        acio_fail(runtime, ACIO_TYPE5_E_CTRL_IOVA, (u32)(vm_base >> 16));
        goto fail;
    }
    memset(control->tx_desc, 0, ACIO_CONTROL_PAGE_SIZE);
    memset(control->tx_data, 0, ACIO_CONTROL_PAGE_SIZE);
    memset(control->rx_desc, 0, ACIO_CONTROL_PAGE_SIZE);
    memset(control->rx_data, 0, ACIO_CONTROL_PAGE_SIZE);

    void *pages[] = {control->tx_desc, control->tx_data, control->rx_desc, control->rx_data};
    for (u32 i = 0; i < 4; i++) {
        if (dart_map(control->dart, control->iova_base + i * ACIO_CONTROL_PAGE_SIZE,
                     pages[i], ACIO_CONTROL_PAGE_SIZE) < 0) {
            acio_fail(runtime, ACIO_TYPE5_E_CTRL_MAP, i);
            goto fail;
        }
    }

    for (u32 i = 0; i < ACIO_CONTROL_RING_SIZE; i++) {
        control->rx_desc[i].address = control->iova_base +
            3 * ACIO_CONTROL_PAGE_SIZE + i * ACIO_TYPE5_CONTROL_FRAME_SIZE;
        control->rx_desc[i].metadata = ACIO_TYPE5_DESC_SW_SEED;
    }
    dc_cvac_range(control->rx_desc, ACIO_CONTROL_PAGE_SIZE);

    u64 tx_iova = control->iova_base;
    u64 rx_iova = control->iova_base + 2 * ACIO_CONTROL_PAGE_SIZE;
    u64 tx = resources->nhi_base + ACIO_CTRL_TX_OFF;
    u64 rx = resources->nhi_base + ACIO_CTRL_RX_OFF;
    /* Ring-manager interrupt enable; m1n1 polls, so mask every ring IRQ.
     * Both the TX and RX GenericACIO ring managers address this same
     * register (regmap-constant-offsets.txt:13-16).
     *
     * Rings 1..11 deliberately require nothing here.  Creating a ring object
     * touches no MMIO at all -- createRings, initWithNHI and allocate() have
     * a proven zero register-access count -- and there is no global
     * "enable all rings" register on this path.  The enable bit is per-ring,
     * bit 31 of that ring's own +0x10, so an unstarted ring's 16 KiB page is
     * simply never written.  Bringing up ring 0 alone is a first-class
     * designed-in mode, not an improvisation. */
    write32(resources->nhi_base + 0xd0010, 0);

    /* Apple's per-ring order, taken from the INSTRUCTION order in
     * TransmitRing::start / ReceiveRing::start rather than from the
     * summary table in the ring-transport contract, whose section 7.2 has
     * the last two steps in the wrong order:
     *
     *   +0x00/+0x04 descriptor IOVA
     *   -> +0x0C geometry
     *   -> +0x14  (RX PDF bitmasks / TX shared-buffer credits)
     *   -> +0x10  options word with bit 31 ENABLE, written LAST.
     *
     * RX: setPDFBitmasks is called at ReceiveRing::start+0x470, the enable
     * write is at +0x637.  TX: configureSharedBuffer at +0x457, enable at
     * +0x570.  Nothing but the doorbell and a bit-31 clear is ever written
     * after enable, and re-writing base/geometry/PDF while enabled is
     * illegal. */
    write32(tx + 0x00, (u32)tx_iova);
    write32(tx + 0x04, (u32)(tx_iova >> 32));
    /* TX +0x0C is a plain entry count. */
    write32(tx + ACIO_TYPE5_RING_COUNT_OFFSET, ACIO_CONTROL_RING_SIZE);
    write32(rx + 0x00, (u32)rx_iova);
    write32(rx + 0x04, (u32)(rx_iova >> 32));
    /* RX +0x0C is NOT a plain count: it is
     * {[27:16] buffer size, [15:0] ring size}, built by
     * `ldrh w22,[x19,#0x38]; ldr w8,[x19,#0x40]; bfi w22,w8,#0x10,#0xc`.
     * Writing a bare count leaves the per-descriptor buffer size at zero,
     * so the engine has no bound on how much it may write per descriptor. */
    write32(rx + ACIO_TYPE5_RING_COUNT_OFFSET,
            ((ACIO_TYPE5_CONTROL_FRAME_SIZE & 0xfffu) << 16) |
                (ACIO_CONTROL_RING_SIZE & 0xffffu));

    /* RX +0x14 = {[31:16] SOF PDF bitmask, [15:0] EOF PDF bitmask}.  The
     * PDF is the descriptor's EOF nibble and each mask is a bitmap over the
     * 16 possible nibble values, so 0xffff/0xffff is the promiscuous
     * control-ring policy Apple's ControlPath::createReceiver installs.
     * Type5 calls the base implementation first and then mirrors the
     * identical word into register range 1 at +0x4000*hop, so BOTH writes
     * are required. */
    write32(rx + 0x14, 0xffffffff);
    write32(resources->pdf_base + ACIO_TYPE5_CONTROL_RING * ACIO_TYPE5_RING_STRIDE,
            0xffffffff);

    /* TX +0x14 is the ACIO shared-buffer credit allocation, NOT a PDF mask.
     * Apple's allocateSharedBuffer splits a 232-credit pool across the 12
     * TX rings as {ring 0: 2, rings 1-5: 40, rings 6-11: 5}, and
     * configureSharedBuffer writes the low 16 bits of that value on every
     * ACIO TX ring start.  A previous revision wrote a literal 2 here with
     * no source; the correct value for the control ring's hop is the pool
     * share for that hop. */
    write32(tx + 0x14, ACIO_TYPE5_CONTROL_TX_CREDITS);

    /* Options word, written LAST: bit 31 ENABLE, bit 30 raw mode, bit 29
     * no-snoop.  No-snoop stays CLEAR: ACIO DMA is cache-coherent on T6020
     * (Apple uses write-back cacheable descriptor memory with only a
     * `dmb ish` and no cache maintenance anywhere), and setting bit 29
     * would make explicit cache maintenance mandatory.  TX additionally has
     * isoch interval in 26:0, isoch in 27 and E2E in 28; the control ring
     * is raw, non-isoch and non-E2E, so all of those stay zero. */
    /* Publish a zero consumer/producer index BEFORE enabling.  Apple's
     * ReceiveRing::start() never writes +0x08 -- only startDMA() does, and it
     * writes ring[0x48] just before its own enable write.  A plain start()
     * therefore leaves the hardware index at whatever a previous life or the
     * reset value left there while software believes head == 0.  Writing it
     * explicitly removes that disagreement. */
    write32(tx + 0x08, 0);
    write32(rx + 0x08, 0);

    write32(rx + ACIO_TYPE5_RING_CONTROL,
            ACIO_TYPE5_RING_ENABLE | ACIO_TYPE5_RX_RAW_MODE);
    write32(tx + ACIO_TYPE5_RING_CONTROL,
            ACIO_TYPE5_RING_ENABLE | ACIO_TYPE5_RX_RAW_MODE);

    control->rx_posted = ACIO_CONTROL_RING_SIZE - 1;
    dma_wmb();
    write32(rx + 0x08, control->rx_posted);
    control->enabled = true;
    printf("acio%u: polling NHI control ring 0 ready (SID %u)\n",
           resources->index, resources->dart_sids[0]);
    return 0;

fail:
    acio_control_free(control);
    return -1;
}

/* Local big-endian reader.  acio_type5.c has its own file-static copy; this
 * is only used to decode inbound frames for logging and for the router's
 * ConfigError detail, so it is not worth widening that file's API. */
static u32 acio_get_be32(const u8 *in)
{
    return (u32)in[0] << 24 | (u32)in[1] << 16 | (u32)in[2] << 8 | in[3];
}

/* Return one consumed RX frame to the ring: re-seed it, advance both the
 * completion and posted cursors, and ring the RX doorbell.  Apple's receive
 * path ALWAYS resubmits the receive command, whether or not the frame was
 * claimed by a pending config command, so the ring never stalls on a frame
 * that is not ours (IOThunderboltControlPath::rxCommandCallback). */
static int acio_control_rx_recycle(acio_type5_runtime_t *runtime,
                                   acio_type5_control_transport_t *control,
                                   acio_type5_descriptor_t *rx_descriptor)
{
    rx_descriptor->metadata = ACIO_TYPE5_DESC_SW_SEED;
    dc_cvac_range(rx_descriptor, sizeof(*rx_descriptor));
    if (acio_type5_ring_next(control->rx_complete, ACIO_CONTROL_RING_SIZE,
                             &control->rx_complete) < 0 ||
        acio_type5_ring_next(control->rx_posted, ACIO_CONTROL_RING_SIZE,
                             &control->rx_posted) < 0) {
        runtime->config_failures++;
        return acio_fail(runtime, ACIO_TYPE5_E_CFG_RING_ADVANCE,
                         control->rx_complete);
    }
    dma_wmb();
    write32(runtime->resources.nhi_base + ACIO_CTRL_RX_OFF + 0x08,
            control->rx_posted);
    return 0;
}

/* Transmit one plug-event ack FROM INSIDE a pending transaction.
 *
 * This must happen mid-transaction, not after it: MEASURED on J414s, the
 * router withholds the pending config response and retries the event for the
 * whole 500 ms budget while the ack is outstanding, so a deferred ack can
 * never converge.  Apple does the same thing structurally -- the event
 * listener path submits sendPlugEventAck's ConfigErrorCommand on the shared
 * TX ring while other config commands are in flight; its control path is
 * multi-command by design.
 *
 * Ring accounting: the transaction owns slot `tx_complete` (its request, in
 * flight) and has advanced `tx_head` past it.  The ack claims the next free
 * slot and advances `tx_head` again, but does NOT touch `tx_complete`; the
 * transaction epilogue's `tx_complete = tx_head` stays truthful because this
 * function also WAITS for the ack descriptor to complete (TX descriptors
 * complete in ring order, microseconds in practice, bounded by the caller's
 * deadline).  On the transaction's failure paths the cursors stay split and
 * the next call trips the existing cfg-busy guard -- the same fail-closed
 * wedge as today, resolved by abort.
 *
 * The caller is responsible for acking each DISTINCT event only once, so
 * nested TX occupancy is bounded by the number of distinct events, not by
 * the router's retry rate. */
static int acio_control_send_plug_ack(acio_type5_runtime_t *runtime,
                                      u64 route, u8 port, bool unplug,
                                      u64 deadline)
{
    acio_type5_control_transport_t *control = &runtime->control;
    if (acio_type5_ring_full(control->tx_head, control->tx_complete,
                             ACIO_CONTROL_RING_SIZE)) {
        printf("acio%u: TX ring full, plug-event ack not sent\n",
               runtime->index);
        return -1;
    }

    u16 slot = control->tx_head;
    u8 *packet = control->tx_data + slot * ACIO_TYPE5_CONTROL_FRAME_SIZE;
    if (acio_type5_plug_ack_pack(route, port, unplug, packet) < 0)
        return -1;
    if (acio_type5_descriptor_prepare(&control->tx_desc[slot],
            control->iova_base + ACIO_CONTROL_PAGE_SIZE +
                slot * ACIO_TYPE5_CONTROL_FRAME_SIZE,
            16, ACIO_TYPE5_PDF_CONFIG_ERROR, ACIO_TYPE5_PDF_CONFIG_ERROR) < 0)
        return -1;
    dc_cvac_range(packet, 16);
    dc_cvac_range(&control->tx_desc[slot], sizeof(control->tx_desc[slot]));
    if (acio_type5_ring_next(control->tx_head, ACIO_CONTROL_RING_SIZE,
                             &control->tx_head) < 0)
        return -1;
    dma_wmb();
    write32(runtime->resources.nhi_base + ACIO_CTRL_TX_OFF + 0x08,
            (u32)control->tx_head << 16);

    while (!timeout_expired(deadline)) {
        dc_civac_range(&control->tx_desc[slot], sizeof(control->tx_desc[slot]));
        if (acio_type5_descriptor_complete(&control->tx_desc[slot])) {
            control->tx_desc[slot].metadata = 0;
            dc_cvac_range(&control->tx_desc[slot],
                          sizeof(control->tx_desc[slot]));
            control->acks_sent++;
            return 0;
        }
        udelay(10);
    }
    return -1;
}

static int acio_control_transaction(acio_type5_runtime_t *runtime,
                                    acio_type5_config_request_t *request,
                                    u8 pdf, const void *payload, size_t payload_size,
                                    u32 *values, size_t value_count)
{
    acio_type5_control_transport_t *control = &runtime->control;
    if (!control->enabled || control->tx_head != control->tx_complete) {
        runtime->config_failures++;
        return acio_fail(runtime, ACIO_TYPE5_E_CFG_BUSY, control->enabled);
    }

    runtime->config_requests++;
    u16 slot = control->tx_head;
    u8 *tx_packet = control->tx_data + slot * ACIO_TYPE5_CONTROL_FRAME_SIZE;
    size_t packet_size;
    request->sequence = acio_type5_next_sequence(&control->sequences, pdf);
    if (acio_type5_config_packet_pack(request, pdf, payload, payload_size, tx_packet,
                                      ACIO_TYPE5_CONTROL_FRAME_SIZE, &packet_size) < 0) {
        runtime->config_failures++;
        return acio_fail(runtime, ACIO_TYPE5_E_CFG_PACK, pdf);
    }
    if (acio_type5_descriptor_prepare(&control->tx_desc[slot],
            control->iova_base + ACIO_CONTROL_PAGE_SIZE +
                slot * ACIO_TYPE5_CONTROL_FRAME_SIZE,
            (u16)packet_size, pdf, pdf) < 0) {
        runtime->config_failures++;
        return acio_fail(runtime, ACIO_TYPE5_E_CFG_DESC, (u32)packet_size);
    }

    dc_cvac_range(tx_packet, packet_size);
    dc_cvac_range(&control->tx_desc[slot], sizeof(control->tx_desc[slot]));
    if (acio_type5_ring_next(control->tx_head, ACIO_CONTROL_RING_SIZE,
                             &control->tx_head) < 0) {
        runtime->config_failures++;
        return acio_fail(runtime, ACIO_TYPE5_E_CFG_RING_ADVANCE, control->tx_head);
    }
    dma_wmb();
    write32(runtime->resources.nhi_base + ACIO_CTRL_TX_OFF + 0x08,
            (u32)control->tx_head << 16);

    /* The control ring is SHARED between config responses and unsolicited
     * router frames -- that is Apple's design, not a fault, and it is why the
     * RX PDF masks are programmed promiscuous (0xffff/0xffff) above.  A
     * router emits PDF-5 hot-plug events on this same ring the moment a
     * topology becomes valid, so "the next completed RX descriptor is my
     * response" is wrong for EVERY transaction; it merely stayed invisible
     * until a device was attached and the event generator armed.
     *
     * Apple's IOThunderboltConfigCommand::processResponse returns 0 ("not
     * mine") for EOF 5/6/7 BEFORE any other check, and the receive command is
     * always resubmitted.  Mirror that exactly: consume such a frame, hand
     * the slot back to the ring, and keep waiting inside the same timeout.
     *
     * `rx_descriptor` must be recomputed each iteration -- consuming a frame
     * advances rx_complete, so a pointer hoisted out of the loop would go on
     * inspecting the slot we just recycled. */
    u64 timeout = timeout_calculate(ACIO_CONTROL_TIMEOUT_US);
    acio_type5_descriptor_t *rx_descriptor = NULL;
    u16 received = 0;
    u8 response_pdf = 0;
    u8 *rx_packet = NULL;
    bool tx_done = false;
    bool have_reply = false;
    u32 foreign_frames = 0;
    /* Plug-event dedupe, per transaction: one log line and ONE ack per
     * DISTINCT (route, dword2) tuple.  Repeats are counted and consumed
     * silently -- if the first ack did not stop them, more identical acks
     * will not either, and re-acking every ~10 ms retry would wrap the
     * 17-slot TX ring under the in-flight request. */
    bool event_acked = false;
    u64 acked_route = 0;
    u32 acked_word = 0;
    u32 event_repeats = 0;

    while (!timeout_expired(timeout)) {
        dc_civac_range(&control->tx_desc[slot], sizeof(control->tx_desc[slot]));
        if (!tx_done)
            tx_done = acio_type5_descriptor_complete(&control->tx_desc[slot]);

        rx_descriptor = &control->rx_desc[control->rx_complete];
        dc_civac_range(rx_descriptor, sizeof(*rx_descriptor));
        if (!acio_type5_descriptor_complete(rx_descriptor)) {
            udelay(10);
            continue;
        }

        received = (u16)(rx_descriptor->metadata & 0xfff);
        response_pdf = (u8)((rx_descriptor->metadata >> 12) & 0xf);
        if (received > ACIO_TYPE5_CONTROL_FRAME_SIZE || received < 16) {
            runtime->config_failures++;
            return acio_fail(runtime, ACIO_TYPE5_E_CFG_RX_LENGTH, received);
        }
        rx_packet = control->rx_data +
                    control->rx_complete * ACIO_TYPE5_CONTROL_FRAME_SIZE;
        dc_civac_range(rx_packet, received);

        /* PDF 5 (hot-plug event), 6/7 (XDomain): never a reply to a config
         * request.  Decode, ACK (PDF 5 only, once per distinct event), then
         * recycle the slot and keep waiting for the real response.
         *
         * Field positions in dword2 -- port [5:0], unplug bit 31 -- are
         * LINUX-DERIVED (struct cfg_event_pkg); Apple's own bit-level event
         * parse was not located in the corpus (only the already-parsed
         * consumers: Switch::processPlugEvent, fakePlugEvent(unplug, route,
         * port)).  The raw dword is printed so a hardware run can falsify
         * the layout: if acks do not stop the retries AND the raw word's
         * [13:8] differs from [5:0], the port was extracted from the wrong
         * field, not the pg encoding. */
        if (response_pdf >= ACIO_TYPE5_PDF_EVENT &&
            response_pdf <= ACIO_TYPE5_PDF_XDOMAIN_RESPONSE) {
            if (response_pdf == ACIO_TYPE5_PDF_EVENT && received >= 16) {
                u32 event = acio_get_be32(rx_packet + 8);
                u64 event_route =
                    (u64)(acio_get_be32(rx_packet) & 0x003fffffu) << 32 |
                    acio_get_be32(rx_packet + 4);
                u8 event_port = event & 0x3f;
                bool event_unplug = ((event >> 31) & 1) != 0;
                if (event_acked && event == acked_word &&
                    event_route == acked_route) {
                    event_repeats++;
                } else {
                    printf("acio%u: plug event route=%#lx port=%u unplug=%u "
                           "raw=%#010x; acking (pg=%u, provisional)\n",
                           runtime->index, event_route, event_port,
                           event_unplug, event,
                           event_unplug ? ACIO_TYPE5_PG_HOT_UNPLUG_ACK
                                        : ACIO_TYPE5_PG_HOT_PLUG_ACK);
                    if (acio_control_send_plug_ack(runtime, event_route,
                                                   event_port, event_unplug,
                                                   timeout) == 0) {
                        event_acked = true;
                        acked_word = event;
                        acked_route = event_route;
                    } else {
                        printf("acio%u: plug-event ack transmit FAILED\n",
                               runtime->index);
                    }
                }
            } else {
                printf("acio%u: unsolicited PDF %u frame (%u bytes)\n",
                       runtime->index, response_pdf, received);
            }
            foreign_frames++;
            if (acio_control_rx_recycle(runtime, control, rx_descriptor) < 0)
                return -1;
            continue;
        }

        have_reply = true;
        break;
    }

    /* Repeats AFTER the ack went out.  The router retries roughly every
     * 10 ms, so a correct ack leaves only the pre-ack RX backlog (expect
     * 0..2); a count in the tens means the router did NOT accept the ack --
     * per the provenance notes, suspect the provisional pg values (or, if
     * raw [13:8] != [5:0] above, the event port field) before anything
     * else.  Printed on success AND failure paths alike. */
    if (event_repeats)
        printf("acio%u: %u repeated plug-event frame(s) after ack "
               "(%u ack(s) this control session)\n",
               runtime->index, event_repeats, control->acks_sent);

    if (!tx_done) {
        runtime->config_failures++;
        return acio_fail(runtime, ACIO_TYPE5_E_CFG_TX_TIMEOUT,
                         control->tx_desc[slot].metadata);
    }
    if (!have_reply) {
        runtime->config_failures++;
        return acio_fail(runtime, ACIO_TYPE5_E_CFG_RX_TIMEOUT,
                         rx_descriptor ? rx_descriptor->metadata : foreign_frames);
    }

    /* A PDF-3 ConfigError IS addressed to us; it is the router refusing the
     * access, not a foreign frame.  Report it distinctly so the detail names
     * the rejected register instead of being read as a demux failure. */
    if (response_pdf == ACIO_TYPE5_PDF_CONFIG_ERROR) {
        runtime->config_failures++;
        return acio_fail(runtime, ACIO_TYPE5_E_CFG_RX_ROUTER_ERROR,
                         received >= 12 ? acio_get_be32(rx_packet + 8) : 0);
    }
    if (response_pdf != pdf) {
        runtime->config_failures++;
        return acio_fail(runtime, ACIO_TYPE5_E_CFG_RX_PDF,
                         (u32)response_pdf << 8 | pdf);
    }
    if (acio_type5_config_response_parse_n(request, response_pdf, rx_packet, received,
                                           values, value_count) < 0) {
        runtime->config_failures++;
        return acio_fail(runtime, ACIO_TYPE5_E_CFG_RX_REJECT,
                         rx_descriptor->metadata);
    }

    control->tx_desc[slot].metadata = 0;
    control->tx_complete = control->tx_head;
    dc_cvac_range(&control->tx_desc[slot], sizeof(control->tx_desc[slot]));
    return acio_control_rx_recycle(runtime, control, rx_descriptor);
}

int acio_type5_config_read32(u32 index, u64 route, u8 port, u8 space,
                             u16 offset, u32 *value)
{
    if (index >= ACIO_RUNTIME_PORT_COUNT || !value ||
        acio_runtime[index].phase < ACIO_TYPE5_RUNTIME_CONTROL_READY)
        return -1;
    acio_type5_config_request_t request = {
        .route = route, .offset = offset, .length = 1,
        .adapter = port, .space = space,
    };
    return acio_control_transaction(&acio_runtime[index], &request,
                                    ACIO_TYPE5_PDF_CONFIG_READ, NULL, 0, value, 1);
}

int acio_type5_config_write32(u32 index, u64 route, u8 port, u8 space,
                              u16 offset, u32 value)
{
    if (index >= ACIO_RUNTIME_PORT_COUNT ||
        acio_runtime[index].phase < ACIO_TYPE5_RUNTIME_CONTROL_READY)
        return -1;
    acio_type5_config_request_t request = {
        .route = route, .offset = offset, .length = 1,
        .adapter = port, .space = space,
    };
    return acio_control_transaction(&acio_runtime[index], &request,
                                    ACIO_TYPE5_PDF_CONFIG_WRITE,
                                    &value, sizeof(value), NULL, 0);
}

int acio_type5_config_read_block(u32 index, u64 route, u8 port, u8 space, u16 offset,
                                 u8 count, u32 *values)
{
    if (index >= ACIO_RUNTIME_PORT_COUNT || !values || !count || count > 0x3f ||
        acio_runtime[index].phase < ACIO_TYPE5_RUNTIME_CONTROL_READY)
        return -1;
    acio_type5_config_request_t request = {
        .route = route, .offset = offset, .length = count,
        .adapter = port, .space = space,
    };
    return acio_control_transaction(&acio_runtime[index], &request,
                                    ACIO_TYPE5_PDF_CONFIG_READ, NULL, 0,
                                    values, count);
}

int acio_type5_config_write_block(u32 index, u64 route, u8 port, u8 space, u16 offset,
                                  u8 count, const u32 *values)
{
    if (index >= ACIO_RUNTIME_PORT_COUNT || !values || !count || count > 0x3f ||
        acio_runtime[index].phase < ACIO_TYPE5_RUNTIME_CONTROL_READY)
        return -1;
    acio_type5_config_request_t request = {
        .route = route, .offset = offset, .length = count,
        .adapter = port, .space = space,
    };
    return acio_control_transaction(&acio_runtime[index], &request,
                                    ACIO_TYPE5_PDF_CONFIG_WRITE,
                                    values, (size_t)count * sizeof(u32), NULL, 0);
}

int acio_type5_hop_write(u32 index, u64 route, u8 port, u16 hop,
                         const acio_type5_hop_descriptor_t *descriptor)
{
    u8 packed[8];
    u16 offset;

    if (!descriptor || acio_type5_hop_offset(hop, &offset) < 0 ||
        acio_type5_hop_pack(descriptor, packed) < 0)
        return -1;

    /* hop_pack emits wire (big-endian) bytes; config_write_block takes native
     * dwords and does its own big-endian encoding.  Decode back to native
     * here rather than adding a second encoding path -- one wire encoder is
     * the whole point. */
    u32 words[ACIO_TYPE5_HOP_DWORDS];
    for (u32 i = 0; i < ACIO_TYPE5_HOP_DWORDS; i++)
        words[i] = (u32)packed[i * 4] << 24 | (u32)packed[i * 4 + 1] << 16 |
                   (u32)packed[i * 4 + 2] << 8 | packed[i * 4 + 3];

    return acio_type5_config_write_block(index, route, port, ACIO_TYPE5_CONFIG_HOPS,
                                         offset, ACIO_TYPE5_HOP_DWORDS, words);
}

/* Program one direction's hop descriptor. Credits are Apple's static
 * literals: 2 on the host side, 14 on the device side. NFC credits are
 * deliberately absent -- they are provably zero for USB3, so the
 * compare-swap phase is skipped entirely. */
static int acio_usb3_program_hop(acio_type5_runtime_t *runtime, u32 index,
                                 u64 route, u8 port, u16 in_hop, u8 out_port,
                                 u16 out_hop, u8 credits, bool valid)
{
    acio_type5_hop_descriptor_t descriptor = {
        .valid = valid,
        .initial_credits = credits,
        .out_port = out_port,
        .out_hop = out_hop,
        .egress_flow_control = true,
        .ingress_flow_control = true,
        .priority = 3,
        .weight = 3,
    };
    if (acio_type5_hop_write(index, route, port, in_hop, &descriptor) < 0)
        return acio_fail_keep_specific(runtime, ACIO_TYPE5_E_TUNNEL_STATE,
                                       (u32)port << 16 | in_hop);
    return 0;
}

static const char *acio_adapter_kind_name(enum acio_type5_adapter_kind kind)
{
    switch (kind) {
        case ACIO_ADAPTER_INACTIVE:   return "inactive";
        case ACIO_ADAPTER_LANE:       return "lane";
        case ACIO_ADAPTER_NHI:        return "NHI";
        case ACIO_ADAPTER_DP:         return "DP";
        case ACIO_ADAPTER_PCIE:       return "PCIe";
        case ACIO_ADAPTER_USB3:       return "USB3";
        case ACIO_ADAPTER_USB_GEN_T:  return "USB-T";
        default:                      return "unknown";
    }
}

/* Apple's ~1 s training budget (`0x3B9ACA01` ns) polled at its 10 ms
 * `IOThunderboltSleepUngated` cadence. */
#define ACIO_LANE_LINK_TIMEOUT_US 1000000u
#define ACIO_LANE_LINK_INTERVAL_US 10000u
#define ACIO_LANE_CAP_MAX_STEPS 32u

/* Walk one adapter's ADAPTER-space capability list looking for (header & mask)
 * == value, and return the offset of the matching capability HEADER dword.
 *
 * Shared by the lane-adapter link-state gate and the USB3 protocol-adapter
 * enable so the two cannot drift: a second, separately-maintained copy of a
 * capability walk is how one of them ends up with a subtly different list
 * terminator. The caller supplies the error code so each site still names its
 * own failure ("could not read the link state" vs "no USB3 adapter capability").
 *
 * Fails CLOSED in every direction: a list that does not contain the capability,
 * a header that cannot be read, and a malformed list all return -1 rather than
 * yielding a plausible-looking offset. An offset we are not sure of is worse
 * than none, because the router will happily ACK a write to it.
 *
 * UNPROVEN, and inherited from the existing lane walk: that the list head is
 * ADP_CS_0[7:0]. That is the USB4 spec layout and matches the capability header
 * format proven from the kernelcache ([7:0] next, [15:8] ID), but Apple's
 * findCapability seed for ADAPTER space was not decoded. Relying on it is safe
 * only because being wrong fails closed here. */
static int acio_find_adapter_capability(acio_type5_runtime_t *runtime, u32 index,
                                        u64 route, u8 adapter, u32 mask, u32 value,
                                        u32 error, u16 *cap_offset)
{
    u32 adp0;
    if (acio_type5_config_read32(index, route, adapter,
                                 ACIO_TYPE5_CONFIG_ADAPTER, 0, &adp0) < 0)
        return acio_fail_keep_specific(runtime, error, adapter);

    u16 offset = (u16)(adp0 & 0xffu);
    for (u32 step = 0; offset && step < ACIO_LANE_CAP_MAX_STEPS; step++) {
        u32 header;
        bool matched = false, finished = false;
        u16 next = 0;
        if (acio_type5_config_read32(index, route, adapter,
                                     ACIO_TYPE5_CONFIG_ADAPTER, offset,
                                     &header) < 0)
            return acio_fail_keep_specific(runtime, error, offset);
        if (acio_type5_capability_step(offset, header, mask, value, &matched,
                                       &finished, &next) < 0)
            return acio_fail(runtime, error, header);
        if (matched) {
            *cap_offset = offset;
            return 0;
        }
        if (finished)
            break;
        offset = next;
    }
    return acio_fail(runtime, error, (u32)adapter << 16 | value);
}

/* Wait for the host router's downstream lane adapter to finish training
 * before descending through it, exactly as `childDeviceScanForPort` does.
 *
 * Every failure here is fail-closed. There is no path that proceeds on an
 * unread or unrecognised link state: an unreadable state is not evidence of a
 * working link. See the whitelist contract in acio.h.
 */
static int acio_scan_wait_link_up(acio_type5_runtime_t *runtime, u32 index,
                                  u8 down_adapter)
{
    u32 adp[8];
    if (acio_type5_config_read_block(index, 0, down_adapter,
                                     ACIO_TYPE5_CONFIG_ADAPTER, 0, 8, adp) < 0)
        return acio_fail_keep_specific(runtime, ACIO_TYPE5_E_SCAN_ADAPTER,
                                       down_adapter);

    /* Apple's `portAllowsDeviceScan` refuses anything whose adapter type is
     * not 1. A child router hangs off a LANE adapter; asking a USB3 or PCIe
     * adapter for a link state would read a different register entirely. */
    u32 type = adp[2] & 0x00ffffffu;
    if (type != ACIO_TYPE5_ADAPTER_LANE) {
        printf("acio%u: host adapter %u is type %#x, not a lane adapter; "
               "refusing to scan through it\n", index, down_adapter, type);
        return acio_fail(runtime, ACIO_TYPE5_E_SCAN_NOT_LANE, type);
    }

    /* Walk the adapter capability list to LANE_ADP (ID 0x01).  See
     * acio_find_adapter_capability for the shared walk and its one unproven
     * assumption (the list head), which fails closed. */
    u16 cap_offset = 0;
    if (acio_find_adapter_capability(runtime, index, 0, down_adapter,
                                     ACIO_TYPE5_LANE_ADP_CAP_MASK,
                                     ACIO_TYPE5_LANE_ADP_CAP_VAL,
                                     ACIO_TYPE5_E_SCAN_LANE_CAP,
                                     &cap_offset) < 0)
        return -1;
    if (!cap_offset) {
        printf("acio%u: adapter %u has no LANE_ADP capability (ID %#x); the "
               "link state cannot be read, so the scan is refused rather than "
               "assumed ready\n",
               index, down_adapter, ACIO_TYPE5_LANE_ADP_CAP_ID);
        return acio_fail(runtime, ACIO_TYPE5_E_SCAN_LANE_CAP, down_adapter);
    }

    u32 cs1 = 0;
    u32 state = 0;
    u64 timeout = timeout_calculate(ACIO_LANE_LINK_TIMEOUT_US);
    u32 attempts = 0;
    do {
        if (acio_type5_config_read32(index, 0, down_adapter,
                                     ACIO_TYPE5_CONFIG_ADAPTER,
                                     (u16)(cap_offset + 1), &cs1) < 0)
            return acio_fail_keep_specific(runtime,
                                           ACIO_TYPE5_E_SCAN_LANE_CAP,
                                           (u32)cap_offset + 1);
        attempts++;
        state = acio_type5_lane_link_state(cs1);
        if (acio_type5_lane_link_up(cs1)) {
            printf("acio%u: adapter %u link is up (LANE_ADP_CS_1=%#010x, "
                   "state %u) after %u attempt(s)\n",
                   index, down_adapter, cs1, state, attempts);
            return 0;
        }
        udelay(ACIO_LANE_LINK_INTERVAL_US);
    } while (!timeout_expired(timeout));

    printf("acio%u: adapter %u link training did not complete within %u us "
           "(LANE_ADP_CS_1=%#010x, state %u, %u attempts). State is outside "
           "the operational set {%u..%u}; refusing to scan.\n",
           index, down_adapter, ACIO_LANE_LINK_TIMEOUT_US, cs1, state, attempts,
           ACIO_TYPE5_LANE_LINK_UP_MIN, ACIO_TYPE5_LANE_LINK_UP_MAX);
    return acio_fail(runtime, ACIO_TYPE5_E_SCAN_LINK_NOT_READY, cs1);
}

int acio_type5_scan_device_router(u32 index, u8 down_adapter, u64 *device_route,
                                  u32 *device_depth, u8 *usb3_up_adapter,
                                  u8 *upstream_port)
{
    if (index >= ACIO_RUNTIME_PORT_COUNT || !device_route || !device_depth ||
        !usb3_up_adapter || !upstream_port)
        return -1;
    acio_type5_runtime_t *runtime = &acio_runtime[index];
    if (runtime->phase < ACIO_TYPE5_RUNTIME_ROUTER_READY)
        return acio_fail(runtime, ACIO_TYPE5_E_BAD_PHASE, runtime->phase);

    /* The host router is route 0 at depth 0, so a device hanging off host
     * downstream adapter N is simply route N at depth 1.  Derived rather
     * than assumed: the shift uses the PARENT's depth. */
    /* Gate on link training BEFORE any config transaction is aimed at the
     * child. A router that has not finished training will not answer, and a
     * timeout there would be misread as "no device". */
    if (acio_scan_wait_link_up(runtime, index, down_adapter) < 0)
        return -1;

    u64 route;
    u32 depth;
    if (acio_type5_route_child(0, 0, down_adapter, &route, &depth) < 0)
        return acio_fail(runtime, ACIO_TYPE5_E_SCAN_ROUTE, down_adapter);

    /* Router header: config space 2, port 0, offset 0, length 5. */
    u32 cs[5];
    if (acio_type5_config_read_block(index, route, 0, ACIO_TYPE5_CONFIG_ROUTER,
                                     0, 5, cs) < 0)
        return acio_fail_keep_specific(runtime, ACIO_TYPE5_E_SCAN_HEADER,
                                       (u32)route);

    /* MaxPort is ROUTER_CS_1 [19:14]; the same word carries Rev [31:24],
     * Depth [22:20], UpstreamPort [13:8] and NextCapPtr [7:0]. */
    u32 max_port = (cs[1] >> 14) & 0x3fu;
    if (!max_port || max_port > ACIO_TYPE5_MAX_ADAPTERS)
        return acio_fail(runtime, ACIO_TYPE5_E_SCAN_MAXPORT, cs[1]);

    /* UpstreamPort is ROUTER_CS_1 [13:8] -- the device router's own lane
     * adapter facing us.  The device-side hops are programmed between THIS
     * adapter and the USB3 Up adapter, so a tunnel cannot be described without
     * it.  Bounds-checked against MaxPort: an upstream port outside the
     * adapter range would send hop writes to an adapter that does not exist. */
    u32 upstream = (cs[1] >> 8) & 0x3fu;
    if (!upstream || upstream > max_port)
        return acio_fail(runtime, ACIO_TYPE5_E_SCAN_MAXPORT, cs[1]);
    *upstream_port = (u8)upstream;

    /* Adapters are numbered 1..MaxPort inclusive; adapter 0 is the router
     * itself and is never scanned.  Each is config space 1, offset 0,
     * length 8. */
    /* Every slot is printed, including inactive and undecodable ones.  A scan
     * that ends in scan-no-usb3 has to read as a TOPOLOGY -- "here is what is
     * on this device router and none of it is a USB3 Up adapter" -- not as a
     * dead end.  The previous revision skipped both cases before the print, so
     * an adapter whose family this decoder does not recognise was invisible:
     * "we could not classify what we saw" and "there was nothing to see"
     * produced byte-identical output, and a cabled run is far too scarce to
     * spend one on that ambiguity. */
    u32 inactive = 0, decoded_count = 0, undecoded = 0;
    for (u32 adapter = 1; adapter <= max_port; adapter++) {
        u32 adp[8];
        if (acio_type5_config_read_block(index, route, (u8)adapter,
                                         ACIO_TYPE5_CONFIG_ADAPTER, 0, 8,
                                         adp) < 0)
            return acio_fail_keep_specific(runtime, ACIO_TYPE5_E_SCAN_ADAPTER,
                                           adapter);

        /* Adapter type is ADP_CS_2 [23:0] -- one field, same encoding as the
         * host router's ADT portmap.  Type 0 means the port is inactive;
         * there is no separate presence bit. */
        u32 type = adp[2] & 0x00ffffffu;
        acio_type5_adapter_t decoded;
        /* NULL: the scan reads ADP_CS_0..7 only, never the port-default register,
         * so the hop/buffer fields must report themselves as unread rather
         * than as measured zeroes. */
        bool known = acio_type5_adapter_decode(type, NULL, &decoded) == 0;

        if (!known) {
            /* Direction is NOT claimed here: the low byte only means UP/DOWN
             * within a family this decoder understands, and by definition it
             * does not understand this one. */
            undecoded++;
            printf("acio%u: device route %#llx adapter %u/%u type %#x -> "
                   "UNDECODED (family %#x, sub %#x)\n",
                   index, (unsigned long long)route, adapter, max_port, type,
                   (type >> 16) & 0xffu, type & 0xffu);
            continue;
        }
        if (decoded.kind == ACIO_ADAPTER_INACTIVE)
            inactive++;
        else
            decoded_count++;
        printf("acio%u: device route %#llx adapter %u/%u type %#x -> %s%s\n",
               index, (unsigned long long)route, adapter, max_port, type,
               acio_adapter_kind_name(decoded.kind),
               decoded.kind == ACIO_ADAPTER_INACTIVE ? ""
                   : (decoded.up ? " up" : " down"));
        if (type != ACIO_TYPE5_ADAPTER_USB3_UP)
            continue;

        *device_route = route;
        *device_depth = depth;
        *usb3_up_adapter = (u8)adapter;
        return 0;
    }

    printf("acio%u: device route %#llx has no USB3 Up adapter (%#x) among %u "
           "adapters: %u active, %u inactive, %u undecoded\n",
           index, (unsigned long long)route, ACIO_TYPE5_ADAPTER_USB3_UP,
           max_port, decoded_count, inactive, undecoded);
    return acio_fail(runtime, ACIO_TYPE5_E_SCAN_NO_USB3, max_port);
}

int acio_type5_usb3_tunnel_up(u32 index, u64 down_route, u8 down_adapter,
                              u64 up_route, u8 up_adapter)
{
    if (index >= ACIO_RUNTIME_PORT_COUNT)
        return -1;
    acio_type5_runtime_t *runtime = &acio_runtime[index];
    if (runtime->phase < ACIO_TYPE5_RUNTIME_ROUTER_READY)
        return acio_fail(runtime, ACIO_TYPE5_E_BAD_PHASE, runtime->phase);

    /* Order is Apple's, and it is NOT symmetric with teardown:
     *   Tx path -> Rx path -> UP adapter enable -> 100 ms -> DOWN enable.
     * The settle sits BETWEEN the two adapter enables, not between path
     * activation and the first enable. */
    if (acio_usb3_program_hop(runtime, index, down_route, down_adapter,
                              ACIO_TYPE5_USB3_HOP, up_adapter,
                              ACIO_TYPE5_USB3_HOP,
                              ACIO_TYPE5_CREDITS_HOST_SIDE, true) < 0)
        return -1;
    if (acio_usb3_program_hop(runtime, index, up_route, up_adapter,
                              ACIO_TYPE5_USB3_HOP, down_adapter,
                              ACIO_TYPE5_USB3_HOP,
                              ACIO_TYPE5_CREDITS_DEVICE_SIDE, true) < 0)
        return -1;

    if (acio_type5_config_write32(index, up_route, up_adapter,
                                  ACIO_TYPE5_CONFIG_ADAPTER, 0,
                                  ACIO_TYPE5_USB3_ENABLE) < 0)
        return acio_fail_keep_specific(runtime, ACIO_TYPE5_E_TUNNEL_STATE, 0x10);
    mdelay(ACIO_TYPE5_USB3_SETTLE_MS);
    if (acio_type5_config_write32(index, down_route, down_adapter,
                                  ACIO_TYPE5_CONFIG_ADAPTER, 0,
                                  ACIO_TYPE5_USB3_ENABLE) < 0)
        return acio_fail_keep_specific(runtime, ACIO_TYPE5_E_TUNNEL_STATE, 0x11);

    runtime->phase = ACIO_TYPE5_RUNTIME_TUNNEL_READY;
    printf("acio%u: USB3 tunnel active (down adapter %u, up adapter %u); "
           "PIPE still DUMMY\n", index, down_adapter, up_adapter);
    return 0;
}

/* Enable or disable one USB3 protocol adapter at its DISCOVERED capability.
 *
 * Read-modify-write over two control packets, because m1n1 has no equivalent of
 * Apple's configModifyDWordWithMask primitive: read the capability dword, clear
 * [31:30], OR in the requested state, write it back. Apple writes bit 30 as 1
 * in BOTH the enable (0xC0000000) and disable (0x40000000) cases; that is
 * preserved rather than "simplified" to a single bit. */
static int acio_usb3_adapter_enable(acio_type5_runtime_t *runtime, u32 index,
                                    u64 route, u8 adapter, u16 cap_offset,
                                    bool enable)
{
    u32 value = 0;
    if (acio_type5_config_read32(index, route, adapter,
                                 ACIO_TYPE5_CONFIG_ADAPTER, cap_offset,
                                 &value) < 0)
        return acio_fail_keep_specific(runtime, ACIO_TYPE5_E_TUNNEL_STATE,
                                       (u32)adapter << 16 | cap_offset);

    u32 next = (value & ~ACIO_TYPE5_USB3_ADAPTER_MASK) |
               (enable ? ACIO_TYPE5_USB3_ENABLE : ACIO_TYPE5_USB3_DISABLE);
    if (acio_type5_config_write32(index, route, adapter,
                                  ACIO_TYPE5_CONFIG_ADAPTER, cap_offset,
                                  next) < 0)
        return acio_fail_keep_specific(runtime, ACIO_TYPE5_E_TUNNEL_STATE,
                                       (u32)adapter << 16 | 0xe000u);
    printf("acio%u: USB3 adapter %u @route %#llx cap %#x: %#x -> %#x (%s)\n",
           index, adapter, (unsigned long long)route, cap_offset, value, next,
           enable ? "enable" : "disable");
    return 0;
}

int acio_type5_usb3_tunnel_device_up(u32 index, u8 host_lane_adapter,
                                     u8 host_usb3_down_adapter)
{
    if (index >= ACIO_RUNTIME_PORT_COUNT)
        return -1;
    acio_type5_runtime_t *runtime = &acio_runtime[index];
    if (runtime->phase < ACIO_TYPE5_RUNTIME_ROUTER_READY)
        return acio_fail(runtime, ACIO_TYPE5_E_BAD_PHASE, runtime->phase);

    /* DISCOVER the far end. Nothing below assumes an adapter number. */
    u64 device_route = 0;
    u32 device_depth = 0;
    u8 usb3_up = 0;
    u8 device_upstream = 0;
    if (acio_type5_scan_device_router(index, host_lane_adapter, &device_route,
                                      &device_depth, &usb3_up,
                                      &device_upstream) < 0)
        return -1;

    printf("acio%u: device router route %#llx depth %u: USB3 Up adapter %u, "
           "upstream (lane) adapter %u; host lane %u, host USB3 Down %u\n",
           index, (unsigned long long)device_route, device_depth, usb3_up,
           device_upstream, host_lane_adapter, host_usb3_down_adapter);

    /* Locate both protocol-adapter capabilities BEFORE programming anything:
     * a tunnel we cannot enable is not worth half-building. */
    u16 host_cap = 0, device_cap = 0;
    if (acio_find_adapter_capability(runtime, index, 0, host_usb3_down_adapter,
                                     ACIO_TYPE5_ADP_CAP_MASK,
                                     ACIO_TYPE5_USB3_ADP_CAP_VAL,
                                     ACIO_TYPE5_E_TUNNEL_ADAPTER_CAP,
                                     &host_cap) < 0 ||
        acio_find_adapter_capability(runtime, index, device_route, usb3_up,
                                     ACIO_TYPE5_ADP_CAP_MASK,
                                     ACIO_TYPE5_USB3_ADP_CAP_VAL,
                                     ACIO_TYPE5_E_TUNNEL_ADAPTER_CAP,
                                     &device_cap) < 0)
        return -1;

    /* FOUR hop descriptors, two per router -- the shape a real path has and
     * the host-internal pairing never did.
     *
     *   Tx (host -> device):  host  USB3-Down --hop8--> host  lane
     *                         device lane     --hop8--> device USB3-Up
     *   Rx (device -> host):  device USB3-Up  --hop8--> device lane
     *                         host  lane      --hop8--> host  USB3-Down
     *
     * Hop ID 8 in both directions (`setSourceHopIDRange(0x00080008)`); the two
     * hops on a router never collide because they are keyed by (adapter, hop).
     *
     * Credits follow getInitialCreditsForPathTableIndex: the hop where a path
     * STARTS takes SourceInitialCredits (2), the hop where it ENDS takes
     * DestinationInitialCredits (14). Grade B -- the 2/14 literals are grade A
     * from createPaths, their per-hop assignment is derived from the path
     * model, not read off a trace. */
    if (acio_usb3_program_hop(runtime, index, 0, host_usb3_down_adapter,
                              ACIO_TYPE5_USB3_HOP, host_lane_adapter,
                              ACIO_TYPE5_USB3_HOP,
                              ACIO_TYPE5_CREDITS_HOST_SIDE, true) < 0 ||
        acio_usb3_program_hop(runtime, index, device_route, device_upstream,
                              ACIO_TYPE5_USB3_HOP, usb3_up,
                              ACIO_TYPE5_USB3_HOP,
                              ACIO_TYPE5_CREDITS_DEVICE_SIDE, true) < 0 ||
        acio_usb3_program_hop(runtime, index, device_route, usb3_up,
                              ACIO_TYPE5_USB3_HOP, device_upstream,
                              ACIO_TYPE5_USB3_HOP,
                              ACIO_TYPE5_CREDITS_HOST_SIDE, true) < 0 ||
        acio_usb3_program_hop(runtime, index, 0, host_lane_adapter,
                              ACIO_TYPE5_USB3_HOP, host_usb3_down_adapter,
                              ACIO_TYPE5_USB3_HOP,
                              ACIO_TYPE5_CREDITS_DEVICE_SIDE, true) < 0)
        return -1;

    /* Apple's order, and it is NOT symmetric with PCIe: UP first, 100 ms,
     * then DOWN (activateInternal @0xfffffe0009d568ec/68/9b8). */
    if (acio_usb3_adapter_enable(runtime, index, device_route, usb3_up,
                                 device_cap, true) < 0)
        return -1;
    mdelay(ACIO_TYPE5_USB3_SETTLE_MS);
    if (acio_usb3_adapter_enable(runtime, index, 0, host_usb3_down_adapter,
                                 host_cap, true) < 0)
        return -1;

    runtime->phase = ACIO_TYPE5_RUNTIME_TUNNEL_READY;
    printf("acio%u: end-to-end USB3 tunnel programmed (host lane %u <-> device "
           "route %#llx USB3-Up %u, 4 hops, caps %#x/%#x); PIPE still DUMMY. "
           "This means every step completed against a router that answered -- "
           "it is NOT evidence that a device enumerated.\n",
           index, host_lane_adapter, (unsigned long long)device_route, usb3_up,
           host_cap, device_cap);
    return 0;
}

int acio_type5_usb3_tunnel_down(u32 index, u64 down_route, u8 down_adapter,
                                u64 up_route, u8 up_adapter)
{
    if (index >= ACIO_RUNTIME_PORT_COUNT)
        return -1;
    acio_type5_runtime_t *runtime = &acio_runtime[index];
    int result = 0;

    /* Teardown is deliberately NOT the mirror of bring-up:
     *  - adapters are disabled DOWN first, then UP;
     *  - paths are deactivated Rx -> Tx, the opposite of activation's
     *    Tx -> Rx (proven from the driver's own log strings, not inferred
     *    from field offsets, which transpose easily);
     *  - there is NO delay on the disable side at all;
     *  - there is NO counter-release step: the counter id is bound at
     *    hop-id allocate time and dies with the hop-id release.
     * Every step continues after failure so a partial teardown still
     * quiesces as much as possible. */
    if (acio_type5_config_write32(index, down_route, down_adapter,
                                  ACIO_TYPE5_CONFIG_ADAPTER, 0, 0) < 0)
        result = -1;
    if (acio_type5_config_write32(index, up_route, up_adapter,
                                  ACIO_TYPE5_CONFIG_ADAPTER, 0, 0) < 0)
        result = -1;
    if (acio_usb3_program_hop(runtime, index, up_route, up_adapter,
                              ACIO_TYPE5_USB3_HOP, down_adapter,
                              ACIO_TYPE5_USB3_HOP, 0, false) < 0)
        result = -1;
    if (acio_usb3_program_hop(runtime, index, down_route, down_adapter,
                              ACIO_TYPE5_USB3_HOP, up_adapter,
                              ACIO_TYPE5_USB3_HOP, 0, false) < 0)
        result = -1;

    if (runtime->phase == ACIO_TYPE5_RUNTIME_TUNNEL_READY)
        runtime->phase = ACIO_TYPE5_RUNTIME_ROUTER_READY;
    return result;
}

int acio_type5_pipe_commit(u32 index)
{
    if (index >= ACIO_RUNTIME_PORT_COUNT)
        return -1;
    acio_type5_runtime_t *runtime = &acio_runtime[index];

    /* The commit is the LAST step and only legal from TUNNEL_READY.  Gating on
     * the phase here is deliberate: on T6020 the pipehandler mux lives in the
     * usb-drd (DWC3) node, so 0x11 steers the DWC3 SuperSpeed PIPE producer to
     * the ACIO host router (atcphy_core.c:951-955 "the selected producer is
     * the ACIO host router").  Committing it before a tunnel exists would
     * point the DWC3 PIPE at a router with no live path. */
    if (runtime->phase != ACIO_TYPE5_RUNTIME_TUNNEL_READY)
        return acio_fail(runtime, ACIO_TYPE5_E_BAD_PHASE, runtime->phase);

    /* atcphy_commit_routed_pipe already reads MUX_CTRL back internally and
     * fails unless it reads 0x11.  Re-read here independently so the runtime
     * records the OBSERVED mux value in its own telemetry and the phase
     * advance is gated on a value this layer saw, not on a boolean handed up
     * from another module. */
    if (atcphy_commit_routed_pipe(index) < 0)
        return acio_fail(runtime, ACIO_TYPE5_E_PIPE_COMMIT, 0);

    u32 mux = 0;
    if (atcphy_read_pipe_mux(index, &mux) < 0)
        return acio_fail(runtime, ACIO_TYPE5_E_PIPE_READBACK, 0);
    if (mux != ATCPHY_PIPEHANDLER_MUX_VALUE_USB4_TUNNEL)
        return acio_fail(runtime, ACIO_TYPE5_E_PIPE_READBACK, mux);

    runtime->phase = ACIO_TYPE5_RUNTIME_PIPE_COMMITTED;
    printf("acio%u: routed USB4 PIPE committed (MUX_CTRL=%#x); phase PIPE_COMMITTED. "
           "NOTE this only means the DWC3 PIPE mux now reads 0x11 -- it is NOT a "
           "claim that a device enumerated.\n",
           index, mux);
    return 0;
}

/* Bounded config-space poll. Apple's blocking poll helper re-issues a full
 * config read per iteration rather than caching, so the transport error path
 * stays the same as a normal read. */
static int acio_router_poll(acio_type5_runtime_t *runtime,
                            const acio_type5_router_action_t *action, u32 *value)
{
    u64 timeout = timeout_calculate(ACIO_ROUTER_POLL_TIMEOUT_US);
    for (;;) {
        if (acio_type5_config_read32(runtime->index, action->route, action->port,
                                     action->space, action->offset, value) < 0)
            return -1;
        if ((*value & action->mask) == action->value)
            return 0;
        if (timeout_expired(timeout))
            return -1;
        udelay(ACIO_ROUTER_POLL_INTERVAL_US);
    }
}

int acio_type5_router_configure(u32 index, u64 route, bool all_parents_support_usb)
{
    if (index >= ACIO_RUNTIME_PORT_COUNT)
        return -1;

    acio_type5_runtime_t *runtime = &acio_runtime[index];
    if (runtime->phase != ACIO_TYPE5_RUNTIME_CONTROL_READY &&
        runtime->phase != ACIO_TYPE5_RUNTIME_ROUTER_READY)
        return acio_fail(runtime, ACIO_TYPE5_E_BAD_PHASE, runtime->phase);
    if (acio_type5_router_start(&runtime->router, route, all_parents_support_usb) < 0)
        return acio_fail(runtime, ACIO_TYPE5_E_ROUTER_STATE, 0);

    for (u32 step = 0; step < ACIO_ROUTER_MAX_STEPS; step++) {
        acio_type5_router_action_t action;
        u32 value = 0;

        if (acio_type5_router_next(&runtime->router, &action) < 0)
            return acio_fail(runtime, ACIO_TYPE5_E_ROUTER_STATE, runtime->router.state);

        switch (action.kind) {
            case ACIO_TYPE5_ROUTER_DONE:
                /* The host router's real programming is the CS_1..CS_4
                 * topology block, written as one length-4 transaction after
                 * a length-5 read of CS_0..CS_4 (CS_1 and CS_4 are
                 * read-modify-write).  configureRouter itself only proves
                 * readiness for route 0. */
                if (route == 0) {
                    u32 cs[5];
                    u32 topology[4];
                    if (acio_type5_config_read_block(index, 0, 0,
                                                     ACIO_TYPE5_CONFIG_ROUTER, 0, 5,
                                                     cs) < 0)
                        return acio_fail_keep_specific(runtime,
                                                       ACIO_TYPE5_E_ROUTER_READ, 0);
                    if (acio_type5_router_topology_pack(cs, ACIO_TYPE5_CM_VERSION,
                                                        topology) < 0)
                        return acio_fail(runtime, ACIO_TYPE5_E_ROUTER_STATE, 0);
                    if (acio_type5_config_write_block(index, 0, 0,
                                                      ACIO_TYPE5_CONFIG_ROUTER, 1, 4,
                                                      topology) < 0)
                        return acio_fail_keep_specific(runtime,
                                                       ACIO_TYPE5_E_ROUTER_WRITE, 1);
                    printf("acio%u: host router topology programmed "
                           "(CS_1=%#x CS_3=%#x CS_4=%#x)\n",
                           index, topology[0], topology[2], topology[3]);
                }
                runtime->phase = ACIO_TYPE5_RUNTIME_ROUTER_READY;
                printf("acio%u: Type5 root router configured (dword5=%#x); "
                       "no tunnel, PIPE remains on DUMMY\n",
                       index, runtime->router.config_value);
                return 0;
            case ACIO_TYPE5_ROUTER_POLL:
                if (acio_router_poll(runtime, &action, &value) < 0)
                    return acio_fail_keep_specific(
                        runtime,
                        runtime->router.state == 0
                            ? ACIO_TYPE5_E_ROUTER_POLL_READY
                            : ACIO_TYPE5_E_ROUTER_POLL_ACK,
                        value);
                break;
            case ACIO_TYPE5_ROUTER_READ:
                if (acio_type5_config_read32(index, action.route, action.port,
                                             action.space, action.offset, &value) < 0)
                    return acio_fail(runtime, ACIO_TYPE5_E_ROUTER_READ, action.offset);
                break;
            case ACIO_TYPE5_ROUTER_WRITE:
                if (acio_type5_config_write32(index, action.route, action.port,
                                              action.space, action.offset,
                                              action.value) < 0)
                    return acio_fail(runtime, ACIO_TYPE5_E_ROUTER_WRITE, action.value);
                value = action.value;
                break;
        }

        if (acio_type5_router_complete(&runtime->router, 0, value) < 0)
            return acio_fail(runtime, ACIO_TYPE5_E_ROUTER_STATE,
                             (u32)runtime->router.state << 16 | (value >> 16));
    }
    return acio_fail(runtime, ACIO_TYPE5_E_ROUTER_STATE, ACIO_ROUTER_MAX_STEPS);
}

/* T6020 CIO reconfiguration pulse.
 *
 * PROVENANCE, and why the constants here are NOT the ones in the T6050
 * BootKC: IOKit `IONameMatch` is an exact string, not a family match.
 * AppleT6050PMGR matches "pmgr1,t6050" and AppleT6020PMGR matches
 * "pmgr1,t6020", so a T6020 platform never instantiates the T6050 class and
 * its register map does not transfer.  Decoded from
 * AppleT6020PMGR::enableCioReconfig in a genuine RELEASE_ARM64_T6020
 * kernelcache:
 *
 *   die       = index > 3
 *   slot      = index - 4 * die
 *   req_bit   = 1 << slot                (T6050 used 1 << (2*slot))
 *   done_mask = 1 << (16 + slot)         (T6050 used 2 << (2*slot))
 *   register  = PMGR RegMap 0 + 0xa02c   (T6050 used 0x20060)
 *
 * RegMap 0 is ADT `/arm-io/pmgr` reg index 0, from
 * AppleT6020PMGR::initRegMaps registering (adt_reg_index 0, RegMap 0) first.
 *
 * Sequence: wait for done_mask to CLEAR, plain-store req_bit (not a
 * read-modify-write -- 0xa02c is absent from the forced-wake workaround
 * table), then wait for done_mask to CLEAR again.  There is no clear-down
 * write; the request bit self-clears.  Apple panics on the second timeout;
 * we fail the bring-up with bounded telemetry instead. */
#define ACIO_CIO_RECONFIG_REG 0xa02cu
#define ACIO_CIO_RECONFIG_PREWAIT_US 8000u
#define ACIO_CIO_RECONFIG_DONE_US 10000000u

/* Resolve which SoCTuner device-set, and therefore which reconfiguration
 * slot, belongs to this ACIO instance.  Stock raises the virtual PMGR device
 * named in `/arm-io/acioN` clock-gates[3] (CIO<N>_RECONFIG-V); SoCTuner sees
 * that device's status bit go 0 -> 1 and pulses the reconfiguration index
 * equal to the device-set array position.  Deriving the slot from the live
 * ADT rather than hardcoding keeps this correct per ACIO instance, which
 * matters because the end state runs two complexes concurrently. */
static int acio_reconfig_slot(u32 index, u32 *slot_out)
{
    char path[32];
    snprintf(path, sizeof(path), "/arm-io/acio%u", index);

    int acio_node = adt_path_offset(adt, path);
    u32 size = 0;
    const u32 *gates = acio_node < 0 ? NULL
                                     : adt_getprop(adt, acio_node, "clock-gates", &size);
    if (!gates || size < 4 * sizeof(u32))
        return -1;

    /* clock-gates[3] is this complex's reconfiguration device.  Resolve its
     * PMGR name and take the slot from the name, e.g. CIO1_RECONFIG-V -> 1.
     *
     * MEASURED: the live J414s ADT has NO /arm-io/soc-tuner node at all and
     * no `device-set-*` property anywhere, so the slot cannot be derived from
     * a device-set array position on this machine.  The PMGR device table
     * does carry the answer unambiguously: ids 530..533 are named
     * CIO0_RECONFIG-V .. CIO3_RECONFIG-V, all virtual, and
     * /arm-io/acio{0,1,2} clock-gates[3] are 530, 531 and 532 respectively.
     *
     * Deriving from the NAME rather than from (id - 530) keeps this honest:
     * if a future part renumbers the devices, the name still says which CIO
     * cluster it is, and an unparsable name fails closed instead of pulsing
     * an arbitrary slot. */
    u16 device_id = (u16)(gates[3] & 0xffff);
    char name[24];
    if (pmgr_device_name_by_id(device_id, name, sizeof(name)) < 0)
        return -1;
    if (name[0] != 'C' || name[1] != 'I' || name[2] != 'O' ||
        name[3] < '0' || name[3] > '9')
        return -1;
    u32 slot = (u32)(name[3] - '0');
    /* Require the rest to actually be the reconfiguration device, not some
     * other CIO<N>_* device that happens to sit at this index. */
    if (strncmp(name + 4, "_RECONFIG", 9) != 0)
        return -1;

    *slot_out = slot;
    return 0;
}

static int acio_cio_reconfig(acio_type5_runtime_t *runtime, u32 index)
{
    u32 reconfig_index;
    if (acio_reconfig_slot(index, &reconfig_index) < 0)
        return acio_fail(runtime, ACIO_TYPE5_E_CIO_RECONFIG, 0xffffffff);

    u32 die = reconfig_index > 3 ? 1 : 0;
    u32 slot = reconfig_index - 4 * die;
    if (die != 0)
        return acio_fail(runtime, ACIO_TYPE5_E_CIO_RECONFIG, reconfig_index);

    int trace[8];
    u64 base;
    if (adt_path_offset_trace(adt, "/arm-io/pmgr", trace) < 0 ||
        adt_get_reg(adt, trace, "reg", 0, &base, NULL) < 0)
        return acio_fail(runtime, ACIO_TYPE5_E_CIO_RECONFIG, 1);

    u64 addr = base + ACIO_CIO_RECONFIG_REG;
    u32 done_mask = 1u << (16 + slot);
    u32 req_bit = 1u << slot;

    if (poll32(addr, done_mask, 0, ACIO_CIO_RECONFIG_PREWAIT_US) < 0)
        printf("acio%u: CIO reconfig slot %u busy before request (%#x)\n", index,
               slot, read32(addr));
    write32(addr, req_bit);
    if (poll32(addr, done_mask, 0, ACIO_CIO_RECONFIG_DONE_US) < 0)
        return acio_fail(runtime, ACIO_TYPE5_E_CIO_RECONFIG, read32(addr));

    printf("acio%u: CIO cluster reconfigured (slot %u)\n", index, slot);
    return 0;
}

static int acio_runtime_power_initial(u32 index)
{
    char path[32];
    snprintf(path, sizeof(path), "/arm-io/acio%u", index);
    /* NHI logically requests indices 0, 1 and 2, but ApplePMGR vote/dependency
     * accounting keeps parent CIO (0) up until CIO_PCIE (1) and CIO_USB (2)
     * have dropped. Its effective TARGET/ACTUAL order is therefore 1, 2, 0.
     * m1n1 has no vote engine, so issue that proven physical order explicitly.
     * VDD_CIO remains owned by the USB-AON PMGR synchronization path. */
    pmgr_adt_power_disable_index(path, 1);
    int result = pmgr_adt_power_disable_index(path, 2);
    if (pmgr_adt_power_disable_index(path, 0) < 0)
        result = -1;
    if (result < 0) {
        printf("acio%u: Apple-equivalent state 5 dependency transition failed\n",
               index);
        return -1;
    }
    return 0;
}

static int acio_runtime_power_running(u32 index)
{
    char path[32];
    snprintf(path, sizeof(path), "/arm-io/acio%u", index);
    /* AppleARMIODevice state 7 maps to action 1 and PMGR target ACTIVE.
     * enableEmbeddedCPU issues indices 0, 1, 2 and logical index 4. On the
     * J414s ADT there are four clock-gates, so index 4 is deliberately OOB;
     * its unsupported result is discarded. It is not ADT entry 3 and does
     * not itself name CIO_RECONFIG. Apple's separate SoCTuner notification
     * consequence must be matched at the PMGR boundary, not by aliasing this
     * index. Apple preserves only index 0's result and discards the results
     * for 1, 2 and 4. */
    int result = pmgr_adt_power_enable_index(path, 0);
    pmgr_adt_power_enable_index(path, 1);
    pmgr_adt_power_enable_index(path, 2);
    if (result < 0) {
        printf("acio%u: Apple state 7 failed for clock-gate 0\n", index);
        return -1;
    }
    return 0;
}

static int acio_runtime_power_off(u32 index)
{
    char path[32];
    int result = 0;
    snprintf(path, sizeof(path), "/arm-io/acio%u", index);
    /* Match ApplePMGR's child-first physical down-transition order. */
    static const u8 order[] = {1, 2, 0};
    for (u32 i = 0; i < ARRAY_SIZE(order); i++)
        if (pmgr_adt_power_disable_index(path, order[i]) < 0)
            result = -1;
    return result;
}

static int acio_type5_copy_firmware(const acio_resources_t *resources,
                                    const acio_type5_fw_bundle_view_t *view)
{
    for (u32 i = 0; i < ACIO_TYPE5_FW_BLOB_COUNT; i++) {
        u64 destination;
        if (acio_type5_fw_destination(resources->nhi_base,
                                      (enum acio_type5_fw_blob_kind)i,
                                      &destination) < 0)
            return -1;
        memcpy((void *)destination, view->data[i], view->size[i]);
        dc_cvac_range((void *)destination, view->size[i]);
    }
    dma_wmb();
    return 0;
}

static int acio_type5_release_firmware(const acio_resources_t *resources)
{
    /* Six writes land in ADT-described windows.  The final +0x200008 write
     * is Apple's exact Type5 manual-loader release for the second firmware
     * core; the T6020 ADT does not describe that wrapper separately. */
    if (resources->asc_mailbox_base > UINT64_MAX - 0x200008)
        return -1;

    write32(resources->asc_mailbox_base + 0x120, 0);
    write32(resources->asc_mailbox_base + 0x124, 0x40000);
    write32(resources->rc_base + 0x74, 1);
    write32(resources->asc_mailbox_base + 0x80, 1);
    write32(resources->asc_mailbox_base + 0xe0, 1);
    write32(resources->asc_mailbox_base + 0x08, 1);
    write32(resources->asc_mailbox_base + 0x200008, 1);
    dma_wmb();
    return 0;
}

int acio_type5_firmware_start(u32 index, const void *bundle, size_t bundle_size,
                              bool flipped, bool thunderbolt_mode,
                              u32 diagnostic_stop)
{
    if (index >= ACIO_RUNTIME_PORT_COUNT)
        return -1;

    acio_type5_runtime_t *runtime = &acio_runtime[index];
    runtime->index = index;
    /* Bump before any early return, including the bad-phase rejection, so a
     * caller can always tell that its request was seen at all. */
    runtime->run_id++;
    runtime->last_error = ACIO_TYPE5_E_NONE;
    runtime->last_error_detail = 0;
    if (runtime->phase != ACIO_TYPE5_RUNTIME_OFF &&
        runtime->phase != ACIO_TYPE5_RUNTIME_FAILED)
        return acio_fail(runtime, ACIO_TYPE5_E_BAD_PHASE, runtime->phase);

    if (acio_discover_resources(index, &runtime->resources) < 0)
        return acio_fail(runtime, ACIO_TYPE5_E_RESOURCES, 0);

    acio_type5_fw_bundle_view_t view = {0};
    if (!runtime->resources.firmware_preloaded_running &&
        acio_type5_fw_bundle_validate(bundle, bundle_size, &view) < 0)
        return acio_fail(runtime, ACIO_TYPE5_E_FW_BUNDLE, (u32)bundle_size);

    /* Stop 6: resource discovery only.  Everything above this point is ADT
     * parsing and validation -- no PMGR transition, no MMIO, no DART, no PHY.
     * It is the deepest stop that still leaves the machine exactly as it was
     * found, so the phase stays OFF rather than becoming FAILED. */
    if (diagnostic_stop == 6) {
        runtime->phase = ACIO_TYPE5_RUNTIME_OFF;
        return 0;
    }

    runtime->phase = ACIO_TYPE5_RUNTIME_FAILED;
    /* Apple's ACIO wake contract first applies power state 5 (PWRGATE) to
     * domains 0/1/2, asks the ATC hardware and PHY to enter their routed
     * state, and only then applies state 7 (ACTIVE) from the embedded-CPU
     * enable path. This reset/quiesce transition is mandatory on T6020. */
    if (acio_runtime_power_initial(index) < 0)
        goto fail;
    if (diagnostic_stop == 2)
        return 0;

    atcphy_mode_t mode = thunderbolt_mode ? ATCPHY_MODE_TBT : ATCPHY_MODE_USB4;
    if (atcphy_prepare_routed_mode(index, mode, flipped) < 0)
        goto fail;
    runtime->phase = ACIO_TYPE5_RUNTIME_PHY_PREPARED;
    if (diagnostic_stop == 1)
        return 0;

    if (acio_runtime_power_running(index) < 0)
        goto fail;
    /* Stock raises the virtual CIO<N>_RECONFIG-V device after the real CIO
     * gates converge to PS_ON, and SoCTuner then pulses the reconfiguration
     * slot synchronously, inside the same setDevicePowerState call.  m1n1 has
     * no SoCTuner and the virtual device writes no register, so the pulse
     * must be issued directly, here, at that same boundary. */
    if (acio_cio_reconfig(runtime, index) < 0)
        goto fail;
    runtime->phase = ACIO_TYPE5_RUNTIME_POWERED;
    if (diagnostic_stop == 3)
        return 0;

    if (runtime->resources.firmware_preloaded_running) {
        /* This is Apple's stock path: iBoot/RTBuddy supplied and started the
         * firmware described by the nub.  Adopt it instead of writing live
         * SRAM.  diagnostic_stop 4/5 retain their phase boundaries without
         * performing the manual copy/release operations. */
        printf("acio%u: adopting iBoot-preloaded running Type5 firmware\n", index);
        if (diagnostic_stop == 4 || diagnostic_stop == 5)
            return 0;
    } else {
        if (acio_type5_copy_firmware(&runtime->resources, &view) < 0)
            goto fail;
        if (diagnostic_stop == 4)
            return 0;
        if (acio_type5_release_firmware(&runtime->resources) < 0)
            goto fail;
        if (diagnostic_stop == 5)
            return 0;

        /* Apple's manual-loader branch settles for 250 ms before beginning
         * the 5 ms cadence firmware-state poll. */
        mdelay(250);
    }
    /* Sample once before polling and compare at the end.  A readiness field
     * that NEVER changed over the whole budget is evidence that nothing is
     * running to change it -- i.e. no firmware image survived -- which is a
     * completely different diagnosis from "firmware is booting but slow".
     * Measured on this target, all three ACIO complexes are PWRGATEd before
     * m1n1 touches them (m1n1's own pmgr_init never gates: it contains no
     * PMGR_PS_PWRGATE and only raises parents of active devices), so the
     * nub's `pre-loaded`/`running` ADT properties describe iBoot's intent,
     * not live coprocessor state.  Distinguishing these two cases here is
     * what lets a single bring-up run decide whether the external
     * manual-loader bundle path is required. */
    u32 first_state = read32(runtime->resources.rc_base + ACIO_TYPE5_FW_STATE);
    u32 state = first_state;
    bool changed = false;
    u64 ready_timeout = timeout_calculate(ACIO_TYPE5_FW_READY_TIMEOUT_US);
    while ((state & ACIO_TYPE5_FW_STATE_MASK) != ACIO_TYPE5_FW_STATE_READY &&
           !timeout_expired(ready_timeout)) {
        mdelay(5);
        state = read32(runtime->resources.rc_base + ACIO_TYPE5_FW_STATE);
        if (state != first_state)
            changed = true;
    }
    if ((state & ACIO_TYPE5_FW_STATE_MASK) != ACIO_TYPE5_FW_STATE_READY) {
        if (first_state == 0xffffffffu && !changed) {
            acio_fail(runtime, ACIO_TYPE5_E_FW_BUS_ERROR, first_state);
            goto fail;
        }
        acio_fail(runtime, changed ? ACIO_TYPE5_E_FW_READY_TIMEOUT
                                   : ACIO_TYPE5_E_FW_ABSENT,
                  state);
        goto fail;
    }

    if (acio_apply_tunables(&runtime->resources) < 0)
        goto fail;

    runtime->phase = ACIO_TYPE5_RUNTIME_FW_READY;
    if (acio_control_start(runtime) < 0)
        goto fail;
    runtime->phase = ACIO_TYPE5_RUNTIME_CONTROL_READY;
    printf("acio%u: Type5 firmware and control transport ready; routed PIPE remains on DUMMY\n",
           index);
    return 0;

fail:
    /* Keep the routed PHY parked on DUMMY after a bounded bring-up failure.
     * Powering the Type-C PHY off here can also remove m1n1's live proxy
     * transport, hiding the useful failure result from the caller. */
    acio_control_disable(runtime);
    acio_control_free(&runtime->control);
    {
        char dart_path[32];
        snprintf(dart_path, sizeof(dart_path), "/arm-io/dart-acio%u", index);
        acio_dart_force_active(runtime, dart_path, false);
    }
    acio_runtime_power_off(index);
    runtime->phase = ACIO_TYPE5_RUNTIME_FAILED;
    return -1;
}

int acio_type5_runtime_abort(u32 index)
{
    if (index >= ACIO_RUNTIME_PORT_COUNT)
        return -1;

    acio_type5_runtime_t *runtime = &acio_runtime[index];
    char dart_path[32];

    runtime->index = index;
    snprintf(dart_path, sizeof(dart_path), "/arm-io/dart-acio%u", index);

    /* Order matters: stop the rings, then remove the DART mappings, and only
     * then release the DART clock vote.  Apple issues Fact(0) first in
     * setHWState because its own teardown guarantees no NHI DMA is in
     * flight by that point; here DMA is only provably quiesced after the
     * ring disable-done handshake and dart_unmap, so the vote is dropped
     * last.  Gating the DART clock with live mappings would be silently
     * unsafe -- Apple's force-inactive flag makes its own code skip the
     * "is any mapper still enabled?" scan. */
    int result = acio_control_disable(runtime);
    acio_control_free(&runtime->control);
    if (acio_dart_force_active(runtime, dart_path, false) < 0)
        result = -1;
    if (acio_runtime_power_off(index) < 0)
        result = -1;
    if (atcphy_abort_routed_mode(index) < 0)
        result = -1;
    /* Preserve run_id across the wipe: it is the caller's only way to tell a
     * fresh result from a stale one, and an abort in the middle of a ladder
     * walk must not silently reset it. */
    u32 preserved_run_id = runtime->run_id;
    memset(runtime, 0, sizeof(*runtime));
    runtime->run_id = preserved_run_id;
    runtime->index = index;
    return result;
}

enum acio_type5_runtime_phase acio_type5_runtime_phase(u32 index)
{
    if (index >= ACIO_RUNTIME_PORT_COUNT)
        return ACIO_TYPE5_RUNTIME_FAILED;
    return acio_runtime[index].phase;
}
