/* SPDX-License-Identifier: MIT */

#ifndef GPU_HANDOFF_H
#define GPU_HANDOFF_H

#include "types.h"

/*
 * Build the AGX preboot initdata triple into a caller-supplied top-of-memory
 * reservation and stamp it with the identity of the generator arm that made
 * it.
 *
 * The reservation must be exactly GPU_HANDOFF_V1_RESERVATION_SIZE bytes at the
 * canonical derivation (see gpu_handoff.c); m1n1 refuses any other address so
 * a host/firmware disagreement is a loud refusal instead of blobs published
 * where nobody looks.
 *
 * Nothing is powered down and no GPU register is written. Any nonzero result
 * means nothing usable was published and the caller must not tell Mu the
 * regions exist.
 */
int gpu_handoff_init(u64 reservation_base, u64 reservation_size);

#endif
