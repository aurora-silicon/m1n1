# J613 (MacBook Air 13" M3) m1n1: daily handoffs, 25G83 handoff, no-proxy stage 1

Branch `satchlj/j613-m1n1`, on Eryk's `m3-gpu-handoff` at `9b9fa13` (v1.6.1 `06a4601` + his two J514S DCP commits).

## What this branch is

- `51c0630`..`0eb2557` (5): the stage 2 behind the J613 daily kernels on 14.8.3 (`satchlj/j613-kernel-14x` in
  dirtyroom-linux). It extends Eryk's M3 internal-DCP handoff to J613, hands the T8122 GPU firmware to Linux, hands the
  display to drm/apple's display gate, logs the DCP CPU state, and keeps the notch rows when
  `chosen.asahi,show-notch=1`.
- `30e01de`: the 25G83 (macOS 26.6.2) mapping handoff for `satchlj/j613-kernel-25g83`. Recognises 26.6.2, adds a
  separate J613-25G83 DCP profile with its own marker, and publishes DCP CPU snapshots.
- `f974c0f`: **J613 stage 1** (`J613_ESP_STAGE1=1`). A 15 s USB proxy window, then it loads stage 2 from an ESP file
  given as `PARTUUID;path` in Aurora's 260-byte CRC-checked in-image configuration. Guarded to J613 + 26.6.2. It
  keeps `preoslog` and updates the BootArgs memory-map pointer when it chainloads.
- `d5d2932`: stage 2 exports the same boot's ADT display clock (`/chosen/apple,j613-25g83-display-clock-adt`) for
  the 25G83 display gate in Linux.

The daily stage 2 build also applies Omarchy's `aurora-cio-aliases.patch` (pkgbuilds/m1n1-aurora in
maralcbr/omarchy-pkgs). It is not ours and not included; nothing here depends on it.

## Build

- Daily 14.x stage 2: `make RELEASE=1 USE_CLANG=1` at `0eb2557` (the daily image also carries the Omarchy boot logos).
- 25G83 stage 2: `make RELEASE=1 USE_CLANG=1` at the branch head (it was built from `30e01de` + `d5d2932`).
- Stage 1: `make RELEASE=1 USE_CLANG=1 CHAINLOADING=1 J613_ESP_STAGE1=1` (built from `30e01de` + `f974c0f`). Then
  fill the configuration with Aurora's `tools/fill_stage1_config.py` from aurora-silicon/m1n1 `aed999725`:
  `--input build/m1n1.bin --output <file> --esp-partuuid <ESP PARTUUID> --stage2-path <path on the ESP>
  --window-ms 15000`. An empty or CRC-invalid configuration means proxy only. Install as a raw custom boot object
  (`kmutil configure-boot --raw --entry-point 2048 --lowest-virtual-address 0`) in the paired recoveryOS of a 26.6.2
  volume group: step by step in [INSTALL.md](INSTALL.md).

## Tested on hardware vs compile-only

- `51c0630`..`0eb2557`: J613 daily boot loader since late September (14.8.3).
- `30e01de`, `d5d2932`: the stage 2 of the G54/G55 25G83 boots. The clock witness reads 712000000 Hz on this
  machine.
- `f974c0f`: installed with `kmutil` as the stage 1 of the 26.6.2 volume group on the J613 and booted by iBoot
  (2026-10-07). With the host connected but idle, the window expired and it booted the ESP stage 2: first the G54
  kernel on the NVMe root (appledrm, native Mesa frames), then the D3 daily candidate (display up by itself, native-GPU
  SDDM greeter; D3's installed qualification passed, see `J613-NOTES.md` on `satchlj/j613-kernel-25g83`). A NOP and a
  proxy reboot inside the window passed with the installed image. The missing-file path was tested with the same image
  loaded into RAM. A proxy-only rollback installed the same way was verified as the object iBoot loads. **Not tested
  yet:** a boot with no USB host attached, and a boot from full power-off.
- Compile: the branch head builds on macOS/clang both as stage 2 and as stage 1 (1,196,032 bytes, the same size as
  the installed image). The only warnings are pre-existing, in files these commits don't touch.

## Key findings

- 26.6.2 identifies as `mBoot-18000.161.10`. The 25G83 DCP firmware UUID is `C042E95C-B9D8-3F0E-94B3-582A08AA6FDD`,
  with disp0 DART `0x28d304000`, SID masks `0x50/0x20` and a 64-row notch. Its profile uses a distinct marker so it
  can never authorise the 14.x display gate.
- The stage-1 USB window does not depend on a framebuffer, so it also works when stage 1 has none.

## Commits (oldest first; author is Satya Benson unless named)

- `51c0630` kboot: extend the M3 internal DCP handoff to J613 (T8122)
- `4ec2348` kboot: hand off the T8122 GPU firmware to Linux
- `bb3ed55` kboot: hand the J613 display to drm/apple's display gate
- `115b613` main: log the T8122 DCP CPU state (read-only, J613 bring-up)
- `0eb2557` kboot: keep the notch rows in the boot framebuffer when chosen.asahi,show-notch=1
- `30e01de` kboot: J613 25G83 (macOS 26.6.2) DCP mapping handoff
- `f974c0f` J613 stage 1: 15 s USB proxy window, then stage 2 from an ESP file
- `d5d2932` kboot: export J613's same-boot ADT display clock on 25G83

## Publish branch `satchlj/j613-stage1-publish` (2026-10-09)

For iconidentify/aurora-linux's `--m3-profile=j613-25g83` route: J613 users boot Linux through
their own macOS 26.6.2 volume with this stage 1 chainloading the Aurora `m1n1/boot.bin`.

- `23c2196`: stage 1 bounds `preoslog`, checks the image allocation, goes straight to the proxy
  on an empty configuration, and requires `CHAINLOADING=1`, as in patch 0054 of the
  m1n1-aurora 1.6.1.aurora15 recipe (which carries this stage 1).
- `05c8fb8`: `tools/j613-stage1/build.sh` (version tag `v1.6.1-j613s1-g<12-hex commit>`),
  `tools/j613-stage1/fill_stage1_config.py` (reads the ESP PARTUUID on Linux; target defaults to
  `m1n1/boot.bin`), `tests/j613-stage1/test_stage1_config.py`.
- Factory image from `05c8fb8`: `m1n1-j613-stage1-v1.6.1-j613s1-g05c8fb886667.bin`, 1,196,032
  bytes, SHA-256 `3816a065ce9f820495d594f71abc4f39475d6044fd23e9da916b0abcf844dd1e`; two builds
  (Rust 1.89.0, clang/lld 18.1.3) in different directories are identical.
- Size matters: iBoot puts the panel DCP's live `__OS_LOG` (144 KiB) right after the installed
  raw object, and stage 1 copies stage 2 over that address. At 0x124000 bytes the log falls in
  Aurora m1n1's zero .bss (its payload starts at 0x3e0000). `build.sh` refuses sizes outside
  0x114000..0x3bc000. The aurora15 tree built as stage 1 is 0x3f0000 bytes: too large.
- Not tested on hardware yet: this commit's image, chainloading the Aurora `boot.bin`, U-Boot on
  26.6.2. The test plan is in mac-aurora-omarchy
  `machines/m3-air/research/2026-10-09-25g83-stage1-publish/`.
