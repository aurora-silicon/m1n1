/* SPDX-License-Identifier: MIT */
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "acio.h"

static u32 test_crc(const u8 *data, size_t size, void *context)
{
    u32 value = *(const u32 *)context;
    for (size_t i = 0; i < size; i++)
        value = value * 33u + data[i];
    return value;
}

/* LANE_ADP_CS_1[29:26] link state.  Apple's gate is a WHITELIST of {2,3,4,5,6};
 * everything else -- including the reserved encodings 8..15 -- means "keep
 * waiting", never "proceed".
 *
 * These assertions are written so that they FAIL if the polarity is inverted
 * and FAIL if any unknown state is treated as "up", because those are the two
 * ways this gate can be wrong while still looking plausible on a bench that
 * happens to train quickly. */
static void test_lane_link_state(void)
{
    /* Field position: bits [29:26], nothing above or below leaks in. */
    assert(acio_type5_lane_link_state(0u) == 0u);
    assert(acio_type5_lane_link_state(0xffffffffu) == 0xfu);
    for (u32 state = 0; state < 16u; state++) {
        assert(acio_type5_lane_link_state(state << 26) == state);
        /* Bits outside [29:26] must not disturb the decode. */
        assert(acio_type5_lane_link_state((state << 26) | 0xc3ffffffu) == state);
    }

    /* The proven operational set, and ONLY it. Written as an explicit
     * whitelist so that widening the accepted range fails here. */
    unsigned up_count = 0;
    for (u32 state = 0; state < 16u; state++) {
        bool up = acio_type5_lane_link_up(state << 26);
        bool expected = (state >= 2u && state <= 6u);
        assert(up == expected);
        if (up)
            up_count++;
    }
    assert(up_count == 5u);

    /* Not-up is not merely "0". A disabled link, a link still training, and a
     * link parked in the non-traffic-carrying state are all distinct values
     * that must each refuse to proceed. */
    assert(!acio_type5_lane_link_up(0u << 26));
    assert(!acio_type5_lane_link_up(1u << 26));
    assert(!acio_type5_lane_link_up(7u << 26));

    /* Reserved encodings. An unknown state is not evidence of a working link,
     * so every one of them must refuse. This is the assertion that fails if
     * anyone "simplifies" the whitelist into `state >= 2`. */
    for (u32 state = 8u; state < 16u; state++)
        assert(!acio_type5_lane_link_up(state << 26));

    /* A register read that came back as all-ones (a bus error shape) decodes
     * to state 0xF, which is reserved, so it refuses -- by the same rule, not
     * by a special case. */
    assert(!acio_type5_lane_link_up(0xffffffffu));

    assert(ACIO_TYPE5_LANE_LINK_UP_MIN == 2u);
    assert(ACIO_TYPE5_LANE_LINK_UP_MAX == 6u);
    assert(ACIO_TYPE5_LANE_ADP_CAP_ID == 0x01u);
    assert(ACIO_TYPE5_LANE_ADP_CAP_MASK == 0xff00u);
    assert(ACIO_TYPE5_LANE_ADP_CAP_VAL == 0x0100u);
}

static void test_descriptors_and_control(void)
{
    acio_type5_descriptor_t descriptor;
    assert(acio_type5_descriptor_prepare(&descriptor, 0x10004000, 16,
                                         ACIO_TYPE5_PDF_CONFIG_READ,
                                         ACIO_TYPE5_PDF_CONFIG_READ) == 0);
    assert(descriptor.address == 0x10004000);
    assert(descriptor.metadata == 0x00411010);
    assert(descriptor.reserved == 0);
    assert(!acio_type5_descriptor_complete(&descriptor));
    assert(acio_type5_descriptor_ack(&descriptor) < 0);
    descriptor.metadata |= ACIO_TYPE5_DESC_COMPLETE;
    assert(acio_type5_descriptor_complete(&descriptor));
    assert(acio_type5_descriptor_ack(&descriptor) == 0);
    assert(descriptor.metadata == 0);
    assert(acio_type5_descriptor_prepare(&descriptor, 0, 1, 0, 0) < 0);
    assert(acio_type5_descriptor_prepare(&descriptor, 1, 0x1000, 0, 0) < 0);

    acio_type5_control_ring_t ring;
    acio_type5_control_ring_defaults(&ring);
    /* The control path is hard-wired to ring 0 by allocateTransmitRing; the
     * enabled-ring mask excludes ring 0 because the control path owns it. */
    assert(ring.ring == ACIO_TYPE5_CONTROL_RING && ring.ring == 0);
    assert(ACIO_TYPE5_CONTROL_TX_CREDITS == 2);
    assert(ring.sof_pdf_mask == 0xffff && ring.eof_pdf_mask == 0xffff);
    assert(ring.max_frame_size == 256);
    assert(ring.rx_options == 0xc0000000);

    u32 base;
    u32 offset;
    u32 value;
    assert(acio_type5_ring_base(ACIO_TYPE5_RING_TX, 11, &base) == 0 && base == 0x3c000);
    assert(acio_type5_ring_base(ACIO_TYPE5_RING_RX, 11, &base) == 0 && base == 0xac000);
    assert(acio_type5_ring_base(ACIO_TYPE5_RING_RX, 12, &base) < 0);
    assert(acio_type5_ring_doorbell(ACIO_TYPE5_RING_TX, 0, 7, &offset, &value) == 0 &&
           offset == 0x10008 && value == 0x00070000);
    assert(acio_type5_ring_doorbell(ACIO_TYPE5_RING_RX, 0, 7, &offset, &value) == 0 &&
           offset == 0x80008 && value == 7);
    u16 next;
    assert(acio_type5_ring_next(7, 8, &next) == 0 && next == 0);
    assert(acio_type5_ring_full(7, 0, 8));
    assert(!acio_type5_ring_full(6, 0, 8));
    u32 mirror;
    assert(acio_type5_rx_pdf(ACIO_TYPE5_RING_RX, 2, 0xffff, 0xffff, &offset, &mirror,
                             &value) == 0);
    assert(offset == 0x88014 && mirror == 0x8000 && value == 0xffffffff);
    assert(acio_type5_rx_pdf(ACIO_TYPE5_RING_TX, 2, 0, 0, &offset, &mirror, &value) < 0);

    enum acio_sid_partition_mode mode;
    u8 slot;
    assert(acio_type5_mapper_slot(1, ACIO_TYPE5_RING_TX, 11, &mode, &slot) == 0);
    assert(mode == ACIO_SID_PARTITION_SHARED && slot == 0);
    assert(acio_type5_mapper_slot(1, ACIO_TYPE5_RING_RX, 11, &mode, &slot) == 0);
    assert(mode == ACIO_SID_PARTITION_SHARED && slot == 0);
    assert(acio_type5_mapper_slot(2, ACIO_TYPE5_RING_TX, 5, &mode, &slot) == 0);
    assert(mode == ACIO_SID_PARTITION_DIRECTION && slot == 0);
    assert(acio_type5_mapper_slot(2, ACIO_TYPE5_RING_RX, 5, &mode, &slot) == 0);
    assert(mode == ACIO_SID_PARTITION_DIRECTION && slot == 1);
    assert(acio_type5_mapper_slot(24, ACIO_TYPE5_RING_TX, 11, &mode, &slot) == 0);
    assert(mode == ACIO_SID_PARTITION_RING && slot == 11);
    assert(acio_type5_mapper_slot(24, ACIO_TYPE5_RING_RX, 11, &mode, &slot) == 0);
    assert(mode == ACIO_SID_PARTITION_RING && slot == 23);
    assert(acio_type5_mapper_slot(3, ACIO_TYPE5_RING_TX, 0, &mode, &slot) < 0);
    assert(acio_type5_mapper_slot(24, ACIO_TYPE5_RING_RX, 12, &mode, &slot) < 0);

    u32 sequences = 0;
    assert(acio_type5_next_sequence(&sequences, ACIO_TYPE5_PDF_CONFIG_READ) == 1);
    assert(acio_type5_next_sequence(&sequences, ACIO_TYPE5_PDF_CONFIG_READ) == 2);
    assert(acio_type5_next_sequence(&sequences, ACIO_TYPE5_PDF_CONFIG_WRITE) == 1);
    assert(acio_type5_next_sequence(&sequences, ACIO_TYPE5_PDF_CONFIG_READ) == 3);
    assert(acio_type5_next_sequence(&sequences, ACIO_TYPE5_PDF_CONFIG_READ) == 0);
}

static void test_config_and_capabilities(void)
{
    assert(acio_type5_crc32c((const u8 *)"123456789", 9, NULL) == 0xe3069283);

    const u32 seed = 7;
    acio_type5_config_request_t request = {
        .route = 0x0012334455667788,
        .offset = 0x123,
        .length = 4,
        .adapter = 8,
        .space = ACIO_TYPE5_CONFIG_ADAPTER,
        .sequence = 2,
    };
    u8 packet[16];
    assert(acio_type5_config_pack(&request, test_crc, (void *)&seed, packet) == 0);
    const u8 prefix[] = {0x00, 0x12, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88};
    assert(memcmp(packet, prefix, sizeof(prefix)) == 0);
    const u32 address = 0x10000000u | 0x02000000u | 0x00400000u | 0x00008000u | 0x123u;
    assert(packet[8] == (u8)(address >> 24));
    assert(packet[9] == (u8)(address >> 16));
    assert(packet[10] == (u8)(address >> 8));
    assert(packet[11] == (u8)address);
    assert(acio_type5_config_pack(&request, NULL, NULL, packet) < 0);
    request.space = ACIO_TYPE5_CONFIG_HOPS;
    assert(acio_type5_config_pack(&request, test_crc, (void *)&seed, packet) == 0);
    request.space = 4;
    assert(acio_type5_config_pack(&request, test_crc, (void *)&seed, packet) < 0);
    request.space = ACIO_TYPE5_CONFIG_ADAPTER;
    assert(acio_type5_config_pack(&request, acio_type5_crc32c, NULL, packet) == 0);

    request.length = 1;
    u8 frame[32];
    size_t frame_size;
    const u32 write_value = 0x00000080;
    assert(acio_type5_config_packet_pack(&request, ACIO_TYPE5_PDF_CONFIG_WRITE,
                                         &write_value, sizeof(write_value), frame,
                                         sizeof(frame), &frame_size) == 0);
    assert(frame_size == 20);
    assert(frame[12] == 0 && frame[13] == 0 && frame[14] == 0 && frame[15] == 0x80);
    assert(acio_type5_crc32c(frame, 16, NULL) ==
           ((u32)frame[16] << 24 | (u32)frame[17] << 16 |
            (u32)frame[18] << 8 | frame[19]));

    /* Synthesize a matching one-dword read response. */
    assert(acio_type5_config_packet_pack(&request, ACIO_TYPE5_PDF_CONFIG_READ,
                                         NULL, 0, frame, sizeof(frame), &frame_size) == 0);
    memmove(frame + 16, frame + 12, 4);
    frame[0] |= 0x80; /* response wire bit */
    frame[12] = 0x12;
    frame[13] = 0x34;
    frame[14] = 0x56;
    frame[15] = 0x78;
    u32 crc = acio_type5_crc32c(frame, 16, NULL);
    frame[16] = (u8)(crc >> 24);
    frame[17] = (u8)(crc >> 16);
    frame[18] = (u8)(crc >> 8);
    frame[19] = (u8)crc;
    u32 response_value;
    assert(acio_type5_config_response_parse(&request, ACIO_TYPE5_PDF_CONFIG_READ,
                                             frame, 20, &response_value) == 0);
    assert(response_value == 0x12345678);
    /* CORRECTION: bit 31 of dword0 must be TOLERATED in either state.
     * This test previously required it as a "response marker" and asserted a
     * frame without it was rejected. That requirement is unproven -- Apple
     * never re-parses the route on receive, it matches against the
     * outstanding command object, and no 22-bit route-high mask exists
     * anywhere in IOThunderboltFamily's __text. Requiring the bit would
     * reject EVERY valid reply if the hardware does not set it, presenting
     * as a dead config channel rather than a parser bug.
     *
     * Reply identity is still fully established: CRC, route, offset,
     * length, adapter, config space and the 2-bit sequence all have to
     * match, which the mismatch cases below exercise. */
    frame[0] &= 0x7f;
    crc = acio_type5_crc32c(frame, 16, NULL);
    frame[16] = (u8)(crc >> 24);
    frame[17] = (u8)(crc >> 16);
    frame[18] = (u8)(crc >> 8);
    frame[19] = (u8)crc;
    assert(acio_type5_config_response_parse(&request, ACIO_TYPE5_PDF_CONFIG_READ,
                                             frame, 20, &response_value) == 0);
    assert(response_value == 0x12345678);
    frame[0] |= 0x80;
    crc = acio_type5_crc32c(frame, 16, NULL);
    frame[16] = (u8)(crc >> 24);
    frame[17] = (u8)(crc >> 16);
    frame[18] = (u8)(crc >> 8);
    frame[19] = (u8)crc;
    frame[12] ^= 1;
    assert(acio_type5_config_response_parse(&request, ACIO_TYPE5_PDF_CONFIG_READ,
                                             frame, 20, &response_value) < 0);

    bool matched;
    bool finished;
    u16 next;
    assert(acio_type5_capability_step(4, 0x00000408, 0xff00, 0x0400, &matched,
                                      &finished, &next) == 0);
    assert(matched && !finished && next == 8);
    assert(acio_type5_capability_step(8, 0x00000500, 0xff00, 0x0400, &matched,
                                      &finished, &next) == 0);
    assert(!matched && finished && next == 0);
    assert(acio_type5_capability_step(8, 0x00000508, 0xff00, 0x0400, &matched,
                                      &finished, &next) < 0);
}

static void test_firmware_bundle(void)
{
    const u32 sizes[ACIO_TYPE5_FW_BLOB_COUNT] = {0x22c00, 0x1960, 0x25c0, 0xa0};
    u32 total = (u32)sizeof(acio_type5_fw_bundle_header_t);
    for (u32 i = 0; i < ACIO_TYPE5_FW_BLOB_COUNT; i++)
        total += sizes[i];

    u8 *bundle = calloc(1, total);
    assert(bundle != NULL);
    acio_type5_fw_bundle_header_t *header = (acio_type5_fw_bundle_header_t *)bundle;
    header->magic = ACIO_TYPE5_FW_BUNDLE_MAGIC;
    header->version = ACIO_TYPE5_FW_BUNDLE_VERSION;
    header->total_size = total;
    header->header_size = (u32)sizeof(*header);

    u32 cursor = header->header_size;
    for (u32 i = 0; i < ACIO_TYPE5_FW_BLOB_COUNT; i++) {
        header->entries[i].offset = cursor;
        header->entries[i].size = sizes[i];
        memset(bundle + cursor, (int)(0x31u + i), sizes[i]);
        header->entries[i].crc32c = acio_type5_crc32c(bundle + cursor, sizes[i], NULL);
        cursor += sizes[i];
    }

    acio_type5_fw_bundle_view_t view;
    assert(acio_type5_fw_bundle_validate(bundle, total, &view) == 0);
    assert(view.size[ACIO_TYPE5_FW_ACIO_TEXT] == 0x22c00);
    assert(view.data[ACIO_TYPE5_FW_TMU_DATA] ==
           bundle + header->entries[ACIO_TYPE5_FW_TMU_DATA].offset);

    bundle[header->entries[ACIO_TYPE5_FW_ACIO_DATA].offset] ^= 1;
    assert(acio_type5_fw_bundle_validate(bundle, total, &view) < 0);
    bundle[header->entries[ACIO_TYPE5_FW_ACIO_DATA].offset] ^= 1;

    u32 saved = header->entries[ACIO_TYPE5_FW_TMU_TEXT].offset;
    header->entries[ACIO_TYPE5_FW_TMU_TEXT].offset = header->entries[0].offset;
    assert(acio_type5_fw_bundle_validate(bundle, total, &view) < 0);
    header->entries[ACIO_TYPE5_FW_TMU_TEXT].offset = saved;

    u64 destination;
    assert(acio_type5_fw_destination(0x100000000ULL, ACIO_TYPE5_FW_ACIO_TEXT,
                                     &destination) == 0);
    assert(destination == 0xff100000ULL);
    assert(acio_type5_fw_destination(0x100000000ULL, ACIO_TYPE5_FW_ACIO_DATA,
                                     &destination) == 0);
    assert(destination == 0xff180000ULL);
    assert(acio_type5_fw_destination(0x100000000ULL, ACIO_TYPE5_FW_TMU_TEXT,
                                     &destination) == 0);
    assert(destination == 0xff300000ULL);
    assert(acio_type5_fw_destination(0x100000000ULL, ACIO_TYPE5_FW_TMU_DATA,
                                     &destination) == 0);
    assert(destination == 0xff380000ULL);
    assert(acio_type5_fw_destination(0x1000, ACIO_TYPE5_FW_ACIO_TEXT, &destination) < 0);

    free(bundle);
}

static void test_tunable_bounds(void)
{
    struct {
        u32 offset;
        u32 size;
        u64 mask;
        u64 value;
    } record = {
        .offset = 0x3ffc,
        .size = 4,
        .mask = 0xffffffff,
        .value = 1,
    };
    assert(sizeof(record) == 24);
    assert(acio_type5_tunables_validate(&record, sizeof(record), 0x4000) == 0);
    record.offset = 0x4000;
    assert(acio_type5_tunables_validate(&record, sizeof(record), 0x4000) < 0);
    record.offset = 3;
    assert(acio_type5_tunables_validate(&record, sizeof(record), 0x4000) < 0);
    record.offset = 0;
    record.size = 8;
    assert(acio_type5_tunables_validate(&record, sizeof(record), 0x4000) < 0);
    assert(acio_type5_tunables_validate(&record, sizeof(record) - 1, 0x4000) < 0);
}

static void test_scan_and_hop(void)
{
    u64 routes[4];
    acio_type5_scan_queue_t queue;
    assert(acio_type5_scan_queue_init(&queue, routes, 4, 0) == 0);
    assert(acio_type5_scan_queue_push(&queue, 1) == 0);
    assert(acio_type5_scan_queue_push(&queue, 2) == 0);
    assert(acio_type5_scan_queue_push(&queue, 1) < 0);
    u64 route;
    assert(acio_type5_scan_queue_pop(&queue, &route) == 0 && route == 0);
    assert(acio_type5_scan_queue_pop(&queue, &route) == 0 && route == 1);
    assert(acio_type5_scan_queue_push(&queue, 3) == 0);
    assert(acio_type5_scan_queue_pop(&queue, &route) == 0 && route == 2);
    assert(acio_type5_scan_queue_pop(&queue, &route) == 0 && route == 3);
    assert(acio_type5_scan_queue_pop(&queue, &route) < 0);

    acio_type5_hop_descriptor_t hop = {
        .valid = true,
        .initial_credits = 14,
        .out_port = 3,
        .out_hop = 8,
        .egress_flow_control = true,
        .ingress_flow_control = true,
        .counter_enable = true,
        .counter_id = 9,
        .priority = 3,
        .weight = 3,
    };
    u8 packet[8];
    assert(acio_type5_hop_pack(&hop, packet) == 0);
    const u8 expected[] = {0x80, 0x1c, 0x18, 0x08, 0x03, 0x80, 0x93, 0x03};
    assert(memcmp(packet, expected, sizeof(expected)) == 0);
    hop.initial_credits = 128;
    assert(acio_type5_hop_pack(&hop, packet) < 0);
}

/* The error ABI is consumed by an out-of-tree diagnostic caller, so both the
 * numeric grouping and the names are pinned here.  A silent renumbering would
 * otherwise make a captured phase-ladder log decode to the wrong condition. */
static void test_error_telemetry(void)
{
    /* High byte selects the phase; low byte the specific condition. */
    assert((ACIO_TYPE5_E_BAD_INDEX >> 8) == 0x01);
    assert((ACIO_TYPE5_E_POWER_STATE5 >> 8) == 0x02);
    assert((ACIO_TYPE5_E_PHY_PREPARE >> 8) == 0x03);
    assert((ACIO_TYPE5_E_FW_READY_TIMEOUT >> 8) == 0x04);
    assert((ACIO_TYPE5_E_TUNABLES >> 8) == 0x05);
    assert((ACIO_TYPE5_E_CTRL_ENABLE >> 8) == 0x06);
    assert((ACIO_TYPE5_E_CFG_RX_TIMEOUT >> 8) == 0x07);
    assert((ACIO_TYPE5_E_ROUTER_POLL_ACK >> 8) == 0x08);
    assert((ACIO_TYPE5_E_TUNNEL_STATE >> 8) == 0x09);
    assert((ACIO_TYPE5_E_PIPE_COMMIT >> 8) == 0x09);
    assert((ACIO_TYPE5_E_PIPE_READBACK >> 8) == 0x09);
    assert((ACIO_TYPE5_E_TUNNEL_ADAPTER_CAP >> 8) == 0x09);

    assert(ACIO_TYPE5_E_NONE == 0);
    assert(strcmp(acio_type5_error_name(ACIO_TYPE5_E_NONE), "none") == 0);
    assert(strcmp(acio_type5_error_name(ACIO_TYPE5_E_FW_READY_TIMEOUT),
                  "fw-ready-timeout") == 0);
    assert(strcmp(acio_type5_error_name(ACIO_TYPE5_E_CTRL_DISABLE_DONE),
                  "ctrl-disable-done") == 0);
    assert(strcmp(acio_type5_error_name(ACIO_TYPE5_E_CFG_RX_PDF),
                  "cfg-rx-pdf") == 0);
    assert(strcmp(acio_type5_error_name(ACIO_TYPE5_E_ROUTER_POLL_READY),
                  "router-poll-ready") == 0);
    assert(strcmp(acio_type5_error_name(ACIO_TYPE5_E_ROUTER_POLL_ACK),
                  "router-poll-ack") == 0);
    assert(strcmp(acio_type5_error_name(ACIO_TYPE5_E_CTRL_DART_FORCE),
                  "ctrl-dart-force-active") == 0);
    /* The commit's two failure modes must stay distinguishable: a mux write
     * that never ran vs one that ran but did not read back 0x11. */
    assert(strcmp(acio_type5_error_name(ACIO_TYPE5_E_PIPE_COMMIT),
                  "pipe-commit") == 0);
    assert(strcmp(acio_type5_error_name(ACIO_TYPE5_E_PIPE_READBACK),
                  "pipe-readback") == 0);
    assert(ACIO_TYPE5_E_PIPE_COMMIT != ACIO_TYPE5_E_PIPE_READBACK);

    /* Never NULL, for any input, including codes that do not exist yet. */
    assert(strcmp(acio_type5_error_name(0xdead), "unknown") == 0);
    for (u32 i = 0; i < 0x0a00; i++)
        assert(acio_type5_error_name(i) != NULL);

    /* The two router poll stages must stay distinguishable: a ready-timeout
     * and an ack-timeout demand different hardware conclusions. */
    assert(ACIO_TYPE5_E_ROUTER_POLL_READY != ACIO_TYPE5_E_ROUTER_POLL_ACK);
    assert(strcmp(acio_type5_error_name(ACIO_TYPE5_E_ROUTER_POLL_READY),
                  acio_type5_error_name(ACIO_TYPE5_E_ROUTER_POLL_ACK)) != 0);
}

/* Host-router topology block, pinned to the updateTopologyID decode:
 * space 2, port 0, offset 1, length 4, big-endian, preceded by a length-5
 * read of CS_0..CS_4 because CS_1 and CS_4 are read-modify-write. */
/* Decoded against the REAL J414s /arm-io/acio1 ADT bytes. */
/* NFC credit compare-swap. The CAS is software (read + conditional write over
 * two control packets), so all of the interesting behaviour is in how the
 * next attempt is derived from an observed ADP_CS_4. */
/* Hop descriptors live in config space 0, two dwords at offset 2*hopID. */
/* Route strings: 8 bits per hop, LSB byte nearest the host, 54 valid bits,
 * depth bounded at 6. The shift uses the PARENT's depth -- using the child's
 * silently produces a route one hop too deep, which is why it is pinned. */
static void test_route_child(void)
{
    u64 route;
    u32 depth;

    /* Device directly on the host router (route 0, depth 0) via adapter 4 --
     * the measured J414s USB3 Down adapter. */
    assert(acio_type5_route_child(0, 0, 4, &route, &depth) == 0);
    assert(route == 0x04 && depth == 1);

    /* Second level: adapter 7 on that device router. Parent depth is 1, so
     * the new hop lands in byte 1, not byte 0. */
    assert(acio_type5_route_child(0x04, 1, 7, &route, &depth) == 0);
    assert(route == 0x0704 && depth == 2);

    /* Third level keeps stacking upward, preserving earlier hops. */
    assert(acio_type5_route_child(0x0704, 2, 2, &route, &depth) == 0);
    assert(route == 0x020704 && depth == 3);

    /* Deepest legal parent depth is 5 (child depth 6). */
    assert(acio_type5_route_child(0, 5, 1, &route, &depth) == 0);
    assert(route == 0x10000000000ULL && depth == 6); /* 1 << (8*5) */
    /* Parent already at max depth must be refused, not wrapped. */
    assert(acio_type5_route_child(0, ACIO_TYPE5_MAX_ROUTE_DEPTH, 1, &route, &depth) < 0);

    /* Adapter 0 is the router itself and is never a downstream hop. */
    assert(acio_type5_route_child(0, 0, 0, &route, &depth) < 0);
    /* A parent route with bits outside the 54-bit field is malformed. */
    assert(acio_type5_route_child(~ACIO_TYPE5_ROUTE_MASK, 0, 1, &route, &depth) < 0);
    assert(acio_type5_route_child(0, 0, 1, NULL, &depth) < 0);
}

static void test_hop_offset(void)
{
    u16 offset;
    assert(ACIO_TYPE5_HOP_DWORDS == 2);
    assert(acio_type5_hop_offset(0, &offset) == 0 && offset == 0);
    assert(acio_type5_hop_offset(ACIO_TYPE5_USB3_HOP, &offset) == 0 && offset == 16);
    assert(acio_type5_hop_offset(0x7ff, &offset) == 0 && offset == 0xffe);
    /* Hop IDs are 11 bits; anything wider must be rejected, not truncated. */
    assert(acio_type5_hop_offset(0x800, &offset) < 0);
    assert(acio_type5_hop_offset(8, NULL) < 0);
}

static void test_usb3_credit_provenance(void)
{
    acio_type5_usb3_path_t tx, rx;
    acio_type5_usb3_path_defaults(&tx, &rx, false);

    /* Apple's static fallback: host side 2, device side 14. */
    assert(ACIO_TYPE5_CREDITS_HOST_SIDE == 2);
    assert(ACIO_TYPE5_CREDITS_DEVICE_SIDE == 14);
    assert(tx.source_initial_credits == 2 && rx.source_initial_credits == 2);
    assert(tx.initial_credits == 14 && tx.destination_initial_credits == 14);

    /* Router Operation 0x33 is not implemented, so nothing may request
     * firmware-recommended credits by default. */
    assert(tx.credit_options == 0 && rx.credit_options == 0);

    /* NFC credits for USB3 are provably zero, so the compare-swap is a
     * no-op and the activation path skips it. */
    assert(ACIO_TYPE5_NFC_CREDITS_USB3 == 0);

    /* hop_pack: credits occupy [23:17] and can never reach bit 24, which is
     * the separate pm_packet flag. Verified at the maximum legal value. */
    acio_type5_hop_descriptor_t d = {0};
    u8 packet[8];
    d.valid = true;
    d.initial_credits = 0x7f;
    d.suppress_out_fields = true;
    assert(acio_type5_hop_pack(&d, packet) == 0);
    u32 first = (u32)packet[0] << 24 | (u32)packet[1] << 16 |
                (u32)packet[2] << 8 | packet[3];
    assert((first & 0x00fe0000u) == 0x00fe0000u);  /* [23:17] all set */
    assert((first & 0x01000000u) == 0);            /* bit 24 clear: not pm_packet */
    assert((first & 0x80000000u) != 0);            /* valid */
    d.pm_packet = true;
    assert(acio_type5_hop_pack(&d, packet) == 0);
    assert((((u32)packet[0] << 24) & 0x01000000u) == 0x01000000u);
    /* Credits above 127 must be rejected, not truncated into bit 24. */
    d.initial_credits = 0x80;
    assert(acio_type5_hop_pack(&d, packet) < 0);
}

static void test_nfc_credits(void)
{
    acio_type5_nfc_cas_t cas;

    /* Apple issues the first attempt optimistically against zero rather than
     * reading first. */
    assert(acio_type5_nfc_begin(&cas, 8) == 0);
    assert(cas.compare == 0 && cas.write == 8 && cas.attempts == 0);

    /* ADP_CS_4 with 4 NFC buffers used of 64 total. */
    u32 cs4 = (64u << ACIO_TYPE5_TOTAL_BUF_SHIFT) | 4u;
    assert(acio_type5_nfc_next(&cas, cs4) == 0);
    assert((cas.write & ACIO_TYPE5_NFC_MASK) == 12);          /* 4 + 8 */
    assert(cas.compare == cs4);                                /* full dword */
    /* Bits outside [19:0] must be preserved verbatim, including Total Buffers. */
    assert((cas.write >> ACIO_TYPE5_TOTAL_BUF_SHIFT & ACIO_TYPE5_TOTAL_BUF_MASK) == 64);
    assert((cas.write & ~ACIO_TYPE5_NFC_MASK) == (cs4 & ~ACIO_TYPE5_NFC_MASK));
    assert(cas.attempts == 1);

    /* Exactly reaching Total Buffers is allowed. */
    assert(acio_type5_nfc_begin(&cas, 60) == 0);
    assert(acio_type5_nfc_next(&cas, (64u << ACIO_TYPE5_TOTAL_BUF_SHIFT) | 4u) == 0);
    assert((cas.write & ACIO_TYPE5_NFC_MASK) == 64);

    /* Exceeding it must fail rather than wrap -- Apple's kIOReturnNoResources. */
    assert(acio_type5_nfc_begin(&cas, 61) == 0);
    assert(acio_type5_nfc_next(&cas, (64u << ACIO_TYPE5_TOTAL_BUF_SHIFT) | 4u) < 0);

    /* A retry folds in the freshly observed value, not the stale one. */
    assert(acio_type5_nfc_begin(&cas, 2) == 0);
    assert(acio_type5_nfc_next(&cas, (64u << ACIO_TYPE5_TOTAL_BUF_SHIFT) | 4u) == 0);
    assert((cas.write & ACIO_TYPE5_NFC_MASK) == 6);
    assert(acio_type5_nfc_next(&cas, (64u << ACIO_TYPE5_TOTAL_BUF_SHIFT) | 10u) == 0);
    assert((cas.write & ACIO_TYPE5_NFC_MASK) == 12);
    assert(cas.attempts == 2);

    /* The "unspecified" sentinel must never be added as a literal. */
    assert(acio_type5_nfc_begin(&cas, ACIO_TYPE5_NFC_UNSPECIFIED) < 0);
    assert(acio_type5_nfc_begin(&cas, ACIO_TYPE5_NFC_MASK + 1) < 0);
    assert(acio_type5_nfc_begin(NULL, 1) < 0);
}

/* Counter clear: config space 3, three dwords of zero at counter_id * 3.
 * There is no release counterpart -- the ID dies with the hop-ID. */
static void test_counter_clear(void)
{
    u16 offset;
    assert(ACIO_TYPE5_COUNTER_DWORDS == 3);
    assert(acio_type5_counter_clear_offset(0, 8, &offset) == 0 && offset == 0);
    assert(acio_type5_counter_clear_offset(1, 8, &offset) == 0 && offset == 3);
    assert(acio_type5_counter_clear_offset(7, 8, &offset) == 0 && offset == 21);
    /* Bounded by getMaxCounters (ADP_CS_1 [18:8]). */
    assert(acio_type5_counter_clear_offset(8, 8, &offset) < 0);
    assert(acio_type5_counter_clear_offset(0, 0, &offset) < 0);
    assert(acio_type5_counter_clear_offset(0, 8, NULL) < 0);

    /* The max-counter field extraction itself. */
    u32 adp_cs_1 = 0x00001f00u; /* [18:8] = 0x1f */
    assert(((adp_cs_1 >> ACIO_TYPE5_MAX_COUNTERS_SHIFT) & ACIO_TYPE5_MAX_COUNTERS_MASK) == 0x1f);
}

static void test_adapter_portmap(void)
{
    /* portmap = 0100000001000000010110000101200001010e0001010e0002000000 */
    static const u32 portmap[7] = {
        0x00000001, 0x00000001, 0x00100101, 0x00200101,
        0x000e0101, 0x000e0101, 0x00000002,
    };
    /* port-defaults = 16b0802b16b0802b0840802b0840802b0948802b0948802b0b58802b */
    static const u32 defaults[7] = {
        0x2b80b016, 0x2b80b016, 0x2b804008, 0x2b804008,
        0x2b804809, 0x2b804809, 0x2b80580b,
    };
    acio_type5_adapter_t a[7];
    for (int i = 0; i < 7; i++)
        assert(acio_type5_adapter_decode(portmap[i], &defaults[i], &a[i]) == 0);

    assert(a[0].kind == ACIO_ADAPTER_LANE && !a[0].up);
    assert(a[1].kind == ACIO_ADAPTER_LANE);
    assert(a[2].kind == ACIO_ADAPTER_PCIE && !a[2].up);
    /* Adapter 4 is the host router's USB3 Down adapter -- the tunnel anchor. */
    assert(a[3].kind == ACIO_ADAPTER_USB3 && !a[3].up);
    assert(a[3].raw == ACIO_TYPE5_ADAPTER_USB3_DOWN);
    assert(a[4].kind == ACIO_ADAPTER_DP && a[5].kind == ACIO_ADAPTER_DP);
    assert(a[6].kind == ACIO_ADAPTER_NHI);

    /* Direction encoding: low byte 0x02 is up/out. */
    acio_type5_adapter_t up;
    assert(acio_type5_adapter_decode(ACIO_TYPE5_ADAPTER_USB3_UP, NULL, &up) == 0);
    assert(up.kind == ACIO_ADAPTER_USB3 && up.up);

    /* port-defaults packing {[10:0] in, [21:11] out, [31:22] buffers}. */
    for (int i = 0; i < 7; i++)
        assert(a[i].defaults_valid);
    assert(a[0].max_in_hop == (0x2b80b016u & 0x7ff));
    assert(a[0].max_out_hop == ((0x2b80b016u >> 11) & 0x7ff));
    assert(a[0].total_buffers == ((0x2b80b016u >> 22) & 0x3ff));

    /* "Not read" must be distinguishable from "read as zero" IN THE TYPE.
     * Zero is a legal value for all three fields, so a caller that never read
     * the port-default register and one that read genuine zeroes produce
     * identical numbers -- only the flag separates them. These assertions fail
     * if the pointer is ever softened back into a plain u32 default. */
    assert(!up.defaults_valid);
    assert(up.max_in_hop == 0 && up.max_out_hop == 0 && up.total_buffers == 0);

    const u32 measured_zero = 0u;
    acio_type5_adapter_t zeroed;
    assert(acio_type5_adapter_decode(ACIO_TYPE5_ADAPTER_USB3_UP,
                                     &measured_zero, &zeroed) == 0);
    assert(zeroed.defaults_valid);
    assert(zeroed.max_in_hop == 0 && zeroed.max_out_hop == 0 &&
           zeroed.total_buffers == 0);
    /* Same numbers, opposite provenance. */
    assert(zeroed.defaults_valid != up.defaults_valid);

    /* An unknown family must fail closed rather than be silently accepted. */
    acio_type5_adapter_t bad;
    assert(acio_type5_adapter_decode(0x00990101u, NULL, &bad) < 0);
}

static void test_router_topology(void)
{
    /* Arbitrary "previously read" CS_0..CS_4 with every mutable field dirty. */
    u32 cs[5] = {0xdeadbeef, 0xffffffff, 0xcafebabe, 0x12345678, 0xffffffff};
    u32 out[4];

    assert(acio_type5_router_topology_pack(cs, ACIO_TYPE5_CM_VERSION, out) == 0);

    /* CS_1: Depth [22:20] and Upstream Port [13:8] cleared; host router is
     * depth 0 / upstream 0 so nothing is OR'd back in. Every other bit of
     * the cached value must survive. */
    assert(out[0] == (0xffffffffu & 0xff8fc0ffu));
    assert((out[0] & (7u << 20)) == 0);
    assert((out[0] & (0x3fu << 8)) == 0);

    /* CS_2/CS_3: TopologyID for route 0, with the valid bit set in the same
     * write rather than a follow-up. */
    assert(out[1] == 0);
    assert(out[2] == 0x80000000u);

    /* CS_4: only [15:8] is replaced by the CM version. */
    assert(out[3] == ((0xffffffffu & 0xffff00ffu) | (ACIO_TYPE5_CM_VERSION << 8)));
    assert(((out[3] >> 8) & 0xff) == ACIO_TYPE5_CM_VERSION);

    /* Preserved-bit check against a sparse cached value. */
    u32 sparse[5] = {0, 0x00700f00, 0, 0, 0x0000ff00};
    assert(acio_type5_router_topology_pack(sparse, 0x10, out) == 0);
    assert(out[0] == 0);
    assert(out[3] == 0x00001000u);

    assert(acio_type5_router_topology_pack(NULL, 0x10, out) < 0);
    assert(acio_type5_router_topology_pack(cs, 0x10, NULL) < 0);
}

static void test_router(void)
{
    acio_type5_router_sm_t sm;
    acio_type5_router_action_t action;
    assert(acio_type5_router_start(&sm, 0x12, true) == 0);
    assert(acio_type5_router_next(&sm, &action) == 0);
    assert(action.kind == ACIO_TYPE5_ROUTER_POLL && action.space == 2 &&
           action.offset == 6 && action.mask == 0x01000000);
    assert(acio_type5_router_complete(&sm, 0, 0) < 0);
    assert(acio_type5_router_complete(&sm, 0, 0x01000000) == 0);
    assert(acio_type5_router_next(&sm, &action) == 0 && action.kind == ACIO_TYPE5_ROUTER_READ);
    assert(acio_type5_router_complete(&sm, 0, 0xffffffff) < 0);
    /* CORRECTION: 0x7FFFFFFF is ACCEPTED. Apple's `ccmn w8,#1,#4,eq ;
     * cset w21,gt` makes GT true for it, so rejecting it would fail a legal
     * router. Bit 31 set is the only value-based rejection. */
    assert(acio_type5_router_complete(&sm, 0, 0x7fffffff) == 0);
    sm.state = 1;
    assert(acio_type5_router_complete(&sm, 0, 0x80000000) < 0);
    assert(acio_type5_router_complete(&sm, 0, 0) == 0);
    assert(acio_type5_router_next(&sm, &action) == 0 && action.value == 0x03000000);
    assert(acio_type5_router_complete(&sm, 0, 0) == 0);
    assert(acio_type5_router_next(&sm, &action) == 0 && action.value == 0x83000000);
    assert(acio_type5_router_complete(&sm, 0, 0) == 0);
    assert(acio_type5_router_next(&sm, &action) == 0 && action.mask == 0x02000000);
    assert(acio_type5_router_complete(&sm, 0, 0x02000000) == 0);
    assert(acio_type5_router_next(&sm, &action) == 0 && action.kind == ACIO_TYPE5_ROUTER_DONE);

    /* The HOST router (route 0, depth 0) must stop after the single
     * ROUTER_CS_6 bit-24 readiness poll.  IOThunderboltSwitchUSB4::
     * configureRouter gates everything after that poll on `depth != 0`, so
     * the CS_5 read, the 0x03000000/0x05000000 write, the Configuration
     * Valid commit and the CS_6 bit-25 poll are the DEVICE-router path only.
     * Running them against route 0 would write a tunnelling-configuration
     * constant into the host router's CS_5, which Apple never does. */
    assert(acio_type5_router_start(&sm, 0, true) == 0);
    assert(acio_type5_router_next(&sm, &action) == 0);
    assert(action.kind == ACIO_TYPE5_ROUTER_POLL && action.space == 2 &&
           action.offset == 6 && action.mask == 0x01000000);
    assert(acio_type5_router_complete(&sm, 0, 0) < 0);
    assert(acio_type5_router_complete(&sm, 0, 0x01000000) == 0);
    assert(acio_type5_router_next(&sm, &action) == 0 &&
           action.kind == ACIO_TYPE5_ROUTER_DONE);
}

static void test_usb3(void)
{
    acio_type5_usb3_sm_t sm;
    acio_type5_usb3_action_t action;
    assert(acio_type5_usb3_start(&sm, 0, 4, 1, 3, true) == 0);
    assert(acio_type5_usb3_next(&sm, &action) == 0);
    assert(action.kind == ACIO_TYPE5_USB3_ACTIVATE_TX && action.path.path_type == 8 &&
           action.path.source_hop == 8 && action.path.credit_options == 4);
    assert(acio_type5_usb3_complete(&sm, 0, 0, 0) == 0);
    assert(acio_type5_usb3_next(&sm, &action) == 0);
    assert(action.kind == ACIO_TYPE5_USB3_ACTIVATE_RX && action.path.path_type == 7);
    assert(acio_type5_usb3_complete(&sm, 0, 0, 0) == 0);
    assert(acio_type5_usb3_next(&sm, &action) == 0);
    assert(action.kind == ACIO_TYPE5_USB3_FIND_UP_CAP && action.space == 1 &&
           action.mask == 0xff00 && action.value == 0x400);
    assert(acio_type5_usb3_complete(&sm, 0, 0x20, 0) == 0);
    assert(acio_type5_usb3_next(&sm, &action) == 0);
    assert(action.kind == ACIO_TYPE5_USB3_ENABLE_UP && action.offset == 0x20 &&
           action.value == 0xc0000000 && action.mask == 0xc0000000);
    assert(acio_type5_usb3_complete(&sm, 0, 0, 900) == 0);
    assert(acio_type5_usb3_next(&sm, &action) == 0);
    assert(action.kind == ACIO_TYPE5_USB3_WAIT_DEADLINE && action.deadline_ms == 1000);
    assert(acio_type5_usb3_complete(&sm, 0, 0, 999) < 0);
    assert(acio_type5_usb3_complete(&sm, 0, 0, 1000) == 0);
    assert(acio_type5_usb3_next(&sm, &action) == 0 &&
           action.kind == ACIO_TYPE5_USB3_FIND_DOWN_CAP);
    assert(acio_type5_usb3_complete(&sm, 0, 0x31, 1000) == 0);
    assert(acio_type5_usb3_next(&sm, &action) == 0 &&
           action.kind == ACIO_TYPE5_USB3_ENABLE_DOWN && action.offset == 0x31);
    assert(acio_type5_usb3_complete(&sm, 0, 0, 1000) == 0);
    assert(acio_type5_usb3_next(&sm, &action) == 0 && action.kind == ACIO_TYPE5_USB3_DONE);
}

/* PDF-3 plug-event ack frame.  dword2 layout {[3:0] error code 7, [13:8]
 * port, [31:30] pg} is grade-A (sendPlugEventAck + ConfigErrorCommand::
 * prepareForExecution); the pg VALUES 2/3 are provisional Linux-derived
 * constants, and this test pins them so a silent change fails loudly. */
static void test_plug_ack_pack(void)
{
    u8 packet[16];

    /* Fail closed: 54-bit route bound, 6-bit port bound, NULL out. */
    assert(acio_type5_plug_ack_pack(1ULL << 54, 0, false, packet) == -1);
    assert(acio_type5_plug_ack_pack(0, 0x40, false, packet) == -1);
    assert(acio_type5_plug_ack_pack(0, 0, false, NULL) == -1);

    /* The measured J414s case: route 0, port 5, plug.
     * dword2 = 7 | 5<<8 | 2<<30 = 0x80000507, big-endian on the wire. */
    static const u8 expected_head[12] = {
        0, 0, 0, 0, 0, 0, 0, 0, 0x80, 0x00, 0x05, 0x07,
    };
    assert(acio_type5_plug_ack_pack(0, 5, false, packet) == 0);
    assert(memcmp(packet, expected_head, sizeof(expected_head)) == 0);
    u32 crc = acio_type5_crc32c(packet, 12, NULL);
    assert(packet[12] == (u8)(crc >> 24) && packet[13] == (u8)(crc >> 16) &&
           packet[14] == (u8)(crc >> 8) && packet[15] == (u8)crc);

    /* Unplug selects pg 3: dword2 = 7 | 5<<8 | 3<<30 = 0xC0000507. */
    assert(acio_type5_plug_ack_pack(0, 5, true, packet) == 0);
    assert(packet[8] == 0xc0 && packet[9] == 0x00 && packet[10] == 0x05 &&
           packet[11] == 0x07);

    /* Route split: same 22-bit-high / 32-bit-low encoding as config
     * packets, byte-reversed onto the wire. */
    assert(acio_type5_plug_ack_pack(0x0000123456789abcULL, 1, false,
                                    packet) == 0);
    assert(packet[0] == 0x00 && packet[1] == 0x00 && packet[2] == 0x12 &&
           packet[3] == 0x34);
    assert(packet[4] == 0x56 && packet[5] == 0x78 && packet[6] == 0x9a &&
           packet[7] == 0xbc);
}

int main(void)
{
    /* Grade-A T6020 Type5 offsets from the pinned local BootKC RE corpus.
     * These assertions prevent a t8103 0x20-stride NHI layout from silently
     * being reused for J414s. */
    assert(ACIO_NHI_RING_COUNT == 12);
    assert(ACIO_TYPE5_RING_STRIDE == 0x4000);
    assert(ACIO_TYPE5_TX_RING_BASE + 11 * ACIO_TYPE5_RING_STRIDE == 0x3c000);
    assert(ACIO_TYPE5_RX_RING_BASE + 11 * ACIO_TYPE5_RING_STRIDE == 0xac000);
    assert(ACIO_TYPE5_TX_DISABLE_DONE == 0x1c);
    assert(ACIO_TYPE5_RX_DISABLE_DONE == 0x18);
    assert(ACIO_TYPE5_FW_CONTROL == 0x0c);
    assert(ACIO_TYPE5_FW_STATE == 0xa8);
    assert(ACIO_TYPE5_FW_STATE_MASK == 0x7f000000);
    assert(ACIO_TYPE5_FW_STATE_READY == 0x01000000);

    assert(ACIO_TYPE5_PDF_CONFIG_READ == 1);
    assert(ACIO_TYPE5_PDF_CONFIG_WRITE == 2);
    assert(ACIO_TYPE5_PDF_PREPARE_SLEEP == 13);

    test_lane_link_state();
    test_descriptors_and_control();
    test_config_and_capabilities();
    test_firmware_bundle();
    test_tunable_bounds();
    test_scan_and_hop();
    test_error_telemetry();
    test_route_child();
    test_hop_offset();
    test_usb3_credit_provenance();
    test_nfc_credits();
    test_counter_clear();
    test_adapter_portmap();
    test_router_topology();
    test_router();
    test_usb3();
    test_plug_ack_pack();

    puts("ACIO Type5 contract tests: PASS");
    return 0;
}
