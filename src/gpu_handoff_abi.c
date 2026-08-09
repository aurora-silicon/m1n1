/* SPDX-License-Identifier: MIT */

#ifdef GPU_HANDOFF_ABI_HOST_TEST
#include <string.h>
#else
#include "string.h"
#endif
#include "gpu_handoff_abi.h"

u32 gpu_handoff_v1_crc32(const void *data, u32 length)
{
    const u8 *bytes = data;
    u32 crc = 0xffffffffU;

    for (u32 index = 0; index < length; index++) {
        crc ^= bytes[index];
        for (u32 bit = 0; bit < 8; bit++)
            crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
    }
    return ~crc;
}

u64 gpu_handoff_v1_stamp_offset(u64 map_size, u64 payload_size)
{
    u64 offset;

    if (map_size < GPU_HANDOFF_V1_STAMP_SIZE)
        return 0;

    offset = map_size - GPU_HANDOFF_V1_STAMP_SIZE;

    /*
     * The stamp must never overlap the payload. If it did, the driver's
     * has-data scan would see stamp bytes and an all-zero payload would look
     * populated -- reintroducing exactly the correlate-vs-identify defect this
     * whole mechanism exists to remove.
     */
    if (payload_size == 0 || payload_size > offset)
        return 0;

    return offset;
}

void gpu_handoff_v1_stamp_seal(struct gpu_handoff_stamp_v1 *stamp)
{
    if (!stamp)
        return;
    stamp->stamp_crc32 = 0;
    stamp->stamp_crc32 = gpu_handoff_v1_crc32(stamp, sizeof(*stamp));
}

int gpu_handoff_v1_stamp_validate(const struct gpu_handoff_stamp_v1 *stamp, u32 expected_role)
{
    struct gpu_handoff_stamp_v1 copy;
    u32 recorded;

    if (!stamp || expected_role >= GPU_HANDOFF_V1_ROLE_COUNT)
        return -1;

    if (stamp->signature != GPU_HANDOFF_V1_SIGNATURE || stamp->version != GPU_HANDOFF_V1_VERSION ||
        stamp->structure_size != GPU_HANDOFF_V1_STAMP_SIZE)
        return -2;

    if (stamp->role != expected_role)
        return -3;

    if (stamp->reserved0 || stamp->reserved1 || stamp->reserved2 || stamp->reserved3)
        return -4;

    memcpy(&copy, stamp, sizeof(copy));
    recorded = copy.stamp_crc32;
    copy.stamp_crc32 = 0;
    if (recorded == 0 || gpu_handoff_v1_crc32(&copy, sizeof(copy)) != recorded)
        return -5;

    return 0;
}
