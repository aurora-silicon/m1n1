/* SPDX-License-Identifier: MIT */

#include "assert.h"
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

// READ_KEY lays its size and key out in the same places WRITE_KEY does; named
// separately so the read path does not read as if it were writing.
#define SMC_READ_KEY_SIZE GENMASK(23, 16)
#define SMC_READ_KEY_KEY  GENMASK(63, 32)

// A reply carries payloads of up to four bytes inline in SMC_RESULT_VALUE.
// Anything larger is left in the shared buffer.
#define SMC_MAX_INLINE_SIZE 4

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
};

static void smc_handle_msg(smc_dev_t *smc, u64 msg)
{
    if (!smc->shmem)
        smc->shmem = (void *)msg;
    else {
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

static int smc_work(smc_dev_t *smc)
{
    int ret;
    struct rtkit_message msg;

    while ((ret = rtkit_recv(smc->rtkit, &msg)) == 0)
        ;

    if (ret < 0) {
        printf("SMC: rtkit_recv failed!\n");
        return ret;
    }

    if (msg.ep != SMC_ENDPOINT) {
        printf("SMC: received message for unexpected endpoint 0x%02x\n", msg.ep);
        return 0;
    }

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

// Issue a command and hand back the raw reply. Callers that only care whether it
// succeeded use smc_cmd(); the read path needs the reply itself, because that is
// where a small payload arrives.
static int smc_cmd_result(smc_dev_t *smc, u64 message, u64 *result)
{
    u8 id = smc->msgid++ & 0xF;
    assert(!smc->outstanding[id]);
    smc->outstanding[id] = true;

    message |= FIELD_PREP(SMC_MSG_ID, id);

    smc_send(smc, message);
    while (smc->outstanding[id])
        smc_work(smc);

    u64 reply = smc->ret[id];
    u32 ret = FIELD_GET(SMC_RESULT_RESULT, reply);
    if (ret) {
        printf("SMC: smc_cmd[0x%x] failed: %u\n", id, ret);
        return ret;
    }

    if (result)
        *result = reply;

    return 0;
}

static int smc_cmd(smc_dev_t *smc, u64 message)
{
    return smc_cmd_result(smc, message, NULL);
}

void smc_shutdown(smc_dev_t *smc)
{
    rtkit_quiesce(smc->rtkit);
    rtkit_free(smc->rtkit);
    asc_free(smc->asc);
    free(smc);
}

smc_dev_t *smc_init(void)
{
    smc_dev_t *smc = calloc(1, sizeof(smc_dev_t));
    if (!smc)
        return NULL;

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

    if (!rtkit_boot(smc->rtkit)) {
        printf("SMC: failed to boot RTKit\n");
        goto out_rtkit;
    }

    if (!rtkit_start_ep(smc->rtkit, SMC_ENDPOINT)) {
        printf("SMC: failed start SMC endpoint\n");
        goto out_rtkit;
    }

    u64 initialize =
        FIELD_PREP(SMC_MSG_TYPE, SMC_INITIALIZE) | FIELD_PREP(SMC_MSG_ID, smc->msgid++);

    smc_send(smc, initialize);

    while (!smc->shmem) {
        int ret = smc_work(smc);
        if (ret < 0)
            goto out_rtkit;
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

int smc_read(smc_dev_t *smc, u32 key, void *buf, size_t size)
{
    if (!buf || !size || size > 0xff)
        return -1;

    u64 result = 0;
    u64 msg = FIELD_PREP(SMC_MSG_TYPE, SMC_READ_KEY);
    msg |= FIELD_PREP(SMC_READ_KEY_SIZE, size);
    msg |= FIELD_PREP(SMC_READ_KEY_KEY, key);

    int ret = smc_cmd_result(smc, msg, &result);
    if (ret)
        return ret;

    // The SMC reports what it actually returned, which need not be what was
    // asked for -- a key with a different width answers with its own size.
    u32 got = FIELD_GET(SMC_RESULT_SIZE, result);
    if (got != size) {
        printf("SMC: read key 0x%08x: asked %zu bytes, got %u\n", key, size, got);
        return -1;
    }

    if (size <= SMC_MAX_INLINE_SIZE) {
        u32 value = FIELD_GET(SMC_RESULT_VALUE, result);
        memcpy(buf, &value, size);
    } else {
        memcpy(buf, smc->shmem, size);
    }

    return 0;
}

int smc_read_u32(smc_dev_t *smc, u32 key, u32 *value)
{
    return smc_read(smc, key, value, sizeof(*value));
}

// Ask the SMC how wide a key is and what type it carries. Needed before a value
// can be interpreted: SMC keys are typed (temperatures are commonly "flt " or
// a fixed-point "ioft"), and the type is not guessable from the key name.
int smc_get_key_info(smc_dev_t *smc, u32 key, u8 *size, u32 *type)
{
    u64 result = 0;
    u64 msg = FIELD_PREP(SMC_MSG_TYPE, SMC_GET_KEY_INFO);
    msg |= FIELD_PREP(SMC_READ_KEY_KEY, key);

    int ret = smc_cmd_result(smc, msg, &result);
    if (ret)
        return ret;

    // Key info lands in the shared buffer: length first, then the type FourCC
    // as four characters in order. Repack it MSB-first so a type compares
    // against SMC_KEY("ui32") the same way a key compares against
    // SMC_KEY("TB0T") -- a straight memcpy would byte-reverse it.
    const u8 *info = smc->shmem;
    if (size)
        *size = info[0];
    if (type)
        *type = ((u32)info[1] << 24) | ((u32)info[2] << 16) | ((u32)info[3] << 8) | info[4];

    return 0;
}

//
// m1n1 links with --gc-sections, which drops anything nothing calls. smc_init()
// and smc_write_u32() survive because dcp.c and pcie.c use them; the read path
// has no in-tree caller yet -- it is driven from the proxy by address during
// bring-up, and reading a temperature is not something to do unbidden on the
// boot path, since smc_init() boots RTKit and smc_cmd() blocks.
//
// Retain the symbols explicitly rather than inventing a fake caller.
//
__attribute__((used, retain)) static void *const smc_retained_for_proxy[] = {
    (void *)smc_read,
    (void *)smc_read_u32,
    (void *)smc_get_key_info,
};

int smc_write_u32(smc_dev_t *smc, u32 key, u32 value)
{
    memcpy(smc->shmem, &value, sizeof(value));
    u64 msg = FIELD_PREP(SMC_MSG_TYPE, SMC_WRITE_KEY);
    msg |= FIELD_PREP(SMC_WRITE_KEY_SIZE, sizeof(value));
    msg |= FIELD_PREP(SMC_WRITE_KEY_KEY, key);

    return smc_cmd(smc, msg);
}
