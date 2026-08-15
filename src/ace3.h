/* SPDX-License-Identifier: MIT */

#ifndef ACE3_H
#define ACE3_H

#include "spmi.h"
#include "types.h"

/*
 * ACE3 is the USB-C/USB-PD controller on M3+ machines (ADT compatible
 * "usbc,sn201202x,spmi").  It speaks the same TI TPS6598x/CD321x logical
 * register set as its I2C predecessors, but reaches it through a thin SPMI
 * transport rather than raw I2C -- see
 * OtherResources/re-work/asahi-docs/docs/hw/peripherals/ace3.md.
 */

/* TI logical registers (linux/drivers/usb/typec/tipd/core.c:30-54). */
#define ACE3_REG_MODE               0x03
#define ACE3_REG_CMD1               0x08
#define ACE3_REG_DATA1              0x09
#define ACE3_REG_STATUS             0x1a
#define ACE3_REG_SYSTEM_POWER_STATE 0x20

/* 4CC task return codes, DATA1 byte 0 (tipd/core.c:121-122). */
#define ACE3_TASK_SUCCESS  0
#define ACE3_TASK_TIMEOUT  1
#define ACE3_TASK_REJECTED 3

/*
 * System power states for the "SSPS" command, as used by macOS'
 * AppleHPMInterface::setPowerState (which passes 3 on its sleep path).
 */
#define ACE3_SYSTEM_POWER_STATE_S0    0
#define ACE3_SYSTEM_POWER_STATE_SLEEP 3

/* Read/write a logical register.  `len` is capped at the register's own size. */
int ace3_read(spmi_dev_t *dev, u8 sid, u8 lreg, u8 *bfr, size_t len);
int ace3_write(spmi_dev_t *dev, u8 sid, u8 lreg, const u8 *bfr, size_t len);

/*
 * Execute a 4CC command.  `cmd` is the four command characters in literal
 * order ("SSPS", not "SPSS"): macOS builds SSPS as the constant 0x53505353,
 * whose little-endian in-memory bytes are 'S','S','P','S'.  `in`/`in_len` is
 * written to DATA1 before the command is issued, and *rc (if non-NULL)
 * receives DATA1 byte 0 afterwards.
 */
int ace3_exec(spmi_dev_t *dev, u8 sid, const char cmd[4], const u8 *in, size_t in_len, u8 *rc);

/*
 * Tell one controller the system power state.  This is the step that makes a
 * port willing to source VBUS at all: an ACE3 that has never been told comes up
 * reading 0x07 in SYSTEM_POWER_STATE, accepts power happily, and refuses to
 * source -- so no passive USB device is ever detected.
 */
int ace3_set_system_power_state(spmi_dev_t *dev, u8 sid, u8 state);

/*
 * Walk `spmi_path`'s hpm* children and set every one to S0.  Best-effort: a
 * missing bus, an unreadable child or a refusing controller logs and continues,
 * because a port that cannot be woken must degrade to today's behaviour rather
 * than abort USB bring-up.  Returns the number of controllers set.
 */
int ace3_power_on_ports(const char *spmi_path);

#endif
