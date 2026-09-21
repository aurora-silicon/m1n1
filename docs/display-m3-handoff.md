# J514S external display reservation handoff

Adapted from the sibling M4 `kboot_display_m4.c`. Original ASC reservation
work by Martin Povišer, external iteration by Janne Grunau, and M4 validation
work by Eryk Wieliczko are retained in the source attribution.

Only J514S/T6030 opts in using `apple,j514s-dcpext-reserve`. The captured ADT
identifies DCPEXT0/1 at 0x2d2c00000/0x2d6c00000, ASC DART SID5, scanout SID0
and PIODMA SID4. All consumers must be disabled. Memory-region references
protect firmware and page tables; segment descriptors remain metadata, never
guessed IOMMU mappings. Version1 is published only when both requested
controllers validate in a transactional FDT copy. Zero/zero ISP and GPU
placeholders claim no memory and remain for their later handoffs.

The connected macOS capture uses DCPEXT0 port0 and ATC3 for native HDMI.
This producer neither powers the route nor starts firmware. No MMIO access
occurs. The working internal handoff runs first and remains unchanged.

Host validation: workspace tools/m3-dcp/test-handoff.py, saved J514S ADT,
actual C producer and libfdt. Shared TEXT, idempotence, optional nodes,
rollback, 29 invalid-input cases, chainload conflicts and real board DT pass.
m1n1 cross-build passes. Hardware qualification is separate. Pre-existing
GPU handoff work remains in the working tree and build, outside this commit.
