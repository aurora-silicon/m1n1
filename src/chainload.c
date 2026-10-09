/* SPDX-License-Identifier: MIT */

#include "../build/build_cfg.h"

#include "chainload.h"
#include "adt.h"
#include "heapblock.h"
#include "malloc.h"
#include "memory.h"
#include "nvme.h"
#include "string.h"
#include "types.h"
#include "usb.h"
#include "utils.h"
#include "xnuboot.h"

#ifdef CHAINLOADING
int rust_load_image(const char *spec, void **image, size_t *size);
void rust_free_image(void *image, size_t size);
#endif

extern u8 _chainload_stub_start[];
extern u8 _chainload_stub_end[];

int chainload_image(void *image, size_t size, char **vars, size_t var_cnt)
{
    u64 new_base = (u64)_base;
    u64 image_size = size;
    u64 ram_end, ro_start = 0, ro_end = 0;
    u64 live_heap = heapblock_high_water();
    u64 inherited_top = cur_boot_args.top_of_kernel_data;
    int ro_status = memory_fw_ro_range(&ro_start, &ro_end);

    printf("chainload: Preparing image...\n");
    next_stage.entry = NULL;
    if (usb_dwc3_shutdown_failed())
        return -1;
    if (!image || !size || (chip_id == T8140 && ro_status != 1) ||
        cur_boot_args.phys_base > UINT64_MAX - cur_boot_args.mem_size)
        return -1;
    ram_end = cur_boot_args.phys_base + cur_boot_args.mem_size;
    if (new_base < cur_boot_args.phys_base || new_base >= ram_end || live_heap > ram_end ||
        inherited_top > ram_end)
        return -1;

    for (size_t i = 0; i < var_cnt; i++) {
        size_t len = strlen(vars[i]) + 1;
        if (image_size > UINT64_MAX - len)
            return -1;
        image_size += len;
    }
    if (image_size > UINT64_MAX - 4 - (SZ_16K - 1))
        return -1;
    image_size = ALIGN_UP(image_size + 4, SZ_16K);

    int anode = adt_path_offset(adt, "/chosen/memory-map");
    if (anode < 0)
        return -1;
    u64 sepfw[2];
    if (ADT_GETPROP_ARRAY(adt, anode, "SEPFW", sepfw) < 0 || sepfw[0] > UINT64_MAX - sepfw[1] ||
        sepfw[0] < cur_boot_args.phys_base || sepfw[0] + sepfw[1] > ram_end ||
        sepfw[1] > UINT64_MAX - (SZ_16K - 1))
        return -1;

    u64 fw_bootargs_off = ALIGN_UP(sepfw[1], SZ_16K);
    if (fw_bootargs_off > UINT64_MAX - SZ_16K)
        return -1;
    u64 fw_size = fw_bootargs_off + SZ_16K;
    if (image_size > UINT64_MAX - fw_size || new_base > UINT64_MAX - image_size - fw_size)
        return -1;

    u64 image_end = new_base + image_size;
    u64 packed_end = image_end + fw_size;
    if (packed_end > ram_end || (ro_status == 1 && image_end > ro_start && new_base < ro_end)) {
        printf("chainload: image destination is invalid or overlaps firmware RO\n");
        return -1;
    }
    if (live_heap > inherited_top && image_end > inherited_top && new_base < live_heap) {
        printf("chainload: image span overlaps the live heap\n");
        return -1;
    }

    bool park_fw =
        (ro_status == 1 && packed_end > ro_start && image_end < ro_end) ||
        (live_heap > inherited_top && packed_end > inherited_top && image_end < live_heap);
    u8 *parked = NULL;
    u64 fw_base = image_end;
    if (park_fw) {
        printf(
            "chainload: packed span would overlap protected/live memory; parking firmware block\n");
        parked = memalign(SZ_16K, fw_size);
        if (!parked || (u64)parked > ram_end || fw_size > ram_end - (u64)parked) {
            free(parked);
            return -1;
        }
        fw_base = (u64)parked;
    }

    u64 copy_size = park_fw ? image_size : image_size + fw_size;
    size_t stub_size = _chainload_stub_end - _chainload_stub_start;
    if (copy_size > SIZE_MAX - stub_size) {
        free(parked);
        return -1;
    }
    u8 *new_image = memalign(SZ_16K, copy_size + stub_size);
    if (!new_image) {
        free(parked);
        return -1;
    }
    memset(new_image, 0, copy_size);
    memcpy(new_image, image, size);

    u8 *p = new_image + size;
    for (size_t i = 0; i < var_cnt; i++) {
        size_t len = strlen(vars[i]);
        memcpy(p, vars[i], len);
        p[len] = '\n';
        p += len + 1;
    }

    u8 *fw_data = parked ? parked : new_image + image_size;
    memset(fw_data, 0, fw_size);
    memcpy(fw_data, (void *)sepfw[0], sepfw[1]);
    struct boot_args *new_boot_args = (struct boot_args *)(fw_data + fw_bootargs_off);
    *new_boot_args = cur_boot_args;
    u64 high_water = max(image_end, fw_base + fw_size);
    high_water = max(high_water, live_heap);
    high_water = max(high_water, inherited_top);
    if (ro_status == 1)
        high_water = max(high_water, ro_end);
    new_boot_args->top_of_kernel_data = high_water;

    u64 new_sepfw[2] = {fw_base, sepfw[1]};
    if (adt_setprop(adt, anode, "SEPFW", new_sepfw, sizeof(new_sepfw)) < 0) {
        free(new_image);
        free(parked);
        return -1;
    }
    if (parked) {
        dc_cvac_range(parked, fw_size);
        sysop("dsb sy");
    }

    void *stub = new_image + copy_size;
    memcpy(stub, _chainload_stub_start, stub_size);
    dc_cvac_range(stub, stub_size);
    sysop("dsb sy");
    ic_ivau_range(stub, stub_size);
    sysop("dsb sy");
    sysop("isb");

    printf("chainload: image 0x%lx bytes; firmware block at 0x%lx; high-water 0x%lx\n", copy_size,
           fw_base, high_water);
    next_stage.entry = stub;
    next_stage.args[0] = fw_base + fw_bootargs_off;
    next_stage.args[1] = (u64)new_image;
    next_stage.args[2] = new_base;
    next_stage.args[3] = copy_size;
    next_stage.args[4] = new_base + 0x800;
    next_stage.restore_logo = false;
    return 0;
}

#ifdef CHAINLOADING

int chainload_load(const char *spec, char **vars, size_t *var_cnt, size_t var_capacity)
{
    static char adopt_var[] = "nvme.adopt=live-rtkit-v1";
    void *image;
    size_t size;
    int ret;

    if (usb_dwc3_shutdown_failed()) {
        next_stage.entry = NULL;
        return -1;
    }

    if (!nvme_init()) {
        printf("chainload: NVME init failed\n");
        return -1;
    }

    if (nvme_has_live_post_m4_session()) {
        bool present = false;
        for (size_t i = 0; i < *var_cnt; i++) {
            if (!strcmp(vars[i], adopt_var))
                present = true;
        }
        if (!present) {
            if (*var_cnt >= var_capacity) {
                printf("chainload: no room for ANS adoption variable\n");
                nvme_shutdown();
                return -1;
            }
            vars[(*var_cnt)++] = adopt_var;
        }
    }

    ret = rust_load_image(spec, &image, &size);
    if (ret == 0) {
        ret = chainload_image(image, size, vars, *var_cnt);
        rust_free_image(image, size);
    }
    if (!nvme_shutdown()) {
        next_stage.entry = NULL;
        return -1;
    }
    if (ret)
        next_stage.entry = NULL;
    return ret;
}

#else

int chainload_load(const char *spec, char **vars, size_t *var_cnt, size_t var_capacity)
{
    UNUSED(spec);
    UNUSED(vars);
    UNUSED(var_cnt);
    UNUSED(var_capacity);

    printf("Chainloading files not supported in this build!\n");
    return -1;
}

#endif
