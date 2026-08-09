# Apple USB4/TBT port contract (t6020/J414s)

Host-only status: 2026-08-03. Runtime remains disabled until every gate below
has an owner and rollback path. A USB4/TBT PHY-only transition is rejected.

## Pinned primary sources

- Asahi Linux active USB4/TBT work:
  `AsahiLinux/linux` `sven/tbt-wip`
  `5265e38457df79188be2e13870192a1d488831ac`.
  - `drivers/thunderbolt/apple.c`: Apple ACIO power/INIT, M3 RTKit, SRAM,
    NHI rings/IRQs, root-router VSE cable information, TB domain.
  - `drivers/usb/typec/tipd/core.c` and `tps6598x.h`: CD321x
    `STATUS`, `DATA_STATUS`, USB4 EUDO, TBT VDO, Type-C mux/switch ordering.
- Device-tree resource reference:
  `f0daf084ff8f5717ec92bff380c816f9fb991475` (t8103 ACIO/NHI).
- CD321x-to-router notification hook:
  `827a9bfedbc78d7ad56c0c286382fe15d70d4f95`.
- Asahi Linux integration comparison: `asahi-wip`
  `52bcdf4dedbd5c9523977b7ff2de4b32d5e6e7df` at review time. It still lacks
  a complete t6020 Apple USB4 host-router runtime.
- Asahi m1n1 ACIO DT fixups: `AsahiLinux/m1n1` `tbt`
  `3e781e2d2582c0c05d70a243d5fb40ad9e253d81`; main was
  `34925643ca627a95b193b43632fae09e270b175e` at review time. The tbt head
  corrects the tunable alias from `usb4_%d_rc` to `usb4_%d_acio`.

These pins are architecture/provenance inputs, not a claim that t8103
register constants are valid on t6020.

Both remote heads were rechecked directly on 2026-08-03 and remain exactly
`5265e38457df79188be2e13870192a1d488831ac` and
`3e781e2d2582c0c05d70a243d5fb40ad9e253d81`.

### Local T6020 Type5 reverse-engineering corpus

Authoritative local inputs under `/Users/dj/Developer/Asahi/research/atcphy`:

- `T6020_TYPE5_ACIO_HAL_CONTRACT_2026-07-29.md`, SHA-256
  `2f99767f10ccd898703593bd36a26496662a36ebf328739ce8cbce03878b4fb8`;
- `T6020_TYPE5_NHI_RING_TRANSPORT_2026-07-29.md`, SHA-256
  `76601a51a1d2de7443145cc12fedc0eca88e989f48045bbda5369e89ce580bc3`;
- `T6020_TYPE5_CONNECTION_MANAGER_TUNNELS_2026-07-29.md`, SHA-256
  `a6a97336539866d583d4755713e3535ad5dc6c41acff958299f9b4c073bfbf3b`.

All three were re-hashed on 2026-08-03 and match. The connection-manager
value was previously recorded here truncated to 63 hex characters; the
correct digest ends `bfbf3b`, matching the Mu-side pin.

DisplayPort tunnelling over the same Type5 router/path/hop machinery is a
deliberate later increment, not a separate stack. Its pins, recorded now so
the path allocator stays generic over path type rather than hardcoding USB3:

- `T6020_TYPE5_DP_TUNNEL_2026-07-29.md`, SHA-256
  `9e381e9575331822017f75676ee5a4f4c7602c473cbdf20193a7d92fd761d7f0`;
- `T6020_DISPLAY_CROSSBAR_DPALT_2026-07-29.md`, SHA-256
  `3aaca52aa833e7c001c76f2631af806cfc428eb1483904ed9c3cf6d49aa74849`.

These are offline static RE of an unstripped Apple BootKC cross-checked with
the live J414s ADT. They supersede t8103 analogies for the Type5 register and
ring contract.

## Authoritative J414s topology

Source: private ADT capture SHA-256
`93d96b4a3ea736288278606b723f263361c6ae6c3d5c4f24f08f6f7a73f4b66e`.

There are three one-to-one port complexes:

| HPM | Physical port | ACIO | DART | ACIO CPU |
| --- | --- | --- | --- | --- |
| hpm0/rid0 | left-back | acio0 | dart-acio0 | acio-cpu0 |
| hpm1/rid1 | left-front | acio1 | dart-acio1 | acio-cpu1 |
| hpm2/rid2 | right | acio2 | dart-acio2 | acio-cpu2 |

Each ACIO declares six MMIO regions, 24 NHI interrupts (12 TX + 12 RX), three
power gates, four clock gates, a t8110-generation DART, a 1 MiB NHI aperture,
RC/NHI/PCIe-adapter tunables, a 76-byte DROM, and an `iop,mxwrap-acio` CPU with
an `iop-nub,rtbuddy-v2` child. The nub's preloaded SRAM segment maps IOP IOVA
`0x10000000` to a distinct physical SRAM aperture. This matches the Linux
ACIO RTKit shared-memory rule but must be translated; the IOVA is not a CPU
physical address.

Live read-only ADT inspection on J414s resolved `acio1/iommu-parent` to the
single phandle 125, `/arm-io/dart-acio1/mapper-acio1`, whose mapper `reg` is
SID 1. This selects Type5 shared-mapper partition mode 0 for the left-front
router. The implementation remains device-agnostic and accepts only the three
Apple layouts established by `determineSIDPartitioning`: 1 shared mapper,
2 direction mappers, or 24 per-ring mappers. Any other count fails closed.

The local `enableDART(bool)` disassembly resolves the invocation contract for
`function-dart_force_active`: Apple calls the cached `AppleARMFunction` with a
pointer to a 32-bit value (`1` before enabling DART, `0` while disabling) and
zero auxiliary arguments. On `acio1` the function is `Fact` on
`/arm-io/dart-acio1`.

The underlying platform action is now decoded too, and it is **not** an ADT
op-stream. `Fact` is a FourCC selector (`0x46616374`) carried in word 1 of an
exactly-8-byte `function-*` property. `IODART::callPlatformFunction` matches
it and dispatches to `AppleT8110DART::_forceAvailable(bool)`, taking the bool
as `*(u32 *)arg != 0` and asserting both auxiliary arguments NULL. That method
sets a software force-available flag and then, via `_updateAvailability` ->
`_powerUp`/`_powerDown`, calls
`AppleARMIODevice::enableDeviceClock(enable, index 0)` against the DART node's
own `clock-gates[0]`, reaching `ApplePMGR::_enableDevice` with action
`(enable, auto = 0)` — PMGR target ACTIVE `0xf` to enable, PWRGATE `0` to
disable.

It writes **no DART MMIO register**, and its `power-gates` leg is a proven
no-op: `ApplePMGRFunctionPowerGate::callFunction` drops the enable argument
and only queries `_wasDeviceDisabled`, with Apple passing a NULL out-pointer.

The m1n1 equivalent is therefore exact rather than approximate:
`pmgr_adt_power_enable_index("/arm-io/dart-acio1", 0)` before any DART
register access, and `pmgr_adt_power_disable_index(..., 0)` on teardown. Same
property, same index, same target value, same recursive parent walk, same
ACTUAL poll. The ordering constraint that matters is clock-ACTIVE before
TCR/TTBR programming, not Apple's placement at the end of `setHWState(2)`.

m1n1 deliberately issues `Fact(0)` **last** in teardown, after the ring
disable-done handshake and `dart_unmap`, where Apple issues it first. Apple
can go first because its own teardown has already guaranteed no NHI DMA is in
flight; m1n1 can only prove quiescence after those two steps. This matters
because the force-inactive flag makes `_updateAvailability` skip its
"is any mapper still enabled?" scan and gate the clock unconditionally, so
issuing it early here would be a genuine use-after-gate.

The software half of the flag exists only to suppress `_updateAvailability`'s
mapper-driven auto-gating, whose sole other entry point is
`AppleT8110DART::initHardware`. m1n1 has no such policy engine, so nothing
will gate the DART underneath a live NHI DMA stream; the clock vote is the
whole of the equivalent and is idempotent when iBoot already left it ACTIVE.

One item needs a live ADT read rather than a decode: whether
`/arm-io/dart-acio1` actually carries `clock-gates`. `manual-availability` is
present by contradiction (Apple calls `Fact` on acio1 and would otherwise
panic at `AppleT8110DART.cpp:883`), but the gate array cannot be proven that
way because `_powerUp` discards the error a missing array would produce. The
implementation fails closed and reports `ctrl-dart-force-active` if absent.

### Exact T6020 ACIO power-state contract

The captured nodes identify this target as `pmgr1,t6020`,
`atc-phy,t6020`, and Type5 `acio`; no Type7/T6050 ACIO register contract is
used.

**CORRECTION — this document previously claimed that "Apple's
`AppleT6050PMGR` class name denotes the shared PMGR family implementation
reached by this T6020 platform". That is FALSE and load-bearing, so it is
called out rather than quietly deleted.**

IOKit `IONameMatch` is an **exact string match**, not a family or prefix
match. In the kernelcache personalities:

- `AppleT6050PMGR` has `IONameMatch = "pmgr1,t6050"`
- `AppleT6020PMGR` has `IONameMatch = "pmgr1,t6020"`

with no wildcard, no `IOProbeScore` tie-break and no `IOCompatibility` key on
either. A `/arm-io/pmgr` node whose `compatible` is `pmgr1,t6020` therefore
binds `AppleT6020PMGR` and **nothing else**; it never instantiates
`AppleT6050PMGR`, and that class's register map does not transfer.

This is not academic. Acting on the false assumption would have written the
T6050 request bit `1 << (2 * slot)` = `0x40` for slot 3 into T6050's register
offset `0x20060`. On T6020 the register is `0xa02c`, the request encoding is
`1 << slot`, and `0x40` would have been slot 6's request bit — a slot that
does not exist on a single-die M2 Pro. Wrong register and wrong bit, as a
live PMGR write.

Only code in the shared `com.apple.driver.ApplePMGR` kext (the device-set
encoding, notification ordering, `waitForCioClusterComplete`, and the
synchronous-after-convergence guarantee) is generation-independent.

`AppleARMIODevice::setDevicePowerState` maps states 5, 6 and 7 through
`{0, 2, 1}` to the `function-clock_gate` action. The action bits are
`(enable, auto)`, producing exact PMGR targets:

| ARM state | action | PMGR target |
| --- | --- | --- |
| 5 | 0 | `PWRGATE` (`0`) |
| 6 | 2 | auto/minimum (`4`) |
| 7 | 1 | `ACTIVE` (`0xf`) |

Each J414s `acioN` has clock gates `[CIO, CIO_PCIE, CIO_USB,
CIO_RECONFIG-V]`. `NHIGenericACIO::poweredStart` applies state 5 to indices
0, 1 and 2 in order, checks only index 2, then enters `setHWState(2)`.
`enableEmbeddedCPU(true)` later applies state 7 to logical indices 0, 1, 2
and 4. The ADT count is four, so index 4 is out of bounds, returns
`kIOReturnUnsupported`, and is intentionally discarded. It does not alias
ADT entry 3 and does not request `CIO_RECONFIG-V`.

Those are NHI's logical calls, not the resulting hardware write order.
ApplePMGR recursively updates dependency votes, then synchronizes down changes
in reverse and up changes forward. On J414s, CIO_PCIE 79 and CIO_USB 80 vote
through CIO 64, so state 5 physically converges `79 -> 80 -> 64`; state 7
converges `64 -> 79 -> 80`. The bounded m1n1 implementation emits those exact
orders directly because its PMGR core does not model Apple's vote engine. Each
TARGET/ACTUAL transition uses Apple's `0x2ee00` (192,000 us) wait budget rather
than m1n1's former 10,000 us timeout.

VDD_CIO and CIO reconfiguration are separate PMGR APIs, not explicit calls
from AppleThunderboltNHI or AppleTypeCPhy. `syncVddCioState()` derives VDD_CIO
ownership from the four USB-AON PMGR states at offsets `0x148`, `0x160`,
`0x178`, and `0x190`; ACIO must not write the shared `pmVC` mask
independently. CIO reconfiguration is nevertheless real: AppleSOCTuner
subscribes to PMGR device-set notifications and calls `enableCioReconfig()`
synchronously on the relevant rising edges, after TARGET/ACTUAL convergence
and before the originating state call returns. A bare m1n1 PMGR transition
does not have that notification machinery, so it must implement a proven
equivalent at the PMGR boundary rather than guessing an NHI-side write.

The reconfiguration primitive is now decoded, with two corrections to what
this document previously claimed.

`AppleT6050PMGR::enableCioReconfig(UInt32 index, bool request)`:
a false request is a literal no-op. A true request accepts only indices below
`4 * die-count`, derives `die` and `slot = index & 3`, and operates on PMGR
register offset **`0x20060`** (not `0x260`, as previously recorded):

1. wait for `done_mask = 2 << (2 * slot)` to CLEAR, budget `0x2ee00`;
2. `write32(reg, 1 << (2 * slot))` — a plain store, NOT a read-modify-write;
3. wait for `done_mask` to CLEAR again via
   `ApplePMGR::waitForCioClusterComplete`, budget 10 s, polled at ~20 ms;
4. no clear-down write — the request bit self-clears in hardware.

Both waits are for the done bit to CLEAR. This document previously said the
second wait was for the completion bit to *assert*; that was wrong.

`0x2ee00` is **not microseconds**. `ApplePMGR::waitReg32` computes
`deadline = mach_absolute_time() + timeout` with no unit conversion, so it is
a raw mach-tick delta: ~8 ms at the 24 MHz timebase, not 192 ms. It is also
ApplePMGR's house-standard constant (it recurs in `configISPRefClock` and
`enableTVM`) rather than anything specific to TARGET/ACTUAL convergence, which
runs its own `mach_absolute_time` loop. m1n1's `PMGR_POLL_TIMEOUT` keeps the
numeric 192000 us purely as a permissive safety margin and no longer claims to
be a decoded Apple constant.

`waitForCioClusterComplete` returns void and has no error channel: on expiry
it **panics** after snapshotting registers via `storeCioCluterStatus`. m1n1
must instead fail the ACIO bring-up loudly with bounded telemetry.

Gate-531 ownership is resolved. The device-set encoding is
`encoded_device = pmgr_device_id | (die << 28)`, so `531` is die 0, PMGR
device id 531, and `522` is die 0, id 522 — the same decode explains both.
That id is the u16 at offset `0x1a` of the 48-byte ADT `/arm-io/pmgr` device
record, i.e. exactly m1n1's `struct pmgr_device`. The status bit is set iff
the device's new PS level is `0xF` (PS_ON); clock-gated (`4`) and off (`0`)
both read as zero. Slot naming is fixed by the driver's own debug strings in
slot order (CIO0..CIO3, then Die1 CIO0..CIO3), so `device-set-3` is CIO3 and
requests slot 3.

The initial-cache question is answered, and the answer moots the branch: the
SOCTuner cache is plain ivar storage, zero on allocation, and
`enableDeviceStatusChangeNotifications` memsets the array and then pushes a
full population through the same `message(0xa)` channel used for runtime
edges. So the pulse is generated **inside XNU from a zero-initialised cache**,
whether at notification-enable time or at the later ACIO power-up edge. It is
not something iBoot performed and m1n1 inherits. Only a 0 -> nonzero
transition pulses; an ON -> OFF edge updates the cache and does nothing.
The pulse is synchronous: it runs on the same thread inside the same
`_enableDeviceGated` call, after the triggering device's `PS_ACTUAL` has
converged to PS_ON.

### The T6020 register contract (proven, and different in every constant)

The values above are `AppleT6050PMGR`'s and **must not be used here**. From a
genuine `RELEASE_ARM64_T6020` image, `AppleT6020PMGR::enableCioReconfig` is:

```text
die       = index > 3
slot      = index - 4 * die
register  = PMGR RegMap 0 + 0xa02c      (T6050: 0x20060)
req_bit   = 1 << slot                   (T6050: 1 << (2 * slot))
done_mask = 1 << (16 + slot)            (T6050: 2 << (2 * slot))
```

RegMap 0 is ADT `/arm-io/pmgr` reg index 0 (`initRegMaps` registers
adt_reg_index 0 as RegMap 0 first). On J414s that window is 512 KiB, so
`0xa02c` is comfortably inside it. Only the sequence shape and the `0x2ee00`
pre-wait are shared with T6050.

The request is a **plain store**, not a read-modify-write: `0xa02c` does not
appear in the forced-wake workaround table that would require one.

m1n1 implements this in `acio_cio_reconfig()`, issued immediately after
state 7 converges — the same boundary at which stock's SoCTuner pulses
synchronously inside `setDevicePowerState`. Apple panics on the completion
timeout; m1n1 fails the bring-up with the bounded `cio-reconfig` telemetry
code instead.

The slot is **derived from the live ADT, never hardcoded**:
`/arm-io/acioN` `clock-gates[3]` names that instance's `CIO<N>_RECONFIG-V`
virtual device, and the reconfiguration slot is the position of the
`/arm-io/soc-tuner` `device-set-N` array that contains it. The pulse is
refused when `cio-config` is 0. This keeps each ACIO complex independent,
which the dual-port end state requires.

### Obtaining a per-generation kernelcache

Recorded because it retires "we have no artifact for that SoC" as a blocker.

macOS ships **restore kernelcaches for every supported Mac model**, not only
for the host. They live under:

```text
/System/Volumes/Preboot/<UUID>/restore/kernelcache.release.<model>
```

for example `kernelcache.release.mac14j` for the J414-family M2 Pro. These are
`RELEASE_ARM64_T6020` images containing `AppleT6020PMGR`, obtainable on a host
whose own BootKernelExtensions collection is a completely different SoC. The
files are IM4P-wrapped and LZFSE-compressed; `ipsw` will decompress and then
disassemble them with the same `--fileset-entry` workflow used for the local
BootKC.

Practical notes: `ipsw macho disass` on this host takes `-c N` for the
instruction count, not `-i N` (the `-i` form in `tb-type5/kc.py` is stale and
errors out).

The live boot ADT has no `acio` boot argument in `/chosen` or `/options`.
Therefore Apple's default `enableEmbeddedCPU` branch applies: it powers the
existing `IOSlaveProcessor`/RTKit image rather than selecting the debug-only
`loadFirmware()` branch gated by `acio` bit 15. There is likewise no ACIO
firmware blob property on the live `acio1` node, only its `acio-cpu` phandle.
The preboot implementation must adopt the iBoot-provisioned firmware/SRAM
state and must not invent or embed a replacement image.

The six Type5 ACIO ranges are not interchangeable: reg0 is the 1 MiB NHI
window, reg1 is the `0x30004` Type5 RX-PDF aperture, reg2 is coprocessor/RC,
reg3 is HBW fabric, reg4 is LBW fabric, and reg5 is PCIe-adapter registers.
There is no t8103-style separate `ctrl` aperture in this topology.

The Type5 ring layout is `TX = reg0 + 0x10000 + 0x4000*n` and
`RX = reg0 + 0x80000 + 0x4000*n`; descriptor DMA addresses are DART IOVAs.
The 16-byte descriptor's software seed is exactly `0x00400000`, completion is
absolute bit 21, and completion acknowledgement zeroes the metadata dword.
Shutdown must clear enable and wait for TX `+0x1c` or RX `+0x18` bit 0.

## Firmware provenance: the manual-loader path is NOT prohibited

Measured on this target, all three ACIO complexes are **PWRGATEd** before
m1n1 touches them. m1n1 is not responsible: `pmgr_init()` contains no
`PMGR_PS_PWRGATE` at all and its only `pmgr_set_mode` call is
`PMGR_PS_ACTIVE`, used solely to raise a parent of an already-active device.
The `pmgr: Cleaning up device states...` banner gates nothing. **iBoot leaves
ACIO powered down.**

Therefore the nub's `pre-loaded = 1` / `running = 1` properties describe
iBoot's *intent*; they are not a claim about live coprocessor state on the
current boot, and there is no boot on which adopting live firmware was ever
possible.

The standing prohibition on "copying firmware over the live preloaded SRAM
path" exists to stop us clobbering **running** firmware. There is no running
firmware here. Loading an image into a powered-down complex is a different
operation and is **not** covered by that prohibition. The external four-blob
bundle path is legitimate and probably mandatory. Do not re-apply the
prohibition out of context.

The bring-up distinguishes the two cases at runtime rather than assuming:
the readiness field is sampled before polling and compared afterwards, so a
field that never changed reports `fw-absent` (nothing is running to change
it) while one that changed but never reached ready reports
`fw-ready-timeout`, and a field that never decodes reports `fw-bus-error`.

### The firmware blobs are generation-independent

The four Type5 ACIO/TMU blobs were verified **byte-identical, and each
uniquely present**, in both a `RELEASE_ARM64_T6050` BootKC and the
`RELEASE_ARM64_T6020` restore kernelcache for this J414s target, with an
identical relative layout of `0x0, 0x22c00, 0x24560, 0x26b20`. Both images
produce the same 158736-byte bundle with the same SHA-256.

Only the absolute file offsets move (`acio_text` at `0x73d568` on T6050 vs
`0x7c1508` on T6020). Identity is the SHA-256, never the offset, so the
extractor records both known layouts and falls back to a digest-keyed content
search. Applying one generation's offsets to the other's image reads the
wrong bytes and fails the digest check, which would present as "wrong
firmware" when it is really "wrong layout".

## Testing rule: assert on APIs, not on words

Recorded because it recurred repeatedly and the failures all looked like
passes. A substring that *correlates* with the property you care about does
not *identify* it. Concrete instances hit while building this port:

- a whole-file `index()` for an error constant matched a different function's
  use of the same constant, so an ordering assertion about `firmware_start`
  was silently testing the router executor instead;
- a "function body" slice taken to end-of-file swept in `mdelay` from later,
  unrelated functions, so "teardown contains no delay" was asserting over the
  rest of the file;
- `assertNotIn("counter", body)` matched the function's own comment
  *explaining* why there is no counter release — prose, not code;
- a wait loop keyed on a log marker matched a marker left over from the
  previous run and returned the previous run's result instantly.

The suite was green between each of these. **A test that passes for the wrong
reason is worse than no test**, because it converts an untested area into one
that looks covered.

Rules that follow:

1. Assert on **API identifiers** (`ACIO_TYPE5_CONFIG_COUNTERS`,
   `acio_type5_counter_clear_offset`) rather than on English words that appear
   in comments.
2. Bound any source slice to real syntactic limits, never to end-of-file.
3. When asserting that something is *absent*, also assert that its
   *explanation* is present, so deleting the reasoning fails the test.
4. Blind bulk string replacement is the same family of error, applied to
   edits instead of assertions: it operates on text without regard to
   structure. Replacing a keyword argument across many call sites produced
   `SyntaxError: keyword argument repeated` where two patterns overlapped —
   caught only because Python parses. The same edit against C, or against a
   pattern that happened not to overlap, would have compiled and been wrong.
   Prefer structural edits, and re-parse every file a bulk edit touched.
5. A poll that waits for a marker in a *reused* file can match the previous
   run's marker and return instantly. Backgrounding a job as
   `(cmd > log; echo EXIT=$? >> log)` and then polling `grep -q EXIT= log`
   races: the redirect truncates when the subshell starts, so a poll that
   arrives first reads the prior run's completed output. Use a per-run
   filename, or truncate before backgrounding — never both reuse the name
   and poll for a marker that the old content already contains.
6. A stale absolute timestamp is not a measurement of the current run.
   Comparing a log's mtime against "now" reports the *previous* run's age at
   arm time. Take a baseline at arm time and require absence of growth, plus
   a warm-up, so the check measures this run or says nothing.
7. Anything that identifies a run must be unique to that run. A phase name, a
   completion marker, or an error string recurs; a monotonic counter latched
   before the run and required to be strictly greater afterwards does not.
   `acio_type5_status_t::run_id` exists for exactly this and must survive
   abort's `memset`.
8. **A 100% kill rate is a red flag, not a result.** If every mutant dies,
   suspect the harness first — especially when any single kill is one the
   tests could not plausibly have produced.

   Rule 8 exists because the *tools* keep failing the same way the code does,
   and a broken tool reports success. Instances so far: an `echo "syntax OK"`
   placed outside the loop it was meant to guard; a `:probe` anchor that could
   never fail; a "function body" slice bounded by `break;`; an unconditional
   `USB2_ONLY`; a sector-size default that survived thirteen mutations because
   not one of them varied it; and a mutation harness whose pass condition
   grepped `^OK` against ANSI-coloured output, so it never matched and **every
   mutant reported caught**. Six tools, all reporting success, none checking.

   The last one was caught by a result being *implausible*, not by a check
   failing: a mutation to the *wiring* cannot be caught by tests that call the
   predicate directly. That mutant was genuinely uncaught, and the real gap it
   exposed is worth naming separately (rule 9 below).

   **What to DO about a clean sweep — pair it with a negative control.** Rule 8
   as first written said only "be uneasy", which leaves you doubting every
   clean result forever. Instead:

   > A clean sweep is only meaningful next to a mutant the harness *lets
   > through*. Pair every mutation run with a **negative control**: a cosmetic
   > edit that should survive. If it dies, the harness is over-sensitive; if
   > nothing dies, it is blind. Only a mixed result demonstrates discrimination.

   This is cheap — one extra run — and it converts the ANSI-grep failure from
   an anecdote into a procedure. A harness that has produced both a kill and a
   survival has demonstrated the property a perfect score cannot.

9. **Testing a unit does not test that anything calls it.** A predicate with
   full coverage and no caller is indistinguishable, to a unit test, from one
   that is wired correctly. This is the same shape as a CLI flag that reaches
   the child environment and is read by nobody. Two complementary fixes are in
   the tree: `auroradbg/tests/test_boot_environment_contract.py` is the
   general form for one class (every variable written must be *consumed* by
   code that runs, discovered structurally, never a maintained list), and an
   explicit wiring test is the specific form for another (drive the caller and
   assert the observable effect, not just the predicate's return value).

## `firmware_start` landing phases (corrected, and now machine-checked)

`acio_type5_firmware_start` returning 0 says the call did not fail. It does not
say the runtime stopped where the request asked: an image that ignores
`diagnostic_stop`, a renumbered ABI, or a stop that quietly ran further all
return 0, and `run_id` cannot separate them because it advances on every entry.
The landing phase can — provided the expected value is right.

Read line by line out of the function, not inferred from the stop's name:

| `diagnostic_stop` | ABG ladder stop | phase at its `return 0` |
| --- | --- | --- |
| 6 | `resources` | `OFF` |
| 2 | `state5` | `FAILED` |
| 1 | `phy` | `PHY_PREPARED` |
| 3 | `state7` | `POWERED` |
| 0 (fall-through) | `control` | `CONTROL_READY` |

Then `acio_type5_router_configure` leaves `ROUTER_READY` and
`acio_type5_usb3_tunnel_up` leaves `TUNNEL_READY`. `FW_READY` is never a
resting phase for any stop; it exists only between the tunable apply and
`acio_control_start` inside a full run.

**CORRECTION.** A prior handover recorded that `state5`, `phy` *and* `state7`
all return while `phase` is still the `FAILED` sentinel written on entry to the
mutating section. That is true for **`state5` only**. `PHY_PREPARED` is assigned
before the `diagnostic_stop == 1` return and `POWERED` before the
`diagnostic_stop == 3` return; only `diagnostic_stop == 2` returns in the window
between the entry sentinel and the first overwrite. `resources` ending at `OFF`
was correct as recorded — it is the deepest stop that leaves the machine exactly
as found, so the function sets `OFF` explicitly rather than leaving a sentinel.

Two consequences worth stating plainly:

1. `FAILED` is *also* the fault phase, so for `state5` the phase alone cannot
   identify success. It is checked together with a zero return and a strictly
   advanced `run_id`; that combination does identify the stop, the phase on its
   own does not.
2. This table is **not maintained by transcription**. ABG parses
   `acio_type5_firmware_start` out of `src/acio_runtime.c` and requires exact
   agreement, and does the same for `enum acio_type5_runtime_phase` in
   `src/acio.h` and for `acio_type5_error_name()` in `src/acio_type5.c`. Three
   places where the two repositories cannot silently drift. **If this document
   ever contradicts those parsed assertions, this document is the thing that is
   wrong.** The error-table check was added after ABG's copy was found to be
   missing `fw-absent`, `fw-bus-error` and the entire device-scan group
   `0x0a01..0x0a05`, which would have decoded a real scan failure as a bare
   `unknown-0x0a01`.

## Mu must READ the transport, not infer it from PHY state

Mu decides a port needs its deferred USB3 PIPE switch from **PHY powered +
out of reset + `MUX_CTRL == 0x22`**. That state is **byte-identical** between
`atcphy_prepare_routed_mode` (USB4/TBT: releases APB_RESET_N|PHY_RESET_N, then
deliberately parks DUMMY) and the direct-USB3 deferred prepare. Mu reads no
crossbar or lane-mode register, so its evidence establishes *what state the PHY
is in* while its conclusion names *which transport the port carries*. Getting it
wrong runs `AtcPhyPipeSwitchToUsb3` against lanes programmed for USB4.

Currently latent — nothing performs a routed prepare in-boot — but **live by
construction** in the goal state, because USB4 boot requires m1n1 to do the
routed prepare and *then* launch Mu so Mu can enumerate the tunnelled xHCI.
This gate is therefore a **prerequisite for the tunnel→Mu handoff**, not
hardening applied afterwards, and must land before or with the wiring that
makes a routed prepare happen in-boot.

The discriminator is `ACIOPHY_CROSSBAR` (ATC PHY **core** window, offset
`0x4C`), which Mu already maps for `POWER_CTRL 0x20000`:

| `CROSSBAR & 0x1F` | m1n1 mode | Mu's USB3 switch |
| --- | --- | --- |
| `0x10` / `0x11` | `ATCPHY_MODE_USB3` | **correct — proceed** |
| `0x00` / `0x01` | `ATCPHY_MODE_USB4` / `ATCPHY_MODE_TBT` | refuse |
| `0x0A` / `0x0B` | `ATCPHY_MODE_OFF` (also parks DUMMY) | refuse |
| `0x14` | `ATCPHY_MODE_DP` | refuse |

Whitelist `{0x10, 0x11}` and refuse everything else **including unrecognised
encodings**, for the same reason as the lane link-state gate: an unknown
crossbar value is not evidence that the lanes are USB3. `ACIOPHY_LANE_MODE`
(`0x48`; USB3 `0x489`, swapped `0x252`, USB4 all-zero) corroborates but is not
the discriminator.

### ⚠️ The constant named `PROTOCOL_USB3` is NOT what USB3 mode programs

This trap is why the table above was read out of `atcphy_core.c` rather than
`atcphy_core.h`:

- `ATCPHY_CORE_ACIOPHY_CROSSBAR_PROTOCOL_USB3` = `0x0A` is what
  **`ATCPHY_MODE_OFF`** programs;
- `ATCPHY_MODE_USB3` programs
  `ATCPHY_CORE_ACIOPHY_CROSSBAR_PROTOCOL_USB3_DP` = `0x10`, because USB3/USB3
  is unsupported (20 Gb/s), so the companion lane is programmed as DP.

Anyone implementing this gate from the header, against the constant whose name
says `USB3`, produces an **exactly inverted gate**: it accepts the OFF state
and refuses real USB3. The header still says `PROTOCOL_USB3`, so this warning
has to live next to the gate. Read `atcphy_modes[]` in `src/atcphy_core.c` for
what each mode actually writes; the header names what a *value* is called, not
which mode uses it.

## Orientation: a default is a guess unless something measured it

"Guessed orientation" is on the do-not-revive list at the end of this document.
It was nevertheless revived, in the least visible way available: ABG's ladder
exposed `--flipped` as a plain `store_true`, so *omitting* the flag meant
`flipped = 0` — a guess wearing a default's clothing. Demonstrated by execution
rather than argued: with a live **flipped** CD3217 contract on hpm1
(`STATUS = 0x100280ff`), a run with no flag passed `flipped = 0` to
`firmware_start`, issued **zero** CD3217 reads, and reported success. Wrong
lanes, silently, with an `ok`.

The rule this establishes, for any future knob of this shape: **a boolean whose
false value is a physical claim about the world must not have a default.** It
must be measured, or explicitly asserted, or refused.

ABG now samples all three Type-C complexes before anything is powered or routed
— one port cannot distinguish "this port lost its contract" from "HPM init never
ran", which is exactly the ambiguity seen when all four HPMs failed identically
— then programs the measured orientation, refuses when an explicit assertion
contradicts the measurement rather than preferring either source, and stamps an
unmeasurable run so it can never later be reported as measured.

## Two m1n1 diagnostics that were silently ambiguous

Both are logging/robustness only; neither changes a hardware sequence.

- `acio_type5_scan_device_router` skipped inactive **and undecodable** adapters
  before its log line, so "we saw an adapter and could not classify it" and
  "there was nothing there" produced byte-identical empty output, and both ended
  in `scan-no-usb3-up`. It now prints every slot `1..MaxPort` with raw type,
  decoded kind and direction, prints `UNDECODED (family, sub)` **without**
  claiming a direction it cannot know, and emits a summary before failing.
- `tps6598x_powerup` logged its wake-retry count only when the count was
  non-zero, so a fast wake and a controller that was never probed both logged
  nothing; and the read *after* `SSPS` was still a single un-retried
  `i2c_smbus_read8` with `SSPS`'s own return value discarded, both failure paths
  returning `-1` with no message. Every call now reports attempt count, elapsed
  time and `power_state`, the post-`SSPS` state is polled rather than sampled
  once, and every exit names itself.

## Required end-to-end order

1. CD321x: atomically sample `STATUS` + `DATA_STATUS`; when applicable read
   USB4 status/EUDO or TBT Intel VID VDOs. Reject contradictory modal bits.
2. ACIO reset/PHY: apply state 5 / `PWRGATE` to ACIO clock-gate indices
   0, 1 and 2, then select the orientation-specific USB4/TBT CIO lanes. Do
   not claim a usable link from crossbar/lane programming alone.
3. ACIO run: apply state 7 / `ACTIVE` to indices 0, 1 and 2. Do not alias
   logical index 4 to ADT entry 3, take VDD_CIO ownership from USB-AON, or use
   the t8103-only CTRL INIT request. Match the SoCTuner notification-driven
   CIO reconfiguration at its actual post-transition PMGR boundary.
4. M3/RTKit: after the proven Type5 power/HW/PHY ordering, start the ACIO CPU,
   boot RTKit, translate every firmware SRAM
   request from IOVA `0x10000000` into the validated ADT SRAM aperture, and
   poll reg2 `+0xa8` until `(value & 0x7f000000) == 0x01000000`. The Apple
   loop uses 5 ms intervals with a scaled 500 ms budget; equality is required.
5. ACIO DART: configure the NHI stream before enabling rings; no unbounded
   bypass. Teardown blocks DMA before removing power.
6. NHI: apply all required ADT tunables, validate the low 11 bits of the path
   count against 12 ring IRQ pairs, program Type5 0x4000-stride TX/RX rings
   with DART IOVAs, and bring up the host router. Preserve positional IRQ
   order and never sort by GSIV. **Corrected:** the positional mapping is the
   opposite of what this document previously said — ADT `interrupts[0..11]`
   are **RX** and `interrupts[12..23]` are **TX**. Proof: the TX manager's
   interrupt-enable bit is `1 << hop` while the RX manager's is
   `0x1000 << hop`; `statusMaskForRing(r, 0) = 1 << r` and
   `statusMaskForRing(r, 1) = (1 << (r+12)) | (1 << (r+24))`; and
   `TxRing::configure` passes `0` where `RxRing::configure` passes `1`. So
   status/enable bits `[0..11]` are TX, `[12..23]` are RX, `[24..35]` an RX
   secondary class, while the dedicated-interrupt index runs the other way.
   The live J414s `acio1` `interrupts` is `[1420..1431, 1408..1419]`, so the
   numerically higher AIC block is RX. There is no "Apple's dispatch is
   swapped" bug; that note was an artifact of the inverted assumption.
7. Router: locate Apple VSE, publish orientation/cable information, discover
   adapters, and establish the USB3 tunnel. USB4 cable presence alone is not
   tunnel readiness.
8. Firmware/OS boundary: either keep ACIO/DART/NHI/router ownership in a
   resident preboot service with an explicit ABI, or publish the complete
   resources and state to Mu/Windows. Split ownership is forbidden.
9. Mu/Windows: enumerate the tunneled USB3 adapter as the boot-capable xHCI
   path, publish ACPI/IOMMU/interrupt resources, and prove boot-volume I/O.
10. Recovery: on every failure, stop rings, remove tunnels, quiesce RTKit,
    block DART streams, power down ACIO, and return ATCPHY to a safe state.

## Implemented foundations

- `tps6598x_read_link_state`: bounded, read-only STATUS + DATA_STATUS plus
  USB4 mode/EUDO sample when USB4 is negotiated.
- Proxy `P_ATCPHY_READ_LINK_STATE`: selector ABI exposes full DATA_STATUS and
  USB4 mode/EUDO/Apple cable information from the same bounded sample.
- ABG direct-USB3 preflight: rejects USB4, TBT, DP, USB2-only, wrong-role,
  disconnected, and unreadable states before any PHY arm/apply operation.
- `acio_discover_resources`: fail-closed ADT extraction/validation for t6020
  NHI, RC, DART, ASC mailbox/CPU, ring IRQs, tunables, DROM, and SRAM mapping.
- Exact Apple Type5 NHI-call sequencing for the J414s clock-gate list: state 5
  quiesces indices 0/1/2 before PHY routing and state 7 raises the same three
  afterward. The former ACTIVE-before-PHY inversion and invented
  entry-3 path are removed; VDD_CIO remains with its USB-AON platform owner.
  The automatic SoCTuner reconfiguration consequence is documented but is
  still a runtime gate until its exact PMGR-boundary equivalent is complete.
- `acio_type5.c`: pure, host-tested Type5 control-plane primitives with no
  runtime caller and therefore no MMIO/power/DMA side effect:
  - 16-byte descriptor seed/completion/ack, 12-ring offset calculation,
    32-bit TX/RX doorbells, modulo arithmetic, and the Type5 range-1 RX PDF
    mirror;
  - the complete known control PDF table, promiscuous raw control-ring-0
    policy, per-PDF two-bit sequence counters, and big-endian config-request
    header packing and CRC32C (Castagnoli, standard init/final inversion),
    pinned by the Asahi/spec implementation and the `123456789` test vector;
  - SID mapper partition selection for the accepted 1/2/24-entry layouts,
    with the live J414s left-front path resolving to shared SID 1;
  - bounded capability-list stepping and a duplicate-rejecting breadth-first
    queue. Discovered child route strings must be supplied by a future
    topology decoder; this code does not invent their encoding;
  - the exact USB4 router sequence: poll router dword 6 bit 24, read dword 5,
    write `0x03000000` or `0x05000000`, write it again with bit 31, then poll
    dword 6 bit 25;
  - grade-A two-dword hop descriptor packing with fail-closed field bounds;
  - USB3 TX/RX path defaults (hop 8, path types 8/7), path activation before
    adapter enable, capability ID `0x04`, UP enable bits 31:30, a 100 ms
    event/deadline state, then DOWN enable. The state machine never sleeps.
- `rtkit_set_sram_window`: explicit IOVA-to-physical SRAM translation and
  containment for ACIO-style firmware requests.
- ATCPHY USB4/TBT entry points fail before PMGR/MMIO until a complete router
  owner exists. The previous USB4-to-dummy success fallback was removed.
- `atcphy_seq_pipehandler_usb4_routed`: host-tested, fail-closed USB4 PIPE
  transition ending at the documented `0x11` mux value. It is intentionally
  unreachable from runtime until ACIO/NHI/router/tunnel ownership is ready.
- m1n1 kboot uses the upstream-correct `usb4_%d_acio` alias.

## Gates closed since the previous revision

- **DART `Fact(1/0)`** is decoded end to end and implemented as an exact PMGR
  clock-gate equivalence, with teardown ordering deliberately safer than
  Apple's. See the DART section above.
- **CRC32C is proven, not assumed.** `_IOThunderboltCRC32` is table-driven
  reflected CRC32 with init `0xFFFFFFFF` and a final inversion; all 256 table
  entries reconstruct exactly from reflected polynomial `0x82F63B78`
  (Castagnoli) and do not match IEEE-802.3 `0xEDB88320`. A second,
  byte-swapped-within-word variant exists, but both `ConfigReadCommand` and
  `ConfigWriteCommand` call the plain byte-sequential one. The CRC covers the
  12-byte header plus payload, excludes the CRC field, is byte-swapped on
  store, and lands at offset `12 + payload`, for a total frame of
  `16 + payload`. This closes what the corpus listed as an open unknown and
  removes a defect that would have caused every config packet to be rejected.
- **Ring start ordering.** Apple's order is descriptor IOVA low/high -> entry
  count -> options word with bit 31 ENABLE -> *then* `+0x14`. An earlier
  revision here programmed `+0x14` and the PDF mirror before enable; that
  inversion is corrected.
- **Type5 PDF mirror ownership.** The Type5 override calls the base
  implementation first and then mirrors the identical word into register
  range 1, so both writes are required — not the mirror alone.
- **Sparse ring-0 operation.** Rings 1..11 require nothing: ring construction
  touches no MMIO, the enable bit is per-ring, and there is no global
  "enable all rings" register. Bringing up ring 0 alone is a designed-in mode.

## Runtime gates still open

- the `enableCioReconfig()` pulse itself. Its trigger edge, encoding, slot
  mapping, sequence and timeouts are all decoded (see above); what remains is
  proving the PMGR register offset and bit layout for T6020 rather than
  inheriting them from a T6050-only BootKC;
- ACIO CPU start/readiness confirmation using the t6020 resource layout;
- NHI runtime: sparse ring ownership, interrupts, tunables, DROM, and Apple
  VSE. Ring geometry, the 32-bit doorbell encoding, descriptor format,
  disable-done offsets/timeout, and the Type5 RX-PDF mirror are settled;
- the TX shared-buffer allocation at `TXB + 0x14`. Apple writes the low 16
  bits of `ring[0xd8]` from `allocateSharedBuffer`; the value is not decoded,
  so the register is deliberately left at reset rather than guessed;
- the TX options word's raw-mode bit. RX raw mode is established for a
  promiscuous control ring; TX raw is currently an inference;
- config-channel response validation and runtime dispatch;
- child-router route-string construction and full `childDeviceScanForPort`
  decode. The bounded BFS queue intentionally accepts only already-decoded
  route strings;
- NFC credit compare-swap, counter allocation/clearing, QNE drain, hop
  teardown, and bandwidth/firmware-credit ownership. Hop packing alone is not
  path activation;
- host-router discovery and USB3 tunnel execution/teardown. The pure state
  machine describes exact ordering but has no runtime transport executor;
- the source and ownership of `Buffer Allocation Request`, required when USB4
  `CreditOptions` bit 2 selects firmware-recommended credits;
- TBT VDO propagation beyond the current link classification;
- persistent ownership ABI across m1n1 -> Mu -> Windows;
- Mu boot-volume discovery and Windows ACPI/driver boundary;
- host tests for each pure state machine and separately authorized hardware
  acceptance for every power/DMA transition.

No runtime gate may be replaced with a sleep, fixed delay, guessed register,
or direct USB3 fallback for a negotiated USB4/TBT contract.
