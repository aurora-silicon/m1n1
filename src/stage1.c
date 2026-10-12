/* SPDX-License-Identifier: MIT */

/*
 * ESP stage 1: m1n1 installed as the custom boot object of a macOS 26.6.2
 * volume. It loads one stage 2 file from the internal EFI system partition
 * and chainloads it. The partition and path are kept in an in-image
 * configuration block (stage1_cfg.h) that tools/j613_stage1_fill.py fills in.
 * If anything is missing or wrong it says so on the panel and falls back to
 * the USB proxy.
 *
 * Each image is built for one board (see the Makefile) and refuses to run on
 * any other.
 */

#include "../build/build_cfg.h"

#ifdef ESP_STAGE1

#include "adt.h"
#include "fb.h"
#include "firmware.h"
#include "iodev.h"
#include "payload.h"
#include "soc.h"
#include "stage1.h"
#include "stage1_cfg.h"
#include "string.h"
#include "types.h"
#include "uartproxy.h"
#include "usb.h"
#include "utils.h"

/*
 * Per board: the ADT target-type to admit, the model named on the failure
 * screen and the identity string's prefix.
 */
#if defined(ESP_STAGE1_J613)
#define STAGE1_TARGET_TYPE "J613"
#define STAGE1_MODEL       "MacBook Air 13\" M3 (J613)"
#define STAGE1_IDENT       "##j613_stage1##"
#elif defined(ESP_STAGE1_J615)
// Experimental: J615 has the same SoC and 26.6.2 OS firmware as J613, but this
// build has not yet been tested on a J615
#define STAGE1_TARGET_TYPE "J615"
#define STAGE1_MODEL       "MacBook Air 15-inch M3 (J615)"
#define STAGE1_IDENT       "##j615_stage1##"
#else
#error "No stage 1 board selected"
#endif

#define STAGE1_OS_FIRMWARE V26_6_2

/*
 * Lets the filler tell a stage 1 image from other m1n1 builds, and which board
 * it was built for. It must appear exactly once in the image.
 */
static const char stage1_ident[] = STAGE1_IDENT STAGE1_TARGET_TYPE " macOS 26.6.2 (25G83)";

/*
 * The configuration block. The filler patches it in the image file, so it
 * lives in initialised data, and this is the only copy of the magic in the
 * image. Nothing writes it at run time.
 */
struct stage1_cfg stage1_cfg_block ALIGNED(4) = {
    .magic = STAGE1_CFG_MAGIC,
    .version = {STAGE1_CFG_VERSION, 0, 0, 0},
};

static enum stage1_cfg_state cfg_state;
static struct stage1_cfg_info cfg_info;
static bool cfg_checked;
static bool usb_up;

static bool stage1_guard(void)
{
    u32 len = 0;
    const char *target = adt_getprop(adt, 0, "target-type", &len);

    if (target && !memchr(target, 0, len))
        target = NULL;

    if (chip_id == T8122 && target && !strcmp(target, STAGE1_TARGET_TYPE) &&
        os_firmware.version == STAGE1_OS_FIRMWARE)
        return true;

    printf("stage1: board/25G83 guard rejected this boot (chip 0x%x, target %s, OS firmware %s)\n",
           chip_id, target ? target : "?", os_firmware.iboot ? os_firmware.iboot : "?");
    return false;
}

static enum stage1_cfg_state stage1_config(void)
{
    const char *why = "unknown";

    if (cfg_checked)
        return cfg_state;

    cfg_state = stage1_cfg_parse(&stage1_cfg_block, &cfg_info, &why);
    cfg_checked = true;

    switch (cfg_state) {
        case STAGE1_CFG_FACTORY:
            printf("stage1: configuration not filled in (factory image), proxy only\n");
            break;
        case STAGE1_CFG_INVALID:
            printf("stage1: in-image config is invalid (%s), using the proxy fallback\n", why);
            break;
        case STAGE1_CFG_VALID:
            printf("stage1: stage 2 is %s, USB window %u ms\n", cfg_info.spec, cfg_info.window_ms);
            break;
    }

    return cfg_state;
}

static void stage1_usb_init(void)
{
    if (usb_up)
        return;

    usb_init();
    usb_iodev_init();
    usb_up = true;
}

/*
 * Developer window: offer the USB proxy for window_ms before loading stage 2.
 * Uses the same connection test as the EARLY_PROXY_TIMEOUT loop in main.c, but
 * does not depend on there being a framebuffer. Returns true if a host
 * connected.
 */
static bool stage1_usb_window(u32 window_ms)
{
    printf("stage1: USB window open for %u ms\n", window_ms);

    stage1_usb_init();

    u64 deadline = timeout_calculate(window_ms * 1000);
    do {
        for (int i = 0; i < USB_IODEV_COUNT; i++) {
            iodev_id_t iodev = IODEV_USB0 + i;

            if (!(iodev_get_usage(iodev) & USAGE_UARTPROXY))
                continue;

            usb_iodev_vuart_setup(iodev);
            iodev_handle_events(iodev);
            if (iodev_can_write(iodev) || iodev_can_write(IODEV_USB_VUART))
                return true;
        }

        mdelay(10);
    } while (!timeout_expired(deadline));

    return false;
}

static void stage1_fail(void)
{
    fb_set_active(true);

    printf("\n");
    printf(STAGE1_TARGET_TYPE " Stage 1: could not start Linux from the EFI partition.\n");
    printf("  It needs a " STAGE1_MODEL ", macOS 26.6.2 exactly on\n");
    printf("  this volume, and the stage 2 file named in this stage 1:\n");
    if (stage1_config() == STAGE1_CFG_VALID)
        printf("    %s on the EFI partition with PARTUUID\n    %.36s\n", cfg_info.path,
               cfg_info.spec);
    else
        printf("    (none: this stage 1 has not been filled in properly)\n");
    printf("  To see what this stage 1 contains, run\n");
    printf("    j613_stage1_fill.py --check <image>\n");
    printf("  Hold the power button to shut down, then hold it again\n");
    printf("  to choose another system.\n");
    printf("\n");

    stage1_usb_init();

    printf("stage1: running the USB proxy\n");
    uartproxy_run(NULL);
}

void stage1_run_actions(void)
{
    printf("stage1: %s\n", stage1_ident + sizeof(STAGE1_IDENT) - 1);

    if (!stage1_guard())
        goto fail;

    if (stage1_config() != STAGE1_CFG_VALID)
        goto fail;

    if (!cfg_info.window_ms) {
        // The default: USB is not touched at all on the way to stage 2
        printf("stage1: no USB window, loading %s from the ESP\n", cfg_info.path);
    } else if (stage1_usb_window(cfg_info.window_ms)) {
        printf("stage1: USB host connected in the window, running the proxy instead of stage 2\n");
        uartproxy_run(NULL);
        return;
    } else {
        printf("stage1: USB window expired, loading %s from the ESP\n", cfg_info.path);
    }

    if (payload_chainload(cfg_info.spec) == 0) {
        printf("stage1: stage 2 loaded\n");
        return;
    }

    printf("stage1: could not load stage 2\n");

fail:
    stage1_fail();
}

#endif
