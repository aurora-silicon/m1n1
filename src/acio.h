/* SPDX-License-Identifier: MIT */
#ifndef ACIO_H
#define ACIO_H

#ifdef ACIO_HOST_TEST
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
#else
#include "types.h"
#endif

#define ACIO_NHI_RING_COUNT 12
#define ACIO_NHI_IRQ_COUNT  (ACIO_NHI_RING_COUNT * 2)
#define ACIO_SRAM_IOVA_BASE 0x10000000ULL

/* T6020 Type5 register contract from the local BootKC RE corpus.  These are
 * not the packed 0x20-stride t8103 NHI registers. */
#define ACIO_TYPE5_TX_RING_BASE       0x10000u
#define ACIO_TYPE5_RX_RING_BASE       0x80000u
#define ACIO_TYPE5_RING_STRIDE        0x4000u
#define ACIO_TYPE5_RING_COUNT_OFFSET  0x0cu
#define ACIO_TYPE5_RING_CONTROL       0x10u
#define ACIO_TYPE5_TX_DISABLE_DONE    0x1cu
#define ACIO_TYPE5_RX_DISABLE_DONE    0x18u
#define ACIO_TYPE5_FW_CONTROL         0x0cu
#define ACIO_TYPE5_FW_STATE           0xa8u
#define ACIO_TYPE5_FW_STATE_MASK      0x7f000000u
#define ACIO_TYPE5_FW_STATE_READY     0x01000000u

#define ACIO_TYPE5_DESC_COMPLETE      0x00200000u
#define ACIO_TYPE5_DESC_SW_SEED       0x00400000u
#define ACIO_TYPE5_RING_ENABLE        0x80000000u
#define ACIO_TYPE5_RX_RAW_MODE        0x40000000u
#define ACIO_TYPE5_CONTROL_FRAME_SIZE 0x100u
/* Hop 0.  An earlier revision moved the control ring to hop 1 on the
 * reasoning that Apple's shipping enabled-ring mask 0xffffffff_fffffffe
 * excludes ring 0, so ring 0 must be reserved for the acio-cpu coprocessor.
 * That inference was BACKWARDS.
 *
 * IOThunderboltControlPath::createTransmitter calls setFlags(1), and
 * ...RingManager::allocateTransmitRing @0xfffffe0009ce5c6c branches on that
 * flag with `tbnz w23,#0` straight to getObject(0) -- bypassing the mask
 * entirely.  RX is symmetric.  So the control path is HARD-WIRED to ring 0,
 * and the mask excludes ring 0 from the *generic* allocator precisely
 * because the control path has already claimed it.  There is no coprocessor
 * ownership of hop 0 anywhere in the host driver.
 *
 * createReceiver independently confirms the rest of our geometry: ring size
 * 0x11 = 17 entries, max frame 0x100 = 256 bytes. */
#define ACIO_TYPE5_CONTROL_RING       0u
/* TX +0x14 shared-buffer allocation, a RAW SCALAR -- confirmed not a packed
 * word.  configureSharedBuffer @0xfffffe0009d0c8b0 writes the zero-extended
 * u16 ring[0xd8] with no shift or mask.  allocateSharedBuffer splits a
 * 232-credit pool with a floor of 2 and ring 0 pre-deducted:
 *   nBig  = min((230 - 2L)/(H-2), L)   with L = paths-1, H = 40
 *   ring 0 -> 2 ; rings 1..5 -> 40 ; rings 6..11 -> 5   (total 232)
 * The split is independent of how many rings are enabled, so hop 0's share
 * is 2 regardless. */
#define ACIO_TYPE5_CONTROL_TX_CREDITS 2u
#define ACIO_TYPE5_CM_VERSION 0x10u

#define ACIO_TYPE5_FW_BUNDLE_MAGIC   0x354f4943u /* "CIO5", little-endian */
#define ACIO_TYPE5_FW_BUNDLE_VERSION 1u

enum acio_type5_fw_blob_kind {
    ACIO_TYPE5_FW_ACIO_TEXT = 0,
    ACIO_TYPE5_FW_ACIO_DATA = 1,
    ACIO_TYPE5_FW_TMU_TEXT = 2,
    ACIO_TYPE5_FW_TMU_DATA = 3,
    ACIO_TYPE5_FW_BLOB_COUNT = 4,
};

typedef struct acio_type5_fw_bundle_entry {
    u32 offset;
    u32 size;
    u32 crc32c;
    u32 reserved;
} acio_type5_fw_bundle_entry_t;

typedef struct acio_type5_fw_bundle_header {
    u32 magic;
    u32 version;
    u32 total_size;
    u32 header_size;
    acio_type5_fw_bundle_entry_t entries[ACIO_TYPE5_FW_BLOB_COUNT];
} acio_type5_fw_bundle_header_t;

typedef struct acio_type5_fw_bundle_view {
    const u8 *data[ACIO_TYPE5_FW_BLOB_COUNT];
    u32 size[ACIO_TYPE5_FW_BLOB_COUNT];
    u32 crc32c[ACIO_TYPE5_FW_BLOB_COUNT];
} acio_type5_fw_bundle_view_t;

enum acio_type5_pdf {
    ACIO_TYPE5_PDF_CONFIG_READ = 1,
    ACIO_TYPE5_PDF_CONFIG_WRITE = 2,
    ACIO_TYPE5_PDF_CONFIG_ERROR = 3,
    ACIO_TYPE5_PDF_NOTIFY_ACK = 4,
    /* RECEIVE-ONLY.  No transmit command class in IOThunderboltFamily uses
     * PDF 5 -- an exhaustive scan of all 28 config command classes shows
     * SOF/EOF only ever in {1,2,3,4,6,7,8,9,13}.  5 is the unsolicited
     * hot-plug event the router pushes onto the control ring
     * (IOThunderboltControlPath::fakePlugEvent carries route + port +
     * unplug).  It was missing from this enum because we only ever modelled
     * the transmit side, which is exactly why an inbound event was mistaken
     * for a malformed config reply. */
    ACIO_TYPE5_PDF_EVENT = 5,
    ACIO_TYPE5_PDF_XDOMAIN_REQUEST = 6,
    ACIO_TYPE5_PDF_XDOMAIN_RESPONSE = 7,
    ACIO_TYPE5_PDF_CM_OVERRIDE = 8,
    ACIO_TYPE5_PDF_RESET = 9,
    ACIO_TYPE5_PDF_PREPARE_SLEEP = 13,
};

enum acio_type5_config_space {
    ACIO_TYPE5_CONFIG_HOPS = 0,
    ACIO_TYPE5_CONFIG_ADAPTER = 1,
    ACIO_TYPE5_CONFIG_ROUTER = 2,
    ACIO_TYPE5_CONFIG_COUNTERS = 3,
};

/* Adapter type as published in ADT `portmap` and in ADP_CS_2 [23:0].  There
 * is only ONE adapter-type field; the IOKit "Adapter Type" property is that
 * value verbatim.  Family lives in bits [23:16], direction in bits [7:0]
 * with 0x01 = down/in and 0x02 = up/out.  Lane and host-interface adapters
 * are the bare scalars 1 and 2. */
#define ACIO_TYPE5_ADAPTER_INACTIVE 0x00000000u
#define ACIO_TYPE5_ADAPTER_LANE     0x00000001u
#define ACIO_TYPE5_ADAPTER_NHI      0x00000002u
#define ACIO_TYPE5_ADAPTER_FAM_DP   0x0eu
#define ACIO_TYPE5_ADAPTER_FAM_PCIE 0x10u
#define ACIO_TYPE5_ADAPTER_FAM_USB3 0x20u
#define ACIO_TYPE5_ADAPTER_FAM_USBT 0x21u
#define ACIO_TYPE5_ADAPTER_DIR_DOWN 0x01u
#define ACIO_TYPE5_ADAPTER_DIR_UP   0x02u
#define ACIO_TYPE5_ADAPTER_USB3_DOWN 0x00200101u
#define ACIO_TYPE5_ADAPTER_USB3_UP   0x00200102u

#define ACIO_TYPE5_MAX_ADAPTERS 32u

enum acio_type5_adapter_kind {
    ACIO_ADAPTER_INACTIVE = 0,
    ACIO_ADAPTER_LANE,
    ACIO_ADAPTER_NHI,
    ACIO_ADAPTER_DP,
    ACIO_ADAPTER_PCIE,
    ACIO_ADAPTER_USB3,
    ACIO_ADAPTER_USB_GEN_T,
    ACIO_ADAPTER_UNKNOWN,
};

typedef struct acio_type5_adapter {
    u32 raw;
    enum acio_type5_adapter_kind kind;
    bool up;      /* direction 0x02 = up/out; false means down/in */
    /* The three fields below are only meaningful when `defaults_valid` is
     * set.  They come from the adapter's port-default register, which some
     * callers (the device-router scan) do not read at all.
     *
     * The flag exists because zero is a LEGAL value for each of them, so a
     * zeroed struct cannot otherwise be told apart from a measured one that
     * happens to read zero -- "not read" and "read as 0" would be identical
     * in the type, distinguishable only by a comment at one call site. That
     * is the same defect as a boolean default standing in for a measurement:
     * absence has to be representable, not documented. */
    bool defaults_valid;
    u16 max_in_hop;
    u16 max_out_hop;
    u16 total_buffers;
} acio_type5_adapter_t;

/* `port_default` is a POINTER so that "I did not read it" is expressible at
 * the call site rather than smuggled in as a zero. NULL leaves the hop/buffer
 * fields zeroed AND clears `defaults_valid`. */
int acio_type5_adapter_decode(u32 raw, const u32 *port_default,
                              acio_type5_adapter_t *out);

typedef struct acio_type5_descriptor {
    u64 address;
    u32 metadata;
    u32 reserved;
} acio_type5_descriptor_t;

typedef struct acio_type5_control_ring {
    u32 ring;
    u16 sof_pdf_mask;
    u16 eof_pdf_mask;
    u32 max_frame_size;
    u32 rx_options;
} acio_type5_control_ring_t;

enum acio_type5_ring_direction {
    ACIO_TYPE5_RING_TX,
    ACIO_TYPE5_RING_RX,
};

enum acio_sid_partition_mode {
    ACIO_SID_PARTITION_SHARED = 0,
    ACIO_SID_PARTITION_DIRECTION = 1,
    ACIO_SID_PARTITION_RING = 2,
};

typedef u32 (*acio_type5_crc32_fn)(const u8 *data, size_t size, void *context);
u32 acio_type5_crc32c(const u8 *data, size_t size, void *context);
int acio_type5_fw_bundle_validate(const void *bundle, size_t bundle_size,
                                  acio_type5_fw_bundle_view_t *view);
int acio_type5_fw_destination(u64 nhi_base, enum acio_type5_fw_blob_kind kind,
                              u64 *destination);

typedef struct acio_type5_config_request {
    u64 route;
    u16 offset;
    u8 length;
    u8 adapter;
    u8 space;
    u8 sequence;
} acio_type5_config_request_t;

typedef struct acio_type5_scan_queue {
    u64 *routes;
    size_t capacity;
    size_t head;
    size_t count;
} acio_type5_scan_queue_t;

typedef struct acio_type5_hop_descriptor {
    bool valid;
    bool pm_packet;
    bool suppress_out_fields;
    u8 initial_credits;
    u8 out_port;
    u16 out_hop;
    bool egress_shared_buffering;
    bool ingress_shared_buffering;
    bool egress_flow_control;
    bool ingress_flow_control;
    bool counter_enable;
    u16 counter_id;
    bool drop_packet;
    u8 priority;
    u8 weight;
} acio_type5_hop_descriptor_t;

enum acio_type5_router_action_kind {
    ACIO_TYPE5_ROUTER_POLL,
    ACIO_TYPE5_ROUTER_READ,
    ACIO_TYPE5_ROUTER_WRITE,
    ACIO_TYPE5_ROUTER_DONE,
};

typedef struct acio_type5_router_action {
    enum acio_type5_router_action_kind kind;
    u64 route;
    u8 port;
    u8 space;
    u16 offset;
    u32 value;
    u32 mask;
} acio_type5_router_action_t;

typedef struct acio_type5_router_sm {
    u64 route;
    u32 config_value;
    u8 state;
    bool all_parents_support_usb;
} acio_type5_router_sm_t;

typedef struct acio_type5_usb3_path {
    u8 priority;
    u8 weight;
    u8 source_initial_credits;
    u8 initial_credits;
    u8 destination_initial_credits;
    u8 counter_enable;
    u8 ingress_flow_control;
    u8 egress_flow_control;
    u16 source_hop;
    u16 destination_hop;
    u8 routing_options;
    u8 path_type;
    u8 credit_options;
} acio_type5_usb3_path_t;

enum acio_type5_usb3_action_kind {
    ACIO_TYPE5_USB3_ACTIVATE_TX,
    ACIO_TYPE5_USB3_ACTIVATE_RX,
    ACIO_TYPE5_USB3_FIND_UP_CAP,
    ACIO_TYPE5_USB3_ENABLE_UP,
    ACIO_TYPE5_USB3_WAIT_DEADLINE,
    ACIO_TYPE5_USB3_FIND_DOWN_CAP,
    ACIO_TYPE5_USB3_ENABLE_DOWN,
    ACIO_TYPE5_USB3_DONE,
};

typedef struct acio_type5_usb3_action {
    enum acio_type5_usb3_action_kind kind;
    acio_type5_usb3_path_t path;
    u64 route;
    u8 port;
    u8 space;
    u16 offset;
    u32 value;
    u32 mask;
    u64 deadline_ms;
} acio_type5_usb3_action_t;

typedef struct acio_type5_usb3_sm {
    u64 up_route;
    u64 down_route;
    u64 deadline_ms;
    u16 up_cap;
    u16 down_cap;
    u8 up_port;
    u8 down_port;
    u8 state;
    bool use_recommended_credits;
} acio_type5_usb3_sm_t;

int acio_type5_descriptor_prepare(acio_type5_descriptor_t *descriptor, u64 iova, u16 length,
                                  u8 sof, u8 eof);
bool acio_type5_descriptor_complete(const acio_type5_descriptor_t *descriptor);
int acio_type5_descriptor_ack(acio_type5_descriptor_t *descriptor);
void acio_type5_control_ring_defaults(acio_type5_control_ring_t *ring);
int acio_type5_ring_base(enum acio_type5_ring_direction direction, u8 ring, u32 *base);
int acio_type5_mapper_slot(size_t mapper_count, enum acio_type5_ring_direction direction,
                           u8 ring, enum acio_sid_partition_mode *mode, u8 *slot);
int acio_type5_tunables_validate(const void *data, size_t size, u32 window_size);
int acio_type5_ring_doorbell(enum acio_type5_ring_direction direction, u8 ring, u16 index,
                             u32 *offset, u32 *value);
int acio_type5_ring_next(u16 index, u16 count, u16 *next);
bool acio_type5_ring_full(u16 producer, u16 consumer, u16 count);
int acio_type5_rx_pdf(enum acio_type5_ring_direction direction, u8 ring, u16 sof_mask,
                      u16 eof_mask, u32 *ring_offset, u32 *mirror_offset, u32 *value);
u8 acio_type5_next_sequence(u32 *packed_sequences, u8 pdf);
int acio_type5_config_pack(const acio_type5_config_request_t *request, acio_type5_crc32_fn crc32,
                           void *context, u8 packet[16]);
int acio_type5_config_packet_pack(const acio_type5_config_request_t *request, u8 pdf,
                                  const void *payload, size_t payload_size,
                                  u8 *packet, size_t capacity, size_t *packet_size);
int acio_type5_config_response_parse(const acio_type5_config_request_t *request, u8 pdf,
                                     const u8 *packet, size_t packet_size,
                                     u32 *first_value);
/* Block form: fills `count` big-endian dwords from the reply payload. */
int acio_type5_config_response_parse_n(const acio_type5_config_request_t *request, u8 pdf,
                                       const u8 *packet, size_t packet_size,
                                       u32 *values, size_t count);
/* Host-testable packing of the host router's ROUTER_CS_1..CS_4 topology
 * block from a prior CS_0..CS_4 read. */
int acio_type5_router_topology_pack(const u32 *cs0_4, u8 cm_version, u32 out[4]);

/* --- Plug-event ack --------------------------------------------------------
 *
 * MEASURED on J414s: an unacked PDF-5 plug event is retried by the router
 * indefinitely and the router withholds pending config responses while it
 * retries, so consuming events without acking starves the config channel
 * (cfg-rx-timeout with the RX descriptor still SW-seeded).  The kernelcache
 * corroborates a retry engine: IOThunderboltSwitchLC/Type1::enablePlugEvents
 * log "set notify retry, packet = 0x%08x".
 *
 * Apple's ack is a PDF-3 ConfigError frame, NOT PDF-4 NOTIFY_ACK:
 * IOThunderboltSwitch::sendPlugEventAck(plug_port, ack_type) fills its
 * pre-allocated ConfigErrorCommand with error code 7 (`mov w1,#0x7` @
 * 0xfffffe000ad74840), the event's port, and ack_type, then submits it.
 * ConfigErrorCommand::prepareForExecution @0xfffffe000ae71fb0..0xae71fb8
 * packs dword2 as {[3:0] error code, [13:8] port (`bfi w8,w9,#0x8,#0x6`),
 * [31:30] pg (`orr w8,w8,w9,lsl #0x1e`)}; SOF=EOF=3; 16-byte frame; no
 * response is ever sent to an error packet.
 *
 * The PG VALUES are PROVISIONAL: Apple passes ack_type through opaquely and
 * its origin was not decoded, so 2=plug / 3=unplug come from Linux's
 * TB_CFG_ERROR_PG_HOT_PLUG / _HOT_UNPLUG (drivers/thunderbolt/tb_msgs.h),
 * which interoperate with TBT3/USB4 routers.  If repeats continue after an
 * ack on hardware, suspect these two literals FIRST. */
#define ACIO_TYPE5_CFG_ERR_ACK_PLUG_EVENT 7u
#define ACIO_TYPE5_PG_HOT_PLUG_ACK        2u
#define ACIO_TYPE5_PG_HOT_UNPLUG_ACK      3u
int acio_type5_plug_ack_pack(u64 route, u8 port, bool unplug, u8 packet[16]);
int acio_type5_capability_step(u16 current, u32 header, u32 mask, u32 value, bool *matched,
                               bool *finished, u16 *next);

/* --- Lane adapter link state: LANE_ADP_CS_1[29:26] -------------------------
 *
 * Apple's gate in `IOThunderboltSwitch::childDeviceScanForPort` reads the
 * LANE_ADP capability (ID 0x01) at `capOffset + 1` in ADAPTER config space,
 * extracts bits [29:26], and branches on `state in {2,3,4,5,6}`.  Resolved
 * three independent ways from the kernelcache:
 *
 *   - taken   -> os_log "childDeviceScanForPort - link is up!", no sleep, no
 *                loop-back, falls straight into the scan work;
 *   - untaken -> "link training not done - attempt %d...", "wait %u
 *                milliseconds for training on port %d", a 10 ms ungated
 *                sleep, and a BACKWARD branch to the config re-read;
 *   - expiry  -> "timed out waiting for link training" and the function
 *                returns `kIOReturnNotReady` (0xE00002D8).
 *
 * The earlier analysis stalled because both immediate arms set the return
 * register to 0; what discriminates them is that the timeout arm overwrites it
 * later.  Polarity is therefore settled, not inferred.
 *
 * It is a WHITELIST, not a blacklist.  Reserved values 8..15, and any state we
 * fail to read at all, fall through to "not up".  There is deliberately no
 * "assume up" path: an unreadable link state is not evidence of a working
 * link, in exactly the way that a boolean whose false value is a physical
 * claim must not have a default.
 *
 * The individual state NAMES are UNPROVEN -- no string in the kernelcache
 * names them.  What is proven is the partition.  The public USB4 naming
 * (0 Disabled, 1 Training, 2 CL0, 3 TX CL0s, 4 RX CL0s, 5 CL1, 6 CL2, 7 CLd)
 * is CONSISTENT with that partition, which is why it appears here as prose and
 * never as an identifier.
 */
#define ACIO_TYPE5_LANE_ADP_CAP_ID   0x01u
#define ACIO_TYPE5_LANE_ADP_CAP_MASK 0xff00u
#define ACIO_TYPE5_LANE_ADP_CAP_VAL  0x0100u
#define ACIO_TYPE5_LANE_LINK_UP_MIN  2u
#define ACIO_TYPE5_LANE_LINK_UP_MAX  6u

u32 acio_type5_lane_link_state(u32 lane_adp_cs_1);
bool acio_type5_lane_link_up(u32 lane_adp_cs_1);
int acio_type5_scan_queue_init(acio_type5_scan_queue_t *queue, u64 *storage, size_t capacity,
                               u64 root_route);
int acio_type5_scan_queue_push(acio_type5_scan_queue_t *queue, u64 route);
int acio_type5_scan_queue_pop(acio_type5_scan_queue_t *queue, u64 *route);
int acio_type5_hop_pack(const acio_type5_hop_descriptor_t *descriptor, u8 packet[8]);

/* --- NFC (non-flow-controlled) credit allocation ---------------------------
 * ADP_CS_4 is adapter config space 1, offset 4:
 *   [19:0]  Non-Flow-Controlled Buffers   (the field being accumulated)
 *   [29:20] Total Buffers                 (the bound)
 * The compare-swap is SOFTWARE, not a hardware primitive: a read followed by
 * a conditional write over two control packets, with the controller gate as
 * the only mutual exclusion.  A mismatch is retried with the freshly read
 * value; Apple retries activation unboundedly with no backoff, so a bounded
 * caller must impose its own ceiling. */
#define ACIO_TYPE5_ADP_CS_4_OFFSET 4u
#define ACIO_TYPE5_NFC_MASK        0x000fffffu
#define ACIO_TYPE5_TOTAL_BUF_SHIFT 20u
#define ACIO_TYPE5_TOTAL_BUF_MASK  0x3ffu
/* "unspecified" sentinel returned by getNFCCreditsForPathTableIndex */
#define ACIO_TYPE5_NFC_UNSPECIFIED 0xffffu

/* DECODED: for a USB3 tunnel the NFC credit step is a provable no-op.
 * IOThunderboltAbstractPath::init leaves NonFlowControlledCredits (+0x84) = 0
 * and Source/DestinationNonFlowControlledCredits (+0xa8/+0xac) = 0xFFFF
 * ("unspecified"), and USB3's createPaths never calls any of the three
 * setters.  getNFCCreditsForPathTableIndex therefore returns literal 0 at
 * every path-table index for both the TX and RX path, so every compare-swap
 * would write back exactly what it read.
 *
 * The CAS machinery is kept because it is correct and other path types do
 * use it, but the USB3 activation path skips it rather than issuing two
 * control packets per adapter to add zero. */
#define ACIO_TYPE5_NFC_CREDITS_USB3 0u

/* Apple's static initial-credit fallback: 2 on the host side, 14 on the
 * device side.  Router Operation 0x33 ("Buffer Allocation Request", the
 * firmware-recommended credits selected by CreditOptions bit 2) is
 * deliberately NOT implemented: Apple ships a boot-arg
 * `tb-port-disable-usb4-allocation` that branches over the entire block,
 * its result is discarded by its caller, and every failure path falls back
 * to exactly these literals.  A vendor would not ship a switch that breaks
 * USB3 tunnelling, so 0x33 is optional and we land on the same values. */
/* USB3 tunnels use hop 8 in both directions; adapter enable is bits 31:30
 * of the adapter's config dword 0; the settle between the two adapter
 * enables is 100 ms (Apple's IOSleep(0x64)). */
#define ACIO_TYPE5_USB3_HOP       8u
#define ACIO_TYPE5_USB3_ENABLE    0xc0000000u
#define ACIO_TYPE5_USB3_SETTLE_MS 100u
/* The enable is a READ-MODIFY-WRITE at the DISCOVERED protocol-adapter
 * capability, NOT a blind write to ADAPTER-space offset 0.
 * `AppleThunderboltUSBUpAdapter::enableAdapter` @0xfffffe0009d5a8f4 does:
 *   findCapability(route, port, space=1, mask=0xff00, value=0x400, &cap)
 *   configModifyDWordWithMask(route, port, space=1, offset=cap,
 *                             data=enable?0xC0000000:0x40000000,
 *                             mask=0xC0000000)
 * i.e. capability ID 0x04 in adapter config space, bit 31 = enable and bit 30
 * written 1 in both cases.  Writing 0xC0000000 to offset 0 instead lands on
 * ADP_CS_0, whose [23:0] is the adapter TYPE -- the router acknowledges it and
 * nothing happens, which is why that bug survived so long. */
#define ACIO_TYPE5_ADP_CAP_MASK      0xff00u
#define ACIO_TYPE5_USB3_ADP_CAP_ID   0x04u
#define ACIO_TYPE5_USB3_ADP_CAP_VAL  0x0400u
#define ACIO_TYPE5_USB3_ADAPTER_MASK 0xc0000000u
#define ACIO_TYPE5_USB3_DISABLE      0x40000000u
#define ACIO_TYPE5_CREDITS_HOST_SIDE   2u
#define ACIO_TYPE5_CREDITS_DEVICE_SIDE 14u

typedef struct acio_type5_nfc_cas {
    u32 compare;   /* value the write is conditional on */
    u32 write;     /* value to write when the compare matches */
    u32 credits;   /* credits this path is adding */
    u32 attempts;
} acio_type5_nfc_cas_t;

/* Seed the optimistic first attempt.  Apple issues the first compare-swap
 * with compare_value = 0 rather than reading first. */
int acio_type5_nfc_begin(acio_type5_nfc_cas_t *cas, u32 credits);
/* Fold an observed ADP_CS_4 into the next attempt.  Returns 0 when a write
 * should be issued, -1 when the request cannot be satisfied (the new NFC
 * value would exceed Total Buffers -- Apple's kIOReturnNoResources). */
int acio_type5_nfc_next(acio_type5_nfc_cas_t *cas, u32 observed_cs4);

/* --- counters -------------------------------------------------------------
 * Counter clear is config space 3, three dwords of zero at offset
 * counter_id * 3, addressed to the source port.  Counter IDs are bounded by
 * ADP_CS_1 [18:8] (getMaxCounters).  There is deliberately NO release step:
 * the counter ID is bound at hop-ID allocate time and dies with the hop-ID
 * release, so teardown must NOT try to free it. */
#define ACIO_TYPE5_COUNTER_DWORDS 3u
#define ACIO_TYPE5_MAX_COUNTERS_SHIFT 8u
#define ACIO_TYPE5_MAX_COUNTERS_MASK  0x7ffu
int acio_type5_counter_clear_offset(u16 counter_id, u32 max_counters, u16 *offset);
int acio_type5_router_start(acio_type5_router_sm_t *sm, u64 route,
                            bool all_parents_support_usb);
int acio_type5_router_next(const acio_type5_router_sm_t *sm, acio_type5_router_action_t *action);
int acio_type5_router_complete(acio_type5_router_sm_t *sm, int status, u32 value);
void acio_type5_usb3_path_defaults(acio_type5_usb3_path_t *tx, acio_type5_usb3_path_t *rx,
                                   bool use_recommended_credits);
int acio_type5_usb3_start(acio_type5_usb3_sm_t *sm, u64 up_route, u8 up_port, u64 down_route,
                          u8 down_port, bool use_recommended_credits);
int acio_type5_usb3_next(const acio_type5_usb3_sm_t *sm, acio_type5_usb3_action_t *action);
int acio_type5_usb3_complete(acio_type5_usb3_sm_t *sm, int status, u32 value, u64 now_ms);

/* Read-only, ADT-derived resources for one t6020 USB4 host router.  Discovery
 * performs no PMGR, MMIO, DART, ASC, or PHY operation. */
typedef struct acio_resources {
    u32 index;
    u32 port_number;
    u64 nhi_base;
    u64 nhi_size;
    u64 pdf_base;
    u64 pdf_size;
    u64 rc_base;
    u64 rc_size;
    u64 hbw_base;
    u64 hbw_size;
    u64 lbw_base;
    u64 lbw_size;
    u64 pcie_adapter_base;
    u64 pcie_adapter_size;
    u64 dart_base;
    u64 dart_size;
    u64 asc_cpu_base;
    u64 asc_mailbox_base;
    u64 sram_iova_base;
    u64 sram_phys_base;
    u64 sram_size;
    bool firmware_preloaded_running;
    enum acio_sid_partition_mode sid_partition_mode;
    u8 dart_sid_count;
    u8 dart_sids[ACIO_NHI_IRQ_COUNT];
    u32 irqs[ACIO_NHI_IRQ_COUNT];
    /* Host-router adapters, from ADT.  The host router is NEVER scanned over
     * the config channel: adapter N's type is portmap[N-1], the adapter count
     * is the portmap length, and port-defaults carries each adapter's hop and
     * buffer limits. */
    u8 adapter_count;
    acio_type5_adapter_t adapters[ACIO_TYPE5_MAX_ADAPTERS];
} acio_resources_t;

/* Locate the first host-router adapter of a given kind and direction.
 * Returns the 1-based adapter number, or -1. Adapter 0 is the router. */
int acio_resources_find_adapter(const acio_resources_t *resources,
                                enum acio_type5_adapter_kind kind, bool up);

/* Validate and extract the J414s/t6020 resource contract. Returns -1 rather
 * than guessing if any identity, aperture, ring IRQ, DART, or SRAM mapping
 * is missing or contradictory. */
int acio_discover_resources(u32 index, acio_resources_t *resources);
int acio_apply_tunables(const acio_resources_t *resources);
int acio_apply_pcie_adapter_tunables(const acio_resources_t *resources);

enum acio_type5_runtime_phase {
    ACIO_TYPE5_RUNTIME_OFF = 0,
    ACIO_TYPE5_RUNTIME_PHY_PREPARED,
    ACIO_TYPE5_RUNTIME_POWERED,
    ACIO_TYPE5_RUNTIME_FW_READY,
    ACIO_TYPE5_RUNTIME_CONTROL_READY,
    ACIO_TYPE5_RUNTIME_ROUTER_READY,
    ACIO_TYPE5_RUNTIME_TUNNEL_READY,
    ACIO_TYPE5_RUNTIME_PIPE_COMMITTED,
    ACIO_TYPE5_RUNTIME_FAILED,
};

/* Bounded error telemetry.  Every runtime failure path records exactly one
 * of these plus a raw detail word, so a diagnostic caller can distinguish
 * "no cable" from "ring never enabled" from "router never acked" without
 * re-running the hardware sequence.  The numeric groups are stable ABI: the
 * high byte selects the phase, the low byte the specific condition. */
enum acio_type5_error {
    ACIO_TYPE5_E_NONE = 0x0000,

    ACIO_TYPE5_E_BAD_INDEX = 0x0101,
    ACIO_TYPE5_E_BAD_PHASE = 0x0102,
    ACIO_TYPE5_E_RESOURCES = 0x0103,

    ACIO_TYPE5_E_POWER_STATE5 = 0x0201,
    ACIO_TYPE5_E_POWER_STATE7 = 0x0202,
    ACIO_TYPE5_E_CIO_RECONFIG = 0x0203,

    ACIO_TYPE5_E_PHY_PREPARE = 0x0301,

    ACIO_TYPE5_E_FW_BUNDLE = 0x0401,
    ACIO_TYPE5_E_FW_COPY = 0x0402,
    ACIO_TYPE5_E_FW_RELEASE = 0x0403,
    ACIO_TYPE5_E_FW_READY_TIMEOUT = 0x0404,
    /* Distinguished on purpose: a readiness field that never changed across
     * the whole budget means there is no firmware running to change it,
     * which is a provenance problem, not a polling problem. */
    ACIO_TYPE5_E_FW_ABSENT = 0x0405,
    ACIO_TYPE5_E_FW_BUS_ERROR = 0x0406,

    ACIO_TYPE5_E_TUNABLES = 0x0501,

    ACIO_TYPE5_E_CTRL_SID_LAYOUT = 0x0601,
    ACIO_TYPE5_E_CTRL_PATH_COUNT = 0x0602,
    ACIO_TYPE5_E_CTRL_DART_INIT = 0x0603,
    ACIO_TYPE5_E_CTRL_DART_FORCE = 0x0604,
    ACIO_TYPE5_E_CTRL_ALLOC = 0x0605,
    ACIO_TYPE5_E_CTRL_IOVA = 0x0606,
    ACIO_TYPE5_E_CTRL_MAP = 0x0607,
    ACIO_TYPE5_E_CTRL_ENABLE = 0x0608,
    ACIO_TYPE5_E_CTRL_DISABLE_DONE = 0x0609,

    ACIO_TYPE5_E_CFG_BUSY = 0x0701,
    ACIO_TYPE5_E_CFG_PACK = 0x0702,
    ACIO_TYPE5_E_CFG_DESC = 0x0703,
    ACIO_TYPE5_E_CFG_TX_TIMEOUT = 0x0704,
    ACIO_TYPE5_E_CFG_RX_TIMEOUT = 0x0705,
    ACIO_TYPE5_E_CFG_RX_LENGTH = 0x0706,
    ACIO_TYPE5_E_CFG_RX_PDF = 0x0707,
    ACIO_TYPE5_E_CFG_RX_REJECT = 0x0708,
    ACIO_TYPE5_E_CFG_RING_ADVANCE = 0x0709,
    /* The router answered our request with a PDF-3 ConfigError frame.  This
     * is a REPLY, not a foreign frame, so it must not be reported as
     * cfg-rx-pdf: the detail carries the router's own error code and port
     * (dword2: code [3:0], port [13:8]) and names the rejected register. */
    ACIO_TYPE5_E_CFG_RX_ROUTER_ERROR = 0x070a,

    ACIO_TYPE5_E_ROUTER_POLL_READY = 0x0801,
    ACIO_TYPE5_E_ROUTER_READ = 0x0802,
    ACIO_TYPE5_E_ROUTER_WRITE = 0x0803,
    ACIO_TYPE5_E_ROUTER_POLL_ACK = 0x0804,
    ACIO_TYPE5_E_ROUTER_STATE = 0x0805,

    ACIO_TYPE5_E_TUNNEL_STATE = 0x0901,
    /* The routed PIPE mux commit itself failed: atcphy_commit_routed_pipe
     * could not run the pipehandler lock/clock/data/unlock sequence (or the
     * PHY was not prepared).  The write never landed. */
    ACIO_TYPE5_E_PIPE_COMMIT = 0x0902,
    /* The commit sequence returned success but MUX_CTRL did NOT read back the
     * routed-USB4 value 0x11.  Distinguished from E_PIPE_COMMIT on purpose: a
     * PHY write that ACKs is not a mux that switched, and this is exactly the
     * "signal that correlates but does not identify" trap -- the detail word
     * carries the value actually read so a diagnostic caller sees what the
     * mux settled on instead of what was requested. */
    ACIO_TYPE5_E_PIPE_READBACK = 0x0903,
    /* The USB3 protocol-adapter capability (ID 0x04) could not be located in
     * that adapter's ADAPTER-space capability list, so there is no offset to
     * write the enable to.  Distinct from tunnel-state on purpose: writing the
     * enable to the WRONG offset is exactly the defect this replaced -- a
     * router ACKs a write to ADP_CS_0 whether or not it means anything, so a
     * missing capability must fail loudly rather than fall back. */
    ACIO_TYPE5_E_TUNNEL_ADAPTER_CAP = 0x0904,

    ACIO_TYPE5_E_SCAN_ROUTE = 0x0a01,
    ACIO_TYPE5_E_SCAN_HEADER = 0x0a02,
    ACIO_TYPE5_E_SCAN_MAXPORT = 0x0a03,
    ACIO_TYPE5_E_SCAN_ADAPTER = 0x0a04,
    ACIO_TYPE5_E_SCAN_NO_USB3 = 0x0a05,
    /* Apple refuses the scan for a port that is not a lane adapter
     * (portAllowsDeviceScan requires getAdapterType() == 1). */
    ACIO_TYPE5_E_SCAN_NOT_LANE = 0x0a06,
    /* The LANE_ADP capability could not be located, so the link state could
     * not be read at all.  Distinct from "read it and it was not up": an
     * unreadable state is a defect in us, not a statement about the link. */
    ACIO_TYPE5_E_SCAN_LANE_CAP = 0x0a07,
    /* Link training did not reach an operational state within the budget. */
    ACIO_TYPE5_E_SCAN_LINK_NOT_READY = 0x0a08,
};

typedef struct acio_type5_status {
    /* Monotonic per-port counter, incremented on every entry to
     * acio_type5_firmware_start.  A caller can latch it before launching a
     * run and require that the status it later reads carries a STRICTLY
     * GREATER value, which is what distinguishes "this run's result" from a
     * previous run's residue.  Phase and error names recur across runs and
     * therefore cannot identify which run produced them. */
    u32 run_id;
    u32 index;
    u32 phase;
    u32 last_error;
    u32 last_error_detail;
    u32 error_count;
    u32 config_requests;
    u32 config_failures;
    u32 router_state;
} acio_type5_status_t;

/* Hop descriptor helpers: space 0, two dwords at 2*hopID. */
/* Route strings: 8 bits per hop, LSB byte = the hop nearest the host,
 * 54 valid bits, depth bounded at 6 so only bytes 0..5 are ever used. */
#define ACIO_TYPE5_ROUTE_MASK      0x003fffffffffffffULL
#define ACIO_TYPE5_MAX_ROUTE_DEPTH 6u
int acio_type5_route_child(u64 parent_route, u32 parent_depth, u8 adapter,
                           u64 *child_route, u32 *child_depth);
#define ACIO_TYPE5_HOP_DWORDS 2u
int acio_type5_hop_offset(u16 hop, u16 *offset);
/* Pure, host-testable: stable short name for an error code. Never NULL. */
const char *acio_type5_error_name(u32 error);
int acio_type5_runtime_status(u32 index, acio_type5_status_t *status);

/* Start/adopt Apple Type5 firmware without committing the USB4 PIPE. The
 * normal iBoot-preloaded/running path accepts a NULL, zero-length bundle.
 * Apple's debug/manual-loader path requires the external bundle, copies it
 * into the four fixed coprocessor windows, and does not retain the caller's
 * buffer after this returns. */
int acio_type5_firmware_start(u32 index, const void *bundle, size_t bundle_size,
                              bool flipped, bool thunderbolt_mode,
                              u32 diagnostic_stop);
int acio_type5_runtime_abort(u32 index);
enum acio_type5_runtime_phase acio_type5_runtime_phase(u32 index);
int acio_type5_config_read32(u32 index, u64 route, u8 port, u8 space,
                             u16 offset, u32 *value);
int acio_type5_config_write32(u32 index, u64 route, u8 port, u8 space,
                              u16 offset, u32 value);

/* Execute the pure root-router state machine against the live config
 * transport.  Advances the runtime to ACIO_TYPE5_RUNTIME_ROUTER_READY on
 * success.  Commits no PIPE mux change and creates no tunnel. */
int acio_type5_router_configure(u32 index, u64 route, bool all_parents_support_usb);
int acio_type5_config_read_block(u32 index, u64 route, u8 port, u8 space, u16 offset,
                                 u8 count, u32 *values);
int acio_type5_config_write_block(u32 index, u64 route, u8 port, u8 space, u16 offset,
                                  u8 count, const u32 *values);
/* Write one two-dword hop descriptor. Hop descriptors live in config space 0
 * at offset 2*hopID, addressed to the owning adapter. */
int acio_type5_hop_write(u32 index, u64 route, u8 port, u16 hop,
                         const acio_type5_hop_descriptor_t *descriptor);
/* Bring up / tear down the bidirectional USB3 tunnel. Teardown is
 * deliberately reverse-order and asymmetric; see the implementation. */
/* Discover the device router attached to a host downstream adapter and
 * locate its USB3 Up adapter. Requires ROUTER_READY and a real link partner.
 * Returns 0 and fills both outputs, or -1 with bounded telemetry. */
/* `upstream_port` is the device router's own upstream (lane) adapter, taken
 * from ROUTER_CS_1 [13:8].  A real tunnel needs it: the device-side hops are
 * programmed BETWEEN that adapter and the USB3 Up adapter, so without it only
 * the host half of the path can be described. */
int acio_type5_scan_device_router(u32 index, u8 down_adapter, u64 *device_route,
                                  u32 *device_depth, u8 *usb3_up_adapter,
                                  u8 *upstream_port);
int acio_type5_usb3_tunnel_up(u32 index, u64 down_route, u8 down_adapter,
                              u64 up_route, u8 up_adapter);

/* Bring up a REAL end-to-end USB3 tunnel to an attached device router.
 *
 * This is the honest replacement for calling acio_type5_usb3_tunnel_up with
 * both endpoints on route 0.  That pairing (host USB3-Down adapter 4 with host
 * adapter 1, a LANE adapter) programmed a host-internal loopback: it reached
 * TUNNEL_READY because two config writes were ACKed, not because anything
 * crossed the cable.  A phase our own code sets after an ACK is not evidence of
 * a path.
 *
 * Here the far end is DISCOVERED, never assumed: this scans through
 * `host_lane_adapter` for the device router and its USB3 Up adapter, then
 * programs FOUR hop descriptors -- two on the host router, two on the device
 * router -- for the Tx (host->device) and Rx (device->host) paths, and enables
 * both protocol adapters at their DISCOVERED capability-0x04 offsets in Apple's
 * order (UP, 100 ms, DOWN).
 *
 * Reaching TUNNEL_READY here still does NOT mean a device enumerated; it means
 * every step above completed against a router that answered. The only thing
 * that identifies success is a disk appearing to the OS. */
int acio_type5_usb3_tunnel_device_up(u32 index, u8 host_lane_adapter,
                                     u8 host_usb3_down_adapter);
int acio_type5_usb3_tunnel_down(u32 index, u64 down_route, u8 down_adapter,
                                u64 up_route, u8 up_adapter);

/* Commit the routed USB4 PIPE mux and advance TUNNEL_READY -> PIPE_COMMITTED.
 *
 * This is the ONLY function in this runtime that leaves the pipehandler mux on
 * anything other than DUMMY.  It requires TUNNEL_READY (a bring-up that has at
 * least reached the USB3 tunnel step), drives atcphy_commit_routed_pipe (the
 * pipehandler lock -> CLK_OFF -> DATA_USB4 -> CLK_USB4 -> unlock sequence that
 * selects whole-register 0x11), and then PROVES the switch by reading MUX_CTRL
 * back: only a readback of exactly 0x11 advances the phase.  A failed sequence
 * records ACIO_TYPE5_E_PIPE_COMMIT; a sequence that "succeeded" but whose mux
 * did not settle on 0x11 records ACIO_TYPE5_E_PIPE_READBACK with the observed
 * value.  The phase it sets means precisely "MUX_CTRL reads 0x11", nothing
 * about whether a device enumerated. */
int acio_type5_pipe_commit(u32 index);

#endif
