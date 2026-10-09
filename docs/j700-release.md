# J700 Stage 1 and Stage 2 release recipe

The release uses a single source commit and two remote build outputs. Build
both images in the KIS carrier flavour with the linked Aurora Silicon logo.
The `LOGO=` Makefile option appends a payload and must not be used for the
installed Stage 1.

```sh
BB=<remote-build-script>
WT=<CLEAN_M1N1_WORKTREE>
$BB m1n1 "$WT" <release>-stage1 RELEASE=1 CHAINLOADING=1 T8140_KIS_PROXY=1 BUILTIN_LOGO=aurora
$BB m1n1 "$WT" <release>-stage2 RELEASE=1 CHAINLOADING=1 T8140_KIS_PROXY=1 BUILTIN_LOGO=aurora
python3 tools/package_j700.py \
  --stage1-out <build-output>/<release>-stage1 \
  --stage2-out <build-output>/<release>-stage2 \
  --output <release-dir>
```

The packaging tool requires identical source commits and tags, clean build
inputs, `RELEASE`, `CHAINLOADING`, `T8140_KIS_PROXY`, `USE_DEBUG_USB` and
`BUILTIN_LOGO=aurora`, no `LOGO=` build variable, one Stage 1 config marker, and
`STACKBOT` as each image's final eight bytes. It ships each binary with the raw
ELF from the same build output folder, source/build metadata, licences,
`fatfs/PROVENANCE.md` with the pinned revision and local changes, and
`SHA256SUMS`. The Aurora Silicon logo terms are in
`3rdparty_licenses/LICENSE.AURORA-LOGO`.

## Fill the installed Stage 1

The release Stage 1 is generic and proxy-only until the installer fills its
versioned block. Use the real ESP PARTUUID privately; it identifies a volume.
The installer should use a candidate Stage 2 path for the RAM-vector test and
the dedicated `aurora/stage2.bin` path only for the final installation.

```sh
python3 tools/fill_stage1_config.py \
  --input <release-dir>/m1n1-stage1.bin \
  --output <release-dir>/m1n1-stage1-filled.bin \
  --esp-partuuid '<ESP PARTUUID>' \
  --stage2-path aurora/stage2.bin --window-ms 15000
```

The tool requires a canonical lowercase UUID, a relative FAT path and a window
from 0 to 99999 ms. It updates only the in-image block and retains the exact
image length and `STACKBOT` tail. The filled raw image contains one
`;aurora/stage2.bin` marker (or the selected path). An empty block stays proxy-only. A malformed
block is ignored by m1n1, which falls back to the proxy.

## Compose the ESP Stage 2

For a persistent ESP Stage 2, build a separate
flavour with `RELEASE=1 CHAINLOADING=1 J700_ESP_STAGE2=1 BUILTIN_LOGO=aurora`.
`CHAINLOADING=1` supplies the `boot=` storage loader; `J700_ESP_STAGE2=1`
omits the Stage 1 config and ESP chainload marker. Keep the KIS Stage 2 above
for tethered tests and the release package.

`aurora/stage2.bin` on the ESP is the complete m1n1 Stage 2 image followed by
newline-terminated variables. For the three-file Linux route, append the
following lines to a copy of `m1n1-stage2.bin`:

```sh
cp m1n1-stage2.bin stage2-for-esp.bin
printf '%s\n' 'boot=<ESP PARTUUID>;aurora/<kernel>/Image;aurora/<kernel>/t8140-j700.dtb;aurora/<kernel>/initramfs.cpio.gz' >> stage2-for-esp.bin
printf '%s\n' 'chosen.bootargs=<kernel command line>' >> stage2-for-esp.bin
```

Do **not** append a four-byte zero terminator to the ESP file: Stage 1 appends
its own chosen variables and `nvme.adopt=live-rtkit-v1`, then adds the terminator
in RAM. A premature terminator prevents Stage 2 from seeing those variables.
`boot=` uses exactly four nonempty semicolon-separated fields. The installer
must put Image or Image.gz, the matching DTB, and a gzip or cpio initramfs at
the named paths. Do not mix a kernel Image and modules from different build
output folders.

For an EFI boot manager such as U-Boot, use `-` as the fourth field:

```sh
printf '%s\n' 'boot=<ESP PARTUUID>;aurora/boot/u-boot.bin;aurora/boot/t8140-j700.dtb;-' >> stage2-for-esp.bin
```

This loads only the image and DTB, without publishing a loader initramfs.
The selected EFI OS image supplies its own initramfs. Do not substitute an
empty cpio archive: Linux gives `linux,initrd-*` in the device tree priority
over the EFI initramfs, so an empty archive can hide the OS's storage modules
and `/init`. The three-file direct Linux route remains unchanged.

## Experimental retained-radio handoff

This is an opt-in integration with a local kernel, not the default public
kernel's PIODMA contract. Only a J700 DTB whose kernel implements Linux-owned
SID1 and retained firmware SID16/18 may set this property on its PCIe host:

```dts
apple,j700-radio-handoff = "retained-sid1-v1";
```

With no marker, m1n1 leaves the existing public PCIe handoff unchanged.
The implementation paired with this mode is the local Semi-dirtyroom kernel
commit `8cc2955ae`; it has not been published. The output properties
`linux-enablement-mac,owned-streams` and
`linux-enablement-mac,static-dart-bypass-test` are its experimental ABI, not
upstream bindings. Do not opt a stock public kernel into this mode. A future
public ownership binding must use a separately qualified contract version.

After PCIe initialization, the opted-in handoff validates J700's ADT DART
policy and radio link. PCIe initialization can already have torn down an
inherited link; the handoff does not promise to preserve it. An inactive link
is brought up using the pcIO rail, active-low PERST#, CLKREQ# and Intr2AXI,
keeping PERST# asserted during power cycling and observing the settling delays.
The power sequence is adapted from the owner's private Neo loader and checked
against J700's ADT and DebugUSB captures. This is not a cleanroom provenance
certification. Intr2AXI is ADT register 10 at 0x390024000; accessing register 11
at 0x390048080 produced a synchronous external abort during development.

A guarded typed ECAM read must identify MT7932 before exposing function 0.
Linux owns SID1, firmware retains SID16/18, and the DMA window stays within
32-bit bus addresses. Function 1 remains unavailable. PIODMA diagnostic nodes,
including the root-level SID17 consumer, are disabled. The host's CLKREQ#
pinctrl properties and the port's power request are removed to prevent Linux
from remuxing or reclaiming firmware-owned resources. These properties may
already be absent in an opted-in DTB.

Policy, link-training or identity rejection disables the PCIe consumers and
PIODMA diagnostics while allowing the rest of the OS to boot. The reason is
recorded in `/chosen/apple,radio-handoff-status`. The existing SMC/RTKit
protocol still has unbounded waits, and resource identity checks do not guard
every MMIO access against a hardware fault. This mode needs cold-power-cycle
qualification and additional fault-path hardening before broader release.

## Qualification and installation

The J700 CDC transition arms a 170 second watchdog while waiting for
the primary host session. A completed primary DTR handshake disarms it;
an incomplete transition resets through the normal recovery path.
This path has host-side tests. Gen1 enumeration, primary handshake and
reconnect still require hardware validation.

First run PLAN P2 T-tethered on K-clean and K-full using only
`aurora-ctl hw request`, and retain each `plan.json`, `receipt.json`, console
log and full dmesg under `runs/hw/h-<request>/`. Verify the build tag in the
Stage 2 console and `/chosen/asahi,m1n1-stage2-version`. K-clean must retain
six CPUs, NVMe root and simplefb; K-full must additionally probe SEP, PMP,
Wi-Fi/BT, USB/ATC, AVE, GPU and ISP without a new error. The internal panel
remains on simplefb. DCP handoff and the ISP heap top require their PLAN G4/G3
clean measurements before full-platform qualification.

After a passing tethered run, use PLAN P2 T-esp with the installed Stage 1 and
its approved persistent route. T-s1ram requires an aurora-ctl request kind that
can RAM-vector an unmodified, hash-pinned Stage 1; it is not available yet.
That test must cover host grab, no-host expiry, missing-file proxy fallback,
image length, and panel logo. The final 1TR installation uses the filled **bare**
Stage 1 with `kmutil configure-boot --raw --entry-point 2048
--lowest-virtual-address 0` against the Asahi proxy System volume. Ryan enters
the owner password. Keep the previous bare Stage 1 for rollback with the same
`kmutil` flags. Changing ESP Stage 2 or kernel files does not require 1TR.

No worker should access the target directly or run the old bench scripts. All
target tests and recovery go through approved `aurora-ctl hw request` entries.

## T8140 NVMe non-secure alias aperture

On J700, the live ADT advertises `ans/reg[9]` as a 0x10000-byte window even
though the linear queue control and doorbells are at +0x24908..+0x24910 in
that non-secure alias. The public T8140 Linux device tree describes
its full 0x30000-byte aperture. Stage 2 must recognize that specific layout
before applying its normal register bounds check.

The compatibility rule is limited to T8140, translated NVMMU base
0x38dcc0000, translated NVMe base 0x3cdcc0000 and the advertised 0x10000-byte
NVMe window. Other SoCs, unexpected addresses, shorter windows, undersized
NVMMU resources, overflow and overlapping resources retain their rejection
paths. The ANS-adoption host regression exercises the observed aperture and
negative layouts before any queue publication. The aperture was verified on
one J700 with two warm ESP boots. Cold-power-cycle qualification remains pending.
