"""Run the entire production constructor with actual command/poll functions."""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def function(source, signature):
    start = source.index(signature)
    end = source.index("\n}", source.index("{", start)) + 2
    return source[start:end] + "\n"


@pytest.mark.parametrize("j700", [False, True])
def test_constructor_never_retries_or_releases_after_any_failure(tmp_path, j700):
    source = (ROOT / "src/usb_dwc3.c").read_text()
    declarations = source[source.index("#define MAX_ENDPOINTS"):source.index("static const struct usb_string_descriptor")]
    # The source declaration chunk already includes its retained-owner helper.
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define max(a,b) ((a) > (b) ? (a) : (b))
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#include "usb_dwc3_regs.h"
#include "usb_dwc3.h"
#include "usb_types.h"
#include "usb_cdc_bulk.h"
#include "usb_cdc_ss_regs.h"
#include "usb_cdc_state.h"
struct dart_dev;
struct mock_dart_params { void (*tlb_invalidate)(struct dart_dev *); };
struct dart_dev { bool failed, locked; uintptr_t regs; u8 device; const struct mock_dart_params *params; };
#define DART_T8110_TLB_CMD 0x80
#define DART_T8110_TLB_CMD_BUSY BIT(31)
#define DART_T8110_TLB_CMD_OP GENMASK(10, 8)
#define DART_T8110_TLB_CMD_OP_FLUSH_SID 1
#define DART_T8110_TLB_CMD_STREAM GENMASK(7, 0)
#define dma_wmb() ((void)0)
typedef struct { int unused; } ringbuffer_t;
static void debug_printf(const char *fmt, ...) {}
'''
    harness += declarations
    dart_source = (ROOT / "src/dart.c").read_text()
    dart_functions = function(dart_source, "static void dart_t8110_tlb_invalidate(")
    dart_functions += function(dart_source, "int dart_map_flags(")
    harness += r'''
static int kind, edge, allocs, maps, polls, status_reads, frees, unmaps, writes, reads;
static bool forbidden;
static void *allocated[32];
static dwc3_dev_t *constructed;
static u32 regs[0x30000/4];
static uintptr_t last_poll;
static bool is_command(uintptr_t addr) {
    if (addr == 0x1000 + DWC3_DGCMD) return true;
    for (int ep = 0; ep < MAX_ENDPOINTS; ep++)
        if (addr == (uintptr_t)0x1000 + DWC3_DEPCMD(ep)) return true;
    return false;
}
static u32 read32(uintptr_t addr) {
    assert(!forbidden); reads++;
    assert(addr >= 0x1000 && addr < 0x31000);
    if (addr == last_poll && is_command(addr)) {
        status_reads++;
        if (kind == 4 && status_reads == edge) { forbidden = true; return 1U << 15; }
    }
    return regs[(addr-0x1000)/4];
}
static void write32(uintptr_t addr, u32 value) {
    assert(!forbidden); writes++; regs[(addr-0x1000)/4] = value;
}
static void set32(uintptr_t addr, u32 value) { assert(!forbidden); write32(addr, regs[(addr-0x1000)/4] | value); }
static void clear32(uintptr_t addr, u32 value) { assert(!forbidden); write32(addr, regs[(addr-0x1000)/4] & ~value); }
static void mask32(uintptr_t addr, u32 mask, u32 value) { assert(!forbidden); write32(addr, (regs[(addr-0x1000)/4] & ~mask) | value); }
static int poll32(uintptr_t addr, u32 mask, u32 value, unsigned timeout) {
    assert(!forbidden); polls++; last_poll = addr;
    if ((kind == 3 && polls == edge) || (kind == 9 && addr == 0x20080 && maps == edge)) {
        forbidden = true; return -1;
    }
    regs[(addr-0x1000)/4] = (regs[(addr-0x1000)/4] & ~mask) | value;
    return 0;
}
static void *allocate(size_t size) {
    assert(!forbidden); allocs++;
    if (kind == 1 && allocs == edge) { forbidden = true; return NULL; }
    void *p = calloc(1, size); assert(p); allocated[allocs-1] = p; return p;
}
static void *test_calloc(size_t count, size_t size) {
    void *p = allocate(count * size); constructed = p; return p;
}
static void *memalign(size_t align, size_t size) { return allocate(size); }
static ringbuffer_t *ringbuffer_alloc(size_t size) { return allocate(sizeof(ringbuffer_t)); }
int dart_map(dart_dev_t *d, uintptr_t iova, void *p, size_t size) {
    assert(!forbidden); maps++;
    /* A failing map may have published a prefix; no rollback is assumed. */
    if (kind == 2 && maps == edge) { d->failed = true; forbidden = true; return -1; }
    if (kind == 9) return dart_map_flags(d, iova, (void *)0x80000, size, 0);
    return 0;
}
bool dart_has_failed(dart_dev_t *d) { return d->failed; }
void dart_unmap(dart_dev_t *d, uintptr_t iova, size_t size) { assert(!forbidden); unmaps++; }
bool dart_shutdown_checked(dart_dev_t *d) { assert(!forbidden); return true; }
static void mock_free(void *p) { assert(!forbidden); frees++; }
static void ringbuffer_free(void *p) { mock_free(p); }
static void mdelay(unsigned ms) { assert(!forbidden); }
#define calloc test_calloc
#define free mock_free
#ifdef J700_CDC_PROXY
static struct usb_device_descriptor usb_cdc_device_descriptor, usb_cdc_ss_device_descriptor;
static u8 cdc_configuration_descriptor[64], usb_cdc_ss_configuration[128];
static size_t usb_cdc_ss_configuration_len;
static size_t usb_cdc_ss_config(const u8 *s, size_t n, u8 *d, size_t cap) {
    if (kind == 7) { forbidden = true; return 0; }
    return 32;
}
int usb_cdc_nump(u32 a, u32 b) {
    if (kind == 6) { forbidden = true; return -1; }
    return 4;
}
#endif
'''
    # Keep production DART bodies after their MMIO/page helpers and before constructor use.
    dart_insertion = harness.index("int dart_map(dart_dev_t")
    dart_helpers = """
static int dart_map_page(dart_dev_t *d, uintptr_t iova, uintptr_t paddr, u32 flags) {
    assert(!forbidden); return 0;
}
"""
    harness = harness[:dart_insertion] + dart_helpers + dart_functions + harness[dart_insertion:]
    harness += function(source, "static int usb_dwc3_command(")
    harness += function(source, "static int usb_dwc3_ep_command(")
    harness += function(source, "static int usb_dwc3_ep_configure(")
    harness += function(source, "dwc3_dev_t *usb_dwc3_init(")
    harness += function(source, "bool usb_dwc3_shutdown_failed(")
    proxy = (ROOT / "src/proxy.c").read_text()
    start = proxy.index("        case P_VECTOR:")
    vector = proxy[start:proxy.index("        case P_GL1_CALL:", start)]
    harness += r'''
typedef void generic_func(void);
enum { P_VECTOR };
static struct { generic_func *entry; u64 args[5]; bool restore_logo; } next_stage;
struct request { unsigned opcode; u64 args[6]; };
struct reply { u64 retval; };
static void usb_hpm_restore_irqs(unsigned force) { assert(!forbidden); }
static void iodev_console_flush(void) { assert(!forbidden); }
static int dispatch(struct request *request, struct reply *reply) {
    switch (request->opcode) {
'''
    harness += vector + "    }\n    return 0;\n}\n"
    harness += r'''
int main(int argc, char **argv) {
    assert(argc == 3); kind = atoi(argv[1]); edge = atoi(argv[2]);
    const struct mock_dart_params params = {.tlb_invalidate = dart_t8110_tlb_invalidate};
    dart_dev_t dart = {.failed = kind == 8, .regs = 0x20000, .device = 1, .params = &params};
    regs[DWC3_GSNPSID/4] = kind == 5 ? 0 : 0x33310000;
    dwc3_dev_t *dev = usb_dwc3_init(0x1000, &dart);
    if (!kind) {
        assert(dev && !usb_shutdown_failed && !usb_retained_dev);
        assert(allocs == 9 && maps == 4 && polls == 20 && status_reads == 19);
        assert(!frees && !unmaps && (regs[DWC3_DCTL/4] & DWC3_DCTL_RUN_STOP));
    } else {
        assert(!dev && usb_shutdown_failed && !frees && !unmaps);
        if (constructed) {
            assert(usb_retained_dev == constructed && constructed->failed && constructed->dma_unsafe);
            for (int i = 0; i < CDC_ACM_PIPE_MAX; i++) assert(!constructed->pipe[i].ready);
        }
        int before = writes + reads + maps + polls + allocs;
        assert(!usb_dwc3_init(0x1000, &dart));
        assert(before == writes + reads + maps + polls + allocs);
        struct request request = {P_VECTOR, {0x1234}};
        struct reply reply = {0}; next_stage.entry = (void *)1;
        assert(dispatch(&request, &reply) == 0 && reply.retval == (u64)-1 && !next_stage.entry);
        assert(!frees && !unmaps && before == writes + reads + maps + polls + allocs);
    }
    /* Host-owned fixture storage only; no production cleanup is invoked. */
    return 0;
}
'''
    binary = tmp_path / "constructor-fixture"
    flags = ["-DJ700_CDC_PROXY"] if j700 else []
    subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-function", "-Wno-unused-parameter", *flags,
                    "-I", str(ROOT / "src"), "-x", "c", "-", "-o", str(binary)],
                   input=harness, text=True, check=True)
    cases = [(0, 0), (5, 0), (8, 0)]
    cases += [(1, i) for i in range(1, 10)]
    cases += [(2, i) for i in range(1, 5)]
    cases += [(3, i) for i in range(1, 21)]
    cases += [(4, i) for i in range(1, 20)]
    cases += [(9, i) for i in range(1, 5)]
    if j700:
        cases += [(6, 0), (7, 0)]
    for kind, edge in cases:
        subprocess.run([str(binary), str(kind), str(edge)], check=True)
