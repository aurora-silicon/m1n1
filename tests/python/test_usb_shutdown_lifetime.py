"""Fault-inject the production shutdown and final native-entry functions."""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def function(path, signature):
    source = (ROOT / path).read_text()
    start = source.index(signature)
    opening = source.index("{", start)
    # Native code uses column-zero function ends; retain both #ifdef branches.
    end = source.index("\n}", opening) + 2
    return source[start:end] + "\n"


def compile_run(tmp_path, harness, flags=(), cases=(0,)):
    binary = tmp_path / "fixture"
    subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-parameter", *flags, "-x", "c", "-", "-o", str(binary)],
                   input=harness, text=True, check=True)
    for case in cases:
        subprocess.run([str(binary), str(case)], check=True)


@pytest.mark.parametrize("j700", [False, True])
def test_dwc3_preserves_dma_and_never_retries_after_uncertainty(tmp_path, j700):
    # Register/IOVA values in this harness are isolated mock tokens, never MMIO.
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
#define MAX_ENDPOINTS 16
#define CDC_ACM_PIPE_MAX 2
#define DWC3_DEPCMD_ENDTRANSFER 1
#define DWC3_DEPCMD_CMDIOC 2
#define DWC3_DEPCMD_HIPRI_FORCERM 0x800
#define DWC3_DEPCMD_PARAM(x) ((x) << 8)
#define DWC3_DEVTEN 10
#define DWC3_DALEPENA 20
#define DWC3_DCTL 30
#define DWC3_DSTS 40
#define DWC3_DSTS_DEVCTRLHLT 1
#define DWC3_DCTL_RUN_STOP 2
#define DWC3_DCTL_CSFTRST 4
#define TRB_BUFFER_IOVA 100
#define XFER_BUFFER_IOVA 200
#define SCRATCHPAD_IOVA 300
#define EVENT_BUFFER_IOVA 400
#define TRB_BUFFER_SIZE 16
#define XFER_BUFFER_SIZE 16
#define DWC3_SCRATCHPAD_SIZE 16
#define DWC3_EVENT_BUFFERS_SIZE 16
#define SZ_16K 16384
#define max(a, b) ((a) > (b) ? (a) : (b))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define usb_debug_printf(...) ((void)0)
typedef struct {
    u64 regs;
    void *dart, *evtbuffer, *scratchpad, *xferbuffer, *trbs;
    bool failed, dma_unsafe, shutting_down;
    struct { bool xfer_in_progress, end_cmd_pending; u8 resource_index; } endpoints[MAX_ENDPOINTS];
    struct { bool ready; void *device2host, *host2device; } pipe[CDC_ACM_PIPE_MAX];
} dwc3_dev_t;
static bool usb_shutdown_failed, forbidden, dart_failed;
static int fault, writes, commands, polls, unmaps, frees, rings, dart_frees, ticks;
static int trace[128], count;
static void record(int op) { assert(!forbidden); trace[count++] = op; }
static void write32(u64 addr, u32 val) { record(1000 + addr); writes++; }
static void clear32(u64 addr, u32 val) { record(1000 + addr); writes++; }
static void set32(u64 addr, u32 val) { record(2000 + addr); writes++; }
static int poll32(u64 addr, u32 mask, u32 val, unsigned timeout) {
    record(3000 + addr); polls++;
    if ((fault == 4 && addr == DWC3_DSTS) || (fault == 5 && addr == DWC3_DCTL)) {
        forbidden = true;
        return -1;
    }
    return 0;
}
static int usb_dwc3_ep_command(dwc3_dev_t *dev, u8 ep, u32 c, u32 a, u32 b, u32 d) {
    record(10 + ep); commands++;
    if (fault == 3 && ep == 2) { forbidden = true; return -1; }
    return 0;
}
static void dart_unmap(void *d, u64 iova, size_t len) {
    record(4000 + iova); unmaps++;
    if (fault >= 11 && fault <= 14 && unmaps == fault - 10) {
        dart_failed = true; forbidden = true;
    }
}
static bool dart_has_failed(void *d) { return dart_failed; }
static bool dart_shutdown_checked(void *d) {
    record(5000);
    if (fault == 15) { dart_failed = true; forbidden = true; return false; }
    dart_frees++; return true;
}
static void ringbuffer_free(void *p) { record(6000); rings++; }
static void mock_free(void *p) { record(7000); frees++; }
#define free mock_free
'''
    harness += r'''
static void usb_dwc3_fail_session(dwc3_dev_t *dev);
static u64 timeout_calculate(u32 us) { return 3; }
static bool timeout_expired(u64 deadline) {
    if (++ticks > 3) { forbidden = true; return true; }
    return false;
}
static void usb_dwc3_handle_events(dwc3_dev_t *dev) {
    record(8000);
    if (fault == 7) {
#ifdef J700_CDC_PROXY
        usb_dwc3_fail_session(dev);
#else
        dev->failed = true;
#endif
        forbidden = true;
    } else if (fault != 8) {
        for (int i = 0; i < MAX_ENDPOINTS; i++)
            dev->endpoints[i].end_cmd_pending = false;
    }
}
'''
    harness += function("src/usb_dwc3.c", "static int usb_dwc3_end_transfer(")
    if j700:
        harness += function("src/usb_dwc3.c", "static void usb_dwc3_fail_session(dwc3_dev_t *dev)\n{")
    else:
        harness = harness.replace("static void usb_dwc3_fail_session(dwc3_dev_t *dev);", "")
    harness += "static dwc3_dev_t *usb_retained_dev;\n"
    harness += function("src/usb_dwc3.c", "static void usb_dwc3_mark_unsafe(")
    harness += function("src/usb_dwc3.c", "bool usb_dwc3_shutdown_failed(")
    harness += function("src/usb_dwc3.c", "bool usb_dwc3_shutdown(")
    harness += r'''
int main(int argc, char **argv) {
    assert(argc == 2);
    fault = atoi(argv[1]);
    dwc3_dev_t dev = {0};
    dev.dart = &dev;
    dev.endpoints[2].xfer_in_progress = true;
    dev.endpoints[4].xfer_in_progress = true;
    dev.pipe[0].ready = dev.pipe[1].ready = true;
    dev.dma_unsafe = fault == 1;
    dev.failed = fault == 2;
    usb_shutdown_failed = fault == 6;
    if (fault == 9) { assert(usb_dwc3_shutdown(NULL)); assert(count == 0); return 0; }
#ifdef J700_CDC_PROXY
    if (fault == 10) {
        dev.dma_unsafe = true;
        usb_dwc3_fail_session(&dev);
        assert(usb_dwc3_shutdown_failed() && dev.failed && count == 0);
        usb_dwc3_fail_session(&dev);
        assert(count == 0);
        return 0;
    }
#endif
    assert(usb_dwc3_shutdown(&dev) == (fault == 0));
    assert(!dev.pipe[0].ready && !dev.pipe[1].ready);
    if (!fault) {
        assert(unmaps == 4 && frees == 5 && rings == 4 && dart_frees == 1);
        assert(commands == 2 && polls == 2 && writes == 4);
        int halt = -1, reset = -1, first_unmap = -1;
        for (int i = 0; i < count; i++) {
            if (trace[i] == 3000 + DWC3_DSTS) halt = i;
            if (trace[i] == 3000 + DWC3_DCTL) reset = i;
            if (trace[i] == 4000 + TRB_BUFFER_IOVA) first_unmap = i;
        }
        assert(halt >= 0 && reset > halt && first_unmap > reset);
    } else {
        assert(usb_dwc3_shutdown_failed() && dev.failed && dev.dma_unsafe);
        assert(!frees && !rings && !dart_frees);
        if (fault < 11) assert(!unmaps);
        else assert(unmaps == (fault == 15 ? 4 : fault - 10));
        int old_count = count;
        assert(!usb_dwc3_shutdown(&dev));
        assert(count == old_count);
        if (fault == 1 || fault == 2 || fault == 6) assert(count == 0);
        if (fault == 3 || fault == 7 || fault == 8) assert(!writes && !polls);
        if (fault == 4) assert(writes == 3 && polls == 1);
        if (fault == 5) assert(writes == 4 && polls == 2);
    }
    return 0;
}
'''
    cases = (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15) + ((10,) if j700 else ())
    compile_run(tmp_path, harness, ["-DJ700_CDC_PROXY"] if j700 else (), cases)


def test_iodev_failure_keeps_owner_and_stops_before_other_controllers(tmp_path):
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#define FIRST_USB_IODEV 0
#define USB_IODEV_COUNT 3
#define IODEV_USB0 0
struct iodev { void *opaque; unsigned usage; };
static struct iodev devs[3], iodev_usb_vuart;
static struct iodev *slots[3];
static bool failed;
static int fault, shuts, frees, unregisters;
static bool usb_dwc3_shutdown_failed(void) { return failed; }
static struct iodev *iodev_unregister_device(int n) {
    struct iodev *p = slots[n]; slots[n] = NULL; unregisters++; return p;
}
static void iodev_register_device(int n, struct iodev *p) { slots[n] = p; }
static bool usb_dwc3_shutdown(void *opaque) {
    assert(!failed);
    int n = (struct iodev *)opaque - devs;
    assert(iodev_usb_vuart.opaque != opaque);
    shuts++;
    failed = n == fault;
    return !failed;
}
static void mock_free(void *p) { assert(!failed); frees++; }
#define free mock_free
'''
    harness += function("src/usb.c", "bool usb_iodev_shutdown(")
    harness += r'''
int main(int argc, char **argv) {
    assert(argc == 2); fault = atoi(argv[1]);
    for (int i = 0; i < 3; i++) {
        devs[i].opaque = &devs[i]; devs[i].usage = 3; slots[i] = &devs[i];
    }
    iodev_usb_vuart.opaque = &devs[fault < 0 ? 0 : fault];
    iodev_usb_vuart.usage = 1;
    assert(usb_iodev_shutdown() == (fault < 0));
    assert(!iodev_usb_vuart.opaque && !iodev_usb_vuart.usage);
    if (fault < 0) {
        assert(shuts == 3 && frees == 3);
        for (int i = 0; i < 3; i++) assert(!slots[i]);
    } else {
        assert(shuts == fault + 1 && frees == fault);
        assert(slots[fault] == &devs[fault] && devs[fault].usage == 0);
        assert(devs[fault].opaque == &devs[fault]);
        for (int i = fault + 1; i < 3; i++) assert(slots[i] == &devs[i]);
        int before = unregisters;
        assert(!usb_iodev_shutdown());
        assert(unregisters == before && shuts == fault + 1 && frees == fault);
    }
    return 0;
}
'''
    compile_run(tmp_path, harness, cases=(-1, 0, 1, 2))


def test_iodev_allocation_failure_cannot_hide_an_active_dma_owner(tmp_path):
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <stddef.h>
#define FIRST_USB_IODEV 0
#define USB_IODEV_COUNT 3
#define IODEV_USB0 0
#define SPINLOCK_ALIGN 64
#define USAGE_CONSOLE 1
#define USAGE_UARTPROXY 2
typedef int dwc3_dev_t;
struct iodev { const void *ops; void *opaque; unsigned usage; int lock; };
static bool failed;
static int fault, allocs, brings, shuts, registered, iodev_usb_ops;
static struct iodev wrappers[3];
static int controllers[3];
static bool usb_dwc3_shutdown_failed(void) { return failed; }
static dwc3_dev_t *usb_iodev_bringup(int n) {
    assert(!failed); brings++;
    if (fault == 2) { failed = true; return NULL; }
    return &controllers[n];
}
static void *memalign(size_t alignment, size_t size) { allocs++; return NULL; }
static bool usb_dwc3_shutdown(dwc3_dev_t *p) {
    assert(!failed); shuts++; failed = fault == 1; return !failed;
}
static void spin_init(int *lock) { assert(0); }
static void iodev_register_device(int n, struct iodev *p) { registered++; }
'''
    harness += function("src/usb.c", "void usb_iodev_init(")
    harness += r'''
int main(int argc, char **argv) {
    assert(argc == 2); fault = atoi(argv[1]);
    (void)wrappers;
    usb_iodev_init();
    assert(!registered);
    if (fault == 0) assert(brings == 3 && allocs == 3 && shuts == 3);
    if (fault == 1) assert(brings == 1 && allocs == 1 && shuts == 1 && failed);
    if (fault == 2) assert(brings == 1 && !allocs && !shuts && failed);
    int before = brings;
    if (failed) { usb_iodev_init(); assert(brings == before); }
    return 0;
}
'''
    compile_run(tmp_path, harness, cases=(0, 1, 2))


@pytest.mark.parametrize("bringup", [False, True])
def test_actual_native_branch_never_vectors_or_reboots_after_usb_failure(tmp_path, bringup):
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <setjmp.h>
#include <string.h>
#define T8152 0x8152
#define DCP_SLEEP_IF_EXTERNAL 1
#define USE_FB
static int chip_id = 0x6040, fault;
static int nvme, exceptions, usb, display, fb, mmu, vectors, parks, reboots;
static char order[32];
static unsigned ordered;
static void record(char step) { order[ordered++] = step; }
static jmp_buf finished;
static struct { void (*entry)(uint64_t, uint64_t, uint64_t, uint64_t, uint64_t);
                uint64_t args[5]; bool restore_logo; } next_stage;
static bool usb_dwc3_shutdown_failed(void) { return fault == 2; }
static bool nvme_shutdown(void) { record('N'); nvme++; return true; }
static void uartproxy_run(void *p) { assert(0); }
static void exception_shutdown(void) { record('E'); exceptions++; }
#ifndef BRINGUP
static bool usb_iodev_shutdown(void) { record('U'); usb++; return fault != 1; }
static void display_shutdown(int x) { record('D'); display++; }
static void fb_shutdown(bool x) { record('F'); fb++; }
static void mmu_shutdown(void) { record('M'); mmu++; }
#endif
static void panic_mock(void) { reboots++; longjmp(finished, 1); }
#define panic(...) panic_mock()
static void park(void) { record('P'); parks++; longjmp(finished, 1); }
#define sysop(x) park()
static void vector(uint64_t a, uint64_t b, uint64_t c, uint64_t d, uint64_t e) {
    record('V'); vectors++; longjmp(finished, 1);
}
'''
    harness += function("src/main.c", "static bool prepare_next_stage(")
    source = (ROOT / "src/main.c").read_text()
    start = source.index("    if (!prepare_next_stage())")
    harness += "static void enter(void)\n{\n" + source[start:source.index('\n}', start)] + "\n}\n"
    harness += r'''
int main(int argc, char **argv) {
    assert(argc == 2); fault = atoi(argv[1]); next_stage.entry = vector;
    if (!setjmp(finished)) enter();
    assert(!reboots);
    if (fault == 2) {
        assert(!strcmp(order, "P"));
        assert(parks == 1 && !next_stage.entry);
        assert(!nvme && !exceptions && !usb && !display && !fb && !mmu && !vectors);
    } else if (fault == 1) {
        assert(!strcmp(order, "NEUP"));
        assert(nvme == 1 && exceptions == 1 && usb == 1);
        assert(parks == 1 && !next_stage.entry && !display && !fb && !mmu && !vectors);
    } else {
        assert(vectors == 1 && !parks && nvme == 1 && exceptions == 1);
#ifdef BRINGUP
        assert(!strcmp(order, "NEV"));
        assert(!usb && !display && !fb && !mmu);
#else
        assert(!strcmp(order, "NEUDFMV"));
        assert(usb == 1 && display == 1 && fb == 1 && mmu == 1);
#endif
    }
    return 0;
}
'''
    compile_run(tmp_path, harness, ["-DBRINGUP"] if bringup else (),
                cases=(0, 2) if bringup else (0, 1, 2))


def test_proxy_native_schedulers_propagate_failed_handoff_status(tmp_path):
    source = (ROOT / "src/proxy.c").read_text()
    vector_start = source.index("        case P_VECTOR:")
    vector = source[vector_start:source.index("        case P_GL1_CALL:", vector_start)]
    boot_start = source.index("        case P_KBOOT_BOOT:")
    boot = source[boot_start:source.index("        case P_KBOOT_SET_CHOSEN:", boot_start)]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
typedef uint64_t u64;
typedef void generic_func(void);
enum { P_VECTOR, P_KBOOT_BOOT };
static bool failed;
static int hpm, flushes, boot_result;
static struct { generic_func *entry; u64 args[5]; bool restore_logo; } next_stage;
struct request { int opcode; u64 args[6]; };
struct reply { u64 retval; };
static bool usb_dwc3_shutdown_failed(void) { return failed; }
static void usb_hpm_restore_irqs(int x) { assert(!failed); hpm++; }
static void iodev_console_flush(void) { assert(!failed); flushes++; }
static int kboot_boot(void *p) { return boot_result; }
static int dispatch(struct request *request, struct reply *reply) {
    switch (request->opcode) {
'''
    harness += vector + boot + r'''
    }
    return 0;
}
int main(void) {
    struct request request = {P_VECTOR, {0x1234, 11, 12, 13, 14, 15}};
    struct reply reply = {0};
    next_stage.entry = (void *)1; failed = true;
    assert(dispatch(&request, &reply) == 0);
    assert(reply.retval == (u64)-1 && !next_stage.entry && !hpm && !flushes);
    failed = false; reply.retval = 0;
    assert(dispatch(&request, &reply) == 1);
    assert((uintptr_t)next_stage.entry == 0x1234 && hpm == 1 && flushes == 1);
    assert(next_stage.restore_logo);
    for (int i = 0; i < 5; i++) assert(next_stage.args[i] == (u64)(11 + i));
    request.opcode = P_KBOOT_BOOT; boot_result = -1;
    assert(dispatch(&request, &reply) == 0 && reply.retval == (u64)-1);
    boot_result = 0;
    assert(dispatch(&request, &reply) == 1 && reply.retval == 0);
    return 0;
}
'''
    compile_run(tmp_path, harness)


def test_shutdown_event_drain_never_rearms_traffic_or_reannounces(tmp_path):
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define DWC3_DEPEVT_EPCMDCMPLT 7
#define DWC3_DEPEVT_XFERCOMPLETE 1
#define DEPEVT_STATUS_BUSERR 1
#define MAX_ENDPOINTS 16
typedef uint8_t u8;
#define DWC3_EVENT_TYPE_DEV 0
static void ignore_debug(const char *format, ...) {}
#define usb_debug_printf(...) ignore_debug(__VA_ARGS__)
typedef struct {
    bool failed, shutting_down;
    struct { bool xfer_in_progress, end_cmd_pending; unsigned resource_index; } endpoints[MAX_ENDPOINTS];
    struct { bool pending; unsigned attempts; } recovery;
} dwc3_dev_t;
union dwc3_event {
    uint32_t raw;
    struct { unsigned is_devspec : 1; unsigned type : 7; unsigned rest : 24; } type;
    struct { unsigned one : 1; unsigned endpoint_number : 5; unsigned endpoint_event : 4;
             unsigned reserved : 2; unsigned status : 4; unsigned parameters : 16; } depevt;
    unsigned devt;
};
static unsigned ep_calls, dev_calls, recovery_calls;
static void usb_dwc3_handle_event_ep(dwc3_dev_t *d, typeof(((union dwc3_event *)0)->depevt) e) {
    ep_calls++;
}
static void usb_dwc3_handle_event_dev(dwc3_dev_t *d, unsigned e) { dev_calls++; }
static bool usb_cdc_recovery_due(void *r, unsigned t) { recovery_calls++; return false; }
static int usb_dwc3_reannounce(dwc3_dev_t *d) { assert(0); return 0; }
static void usb_cdc_recovery_attempted(void *r, unsigned t) { assert(0); }
static unsigned get_ticks(void) { return 0; }
static unsigned ticks_to_msecs(unsigned t) { return t; }
'''
    harness += function("src/usb_dwc3.c", "static void usb_dwc3_handle_event(dwc3_dev_t *dev,")
    harness += function("src/usb_dwc3.c", "static void usb_dwc3_maybe_reannounce(")
    harness += r'''
int main(void) {
    dwc3_dev_t dev = {.shutting_down = true};
    for (unsigned device = 0; device < 2; device++) {
        for (unsigned event = 0; event < 16; event++) {
            union dwc3_event e = {0};
            e.depevt.endpoint_event = event; e.type.is_devspec = device;
            usb_dwc3_handle_event(&dev, e);
        }
    }
    assert(ep_calls == 1 && dev_calls == 0);
    usb_dwc3_maybe_reannounce(&dev);
    assert(recovery_calls == 0);
    dev.shutting_down = false;
    usb_dwc3_maybe_reannounce(&dev);
    assert(recovery_calls == 1);
    return 0;
}
'''
    compile_run(tmp_path, harness)
