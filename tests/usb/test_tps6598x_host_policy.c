/* SPDX-License-Identifier: MIT */

#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "tps6598x_host_policy.h"

static void fill_config(u8 config[TPS6598X_SYSTEM_CONFIG_LEN], u8 port_info)
{
    for (size_t i = 0; i < TPS6598X_SYSTEM_CONFIG_LEN; ++i)
        config[i] = (u8)(0xa0U + (u8)i);
    config[0] = (u8)((config[0] & (u8)~7U) | port_info);
}

static void test_dual_role_becomes_source_dfp(void)
{
    u8 current[TPS6598X_SYSTEM_CONFIG_LEN];
    u8 desired[TPS6598X_SYSTEM_CONFIG_LEN];
    fill_config(current, 3);

    assert(tps6598x_host_policy_prepare(2, 4, 1, current, desired) == TPS6598X_HOST_POLICY_UPDATED);
    assert((desired[0] & 7U) == 5U);
    assert((desired[0] & (u8)~7U) == (current[0] & (u8)~7U));
    assert(memcmp(&desired[1], &current[1], TPS6598X_SYSTEM_CONFIG_LEN - 1) == 0);

    fill_config(current, 2);
    assert(tps6598x_host_policy_prepare(2, 4, 1, current, desired) == TPS6598X_HOST_POLICY_UPDATED);
    assert((desired[0] & 7U) == 4U);

    /* XHC1/storage is also a non-proxy host port and keeps all non-role bytes. */
    fill_config(current, 3);
    assert(tps6598x_host_policy_prepare(1, 3, 0, current, desired) ==
           TPS6598X_HOST_POLICY_UPDATED);
    assert((desired[0] & 7U) == 5U);
    assert(memcmp(&desired[1], &current[1], TPS6598X_SYSTEM_CONFIG_LEN - 1) == 0);
}

static void test_proxy_and_wrong_port_are_rejected(void)
{
    u8 current[TPS6598X_SYSTEM_CONFIG_LEN];
    u8 desired[TPS6598X_SYSTEM_CONFIG_LEN];
    fill_config(current, 3);

    memset(desired, 0x5a, sizeof(desired));
    u8 untouched[sizeof(desired)];
    memcpy(untouched, desired, sizeof(untouched));

    assert(tps6598x_host_policy_prepare(1, 4, 1, current, desired) == TPS6598X_HOST_POLICY_SKIP);
    assert(memcmp(desired, untouched, sizeof(desired)) == 0);
    assert(tps6598x_host_policy_prepare(4, 4, 1, current, desired) ==
           TPS6598X_HOST_POLICY_ERR_PORT);
}

static void test_adt_port_identity_not_hpm_child_name_controls_mapping(void)
{
    u32 index = 99;

    /* Child labels are irrelevant: rid/port/location are authoritative. */
    assert(tps6598x_host_port_resolve(0, 1, "left-back", 10, 3, &index) ==
           TPS6598X_HOST_POLICY_READY);
    assert(index == 0);
    assert(tps6598x_host_port_resolve(1, 2, "left-front", 11, 3, &index) ==
           TPS6598X_HOST_POLICY_READY);
    assert(index == 1);
    assert(tps6598x_host_port_resolve(2, 3, "right", 6, 3, &index) ==
           TPS6598X_HOST_POLICY_READY);
    assert(index == 2);

    /* J414s hpm5 is a non-port function and can never resolve into USB. */
    assert(tps6598x_host_port_resolve(5, 6, "internal", 9, 3, &index) ==
           TPS6598X_HOST_POLICY_ERR_PORT);
    assert(tps6598x_host_port_resolve(2, 3, "left", 5, 3, &index) ==
           TPS6598X_HOST_POLICY_ERR_PORT);
}

static void test_unsupported_power_paths_fail_closed(void)
{
    u8 current[TPS6598X_SYSTEM_CONFIG_LEN];
    u8 desired[TPS6598X_SYSTEM_CONFIG_LEN];

    for (u8 role = 0; role < 8; ++role) {
        fill_config(current, role);
        int result = tps6598x_host_policy_prepare(2, 4, 1, current, desired);
        if (role == 0 || role == 1 || role == 7)
            assert(result == TPS6598X_HOST_POLICY_ERR_ROLE);
    }
}

static void test_exact_readback_is_mandatory(void)
{
    u8 expected[TPS6598X_SYSTEM_CONFIG_LEN];
    u8 readback[TPS6598X_SYSTEM_CONFIG_LEN];
    fill_config(expected, 5);
    memcpy(readback, expected, sizeof(readback));

    assert(tps6598x_host_policy_verify(expected, readback) == TPS6598X_HOST_POLICY_READY);
    readback[16] ^= 1U;
    assert(tps6598x_host_policy_verify(expected, readback) == TPS6598X_HOST_POLICY_ERR_READBACK);
}

static void test_live_contract_is_preserved_or_rejected(void)
{
    assert(tps6598x_host_policy_live_contract(0) == TPS6598X_HOST_POLICY_SKIP);
    assert(tps6598x_host_policy_live_contract(TPS6598X_HOST_STATUS_PORTROLE |
                                              TPS6598X_HOST_STATUS_DATAROLE) ==
           TPS6598X_HOST_POLICY_SKIP);

    const u32 attached_host = TPS6598X_HOST_STATUS_PLUG_PRESENT |
                              TPS6598X_HOST_STATUS_PORTROLE |
                              TPS6598X_HOST_STATUS_DATAROLE;
    assert(tps6598x_host_policy_live_contract(attached_host) == TPS6598X_HOST_POLICY_READY);
    assert(tps6598x_host_policy_live_contract(attached_host | (1U << 7)) ==
           TPS6598X_HOST_POLICY_READY);

    assert(tps6598x_host_policy_live_contract(TPS6598X_HOST_STATUS_PLUG_PRESENT) ==
           TPS6598X_HOST_POLICY_ERR_ROLE);
    assert(tps6598x_host_policy_live_contract(TPS6598X_HOST_STATUS_PLUG_PRESENT |
                                              TPS6598X_HOST_STATUS_DATAROLE) ==
           TPS6598X_HOST_POLICY_ERR_ROLE);
}

static void test_direct_usb3_requires_a_plain_usb3_host_contract(void)
{
    const u32 host = TPS6598X_HOST_STATUS_PLUG_PRESENT |
                     TPS6598X_HOST_STATUS_PORTROLE |
                     TPS6598X_HOST_STATUS_DATAROLE;
    const u32 connected = (1U << 0);
    const u32 usb3 = (1U << 5);

    assert(tps6598x_direct_usb3_ready(host, connected | usb3));
    assert(!tps6598x_direct_usb3_ready(0, connected | usb3));
    assert(!tps6598x_direct_usb3_ready(host, connected));
    assert(!tps6598x_direct_usb3_ready(host, usb3));
    assert(!tps6598x_direct_usb3_ready(host, connected | usb3 | (1U << 8)));
    assert(!tps6598x_direct_usb3_ready(host, connected | usb3 | (1U << 16)));
    assert(!tps6598x_direct_usb3_ready(host, connected | (1U << 23)));
    /* The preserved failing cable state: USB2 + USB4, no direct USB3. */
    assert(!tps6598x_direct_usb3_ready(host, 0x96800013U));
}

/* Port action after plug detection has been given time to settle.
 *
 * The bug this encodes: the SYSTEM_CONFIG rewrite was reachable by EXACTLY one
 * route -- PLUG_PRESENT clear -- so "nothing is attached" was read as "this
 * port needs configuring". On this target both ports read no plug at boot
 * while both cables were physically attached, so the rewrite fired for two
 * ports that had merely not finished Type-C detection, and the chip rejected
 * it. An empty port is not an unconfigured port.
 */
static void test_port_action_after_settle(void)
{
    const u32 plug = TPS6598X_HOST_STATUS_PLUG_PRESENT;
    const u32 src = TPS6598X_HOST_STATUS_PORTROLE;
    const u32 dfp = TPS6598X_HOST_STATUS_DATAROLE;

    /* Attached and already the role we want: preserve, never rewrite. */
    assert(tps6598x_host_policy_port_action(plug | src | dfp, true) ==
           TPS6598X_HOST_PORT_PRESERVE);

    /* Attached in the wrong role: refuse, never rewrite. */
    assert(tps6598x_host_policy_port_action(plug, true) == TPS6598X_HOST_PORT_WRONG_ROLE);
    assert(tps6598x_host_policy_port_action(plug | src, true) == TPS6598X_HOST_PORT_WRONG_ROLE);
    assert(tps6598x_host_policy_port_action(plug | dfp, true) == TPS6598X_HOST_PORT_WRONG_ROLE);

    /* Nothing attached after the budget: EMPTY, and emphatically not a
     * request to rewrite anything. */
    assert(tps6598x_host_policy_port_action(0, false) == TPS6598X_HOST_PORT_EMPTY);

    /* The settle flag is authoritative over a stale status word: if plug
     * detection never completed we must not act on whatever the last read
     * happened to contain, even if it looks fully populated. This assertion
     * fails if anyone "simplifies" the function to test the status bit alone.
     */
    assert(tps6598x_host_policy_port_action(plug | src | dfp, false) ==
           TPS6598X_HOST_PORT_EMPTY);

    /* And a settled read with the bit clear is still empty -- both inputs
     * must agree before we touch a port. */
    assert(tps6598x_host_policy_port_action(src | dfp, true) == TPS6598X_HOST_PORT_EMPTY);
}

int main(void)
{
    test_dual_role_becomes_source_dfp();
    test_proxy_and_wrong_port_are_rejected();
    test_adt_port_identity_not_hpm_child_name_controls_mapping();
    test_unsupported_power_paths_fail_closed();
    test_exact_readback_is_mandatory();
    test_live_contract_is_preserved_or_rejected();
    test_direct_usb3_requires_a_plain_usb3_host_contract();
    test_port_action_after_settle();
    puts("TPS6598x host-policy tests: PASS");
    return 0;
}
