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
