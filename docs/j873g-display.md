# J873g USB-C display

This path targets the outermost rear USB-C connector, farthest from HDMI,
using ATC3 and the firmware-initialized DCP/DART state. It must validate the
J873g board, firmware topology, mapped streams, PHY aperture and training
tables before changing the route. Other connectors are not qualified.

Failure cases include a missing sink, wrong cable orientation, incomplete AUX
setup, incorrect AFKv2 response allocation, stale DART translations, a firmware
timeout, link-training failure, and a framebuffer pixel format that disagrees
with the next stage. Keep legacy display paths unchanged. Avoid an unqualified
DCP power-off/reset sequence on failure; preserve diagnostics for recovery.

Qualification requires a fresh RAM boot, HPD and DPCD evidence, four trained
lanes, an acknowledged modeset, framebuffer readback, and visible output on the
physical monitor. A framebuffer image alone does not prove the USB-C link.
Then hand over the active simple-framebuffer to U-Boot/Linux and verify both
the console and display persist. Save exact image hashes, raw logs and receipts.

From a fresh RAM-loaded candidate with no secondary CPUs started, run
`proxyclient/experiments/t8152-display.py build/m1n1.bin SHA256 --device PORT
--output CAPTURE_DIR --nm llvm-nm`. The output directory must be new. Keep the
matching debug `m1n1-raw.elf` beside the image. The helper validates the loaded
code and target, initializes ATC3, performs the AUX/device-open sequence and
selects 1280x720 at 60 Hz. Startup remains explicit; the normal KIS proxy does
not automatically change display routing.

On success the display session and callback allocations remain live. Do not
create another `ProxyUtils` heap or RAM-chainload over it. A next-stage loader
must retain those allocations and build its framebuffer node from the updated
`cur_boot_args`, including 32-bit BGRA pixels and the live stride, rather than
the original firmware boot arguments. U-Boot's J873 configuration already enables
`VIDEO_SIMPLE`, `NO_FB_CLEAR` and the video console.
