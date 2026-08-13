/* SPDX-License-Identifier: MIT */
/*
 * Host tests for the framebuffer change detector. No hardware: the point is
 * the ABI the host tool depends on, and the dirty-tracking logic that decides
 * how much of a 16.25 MiB frame ever crosses the USB link.
 */
#define FB_CAPTURE_HOST_TEST 1
#include "fb_capture.h"
#include "fb_capture.c"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures;

static void check(int cond, const char *what)
{
    if (!cond) {
        printf("fb_capture: FAIL: %s\n", what);
        failures++;
    }
}

static u32 dirty_count(const struct fb_capture_desc *desc)
{
    u32 n = 0;
    for (u32 i = 0; i < desc->tile_count; i++)
        if (desc->dirty[i / 32] & (1u << (i % 32)))
            n++;
    return n;
}

int main(void)
{
    /* The layout the host tool mirrors. If this moves, the decoder silently
     * reads garbage, so it is asserted rather than reviewed. */
    check(sizeof(struct fb_capture_surface) == 32, "surface is 32 bytes");
    check(FB_CAPTURE_MAGIC == 0x50424346, "magic is 'FBCP'");

    static struct fb_capture_desc desc;
    memset(&desc, 0, sizeof(desc));

    /* J813's panel divides evenly; assert the arithmetic anyway. */
    fb_capture_layout(&desc, 2560, 1664);
    check(desc.tiles_x == 40 && desc.tiles_y == 26, "2560x1664 -> 40x26 tiles");
    check(desc.tile_count == 1040, "1040 tiles");

    /* A panel that does NOT divide evenly must round up, or the right and
     * bottom edges would never be watched. */
    fb_capture_layout(&desc, 1920, 1200);
    check(desc.tiles_x == 30 && desc.tiles_y == 19, "1920x1200 rounds up");

    /* Refuse rather than overflow the fixed arrays. */
    fb_capture_layout(&desc, 16384, 16384);
    check(desc.tile_count == 0, "oversized panel fails closed");

    fb_capture_layout(&desc, 2560, 1664);
    memset(desc.hash, 0, sizeof(desc.hash));
    memset(desc.dirty, 0, sizeof(desc.dirty));

    /* First sighting of any tile is dirty: the descriptor starts zeroed, and
     * a real hash must never collide with "never seen". */
    check(fb_capture_note(&desc, 0, 0x1234), "first hash marks dirty");
    check(!fb_capture_note(&desc, 0, 0x1234), "unchanged hash is not dirty");
    check(fb_capture_note(&desc, 0, 0x1235), "changed hash marks dirty");
    check(dirty_count(&desc) == 1, "one dirty tile");
    check(!fb_capture_note(&desc, desc.tile_count, 0xabc),
          "out-of-range index is rejected");

    /* The property the whole design rests on: a static screen produces no
     * dirty tiles, so it costs no bandwidth. */
    static u32 fb[128 * 128];
    for (int i = 0; i < 128 * 128; i++)
        fb[i] = 0x00204060u + (u32)i;
    u32 h1 = fb_capture_tile_hash(fb, 128, 64, 64);
    u32 h2 = fb_capture_tile_hash(fb, 128, 64, 64);
    check(h1 == h2, "hash is stable over unchanged pixels");
    check(h1 != 0, "hash is never zero");

    /* One flipped pixel anywhere in the tile must be caught -- including the
     * last, which an off-by-one in the loop bounds would miss. */
    u32 save = fb[63 * 128 + 63];
    fb[63 * 128 + 63] ^= 1u;
    check(fb_capture_tile_hash(fb, 128, 64, 64) != h1,
          "single flipped pixel in the last position changes the hash");
    fb[63 * 128 + 63] = save;
    fb[0] ^= 1u;
    check(fb_capture_tile_hash(fb, 128, 64, 64) != h1,
          "single flipped pixel in the first position changes the hash");
    fb[0] ^= 1u;
    check(fb_capture_tile_hash(fb, 128, 64, 64) == h1, "restore is exact");

    /* A tile must not read pixels belonging to its neighbour: hashing a 64x64
     * window of a 128-wide surface must ignore columns 64..127. */
    u32 before = fb_capture_tile_hash(fb, 128, 64, 64);
    fb[64] ^= 0xffffffffu; /* first pixel of the NEXT tile on row 0 */
    check(fb_capture_tile_hash(fb, 128, 64, 64) == before,
          "hash respects the tile width and does not bleed into the neighbour");

    /* Partial edge tiles are legal and must hash only what exists. */
    check(fb_capture_tile_hash(fb, 128, 17, 5) != 0, "partial tile hashes");

    if (failures) {
        printf("fb_capture: %d check(s) failed\n", failures);
        return 1;
    }
    printf("fb_capture: ABI + dirty-tracking + tile hash contract passed\n");
    return 0;
}
