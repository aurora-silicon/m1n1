# J616s / T6040 proxy bring-up

Tested on J616sAP (Mac16,7), board 6, with the ADT supplied by macOS build
26A428. The branch is stacked on `j700` while the draft targets `aurora-wip`.
Upstream Asahi M1/M2/M3 m1n1 and proxyclient behavior is the implementation
standard; J700 is the feature-parity target. Linux RAM boot preparation is
now in scope. A bounded one-CPU hypervisor Linux boot reaches a RAM-only
BusyBox shell; native boot, persistent operation and Linux peripherals remain
unqualified.

For implementation order, formats and ownership rules, start with the
[driver contracts](j616s-driver-contracts.md).

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

Deep sleep, hotplug, multicore hypervisor operation and sustained thermal load remain
unqualified. Reproduce the cold independent-workload test with:

```sh
M1N1DEVICE=/dev/ttyACM0 python proxyclient/experiments/t6040_amp.py
```

The probe leaves secondaries running in WFE mode. Reboot before repeating it.

## Minimal Linux RAM boot

A guarded direct-entry test boots Linux 7.1.12+ through m1n1's hypervisor on
one CPU with an owned 64 MiB guest allocation. Image, FDT and initramfs are
read back twice and cache-published before entry. The guest maps only its RAM,
the physical AIC and an intercepted UART console on the second CDC interface.
Its CPU node uses the observed architectural affinity 0x10100.

The RAM initramfs reaches a BusyBox shell and accepts a diagnostic command:
kernel version, CPU online 0, uptime advancing across a two-second sleep,
kernel configuration hash and RAM-only mounts all return. Timer interrupts
advance and the interrupt error count remains zero. No block device is mounted.

An initial run exposed a host reply-ordering defect: register postchecks after
the exception proxy's EXIT command race the resumed guest's next notification.
Deferring those checks until the next suspended handler fixes the captured
opcode mismatch; byte-level regressions exercise the actual proxy framing.
Normal prechecks, pending records and write-ownership bounds remain enforced.

A later 60-second console window accepts four marked commands separated by
10-second waits. Every command returns status zero, uptime and timer interrupts
advance, and the interrupt error count remains zero. The queue rejects echoed,
unmatched or duplicate completion markers and never retries ambiguous input.
An absolute deadline bounds both proxy reads and console dispatch; a 120-second
hardware watchdog preserves recovery after the longer window.

The current aurora-wip kernel also boots with USB/configfs ACM/ECM and network
support compiled in, using the same RAM initramfs and minimal HV DT. Configfs
mounts successfully and its gadget directory is present. No UDC is exposed by
that DT, so this is kernel/configfs qualification, not Linux gadget operation.
One config-query command fails because the initramfs lacks a zcat symlink;
its BusyBox applet remains available by explicit invocation.

The bounded captures end with a host timeout while the guest is running.
Mappings and guest memory remain retained until hardware-watchdog recovery
returns the installed proxy with its watchdog disabled. This is not a clean
guest shutdown or a persistent Linux development session. Native boot, Linux
SMP and peripheral drivers require separate qualification.

Separate native RAM diagnostics reach the original Linux CPU setup, TTBR
loads, MMU enable and early kernel mapping. Nonce-bound markers survive a
warm return to the fully verified proxy. Fresh page-table walks verify the
diagnostic code, marker and FDT mappings, plus the virtual address of
`__primary_switched`. A further checkpoint reaches that virtual entry and
verifies the kernel stack, task pointer and exception vectors before
`finalise_el2`. Completed kernel startup and gadget traffic remain unqualified.
Diagnostic code uses owned linker padding with verified permissions; guest
tables and CPU context remain retained after the warm return.

A later checkpoint also completes the original `finalise_el2` transition to
VHE EL2 with the kernel MMU enabled. A separately compiled copy of Linux's
upstream reset routine runs through its verified physical identity mapping,
disables translation and returns to the verified proxy. This establishes the
early VHE transition and bounded recovery, not complete reset-register
qualification or completed kernel startup.

The next checkpoint reaches the original `start_kernel` prefix and records
the physical CPU identification and Linux version banner. The stopped
writer's bounded printk snapshot is cache-published before recovery; two
complete captures match and their record IDs, sequence and text bounds pass.
`setup_arch`, interrupt delivery and Linux gadget traffic remain unqualified.

## Memory controller

The ADT identifies the controller as `mcc,t6041`, with four AMCC apertures
and four planes per aperture. Initialization accepts the tested J616s layout,
checks the way-count status on every plane and preserves firmware cache
settings. A RAM-only candidate boot passed with zero CPU exceptions and
exported both carveout ranges matching the fresh ADT. Failed initialization
or unavailable carveout metadata stops startup on T6040.
An independent walk of the exported translation tables checked all 110,544
page/alias positions across both carveouts and the identity, RWX EL0, RW EL0
and RX EL1 aliases. Every position was unmapped. All 67 table snapshots
matched a second read, with zero CPU exceptions; replaying the archived
tables gave the same result. This verifies table contents, not independent
TTBR/TLB enforcement.
One guarded read of controller 0, plane 0 at offset `0x2800` returned
`0x8000000c`. A separate cold test read offset `0x2804` and returned
`0x0c000c00`, consistent with upstream PR664's proposed encoding of 12 data
ways and 12 tag ways. Both reads completed without CPU exceptions, followed
by successful watchdog recovery to the installed proxy. A further cold sweep
read `0x2804` once on each of the 16 controller/plane pairs. Every word
returned `0x0c000c00`, with no CPU exceptions.

These observations qualify status reads, not cache-control writes.
The ADT lock predicate uses offset `0x2800`, mask `0x1f` and expected value
zero; it must not be interpreted as the way-count status at `0x2804`.
PR664 defines the newer offsets but its enable routine still uses the older
`0x1c00`/`0x1c04` offsets. This implementation uses the physical ranges in
`/chosen/carveout-memory-map` rather than interpreting the unqualified TZ
registers. Cache-control writes and TZ register interpretation remain
unqualified.

## Native USB retirement

Generic DWC3 shutdown now ends every active endpoint, including control OUT,
using its recorded resource index and a forced ENDTRANSFER command. It waits
for the matching command-completion event before checking controller halt,
reset and DART invalidation. Constructor or retirement failures retain DMA
memory and tables, latch the failed owner and prevent another initialization
or native entry. The caller must provide recovery after an uncertain stop.

A guarded RAM test with this C implementation boots a candidate and vectors
back to a recovery image. Both new USB generations pass complete relocated
executable checks and preserve the caller's 45-second watchdog. The final
verified recovery explicitly disables it. The earlier checked implementation
failed this transition; adding the resource, force and completion handling
made it pass. Test-only watchdog startup and RAM diagnostics stay outside
the production changes. Native Linux gadget traffic and SuperSpeed remain
unqualified.

## DCP management

A guarded inherited-state test checks the current 25G76 firmware UUID, exact
clock ancestry, absence of a target-side DCP client and existing stream-23
translation before mailbox access. The CPU is running and both mailboxes are
healthy and empty; no unsolicited Hello is pending.

A separate bounded test sends one standard RTKit IOP power INIT request,
without starting/resetting the CPU or changing DART. It receives Hello,
negotiates protocol 12 and acknowledges the complete endpoint map, including
endpoint 0x23 used by the existing iBoot service. DART errors remain zero and
watchdog recovery passes. This establishes management communication.

A later bounded test starts endpoint 0x23 and sends the initial AFK request.
It receives INIT_ACK and GETBUF requesting 256 64-byte blocks (16 KiB), with
tag 0xcafe. No buffer address is acknowledged and no DMA allocation, ring
start or application query follows. Watchdog recovery passes. This establishes
initial AFK control, not shared-buffer ownership, a running application queue,
firmware-version queries or scanout. The inherited DART context has three
levels; the SoC's support for four levels does not authorize replacing that
existing context.

## Other devices

* CDC proxy and bulk reads work at USB high speed (480 Mb/s). RAM-only
  controller halt/reset/reinitialization tests re-enumerate in the same image
  and pass an exact 64 KiB transfer. SS-capable controller settings also return
  through high-speed fallback when the ATC PHY is left in its initial state.
  Native mode-4 PHY preparation, the complete ten-write PIPE sequence and all
  immediate masked readbacks are qualified with high-speed operation. Tests
  retain the installed image and recover through the hardware watchdog.
  Requesting SS after PHY preparation still does not re-enumerate. The firmware
  tunables-done indication remains clear in the tested sequence; this is not a
  general PHY-ready flag. Physical SuperSpeed remains unqualified. See the
  driver contracts for ownership and firmware limitations.
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
  The native audio directory contains 33 devices and rejects index 33.
  `hpai`, `lpai` and `pdm0` attach successfully. Both input frontends return
  `idle` before and after attachment; their main-property replies list separate
  dependency chains. These control queries produce no DART fault.
  Inline PDM configuration queries are rejected. A shared-buffer command read
  returns 626 bytes for `pdm0` property 200; the legacy client structure expects
  631 bytes and is incompatible with the captured layout. Four trailing
  24-byte blocks match the separate property 206-209 replies. Earlier
  fields, timing units and setter ABI remain unqualified; no PDM configuration
  writes are made. The production shared-buffer command client also reads
  8,200 bytes for `pdm0` properties 211 and 212, with identical payloads in
  this capture. The existing high-frequency decimator decoder consumes and
  rebuilds both payloads exactly; property 212 also passes the typed response
  reader on a fresh live reply. The decoder reports latency 36, ratios
  10/5/3 and 386 coefficients. No new decoder is needed; setter behavior and
  changes to HP source configuration remain unqualified.
  It accepts command envelopes carried by subtype 0xa0 while preserving typed
  notification replies, validates request and response buffer bounds, performs
  cache maintenance and clears pending state on failure. The live command
  getters complete with both input frontends idle and zero DART faults.
  Typed property replies
  use the request's device and modifier when decoding their payload; live
  frontend-state queries and recovery after a rejected request build pass.
  Error replies contain only the firmware return code; the decoder preserves
  it without trying to parse a configuration payload. Successful payloads
  are bounded by the reported length. Live PDM queries return 0xe00002c2
  or 0xe00002f0, followed by a successful frontend-state query.
  `AudioPropertyFormat` uses the same reply envelope and returns `retcode`,
  `len` and raw `data`. Its old PCM field names incorrectly interpreted the
  length prefix as a format code. Live property 302 replies contain 24 bytes
  for `lpai`, four-byte `idle` for `a2px`/`adpx`, and four zero bytes for `dvpx`;
  their meanings remain unqualified.
  The J616s 25G76 AOP image matches the running ADT UUID and segment layout.
  Its metadata identifies the T604x audio driver; the main PDM configuration
  setter ABI remains unqualified. The 16-byte `lpai` property 301
  reads successfully; writing the same bytes returns 0xe00002cf (not writable).
  It cannot serve as a configuration-write template.
  A read-only input property scan also returns three words of value 7 from
  `lpai` property 300, matching the ADT channel masks. The matching firmware's
  initializer and validator identify a 12-byte structure ordered as supported,
  enabled and history channel masks. Its write handler requires that size and
  validates the values against the board configuration. An exact GET-byte SET
  returns zero; GET readback remains `(7, 7, 7)`, both input frontends remain
  idle and DART faults stay zero. Alternate masks are unqualified. All 105
  getter requests complete without DART faults.
  Matching firmware identifies `lpai` property 303 as latency in frames (320)
  and property 304 as safety offset in frames (672). Property 305 contains a
  48-byte `IOAudio2Status`. In 20 active captures, its second u64 advances as
  a frame counter and the last u64 equals `(counter + 1) * 12 % 768000`.
  Other fields and exact clock behavior remain unqualified.

  LP input capture works with an owned destination configured before startup.
  The ADT assigns the `adpx` buffer 768,000 bytes on DART stream 8. Zero a
  page-rounded allocation, install and verify four-level identity mappings,
  then SET `adpx` property 300 with a 16-byte address/capacity pair. Readback
  returns the owned address and normalized size 768,000. Public attachment of
  `lpai`, `pdm0` and `adpx`, followed by `lpai` `pw1 `, succeeds. Attach `lai `,
  then request `runn` on `lai ` and `lpai`; both return matching state readbacks.
  Earlier startup attempts without the buffer caused null-address aborts.

  Twenty native memory copies at half-second intervals produce contiguous
  observed update regions. Including an inferred initial 8,000-frame window,
  reconstruction produces a normalized ten-second mono preview at 16 kHz.
  The tester confirms recognizable speech; exact snapshot-boundary integrity
  remains unqualified. This qualifies the tested LP input path, not three
  independent microphones or
  exact sample-clock accuracy. Candidate lanes 0/2 largely duplicate each
  other; lane 1 has DC plus correlated signal. Allocation padding stays zero,
  DART reports no faults and CDC remains responsive.

  Stop requests `lpai` `pw1 ` and `lai` `idle` return success with matching
  states; the destination then becomes zero and stable. Copy data before stop.
  The buffer setter publishes its address before later validation, and public
  ownership release is not established. Retain the buffer, heap, AOP and all
  mappings on every attempted handoff until validated watchdog cold recovery.
  Recordings, firmware and private probe harnesses are not included here.

  HP microphone capture also contains user-recognized audio. With LP report
  readiness established, attach `hpai`, request `pw1 `, publish an owned
  stream-10 mapping and start NS RX0 before requesting `pwrd`. The tested
  channel uses the split RX bank and one non-repeating descriptor. Complete
  2 MiB and 6 MiB captures pass fresh full-range translation, matching report
  IDs, zero residue, zero DART faults and two identical full reads before RX
  stop. The 6 MiB recording contains 524,288 three-channel Float32 frames at
  the nominal 48 kHz rate, lasting 10.92 seconds; the tester confirms it works.

  Both captures begin with 7,211 zero frames, about 150 ms. Earlier 16 KiB
  captures were too short to cover that observed interval at this format.
  Longer capture windows produce samples without changing the DMA provider,
  clocks, coefficients or native stream-classification commands. Stop RX
  before returning the frontend to `idle`; retain AOP, buffers and mappings
  through watchdog recovery because ownership release remains unqualified.
  This qualifies cold captures, not continuous streaming, restart, calibration
  or measured sample-clock accuracy. The public `aop_capture.py` command now qualifies both cold extents;
  continuous streaming and restart remain outside its contract.

  Existing LEAP tools decode the pinned Apple program and identify three float
  outputs on port 41. The older Asahi driver supplies read-only execution
  counters; a routine-enable readback alone does not prove execution. Neo's
  tested capture provides useful timing and lifecycle evidence, but its DMA
  provider differs from the NS provider advertised by J616s and is not a
  transferable address assignment.
* Headset input `cin ` reaches `pwrd` with the exact 109-byte MCA profile.
  The working macOS profile matches these bytes, including `ms02`, `syn2`,
  mono 24-bit samples in 32-bit slots and a nominal 48 kHz rate. Native
  CS42L84 input uses a 32-bit TX word (`ASP_TX1_CTRL = 0x1f0001`).
  Matching that word alone still produces static. Initializing the documented
  `MIC_DET_CTL4.LATCH_TO_VP` bit before detection produces user-recognized
  phone music in the proxy capture. The cold latch changes from 0 to 2 with
  readback; other bits are preserved.
  The private harness maps an owned 2 MiB buffer on DART stream 8 while
  preserving stream 0 and submits one non-repeating RX2 descriptor. The
  report matches its ID with zero residue. Two full copies taken before RX
  stop match; all six RX2 configuration fields restore exactly. This
  qualifies one cold acoustic capture, not restart, calibration or a measured
  sample clock. The recording uses the nominal 48 kHz profile.
  Separate detection-mode and bias restoration succeeds after the full
  capture, followed by exact codec/controller restoration and frontend idle.
  Earlier I2C FIFO timeouts occur with a growing DMA checkpoint serialized
  between MMIO operations. Compact codec journals preserve pending-state
  evidence and pass the longer restoration trial. Transport timing and
  restart remain under qualification. AOP shutdown still needs watchdog
  recovery;
  installed images are preserved. Native macOS speaker traces supplied the
  protection and transport references for the separate playback tests below.
  The shared `CS42L84Input` class supplies the validated input-only codec
  lifecycle through bounded caller-provided register access. It requires
  receiver-start and receiver-stop gates, preserves unrelated register bits,
  and retains uncertain state without further access. The board power/reset,
  fresh MCA-profile check and owned DMA orchestration remain caller-owned.
  A live capture using this shared class passes exact cleanup as above.
* Headphone playback through `cout` is now user-confirmed. Its live 109-byte
  stereo MCA profile matches the native macOS profile. An owned, non-repeating
  TX2 descriptor plays a two-second sequence with a 440 Hz left-channel tone
  followed by an 880 Hz right-channel tone. The tester confirms audible output.
  The nominal format is 48 kHz, 24-bit samples in 32-bit slots.

  The native TX FIFO calculation requires the upper almost-full threshold to
  exceed the lower transfer threshold. The tested full-depth policy uses
  `0x03000180` for the owned 768-byte FIFO; the previous zero upper half stalled
  TX. The matching completion report, zero residue, empty rings and zero DART
  faults pass. TX configuration, codec controls, I2C controller/reset state and
  frontend power restore successfully before watchdog cold recovery.

  The test preserves voltage/impedance fields and uses a raw DAC gain of
  -60 dB. Compact per-operation I2C journals retain pending-state evidence
  while avoiding growing snapshots between FIFO bytes. Headset capture and
  headphone playback are qualified separately; simultaneous duplex,
  continuous playback, calibration and Linux audio remain unqualified.
* All six SN012776 speaker amplifiers pass native identity reads and a bounded
  protected configuration test while remaining in shutdown. The test completes
  534 byte writes, checks page 0, mode 0x82, identity 0x30 and digital volume
  0xc9 on each amp, and restores both I2C controllers and the shared reset GPIO.
  No output is enabled during that test. Native `spkr`/`ms00` playback uses six
  24-bit samples in 32-bit slots. A separate cold test checks its exact profile
  and power transitions, then completes one 1,152,000-byte zero-data TX0
  descriptor with all amps held in reset. Its report, zero residue/DART faults,
  configuration and frontend restoration, and watchdog recovery pass.
  A later bounded left-woofer pulse is user-confirmed audible: one second of
  300 Hz, peak 0.125 and DVC attenuation of 60 dB. Matching TX completion,
  zero residue/DART faults, muted stop, amplifier/controller/GPIO restoration
  and watchdog recovery pass. Its measured active interval is 1.219 seconds.
  A separate right-woofer-only pulse at the codec control midpoint (DVC 0x65,
  50.5 dB attenuation) also passes transport and cleanup and is user-confirmed
  audible. The other five amps remain in their verified initial shutdown.
  A subsequent paired first-woofer test is user-confirmed audible for five
  seconds. Its finite 5,760,000-byte TX0 descriptor completes with zero residue
  and DART faults; both amps mute before TX stops and restores its fields.
  An I2C timeout during the later amplifier shutdown wrapper prevents complete
  cleanup. Mappings remain retained until successful watchdog recovery.
  A subsequent observed-status-clear sequence completes all 534 shutdown
  writes with exact controller/GPIO restoration in 7.1 seconds. The second
  left/right woofers and both tweeters then each pass an individual one-second
  pulse, matching TX completion, zero residue/DART faults, muted stop, full
  amplifier/controller/GPIO restoration and watchdog recovery. The user
  confirms tones from every driver. Woofers use 300 Hz; tweeters use 2 kHz.
  The second woofers and left tweeter use DVC 0x65, while the right tweeter
  uses the requested lower setting, DVC 0x78 (60 dB attenuation). These tests
  qualify all six individually, not simultaneous full-array playback.

  Python I2C status clearing now writes the observed status word and refuses
  an active transfer. This follows native and upstream Linux handling;
  undefined status bits remain intact in the snapshot. Eight host regressions
  and direct live calls on both idle speaker buses pass. The bounded private
  amp trials keep their existing error and XEN/no-XIP completion gates.
  These successful runs do not prove that intermittent timeouts are cured.
  Native DMA format selection, feedback, calibration and continuous playback
  remain unqualified.
  See the driver contracts for initialization order and the tested AP policy.
* MTP completes RTKit startup, answers a management ping and acknowledges
  AP/IOP quiescence. The ADT selects DART stream 0, unlike the older MTP
  experiment's stream 1. Four-level mappings and ADT DAPF ranges are restored
  after shutdown with no DART fault. Passive DockChannel capture receives six
  checksum-validated initialization packets with keyboard and multitouch HID
  descriptors. The optional keyboard probe sends enable only to the announced
  keyboard interface 2; its zero-status command ACK and fresh DeviceReady
  response pass on repeated cold boots. The optional touchpad probe uploads the
  tested J616s 26A428 firmware and patches its interface slot. It first enables
  the announced actuator interface and requires its fresh DeviceReady response,
  then requires upload ACKs, four version-2 power-request replies and touchpad
  DeviceReady. A sensor-dimensions query returns sizes 15600/9600 and coordinate
  bounds X -7456..7976, Y -163..9283. Human movement tests produce report 0x75
  frames with changing coordinates; earlier runs without actuator enable
  produced only startup reports. Enabling keyboard only after touchpad readiness and dimensions gives a
  combined capture of 16 keyboard reports and 1791 touch frames, including
  two- and three-contact frames, button transitions and pressure changes.
  Enabling keyboard before touchpad startup produced neither input stream.
  Both final touchpad Off replies, whole-MTP AP/IOP quiescence and DART/DAPF restoration pass with the actuator enabled.
  The human tester confirms normal haptic clicks during touchpad-only and
  combined tests; no host actuator output reports are sent. Separate actuator
  output control remains unqualified.
* SEP's existing RNG routine returned zero bytes. Successful SEP communication
  is not established.
* ISP revision `0x100003` was read after powering the ADT parents and the
  ISP_CPU/ISP_FE global slots at offsets 0x4000/0x4008. All tested domains
  returned to their original states, including ISPSENS0 state 4. A separate
  readback trial confirms ISP_SYS and ISPSENS0 reach active mode through the
  existing ADT parent traversal; missing parent power is not the cause in this
  sequence. A guarded proxy trial now receives the first firmware handshake
  with full 42-bit SID0 mappings and native stream aliases: SIDs 1, 6, 7, 10
  and 11 use TCR `0x80` on all three DARTs, with enable mask 1. The previous
  trials left these streams in separate contexts with empty tables. The
  firmware announces seven channels, queue offset `0xef40`, descriptor flags
  2 and an extra-heap request of `0x6500000` bytes. Its bootstrap metadata
  changes, with zero DART faults and CPU exceptions. Watchdog recovery returns
  to macOS after a one-time m1n1 boot, or to CDC after m1n1 is selected as
  default. The modern `0x290`-byte boot block is accepted. All seven channel
  descriptors parse with 64-bit ring addresses in the owned IPC allocation,
  and the requested `0x6500000`-byte heap is mapped on all three DARTs.
  Type-0 ring initialization and the third boot handshake also pass, with
  zero faults. The owned allocation broker and CONFIG_GET pass. Native
  T604x DSID configuration, START, IMX958 identification and all 13 preset
  queries also pass. RAW receiver configuration and buffer submission are
  acknowledged, and CH_START produces fresh pixels in two owned surfaces.
  A later RAW trial acknowledges CH_STOP while servicing shared allocations,
  buffer returns and bounded terminal batches. Two complete 4,644,864-byte
  reads after stop match byte for byte and produce a diagnostic camera image.
  No completed RAW buffer report was received; stable memory after stop does
  not qualify frame completion or continuous capture. Per-module lens-shading
  queries return a valid 19-by-17, 8-bit grid. Its mapping to the RAW crop is
  unqualified. A private processed-preview trial enables the native local RAW
  allocation path and receives two completed pool-9 output reports. The native
  firmware exports zero-extended 32-bit plane addresses; each report matches
  one submitted surface uniquely by both addresses, plane count, pool and tag.
  The 1280-by-720 output uses 2560-byte rows and MSB-aligned 10-bit samples in
  two planes. Two complete reads of one returned surface match before reuse,
  and CH_STOP acknowledges. Video-range BT.709 rendering produces a coherent
  colour image. A bounded reuse trial returns six distinct frames across
  three generations of both output surfaces. Fresh tags identify each lease;
  completion acknowledgment precedes reads, and two matching full reads
  precede resubmission. Metadata refill, CH_STOP, zero DART faults and zero
  CPU exceptions also pass. The private capture consumes shared profile,
  boot-block, lease, mapping, startup, broker and command components. The
  mapper verifies 42-bit DART geometry, read-only TEXT, stream aliases and
  table readbacks before launch. Startup configures DAPF and GPIO, completes
  both handshakes, publishes the boot block and validates channel ownership.
  Phase order, fresh CONTROL/GPIO checks and attempt latches prevent repeated
  starts; failure retains resources. The broker rejects allocation flags
  before forwarding and retains retired storage. Command submission validates
  ownership and deadlines before publication, rejects late ACKs and captures
  raw replies before status checks. Shared sensor discovery identifies IMX958
  with 13 presets and bounds preset queries. A live capture through these
  components returns six full stable P010 frames with clean stop and fault
  checks. Shared T6040 preview configuration validates the IMX958 replies
  before issuing the tested P010 setup sequence and rejects retries after
  an ambiguous failure. A live capture through this component returns six
  full stable frames, clean stop, zero faults and a passing NOP.
  Power/residency qualification and capture pool/stream orchestration
  remain private. Capture wire builders validate pool/plane fields, 42-bit
  submitted addresses, opaque tags and report bounds. Their bytes match
  saved live submissions; they do not establish mapping or lease ownership.
  A later three-generation run returned four stable frames, then timed out
  with both output leases submitted and the current completion slot idle.
  Servicing control traffic during downloads received all six reports, but
  the host time limit interrupted the final copy. Neither run qualifies
  reliable streaming or a stall fix. A follow-up services at most one
  terminal message between pixel chunks while retaining the eight-message
  quota for normal polling. It completes six full stable frames, CH_STOP,
  zero DART faults and zero CPU exceptions, with NOP passing. Sustained
  reuse with this schedule also completes 80 frames: every frame has two
  matching 4096-byte sample reads, and the first/last surfaces have full
  matching double reads. Both rings wrap; CH_STOP, zero faults and NOP pass.
  This bounded run does not establish long-term reliability or sensor FPS. An earlier
  40-generation trial timed out at CH_START before returning frames; its
  command payloads and first 96 allocation requests match the successful
  bounded run. Timing of host allocation service remains under investigation.
  Watchdog recovery returned the installed proxy with NOP passing.
  A subsequent instrumented run completes 80 frames across 40 generations,
  wrapping both buffer rings. All 80 returned surfaces pass two matching
  4096-byte sample reads; the first and last generation of each surface also
  pass two matching full reads. CH_STOP, zero DART faults, zero exceptions
  and proxy NOP pass. Host request servicing consumes about 26 seconds of
  the 30-second startup deadline, plus about three seconds of terminal
  draining. Reliable startup timing, real-time frame rate, colour calibration
  and the complete production camera interface remain unqualified. A combined RAW/YUV trial exceeds
  the retained shared-memory aperture and refuses the allocation before
  mapping it. Watchdog recovery remains required after these private probes.
  The captured boot firmware is `mBoot-18000.161.9` from macOS 26.6.1 (25G76).
  Its ISP UUID and segment ranges match that restore image; macOS 26.6.2 and
  the installed macOS 27.0 ISP image have different UUIDs. The version table
  recognizes 26.6.1; other ISP firmware profiles remain unqualified.
  Host regressions cover initialization followed by shared-buffer allocation.
  Surface allocations now start beyond the fixed firmware mappings, preserving
  the configured IOVA bounds and a guard page. Captured J616s segment sizes
  combined with a synthetic legacy extra-heap request reproduce an overlap
  with the old allocator start. The live T6040 extra-heap request is larger
  than that synthetic request. The guarded boot-block trial qualifies its
  allocation and mapping; combined RAW/YUV capture remains unqualified.
  Required boot-argument and channel-message imports are explicit.
  Channel descriptors retain their 64-bit ring addresses at offset 0x50.
  The matching firmware writes that field with 64-bit stores in all seven
  0x100-byte records. Host tests cover legacy addresses and addresses above
  1 TB; live descriptor reads now confirm these high ring addresses.
  A subsequent cold 1920x1080 P010 trial queues one owned pool-9 surface
  while keeping native pool capacity two, the 256 MiB shared aperture and
  retained-allocation budgets unchanged. One uniquely matched completed
  report arrives; both full 6,242,304-byte reads match before CH_STOP.
  The stride is 3840, with aligned Y/UV extents 0x3f8000/0x1fc000. All three
  DART errors and CPU exceptions are zero; NOP and watchdog recovery pass.
  This qualifies one completed 1080p frame, not continuous 1080p streaming,
  restart, exposure/noise calibration or measured FPS.
* All six speakers have user-confirmed individual tone output. Feedback,
  calibration, simultaneous full-array and continuous playback remain
  unqualified; the separate headphone path also has finite output.

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
A later ISP power probe matched the raw code but hit a synchronous exception
and lost CDC. Its failing step was not preserved before cleanup; the cause
remains unconfirmed. A later probe called matching raw-ELF symbols at their
native addresses and repeated ISP parent/CPU/FE power, revision readback and
state restoration without exceptions. A guarded follow-up fails while reading
the older ASC control offset 0x1400044, after the revision read succeeds. The
watchdog automatically restores a responsive installed-stage-1 proxy. This
offset is unqualified for T6040; do not use the older ISP startup sequence.
Hardware watchdog recovery now protects these experiments.


## Hardware watchdog recovery

`p.wdt_arm(seconds)` arms the primary watchdog on the tested T6040 or T8140
version-2 layout. It accepts 1-178 seconds, verifies both ADT apertures and
requires the secondary watchdog to be inactive. The proxy checks the full
64-bit argument before narrowing it. The helper returns zero after alarm and
control readback, or -1 if validation or readback fails. A readback failure
occurs after programming and does not guarantee that the watchdog is disarmed.
`p.wdt_disable()` uses the existing watchdog disable routine. J616s startup
does not arm it; experiments must arm it explicitly. The existing J700 CDC
startup policy still arms its watchdog.

On J616s, counter measurements confirm a 24 MHz clock. A six-second alarm
reset the target after 6.12 seconds and returned CDC in about 15 seconds.
A second test deliberately stalled the CPU in an infinite loop; the watchdog
again reset the target and returned a working proxy in 14.83 seconds. Both
returned to the installed stage 1 without changing installed images. An AOP
microphone query/attach probe also recovered automatically after its shutdown
ACK timed out. Keep the watchdog armed when cleanup cannot establish IOP
quiescence; reboot before creating another proxy heap or reusing DMA memory.

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
interfaces; keyboard enable follows touchpad startup. Add `--capture-seconds 45`
for a longer input window (1-60 seconds;
up to 1 MiB). It enables the announced multitouch and actuator interfaces,
sends no actuator output reports and makes no GPIO writes. Version-2 power
requests include
Off/On and WillChange/HasChanged phases; replies must echo the exact request
with the expected transfer counter and zero status. DeviceReady must arrive
after the On request. After capture, both Off phases must be acknowledged
before AP/IOP quiescence and register restoration. Failed or uncertain shutdown
returns `reboot_required` and retains the IOP, DART, DAPF and DMA allocations
until reboot. Control requests require an empty receive FIFO before transmission;
continuous input may cause a safe refusal. FIFO reads use bounded native
batches of at most 4096 bytes; capture finishes the current packet within an
additional one-second deadline. Reboot before each probe run. No firmware blob
is included in this PR.

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

An ASC address-range precedence error treated addresses above the DVA window
as physical even when `allow_phys` was false. This made the AOP crash decoder
read its firmware virtual stack as host physical memory, producing additional
exceptions and obscuring the firmware failure. Physical bypass now requires
explicit opt-in on either side of the DVA window; other addresses use the
configured DART and address mask. Host cases cover reads, writes and translation
for addresses below, within and above the window with both opt-in settings.
