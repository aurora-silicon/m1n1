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
the current ADT. Use four-level 42-bit DART mappings and preserve other streams.
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

## Other implementation boundaries

CPU states 1/2, secondary MMU/dispatch and shared-memory coherence have live
evidence; higher states and sleep/hotplug do not. Reuse existing upstream SMP
fixes rather than duplicating them. MTP initialization, keyboard, touchpad and
haptic click paths have their ACK/readiness contracts in the bring-up document.
SEP communication and T6040 SuperSpeed CDC remain unqualified. No functional
driver recipe for either follows from a nearby SoC or a successful register read.
