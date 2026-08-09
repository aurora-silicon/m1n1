/* SPDX-License-Identifier: MIT */

#ifndef GPU_INITDATA_H
#define GPU_INITDATA_H

#include "types.h"

/*
 * Identity of the initdata triple that rust_fill_gpu_initdata_stamped() just
 * produced, reported by the generator itself rather than re-derived by the
 * caller. Mirrors `GpuInitdataIdent` in rust/src/gpu/initdata.rs; the two must
 * be changed together.
 */
struct gpu_initdata_ident {
    u32 chip_id;
    u32 gpu_gen;
    u32 gpu_variant;
    u32 gpu_core;
    u32 gpu_rev_id;
    u32 num_cores;
    u32 compat_maj;
    u32 compat_min;
    u32 data_a_size;
    u32 data_b_size;
    u32 globals_size;
    u32 builder_arm;
};

/*
 * Gather this machine's ADT power/perf tables and run the AGX initdata
 * generator over three caller-owned buffers. Each buffer is zeroed over its
 * whole capacity first, so any tail beyond the payload is deterministic.
 *
 * Fails (and writes nothing) if the generated payload would not fit, if the
 * chip has no power model, or if the generator has no arm for this
 * (gpu_gen, gpu_variant, firmware) combination.
 *
 * Returns 0 on success, -1 otherwise. `ident` is zeroed on entry, so a failed
 * call leaves builder_arm == 0.
 */
int gpu_initdata_generate(void *data_a, size_t data_a_capacity, void *data_b,
                          size_t data_b_capacity, void *globals, size_t globals_capacity,
                          struct gpu_initdata_ident *ident);

#endif
