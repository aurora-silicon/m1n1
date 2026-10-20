/* SPDX-License-Identifier: MIT */

#ifndef SHA256_H
#define SHA256_H

#include <stddef.h>
#include <stdint.h>

#define SHA256_DIGEST_SIZE 32

struct sha256_ctx {
    uint32_t h[8];
    uint64_t length;
    uint8_t block[64];
    size_t fill;
};

void sha256_init(struct sha256_ctx *ctx);
void sha256_update(struct sha256_ctx *ctx, const void *data, size_t size);
void sha256_final(struct sha256_ctx *ctx, uint8_t digest[SHA256_DIGEST_SIZE]);

#endif
