/* SPDX-License-Identifier: MIT */

#include "sha256.h"

static const uint32_t k[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

static uint32_t ror(uint32_t x, unsigned n)
{
    return (x >> n) | (x << (32 - n));
}

static void compress(struct sha256_ctx *ctx, const uint8_t *p)
{
    uint32_t w[64];

    for (int i = 0; i < 16; i++)
        w[i] = (uint32_t)p[4 * i] << 24 | (uint32_t)p[4 * i + 1] << 16 |
               (uint32_t)p[4 * i + 2] << 8 | p[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = ror(w[i - 15], 7) ^ ror(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = ror(w[i - 2], 17) ^ ror(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }

    uint32_t a = ctx->h[0], b = ctx->h[1], c = ctx->h[2], d = ctx->h[3];
    uint32_t e = ctx->h[4], f = ctx->h[5], g = ctx->h[6], h = ctx->h[7];

    for (int i = 0; i < 64; i++) {
        uint32_t t1 = h + (ror(e, 6) ^ ror(e, 11) ^ ror(e, 25)) + ((e & f) ^ (~e & g)) + k[i] + w[i];
        uint32_t t2 = (ror(a, 2) ^ ror(a, 13) ^ ror(a, 22)) + ((a & b) ^ (a & c) ^ (b & c));
        h = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }

    ctx->h[0] += a;
    ctx->h[1] += b;
    ctx->h[2] += c;
    ctx->h[3] += d;
    ctx->h[4] += e;
    ctx->h[5] += f;
    ctx->h[6] += g;
    ctx->h[7] += h;
}

void sha256_init(struct sha256_ctx *ctx)
{
    static const uint32_t init[8] = {0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
                                     0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19};

    for (int i = 0; i < 8; i++)
        ctx->h[i] = init[i];
    ctx->length = 0;
    ctx->fill = 0;
}

void sha256_update(struct sha256_ctx *ctx, const void *data, size_t size)
{
    const uint8_t *p = data;

    ctx->length += size;
    while (size) {
        size_t n = 64 - ctx->fill;

        if (n > size)
            n = size;
        if (ctx->fill == 0 && size >= 64) {
            compress(ctx, p);
            p += 64;
            size -= 64;
            continue;
        }
        for (size_t i = 0; i < n; i++)
            ctx->block[ctx->fill + i] = p[i];
        ctx->fill += n;
        p += n;
        size -= n;
        if (ctx->fill == 64) {
            compress(ctx, ctx->block);
            ctx->fill = 0;
        }
    }
}

void sha256_final(struct sha256_ctx *ctx, uint8_t digest[SHA256_DIGEST_SIZE])
{
    uint64_t bits = ctx->length * 8;
    static const uint8_t pad[64] = {0x80};
    uint8_t length[8];

    for (int i = 0; i < 8; i++)
        length[i] = bits >> (56 - 8 * i);

    size_t padding = (ctx->fill < 56) ? 56 - ctx->fill : 120 - ctx->fill;
    sha256_update(ctx, pad, padding);
    sha256_update(ctx, length, 8);

    for (int i = 0; i < 8; i++) {
        digest[4 * i] = ctx->h[i] >> 24;
        digest[4 * i + 1] = ctx->h[i] >> 16;
        digest[4 * i + 2] = ctx->h[i] >> 8;
        digest[4 * i + 3] = ctx->h[i];
    }
}
