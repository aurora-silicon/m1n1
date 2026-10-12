/* SPDX-License-Identifier: MIT */

#include "../build/build_cfg.h"

#include "chainload.h"
#include "adt.h"
#include "malloc.h"
#include "memory.h"
#include "nvme.h"
#include "string.h"
#include "types.h"
#include "utils.h"
#include "xnuboot.h"

#ifdef CHAINLOADING
int rust_load_image(const char *spec, void **image, size_t *size);
#endif

extern u8 _chainload_stub_start[];
extern u8 _chainload_stub_end[];

/*
 * Boot blobs in /chosen/memory-map that travel with the next stage. Each one
 * is copied behind the image and its ADT range is pointed at the new copy.
 */
struct chainload_blob {
    const char *name;
    bool required;  // fail if the property is missing
    bool check_ram; // the source must lie inside RAM
    u64 range[2];   // as found in the ADT: phys, size
    size_t off;     // offset of the copy in the new image, 0 if not copied
};

static bool chainload_in_ram(u64 phys, u64 size)
{
    u64 base = cur_boot_args.phys_base;
    u64 ram = cur_boot_args.mem_size;

    if (phys < base)
        return false;

    u64 off = phys - base;
    return off <= ram && size <= ram - off;
}

static int chainload_get_range(int node, const char *name, u64 range[2], bool required)
{
    u32 len = 0;
    const void *prop = adt_getprop(adt, node, name, &len);

    range[0] = range[1] = 0;

    if (!prop) {
        if (required) {
            printf("chainload: Failed to find %s\n", name);
            return -1;
        }
        return 0;
    }

    if (len != 2 * sizeof(u64)) {
        printf("chainload: Malformed %s property (%u bytes)\n", name, len);
        return -1;
    }

    memcpy(range, prop, 2 * sizeof(u64));
    return 1;
}

static int chainload_set_range(int node, const char *name, u64 phys, u64 size)
{
    u64 range[2] = {phys, size};

    if (adt_setprop(adt, node, name, range, sizeof(range)) < 0) {
        printf("chainload: Failed to set %s prop\n", name);
        return -1;
    }

    return 0;
}

int chainload_image(void *image, size_t size, char **vars, size_t var_cnt)
{
    u64 new_base = (u64)_base;
    size_t image_size = size;
    struct chainload_blob blobs[] = {
        {.name = "SEPFW", .required = true},
        {.name = "preoslog", .check_ram = true},
    };
    u64 bootargs_range[2];

    printf("chainload: Preparing image...\n");

    // m1n1 variables
    for (size_t i = 0; i < var_cnt; i++)
        image_size += strlen(vars[i]) + 1;

    // pad to end payload
    image_size += 4;
    image_size = ALIGN_UP(image_size, SZ_16K);

    int anode = adt_path_offset(adt, "/chosen/memory-map");
    if (anode < 0) {
        printf("chainload: /chosen/memory-map not found\n");
        return -1;
    }

    // Boot blobs (SEPFW, preoslog), each 16K aligned
    for (size_t i = 0; i < ARRAY_SIZE(blobs); i++) {
        struct chainload_blob *b = &blobs[i];

        if (chainload_get_range(anode, b->name, b->range, b->required) < 0)
            return -1;
        if (!b->range[1])
            continue;

        if (b->check_ram && !chainload_in_ram(b->range[0], b->range[1])) {
            printf("chainload: %s at 0x%lx (0x%lx bytes) is not inside RAM\n", b->name, b->range[0],
                   b->range[1]);
            return -1;
        }

        b->off = image_size;
        image_size += b->range[1];
        image_size = ALIGN_UP(image_size, SZ_16K);
    }

    if (chainload_get_range(anode, "BootArgs", bootargs_range, true) < 0)
        return -1;

    // Bootargs
    size_t bootargs_off = image_size;
    const size_t bootargs_size = SZ_16K;
    image_size += bootargs_size;

    printf("chainload: Total image size: 0x%lx\n", image_size);

    size_t stub_size = _chainload_stub_end - _chainload_stub_start;

    void *new_image = malloc(image_size + stub_size);
    if (!new_image) {
        printf("chainload: Failed to allocate 0x%lx bytes for the new image\n",
               image_size + stub_size);
        return -1;
    }

    // Copy m1n1
    memcpy(new_image, image, size);

    // Add vars
    u8 *p = new_image + size;
    for (size_t i = 0; i < var_cnt; i++) {
        size_t len = strlen(vars[i]);

        memcpy(p, vars[i], len);
        p[len] = '\n';
        p += len + 1;
    }

    // Add end padding
    memset(p, 0, 4);

    // Copy the boot blobs
    for (size_t i = 0; i < ARRAY_SIZE(blobs); i++) {
        struct chainload_blob *b = &blobs[i];

        if (b->range[1])
            memcpy(new_image + b->off, (void *)b->range[0], b->range[1]);
    }

    // Point the ADT at the new copies, undoing earlier changes if one fails
    size_t set;
    int ret = 0;
    for (set = 0; set < ARRAY_SIZE(blobs); set++) {
        struct chainload_blob *b = &blobs[set];

        if (b->range[1] &&
            chainload_set_range(anode, b->name, new_base + b->off, b->range[1]) < 0) {
            ret = -1;
            break;
        }
    }

    if (!ret && chainload_set_range(anode, "BootArgs", new_base + bootargs_off, bootargs_size) < 0)
        ret = -1;

    if (ret < 0) {
        while (set--) {
            struct chainload_blob *b = &blobs[set];

            if (b->range[1])
                chainload_set_range(anode, b->name, b->range[0], b->range[1]);
        }
        free(new_image);
        return -1;
    }

    // Copy bootargs
    struct boot_args *new_boot_args = new_image + bootargs_off;
    *new_boot_args = cur_boot_args;
    new_boot_args->top_of_kernel_data = new_base + image_size;

    // Copy chainload stub
    void *stub = new_image + image_size;
    memcpy(stub, _chainload_stub_start, stub_size);
    dc_cvau_range(stub, stub_size);
    ic_ivau_range(stub, stub_size);

    // Set up next stage
    next_stage.entry = stub;
    next_stage.args[0] = new_base + bootargs_off;
    next_stage.args[1] = (u64)new_image;
    next_stage.args[2] = new_base;
    next_stage.args[3] = image_size;
    next_stage.args[4] = new_base + 0x800; // m1n1 entrypoint
    next_stage.restore_logo = false;

    return 0;
}

#ifdef CHAINLOADING

int chainload_load(const char *spec, char **vars, size_t var_cnt)
{
    void *image;
    size_t size;
    int ret;

    if (!nvme_init()) {
        printf("chainload: NVME init failed\n");
        return -1;
    }

    ret = rust_load_image(spec, &image, &size);
    nvme_shutdown();
    if (ret < 0)
        return ret;

    return chainload_image(image, size, vars, var_cnt);
}

#else

int chainload_load(const char *spec, char **vars, size_t var_cnt)
{
    UNUSED(spec);
    UNUSED(vars);
    UNUSED(var_cnt);

    printf("Chainloading files not supported in this build!\n");
    return -1;
}

#endif
