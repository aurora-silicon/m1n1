"""Exercise queue lifetime on failures in the production ANS adoption path."""

from pathlib import Path
import subprocess


def test_adopted_queue_lifetime(tmp_path):
    source = (Path(__file__).resolve().parents[2] / "src/nvme.c").read_text()
    declarations = source[source.index("#define NVME_TIMEOUT"):source.index("static u64 nvme_read64_lo_hi")]
    init = source[source.index("bool nvme_init(void)"):source.index("void nvme_ensure_shutdown(void)")]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint8_t u8;
typedef uint16_t u16;
typedef uint32_t u32;
typedef uint64_t u64;
typedef int asc_dev_t;
typedef int rtkit_dev_t;
typedef int sart_dev_t;
#define static_assert _Static_assert
#define BIT(n) (1ULL << (n))
#define GENMASK(h, l) ((BIT((h) + 1) - 1) & ~(BIT(l) - 1))
#define FIELD_GET(mask, value) (((value) & (mask)) >> __builtin_ctzll(mask))
#define PMGR_DIE_ID GENMASK(31, 28)
#define USEC_PER_SEC 1000000
#define ADT_GETPROP(...) (-1)
static void *adt;
''' + declarations + r'''
static int failure, queue_frees, asc_frees, publications, commands;
static void *allocations[6];
static unsigned allocated;
static int asc;
static bool alloc_queue(struct nvme_queue *q)
{
    q->tcbs = allocations[allocated++] = calloc(1, sizeof(*q->tcbs));
    q->cmds = allocations[allocated++] = calloc(1, sizeof(*q->cmds));
    q->cqes = allocations[allocated++] = calloc(1, sizeof(*q->cqes));
    return true;
}
static void free_queue(struct nvme_queue *q) { queue_frees++; }
static int adt_path_offset_trace(void *a, const char *p, int *trace) { return 1; }
static const void *adt_getprop(void *a, int n, const char *p, u32 *len)
{
    *len = 0;
    return &asc;
}
static bool adt_is_compatible(void *a, int n, const char *p) { return true; }
static int adt_get_reg(void *a, int *path, const char *p, int i, u64 *base, u64 *size)
{
    *base = i == 3 ? 0x10000000 : 0x20000000;
    *size = failure == 6 && i == 9 ? NVME_BOOT_STATUS + 4 : 0x100000;
    return 0;
}
static asc_dev_t *asc_init(const char *path) { return &asc; }
static bool asc_cpu_running(asc_dev_t *a) { return failure != 1; }
static void asc_free(asc_dev_t *a) { asc_frees++; }
static u32 read32(u64 addr)
{
    return addr == nvme_base + NVME_BOOT_STATUS ? NVME_BOOT_STATUS_OK : 0;
}
static void write32(u64 addr, u32 value) {}
static void set32(u64 addr, u32 value) {}
static void write64_lo_hi(u64 addr, u64 value) { publications++; }
static void nvme_log_entry_state(bool running) {}
static sart_dev_t *sart_init(const char *path) { assert(0); return NULL; }
static void sart_free(sart_dev_t *s) { assert(!s); }
static rtkit_dev_t *rtkit_init(const char *name, asc_dev_t *a, void *d, void *i,
                              sart_dev_t *s, bool sram) { assert(0); return NULL; }
static bool rtkit_boot(rtkit_dev_t *r) { assert(0); return false; }
static bool rtkit_sleep(rtkit_dev_t *r) { assert(0); return false; }
static void rtkit_free(rtkit_dev_t *r) { assert(!r); }
static void pmgr_reset(u8 die, const char *name) { assert(0); }
static int poll32(u64 a, u32 mask, u32 value, unsigned timeout) { return 0; }
static bool nvme_ctrl_disable(void) { return failure != 2; }
static bool nvme_ctrl_enable(void) { return failure != 3; }
static bool nvme_ctrl_shutdown(void) { return true; }
static void nvme_poll_syslog(void) {}
static bool nvme_exec_command(struct nvme_queue *q, struct nvme_command *cmd, void *result)
{
    commands++;
    return !((failure == 4 && commands == 1) || (failure == 5 && commands == 2));
}
''' + init + r'''
int main(void)
{
    for (failure = 0; failure <= 6; failure++) {
        allocated = queue_frees = asc_frees = publications = commands = 0;
        nvme_initialized = false;
        nvme_adopt_live_session = true;
        assert(nvme_init() == (failure == 0));
        if (failure == 1) {
            /* Rejected before queue addresses reached the device. */
            assert(publications == 0 && queue_frees == 2 && asc_frees == 1);
        } else if (failure == 6) {
            assert(allocated == 0 && publications == 0);
        } else {
            assert(publications >= 2 && queue_frees == 0 && asc_frees == 0);
        }
        /* The host mock owns these allocations; no real DMA is running. */
        for (unsigned i = 0; i < allocated; i++)
            free(allocations[i]);
    }
    return 0;
}
'''
    binary = tmp_path / "nvme-adoption"
    subprocess.run(
        ["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
         "-Wno-unused-variable", "-Wno-format", "-x", "c", "-", "-o", str(binary)],
        input=harness, text=True, check=True,
    )
    subprocess.run([str(binary)], check=True)
