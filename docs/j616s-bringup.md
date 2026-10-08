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
  HP source configuration remain unqualified.
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

  A guarded prototype maps the ADT-selected ADMAC stream 10 with four-level
  42-bit tables and submits one non-repeating 16 KiB RX descriptor only after
  `hpai` reaches `pwrd`. The split RX bank at offset 0xc000, completion report,
  zero residue and zero DART fault are observed; the sentinel buffer becomes
  all zeros. RX stops and the frontend returns through `pw1 ` to `idle`; the
  proxy remains responsive. This tests input DMA but does not establish live
  microphone audio. Source configuration and PCM format remain unqualified.
  `hppx`, `lcpx`, `mcpx` and `smpx` state queries track each tested frontend
  transition, including `pwrd`. Attaching `hppx` succeeds, while attaching
  `lACp` returns 0xe00002e2 (not permitted); that trial stops before power
  or DMA requests.
  With LP active, all 30 public `hppx` property queries pass across HP
  attachment and power transitions. Properties 500, 501 and 600 change from
  zero to one at `pwrd` and return to zero at `pw1 ` and `idle`, confirming
  native AudioControl enable in this sequence. Property 502 remains zero;
  property 503 remains two. Property 600 returns 224 bytes because its native
  getter leaves the response length unchanged; only its four-byte Boolean
  prefix is qualified. No HP DMA is submitted in this status probe, and these
  results do not establish PCM delivery or preset application.
  Cleanup must stop RX before returning the frontend to `idle`; an earlier
  prototype accessed ADMAC again afterward and lost CDC.
  The Python ADMAC driver selects split channel banks for `admac,t604x` while
  preserving logical channel IDs, FIFO ports and global enable bits. A live
  RX0 trial uses its bus-width, frame-size, carveout and burst-size accessors,
  descriptor submission and report reader, with the same completion and
  all-zero-buffer result. Channel startup configuration and final peripheral
  teardown remain in the prototype; this change does not qualify stream
  restart or select the DART stream automatically.
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
  CPU exceptions also pass. The private capture uses the shared profile,
  boot-block encoder, buffer leases and owned transport. The shared firmware
  mapper also passes a six-frame live trial: 42-bit mappings, read-only TEXT,
  stream aliases and table readbacks qualify before firmware startup. It
  rejects a running processor before writes and retains resources on failure.
  The explicit T6040 launch entrypoint now configures DAPF, verifies native
  stream contexts, initializes GPIO and completes the first firmware
  handshake in a six-frame live trial. It enforces phase order and refuses
  repeated starts or changed CONTROL/GPIO before writes; faults and timeout
  retain resources. The same entrypoint now completes the second handshake,
  maps and publishes the boot block, and validates the channel table in
  another six-frame live trial. Power/residency qualification and capture
  configuration remain private. The shared allocation/terminal broker also
  passes six full stable frames. It rejects allocation flags before forwarding,
  retains retired storage, and prevents retry after an ambiguous handoff.
  Shared command polling checks deadlines before and after reads, rejecting
  late ACKs. A live run returns both processed reports and saves one full
  P010 frame with CH_STOP, zero faults and proxy NOP passing.
  The shared IO command dispatcher and owned payload write then pass six
  full stable P010 frames, clean stop and fault checks. Named camera
  sensor discovery and preset queries now use the shared T6040/IMX958
  sequence as well: 13 presets and six full stable frames pass live. Capture
  pool and stream configuration remain private. A subsequent
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
  recognizes 26.6.1; this does not qualify an ISP startup profile.
  Host regressions cover initialization followed by shared-buffer allocation.
  Surface allocations now start beyond the fixed firmware mappings, preserving
  the configured IOVA bounds and a guard page. Captured J616s segment sizes
  combined with a synthetic legacy extra-heap request reproduce an overlap
  with the old allocator start. The live T6040 extra-heap request is larger
  than that synthetic request. The guarded boot-block trial qualifies its
  allocation and mapping; camera operation remains unqualified.
  Required boot-argument and channel-message imports are explicit.
  Channel descriptors retain their 64-bit ring addresses at offset 0x50.
  The matching firmware writes that field with 64-bit stores in all seven
  0x100-byte records. Host tests cover legacy addresses and addresses above
  1 TB; live descriptor reads now confirm these high ring addresses.
* No speaker output or speaker amplifier programming was performed. Only
  the input-only ADMAC prototype described above submitted an audio descriptor.

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
