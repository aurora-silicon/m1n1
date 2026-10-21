/* SPDX-License-Identifier: MIT */

#include "assert.h"
#include "exception.h"
#include "malloc.h"
#include "smc.h"
#include "string.h"
#include "types.h"
#include "utils.h"

#define SMC_READ_KEY         0x10
#define SMC_WRITE_KEY        0x11
#define SMC_GET_KEY_BY_INDEX 0x12
#define SMC_GET_KEY_INFO     0x13
#define SMC_INITIALIZE       0x17
#define SMC_NOTIFICATION     0x18
#define SMC_RW_KEY           0x20

#define SMC_MSG_TYPE GENMASK(7, 0)
#define SMC_MSG_ID   GENMASK(15, 12)

#define SMC_WRITE_KEY_SIZE GENMASK(23, 16)
#define SMC_WRITE_KEY_KEY  GENMASK(63, 32)

#define SMC_RESULT_RESULT GENMASK(7, 0)
#define SMC_RESULT_ID     GENMASK(15, 12)
#define SMC_RESULT_SIZE   GENMASK(31, 16)
#define SMC_RESULT_VALUE  GENMASK(63, 32)

#define SMC_NUM_IDS 16

#define SMC_ENDPOINT 0x20

struct smc_dev {
    asc_dev_t *asc;
    rtkit_dev_t *rtkit;

    void *shmem;
    u32 msgid;

    bool outstanding[SMC_NUM_IDS];
    u64 ret[SMC_NUM_IDS];
    bool adopted;
    bool init_done;
};

static void smc_handle_msg(smc_dev_t *smc, u64 msg)
{
    if (!smc->init_done) {
        smc->shmem = (void *)msg;
        smc->init_done = true;
    } else {
        u8 result = FIELD_GET(SMC_RESULT_RESULT, msg);
        u8 id = FIELD_GET(SMC_RESULT_ID, msg);
        if (result == SMC_NOTIFICATION) {
            printf("SMC: Notification: 0x%08lx\n", FIELD_GET(SMC_RESULT_VALUE, msg));
            return;
        }
        smc->outstanding[id] = false;
        smc->ret[id] = msg;
    }
}

#define SMC_TIMEOUT_USEC 3000000

/* Returns 1 when no message arrived before the deadline, so callers never hang. */
static int smc_work(smc_dev_t *smc)
{
    int ret;
    struct rtkit_message msg;
    u64 deadline = timeout_calculate(SMC_TIMEOUT_USEC);

    while ((ret = rtkit_recv(smc->rtkit, &msg)) == 0)
        if (timeout_expired(deadline))
            return 1;

    if (ret < 0) {
        printf("SMC: rtkit_recv failed!\n");
        return ret;
    }

    if (msg.ep != SMC_ENDPOINT) {
        printf("SMC: received message for unexpected endpoint 0x%02x\n", msg.ep);
        return 0;
    }

    printf("SMC: rx ep 0x%02x msg 0x%016lx\n", msg.ep, msg.msg);
    smc_handle_msg(smc, msg.msg);

    return 0;
}

static void smc_send(smc_dev_t *smc, u64 message)
{
    struct rtkit_message msg;

    msg.ep = SMC_ENDPOINT;
    msg.msg = message;

    rtkit_send(smc->rtkit, &msg);
}

static int smc_cmd(smc_dev_t *smc, u64 message)
{
    u8 id = smc->msgid++ & 0xF;
    assert(!smc->outstanding[id]);
    smc->outstanding[id] = true;

    message |= FIELD_PREP(SMC_MSG_ID, id);

    smc_send(smc, message);
    while (smc->outstanding[id]) {
        if (smc_work(smc)) {
            printf("SMC: smc_cmd[0x%x] got no reply\n", id);
            smc->outstanding[id] = false;
            return -1;
        }
    }

    u64 result = smc->ret[id];
    u32 ret = FIELD_GET(SMC_RESULT_RESULT, result);
    if (ret) {
        printf("SMC: smc_cmd[0x%x] failed: %u\n", id, ret);
        return ret;
    }

    return 0;
}

/* T8152 bring-up: the SMC keeps iBoot's shared buffer. Read an 8-byte key (its value lands in
 * that buffer) and diff the SMC SRAM window to find where it went. Diagnostic only. */
#define SMC_SRAM_SCAN_BASE 0x30de68000UL
#define SMC_SRAM_SCAN_SIZE 0xc000
/* Returns false when the word faults (partly readable SRAM pages). */
static bool smc_read_guarded(u64 addr, u32 *val)
{
    int before = exc_count;
    exc_guard = GUARD_SKIP | GUARD_SILENT;
    *val = read32(addr);
    exc_guard = GUARD_OFF;
    return exc_count == before;
}

/* The shared buffer found by the first session. A second session in the same m1n1 run reads the same pcIO value into it,
 * so the changed-word scan finds nothing and writes would fail (T8152: HDMI power opens the SMC before PCIe does). */
static void *smc_shmem_known;

static void smc_find_shmem(smc_dev_t *smc)
{
    if (smc_shmem_known) {
        smc->shmem = smc_shmem_known;
        printf("SMC: reusing shared buffer %lx\n", (u64)smc_shmem_known);
        return;
    }
    u32 words = SMC_SRAM_SCAN_SIZE / 4;
    u32 *before = malloc(SMC_SRAM_SCAN_SIZE);
    u8 *ok = malloc(words);
    if (!before || !ok) {
        printf("SMC: scan: no memory\n");
        free(before);
        free(ok);
        return;
    }
    u64 sram = SMC_SRAM_SCAN_BASE;
    u32 readable = 0;
    for (u32 i = 0; i < words; i++) {
        ok[i] = smc_read_guarded(sram + 4 * i, &before[i]);
        readable += ok[i];
    }
    printf("SMC: scan: %u of %u words readable\n", readable, words);

    u64 msg = FIELD_PREP(SMC_MSG_TYPE, SMC_READ_KEY) | FIELD_PREP(SMC_WRITE_KEY_SIZE, 8) |
              FIELD_PREP(SMC_WRITE_KEY_KEY, 0x7063494f);
    int ret = smc_cmd(smc, msg);
    printf("SMC: scan: read pcIO (8 bytes) -> %d\n", ret);

    int shown = 0;
    u64 hit = 0;
    for (u32 i = 0; i < words && shown < 32; i++) {
        u32 now;
        if (!ok[i] || !smc_read_guarded(sram + 4 * i, &now) || now == before[i])
            continue;
        printf("SMC: scan: %lx %08x -> %08x\n", sram + 4 * i, before[i], now);
        if (!hit)
            hit = sram + 4 * i;
        shown++;
    }
    printf("SMC: scan: %d changed words shown\n", shown);
    /* One changed word at a page start is iBoot's shared buffer (live 2026-10-19: 0x30de70000). */
    if (shown == 1 && !(hit & 0xfff)) {
        smc->shmem = (void *)hit;
        smc_shmem_known = smc->shmem;
        printf("SMC: using shared buffer %lx\n", hit);
        u32 plt[2] = {0};
        int r = smc_read_u64(smc, 0x52506c74, plt); /* "RPlt": 8-byte platform name */
        printf("SMC: RPlt -> %d: %08x %08x '%.8s'\n", r, plt[0], plt[1], (char *)plt);
    }
    free(before);
    free(ok);
}

void smc_shutdown(smc_dev_t *smc)
{
    /* An adopted SMC belongs to the running system: never quiesce it. */
    if (!smc->adopted)
        rtkit_quiesce(smc->rtkit);
    rtkit_free(smc->rtkit);
    asc_free(smc->asc);
    free(smc);
}

static smc_dev_t *smc_init_common(bool boot)
{
    smc_dev_t *smc = calloc(1, sizeof(smc_dev_t));
    if (!smc)
        return NULL;
    smc->adopted = !boot;

    smc->asc = asc_init("/arm-io/smc");
    if (!smc->asc) {
        printf("SMC: failed to initialize ASC\n");
        goto out_free;
    }

    smc->rtkit = rtkit_init("smc", smc->asc, NULL, NULL, NULL, true);
    if (!smc->rtkit) {
        printf("SMC: failed to initialize RTKit\n");
        goto out_asc;
    }

    /* Adopt mode talks to an SMC that iBoot already started: a fresh RTKit boot
     * restarts the SMC, which on T8152 also resets the USB-C ports. */
    if (boot && !rtkit_boot(smc->rtkit)) {
        printf("SMC: failed to boot RTKit\n");
        goto out_rtkit;
    }

    /* A running SMC already has endpoint 0x20 started. */
    if (boot && !rtkit_start_ep(smc->rtkit, SMC_ENDPOINT)) {
        printf("SMC: failed start SMC endpoint\n");
        goto out_rtkit;
    }

    u64 initialize =
        FIELD_PREP(SMC_MSG_TYPE, SMC_INITIALIZE) | FIELD_PREP(SMC_MSG_ID, smc->msgid++);

    smc_send(smc, initialize);

    while (!smc->init_done) {
        if (smc_work(smc)) {
            /* T8152: the SMC keeps iBoot's shared buffer and ignores a second INITIALIZE;
             * inline key reads (<= 4 bytes) still work, writes need the buffer. */
            printf("SMC: no INITIALIZE reply; read-only session\n");
            smc->init_done = true;
            smc_find_shmem(smc);
        }
    }

    return smc;

out_rtkit:
    rtkit_free(smc->rtkit);
out_asc:
    asc_free(smc->asc);
out_free:
    free(smc);
    return NULL;
}

smc_dev_t *smc_init(void)
{
    return smc_init_common(true);
}

smc_dev_t *smc_init_adopt(void)
{
    return smc_init_common(false);
}

/* len must be a multiple of 4: the T8152 shared buffer is SRAM mapped as device memory,
 * so it is filled with 32-bit stores, not memcpy. */
int smc_write(smc_dev_t *smc, u32 key, const u32 *words, u32 len)
{
    if (!smc->shmem) {
        printf("SMC: write of key %08x needs the shared buffer (read-only session)\n", key);
        return -1;
    }
    for (u32 i = 0; i < len / 4; i++)
        write32((u64)smc->shmem + 4 * i, words[i]);
    u64 msg = FIELD_PREP(SMC_MSG_TYPE, SMC_WRITE_KEY);
    msg |= FIELD_PREP(SMC_WRITE_KEY_SIZE, len);
    msg |= FIELD_PREP(SMC_WRITE_KEY_KEY, key);

    return smc_cmd(smc, msg);
}

int smc_write_u32(smc_dev_t *smc, u32 key, u32 value)
{
    return smc_write(smc, key, &value, sizeof(value));
}

/* Reads a key of up to 8 bytes through the shared buffer (values over 4 bytes are not inline). */
int smc_read_u64(smc_dev_t *smc, u32 key, u32 out[2])
{
    if (!smc->shmem)
        return -1;
    u64 msg = FIELD_PREP(SMC_MSG_TYPE, SMC_READ_KEY) | FIELD_PREP(SMC_WRITE_KEY_SIZE, 8) |
              FIELD_PREP(SMC_WRITE_KEY_KEY, key);
    int ret = smc_cmd(smc, msg);
    out[0] = read32((u64)smc->shmem);
    out[1] = read32((u64)smc->shmem + 4);
    return ret;
}

int smc_read_u32(smc_dev_t *smc, u32 key, u32 *value)
{
    u8 id = smc->msgid & 0xF;
    u64 msg = FIELD_PREP(SMC_MSG_TYPE, SMC_READ_KEY);
    msg |= FIELD_PREP(SMC_WRITE_KEY_SIZE, sizeof(*value));
    msg |= FIELD_PREP(SMC_WRITE_KEY_KEY, key);

    int ret = smc_cmd(smc, msg);
    if (ret)
        return ret;
    *value = FIELD_GET(SMC_RESULT_VALUE, smc->ret[id]);
    return 0;
}
