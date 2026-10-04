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
#include "mcc.h"
#include "memory.h"
#include "nvme.h"
#include "payload.h"
#include "pcie.h"
#include "pmgr.h"
#include "sep.h"
#include "smp.h"
#include "stage1_config.h"
#include "string.h"
#include "tps6598x.h"
#include "uart.h"
#include "uartproxy.h"
#include "usb.h"
#include "usb_cdc.h"
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

void run_actions(void)
{
    bool usb_up = false;

    u32 window_ms = chip_id == T8140 ? stage1_config_window_ms() : 0;
#ifdef T8140_PROXY_WINDOW_MS
    if (!stage1_config_target())
        window_ms = T8140_PROXY_WINDOW_MS;
#endif
    if (chip_id == T8140 && window_ms) {
        if (uartproxy_wait_dockchannel(window_ms)) {
            printf("Stage 1: host request received\n");
            fb_set_active(true);
            uartproxy_run_presynced(IODEV_DOCKCHANNEL_UART);
            while (!next_stage.entry)
                uartproxy_run(NULL);
            return;
        }
        printf("Stage 1: no host during proxy window\n");
    }

#ifndef BRINGUP
#ifdef EARLY_PROXY_TIMEOUT
    int node = adt_path_offset(adt, "/chosen/asmb");
    u64 lp_sip0 = 0;

    if (node >= 0) {
        ADT_GETPROP(adt, node, "lp-sip0", &lp_sip0);
        printf("Boot policy: sip0 = %ld\n", lp_sip0);
    }

    if (chip_id != T8140 && !cur_boot_args.video.display && lp_sip0 == 127) {
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

#ifndef J700_CDC_PROXY
    if (payload_run() == 0) {
        printf("Valid payload found\n");
        return;
    }
#endif
    fb_set_active(true);

    printf("No valid payload found\n");

#ifndef BRINGUP
    if (!usb_up && chip_id != T8140) {
        usb_init();
        usb_iodev_init();
    }
#endif

    printf("Running proxy...\n");

#ifdef J700_CDC_AUTOSTART
    if (usb_cdc_schedule(1000, 0, BIT(1) | BIT(2)))
        panic("CDC autostart scheduling failed\n");
#endif

    uartproxy_run(NULL);
}

static void j873g_run_proxy(void)
{
    u32 model_len = 0, target_len = 0;
    const char *model = adt_getprop(adt, 0, "model", &model_len);
    const char *target = adt_getprop(adt, 0, "target-type", &target_len);

    if (board_id != 0x24 || !model || model_len != sizeof("Mac18,5") ||
        memcmp(model, "Mac18,5", sizeof("Mac18,5")) || !target ||
        target_len != sizeof("J873g") || memcmp(target, "J873g", sizeof("J873g")))
        panic("Unsupported T8152 board\n");

    /* Adopt firmware power and USB state. Only the qualified display route
     * may be initialized here; secondary CPUs remain parked for the proxy. */
    printf("J873g: KIS bring-up with native SMP\n");
    mmu_init();
    wdt_disable();
    if (smp_init() < 0)
        panic("Unsupported T8152 CPU topology\n");
#ifdef USE_FB
    if (display_init() < 0)
        printf("display: initialization failed, continuing with firmware framebuffer\n");
    fb_init(false);
    fb_display_logo();
    fb_set_active(true);
#endif
    printf("Initialization complete. Running proxy...\n");
    uartproxy_run(NULL);
}

void m1n1_main(void)
{
    printf("\n\nm1n1 %s\n", m1n1_version);
    printf("Copyright The Asahi Linux Contributors\n");
    printf("Licensed under the MIT license\n\n");
#ifdef T8140_KIS_PROXY
    printf("KIS carrier: retaining inherited DebugUSB\n");
#endif

    printf("Running in EL%lu\n\n", mrs(CurrentEL) >> 2);

    firmware_init();

    heapblock_init();

    if (chip_id == T8152) {
        j873g_run_proxy();
        goto next_stage;
    }

#ifndef BRINGUP
    if (supports_gxf())
        gxf_init();
    if (mcc_init() && chip_id == T8140)
        panic("T8140 MCC initialization failed\n");
    mmu_init();
    aic_init();
    smp_init();
#endif
    wdt_disable();
#ifdef J700_CDC_PROXY
    if (usb_cdc_arm_watchdog()) {
        wdt_reboot();
        panic("CDC watchdog could not be armed\n");
    }
    printf("J700 CDC flavour: DebugUSB until scheduled Gen1 transition\n");
#endif
#ifndef BRINGUP
    if (pmgr_init() && chip_id == T8140)
        panic("T8140 PMGR initialization failed\n");
#ifdef USE_DEBUG_USB
    tps6598x_enable_debugusb();
#endif
#ifdef USE_FB
    display_init();
    // Kick DCP to sleep, so dodgy monitors which cause reconnect cycles don't cause us to lose the
    // framebuffer.
    display_shutdown(DCP_SLEEP_IF_EXTERNAL);
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

next_stage:
    if (!next_stage.entry) {
        panic("Nothing to do!\n");
    }

    printf("Preparing to run next stage at %p...\n", next_stage.entry);

    if (chip_id != T8152 && !nvme_shutdown()) {
        printf("NVMe handoff failed; returning to proxy\n");
        uartproxy_run(NULL);
        panic("NVMe handoff failed\n");
    }
    exception_shutdown();
#ifndef BRINGUP
    if (chip_id != T8152) {
        usb_iodev_shutdown();
        display_shutdown(DCP_SLEEP_IF_EXTERNAL);
    }
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
