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
per-board table in `kboot.c`. The DT ABI is the same with a `j613` prefix:
`apple,firmware-uuid` and `memory-region` links to every `iop-dcp-nub`
segment on the `dcp` alias, `apple,dcp-os-log` on the `__OS_LOG` segment
(remap == phys), and, only when the DT opts in with `apple,j613-native-piodma`
(and `apple,j613-native-scanout`), `apple,j613-inherited-mappings` on
`disp0_piodma` (and `disp0`). Firmware UUID 90F849E1-B422-367E-B389-50246F8DEC47
(os-fw 14.7) is the only one allowed to publish inherited scanout mappings.
The disp0 DART block (0x28d304000, SID0 scanout, SID4 PIODMA) matches J514S.

Differences from J514S, because J613 is not yet qualified on hardware:
- The SID0/SID4 split of the four scanout segments is unknown. Instead of the
  exact J514S 3/1 counts, each stream must map at least one segment, every
  scanout segment must be mapped by one of them, and every inherited page must
  still match its ADT segment. The read-only walk result is always logged
  (`DISP0 SIDn maps ... mask`), even without the opt-in, to qualify the split.
- A rejected handoff never fails the boot. The handoff and the PIODMA/scanout
  publication each run on a copy of the FDT and are committed whole or not at
  all. On rejection, or with a DT that has no `dcp` alias, every DCP segment is
  still reserved `no-map` (unlinked), because the running DCP writes its
  `__OS_LOG` inside RAM otherwise given to the OS.

Host validation: `tests/kboot/test_j613_dcp_handoff.py` (real libfdt and the
production kboot code; `J613_ADT` / `J613_DCP_DTSI` select a saved ADT and the
real board dtsi). Not yet run on hardware.
