/* SPDX-License-Identifier: MIT */

#include "adt.h"
#include "chickens.h"
#include "dockchannel_uart.h"
#include "exception.h"
#include "firmware.h"
#include "platform_identity.h"
#include "smp.h"
#include "string.h"
#include "types.h"
#include "uart.h"
#include "utils.h"
#include "xnuboot.h"


u64 boot_args_addr;
struct boot_args cur_boot_args;
void *adt;

struct rela_entry {
    uint64_t off, type, addend;
};

void debug_putc(char c);
void m1n1_main(void);

extern char _bss_start[0];
extern char _bss_end[0];

#define R_AARCH64_RELATIVE 1027

void apply_rela(uint64_t base, struct rela_entry *rela_start, struct rela_entry *rela_end)
{
    struct rela_entry *e = rela_start;

    while (e < rela_end) {
        switch (e->type) {
            case R_AARCH64_RELATIVE:
                *(u64 *)(base + e->off) = base + e->addend;
                break;
            default:
                debug_putc('R');
                debug_putc('!');
                while (1)
                    ;
        }
        e++;
    }
}

extern uint32_t _v_sp0_sync[], _v_sp0_irq[], _v_sp0_fiq[], _v_sp0_serr[];
void pan_fixup(void)
{
    if (supports_pan())
        return;

    /* Patch msr pan, #0 to nop */
    _v_sp0_sync[0] = 0xd503201f;
    _v_sp0_irq[0] = 0xd503201f;
    _v_sp0_fiq[0] = 0xd503201f;
    _v_sp0_serr[0] = 0xd503201f;

    sysop("isb");
}

u64 boot_flags, mem_size_actual;
void dump_boot_args(struct boot_args *ba)
{
    if (ba->revision > 3) {
        printf("Unsupported boot_args revision %hu\n!", ba->revision);
    }

    printf("  revision:     %d\n", ba->revision);
    printf("  version:      %d\n", ba->version);
    printf("  virt_base:    0x%lx\n", ba->virt_base);
    printf("  phys_base:    0x%lx\n", ba->phys_base);
    printf("  mem_size:     0x%lx\n", ba->mem_size);
    printf("  top_of_kdata: 0x%lx\n", ba->top_of_kernel_data);
    printf("  video:\n");
    printf("    base:       0x%lx\n", ba->video.base);
    printf("    display:    0x%lx\n", ba->video.display);
    printf("    stride:     0x%lx\n", ba->video.stride);
    printf("    width:      %lu\n", ba->video.width);
    printf("    height:     %lu\n", ba->video.height);
    printf("    depth:      %lubpp\n", ba->video.depth & 0xff);
    printf("    density:    %lu\n", ba->video.depth >> 16);
    printf("  machine_type: %d\n", ba->machine_type);
    printf("  devtree:      %p\n", ba->devtree);
    printf("  devtree_size: 0x%x\n", ba->devtree_size);
    int node = adt_path_offset(adt, "/chosen");

    if (node < 0) {
        printf("ADT: no /chosen found\n");
        return;
    }

    /* This is called very early - before firmware information is initialized */
    u32 len;
    const char *p = adt_getprop(adt, node, "firmware-version", &len);
    if (!p) {
        printf("ADT: failed to find firmware-version\n");
        return;
    }

    uint16_t version = ba->revision;
    u32 iboot_min[IBOOT_VER_COMP] = {0};
    u32 iboot_version[IBOOT_VER_COMP] = {0};
    u32 iboot_ba_v1_max[IBOOT_VER_COMP] = {5539}; /* iOS 13 = 5540 */

    firmware_parse_version(p, iboot_version);
    if (firmware_iboot_in_range(iboot_min, iboot_ba_v1_max, iboot_version))
        version = 1;

    switch (version) {
        case 1:
            printf("  cmdline:      %s\n", ba->rv1.cmdline);
            printf("  boot_flags:   0x%lx\n", ba->rv1.boot_flags);
            printf("  mem_size_act: 0x%lx\n", ba->rv1.mem_size_actual);
            boot_flags = ba->rv1.boot_flags;
            mem_size_actual = ba->rv1.mem_size_actual;
            break;
        case 2:
            printf("  cmdline:      %s\n", ba->rv2.cmdline);
            printf("  boot_flags:   0x%lx\n", ba->rv2.boot_flags);
            printf("  mem_size_act: 0x%lx\n", ba->rv2.mem_size_actual);
            boot_flags = ba->rv2.boot_flags;
            mem_size_actual = ba->rv2.mem_size_actual;
            break;
        case 3:
        default:
            printf("  cmdline:      %s\n", ba->rv3.cmdline);
            printf("  boot_flags:   0x%lx\n", ba->rv3.boot_flags);
            printf("  mem_size_act: 0x%lx\n", ba->rv3.mem_size_actual);
            boot_flags = ba->rv3.boot_flags;
            mem_size_actual = ba->rv3.mem_size_actual;
            break;
    }
    if (!mem_size_actual) {
        if (chip_id == T8012) {
            int anode = adt_path_offset(adt, "/arm-io/mcc");

            /*
             * For T8012, compute mem_size_actual from the amount of memory channels
             * enabled as there are large amounts of reserved memory intended as
             * SSD cache. Cannot use dram-size, it may not exist in older firmwares.
             * /arm-io/mcc/dcs_num_channels is changed from 4 to 2 by iBoot on 1 GB RAM
             * models.
             */

            u32 dcs_num_channels = 0;
            if (anode > 0 && ADT_GETPROP(adt, anode, "dcs_num_channels", &dcs_num_channels) > 0)
                mem_size_actual = dcs_num_channels * 0x20000000;
            else
                mem_size_actual = 0x40000000;
        } else {
            mem_size_actual = ALIGN_UP(ba->phys_base + ba->mem_size - 0x800000000, BIT(30));
        }
        printf("Correcting mem_size_actual to 0x%lx\n", mem_size_actual);
    }
}

extern void get_device_info(void);

#ifdef J873_DIAGNOSTIC
/* Opt-in first-entry recorder. Keep this before all normal platform init.
 * UART is the only device accessed, and only after a complete identity check.
 * The assembly entry, iBoot ABI and UART clock state still require hardware proof.
 */
static __attribute__((noreturn)) void j873_diagnostic(void)
{
    int chosen = adt_path_offset(adt, "/chosen");
    if (chosen < 0 || ADT_GETPROP(adt, chosen, "chip-id", &chip_id) != sizeof(chip_id) ||
        ADT_GETPROP(adt, chosen, "board-id", &board_id) != sizeof(board_id) ||
        !platform_is_j873g())
        goto halt;

    if (uart_init() < 0)
        goto halt;

    uart_puts("J873g M6 diagnostic v1: experimental first entry");
    uart_printf("chip=%x board=%x EL=%lu MIDR=%016lx MPIDR=%016lx\n", chip_id, board_id,
                mrs(CurrentEL) >> 2, mrs(MIDR_EL1), mrs(MPIDR_EL1));
    uart_printf("CNTFRQ=%lu MMFR0=%016lx ISAR0=%016lx PFR0=%016lx\n", mrs(CNTFRQ_EL0),
                mrs(ID_AA64MMFR0_EL1), mrs(ID_AA64ISAR0_EL1), mrs(ID_AA64PFR0_EL1));
    uart_printf("bootargs rev=%u ver=%u phys=%016lx virt=%016lx mem=%016lx\n",
                cur_boot_args.revision, cur_boot_args.version, cur_boot_args.phys_base,
                cur_boot_args.virt_base, cur_boot_args.mem_size);
    uart_printf("ADT=%p size=%x video=%016lx %lux%lu stride=%lu\n", adt,
                cur_boot_args.devtree_size, cur_boot_args.video.base,
                cur_boot_args.video.width, cur_boot_args.video.height, cur_boot_args.video.stride);
    int cpus = adt_path_offset(adt, "/cpus");
    if (cpus >= 0) {
        int remaining = adt_get_child_count(adt, cpus);
        int cpu = remaining > 0 ? adt_first_child_offset(adt, cpus) : -1;
        while (remaining-- > 0 && cpu >= 0) {
            u32 len = 0;
            const char *state = adt_getprop(adt, cpu, "state", &len);
            u32 cpu_id = ~0;
            ADT_GETPROP(adt, cpu, "cpu-id", &cpu_id);
            uart_printf("ADT cpu-id=%u state=%.*s\n", cpu_id, (int)len, state ? state : "");
            /* The last CPU can also be the last node in a synthetic ADT. */
            if (remaining > 0)
                cpu = adt_next_sibling_offset(adt, cpu);
        }
    }
    uart_puts("J873_DIAGNOSTIC_COMPLETE: halting before normal initialization");
halt:
    while (1)
        sysop("wfe");
}
#endif

void _start_c(void *boot_args, void *base)
{
    UNUSED(base);
    u32 cpu_id = 0;

    memset64(_bss_start, 0, _bss_end - _bss_start);
    boot_args_addr = (u64)boot_args;
    memcpy(&cur_boot_args, boot_args, sizeof(cur_boot_args));

    adt =
        (void *)(((u64)cur_boot_args.devtree) - cur_boot_args.virt_base + cur_boot_args.phys_base);

#ifdef J873_DIAGNOSTIC
    j873_diagnostic();
#endif

#ifndef BRINGUP
    int node = adt_path_offset(adt, "/cpus");
    if (node >= 0) {
        ADT_FOREACH_CHILD(adt, node)
        {
            const char *state = adt_getprop(adt, node, "state", NULL);
            if (!state)
                continue;
            if (strcmp(state, "running") == 0)
                if (ADT_GETPROP(adt, node, "cpu-id", &cpu_id) == sizeof(cpu_id))
                    break;
        }
    }
#endif

    if (in_el2())
        msr(TPIDR_EL2, cpu_id);
    else
        msr(TPIDR_EL1, cpu_id);

    int ret = uart_init();
    if (ret < 0) {
        debug_putc('!');
    }

    /* Use Dockchannel as a secondary UART for now and ignore failure */
    dockchannel_uart_init();

    uart_puts("Initializing");
    get_device_info();

    printf("CPU init (MIDR: 0x%lx smp_id:0x%x)...\n", mrs(MIDR_EL1), smp_id());
    init_cpu();
    printf("\n");

    printf("boot_args at %p\n", boot_args);

    dump_boot_args(&cur_boot_args);
    printf("\n");

    exception_initialize();
    m1n1_main();
}

/* Secondary SMP core boot */
void _cpu_reset_c(void *stack)
{
    //
    // Secondaries stay off the console on T8142 -- see T8142_QUIET_SECONDARY in
    // src/smp.c for why. The boot CPU still prints normally.
    //
    bool quiet = (chip_id == T8142) && !is_boot_cpu();
    int secondary_cpu = -1;

    if (!is_boot_cpu()) {
        secondary_cpu = smp_secondary_prepare();
        if (secondary_cpu < 0)
            while (1)
                sysop("wfe");
    }

    if (!quiet) {
        if (!is_boot_cpu())
            uart_puts("RVBAR entry on secondary CPU");
        else
            uart_puts("RVBAR entry on primary CPU");

        printf("\n  Stack base: %p\n", stack);
        printf("  MPIDR: 0x%lx\n", mrs(MPIDR_EL1));
    }

    init_cpu();

    if (secondary_cpu >= 0)
        smp_secondary_mark_init_complete(secondary_cpu);

    if (!quiet)
        printf("  Running in EL%lu\n\n", mrs(CurrentEL) >> 2);

    exception_initialize();

    if (in_el3()) {
        smp_secondary_prep_el3();
        return;
    }

    if (!is_boot_cpu())
        smp_secondary_entry();
    else
        m1n1_main();
}
