# Native AIC on Windows/Apple Silicon

Status: corrected hardware checkpoint, not a claim that every future workload
is proven. Target: J414s / T6020 M2 Pro, ten CPUs, AIC2, Windows ARM64.
Updated: 2026-08-05.

This is the canonical handoff for the complete native-AIC design: Mu's pre-OS
role, the Windows HAL extension, m1n1's narrow EL2 bridge, the failures captured
on hardware, the fixes, and the remaining non-native pieces. Historical build
attempts are intentionally omitted.

## Current result

Windows now uses the physical AIC2 for ordinary peripheral interrupts. The HAL
reads hardware EVENT tokens and directly controls masks/EOI. m1n1 does not run a
steady-state vGIC; it retains only the work stock Windows cannot perform:
Apple-FIQ timer/Fast-IPI reception, CPU-local synthetic EVENT arbitration, and
rare collision recovery. AIC2 lacks proven runtime CPU targeting, so the HAL
still performs bounded software forwarding when hardware delivers a line to a
CPU on which Windows did not connect its ISR.

The corrected stack passed four consecutive resident-chainload boots to a
responsive internal-SSD Windows login/desktop, with raw SSH, keyboard,
touchpad, keyboard backlight, xHCI, and Realtek USB Ethernet working. A final
300-second, ten-worker cross-CPU PTE-shootdown test completed 781,653 rounds and
1,563,306 protection transitions without a lost worker, hang, or bugcheck.

Exact checkpoint:

- m1n1 `2e61bd8af9d2b38df346d2ed401c47012776c732`, Mach-O SHA-256
  `c7adbc1dadc06cca0bdb2f1f1ce937a1950b8cf6dcf8856def121da0b2eb04d1`.
- Mu `a5d59a223cdb8b3d6df029f678037f2c2ea57fbe`, firmware SHA-256
  `ac41a315e559b72d6cda9d49074b5c28a6dc97058b4bc460d9183306cf75d04b`.
- Production HAL package `1.0.0.6`, DLL SHA-256
  `dde98eab33f81acdecaabe0be9a5a1ebdbf99b28e0ca5784fe43908f96b23655`.

## Ownership and boot phases

There are three cooperating owners:

1. **Mu** initializes AIC2 and describes it through ACPI. During firmware it
   uses reserved software IRQs to reflect architected timers.
2. **`HalExtAppleInterruptController.dll`** is Windows' live interrupt
   controller. This profile cannot boot correctly without it.
3. **m1n1 at EL2** retains physical Apple FIQs, performs startup handoff, and
   converts timer/Fast-IPI completion into tokens understood by the HAL.

`drivers/AicHal` builds the deployed DLL. `drivers/AppleAic` is portable code
linked into that DLL and host-tested; `drivers/AppleAic` is not a second live
controller driver.

| Phase | Owner and delivery |
|---|---|
| Mu | Mu owns AIC setup. m1n1 retains Apple FIQs with `HCR.FMO`. |
| Windows startup | MADT/GTDT expose a GICv3-shaped startup topology while APs and the HAL initialize. m1n1 supplies a bounded startup carrier. |
| Windows native | HAL enables AIC2 `CONFIG`; the carrier retires. Ordinary IRQ delivery uses native AIC MMIO. m1n1 synthesizes only CPU-local timer/Fast-IPI tokens and resolves rare real-IRQ/synthetic-doorbell collisions. |

Mu publishes MADT CPU/startup topology, GTDT timer identities, and an
`APPL`/`NTAS` CSRT record containing generation, chip, IRQ count, MSI range,
and optional `ALI2` GSIV-to-physical aliases. For T6020, AIC2 is at
`0x28e100000`, EVENT at `0x28e10c000`, chip ID `0x6020`, with 1961 lines and
MSI range `[1672,1704)`. Important aliases include xHCI `37 -> 1274`, ANS
`38 -> 1832`, second xHCI `39 -> 1292`, and GPU mailbox `46 -> 1146`.

## The token contract

AIC EVENT is destructive: reading a hardware event identifies and auto-masks
the source. Its raw 32-bit value is the acknowledge token and must survive
unchanged until controller EOI.

- Type 0: processor-local timer/PMU identity; no physical AIC EOI owner.
- Type 1: physical hardware line (`die`, `number`).
- Type 4: Fast IPI; m1n1 embeds a per-CPU generation in the raw token.

The HAL translates physical lines to Windows GSIVs only for dispatch. Token
ownership and EOI always use the exact raw value.

The invariants are:

1. Read EVENT at most once per acknowledge and never lose a nonzero token.
2. A physical line has at most one Windows owner while AIC auto-mask is active.
3. EOIs are exact-token completions, not assumed LIFO callback order.
4. A known unread physical IRQ precedes every newer synthetic timer/IPI token.
5. That precedence guard must itself have a bounded recovery path.
6. A Fast-IPI generation remains in flight until its exact HAL EOI commits it.
7. Timer reflection is complete only after the guest reprograms/rearms it.
8. Mu must not synthesize a timer callback before TimerDxe has initialized its
   period and compare state.

## Windows HAL extension

The HAL validates exact private callback signatures, the live AIC2 capability
shape, CSRT data, MMIO extent, aliases, and every compact MPIDR. Unknown Windows
kernel shapes fail closed. It replaces all selected Unit-0 function pointers
and private data while preserving the GICv3-shaped controller shell Windows
already materialized during startup.

Runtime behavior:

- `AcceptAndGetSource` reads one EVENT and decodes one token.
- Each CPU has a bounded 16-entry live-token set. A hardware token also claims
  its physical line globally.
- `EndOfInterrupt` searches newest-to-oldest for the exact raw token, completes
  it, then stably compacts the remaining set. Unknown EOIs change no owner.
- Hardware completion releases the line claim and reapplies current admission;
  Fast-IPI completion commits the matching m1n1 generation.
- Connected-line tracking makes IRQL updates proportional to active lines and
  never unmasks a source still owned by an ISR.
- The production build disables scalar diagnostics and traffic rings, removing
  their atomic loops from interrupt hot paths while preserving fatal state.

### AIC2 affinity and software forwarding

No AIC2 runtime CPU-target register has been proven. The IRQ-config low nibble
comes from ADT firmware metadata and is not a demonstrated OS affinity field.
On J414s, writing values 0-15 to a reserved software line showed value 0 always
arriving on CPU 0 and 1-15 producing no EVENT; the second bank and
`PREFER_PCPU` did not alter this. m1n1 therefore leaves AIC2/AIC3
`aic_set_affinity()` as a no-op.

Windows connects `KINTERRUPT` objects only on CPUs selected when the line is
connected. Changing later `SetLineState` target metadata cannot create missing
ISR slots. Directly dispatching xHCI line 1274 on an unconnected CPU was
captured as a NULL ISR slot and `TRAP_CAUSE_UNKNOWN 0x12`.

The HAL therefore records Windows' admissible mask in software. If AIC delivers
a hardware token elsewhere, the source stays auto-masked, a bounded
generation-qualified forwarding slot is CAS-claimed, and Apple Fast IPI wakes
an admissible CPU. That CPU dispatches and later completes the original raw
token. A global active-line bitmap remains the single-owner authority. Queue or
wake failure reclaims ownership rather than silently losing the line.

This forwarding is correctness work under the current hardware/Windows
contract. Remove it only after proving either real AIC2 CPU steering or a
Windows connection with valid ISR objects on every possible delivery CPU.

## m1n1 EL2 bridge

After native handoff, almost all AIC MMIO is direct. m1n1 hooks the EVENT page
only to arbitrate a retained physical token or a CPU-local timer/Fast-IPI token
against the real hardware value. Normal mask and EOI operations remain direct.

### Timers

Physical and virtual timer FIQs remain at EL2 under `HCR.FMO`. On expiry, m1n1
masks/coalesces the FIQ source, records one unread reflection, and raises a
CPU-local IRQ wake using the synthetic doorbell route. The next HAL EVENT read
returns type 0 source 2 or 3. A guest timer reprogram/rearm ends that generation
and re-enables the physical source.

Raw Apple FIQ delivery is not usable with stock Windows. Every FIQ vector in
the exact running kernel reaches `KiFIQException`, whose image code calls
`KeBugCheck(0x3D)`. `HCR.FMO` must remain until Windows itself gains a supported
FIQ path.

Synthetic `HCR.VI` observes `PSTATE.I` but not the HAL's controller-priority
threshold. The HAL therefore caches virtual-timer line 27 priority. If timer
EVENT(3) is read while blocked, it returns spurious and records a deferred bit.
When IRQL drops, a rare SMC asks m1n1 to repost only the same still-live
generation. A guest timer write makes the request stale, so no tick is invented.

### Fast IPI

The HAL publishes the Windows IPI class before ringing Apple's local/global RR
system register. m1n1 resolves the MPIDR, emits the physical Fast IPI,
acknowledges its EL2 FIQ, tags a generation, and posts a type-4 EVENT.

The generation remains `INFLIGHT` until exact HAL EOI. If Windows has not EOI'd
within 250 ms, m1n1 may re-emit the same generation as a wake retry. The HAL
recognizes a replay already present in its live set as read-only/spurious; only
the original token commits transport ownership.

### Real IRQ versus synthetic doorbell

A real IRQ can arrive during the short `HCR.IMO` window used by the synthetic
doorbell. m1n1 immediately returns to passthrough and records `UNREAD_REAL`, so
the destructive hardware EVENT read stays with Windows and no synthetic token
overtakes it.

On a later EL2 entry, an `ISB` plus non-destructive `ISR_EL1.I` sample releases
the guard if the IRQ input is quiet. If it remains asserted for at least 250 ms,
the cold fallback reads AIC EVENT exactly once, stores the raw token in one
per-CPU replay slot, then releases the guard. The retained token is returned
before any newer physical EVENT read or synthetic token. A zero read proves
nothing was consumed. Reserved Mu timer-reflection lines are cleared/masked
instead of escaping to Windows. Normal collisions never enter this fallback.

One HCR policy owner selects passthrough versus synthetic-doorbell routing.
There is no competing periodic forward-progress injector.

### x18 and WFI

Windows owns x18 as its KPCR alias. m1n1 saves/restores it verbatim and never
repairs it. Earlier x18-zero traces were Windows' legal BHB-hardening vector
stub temporarily counting x18 to zero around `SB`, then restoring it. Rewriting
that transient value was wrong.

Separately, m1n1 selects clock-gate-only `CYC_OVRD_WFI_MODE(2)`, clears Apple's
WFI-retention-disable state on every guest CPU entry, and sanitizes trapped
guest writes. This prevents real retention loss without reconstructing a
Windows-owned register.

## Captured failures and fixes

### 1. `UNREAD_REAL` suppressed all future synthetic wakes

The original guard cleared only when Windows read a real EVENT. If the device
deasserted or another CPU masked it first, the IRQ input became quiet but the
guard remained forever. The first fix released it on a later quiet
`ISR_EL1.I` sample.

A second hardware failure showed that this was still unbounded when a level
IRQ remained asserted. A queued Fast IPI could not restore the synthetic route,
leaving a target CPU without rendezvous progress and producing intermittent
`0x1DB` IPI watchdog bugchecks or hangs. m1n1 `2e61bd8a` adds the bounded,
one-token capture/replay fallback described above. It is absent from the direct
path and cannot discard or reorder a captured hardware token.

### 2. Fast-IPI retry committed before Windows EOI

An earlier HAL treated m1n1's retry as a new acknowledge and committed it in
`AcceptAndGetSource`. Generation N+1 could then become visible while Windows
still owned N. The HAL now treats an already-live generation as a read-only
replay; only exact EOI commits it.

### 3. Strict-LIFO EOI leaked an outer IPI

A watchpoint captured CPU 0 owning outer Fast IPI `0x61040001`, then hardware
token `0x00010728` (ANS line 1832), while Windows legally EOI'd the outer IPI
first. The old HAL compared only the newest entry, rejected the EOI, and never
committed the IPI. Subsequent rendezvous stalled, appearing as
`CLOCK_WATCHDOG_TIMEOUT`, a full freeze, or a live login/desktop whose input
source remained permanently auto-masked.

HAL `761d605` replaced stack ownership with exact-token-set completion. The
outer owner can complete while the nested hardware owner remains valid.

### 4. Timer EVENT bypassed Windows priority

Synthetic `HCR.VI` allowed the HAL to consume timer EVENT(3) at an IRQL where
Windows would not dispatch line 27. No timer callback meant no rearm; m1n1 then
correctly coalesced every expiry behind a stranded generation. HAL `bf2ef1e`
adds priority-aware deferral, and m1n1 `6fa1df97` reposts only the same live
generation after IRQL drops.

### 5. Mu synthesized a timer before TimerDxe was armed

A resident-chainload boot repeatedly spun before Windows. `abg hv state
--all --symbols` located CPU 0 in exact `ArmTimerDxe` code,
`TimerInterruptHandler + 0x80`, inside its compare catch-up loop. Live values
were:

```text
CompareValue = 0
CurrentValue = 0x1606d36c
mElapsedPeriod = 1
mTimerNotifyFunction = 0
mTimerPeriod = 0
mTimerTicks = 0
```

The loop added `mTimerTicks` to `CompareValue`; adding zero could never catch
the current counter. Mu had injected source 17 while registering the callback,
before TimerDxe initialized ticks/period and enabled the architectural timer.
A stale software IRQ from the prior resident boot made this deterministic.

Mu `a5d59a2` removes registration-time synthesis. AIC init leaves both private
timer-reflection blocks masked. Registering source 17/18 clears every software
slot before unmasking it, and m1n1 keeps reflection gated until TimerDxe's final
`ENABLE=1, IMASK=0` write. Thus no valid post-arm tick is discarded and no
pre-arm callback can run. The host contract models the old zero-step deadlock
and proves clear-before-unmask/no-synthesis ordering.

## Validation evidence

Host/build gates:

- HAL/native-AIC contracts cover AIC layouts, CSRT, aliases, MSI, exact-token
  EOI, forwarding, Fast-IPI generations, timer priority, and WinPE deployment.
- m1n1 contracts cover sole HCR ownership, deferred collision ordering,
  timeout capture/replay, reserved lines, and empty capture.
- All 163 Mu host tests pass, including the TimerDxe registration model; the
  full ARM64 firmware build passes.
- The production HAL is signed, import-free, and built with hot diagnostics
  disabled.

Final hardware gates on the exact checkpoint:

- Four consecutive resident-chainload boots immediately crossed Mu's
  `waiting for timer handler` boundary, announced timer readiness, and reached
  Windows/SSH in about 45-60 seconds.
- Live inspection found `HalExtAppleInterruptController.dll` selected on Unit
  0 with capabilities `0x72`; all 11 callback addresses and private data match
  the exact map.
- After the 300-second shootdown soak, HAL software forwarding was
  `88273 sent / 88273 dispatched / 0 dropped`, with zero line-state rejects.
- m1n1 had no deferred CPU and no retained replay token. Normal quiescence
  releases occurred; timeout capture and replay counters stayed zero, showing
  the cold fallback was not needed during the final run.
- Windows reported keyboard, HID touchpad, and Realtek USB GbE as started. The
  only System critical/error event was an unrelated Secure Boot certificate
  notice; there was no bugcheck or interrupt failure.

The BCM4388 Wi-Fi service remains disabled while the separate GPU/Wi-Fi issue
is resolved. GPU ACPI publication is deliberately off in this checkpoint; that
state must not be misdiagnosed as an AIC failure.

## Remaining architectural debt

1. Timer/Fast-IPI EVENTs still need a narrow EL2 synthetic IRQ doorbell because
   stock Windows bugchecks on raw FIQ. This is generation-owned and event
   driven, not polling.
2. Peripheral lines can require lock-free software forwarding because AIC2 CPU
   steering is unproven and Windows does not install every ISR on every CPU.
3. The 250-ms real-IRQ collision capture is a cold safety bound. It preserves
   hardware token order but should remain observable and rare.
4. Sustained mixed GPU/Wi-Fi operation has not yet been validated; it is the
   next subsystem gate after AIC.

The desired endpoint remains direct peripheral delivery, no hot-path UART or
diagnostic atomics, no polling/progress timer, exact hardware token ownership,
and no EL2 mutation of Windows x18. Those requirements are met except for the
two explicitly justified bridges above.

## Deployment, recovery, and debugging

For a changed HAL: host-test and sign it, boot it first in RAM-only WinPE,
verify the WIM/FAT readback and live callback owner, then use guarded WinPE with
AppleNvme to stage the exact package/DLL into offline internal Windows. Disable
hibernation/Fast Startup; an on-disk hash alone does not prove the current
kernel did not resume an old HAL image. Verify live callbacks against the map.

If internal boot is broken, boot known-good RAM-disk WinPE, load AppleNvme,
locate the internal Windows volume, and replace
`Windows\System32\HalExtAppleInterruptController.dll` plus the matching
OSEDB/DriverStore state. Do not install the HAL into WinPE merely to repair the
offline OS.

Useful commands:

```text
abg hv state --all --symbols
abg windows call win_msi_halext --timeout 120
abg windows call win_msi_lines --timeout 120
abg windows debug aic-aliases --timeout 120
ssh j414s
```

Source map:

```text
HAL       AuroraSilicon/drivers/AicHal/HalExtAppleInterruptController.{c,h}
models    AuroraSilicon/drivers/AppleAic/, AuroraSilicon/tests/test_aic_*.py
m1n1      m1n1/src/hv_aic.c, hv_exc.c, hv.c, aic.c, aic_regs.h
Mu        mu/Silicon/Apple/AppleSiliconPkg/Drivers/AppleAicDxe/
Mu test   mu/Tests/test_aic_timer_registration_contract.py
stress    AuroraSilicon/tools/aic-ipi-stress/
deploy    AuroraSilicon/tools/build-j414s-aic-hal-*-winpe.sh
```
