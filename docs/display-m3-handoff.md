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

# J613 internal display handoff

J613 (MacBook Air 13" M3, T8122) shares the J514S internal handoff through a
per-board table in `kboot.c`, but publishes the ABI of the in-tree drm/apple
14.x IOMFB path (J613 board record) and its display gate, described by the
kernel's `t8122-j613-dcp.dtsi`:

- always, on the `dcp` alias: `apple,firmware-uuid` (ADT `iop-dcp-nub`
  uuid), `apple,firmware-version`, `apple,firmware-compat` (<14 7 0> for
  os-fw 14.7) and a `no-map` `memory-region` per `iop-dcp-nub` segment with
  `iommu-addresses = <&dcp remap size>`, or `apple,dcp-os-log` on `__OS_LOG`
  (remap == phys). `apple,notch-height` comes from `dt_set_fb()`.
- only if the DT has the `apple,t8122-display-subsystem` node (`disp0`
  alias; the kernel's `apple_t8122_display.enable=1` is the real opt-in):
  a `framebuffer` `memory-region` (ADT `/vram`, no-map, compatible
  `framebuffer`, no DART tuple) first on the display subsystem, the SID0
  segments linked to it and the SID4 segment to the `piodma` child, all named,
  and then `apple,t8122-handoff = <1>` on the DCP, the display subsystem and
  PIODMA, last. No `status` is changed: the display gate enables the nodes.

The display part is published, as one FDT transaction, only if the firmware
UUID is 90F849E1-B422-367E-B389-50246F8DEC47; the read-only walk of the
locked disp0 DART (0x28d304000) finds every inherited page inside its ADT
segment at the ADT physical address, SID0 mapping exactly segments 4 and 6
(0x50), SID4 exactly segment 5 (0x20), and no stream the 16 MB segment 3
(0x08, linked to the DCP only); `apple,firmware-compat` is 14.7.0 and
`apple,notch-height` 64; the display subsystem and PIODMA are streams 0 and 4
of that DART, PIODMA is the DCP's available `piodma` child, and the DCP,
display subsystem and disp0 DART are still disabled with no memory-region;
and the boot framebuffer lies inside `/vram`. Otherwise no marker is set and
the kernel does not take the display over. The walk is always logged.

A rejected DCP handoff never fails the boot: the original DT is kept and
every DCP segment is still reserved `no-map` (unlinked), because the running
DCP writes its `__OS_LOG` inside RAM otherwise given to the OS.

Host validation: `tests/kboot/test_j613_dcp_handoff.py` (real libfdt and the
production kboot code; `J613_ADT` selects a saved ADT, `J613_DCP_DTSI` the
kernel's dtsi and `J613_DTB` a complete t8122-j613.dtb). Not yet run on
hardware.

# T8122 GPU firmware handoff

`dt_set_gpu()` hands T8122 to `dt_set_gpu_t8122()` in `kboot_gpu.c` instead
of reporting an unsupported chip. iBoot preloads the GFX ASC firmware and
leaves its UAT tables in RAM, and nothing else reserved that memory on T8122.
Every T8122 boot now reserves, `no-map`, the four `/arm-io/sgx` regions
(`gpu-region`, `gfx-shared-region`, `gfx-handoff`, `gfx-shared-l2-region`)
in the `uat-ttbs`, `uat-pagetables`, `uat-handoff` and `uat-pagetables-l2`
nodes (filling the DT's placeholders, or creating them), and the
`/arm-io/gfx-asc` `__TEXT`/`__DATA` segments as `asc-firmware@<phys>`.

When the ADT is consistent (gfx-asc and iop-gfx-nub `segment-ranges` equal,
`__TEXT;__DATA`, `pre-loaded` 1, phys == remap, 16K-aligned, flags 1/0,
`__TEXT` at VA 0xfffffc0000000000 with `__DATA` right after it, all six ranges
disjoint and inside DRAM), nothing else in `/reserved-memory` or
`/memreserve/` overlaps them, and the DT has an `apple,agx-t8122` node (the
`gpu` alias, or the only such node), the handoff links the six regions as
`ttbs pagetables handoff shared-l2 fw-text fw-data` (other `memory-region`
entries are kept) and sets `apple,m3-handoff-version = <1>`,
`apple,firmware-segment-vas` (two u64) and `apple,firmware-segment-flags`.
That is the ABI of the M3 runtime (drm/asahi `m3_board.rs`) and of
`make-gpu-overlay.py`. It is committed as a whole FDT copy or not at all.

The GPU node is set to `disabled` unless the root is `apple,j613` and the
node has `apple,j613-native-gpu`, in which case it is set to `okay`. If the
handoff is refused, the node is set to `disabled` and every usable range is
still reserved, unlinked, one transaction per range. Ranges that are already
covered, or that partially overlap another reservation, are logged and
skipped. This never fails the boot.

Host validation: `tests/kboot/test_t8122_gpu_handoff.py` (real libfdt and
the production code; `J613_ADT` / `J613_GPU_DTSI` select a saved ADT and a
real dtsi). Not yet run on hardware.
