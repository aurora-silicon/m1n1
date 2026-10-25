#!/bin/sh
# Host check of the compressed built-in boot logos (Makefile bootlogo rules, fb.c builtin_logo()).
#
#   sh tests/run-bootlogo.sh
#
# Compresses data/bootlogo_{48,128,256}.bin as the Makefile does and checks that m1n1's tinf
# inflates each to exactly the original pixels, and refuses a buffer one byte too small.
set -eu
cd "$(dirname "$0")/.."
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT HUP INT TERM
${CC:-cc} -std=gnu11 -O1 -g -Wall -Wextra -fsanitize=address,undefined -fno-omit-frame-pointer \
    -fno-sanitize-recover=undefined -Isrc tests/bootlogo_host.c src/tinf/tinflate.c \
    src/tinf/tinfgzip.c src/tinf/crc32.c -o "$tmp/bootlogo-host"
for n in 48 128 256; do
    gzip -9 -n -c "data/bootlogo_$n.bin" > "$tmp/bootlogo_$n.bin.gz"
    "$tmp/bootlogo-host" "$n" "data/bootlogo_$n.bin" "$tmp/bootlogo_$n.bin.gz"
done
