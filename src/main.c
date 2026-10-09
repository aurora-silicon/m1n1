/* SPDX-License-Identifier: MIT */

#include "../build/build_cfg.h"
#include "../build/build_tag.h"

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
#include "kboot.h"
#include "mcc.h"
#include "memory.h"
#include "nvme.h"
#include "payload.h"
#include "pcie.h"
#include "pmgr.h"
#include "sep.h"
#include "smp.h"
#include "soc.h"
#include "string.h"
#include "tps6598x.h"
#include "uart.h"
#include "uartproxy.h"
#include "usb.h"
#include "utils.h"
#include "wdt.h"
#include "stage1_config.h"
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

void run_actions(void)
{
    bool usb_up = false;

#ifdef J613_ESP_STAGE1
    const char *target = adt_getprop(adt, 0, "target-type", NULL);
    bool j613 = chip_id == T8122 && target && !strcmp(target, "J613") &&
                os_firmware.version == V26_6_2;
    if (!j613) {
        printf("J613 Stage 1: board/25G83 guard rejected; proxy only\n");
        goto proxy_fallback;
    }
    /* A factory or invalid configuration is a proxy-only image: no payload scan. */
    if (!stage1_config_target())
        goto proxy_fallback;
    /* A zero window skips USB entirely: straight to the ESP file, as a plain stage 1 does. */
    if (stage1_config_window_ms()) {
        u32 window_ms = stage1_config_window_ms();
        usb_init();
        usb_iodev_init();
        usb_up = true;
        u64 deadline = timeout_calculate((u64)window_ms * 1000);
        while (!timeout_expired(deadline)) {
            for (int j = 0; j < USB_IODEV_COUNT; j++) {
                iodev_id_t iodev = IODEV_USB0 + j;
                if (!(iodev_get_usage(iodev) & USAGE_UARTPROXY))
                    continue;
                usb_iodev_vuart_setup(iodev);
                iodev_handle_events(iodev);
                if (iodev_can_write(iodev) || iodev_can_write(IODEV_USB_VUART)) {
                    printf("J613 Stage 1: host grabbed proxy window\n");
                    uartproxy_run(NULL);
                    return;
                }
            }
            mdelay(10);
        }
        printf("J613 Stage 1: window expired; loading ESP candidate\n");
    } else {
        printf("J613 Stage 1: no USB window; loading ESP candidate\n");
    }
#endif

#ifndef BRINGUP
#if defined(EARLY_PROXY_TIMEOUT) && !defined(J613_ESP_STAGE1)
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
#ifdef J613_ESP_STAGE1
proxy_fallback:
#endif
    fb_set_active(true);

    printf("No valid payload found\n");
#ifdef J613_ESP_STAGE1
    /* Shown on the panel: this is where a user without a USB host ends up. */
    printf("\nJ613 Stage 1: could not start Linux from the EFI partition.\n"
           "  Needs: MacBook Air 13\" M3 (J613), this volume's macOS exactly 26.6.2,\n"
           "  and the stage-2 file named in this stage 1 (fill_stage1_config.py --check).\n"
           "  Hold the power button to shut down, then hold it again to choose another system.\n");
#endif

#ifndef BRINGUP
    if (!usb_up) {
        usb_init();
        usb_iodev_init();
    }
#endif

    printf("Running proxy...\n");

    uartproxy_run(NULL);
}

/* J613 bring-up: read-only DCP ASC CPU state, to see whether the DCP runs when stage 2 starts. */
u32 j613_dcp_cpu_snapshots[10] = {~0u, ~0u, ~0u, ~0u, ~0u, ~0u, ~0u, ~0u, ~0u, ~0u};

static void log_dcp_cpu(const char *when, unsigned index)
{
    if (chip_id != T8122)
        return;
    int before = exc_count;
    exc_guard = GUARD_SKIP | GUARD_SILENT;
    u32 control = read32(0x28ec00044);
    u32 status = read32(0x28ec00048);
    sysop("dsb sy");
    sysop("isb");
    exc_guard = GUARD_OFF;
    if (exc_count != before)
        printf("DCP: CPU state unreadable (%s)\n", when);
    else {
        j613_dcp_cpu_snapshots[2 * index] = control;
        j613_dcp_cpu_snapshots[2 * index + 1] = status;
        printf("DCP: CPU control 0x%x status 0x%x (%s)\n", control, status, when);
    }
}

void m1n1_main(void)
{
    printf("\n\nm1n1 %s\n", m1n1_version);
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
    aic_init();
#endif
    wdt_disable();
#ifndef BRINGUP
    log_dcp_cpu("before pmgr init", 0);
    pmgr_init();
#ifdef USE_DEBUG_USB
    tps6598x_enable_debugusb();
#endif
    log_dcp_cpu("stage 2 start", 1);
#ifdef USE_FB
    display_init();
    // Kick DCP to sleep, so dodgy monitors which cause reconnect cycles don't cause us to lose the
    // framebuffer.
    display_shutdown(DCP_SLEEP_IF_EXTERNAL);
    log_dcp_cpu("after display init", 2);
    // On idevice we need to always clear, because otherwise it looks scuffed on white devices
    fb_init(!is_mac);
    fb_display_logo();
#ifdef FB_SILENT_MODE
    fb_set_active(!cur_boot_args.video.display);
#else
    fb_set_active(true);
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

    log_dcp_cpu("after boot preparation", 3);

    nvme_shutdown();
    exception_shutdown();
#ifndef BRINGUP
    usb_iodev_shutdown();
    display_shutdown(DCP_SLEEP_IF_EXTERNAL);
    log_dcp_cpu("after final display shutdown", 4);
    if (kboot_update_j613_dcp_snapshots())
        printf("DCP: final CPU snapshot publication failed\n");
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
