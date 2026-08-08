/* SPDX-License-Identifier: MIT */

#include "acio.h"

#ifndef ACIO_HOST_TEST
#include "string.h"
#else
#include <string.h>
#endif

#define USB3_CAP_MASK       0x0000ff00u
#define USB3_CAP_VALUE      0x00000400u
#define USB3_ENABLE_MASK    0xc0000000u
#define USB3_ENABLE_VALUE   0xc0000000u
#define USB3_SETTLE_MS      100u

static void put_be32(u8 *out, u32 value)
{
    out[0] = (u8)(value >> 24);
    out[1] = (u8)(value >> 16);
    out[2] = (u8)(value >> 8);
    out[3] = (u8)value;
}

static u32 get_be32(const u8 *in)
{
    return (u32)in[0] << 24 | (u32)in[1] << 16 | (u32)in[2] << 8 | in[3];
}

u32 acio_type5_crc32c(const u8 *data, size_t size, void *context)
{
    (void)context;
    if (!data && size)
        return 0;

    u32 crc = ~0u;
    while (size--) {
        crc ^= *data++;
        for (u8 bit = 0; bit < 8; bit++)
            crc = (crc >> 1) ^ (0x82f63b78u & (0u - (crc & 1u)));
    }
    return ~crc;
}

const char *acio_type5_error_name(u32 error)
{
    switch (error) {
        case ACIO_TYPE5_E_NONE:
            return "none";
        case ACIO_TYPE5_E_BAD_INDEX:
            return "bad-index";
        case ACIO_TYPE5_E_BAD_PHASE:
            return "bad-phase";
        case ACIO_TYPE5_E_RESOURCES:
            return "resource-discovery";
        case ACIO_TYPE5_E_POWER_STATE5:
            return "power-state5";
        case ACIO_TYPE5_E_POWER_STATE7:
            return "power-state7";
        case ACIO_TYPE5_E_CIO_RECONFIG:
            return "cio-reconfig";
        case ACIO_TYPE5_E_PHY_PREPARE:
            return "phy-prepare";
        case ACIO_TYPE5_E_FW_BUNDLE:
            return "fw-bundle";
        case ACIO_TYPE5_E_FW_COPY:
            return "fw-copy";
        case ACIO_TYPE5_E_FW_RELEASE:
            return "fw-release";
        case ACIO_TYPE5_E_FW_READY_TIMEOUT:
            return "fw-ready-timeout";
        case ACIO_TYPE5_E_FW_ABSENT:
            return "fw-absent";
        case ACIO_TYPE5_E_FW_BUS_ERROR:
            return "fw-bus-error";
        case ACIO_TYPE5_E_TUNABLES:
            return "tunables";
        case ACIO_TYPE5_E_CTRL_SID_LAYOUT:
            return "ctrl-sid-layout";
        case ACIO_TYPE5_E_CTRL_PATH_COUNT:
            return "ctrl-path-count";
        case ACIO_TYPE5_E_CTRL_DART_INIT:
            return "ctrl-dart-init";
        case ACIO_TYPE5_E_CTRL_DART_FORCE:
            return "ctrl-dart-force-active";
        case ACIO_TYPE5_E_CTRL_ALLOC:
            return "ctrl-alloc";
        case ACIO_TYPE5_E_CTRL_IOVA:
            return "ctrl-iova";
        case ACIO_TYPE5_E_CTRL_MAP:
            return "ctrl-map";
        case ACIO_TYPE5_E_CTRL_ENABLE:
            return "ctrl-enable";
        case ACIO_TYPE5_E_CTRL_DISABLE_DONE:
            return "ctrl-disable-done";
        case ACIO_TYPE5_E_CFG_BUSY:
            return "cfg-busy";
        case ACIO_TYPE5_E_CFG_PACK:
            return "cfg-pack";
        case ACIO_TYPE5_E_CFG_DESC:
            return "cfg-descriptor";
        case ACIO_TYPE5_E_CFG_TX_TIMEOUT:
            return "cfg-tx-timeout";
        case ACIO_TYPE5_E_CFG_RX_TIMEOUT:
            return "cfg-rx-timeout";
        case ACIO_TYPE5_E_CFG_RX_LENGTH:
            return "cfg-rx-length";
        case ACIO_TYPE5_E_CFG_RX_PDF:
            return "cfg-rx-pdf";
        case ACIO_TYPE5_E_CFG_RX_REJECT:
            return "cfg-rx-reject";
        case ACIO_TYPE5_E_CFG_RING_ADVANCE:
            return "cfg-ring-advance";
        case ACIO_TYPE5_E_CFG_RX_ROUTER_ERROR:
            return "cfg-rx-router-error";
        case ACIO_TYPE5_E_ROUTER_POLL_READY:
            return "router-poll-ready";
        case ACIO_TYPE5_E_ROUTER_READ:
            return "router-read";
        case ACIO_TYPE5_E_ROUTER_WRITE:
            return "router-write";
        case ACIO_TYPE5_E_ROUTER_POLL_ACK:
            return "router-poll-ack";
        case ACIO_TYPE5_E_ROUTER_STATE:
            return "router-state";
        case ACIO_TYPE5_E_TUNNEL_STATE:
            return "tunnel-state";
        case ACIO_TYPE5_E_PIPE_COMMIT:
            return "pipe-commit";
        case ACIO_TYPE5_E_PIPE_READBACK:
            return "pipe-readback";
        case ACIO_TYPE5_E_TUNNEL_ADAPTER_CAP:
            return "tunnel-adapter-capability";
        case ACIO_TYPE5_E_SCAN_ROUTE:
            return "scan-route";
        case ACIO_TYPE5_E_SCAN_HEADER:
            return "scan-router-header";
        case ACIO_TYPE5_E_SCAN_MAXPORT:
            return "scan-maxport";
        case ACIO_TYPE5_E_SCAN_ADAPTER:
            return "scan-adapter";
        case ACIO_TYPE5_E_SCAN_NO_USB3:
            return "scan-no-usb3-up";
        case ACIO_TYPE5_E_SCAN_NOT_LANE:
            return "scan-not-lane-adapter";
        case ACIO_TYPE5_E_SCAN_LANE_CAP:
            return "scan-lane-capability";
        case ACIO_TYPE5_E_SCAN_LINK_NOT_READY:
            return "scan-link-not-ready";
        default:
            return "unknown";
    }
}

static const u32 acio_type5_fw_expected_size[ACIO_TYPE5_FW_BLOB_COUNT] = {
    [ACIO_TYPE5_FW_ACIO_TEXT] = 0x22c00,
    [ACIO_TYPE5_FW_ACIO_DATA] = 0x1960,
    [ACIO_TYPE5_FW_TMU_TEXT] = 0x25c0,
    [ACIO_TYPE5_FW_TMU_DATA] = 0xa0,
};

int acio_type5_fw_bundle_validate(const void *bundle, size_t bundle_size,
                                  acio_type5_fw_bundle_view_t *view)
{
    if (!bundle || !view || bundle_size < sizeof(acio_type5_fw_bundle_header_t) ||
        bundle_size > UINT32_MAX)
        return -1;

    const acio_type5_fw_bundle_header_t *header = bundle;
    if (header->magic != ACIO_TYPE5_FW_BUNDLE_MAGIC ||
        header->version != ACIO_TYPE5_FW_BUNDLE_VERSION ||
        header->header_size != sizeof(*header) || header->total_size != bundle_size)
        return -1;

    acio_type5_fw_bundle_view_t out = {0};
    for (u32 i = 0; i < ACIO_TYPE5_FW_BLOB_COUNT; i++) {
        const acio_type5_fw_bundle_entry_t *entry = &header->entries[i];
        if (entry->reserved || entry->size != acio_type5_fw_expected_size[i] ||
            entry->offset < header->header_size || entry->offset > header->total_size ||
            entry->size > header->total_size - entry->offset)
            return -1;

        u32 end = entry->offset + entry->size;
        for (u32 j = 0; j < i; j++) {
            u32 other_start = header->entries[j].offset;
            u32 other_end = other_start + header->entries[j].size;
            if (entry->offset < other_end && other_start < end)
                return -1;
        }

        const u8 *data = (const u8 *)bundle + entry->offset;
        if (acio_type5_crc32c(data, entry->size, NULL) != entry->crc32c)
            return -1;
        out.data[i] = data;
        out.size[i] = entry->size;
        out.crc32c[i] = entry->crc32c;
    }

    *view = out;
    return 0;
}

int acio_type5_fw_destination(u64 nhi_base, enum acio_type5_fw_blob_kind kind,
                              u64 *destination)
{
    if (!destination || kind < ACIO_TYPE5_FW_ACIO_TEXT || kind >= ACIO_TYPE5_FW_BLOB_COUNT)
        return -1;

    u64 aperture;
    u64 offset = 0;
    switch (kind) {
        case ACIO_TYPE5_FW_ACIO_TEXT:
            aperture = 0xf00000;
            break;
        case ACIO_TYPE5_FW_ACIO_DATA:
            aperture = 0xf00000;
            offset = 0x80000;
            break;
        case ACIO_TYPE5_FW_TMU_TEXT:
            aperture = 0xd00000;
            break;
        case ACIO_TYPE5_FW_TMU_DATA:
            aperture = 0xd00000;
            offset = 0x80000;
            break;
        default:
            return -1;
    }
    if (nhi_base < aperture || UINT64_MAX - (nhi_base - aperture) < offset)
        return -1;
    *destination = nhi_base - aperture + offset;
    return 0;
}

/* port-defaults packs {[10:0] MaxInHopID, [21:11] MaxOutHopID,
 * [31:22] TotalBuffers} per adapter, parallel to portmap. */
int acio_type5_adapter_decode(u32 raw, const u32 *port_default,
                              acio_type5_adapter_t *out)
{
    if (!out)
        return -1;

    out->raw = raw;
    out->up = (raw & 0xffu) == ACIO_TYPE5_ADAPTER_DIR_UP;
    out->defaults_valid = port_default != NULL;
    if (port_default) {
        out->max_in_hop = (u16)(*port_default & 0x7ffu);
        out->max_out_hop = (u16)((*port_default >> 11) & 0x7ffu);
        out->total_buffers = (u16)((*port_default >> 22) & 0x3ffu);
    } else {
        out->max_in_hop = 0;
        out->max_out_hop = 0;
        out->total_buffers = 0;
    }

    if (raw == ACIO_TYPE5_ADAPTER_INACTIVE) {
        out->kind = ACIO_ADAPTER_INACTIVE;
        out->up = false;
        return 0;
    }
    if (raw == ACIO_TYPE5_ADAPTER_LANE) {
        out->kind = ACIO_ADAPTER_LANE;
        out->up = false;
        return 0;
    }
    if (raw == ACIO_TYPE5_ADAPTER_NHI) {
        out->kind = ACIO_ADAPTER_NHI;
        out->up = false;
        return 0;
    }
    switch ((raw >> 16) & 0xffu) {
        case ACIO_TYPE5_ADAPTER_FAM_DP:
            out->kind = ACIO_ADAPTER_DP;
            break;
        case ACIO_TYPE5_ADAPTER_FAM_PCIE:
            out->kind = ACIO_ADAPTER_PCIE;
            break;
        case ACIO_TYPE5_ADAPTER_FAM_USB3:
            out->kind = ACIO_ADAPTER_USB3;
            break;
        case ACIO_TYPE5_ADAPTER_FAM_USBT:
            out->kind = ACIO_ADAPTER_USB_GEN_T;
            break;
        default:
            out->kind = ACIO_ADAPTER_UNKNOWN;
            return -1;
    }
    return 0;
}

int acio_type5_descriptor_prepare(acio_type5_descriptor_t *descriptor, u64 iova, u16 length,
                                  u8 sof, u8 eof)
{
    if (!descriptor || !iova || length > 0xfff || sof > 0xf || eof > 0xf)
        return -1;
    descriptor->address = iova;
    descriptor->metadata = ACIO_TYPE5_DESC_SW_SEED | ((u32)sof << 16) | ((u32)eof << 12) | length;
    descriptor->reserved = 0;
    return 0;
}

bool acio_type5_descriptor_complete(const acio_type5_descriptor_t *descriptor)
{
    return descriptor && (descriptor->metadata & ACIO_TYPE5_DESC_COMPLETE) != 0;
}

int acio_type5_descriptor_ack(acio_type5_descriptor_t *descriptor)
{
    if (!acio_type5_descriptor_complete(descriptor))
        return -1;
    descriptor->metadata = 0;
    return 0;
}

void acio_type5_control_ring_defaults(acio_type5_control_ring_t *ring)
{
    if (!ring)
        return;
    ring->ring = ACIO_TYPE5_CONTROL_RING;
    ring->sof_pdf_mask = 0xffff;
    ring->eof_pdf_mask = 0xffff;
    ring->max_frame_size = ACIO_TYPE5_CONTROL_FRAME_SIZE;
    ring->rx_options = ACIO_TYPE5_RING_ENABLE | ACIO_TYPE5_RX_RAW_MODE;
}

int acio_type5_ring_base(enum acio_type5_ring_direction direction, u8 ring, u32 *base)
{
    if (!base || ring >= ACIO_NHI_RING_COUNT ||
        (direction != ACIO_TYPE5_RING_TX && direction != ACIO_TYPE5_RING_RX))
        return -1;
    u32 first = direction == ACIO_TYPE5_RING_TX ? ACIO_TYPE5_TX_RING_BASE :
                                                  ACIO_TYPE5_RX_RING_BASE;
    *base = first + (u32)ring * ACIO_TYPE5_RING_STRIDE;
    return 0;
}

int acio_type5_mapper_slot(size_t mapper_count, enum acio_type5_ring_direction direction,
                           u8 ring, enum acio_sid_partition_mode *mode, u8 *slot)
{
    if (!mode || !slot || ring >= ACIO_NHI_RING_COUNT ||
        (direction != ACIO_TYPE5_RING_TX && direction != ACIO_TYPE5_RING_RX))
        return -1;

    switch (mapper_count) {
        case 1:
            *mode = ACIO_SID_PARTITION_SHARED;
            *slot = 0;
            return 0;
        case 2:
            *mode = ACIO_SID_PARTITION_DIRECTION;
            *slot = direction == ACIO_TYPE5_RING_TX ? 0 : 1;
            return 0;
        case ACIO_NHI_IRQ_COUNT:
            *mode = ACIO_SID_PARTITION_RING;
            *slot = direction == ACIO_TYPE5_RING_TX ? ring : ACIO_NHI_RING_COUNT + ring;
            return 0;
        default:
            return -1;
    }
}

int acio_type5_tunables_validate(const void *data, size_t size, u32 window_size)
{
    const u8 *bytes = data;
    if (!bytes || !size || size % 24 || !window_size)
        return -1;

    for (size_t offset = 0; offset < size; offset += 24) {
        u32 register_offset;
        u32 access_size;
        memcpy(&register_offset, bytes + offset, sizeof(register_offset));
        memcpy(&access_size, bytes + offset + 4, sizeof(access_size));
        if (access_size != 4 || window_size < access_size || register_offset % access_size ||
            register_offset > window_size - access_size)
            return -1;
    }
    return 0;
}

int acio_type5_ring_doorbell(enum acio_type5_ring_direction direction, u8 ring, u16 index,
                             u32 *offset, u32 *value)
{
    u32 base;
    if (!offset || !value || acio_type5_ring_base(direction, ring, &base) < 0)
        return -1;
    /* T6020's AppleARMIODevice provider unconditionally selects the grade-A
     * 32-bit doorbell encoding. The caller owns the required dmb ish. */
    *offset = base + 8;
    *value = direction == ACIO_TYPE5_RING_TX ? (u32)index << 16 : index;
    return 0;
}

int acio_type5_ring_next(u16 index, u16 count, u16 *next)
{
    if (!next || count < 2 || index >= count)
        return -1;
    *next = index + 1 == count ? 0 : (u16)(index + 1);
    return 0;
}

bool acio_type5_ring_full(u16 producer, u16 consumer, u16 count)
{
    u16 next;
    return acio_type5_ring_next(producer, count, &next) == 0 && next == consumer;
}

int acio_type5_rx_pdf(enum acio_type5_ring_direction direction, u8 ring, u16 sof_mask,
                      u16 eof_mask, u32 *ring_offset, u32 *mirror_offset, u32 *value)
{
    u32 base;
    if (!ring_offset || !mirror_offset || !value || direction != ACIO_TYPE5_RING_RX ||
        acio_type5_ring_base(direction, ring, &base) < 0)
        return -1;
    *ring_offset = base + 0x14;
    *mirror_offset = (u32)ring * ACIO_TYPE5_RING_STRIDE;
    *value = (u32)sof_mask << 16 | eof_mask;
    return 0;
}

u8 acio_type5_next_sequence(u32 *packed_sequences, u8 pdf)
{
    if (!packed_sequences || pdf > 0xf)
        return 0;
    u32 shift = (u32)pdf * 2;
    u8 next = (u8)(((*packed_sequences >> shift) + 1) & 3);
    *packed_sequences = (*packed_sequences & ~(3u << shift)) | ((u32)next << shift);
    return next;
}

int acio_type5_config_pack(const acio_type5_config_request_t *request, acio_type5_crc32_fn crc32,
                           void *context, u8 packet[16])
{
    if (!request || !crc32 || !packet || request->route >> 54 ||
        request->offset > 0x1fff || !request->length || request->length > 0x3f ||
        request->adapter > 0x3f || request->sequence > 3 ||
        request->space > ACIO_TYPE5_CONFIG_COUNTERS)
        return -1;

    put_be32(packet, (u32)(request->route >> 32));
    put_be32(packet + 4, (u32)request->route);
    u32 address = request->offset | ((u32)request->length << 13) |
                  ((u32)request->adapter << 19) | ((u32)request->space << 25) |
                  ((u32)request->sequence << 27);
    put_be32(packet + 8, address);
    put_be32(packet + 12, crc32(packet, 12, context));
    return 0;
}

int acio_type5_config_packet_pack(const acio_type5_config_request_t *request, u8 pdf,
                                  const void *payload, size_t payload_size,
                                  u8 *packet, size_t capacity, size_t *packet_size)
{
    if (!request || !packet || !packet_size ||
        (pdf != ACIO_TYPE5_PDF_CONFIG_READ && pdf != ACIO_TYPE5_PDF_CONFIG_WRITE))
        return -1;

    size_t expected_payload = pdf == ACIO_TYPE5_PDF_CONFIG_WRITE ? request->length * 4u : 0;
    if (payload_size != expected_payload || (payload_size && !payload) ||
        payload_size > SIZE_MAX - 16 || capacity < 16 + payload_size)
        return -1;

    /* The fixed helper validates and emits the 12-byte route/address header.
     * Its CRC is overwritten below after the optional write payload. */
    if (acio_type5_config_pack(request, acio_type5_crc32c, NULL, packet) < 0)
        return -1;
    if (payload_size) {
        /* USB4 configuration payloads are arrays of big-endian dwords.
         * Callers use native u32 values; never leak the little-endian host
         * representation onto the wire. */
        const u32 *words = payload;
        for (size_t offset = 0; offset < payload_size; offset += sizeof(u32))
            put_be32(packet + 12 + offset, words[offset / sizeof(u32)]);
    }
    /* Grade-A against ConfigWriteCommand::prepareForExecution
     * @0xfffffe000ad3e154..0xad3e190 and the identical ConfigReadCommand
     * path.  Apple computes `length = (dwords << 2) + 0xc`, i.e. the CRC
     * covers the 12-byte route/address header plus the payload and excludes
     * the CRC field itself; calls the plain byte-sequential
     * _IOThunderboltCRC32 @0xfffffe000ad22650 (NOT the byte-swapped-word
     * variant at 0xad226a0); byte-swaps the result with `rev`; and stores it
     * at `(dwords + 3) << 2` == 12 + payload.  Total frame is 16 + payload.
     *
     * The CRC itself is CRC32C/Castagnoli: the 256-entry table in the
     * kernelcache reconstructs exactly from reflected polynomial
     * 0x82F63B78 (and not from 0xEDB88320), with init 0xFFFFFFFF and a
     * final inversion. */
    put_be32(packet + 12 + payload_size,
             acio_type5_crc32c(packet, 12 + payload_size, NULL));
    *packet_size = 16 + payload_size;
    return 0;
}

int acio_type5_config_response_parse(const acio_type5_config_request_t *request, u8 pdf,
                                     const u8 *packet, size_t packet_size,
                                     u32 *first_value)
{
    return acio_type5_config_response_parse_n(request, pdf, packet, packet_size,
                                              first_value, first_value ? 1 : 0);
}

/* Grade-A host-router topology block.
 *
 * updateTopologyID writes ROUTER_CS_1..CS_4 as ONE transaction: config space
 * 2 (router), port 0, offset 1, length 4, big-endian payload.  It is preceded
 * by a length-5 read of CS_0..CS_4 because CS_1 and CS_4 are
 * read-modify-write.
 *
 *   CS_1 = cached_CS_1 & 0xFF8FC0FF        clears Depth [22:20] and
 *                                          Upstream Port [13:8], then ORs
 *                                          depth<<20 | upstream<<8.
 *                                          Host router: depth 0, upstream 0,
 *                                          so both OR terms vanish.
 *   CS_2 = 0                               TopologyID low  (route 0)
 *   CS_3 = 0x80000000                      TopologyID high | valid bit 31,
 *                                          set in this same write
 *   CS_4 = (cached_CS_4 & 0xFFFF00FF) | (cm_version << 8)
 */
int acio_type5_router_topology_pack(const u32 *cs0_4, u8 cm_version, u32 out[4])
{
    if (!cs0_4 || !out)
        return -1;
    out[0] = cs0_4[1] & 0xff8fc0ffu;
    out[1] = 0;
    out[2] = 0x80000000u;
    out[3] = (cs0_4[4] & 0xffff00ffu) | ((u32)cm_version << 8);
    return 0;
}

int acio_type5_config_response_parse_n(const acio_type5_config_request_t *request, u8 pdf,
                                       const u8 *packet, size_t packet_size,
                                       u32 *values, size_t count)
{
    if (!request || !packet ||
        (pdf != ACIO_TYPE5_PDF_CONFIG_READ && pdf != ACIO_TYPE5_PDF_CONFIG_WRITE) ||
        packet_size < 16 || packet_size % 4)
        return -1;

    u32 expected_crc = get_be32(packet + packet_size - 4);
    if (acio_type5_crc32c(packet, packet_size - 4, NULL) != expected_crc)
        return -1;

    u32 route_high = get_be32(packet);
    /* Route strings are 54 bits (mask 0x003fffffffffffff), so dword0 bits
     * [31:22] carry no route on a request.
     *
     * An earlier revision REQUIRED bit 31 here as a "response marker" and
     * rejected the frame without it.  That requirement is unproven and
     * dangerous: Apple never re-parses the route on receive at all -- it
     * matches the inbound frame against the outstanding command object --
     * and an exhaustive scan of IOThunderboltFamily's __text found no
     * 22-bit route-high mask anywhere.  If the hardware does not set that
     * bit, requiring it rejects EVERY valid reply, which would present as a
     * dead config channel rather than as a parser bug.
     *
     * So bit 31 is tolerated in either state and excluded from the route.
     * Reply identity is still fully established by the checks below: CRC,
     * route, offset, length, adapter, config space and the 2-bit sequence
     * number must all match the outstanding request. */
    u64 route = (u64)(route_high & 0x003fffffu) << 32 | get_be32(packet + 4);
    u32 address = get_be32(packet + 8);
    u16 offset = (u16)(address & 0x1fff);
    u8 length = (u8)((address >> 13) & 0x3f);
    u8 adapter = (u8)((address >> 19) & 0x3f);
    u8 space = (u8)((address >> 25) & 3);
    u8 sequence = (u8)((address >> 27) & 3);
    if (route != request->route || offset != request->offset || length != request->length ||
        space != request->space || sequence != request->sequence)
        return -1;

    /* The Adapter Num echo is NOT checked on a ROUTER-space read response.
     *
     * A router answering a read of its own config space stamps its UPSTREAM
     * ADAPTER number in this field, not the adapter the request addressed.
     * Requiring an echo therefore rejects every valid ROUTER-space read --
     * which is the whole router phase, since CS_6 readiness, the CS_0..4
     * block and every device-scan read are all space 2.  It presents as a
     * dead config channel (cfg-rx-reject on the first ever transaction)
     * rather than as a parser bug: exactly the failure mode the bit-31
     * note above describes, one field over.
     *
     * Apple does the same carve-out explicitly.  In
     * IOThunderboltConfigReadCommand::processResponse the response's
     * Adapter Num is stored but never compared, and the port comparison is
     * branched around when the Configuration Space is ROUTER:
     *     cmp  w21, #0x2      ; space == ROUTER?
     *     b.eq <skip>         ; yes -> do not compare the port
     * By contrast IOThunderboltConfigWriteCommand::processResponse compares
     * it unconditionally, so the exemption is specific to READ responses.
     * Linux agrees in drivers/thunderbolt/ctl.c check_config_address():
     * the port cannot be checked because it is set to the sender's upstream
     * port.  Hence: enforce the echo on writes, ignore it on router reads. */
    if (!(pdf == ACIO_TYPE5_PDF_CONFIG_READ && space == ACIO_TYPE5_CONFIG_ROUTER) &&
        adapter != request->adapter)
        return -1;

    size_t payload_size = packet_size - 16;
    if (pdf == ACIO_TYPE5_PDF_CONFIG_READ) {
        if (payload_size != (size_t)request->length * 4)
            return -1;
        if (count > request->length || (count && !values))
            return -1;
        for (size_t i = 0; i < count; i++)
            values[i] = get_be32(packet + 12 + i * 4);
    } else if (payload_size != 0) {
        return -1;
    }
    return 0;
}

/* One 16-byte PDF-3 plug-event ack frame.  See the provenance block over the
 * constants in acio.h; the layout is grade-A from sendPlugEventAck +
 * ConfigErrorCommand::prepareForExecution, the pg VALUES are provisional
 * (Linux-derived).  Route uses the same 22-bit-high/32-bit-low split as
 * config packets; the caller echoes route and port from the received event
 * frame, never from configuration. */
int acio_type5_plug_ack_pack(u64 route, u8 port, bool unplug, u8 packet[16])
{
    if (!packet || route >> 54 || port > 0x3f)
        return -1;
    put_be32(packet, (u32)(route >> 32));
    put_be32(packet + 4, (u32)route);
    put_be32(packet + 8,
             ACIO_TYPE5_CFG_ERR_ACK_PLUG_EVENT | ((u32)port << 8) |
                 ((unplug ? ACIO_TYPE5_PG_HOT_UNPLUG_ACK
                          : ACIO_TYPE5_PG_HOT_PLUG_ACK) << 30));
    put_be32(packet + 12, acio_type5_crc32c(packet, 12, NULL));
    return 0;
}

int acio_type5_capability_step(u16 current, u32 header, u32 mask, u32 value, bool *matched,
                               bool *finished, u16 *next)
{
    if (!current || !matched || !finished || !next || (mask & 0xff) != 0 ||
        (value & ~mask) != 0)
        return -1;
    *matched = (header & mask) == value;
    *finished = (header & 0xff0000ffu) == 0;
    *next = (u16)(header & 0xff);
    if (!*matched && !*finished && (*next == 0 || *next == current))
        return -1;
    return 0;
}

u32 acio_type5_lane_link_state(u32 lane_adp_cs_1)
{
    return (lane_adp_cs_1 >> 26) & 0xfu;
}

bool acio_type5_lane_link_up(u32 lane_adp_cs_1)
{
    u32 state = acio_type5_lane_link_state(lane_adp_cs_1);
    /* Whitelist. Every state outside the proven operational set -- including
     * the reserved encodings 8..15 -- is "not up" and must be polled, never
     * proceeded on. See the contract in acio.h. */
    return state >= ACIO_TYPE5_LANE_LINK_UP_MIN &&
           state <= ACIO_TYPE5_LANE_LINK_UP_MAX;
}

int acio_type5_scan_queue_init(acio_type5_scan_queue_t *queue, u64 *storage, size_t capacity,
                               u64 root_route)
{
    if (!queue || !storage || !capacity || root_route != 0)
        return -1;
    queue->routes = storage;
    queue->capacity = capacity;
    queue->head = 0;
    queue->count = 1;
    storage[0] = root_route;
    return 0;
}

int acio_type5_scan_queue_push(acio_type5_scan_queue_t *queue, u64 route)
{
    if (!queue || !queue->routes || !route || queue->count >= queue->capacity)
        return -1;
    for (size_t i = 0; i < queue->count; i++) {
        if (queue->routes[i] == route)
            return -1;
    }
    queue->routes[queue->count] = route;
    queue->count++;
    return 0;
}

int acio_type5_scan_queue_pop(acio_type5_scan_queue_t *queue, u64 *route)
{
    if (!queue || !route || !queue->routes || queue->head >= queue->count)
        return -1;
    *route = queue->routes[queue->head];
    queue->head++;
    return 0;
}

int acio_type5_hop_pack(const acio_type5_hop_descriptor_t *descriptor, u8 packet[8])
{
    if (!descriptor || !packet || descriptor->initial_credits > 0x7f ||
        descriptor->out_port > 0x3f || descriptor->out_hop > 0x7ff ||
        descriptor->counter_id > 0x7ff || descriptor->priority > 7)
        return -1;

    u32 first = descriptor->valid ? 0x80000000u : 0;
    first |= (u32)descriptor->initial_credits << 17;
    if (descriptor->pm_packet)
        first |= 0x01000000u;
    if (!descriptor->suppress_out_fields)
        first |= (u32)descriptor->out_port << 11 | descriptor->out_hop;

    u32 second = 0;
    second |= descriptor->egress_shared_buffering ? 0x08000000u : 0;
    second |= descriptor->ingress_shared_buffering ? 0x04000000u : 0;
    second |= descriptor->egress_flow_control ? 0x02000000u : 0;
    second |= descriptor->ingress_flow_control ? 0x01000000u : 0;
    second |= descriptor->counter_enable ? 0x00800000u : 0;
    second |= (u32)descriptor->counter_id << 12;
    second |= descriptor->drop_packet ? 0x00000800u : 0;
    second |= (u32)descriptor->priority << 8;
    second |= descriptor->weight;
    put_be32(packet, first);
    put_be32(packet + 4, second);
    return 0;
}

int acio_type5_nfc_begin(acio_type5_nfc_cas_t *cas, u32 credits)
{
    if (!cas || credits > ACIO_TYPE5_NFC_MASK)
        return -1;
    /* An "unspecified" per-hop value must never be added as a literal --
     * 0xffff credits would be nonsense and would trip the Total Buffers
     * bound on any real adapter. */
    if (credits == ACIO_TYPE5_NFC_UNSPECIFIED)
        return -1;
    cas->credits = credits;
    cas->compare = 0;
    cas->write = credits;
    cas->attempts = 0;
    return 0;
}

int acio_type5_nfc_next(acio_type5_nfc_cas_t *cas, u32 observed_cs4)
{
    if (!cas)
        return -1;

    u32 current = observed_cs4 & ACIO_TYPE5_NFC_MASK;
    u32 total = (observed_cs4 >> ACIO_TYPE5_TOTAL_BUF_SHIFT) & ACIO_TYPE5_TOTAL_BUF_MASK;
    u32 next = current + cas->credits;

    /* Apple bounds the 20-bit field against Total Buffers and returns
     * kIOReturnNoResources rather than wrapping. */
    if (next > ACIO_TYPE5_NFC_MASK || next > total)
        return -1;

    cas->compare = observed_cs4;
    cas->write = (observed_cs4 & ~ACIO_TYPE5_NFC_MASK) | next;
    cas->attempts++;
    return 0;
}

int acio_type5_counter_clear_offset(u16 counter_id, u32 max_counters, u16 *offset)
{
    if (!offset || !max_counters || counter_id >= max_counters)
        return -1;
    u32 value = (u32)counter_id * ACIO_TYPE5_COUNTER_DWORDS;
    if (value > 0x1fffu)
        return -1;
    *offset = (u16)value;
    return 0;
}

/* Child route string from a parent's route and the downstream adapter the
 * child hangs off.
 *
 *   childRoute = (parentRoute | (adapter << (8 * parentDepth))) & 54-bit mask
 *   childDepth = parentDepth + 1
 *
 * The shift uses the PARENT's depth, not the child's -- easy to get wrong,
 * and it silently produces a route one hop too deep.  Eight bits per hop,
 * LSB byte first (the hop nearest the host), so a device directly attached
 * to the host router on adapter N has route == N at depth 1.
 *
 * Depth is bounded at 6 by childDeviceScanForPort (`cmp w8,#6 ; b.lo`), so
 * only bytes 0..5 are ever populated. */
int acio_type5_route_child(u64 parent_route, u32 parent_depth, u8 adapter,
                           u64 *child_route, u32 *child_depth)
{
    if (!child_route || !child_depth || !adapter ||
        parent_depth >= ACIO_TYPE5_MAX_ROUTE_DEPTH ||
        (parent_route & ~ACIO_TYPE5_ROUTE_MASK))
        return -1;

    *child_route = (parent_route | ((u64)adapter << (8u * parent_depth))) &
                   ACIO_TYPE5_ROUTE_MASK;
    *child_depth = parent_depth + 1;
    return 0;
}

int acio_type5_hop_offset(u16 hop, u16 *offset)
{
    /* Hop IDs are 11 bits; two dwords each. Reject anything that would not
     * fit the 13-bit config-request offset field rather than truncating. */
    if (!offset || hop > 0x7ff)
        return -1;
    u32 value = (u32)hop * ACIO_TYPE5_HOP_DWORDS;
    if (value > 0x1fffu)
        return -1;
    *offset = (u16)value;
    return 0;
}

int acio_type5_router_start(acio_type5_router_sm_t *sm, u64 route,
                            bool all_parents_support_usb)
{
    if (!sm || route >> 54)
        return -1;
    memset(sm, 0, sizeof(*sm));
    sm->route = route;
    sm->all_parents_support_usb = all_parents_support_usb;
    sm->config_value = all_parents_support_usb ? 0x03000000u : 0x05000000u;
    return 0;
}

int acio_type5_router_next(const acio_type5_router_sm_t *sm, acio_type5_router_action_t *action)
{
    if (!sm || !action || sm->state > 5)
        return -1;
    memset(action, 0, sizeof(*action));
    action->route = sm->route;
    action->space = ACIO_TYPE5_CONFIG_ROUTER;
    switch (sm->state) {
        case 0:
            action->kind = ACIO_TYPE5_ROUTER_POLL;
            action->offset = 6;
            action->value = action->mask = 0x01000000u;
            break;
        case 1:
            action->kind = ACIO_TYPE5_ROUTER_READ;
            action->offset = 5;
            break;
        case 2:
            action->kind = ACIO_TYPE5_ROUTER_WRITE;
            action->offset = 5;
            action->value = sm->config_value;
            break;
        case 3:
            action->kind = ACIO_TYPE5_ROUTER_WRITE;
            action->offset = 5;
            action->value = sm->config_value | 0x80000000u;
            break;
        case 4:
            action->kind = ACIO_TYPE5_ROUTER_POLL;
            action->offset = 6;
            action->value = action->mask = 0x02000000u;
            break;
        case 5:
            action->kind = ACIO_TYPE5_ROUTER_DONE;
            break;
    }
    return 0;
}

int acio_type5_router_complete(acio_type5_router_sm_t *sm, int status, u32 value)
{
    if (!sm || sm->state >= 5 || status)
        return -1;
    if (sm->state == 0) {
        /* ROUTER_CS_6 bit 24 = router ready. */
        if ((value & 0x01000000u) != 0x01000000u)
            return -1;
        /* THE HOST ROUTER STOPS HERE.  IOThunderboltSwitchUSB4::configureRouter
         * gates everything after this first poll on `depth != 0`:
         *
         *   ldr  w8, [x19, #0x10c]   ; fThunderboltDepth
         *   cmp  w8, #0
         *   ccmp w20, #0, #0, ne     ; depth==0 -> Z=0
         *   b.ne <return>            ; always taken for the host router
         *
         * So for route 0 the whole sequence is "poll CS_6 bit 24, return".
         * The CS_5 read, the 0x03000000/0x05000000 write, the CV (bit 31)
         * commit and the CS_6 bit 25 poll are the DEVICE-router path and must
         * NOT be issued against the host router.  This mirrors Linux's
         * `usb4_switch_setup()` doing `if (!tb_route(sw)) return 0;`.
         *
         * An earlier revision here ran the full six-step sequence against
         * route 0.  That wrote a tunnelling-configuration constant and a
         * Configuration Valid commit into the host router's CS_5, which is
         * not something Apple ever does. */
        sm->state = sm->route == 0 ? 5 : 1;
        return 0;
    }
    /* ROUTER_CS_5 acceptance is bit 31 clear, and nothing else.  Apple's
     * `ccmn w8,#1,#4,eq ; cset w21,gt` accepts 0x7FFFFFFF (CMN sets N=1,V=1,
     * Z=0 so GT is true), so an earlier revision that also rejected
     * 0x7FFFFFFF was over-strict and would have failed a legal router.
     * 0xFFFFFFFF and 0x80000000 are already covered by the bit-31 test.
     *
     * The read value is DISCARDED, never OR'd into the subsequent write --
     * the written value comes solely from allParentsSupportUSBTunnels(). */
    if ((sm->state == 1 && (value & 0x80000000u)) ||
        (sm->state == 4 && (value & 0x02000000u) != 0x02000000u))
        return -1;
    sm->state++;
    return 0;
}

void acio_type5_usb3_path_defaults(acio_type5_usb3_path_t *tx, acio_type5_usb3_path_t *rx,
                                   bool use_recommended_credits)
{
    if (!tx || !rx)
        return;
    acio_type5_usb3_path_t common = {
        .priority = 3,
        .weight = 3,
        /* Host side 2, device side 14 -- Apple's static literals, which are
         * also where every Router Operation 0x33 failure path lands. */
        .source_initial_credits = ACIO_TYPE5_CREDITS_HOST_SIDE,
        .initial_credits = ACIO_TYPE5_CREDITS_DEVICE_SIDE,
        .destination_initial_credits = ACIO_TYPE5_CREDITS_DEVICE_SIDE,
        .counter_enable = 7,
        .ingress_flow_control = 7,
        .egress_flow_control = 3,
        .source_hop = 8,
        .destination_hop = 8,
        .routing_options = 0x17,
        .credit_options = use_recommended_credits ? 4 : 0,
    };
    *tx = common;
    *rx = common;
    tx->path_type = 8;
    rx->path_type = 7;
}

int acio_type5_usb3_start(acio_type5_usb3_sm_t *sm, u64 up_route, u8 up_port, u64 down_route,
                          u8 down_port, bool use_recommended_credits)
{
    if (!sm || up_route >> 54 || down_route >> 54 ||
        up_port > 0x3f || down_port > 0x3f)
        return -1;
    memset(sm, 0, sizeof(*sm));
    sm->up_route = up_route;
    sm->down_route = down_route;
    sm->up_port = up_port;
    sm->down_port = down_port;
    sm->use_recommended_credits = use_recommended_credits;
    return 0;
}

int acio_type5_usb3_next(const acio_type5_usb3_sm_t *sm, acio_type5_usb3_action_t *action)
{
    if (!sm || !action || sm->state > 7)
        return -1;
    memset(action, 0, sizeof(*action));
    action->space = ACIO_TYPE5_CONFIG_ADAPTER;
    switch (sm->state) {
        case 0:
            action->kind = ACIO_TYPE5_USB3_ACTIVATE_TX;
            acio_type5_usb3_path_defaults(&action->path, &(acio_type5_usb3_path_t){0},
                                           sm->use_recommended_credits);
            break;
        case 1:
            action->kind = ACIO_TYPE5_USB3_ACTIVATE_RX;
            acio_type5_usb3_path_defaults(&(acio_type5_usb3_path_t){0}, &action->path,
                                           sm->use_recommended_credits);
            break;
        case 2:
            action->kind = ACIO_TYPE5_USB3_FIND_UP_CAP;
            action->route = sm->up_route;
            action->port = sm->up_port;
            action->mask = USB3_CAP_MASK;
            action->value = USB3_CAP_VALUE;
            break;
        case 3:
            action->kind = ACIO_TYPE5_USB3_ENABLE_UP;
            action->route = sm->up_route;
            action->port = sm->up_port;
            action->offset = sm->up_cap;
            action->mask = USB3_ENABLE_MASK;
            action->value = USB3_ENABLE_VALUE;
            break;
        case 4:
            action->kind = ACIO_TYPE5_USB3_WAIT_DEADLINE;
            action->deadline_ms = sm->deadline_ms;
            break;
        case 5:
            action->kind = ACIO_TYPE5_USB3_FIND_DOWN_CAP;
            action->route = sm->down_route;
            action->port = sm->down_port;
            action->mask = USB3_CAP_MASK;
            action->value = USB3_CAP_VALUE;
            break;
        case 6:
            action->kind = ACIO_TYPE5_USB3_ENABLE_DOWN;
            action->route = sm->down_route;
            action->port = sm->down_port;
            action->offset = sm->down_cap;
            action->mask = USB3_ENABLE_MASK;
            action->value = USB3_ENABLE_VALUE;
            break;
        case 7:
            action->kind = ACIO_TYPE5_USB3_DONE;
            break;
    }
    return 0;
}

int acio_type5_usb3_complete(acio_type5_usb3_sm_t *sm, int status, u32 value, u64 now_ms)
{
    if (!sm || sm->state >= 7 || status)
        return -1;
    if (sm->state == 2) {
        if (!value || value > 0x1fff)
            return -1;
        sm->up_cap = (u16)value;
    } else if (sm->state == 3) {
        if (now_ms > UINT64_MAX - USB3_SETTLE_MS)
            return -1;
        sm->deadline_ms = now_ms + USB3_SETTLE_MS;
    } else if (sm->state == 4) {
        if (now_ms < sm->deadline_ms)
            return -1;
    } else if (sm->state == 5) {
        if (!value || value > 0x1fff)
            return -1;
        sm->down_cap = (u16)value;
    }
    sm->state++;
    return 0;
}
