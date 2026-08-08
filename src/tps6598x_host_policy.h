/* SPDX-License-Identifier: MIT */

#ifndef TPS6598X_HOST_POLICY_H
#define TPS6598X_HOST_POLICY_H

#ifdef TPS6598X_HOST_POLICY_HOST_TEST
#include <stdbool.h>
#include <stdint.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef int32_t s32;
#else
#include "types.h"
#endif

#define TPS6598X_SYSTEM_CONFIG_LEN 17

enum tps6598x_host_policy_result {
    TPS6598X_HOST_POLICY_SKIP = 0,
    TPS6598X_HOST_POLICY_READY = 1,
    TPS6598X_HOST_POLICY_UPDATED = 2,
    TPS6598X_HOST_POLICY_ERR_ARGUMENT = -1,
    TPS6598X_HOST_POLICY_ERR_PORT = -2,
    TPS6598X_HOST_POLICY_ERR_ROLE = -3,
    TPS6598X_HOST_POLICY_ERR_READBACK = -4,
};

/* Low STATUS bits used to decide whether a live contract must be preserved. */
#define TPS6598X_HOST_STATUS_PLUG_PRESENT (1U << 0)
#define TPS6598X_HOST_STATUS_PORTROLE     (1U << 5)
#define TPS6598X_HOST_STATUS_DATAROLE     (1U << 6)

int tps6598x_host_port_resolve(u32 rid, u32 port_number, const char *port_location,
                               u32 port_location_size, u32 controller_count,
                               u32 *controller_index);

int tps6598x_host_policy_prepare(u32 hpm_index, u32 controller_count, s32 preserved_index,
                                 const u8 current[TPS6598X_SYSTEM_CONFIG_LEN],
                                 u8 desired[TPS6598X_SYSTEM_CONFIG_LEN]);
int tps6598x_host_policy_live_contract(u32 status);

/* What to do about a port after plug detection has been given time to settle.
 *
 * `plug_settled` is true when STATUS reported PLUG_PRESENT within the budget.
 *
 * The distinction this encodes, and the reason it exists: an empty port is NOT
 * an unconfigured port. m1n1 used to reach its SYSTEM_CONFIG rewrite by
 * exactly one route -- `live_contract` returning SKIP because PLUG_PRESENT was
 * clear -- so "nothing is attached" was silently treated as "this port needs
 * configuring as Source/DFP". Measured on this target, both ports read no plug
 * at boot while the SSD and the Ethernet adapter were physically attached the
 * whole time, so the rewrite fired for two ports that simply had not finished
 * Type-C detection, and the chip rejected it. Rewriting the System
 * Configuration of a port we cannot even see a cable on is acting on an
 * absence of evidence.
 */
enum {
    /* Attached and already Source/DFP: leave the live contract alone. */
    TPS6598X_HOST_PORT_PRESERVE = 0,
    /* Nothing attached after the settle budget: nothing to prepare. */
    TPS6598X_HOST_PORT_EMPTY = 1,
    /* Attached in a role we must not silently rewrite. */
    TPS6598X_HOST_PORT_WRONG_ROLE = 2,
};
int tps6598x_host_policy_port_action(u32 status, bool plug_settled);
/* True only for an attached Source/DFP contract whose negotiated transport
 * is direct USB3. USB4, Thunderbolt, DisplayPort, USB2-only, disconnected,
 * and contradictory modal states all fail closed. */
int tps6598x_direct_usb3_ready(u32 status, u32 data_status);
int tps6598x_host_policy_verify(const u8 expected[TPS6598X_SYSTEM_CONFIG_LEN],
                                const u8 readback[TPS6598X_SYSTEM_CONFIG_LEN]);

#endif
