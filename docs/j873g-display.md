# J873g display

This path targets the outermost rear USB-C connector, farthest from HDMI,
using ATC3 and the firmware-initialized DCP/DART state. It must validate the
J873g board, firmware topology, mapped streams, PHY aperture and training
tables before changing the route. Other connectors are not qualified.

Failure cases include a missing sink, wrong cable orientation, incomplete AUX
setup, incorrect AFKv2 response allocation, stale DART translations, a firmware
timeout, link-training failure, and a framebuffer pixel format that disagrees
with the next stage. Keep legacy display routing unchanged. Avoid an unqualified
DCP power-off/reset sequence on failure; preserve diagnostics for recovery.

Qualification requires a fresh RAM boot, HPD and DPCD evidence, four trained
lanes, an acknowledged modeset, framebuffer readback, and visible output on the
physical monitor. A framebuffer image alone does not prove the USB-C link.
Then hand over the active simple-framebuffer to U-Boot/Linux and verify both
the console and display persist. Save exact image hashes and raw logs.

When firmware provides its dummy framebuffer, startup now tries the built-in
HDMI PHY before the ATC3 direct DisplayPort route. Both routes request
1280x720 at 60 Hz. A disconnected HDMI sink permits the USB-C attempt;
missing PHY nodes, initialization errors and failed modesets stop initialization.
A real firmware framebuffer remains unchanged. The HDMI changes have compiled
CPU checks only; physical display and Linux handoff qualification remain open.

The latest RAM diagnostic passed static firmware mapping checks and reached
RTKit v12. Endpoint 4 then requested a fresh 16 KiB IOReport buffer. Its
allocation failed at the locked DART mapping boundary, before either PHY ran.
The native driver maps owned backing through the protected monitor. The loader
has no proven monitor ABI or ownership contract; mapping failure remains fatal.

T8152 RAM reloads require the Mach-O image, including runtime stacks and BSS.
The host places the complete image and firmware block in fresh RAM. It checks
the destination, staging block and next heap against preserved physical ranges.
Do not reload a raw image over the enrolled loader or change inherited DART mappings.
The host preserves cold T8152 secondary reset vectors. Native SMP owns their
vector addresses, cache publication and release sequence after relocation.

KIS memory writes retain the requested length and checksum. The shared transport
combines data and its optional sentinel before word padding. Do not install a
per-loader write adapter.

The native ASCWrap `_dartMapiBootFirmware` routine excludes raw ADT bit 1
records before it creates static firmware maps. The loader applies this rule
without changing the inherited state of excluded records. For included records,
DCP firmware reuse checks every page against the recorded physical backing.
A hole or a conflicting translation fails before modifying a partial mapping.
The loader retains the recorded remap domain and preserves locked DART roots.

From a fresh RAM-loaded candidate with no secondary CPUs started, run
`proxyclient/experiments/t8152-display.py build/m1n1.bin SHA256 --device PORT
--output CAPTURE_DIR --nm llvm-nm`. The output directory must be new. Keep the
matching debug `m1n1-raw.elf` beside the image. The helper validates the loaded
code and target and records the console history, scanout pixels, PNG screenshot
and a JSON receipt. By default it only observes the native boot; it does not
clear the screen or draw a test banner. Use `--initialize` to retry native
initialization after connecting a monitor, or `--reinit` to verify console
history replay after rebuilding the framebuffer. Automatic hotplug is not
implemented.

Framebuffer reinitialization replays the retained console history on the new
surface and preserves whether the console was active. This is shared by USB-C,
HDMI and other framebuffer outputs; serial output is not replayed, and a silent
console remains silent. The finite console ring retains the most recent text.
Routine successful AFKv2 packet/callback and per-lane tuning traces use debug
logging; errors and display startup summaries remain visible.

The USB-C-only baseline completed native boot and history replay on physical J873g.
This image was read from the actual scanout framebuffer with the capture tool's
default observation mode, before any host display-initialization call:

![M6 native m1n1 boot console](images/j873g-boot-console.png)

The automatic-display candidate also completed the public U-Boot/Linux handoff
with all twelve CPUs, shared atomic workloads, bounded CPU-frequency changes
and the Linux framebuffer console. The 105 compiled legacy CPU replay cases
matched the merged baseline; this is not physical qualification of older chips.

On success the native display session and allocations remain live. Once a
next-stage loader has staged payloads, retain its host heap and reserved regions;
do not recreate them or RAM-chainload over the live display. A next-stage loader
must retain those allocations and build its framebuffer node from the updated
`cur_boot_args`, including 32-bit BGRA pixels and the live stride, rather than
the original firmware boot arguments. U-Boot's J873 configuration already enables
`VIDEO_SIMPLE`, `NO_FB_CLEAR` and the video console.

The USB-C-only baseline trained four HBR lanes and acknowledged the 1280x720
modeset. Public U-Boot inherited the framebuffer and booted Aurora WIP Linux;
Linux bound its framebuffer console and passed twelve-CPU and bounded frequency
workloads. The operator confirmed the Linux console and test banner were visible
on the USB-C monitor. Hotplug and other connectors remain unqualified.
