"""Exercise display adoption and sink/error outcomes without hardware access."""

import subprocess
from pathlib import Path


def test_t8152_display_outcomes(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/display.c").read_text()
    start = source.index("static int display_t8152_work(")
    end = source.index("\nint display_init(void)", start)
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
typedef unsigned long u64;
typedef unsigned int u32;
typedef struct { int route; } dptx_phy_t;
typedef struct { dptx_phy_t *phy; void *dpav_ep, *dptx_ep; } dcp_dev_t;
static struct { struct { u64 base, width, height; } video; } cur_boot_args;
static dptx_phy_t phys[2] = {{0}, {1}};
static dcp_dev_t device, *dcp;
static void *iboot;
static bool present[2], sink[2], node_present[2];
static int route, start_error, hpd_error, modeset, modes, stopped, tick;
static void *adt;
#define DCP_QUIESCED 0
static u64 timeout_calculate(u32 usec) { (void)usec; tick = 0; return 2; }
static u64 get_ticks(void) { return ++tick; }
static int dcp_work(dcp_dev_t *dev) { assert(dev == &device); return 0; }
static int adt_path_offset(void *tree, const char *path)
{
    int r = !strcmp(path, "/arm-io/atc-phy3");
    assert(tree == adt);
    assert(r || !strcmp(path, "/arm-io/dptx-phy"));
    return node_present[r] ? 1 : -1;
}
static dptx_phy_t *dptx_phy_init(const char *path, u32 index)
{
    assert(index == 0);
    route = !strcmp(path, "/arm-io/atc-phy3");
    assert(route || !strcmp(path, "/arm-io/dptx-phy"));
    return adt_path_offset(adt, path) >= 0 && present[route] ? &phys[route] : NULL;
}
static int display_start_dcp(void)
{
    if (start_error) return -1;
    memset(&device, 0, sizeof(device)); dcp = &device; iboot = &device;
    return 0;
}
static void dptx_phy_shutdown(dptx_phy_t *phy) { assert(phy); stopped++; }
static void *dcp_dpav_init(dcp_dev_t *dev) { return dev; }
static void *dcp_dptx_init(dcp_dev_t *dev, int count) { assert(count == 2); return dev; }
static int dcp_connect_dptx(dcp_dev_t *dev) { assert(dev->phy == &phys[route]); return 0; }
static int dcp_dptx_hpd(void *ep, int port, bool active)
{ assert(ep && port == 0 && active); return 0; }
static int dcp_ib_get_hpd(void *ep, int *timings, int *colors)
{ assert(ep); *timings = *colors = modes; return hpd_error ? -1 : sink[route]; }
static int dcp_ib_set_power(void *ep, bool active) { assert(ep && active); return 0; }
static int display_configure(const char *mode)
{ assert(!strcmp(mode, "1280x720@60")); return modeset; }
static int dcp_dptx_disconnect(void *ep, int port) { assert(ep && port == 0); return 0; }
static void display_shutdown(int mode) { assert(mode == DCP_QUIESCED); iboot = NULL; }
''' + source[start:end] + r'''
static void reset(void)
{
    cur_boot_args.video.base = 1; cur_boot_args.video.width = 640;
    cur_boot_args.video.height = 1136;
    present[0] = present[1] = sink[0] = sink[1] = false;
    node_present[0] = node_present[1] = true;
    start_error = hpd_error = stopped = 0; modeset = modes = 1;
    route = -1; dcp = NULL; iboot = NULL;
}
int main(void)
{
    reset(); cur_boot_args.video.width = 1920; cur_boot_args.video.height = 1080;
    assert(display_init_t8152() == 0 && route == -1);
    reset(); present[0] = sink[0] = true;
    assert(display_init_t8152() == 1 && dcp->phy == &phys[0] && !stopped);
    reset(); present[0] = present[1] = sink[1] = true;
    assert(display_init_t8152() == 1 && dcp->phy == &phys[1] && stopped == 1);
    reset(); present[0] = present[1] = true; start_error = 1;
    assert(display_init_t8152() == -1 && route == 0 && stopped == 1);
    reset(); present[0] = present[1] = true; hpd_error = 1;
    assert(display_init_t8152() == -1 && route == 0 && stopped == 1);
    reset(); present[0] = present[1] = sink[0] = true; modes = 0;
    assert(display_init_t8152() == -1 && route == 0 && stopped == 1);
    reset(); present[0] = present[1] = sink[0] = true; modeset = 0;
    assert(display_init_t8152() == -1 && route == 0 && stopped == 1);
    reset(); node_present[0] = false; present[1] = sink[1] = true;
    assert(display_init_t8152() == -1 && !dcp && route == 0 && stopped == 0);
    reset(); present[0] = false;
    assert(display_init_t8152() == -1 && route == 0 && !dcp && stopped == 0);
    puts("display adoption, HDMI, USB-C, and fail-closed outcomes passed");
}
'''
    binary = tmp_path / "display-outcomes"
    subprocess.run(
        ["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-x", "c", "-", "-o", str(binary)],
        input=harness, text=True, check=True,
    )
    result = subprocess.run([str(binary)], text=True, capture_output=True, check=True)
    assert "fail-closed outcomes passed" in result.stdout
