/* SPDX-License-Identifier: MIT */

#include "build_cfg.h"
#include "build_tag.h"

#include "../config.h"

#include "adt.h"
#include "aic.h"
#include "cpufreq.h"
#include "display.h"
#include "exception.h"
#include "fb.h"
#include "firmware.h"
#include "gxf.h"
#include "heapblock.h"
#include "mcc.h"
#include "memory.h"
#include "nvme.h"
#include "payload.h"
#include "pcie.h"
#include "pmgr.h"
#include "sep.h"
#include "smp.h"
#include "string.h"
#include "tps6598x.h"
#include "uart.h"
#include "uartproxy.h"
#include "usb.h"
#include "utils.h"
#include "wdt.h"
#include "xnuboot.h"

struct vector_args next_stage;

const char version_tag[] = "##m1n1_ver##" BUILD_TAG;
const char *const m1n1_version = version_tag + 12;

u32 board_id = ~0, chip_id = ~0;

void get_device_info(void)
{
    const char *model = (const char *)adt_getprop(adt, 0, "model", NULL);
    const char *target = (const char *)adt_getprop(adt, 0, "target-type", NULL);

    printf("Device info:\n");

    if (model)
        printf("  Model: %s\n", model);

    if (target)
        printf("  Target: %s\n", target);

    is_mac = !!strstr(model, "Mac");

    int chosen = adt_path_offset(adt, "/chosen");
    if (chosen > 0) {
        if (ADT_GETPROP(adt, chosen, "board-id", &board_id) < 0)
            printf("Failed to find board-id\n");
        if (ADT_GETPROP(adt, chosen, "chip-id", &chip_id) < 0)
            printf("Failed to find chip-id\n");

        printf("  Board-ID: 0x%x\n", board_id);
        printf("  Chip-ID: 0x%x\n", chip_id);
    } else {
        printf("No chosen node!\n");
    }

    printf("\n");
}

#ifdef DUMP_T8142_PMGR
//
// T8142 replaced the pmgr `ps-regs` property (M4 and earlier: 16 entries of
// {reg_idx, offset, mask}) with `ps-groups`, so pmgr_init() bails and nothing
// gets powered -- which is why the USB DART faults.
//
// Working the new format out normally needs the interactive proxy, but the proxy
// needs USB, which needs pmgr. This dumps the raw ADT data to the framebuffer so
// the format can be worked out offline from a photograph instead.
//
// Reads only ADT properties and the pmgr node's own reg windows. It does not
// touch any unpowered device, so it cannot repeat the DART fault.
//
static void dump_t8142_pmgr(void)
{
    int path[8];
    int node = adt_path_offset_trace(adt, "/arm-io/pmgr", path);
    if (node < 0) {
        printf("PMGRDUMP: no /arm-io/pmgr\n");
        return;
    }

    printf("\n=== PMGR DUMP (T8142) ===\n");

    // pmgr's own reg windows -- these are what ps-regs/ps-groups index into.
    for (int i = 0; i < 8; i++) {
        u64 base, size;
        if (adt_get_reg(adt, path, "reg", i, &base, &size) < 0)
            break;
        printf("reg[%d] 0x%09lx sz 0x%lx\n", i, base, size);
    }

    // The replacement property, raw. Only 36 bytes on J704.
    u32 len = 0;
    const u8 *p = adt_getprop(adt, node, "ps-groups", &len);
    if (p) {
        printf("ps-groups (%u bytes):\n ", len);
        for (u32 i = 0; i < len && i < 64; i++) {
            printf("%02x ", p[i]);
            if ((i % 16) == 15)
                printf("\n ");
        }
        printf("\n");
    } else {
        printf("ps-groups: ABSENT\n");
    }

    static const char *const other_props[] = {"ps-regs", "pwrgate-regs", "perf-regs",
                                              "ps-groups", "perf-groups"};
    for (u32 i = 0; i < sizeof(other_props) / sizeof(other_props[0]); i++) {
        u32 l = 0;
        printf("%-14s %s\n", other_props[i],
               adt_getprop(adt, node, other_props[i], &l) ? "present" : "ABSENT");
    }

    //
    // Device entries. struct pmgr_device is private to pmgr.c, so index the raw
    // 48-byte records directly:
    //   +0x00 flags  +0x03 id1  +0x0a addr_offset  +0x0b psreg_idx
    //   +0x1a id2    +0x20 name[16]
    //
    const u8 *devs = adt_getprop(adt, node, "devices", &len);
    if (devs) {
        u32 stride = 48;
        u32 count = len / stride;
        printf("devices: %u bytes / %u = %u entries\n", len, stride, count);
        for (u32 i = 0; i < count; i++) {
            const u8 *d = devs + i * stride;
            const char *nm = (const char *)(d + 0x20);
            // Only the devices USB bringup powers, to fit on screen.
            if (strncmp(nm, "ATC0", 4) && strncmp(nm, "DART_USB0", 9) &&
                strncmp(nm, "USB_DRD0", 8))
                continue;
            printf("  %-14s ps_idx:%02x addr_off:%02x flags:%02x id1:%02x\n", nm, d[0x0b],
                   d[0x0a], d[0x00], d[0x03]);
        }
    }
    printf("=========================\n");
}
#endif

void run_actions(void)
{
    bool usb_up = false;

#ifndef BRINGUP
#ifdef EARLY_PROXY_TIMEOUT
    int node = adt_path_offset(adt, "/chosen/asmb");
    u64 lp_sip0 = 0;

    if (node >= 0) {
        ADT_GETPROP(adt, node, "lp-sip0", &lp_sip0);
        printf("Boot policy: sip0 = %ld\n", lp_sip0);
    }

    if (!cur_boot_args.video.display && lp_sip0 == 127) {
        printf("Bringing up USB for early debug...\n");

        usb_init();
        usb_iodev_init();

        usb_up = true;

        printf("Waiting for proxy connection... ");
        for (int i = 0; i < EARLY_PROXY_TIMEOUT * 100; i++) {
            for (int j = 0; j < USB_IODEV_COUNT; j++) {
                iodev_id_t iodev = IODEV_USB0 + j;

                if (!(iodev_get_usage(iodev) & USAGE_UARTPROXY))
                    continue;

                usb_iodev_vuart_setup(iodev);
                iodev_handle_events(iodev);
                if (iodev_can_write(iodev) || iodev_can_write(IODEV_USB_VUART)) {
                    printf(" Connected!\n");
                    uartproxy_run(NULL);
                    return;
                }
            }

            mdelay(10);
            if (i % 100 == 99)
                printf(".");
        }
        printf(" Timed out\n");
    }
#endif
#endif

    printf("Checking for payloads...\n");

    if (payload_run() == 0) {
        printf("Valid payload found\n");
        return;
    }
    fb_set_active(true);

    printf("No valid payload found\n");

#if !defined(BRINGUP) && !defined(SKIP_USB_BRINGUP)
    if (!usb_up) {
        usb_init();
        usb_iodev_init();
    }
#elif defined(SKIP_USB_BRINGUP)
    printf("USB bringup skipped (SKIP_USB_BRINGUP) - no proxy console.\n");
    //
    // init_cpu() prints the MIDR part very early, before the boot-args dump, so
    // on a 57-row framebuffer console it has already scrolled away by the time
    // anything can be photographed. Reprint it here, at the bottom of the log.
    //
    // This is the one value we cannot obtain any other way: MIDR_EL1 is not
    // readable from macOS userspace, and it is what gates the midr.h /
    // chickens.c entries for T8142.
    //
    u64 midr = mrs(MIDR_EL1);
    printf("\n=== T8142 CPU IDENTIFICATION ===\n");
    printf("MIDR_EL1   : 0x%016lx\n", midr);
    printf("  implementer 0x%02lx  part 0x%03lx  variant 0x%lx  revision 0x%lx\n",
           (midr >> 24) & 0xff, (midr >> 4) & 0xfff, (midr >> 20) & 0xf, midr & 0xf);
    printf("MPIDR_EL1  : 0x%016lx\n", mrs(MPIDR_EL1));
    printf("================================\n");
    //
    // Deliberately fall through to uartproxy_run() below rather than halting.
    //
    // An earlier version spun here forever, purely so the boot log stayed on the
    // framebuffer long enough to photograph. That is no longer the only way to
    // read it: with serial up (macvdmtool from the M4), falling through gives a
    // working proxy console over UART even though USB is skipped -- which is what
    // makes the pmgr ps-groups reverse engineering possible at all.
    //
#endif

#ifdef DUMP_T8142_PMGR
    dump_t8142_pmgr();
#endif

    printf("Running proxy...\n");

    uartproxy_run(NULL);
}

void m1n1_main(void)
{
    printf("\n\nm1n1 %s\n", m1n1_version);
    //
    // The git tag stays "b791225-dirty" across every local build, and successive
    // builds come out byte-identical in size, so there was no way to tell from a
    // boot log which image was actually installed. Bump this by hand whenever a
    // build is handed over for testing.
    //
    printf("T8142 work-in-progress build: M5-DEBUG-21\n");
    printf("Copyright The Asahi Linux Contributors\n");
    printf("Licensed under the MIT license\n\n");

    printf("Running in EL%lu\n\n", mrs(CurrentEL) >> 2);

    firmware_init();

    heapblock_init();

#ifndef BRINGUP
    if (supports_gxf())
        gxf_init();
    mcc_init();
    mmu_init();
#if defined(USE_FB) && defined(EARLY_FB_CONSOLE)
    /*
     * Bringup aid: get the console onto the screen before any SoC-specific init
     * runs, so that a hang in aic_init(), pmgr_init() or display_init() on
     * silicon we do not know yet leaves a readable log rather than a blank
     * panel.
     *
     * fb_init() only needs malloc() (heapblock_init, above) and
     * mmu_add_mapping() (mmu_init, above); chip_id and is_mac are set by
     * get_device_info() in startup.c long before this point. It adopts the
     * framebuffer iBoot already configured (cur_boot_args.video.base), so the
     * DCP does not have to be up. The 8K console ring buffer is replayed on
     * activation, so the banner and device info printed earlier are not lost.
     *
     * Caveat: this runs before display_init(), reversing the usual order. If the
     * DCP relocates the framebuffer, the console can go stale from that point
     * on -- but by then we have the early log, which is the entire purpose.
     */
    fb_init(!is_mac);
    fb_set_active(true);
#endif
    aic_init();
#endif
    wdt_disable();
#ifndef BRINGUP
    pmgr_init();
#ifdef USE_DEBUG_USB
    tps6598x_enable_debugusb();
#endif
#ifdef USE_FB
    display_init();
    // Kick DCP to sleep, so dodgy monitors which cause reconnect cycles don't cause us to lose the
    // framebuffer.
    display_shutdown(DCP_SLEEP_IF_EXTERNAL);
#ifndef EARLY_FB_CONSOLE
    // On idevice we need to always clear, because otherwise it looks scuffed on white devices
    fb_init(!is_mac);
    fb_display_logo();
#ifdef FB_SILENT_MODE
    fb_set_active(!cur_boot_args.video.display);
#else
    fb_set_active(true);
#endif
#else
    /*
     * Already initialised and activated above. Deliberately skip the logo and the
     * FB_SILENT_MODE handling here: both would erase or hide the early boot log
     * that is the whole point of EARLY_FB_CONSOLE.
     */
#endif
#endif

    cpufreq_fixup();
    sep_init();
#endif

    printf("Initialization complete.\n");

    run_actions();

    if (!next_stage.entry) {
        panic("Nothing to do!\n");
    }

    printf("Preparing to run next stage at %p...\n", next_stage.entry);

    nvme_shutdown();
    exception_shutdown();
#ifndef BRINGUP
    usb_iodev_shutdown();
    display_shutdown(DCP_SLEEP_IF_EXTERNAL);
#ifdef USE_FB
    fb_shutdown(next_stage.restore_logo);
#endif
    mmu_shutdown();
#endif

    printf("Vectoring to next stage...\n");

    next_stage.entry(next_stage.args[0], next_stage.args[1], next_stage.args[2], next_stage.args[3],
                     next_stage.args[4]);

    panic("Next stage returned!\n");
}
