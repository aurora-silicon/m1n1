/* SPDX-License-Identifier: MIT */

#ifndef RTKIT_H
#define RTKIT_H

#include "asc.h"
#include "dart.h"
#include "iova.h"
#include "sart.h"
#include "types.h"

typedef struct rtkit_dev rtkit_dev_t;

struct rtkit_message {
    u8 ep;
    u64 msg;
};

struct rtkit_buffer {
    void *bfr;
    u64 dva;
    size_t sz;
};

rtkit_dev_t *rtkit_init(const char *name, asc_dev_t *asc, dart_dev_t *dart,
                        iova_domain_t *dart_iovad, sart_dev_t *sart, bool sram);
/*
 * Permit IOP-owned preallocated buffers only inside one explicit physical
 * aperture while retaining the normal DART/SART mapping path for AP-owned
 * buffers. This is intended for coprocessors such as MTP that combine a DART
 * with a dedicated SRAM region.
 */
bool rtkit_set_phys_window(rtkit_dev_t *rtk, u64 base, size_t size);
/* Translate IOP-provided SRAM IOVAs into one explicit CPU-visible SRAM
 * aperture. Apple ACIO uses IOVA 0x10000000 while its SRAM is mapped at a
 * different physical address; accepting the IOVA as a physical pointer is
 * incorrect. Valid only for an RTKit instance created with sram=true. */
bool rtkit_set_sram_window(rtkit_dev_t *rtk, u64 iova_base, u64 phys_base,
                          size_t size);
/*
 * Serve AP-allocated buffer grants from a caller-owned, 16 KiB-aligned
 * physical region instead of the m1n1 heap. Required whenever the IOP keeps
 * running into the next OS: the pool region must be reserved out of that
 * OS's memory map, and exhaustion fails the request rather than falling
 * back to heap memory the IOP would scribble over post-boot.
 */
bool rtkit_set_buffer_pool(rtkit_dev_t *rtk, u64 base, size_t size);
/*
 * Declare AP power ON as soon as the system endpoints are started, instead of
 * waiting for the IOP to announce itself first. Required by IOPs that gate
 * their own ON transition on the AP's, where the default ordering deadlocks;
 * harmless for IOPs that announce ON unprompted, which simply ack earlier.
 */
bool rtkit_set_early_ap_power(rtkit_dev_t *rtk, bool enable);
bool rtkit_quiesce(rtkit_dev_t *rtk);
bool rtkit_sleep(rtkit_dev_t *rtk);
void rtkit_free(rtkit_dev_t *rtk);

bool rtkit_start_ep(rtkit_dev_t *rtk, u8 ep);
bool rtkit_boot(rtkit_dev_t *rtk);
/*
 * Bounded boot variant. In addition to timing out while waiting for IOP=ON,
 * this waits for the AP=ON acknowledgement before returning.
 */
bool rtkit_boot_timed(rtkit_dev_t *rtk, u32 timeout_usec);

bool rtkit_can_recv(rtkit_dev_t *rtk);

int rtkit_recv(rtkit_dev_t *rtk, struct rtkit_message *msg);
/*
 * Bounded, silent, never-blocking variant of rtkit_recv() for servicing an
 * IOP's mailbox while a guest owns the machine.  Consumes at most max_msgs
 * messages (rtkit_recv() drains the whole mailbox per call); requires a free
 * A2I slot before each consume so the one reply a message can need is sent
 * without asc_send() entering its 200 ms poll; prints nothing on any path;
 * latches an IOP crash without walking the crashlog.  App-endpoint messages
 * are counted and dropped.  Returns messages consumed, or negative once the
 * IOP has crashed (stop servicing).
 */
int rtkit_service_quiet(rtkit_dev_t *rtk, int max_msgs);
bool rtkit_send(rtkit_dev_t *rtk, const struct rtkit_message *msg);

bool rtkit_map(rtkit_dev_t *rtk, void *phys, size_t sz, u64 *dva);
bool rtkit_unmap(rtkit_dev_t *rtk, u64 dva, size_t sz);

bool rtkit_alloc_buffer(rtkit_dev_t *rtk, struct rtkit_buffer *bfr, size_t sz);
bool rtkit_free_buffer(rtkit_dev_t *rtk, struct rtkit_buffer *bfr);

#endif
