# Installing the J613 stage 1 (`J613_ESP_STAGE1=1`)

How the stage 1 from this branch (`f974c0f`) was installed and checked on our J613 (MacBook Air 13" M3) on
2026-10-07. It replaces the raw m1n1 custom boot object of a macOS 26.6.2 (25G83) volume group; nothing else changes.
After this, booting that volume goes iBoot → stage 1 → 15 s USB proxy window → stage 2 read from a file on the ESP.

## What you need

- A J613 with a macOS **26.6.2** install in its own volume group, already set up to boot a raw m1n1 custom boot object
  (reduced security, as for any m1n1 stage 1). Ours is a research volume we call TahoeResearch; your daily macOS and
  Linux stub are not touched. The stage 1 refuses anything that isn't T8122 + J613 + 26.6.2 and stays proxy-only.
- A FAT EFI system partition on the internal disk with room for stage 2 (ours: a G54/D3 stage 2 of ~43 MB with a
  gzip-compressed initramfs). Note its PARTUUID from Linux (`lsblk -o NAME,PARTUUID`).
- A proxy-only m1n1 for rollback. We built a plain `make RELEASE=1 USE_CLANG=1` (no `CHAINLOADING`) proxy and
  verified it boots before touching anything. Keep it on the ESP next to the new stage 1.

## Build

```sh
make RELEASE=1 USE_CLANG=1 CHAINLOADING=1 J613_ESP_STAGE1=1
# Aurora's tool, from aurora-silicon/m1n1 aed999725:
python3 tools/fill_stage1_config.py --input build/m1n1.bin --output m1n1-stage1-j613.bin \
  --esp-partuuid <ESP PARTUUID> --stage2-path <dir>/stage2.bin --window-ms 15000
```

`--stage2-path` is relative to the ESP root. The configuration is a 260-byte CRC-checked block inside the image: filling it does not change the size (ours is
1,196,032 bytes). An empty or CRC-invalid block means proxy only. Stage 2 is a normal m1n1 stage 2 with DTB, kernel
and initramfs appended (from this branch at its head for 25G83).

Copy stage 2 to that ESP path, and the filled stage 1 and the rollback image anywhere on the ESP (recoveryOS can read
it). Record their sizes and MD5s from Linux: recoveryOS has **no `shasum`**, only `md5` and `cksum`.

## Install from the volume's own recoveryOS

1. **Make the 26.6.2 volume the default startup disk** (macOS Startup Disk, or `asahi-bless --set-boot <name>` from
   Linux; not `--next`). Holding power → Options enters the recoveryOS paired with the *default* startup volume, and a
   one-time selection does not change that. From a 14.x recoveryOS `kmutil` fails with
   `Error setting third-party kexts ... pairing 17`.
2. Shut down. Our J613 powers itself back on about 18 s after a software shutdown, so be ready to hold power at
   once. Hold power until startup options appear, choose **Options**, authenticate.
3. Check `sw_vers` says 26.6.2. Mount the ESP and unlock the target volume in Disk Utility (`diskutil apfs
   listVolumeGroups` / `diskutil info` to be sure which is which). Check the stage 1 with `ls -l`, `md5`, `cksum`
   against the values from Linux.
4. Install:

   ```sh
   kmutil configure-boot -c "/Volumes/<ESP>/<path>/m1n1-stage1-j613.bin" \
     --raw --entry-point 2048 --lowest-virtual-address 0 -v "/Volumes/<target volume>"
   ```

5. **Set the default startup disk back** (e.g. to your Linux install) before testing.

Rollback is the same procedure with the proxy-only image in step 4.

## Boot

Select the volume (from Linux, for one boot: `asahi-bless --next --set-boot <name> -y`) and reboot.

- A host that talks to the proxy within 15 s gets it (we checked a `proxyclient` NOP and a proxy reboot inside the
  window). The window doesn't depend on a framebuffer.
- Otherwise stage 1 reads stage 2 from `PARTUUID;path` and chainloads it, keeping `preoslog` and updating the
  BootArgs memory-map pointer.
- A missing file or bad configuration leaves the proxy running.

## What we tested (J613, 2026-10-07)

- Installed via `kmutil` from the 26.6.2 paired recoveryOS; booted by iBoot.
- Window expiry with the host connected but idle → ESP stage 2 → the G54 kernel on the NVMe root, and later the D3
  kernel (display coldplug, native-GPU SDDM greeter, installed qualification passed).
- NOP and proxy reboot within the window.
- The proxy-only rollback, installed the same way, was verified as the object iBoot actually loaded (its code
  sections compared over the proxy).
- **Not tested:** a boot with no USB host attached at all, and a boot after the machine was fully powered off.
