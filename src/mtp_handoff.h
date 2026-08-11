/* SPDX-License-Identifier: MIT */

#ifndef MTP_HANDOFF_H
#define MTP_HANDOFF_H

/*
 * Boot the J414s MTP RTKit endpoint for the Windows-native-AIC guest, then
 * leave the DockChannel FIFO and interrupt state entirely to Windows.
 *
 * This is intentionally a no-op unless both the build-time Windows profile
 * and the runtime J414s board check match.  See docs/windows-mtp-handoff.md.
 */
void mtp_handoff_init(void);

/*
 * Back the guest's view of the firmware staging window with the real carveout.
 * Call once from hv_start(), after the host's broad mappings are complete.
 */
void mtp_handoff_map_guest_staging(void);

/*
 * Service the IOP's RTKit mailbox.  Call for as long as the guest runs:
 * nothing in Windows owns this mailbox, and an unacknowledged syslog backlog
 * stops the IOP producing HID reports.
 *
 * Safe from any core with or without the big hypervisor lock: internally
 * rate-limited, single-servicer, message-bounded, silent, and never blocking
 * (rtkit_service_quiet).  The primary call site is the unlocked WFI-idle path
 * in hv_exc.c; hv_tick() provides a 1 Hz floor.
 */
void mtp_handoff_poll(void);

/*
 * Print the one-shot notice for an IOP crash detected by mtp_handoff_poll().
 * Call only where a bounded printf is acceptable (hv_tick, under the lock).
 */
void mtp_handoff_report(void);

#endif
