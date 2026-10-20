/* SPDX-License-Identifier: MIT */

#include "g18g_identity.h"
#include "sha256.h"

#include <stdbool.h>
#include <string.h>

struct tlv {
    char name[5];
    uint8_t length;
};

/*
 * The boot TLV run in _rtk_patchbay: display name and value length. It is identical
 * in both B-revision images. The bytes in memory hold the name reversed (STKG is
 * stored as 'GKTS') and a little-endian u32 length.
 */
static const struct tlv sequence[] = {
    {"STKG", 8}, {"GxPB", 8}, {"GxPS", 8}, {"SLID", 8}, {"ASRS", 8}, {"MCFG", 8}, {"CSSS", 8},
    {"v8Fq", 4}, {"DSRL", 1}, {"DSVS", 8}, {"DSCL", 8}, {"RTSZ", 4}, {"PTPB", 8}, {"IGNT", 1},
    {"SNeo", 1}, {"CBES", 8}, {"PACE", 8}, {"PTVB", 8}, {"T1Ps", 8}, {"GptB", 8}, {"GptS", 8},
    {"PplB", 8}, {"Gx2S", 8}, {"PTPS", 8}, {"DSZS", 8}, {"DSZL", 8}, {"TUNS", 8}, {"TUNZ", 4},
    {"RTTO", 8}, {"WrIR", 1}, {"SOC_", 4}, {"SOCR", 4}, {"CpAd", 8}, {"WrAd", 8}, {"DEVf", 1},
    {"pFLG", 8}, {"IOBA", 8}, {"IOSZ", 4}, {"RTLL", 1}, {"McRA", 4},
};

#define N_TLV (sizeof(sequence) / sizeof(sequence[0]))

/* Random on every boot, filled by iBoot with boot addresses and SoC identity, or boot-filled. */
static const char *const excluded[] = {"STKG", "PACE", "PTPB", "PTVB", "PplB", "Gx2S", "PTPS",
                                       "SOC_", "SOCR", "CpAd", "WrAd", "SLID", "pFLG"};

/* Three u32 words of the loader record just before the 'dseg' segment record. */
static const size_t record_offsets[] = {0x224, 0x23c, 0x240};
#define RECORD_WORD 4

struct role_data {
    struct g18g_role_profile profile;
    size_t patchbay_start;
    size_t patchbay_size;
    const char *canonical_sha256;
};

static const struct role_data roles[G18G_ROLES] = {
    [G18G_GFX] = {{"armfw_g18g-b000", "B958E8AE-6B33-3465-A4E2-F644FC16F9CA", 0x50000},
                  0x4a62c,
                  0x23a,
                  "d5b943fe7d9a7bd20e684c6f237d63e7fcd5610f53f5e4a37be98db08e9075b1"},
    [G18G_GFX1] = {{"armfw1_g18g-b010", "6AC50A03-443B-3A6F-A447-B2ECFC12AB1A", 0x54000},
                   0x4e57c,
                   0x23a,
                   "4304b2a00ebbcc2196af44bbf8b889791a39d62c7a11370e779115e9f1a62c62"},
};

const struct g18g_role_profile *g18g_profile(enum g18g_role role)
{
    if ((unsigned)role >= G18G_ROLES)
        return NULL;
    return &roles[role].profile;
}

void g18g_expected_loader(enum g18g_role role, uint64_t shared_base, uint64_t shared_size,
                          uint64_t handoff_base, uint64_t l2_base, uint64_t rtkit_private_vm_base,
                          uint64_t wrapper, uint64_t chip_id, struct g18g_loader_expect *out)
{
    uint64_t half = shared_size / 2;

    out->ptpb = shared_base + (uint64_t)role * half;
    out->ptps = half;
    out->pplb = handoff_base;
    out->gx2s = l2_base;
    out->ptvb = rtkit_private_vm_base;
    out->wrad = wrapper;
    out->soc = chip_id;
}

static bool is_excluded(const char *name)
{
    for (size_t i = 0; i < sizeof(excluded) / sizeof(excluded[0]); i++)
        if (!memcmp(name, excluded[i], 4))
            return true;
    return false;
}

static uint64_t le(const uint8_t *p, size_t length)
{
    uint64_t v = 0;

    for (size_t i = length; i-- > 0;)
        v = v << 8 | p[i];
    return v;
}

/* Verify the TLV run exactly and record each TLV's offset. */
static bool check_layout(const struct role_data *r, const uint8_t *text, size_t offsets[N_TLV])
{
    size_t offset = r->patchbay_start;

    for (size_t i = 0; i < N_TLV; i++) {
        const uint8_t *header = text + offset;

        if (header[0] != (uint8_t)sequence[i].name[3] || header[1] != (uint8_t)sequence[i].name[2] ||
            header[2] != (uint8_t)sequence[i].name[1] || header[3] != (uint8_t)sequence[i].name[0] ||
            le(header + 4, 4) != sequence[i].length)
            return false;
        offsets[i] = offset;
        offset += 8 + sequence[i].length;
    }
    return offset == r->patchbay_start + r->patchbay_size;
}

static void hex(const uint8_t *in, size_t n, char *out)
{
    static const char digits[] = "0123456789abcdef";

    for (size_t i = 0; i < n; i++) {
        out[2 * i] = digits[in[i] >> 4];
        out[2 * i + 1] = digits[in[i] & 15];
    }
    out[2 * n] = 0;
}

/* SHA-256 of the text with every excluded value zeroed, without copying the text. */
static void canonical_digest(const uint8_t *text, size_t size, const size_t offsets[N_TLV],
                             uint8_t digest[SHA256_DIGEST_SIZE])
{
    static const uint8_t zeros[8];
    struct sha256_ctx ctx;
    size_t pos = 0;

    sha256_init(&ctx);

    /* Record words sit below the patchbay, so they come first in offset order. */
    for (size_t i = 0; i < sizeof(record_offsets) / sizeof(record_offsets[0]); i++) {
        sha256_update(&ctx, text + pos, record_offsets[i] - pos);
        sha256_update(&ctx, zeros, RECORD_WORD);
        pos = record_offsets[i] + RECORD_WORD;
    }
    for (size_t i = 0; i < N_TLV; i++) {
        if (!is_excluded(sequence[i].name))
            continue;
        size_t value = offsets[i] + 8;

        sha256_update(&ctx, text + pos, value - pos);
        sha256_update(&ctx, zeros, sequence[i].length);
        pos = value + sequence[i].length;
    }
    sha256_update(&ctx, text + pos, size - pos);
    sha256_final(&ctx, digest);
}

int g18g_qualify_text(enum g18g_role role, const uint8_t *text, size_t size,
                      const struct g18g_loader_expect *want, char *bad_tlv)
{
    size_t offsets[N_TLV];
    uint8_t digest[SHA256_DIGEST_SIZE];
    char digest_hex[2 * SHA256_DIGEST_SIZE + 1];

    if ((unsigned)role >= G18G_ROLES)
        return G18G_BAD_ROLE;
    const struct role_data *r = &roles[role];

    if (size != r->profile.text_size)
        return G18G_BAD_SIZE;
    if (!check_layout(r, text, offsets))
        return G18G_BAD_LAYOUT;

    canonical_digest(text, size, offsets, digest);
    hex(digest, sizeof(digest), digest_hex);
    if (strcmp(digest_hex, r->canonical_sha256))
        return G18G_BAD_DIGEST;

    if (!want)
        return G18G_OK;

    const struct {
        const char *name;
        uint64_t value;
    } checks[] = {
        {"PTPB", want->ptpb}, {"PTPS", want->ptps}, {"PplB", want->pplb}, {"Gx2S", want->gx2s},
        {"PTVB", want->ptvb}, {"WrAd", want->wrad}, {"SOC_", want->soc},
    };

    for (size_t c = 0; c < sizeof(checks) / sizeof(checks[0]); c++)
        for (size_t i = 0; i < N_TLV; i++) {
            if (strcmp(sequence[i].name, checks[c].name))
                continue;
            if (le(text + offsets[i] + 8, sequence[i].length) != checks[c].value) {
                if (bad_tlv)
                    memcpy(bad_tlv, checks[c].name, 5);
                return G18G_BAD_LOADER_VALUE;
            }
        }
    return G18G_OK;
}

const char *g18g_result_string(int result)
{
    switch (result) {
        case G18G_OK:
            return "ok";
        case G18G_BAD_ROLE:
            return "unknown firmware role";
        case G18G_BAD_SIZE:
            return "loaded TEXT size changed";
        case G18G_BAD_LAYOUT:
            return "boot TLV tag or length changed";
        case G18G_BAD_DIGEST:
            return "unrecognized firmware TEXT identity";
        case G18G_BAD_LOADER_VALUE:
            return "loader TLV differs from the ADT prediction";
        default:
            return "unknown";
    }
}
