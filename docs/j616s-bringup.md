# J616s / T6040 proxy bring-up

Tested on J616sAP (Mac16,7), board 6, with the ADT supplied by macOS build
26A428. The branch is stacked on `j700` while the draft targets `aurora-wip`.
The target is m1n1/proxy parity with M1, M2 and A18 Pro. Linux and U-Boot
handoff are outside this bring-up's scope.

## CPU clocks

`cpufreq_init()` validates the device identity, three translated CPM apertures,
and the ADT OPP tables without changing clock settings. Proxy operations
`cpufreq_get_cluster_hz(cluster)` and `cpufreq_set_cluster_pstate(cluster, state)`
use cluster 0 for E, 1 for P0 and 2 for P1. The getter reports the ADT frequency
for the accepted command state; it is not a frequency measurement.

Only states 1 and 2 are enabled. Fixed-work measurements on running cores
matched the ADT nominal transitions of 1020 -> 1404 MHz for E and
1260 -> 1512 MHz for both P clusters, with execution-rate ratios of 1.376,
1.200 and 1.200. Each test restored state 1 and approximately its original
execution time. Higher states were rejected without changing the command.
There is no voltage, PLL or thermal-policy programming here.

The driver preserves other command bits, checks the previous expected state,
and bounds each busy poll to 2 ms. An unexpected command state or failed transition
locks out subsequent writes for that cluster until the image is restarted.
A timeout does not imply that hardware reverted to the previous state.

## SMP and MMU

Existing startup code brought up all 13 secondary CPUs: IDs 0-3, 5-8 and 10-14;
CPU 4 is the boot CPU. The hardware has four E and ten P cores. CPU ID 9 is
absent from the ADT and must not be treated as a missing startup.

Every secondary executed scratch code, returned its expected MPIDR and ran
with EL1/EL2 MMU and caches enabled after `mmu_init_secondary()`. AT S1E1R
translations succeeded for shared scratch memory, and four-argument calls
returned the expected result. Concurrent exclusive-access increments across
all 13 secondaries produced exactly 130,000 updates to one shared counter.
Ten additional dispatches per secondary also passed. No new SMP startup code
was needed for this tested configuration. The boot CPU's `smp_is_alive()`
result is not a secondary-start indicator.

These checks cover startup, WFE/SEV dispatch and shared memory coherence.
A cold independent-workload probe also passed on all 13 secondaries. The E
cores checked repeated memory sums, P0 cores ran integer recurrences and P1
cores ran rotate/XOR loops in separate buffers. Every result matched its host
calculation, all execution intervals overlapped, and nominal cluster clock
settings were unchanged. The test initializes every secondary's MMU before
calling through the RX alias and keeps its scratch code unchanged until all
workers return. Earlier trials violated these assumptions and are invalid
qualification evidence. No new SMP/AMP driver code was needed.

Deep sleep, hotplug, hypervisor operation and sustained thermal load remain
unqualified. Reproduce the cold independent-workload test with:

```sh
M1N1DEVICE=/dev/ttyACM0 python proxyclient/experiments/t6040_amp.py
```

The probe leaves secondaries running in WFE mode. Reboot before repeating it.

## Other devices

* CDC proxy and bulk reads work at USB high speed (480 Mb/s). SuperSpeed is
  pending PHY/role bring-up and cable/port qualification.
* SIO completes RTKit startup with four-level, full 42-bit mappings of its
  ADT-described external firmware. Firmware text is mapped read-only. The
  standard system endpoints start, AP/IOP both acknowledge state 0x20,
  IOReporting messages are handled and management ping receives a pong.
  Endpoint 0x20 is advertised but its application protocol and audio DMA are
  untested. AP/IOP acknowledge quiescence at state 0x10; CPU control, table
  pointer, TCR and stream mask return to their original values, with zero
  DART errors and a successful proxy NOP.
* AOP completes RTKit startup and answers a management ping with full 42-bit
  mappings, ADT DAPF ranges and its external-code boot argument set to the
  ADT remap address (1 TiB). Starting the advertised application endpoints
  announces SPUApp, wakehint, aop-audio, aop-voicetrigger, accelerometer,
  gyroscope, ambient-light and lid-angle services. All twelve application
  endpoints acknowledge queue startup, including the standard gyroscope queue.
  Only the external-code boot argument needs changing for this startup probe.
  ALS interval enable/disable, HID-descriptor and manufacturer queries return
  success, but no ALS samples were observed in a two-second capture. Periodic
  lid-angle reports from `las` now use the Asahi driver's angle byte and
  return a live raw value of 112. Response to lid movement remains untested.
  Reports from `cma` were captured; their format is unqualified.
  Fresh AFK shutdown responses are not received within the bounded wait.
  Earlier tests incorrectly counted the initially false `alive` flags as ACKs.
  IOP quiescence and sleep trials cause a firmware instruction abort. Mappings
  must remain installed until reboot on this
  failure. ALS and inertial measurements, audio paths and IOP shutdown remain
  unqualified.
* MTP completes RTKit startup, answers a management ping and acknowledges
  AP/IOP quiescence. The ADT selects DART stream 0, unlike the older MTP
  experiment's stream 1. Four-level mappings and ADT DAPF ranges are restored
  after shutdown with no DART fault. Passive DockChannel capture receives six
  checksum-validated initialization packets with keyboard and multitouch HID
  descriptors. The optional keyboard probe sends enable only to the announced
  keyboard interface 2; its zero-status command ACK and fresh DeviceReady
  response pass on repeated cold boots. The optional touchpad probe uploads the
  tested J616s 26A428 firmware, patches its interface slot and requires fresh
  enable/upload ACKs, four version-2 power-request replies and DeviceReady.
  Firmware logs report Touch MT ready and raw reports arrive. A combined
  45-second capture recorded keyboard press/release reports for hello and space;
  finger movement and taps produced no touch reports beyond startup status.
  Both final Off replies, AP/IOP quiescence and DART/DAPF restoration pass.
  Touchpad motion reporting remains unqualified.
* SEP's existing RNG routine returned zero bytes. Successful SEP communication
  is not established.
* ISP revision `0x100003` was read after powering the ADT parents and the
  ISP_CPU/ISP_FE global slots at offsets 0x4000/0x4008. All tested domains
  returned to their original states, including ISPSENS0 state 4. Firmware
  startup, channels, heap requirements and camera operation remain untested.
* No speaker output, speaker amplifier programming or audio DMA stream was
  started.

The experimental stage 2 was chainloaded into RAM. Installed boot images,
boot policy and partitions were not changed. Preserve a working CDC recovery
path before experimenting with further devices.

## Proxy-client DART fixes

Rejected mapping requests previously enabled a stream before validating its
mode and alignment. New stream publication now follows populated tables. A
four-level root-reuse path referenced an undefined variable in an unnecessary
assignment; removing it permits repeated mappings under the same root. Eight
host cases cover rejected requests, publication order, three/four-level
translation, reused roots and additional leaf allocation. Live three-level
firmware mapping/readback and restoration passed. SIO's successful cold
firmware startup also validates four-level hardware operation on its DART.

Direct native calls must use symbols from the matching raw ELF for raw images.
An initial ISP trial used the Mach-O-layout ELF, called the wrong code and
triggered a reboot. Installed stage 1 recovered CDC; the corrected raw-ELF
probe passed. The earlier SEP result was discarded and repeated with verified
raw ELF/image pairing. Proxy-opcode clock tests and scratch-code SMP tests
were unaffected.

## Reproducing the cold IOP probes

After rebooting into m1n1 proxy mode, run:

```sh
M1N1DEVICE=/dev/ttyACM0 python proxyclient/experiments/t6040_iop.py sio
```

Select `sio`, `mtp` or `aop` as the final argument. SIO/MTP use existing
StandardASC system endpoints. MTP also captures initialization packets from
DockChannel without enabling any device. Add `--keyboard` to the MTP command
to enable only the announced keyboard and check its ACK/readiness responses.
The capture is bounded to five seconds per phase and 16 KiB; it verifies each
packet checksum before parsing its contents. It does not enable the actuator
or multitouch interface, program GPIOs or upload firmware. Keyboard input
events still require separate validation. AOP
also initializes advertised AFK endpoints to read service announcements, then
waits for all queue-start ACKs and requests AFK shutdown. Fresh responses
currently time out, so the AOP probe returns a failure and requires reboot.
It sends no audio application commands.
AOP retains the IOP with its mappings and boot arguments installed and
reports `reboot_required`: recover by rebooting before further experiments. It
requires the tested cold IOP/DART state and prints a
JSON result. Reboot before running it again: firmware resume is not qualified.
Send, table invalidation, startup and quiescence waits are bounded. If shutdown
is not acknowledged, mappings remain installed and the result requires reboot.

To test the touchpad firmware in RAM, pass the HIDF file produced by Asahi's
firmware extractor for J616s and macOS build 26A428:

```sh
M1N1DEVICE=/dev/ttyACM0 python proxyclient/experiments/t6040_iop.py mtp \
    --touchpad-firmware /path/to/tpmtfw-j616s.bin
```

This mode requires the tested firmware hash. Add `--keyboard` to capture both
interfaces and `--capture-seconds 45` for a longer input window (1-60 seconds;
up to 1 MiB). It sends no actuator commands and makes no GPIO writes. Version-2 power requests include
Off/On and WillChange/HasChanged phases; replies must echo the exact request
with the expected transfer counter and zero status. DeviceReady must arrive
after the On request. After capture, both Off phases must be acknowledged
before AP/IOP quiescence and register restoration. Failed or uncertain shutdown
returns `reboot_required` and retains the IOP, DART, DAPF and DMA allocations
until reboot. Control requests require an empty receive FIFO before transmission;
continuous input may cause a safe refusal. Reboot before each probe run. No firmware blob is included in this PR.

AFK shutdown now waits for a fresh ACK instead of interpreting an initially
false `alive` flag as completion. Its timeout bounds ACK polling; the shared
IOP probe also bounds ASC sends. Ring pointers use one aligned 32-bit store
at each negotiated header offset. This removes duplicate stores that
overwrote the read pointer when the header block size was 128 bytes. Host
tests cover both 64-byte and 128-byte layouts; live AOP queries and report
reception pass with the corrected publication.

StandardASC now accepts an optional DVA mask while preserving its existing
36-bit default. The IOP probes configure a full 42-bit address range and mask.
IOReporting reads its buffer through the ASC/DART translation path, including
firmware-preallocated buffers. Host cases cover both masks across read, write
and translation, preallocated
IOReporting buffers, and invalidation of the selected stream.
