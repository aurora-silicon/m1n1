/* SPDX-License-Identifier: MIT */

#ifdef TPS6598X_HOST_POLICY_HOST_TEST
#include <string.h>
#else
#include "string.h"
#endif
#include "tps6598x_host_policy.h"

#define TPS6598X_PORT_INFO_MASK 0x07

#define TPS6598X_DATA_CONNECTION      (1U << 0)
#define TPS6598X_DATA_USB3_CONNECTION (1U << 5)
#define TPS6598X_DATA_DP_CONNECTION   (1U << 8)
#define TPS6598X_DATA_TBT_CONNECTION  (1U << 16)
#define TPS6598X_DATA_USB4_CONNECTION (1U << 23)

int tps6598x_host_port_resolve(u32 rid, u32 port_number, const char *port_location,
                               u32 port_location_size, u32 controller_count,
                               u32 *controller_index)
{
    if (!port_location || !port_location_size || !controller_index)
        return TPS6598X_HOST_POLICY_ERR_ARGUMENT;
    if (rid >= controller_count || port_number != rid + 1)
        return TPS6598X_HOST_POLICY_ERR_PORT;
    if (port_location[port_location_size - 1] != '\0' || port_location[0] == '\0')
        return TPS6598X_HOST_POLICY_ERR_PORT;

    const char *expected_location;
    switch (rid) {
        case 0:
            expected_location = "left-back";
            break;
        case 1:
            expected_location = "left-front";
            break;
        case 2:
            expected_location = "right";
            break;
        default:
            return TPS6598X_HOST_POLICY_ERR_PORT;
    }
    if (strcmp(port_location, expected_location))
        return TPS6598X_HOST_POLICY_ERR_PORT;

    *controller_index = rid;
    return TPS6598X_HOST_POLICY_READY;
}

/*
 * TPS6598x System Configuration.PortInfo values provide matching dual-role
 * encodings that select the preferred initial role without changing whether
 * PR_Swap or DR_Swap is supported:
 *
 *   010b Sink/UFP, PR_Swap    -> 100b Source/DFP, PR_Swap
 *   011b Sink/UFP, PR+DR Swap -> 101b Source/DFP, PR+DR Swap
 *
 * Sink-only, accessory and disabled configurations are not rewritten: their
 * power-path configuration is not proven capable of safely sourcing VBUS.
 */
int tps6598x_host_policy_prepare(u32 hpm_index, u32 controller_count, s32 preserved_index,
                                 const u8 current[TPS6598X_SYSTEM_CONFIG_LEN],
                                 u8 desired[TPS6598X_SYSTEM_CONFIG_LEN])
{
    if (!current || !desired)
        return TPS6598X_HOST_POLICY_ERR_ARGUMENT;
    if (hpm_index >= controller_count)
        return TPS6598X_HOST_POLICY_ERR_PORT;
    if (preserved_index >= 0 && hpm_index == (u32)preserved_index)
        return TPS6598X_HOST_POLICY_SKIP;

    memcpy(desired, current, TPS6598X_SYSTEM_CONFIG_LEN);

    u8 port_info = current[0] & TPS6598X_PORT_INFO_MASK;
    u8 host_port_info;
    switch (port_info) {
        case 2:
            host_port_info = 4;
            break;
        case 3:
            host_port_info = 5;
            break;
        case 4:
        case 5:
        case 6:
            return TPS6598X_HOST_POLICY_READY;
        default:
            return TPS6598X_HOST_POLICY_ERR_ROLE;
    }

    desired[0] = (current[0] & (u8)~TPS6598X_PORT_INFO_MASK) | host_port_info;
    return TPS6598X_HOST_POLICY_UPDATED;
}

/*
 * Never rewrite static role preferences underneath an attached device.  A
 * currently attached Source/DFP contract is already exactly what Windows
 * needs, while changing System Configuration does not renegotiate an
 * attached contract and can disturb an otherwise healthy USB link.
 *
 * SKIP means no live contract exists, so the caller may apply the normal
 * preferred-role policy.  An attached contract in either wrong role is
 * rejected rather than modified in place.
 */
int tps6598x_host_policy_port_action(u32 status, bool plug_settled)
{
    /* No cable, after we waited for one. There is nothing to preserve and
     * nothing to configure. Crucially this is NOT a request to rewrite the
     * System Configuration: see the contract in the header. */
    if (!plug_settled || !(status & TPS6598X_HOST_STATUS_PLUG_PRESENT))
        return TPS6598X_HOST_PORT_EMPTY;

    const u32 required = TPS6598X_HOST_STATUS_PORTROLE | TPS6598X_HOST_STATUS_DATAROLE;
    if ((status & required) == required)
        return TPS6598X_HOST_PORT_PRESERVE;

    return TPS6598X_HOST_PORT_WRONG_ROLE;
}

int tps6598x_host_policy_live_contract(u32 status)
{
    if (!(status & TPS6598X_HOST_STATUS_PLUG_PRESENT))
        return TPS6598X_HOST_POLICY_SKIP;

    const u32 required = TPS6598X_HOST_STATUS_PORTROLE | TPS6598X_HOST_STATUS_DATAROLE;
    if ((status & required) == required)
        return TPS6598X_HOST_POLICY_READY;

    return TPS6598X_HOST_POLICY_ERR_ROLE;
}

int tps6598x_direct_usb3_ready(u32 status, u32 data_status)
{
    const u32 host = TPS6598X_HOST_STATUS_PLUG_PRESENT |
                     TPS6598X_HOST_STATUS_PORTROLE |
                     TPS6598X_HOST_STATUS_DATAROLE;
    const u32 tunneled = TPS6598X_DATA_DP_CONNECTION |
                         TPS6598X_DATA_TBT_CONNECTION |
                         TPS6598X_DATA_USB4_CONNECTION;

    if ((status & host) != host)
        return 0;
    if (!(data_status & TPS6598X_DATA_CONNECTION) ||
        !(data_status & TPS6598X_DATA_USB3_CONNECTION))
        return 0;
    if (data_status & tunneled)
        return 0;
    return 1;
}

int tps6598x_host_policy_verify(const u8 expected[TPS6598X_SYSTEM_CONFIG_LEN],
                                const u8 readback[TPS6598X_SYSTEM_CONFIG_LEN])
{
    if (!expected || !readback)
        return TPS6598X_HOST_POLICY_ERR_ARGUMENT;
    if (memcmp(expected, readback, TPS6598X_SYSTEM_CONFIG_LEN))
        return TPS6598X_HOST_POLICY_ERR_READBACK;
    return TPS6598X_HOST_POLICY_READY;
}
