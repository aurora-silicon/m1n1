/* SPDX-License-Identifier: MIT */

#ifndef XNUBOOT_H
#define XNUBOOT_H

#include "types.h"

#define CMDLINE_LENGTH_RV1 256
#define CMDLINE_LENGTH_RV2 608
#define CMDLINE_LENGTH_RV3 1024

struct boot_video {
    u64 base;
    u64 display;
    u64 stride;
    u64 width;
    u64 height;
    u64 depth;
};

struct boot_args {
    u16 revision;
    u16 version;
    u64 virt_base;
    u64 phys_base;
    u64 mem_size;
    u64 top_of_kernel_data;
    struct boot_video video;
    u32 machine_type;
    void *devtree;
    u32 devtree_size;
    /*
     * iBoot places the command line directly after devtree_size, at 0x6c, as
     * XNU's boot_args and the proxyclient's BootArgs structs do. A naturally
     * aligned union would start at 0x70 and drop its first four characters.
     */
    union {
        struct {
            char cmdline[CMDLINE_LENGTH_RV1];
            u32 cmdline_pad;
            u64 boot_flags;
            u64 mem_size_actual;
        } PACKED rv1;
        struct {
            char cmdline[CMDLINE_LENGTH_RV2];
            u32 cmdline_pad;
            u64 boot_flags;
            u64 mem_size_actual;
        } PACKED rv2;
        struct {
            char cmdline[CMDLINE_LENGTH_RV3];
            u32 cmdline_pad;
            u64 boot_flags;
            u64 mem_size_actual;
        } PACKED rv3;
    } PACKED;
};

_Static_assert(__builtin_offsetof(struct boot_args, rv3.cmdline) == 0x6c,
               "boot_args command line must follow devtree_size");
_Static_assert(__builtin_offsetof(struct boot_args, rv1.boot_flags) == 0x170 &&
                   __builtin_offsetof(struct boot_args, rv2.boot_flags) == 0x2d0 &&
                   __builtin_offsetof(struct boot_args, rv3.boot_flags) == 0x470,
               "boot_args flags must keep their natural alignment");
_Static_assert(sizeof(struct boot_args) == 0x480, "boot_args size changed");

extern u64 boot_args_addr;
extern struct boot_args cur_boot_args;

#endif
