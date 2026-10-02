# J873g SMP bring-up

## Failure cases

Before hardware execution, reject an unexpected board, duplicate or incomplete
CPU descriptors, a mismatched PMGR2 register map, an invalid reset-vector
aperture, or a locked RVBAR pointing elsewhere. Each secondary needs its own
stack before entering C; a late core must never consume another core's stack.
Publish reset state and page tables before release. Establish coherent cached
access before reporting a core alive. Keep secondary startup off the shared
console and use WFE/SEV rather than an unqualified fast-IPI path.

Startup must be idempotent, bound its wait, and preserve allocations on timeout.
Do not retry a failed release on the same boot. CPU power-off and deep sleep are
not qualified: preserve running state and use a complete target reset before
loading another image.

## End-to-end qualification

RAM-load a hash-pinned candidate from a fresh recovery boot after verifying
J873g / Mac18,5, chip 0x8152, board 0x24. Record the candidate commit and SHA-256,
complete startup log, all twelve MPIDRs, native started/failure masks and repeated
startup behavior. Dispatch four-argument arithmetic work on every CPU, then run
concurrent atomic updates against ordinary cached RAM and verify the exact sum.
Capture independent proxy responsiveness during an asynchronous secondary worker.
Save a JSON receipt and source scripts alongside raw logs. A build or a started
bit alone is not an SMP pass.

The physical J873g candidate passed: twelve CPUs at EL2 with MMU and caches
enabled; 32 arithmetic calls per CPU; twelve million concurrent atomic updates
with an exact final sum and overlapping execution on every CPU; and 100 proxy
requests completed during a 50-million-iteration secondary worker. Startup was
idempotent with started mask `0xfbf`, failure mask zero, and boot CPU 6.
These checks qualify native dispatch, not Linux scheduling or CPU power-off.

Run `proxyclient/experiments/t8152-smp.py` with the action (`start`, `cores`,
`atomic`, `amp`), candidate `build/m1n1.bin`, its SHA-256, `--device` pointing to
the verified target console, and `--receipt result.json`. Keep the matching
`m1n1-raw.elf` beside the image; select the toolchain with `--nm` if needed.
Use a fresh receipt after each full reset. The harness checks loaded code
against the pinned image and uses native allocations without resetting the
live heap. Preserve its JSON receipt, console output, image and source revision.
