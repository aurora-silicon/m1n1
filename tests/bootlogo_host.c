/* SPDX-License-Identifier: MIT */
/*
 * Host check of the compressed built-in boot logos: each data/bootlogo_N.bin, compressed the way
 * the Makefile does, must inflate with m1n1's tinf to exactly the original pixels, and must be
 * refused by a buffer one byte smaller. Run by tests/run-bootlogo.sh.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "tinf/tinf.h"

static unsigned char *slurp(const char *path, unsigned int *len)
{
    FILE *f = fopen(path, "rb");
    unsigned char *buf = NULL;
    long n;

    if (!f || fseek(f, 0, SEEK_END) || (n = ftell(f)) < 0 || fseek(f, 0, SEEK_SET) ||
        !(buf = malloc(n ? n : 1)) || fread(buf, 1, n, f) != (size_t)n) {
        fprintf(stderr, "FAIL: cannot read %s\n", path);
        exit(1);
    }
    fclose(f);
    *len = n;
    return buf;
}

int main(int argc, char **argv)
{
    if (argc != 4) {
        fprintf(stderr, "usage: %s <size> <raw> <gz>\n", argv[0]);
        return 2;
    }
    unsigned int size = atoi(argv[1]), raw_len, gz_len;
    unsigned char *raw = slurp(argv[2], &raw_len), *gz = slurp(argv[3], &gz_len);
    unsigned int want = size * size * 4, len = want, src = gz_len;
    unsigned char *out = malloc(want);

    if (raw_len != want) {
        fprintf(stderr, "FAIL: %s is %u bytes, not %ux%ux4\n", argv[2], raw_len, size, size);
        return 1;
    }
    if (tinf_gzip_uncompress(out, &len, gz, &src) != TINF_OK || len != want ||
        memcmp(out, raw, want)) {
        fprintf(stderr, "FAIL: %s does not inflate to %s\n", argv[3], argv[2]);
        return 1;
    }
    len = want - 1;
    src = gz_len;
    if (tinf_gzip_uncompress(out, &len, gz, &src) == TINF_OK) {
        fprintf(stderr, "FAIL: %s inflated into a buffer one byte short\n", argv[3]);
        return 1;
    }
    printf("bootlogo %u: %u bytes -> %u compressed, inflates exactly\n", size, raw_len, gz_len);
    free(out);
    free(gz);
    free(raw);
    return 0;
}
