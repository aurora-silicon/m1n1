# J616s driver contracts

Use this as the implementation checklist. [Bring-up evidence](j616s-bringup.md)
records the tests and limitations behind these contracts. A successful control
reply is not evidence of samples or completed frames.

## Admission and ownership

The tested target is Mac16,7/J616s, chip 0x6040, board 6. The recent proxy
captures use OS firmware 26.6.1, AOP RTKit version 12 and EPIC message/subheader
version 2. Native macOS 27/build 26A428 captures are separate reference evidence;
do not assume their firmware or phandles match the proxy boot.

Resolve registers, DMA parents, mapper streams and firmware segments through
the current ADT. Read the active DART mode before using an inherited context
and preserve its tables and other streams. The tested new AOP/SIO mappings
use four levels and 42-bit addresses; inherited DCP stream 23 retains its
36-bit mode and captured root/leaf tables. SoC capability does not authorize
changing an active firmware context. A complete read-only capture covers its
root and five leaves; this does not qualify cache publication or invalidation
for that stream. Its parameter register reports version 2.2, not version 3.
Clean all new page tables, execute the publication barrier, invalidate the
affected stream and verify the entire translation before enabling its DMA.
Map firmware text read-only. These are owned mappings, not permission to map
an adjacent aperture or copy another SoC's addresses.

Arm the validated hardware watchdog before starting an IOP or publishing DMA
memory. Bound waits and check exceptions and DART faults. Record an operation
as pending before handing ownership to hardware. On ambiguous failure, stop
issuing commands and retain the allocation, tables and IOP until recovery.
Do not free memory merely because a command timed out. AOP shutdown does not
yet provide a reliable ownership-release acknowledgement.

## Audio transport and mappings

Use the advertised `aop-audio` service on endpoint 0x22 after application queue
startup. `AudioAttachDevice`, `GetDeviceProp` and `SetDeviceProp` are defined in
[ipc.py](../proxyclient/m1n1/fw/aop/ipc.py). Calls use EPIC version 2. Reject
nonzero return codes and malformed reply lengths before interpreting payloads.

| Path | Tested mapping | Sample contract |
| --- | --- | --- |
| AOP firmware and endpoint buffers | DART stream 0 | External code boot argument uses the ADT remap address |
| LP warm-up ring | AOP DART stream 8 | 768,000-byte ring; producer report has 12-byte frames |
| HQ microphone | `admac-leap-ns`, mapper stream 10, logical channel 1 | Split RX0 bank 32; three-channel Float32LE, nominal 48 kHz |
| Headset microphone | `admac-base-ns`, mapper stream 8, RX2 | Mono 24-bit samples in 32-bit slots, nominal 48 kHz |

LP and headset stream-8 results come from separate tested sequences. They do
not establish concurrent ownership of that stream. J700's secure LEAP provider
and advertised proxy aperture are not J616s's DMA assignment.

### LP readiness required by the tested HQ sequence

1. Require `hpai` and `lpai` state property 200 to report `idle`, and `adpx`
   property 300 to report `(-1, 0)`. Stream 8 must be disabled with an invalid
   TTBR; idle frontend state alone does not establish memory ownership.
2. Allocate a zeroed 770,048-byte, 16 KiB-aligned backing buffer for the
   768,000-byte ring. Publish and verify its stream-8 mapping.
3. The tested mapping is identity: PA equals IOVA. SET `adpx` property 300
   with `<QQ` little-endian PA and 768,000. GET must return that same address
   and exactly 768,000. This setter can expose the
   address before later validation; retain ownership on every attempted SET.
4. Attach `lpai`, `pdm0` and `adpx`, and recheck the published buffer.
5. Request `lpai` `pw1 ` with `unk2=0`, attach `lai `, then request `runn` on
   `lai ` and `lpai` with `unk2=1`. Check both state readbacks. Preserve spaces
   in FourCC strings; use the 48-byte `PowerSetting` encoder for raw `lai ` data.
6. Observe a fresh REPORT/subtype 0x20 after arming the report handler. The
   tested payload is 0x68 bytes, starts with words 0x6c706169/0xc3000008, and
   contains a little-endian u64 counter at 0x40 and cursor at 0x60. Validate
   `cursor == ((counter + 1) * 12) % ring_bytes`.

Keep LP running and retain its ring throughout the tested HQ capture.
The last check establishes a producer notification, not an HP completion.
Do not substitute cached `hppx` enable flags for it. The legacy 631-byte PDM
configuration does not fit the observed 626-byte property; no new coefficient
upload was required by the working HQ capture.

### HQ one-shot capture

The public command is now qualified for both owned capture extents on the
pinned cold profile. From a recent compatible RAM proxy with cold AOP state:

```sh
M1N1DEVICE=/dev/ttyACM0 python proxyclient/experiments/aop_capture.py /tmp/hqai.wav
```

The default is 6 MiB (nominally 10.92 seconds); `--bytes 0x200000` selects the
qualified 3.64-second capture. It saves three-channel Float32 WAV, raw bytes,
metadata and a normalized single-channel PCM16 preview. Existing output stems
are refused before opening hardware. The preview records its gain/channel;
normalization is not calibration or proof of recognizable sound. Wait for
watchdog recovery before another capture. Changed firmware fingerprints,
RTKit versions, service identities or layouts fail closed.

Attach `hpai` and request `pw1 `. Require stream 10 to be unused and disabled
before installing its owned mapping. Prepare one owned non-repeating NS RX0
descriptor, start DMA, then request `hpai` `pwrd` with `unk2=1`. Check its
property-200 readback. The tested channel configuration is 32-bit bus width,
one-word frame, SRAM carveout `(0x800, 0x800)` and burst word `0xc00060`.
Use [admac.py](../proxyclient/m1n1/hw/admac.py) rather than recreating split-bank
address arithmetic.

Tested descriptor extents are 2 MiB and 6 MiB. The latter contains 524,288
complete three-channel frames, nominally 10.9227 seconds. Both cold captures
begin with 7,211 zero frames, about 150 ms. A 16 KiB transfer is shorter than
that interval at this format; initial silence is not evidence of a dead path.

Require the matching descriptor ID, zero residue, and descriptor/report rings
with EMPTY=1, FULL=0 and ERR=0. Other ring bits are not qualified error flags.
Require zero DART faults. Invalidate the complete owned buffer before each
download and
obtain two matching full copies while HP remains `pwrd`, before cleanup. Retain
partial-frame bytes in raw evidence; the 2 MiB extent has an eight-byte tail.
After copying, stop RX and reset its rings before returning HP through `pw1 `
to `idle`. Clock accuracy, continuous streaming and restart remain unqualified.

Keep the original three-channel float data. A mono average with software gain
is a listening preview, not calibration or beamforming. Never decode these
float words as signed integer PCM.

### Headset input

Use [CS42L84Input](../proxyclient/m1n1/hw/codecs/cs42l84_input.py) for the tested
input-only codec lifecycle. The caller owns board power/reset, the fresh
109-byte MCA profile, DMA preparation and receiver start/stop. The documented
`MIC_DET_CTL4.LATCH_TO_VP` initialization is required by the working capture;
matching the 32-bit TX framing alone was insufficient. Stop TX and RX before
restoring codec/controller state. This does not qualify playback or speakers.

### Headphone output reference

Native macOS 27 build 26A428 uses the separate `audio-codec-output` node
(phandle 442, DMA parent 455). Its current `coS ` profile selects `ms02` TX,
rising edge, delay 1, two channels with mask 3, 24-bit samples in 32-bit slots,
48 kHz and a 3.072 MHz clock. The TX routing mask is 4. A finite proxy test
completed stereo DMA and the user heard the separate left/right tones.
Simultaneous input/output and continuous playback remain unqualified.

The native CS42L84 setup routes `BUS_DAC_SRC` at 0x4001 to 0xed: DAC A takes
ASP RX channel 1 and DAC B takes channel 2. It enables both ASP RX channels
at 0x5020. The output DMA records use ID 4; input records use ID 5. Bind the
controller's direction and channel selection before submitting output DMA.

The native `PrepareIo` output branch switches away from RCO, configures the
common TDM bus, configures headphone routing and slots, then powers the
headphone path. Its post-DAC call through hardware field 0x48 notifies Mikey
headset detection (`mikeyChangingHPOUTEnabled`); it is not an IIS clock-enable
call. Preserve shared clock ownership when testing input and output together.
Qualify left and right separately, then verify stop and restoration. The
tested two-second transfer used left 440 Hz and right 880 Hz, 20 ms fades,
PCM peak 0.25 and DAC attenuation of 60 dB. The matching report, zero residue
and DART status, muted DAC stop and codec/controller restoration all passed.
The user confirmed audible output; no acoustic calibration follows from it.

### Speaker amplifiers

The master `audio-speaker` node links six SN012776 amplifiers. Provider order
is left woofer 1, right woofer 1, left woofer 2, right woofer 2, left tweeter,
right tweeter. Their bus/address pairs are `i2c1:38`, `i2c3:3b`, `i2c1:39`,
`i2c3:3c`, `i2c1:3a`, `i2c3:3d`. All six returned page 0, shutdown mode
0x1a and identity 0x30 after releasing the shared AP GPIO reset at pin 0x36.

A bounded proxy test configured all six while keeping them shut down. Native
configuration includes DC blocking, over-power protection, board BOP bytes,
test-page writes, per-amp RX/I/V slots and TX zero-drive masks. The first
shutdown transition includes the native test-page 0x64 wrapper and a 1 ms
dwell. Configure calls `setOperationalMode(false, true)`, which selects
protected shutdown 0x82. It does not select muted operation 0x81.

The test set digital volume to 0xc9 before configuration and expanded native
broadcasts one amp at a time. All 534 byte writes completed. Final page,
mode, identity and volume readbacks matched 0/0x82/0x30/0xc9 on each amp;
each I2C controller and the shared reset GPIO were restored. No speaker DMA
or output was enabled. These results qualify communication and configuration,
not protection efficacy, feedback decoding or audible playback.

The native `spkr` / `spS ` profile uses MCA group 0 and `ms00`: six 24-bit
playback samples in 32-bit slots, nominally 48 kHz. Feedback uses twelve
16-bit slots; its native sample-width field is zero, so the decoded sample
format remains unqualified. The ADT selects ADMAC TX0/RX1 and mapper stream
8. A subsequent cold control test attaches `spkr`, checks its exact 109-byte
profile before and after power changes, and returns it through `pw1 `,
`pwrd` with output flag 2, `pw1 ` and `idle`. All state readbacks match.

A finite zero-data TX0 test then completes 1,152,000 bytes using a dedicated
2 MiB stream-8 allocation at IOVA 0x80000000. Both complete payload reads are
zero before submission. The AP policy selects the supplied 32-bit DMA record,
preserves FRAME and the firmware-published adapter settings, and uses an owned
0x480-byte FIFO with 0x240/0x480 transfer/almost-full thresholds. The matching
report, zero residue, empty rings, zero DART faults, TX configuration restore,
frontend idle and watchdog recovery pass. All amps remain held in reset.
This qualifies finite silent DMA. A subsequent left-woofer pulse has audible
user confirmation; feedback remains untested.
Audio channel count alone does not establish an ADMAC FRAME encoding.

The tested left pulse enables only native slot 0 / `i2c1:38`, using 300 Hz,
peak 0.125, 20 ms fades and DVC 0x78 (60 dB attenuation). The native protection
configuration precedes muted mode 0x81 and a measured minimum 33 ms dwell.
Fresh profile, power, owned payload/translation and DMA-readiness checks precede
active mode 0x80. The matching completed report triggers mute before TX stops
or restores its fields. Cleanup restores minimum volume, uses the native
test-page 0x64 shutdown wrapper and 1 ms dwell, then restores controllers/GPIO.
The measured active interval is 1.219 seconds for the one-second payload.

The right diagnostic enables only slot 1 / `i2c3:3b`, with full protection on
that amp and the other five left in verified shutdown 0x1a. Its finite transfer,
cleanup and recovery pass; a repeat has user-confirmed audible output. DVC 0x65
is about
the midpoint of the documented inverted 201-step mixer range, not a calibrated
macOS volume-slider percentage. Full-array setup still has intermittent I2C
completion failures. Keep uncertain state for watchdog recovery and retain
XEN completion/error checks; unknown status bit 29 is not an acknowledgement.

A five-second paired first-woofer test at that codec setting is user-confirmed
audible. It uses an owned 6 MiB output allocation for 5,760,000 payload bytes.
The matching report, zero residue/DART faults, both post-report mute writes and
TX configuration restoration pass. A later I2C timeout in the amplifier
shutdown wrapper prevents complete cleanup; resources remain retained until
watchdog recovery. This qualifies paired playback, not reliable repeated
cleanup, all-six output, continuous streaming or feedback calibration.

All six drivers now have user-confirmed individual tone output. The second
left/right woofers use their native RX slots 2/3 and 300 Hz; the left/right
tweeters use slots 4/5 and 2 kHz. Every new test is limited to one second with
peak 0.125 and 20 ms fades. Full protection is reapplied to the selected amp;
the other five remain in their freshly verified factory shutdown state.
Matching TX reports, zero residue/DART faults, mute before TX stop, protected
shutdown, exact controller/GPIO restoration and watchdog recovery all pass.
DVC is 0x65 for the second woofers and left tweeter, 0x78 for the right tweeter.
These levels are finite test settings, not calibrated system volume.

The Python I2C helper clears only the observed status snapshot and rejects XIP
before writing it. Its direct calls are qualified on both idle speaker buses;
private bounded amp trials use the same clear policy with their stronger
ownership, error and XEN/no-XIP completion gates unchanged. The existing generic
transfer methods still reset FIFOs before this check, so the helper change
alone does not make their entire operation safe for concurrent clients.
Successful shutdown and four new playback cleanups do not establish that the
intermittent STOP fault is resolved. I/V decoding, feedback framing, acoustic
calibration, simultaneous all-six and continuous operation remain unfinished.


## ISP completed colour frames

Compose the existing [startup](../proxyclient/m1n1/fw/isp/isp_startup.py),
[sensor](../proxyclient/m1n1/fw/isp/isp_sensor.py),
[capture](../proxyclient/m1n1/fw/isp/isp_capture.py) and
[buffer](../proxyclient/m1n1/fw/isp/isp_buffer.py) components. The profile pins
the firmware/ADT layout. Check IMX958 identity and complete preset replies
before selecting the render route.

The repeated-capture tests use 1280x720 P010: two-plane YCbCr 4:2:0 with MSB-aligned
10-bit values in little-endian 16-bit words, stride 2560. Its aligned Y/UV
extents are 1,851,392/933,888 bytes, totaling 2,785,280. Use the returned layout,
not tightly packed width/height arithmetic. This is the driver's aligned
`output_geometry`, not an independently returned firmware allocation size.
BT.709 video-range conversion in
[isp_pixels.py](../proxyclient/m1n1/fw/isp/isp_pixels.py) produces a display PNG.

A cold 1920x1080 test now receives one completed P010 frame. Its stride is
3840 and aligned Y/UV extents are 0x3f8000/0x1fc000, totaling 0x5f4000.
Both full reads match; CH_STOP, zero faults and NOP pass. The trial queues one
owned output while retaining the native pool capacity of two and existing
allocation limits. This qualifies a completed still frame, not continuous
1080p or measured FPS. Repeated two-surface tests remain qualified at 720p.
The current profile advertises a 256 MiB shared aperture; the broker separately
limits retained allocations to 128 MiB, 128 allocations and 32 MiB per request.
Increasing one budget does not establish that the other limits can be raised.

Receive a completed-frame report and match both plane addresses, pool, plane
count and tag to exactly one submitted owned surface. Copy and validate before
requeue. Continue servicing allocation and terminal messages during bounded
downloads; an ACK or stable memory after stop does not establish completion.
Require CH_STOP and zero faults for clean stream stop. This does not qualify
firmware shutdown or allocation release: tested clients retain firmware
mappings, broker allocations and buffers until watchdog/cold recovery, even
after CH_STOP. Retain leases on ambiguous failure.
Intermittent streaming stalls, exposure/noise calibration and measured FPS
remain open. Do not implement guessed opcodes to compensate for a stall.

## PHY processor prerequisites

T6040 SuperSpeed remains unqualified. The saved PHY0 ADT references phandle
102, the RTBuddy-v2 nub under `/arm-io/acio-phy-cpu0`; its parent has phandle
101 and compatible `iop,mxwrap`. Preloaded/running properties describe
firmware metadata, not current execution or endpoint readiness.

The captured native MXWrap implementation uses these register-bank offsets:

| Register | ADT bank | Offset |
| --- | --- | --- |
| Inbox control | 0 | `0x110` |
| Outbox control | 0 | `0x114` |
| Inbox data, 16 bytes | 0 | `0x800` |
| Outbox data, 16 bytes | 0 | `0x830` |
| CPU run request | 1 | `0x28` |
| CPU idle status | 1 | `0x44` |

Bank 0 is `0x383148000`, bank 1 is `0x383140000`, each with a `0x4000`
aperture. Standard ASC mailbox offsets based at bank 1 coincide with the
native mailbox addresses, but its CPU-control/status layout differs.
Do not use generic ASC boot/shutdown on this processor.

One guarded passive snapshot found AFI, FAB2_SOC, ATC0_COMMON, ATC0_USB_AON
and ATC0_PHYMXWRAP power dependencies active. Both mailbox control words
returned zero without CPU exceptions; no FIFO data was consumed or message
sent. This does not establish a usable mailbox. Native outbox enable changes
control bit 0; mailbox initialization, processor ownership, firmware startup
and the subsequent PHY/PIPE transition still need qualification.

A separate guarded cold test applied native small-sleep and then big-sleep
requests. PHY reset control changed `0x4 -> 0x5 -> 0x7` and status changed
`0x100 -> 0x101 -> 0x103`, with zero CPU exceptions and successful cold
watchdog recovery. The clamp remained set. This qualifies those ordered
request/acknowledgment transitions only; host observation latency does
not establish the native one-millisecond poll timing or USB3 readiness.
A subsequent cold test released the clamp, with control `0x7 -> 0x3`, both
sleep acknowledgments retained and status still `0x103`. Readback and cold
recovery passed without CPU exceptions. Reset-busy remained set; no reset
request, firmware start or mailbox command was performed.
A following cold test cleared bit 0 of PHY0 register 39 offset `0x28`
(`0x383200028`), changing the request word `0x1 -> 0x0` and status
`0x103 -> 0x3`. Both sleep acknowledgments remained set; request/control
readback and cold recovery passed without CPU exceptions. This PHY register
is distinct from the processor's bank-1 CPU run request. Firmware startup
and USB3 readiness remain unqualified.
The native phase-A final control-bit write also passed: `0x3 -> 0xb`, with
status `0x3`, request word zero and successful cold recovery. These register
transitions do not qualify subsequent PLL tunables, crossbar programming,
firmware startup or PIPE/controller handoff.

A guarded ACE3 query of HPM0 logical status register `0x1a` returned
`0x1000b40d`: plug present and orientation bit 4 clear. Controller FIFOs were
empty before and after, and CPU exceptions stayed zero. The query used only
logical-register selection and reads, with no HPM wakeup/shutdown, role or VDM
commands. Recheck orientation after a reconnect; this snapshot does not prove
SuperSpeed cable capability.

A separate guarded HPM0 query of logical data-status register `0x5f`
requested up to 64 bytes and returned four: `f3 00 00 80`. The returned
length is authoritative; the remaining buffer was not read or padded.
Controller FIFOs were empty before and after the single query, and the
proxy completed it without CPU exceptions. Natural watchdog recovery
returned to the installed proxy and retired the temporary allocations.
This tests the ACE3 variable-length read path. The raw reply has not been
bound to native status fields or the PHY processor's tunables-ready bit;
it does not establish SuperSpeed readiness or enumeration.

A later guarded RAM test traversed all 164 compact records in the nine
present native mode-1 tuning tables, then applied the three crossbar writes,
common sleep override and eight override clears for each lane. All 20
crossbar/sleep-control masked readbacks matched, with zero CPU exceptions and
a responsive proxy. Cold watchdog recovery returned the installed proxy
without a physical restart. Each sleep override used the native one-microsecond delay;
extra proxy checks and readbacks mean this was not an exact native timing test.

The tuning helper skips unchanged commands and does not check retained bits
after writing. The experiment kept additional readback observations: 52
commands were unchanged, 110 writes matched and two differed only in bit 31
at AXI2AF offsets `0x40` and `0x48` (`0x80000016 -> 0x16`). These differences
remain unexplained; they are not proof of a strobe or successful hardware
configuration. Traversal completion does not establish PLL lock, processor
firmware startup, PIPE readiness or SuperSpeed enumeration. Crossbar selection
used a fresh HPM status query (`0x1000b41d`, orientation bit set), rather than
the older cable's saved orientation. No VDM or role command was sent.

The native USB interface uses mode 4. A later mode-4 test added its nine
fixed helper RMWs after the common/USB tables and reapplied the six AUSPLL
records before crossbar setup. All nine fixed masked readbacks matched; the
AUSPLL values were already unchanged. Modes 1 and 4 share the crossbar branch,
but mode 4 releases only the orientation-selected lane. All 12 crossbar/common
and selected-lane readbacks matched. Native RUN again produced a complete
RTKit version-12 Hello, with clear CPU exception checks and NOP. The firmware
tunables-done flag remained clear throughout the one-second window (`0x881`), so this does
not establish PLL lock, a ready PHY or a working SuperSpeed gadget.

A subsequent cold RAM test applied the native MXWrap RUN request `0x10` to
processor bank 1 offset `0x28`, after checking prepared control `0x2`, firmware
enable `0xa0003` and both enabled/empty mailbox controls. RUN readback was
`0x10`, processor status was `0x14e` and one outbox message became available.
One complete receive using upstream ASC word ordering decoded as endpoint 0
RTKit Hello, supporting version 12 only. Both raw words were recorded; the
outbox returned to empty, CPU exception checks stayed clear and NOP passed.
No Hello acknowledgment or other firmware command was sent. The firmware tunables-done flag
at `0x383000260` remained clear (`0x881`) throughout the one-second polling window.
This establishes first-message firmware communication, not full RTKit startup
or SuperSpeed. Native IRQ-provider startup remains outside this polling test.
A later HelloAck and complete two-segment map exchange advertises wire
endpoints 0, 1, 32, 33 and 34. The native service name `ACIOPHY0Endpoint1`
refers to wire endpoint 32: its suffix is the wire number minus 31. It must
not be confused with wire endpoint 1. CIO80 application startup and the roles
of wire endpoints 33/34 remain unqualified.

The native timeout diagnostic names bit 10 at `0x383000260` as
`ACIOPHY_TOP_BLK_UC_CTRL_REG.UC_TNBL_DONE`. It must not be treated as a general
PHY-ready or PLL-lock indication. The native phase-C path can continue after
that timeout when its debug-policy bit 31 is clear, releasing PHY reset by
setting register 39 offset 0 bit 4 and delaying 100 microseconds. The live
kernel policy value is not established under proxy; this continuation has
not yet been tested. Recording a timeout and following that path would not
establish firmware tunable completion.

## Native Linux USB handoff

USB0 has two T8110 DART banks in the ADT. Two independent RAM-only Linux boots
completed root-shell commands over high-speed CDC with both providers in
the DWC3 `iommus` property, following the upstream T8112 model:

| Bank | Aperture | Stream in the tested Linux model |
| --- | --- | --- |
| 0 | `0x382f00000`, size `0xc000` | 0 |
| 1 | `0x382f80000`, size `0xc000` | 1 |

Both providers use interrupt 1623 and the same low 32-bit DMA window. DWC3
uses interrupt 1619. The existing Apple DART driver supports multiple
providers and shared interrupts; this experiment did not change that driver.
The result does not identify which transactions use each bank.

Before handoff, check both banks in the current proxy session. The tested
bank0 state had no protection or lock, no enabled streams and no valid TTBRs;
all 16 TCRs were 1. Check it again immediately before the vector. Retain the
existing bank1 SID1 and controller retirement checks. Linux's ordinary DART
probe resets the bank, so an old snapshot cannot authorize a later handoff.

The successful test used notification suppression and the existing endpoint
diagnostic controls. Normal CDC notifications, sustained transfers, ECM and
SuperSpeed still require separate validation. The test recovered through the
original hardware watchdog and changed no installed images or partitions.

## Other implementation boundaries

The Linux implementation draft is
[aurora-silicon/linux#231](https://github.com/aurora-silicon/linux/pull/231),
based on `aurora-wip`. It prepares ownership, profile and retirement checks
without enabling the unqualified camera or streaming audio paths. Use the
proxy evidence and route names above as implementation inputs; the draft's
host/build results do not establish native hardware operation.

CPU states 1/2, secondary MMU/dispatch and shared-memory coherence have live
evidence; higher states and sleep/hotplug do not. Reuse existing upstream SMP
fixes rather than duplicating them. MTP initialization, keyboard, touchpad and
haptic click paths have their ACK/readiness contracts in the bring-up document.
SEP communication and T6040 SuperSpeed CDC remain unqualified. No functional
driver recipe for either follows from a nearby SoC or a successful register read.
