# windows-native-aic: native AIC passthrough + timer-FIQ reflector

Branch: `windows-native-aic`. Base: `windows` (vGIC-emulation design, `ENABLE_VGIC_MODULE`).

**Status: HARDWARE-VALIDATED CORRECTNESS CHECKPOINT.** Native AIC2 CONFIG
handoff, ten-core startup, timer reflection, and real Fast-IPI sends execute on
a J414s M2 Pro. The pinned internal-storage profile reaches responsive Windows
with SSH, HID, xHCI, and USB Ethernet under ten-core load. Exact running-kernel
disassembly proves raw FIQ reaches `KiFIQException` and bugchecks `0x3D`, so the
narrow EL2 FIQ bridge remains required for stock Windows. Startup carrier
readiness observes Windows x18 and its exception stacks fail-closed; it never
rewrites x18, and routine exception-path diagnostics are compiled out.

**Current timer-path correction (2026-08-04):** the older timer-software-IRQ
description that used to occupy section 4 is historical and must not be used as
the Windows-ready design. Mu alone uses the reserved `HV_TIMER_*_SWIRQ` lines
before `ExitBootServices()`. Windows-ready timer delivery is per-CPU synthetic
`EVENT(2/3)` through the HCR.IMO|HCR.VI doorbell, with timer-write re-arm. The
J414s AIC2 target/SW_SET experiment did not establish a usable per-CPU target
encoding, so no AIC2 affinity mapping is inferred. The root
`INTERRUPT_ISSUE.md` is the authoritative condensed architecture record.

**Toolchain note (this DOES change the starting premise for future work on this
patch):** the task that produced this patch was framed as "cannot be built on this
macOS host, no aarch64 toolchain." That turned out to be wrong for *this* checkout: a
Homebrew LLVM + lld toolchain is present (`brew --prefix llvm`, `brew --prefix lld`),
and `make -j4` in this repo builds `build/m1n1.bin` successfully with
`ENABLE_NATIVE_AIC_PASSTHROUGH` defined -- zero errors. This was cross-checked by
diffing the compiler warning list between a build with the flag on and one with it off:
the *only* differences are three `-Wunused-function` warnings for
`handle_vgic_dist_access`/`handle_vgic_redist_access`/`handle_vgic_its_access` in
`hv_vgic.c`, which is exactly and only the expected consequence of no longer installing
those three `hv_map_hook()` calls. No new warnings, no errors, no change anywhere else.
**This validates C syntax, types, and macro/`#ifdef` correctness only.** It says nothing
about register semantics, timing, or runtime behavior -- those remain entirely
M1-gated, per the checklist at the bottom of this document.

## 1. Before / after architecture

**Before (`windows` branch, `ENABLE_VGIC_MODULE`, no `ENABLE_NATIVE_AIC_PASSTHROUGH`):**

- `HCR_EL2.{IMO,FMO}` both set (`src/hv.c`). Every physical IRQ *and* FIQ traps to EL2.
- `hv_vgicv3_init()` (`src/hv_vgic.c`) allocates and MMIO-hooks (`hv_map_hook()`) a full
  software GICv3 distributor + per-CPU redistributors + a stub ITS at fake addresses
  (`DIST_BASE_*`/`REDIST_BASE_*`/`ITS_BASE_*`). The guest sees a synthetic GICv3, not
  AIC.
- `hv_exc_irq()` (`src/hv_exc.c`) is the physical-IRQ handler: on every trapped AIC IRQ
  it calls `aic_ack()`, then translates the result into a GICv3 list-register injection
  (`hv_vgic3_inject_irq()`) so the guest's (also emulated) GICv3 CPU interface delivers
  it as a virtual IRQ. A UEFI `ArmGicDxe`-class driver, or a generic Windows GIC HAL
  extension, is expected to drive this.
- The timer FIQ is reflected the same way: `hv_update_fiq()` masks the physical
  CNTP/CNTV FIQ source and injects a GICv3 list-register virtual IRQ at a hardcoded
  vINTID (17 for CNTP, 18 for CNTV), falling back to a per-CPU software queue
  (`timer_queue`) when no list register is free, drained later from `hv_exc_irq()`'s
  GICv3-maintenance-interrupt branch.

**After (`ENABLE_NATIVE_AIC_PASSTHROUGH`, this patch):**

- `HCR_EL2.IMO` is clear in the idle native-AIC state and `HCR_EL2.FMO` stays set.
  Physical AIC IRQs go straight to the guest at EL1 with zero EL2 involvement.
  Physical FIQs (timer, PMU, Fast-IPI) still trap to EL2; timer reflection briefly
  arms per-CPU HCR.IMO|HCR.VI only for a synthetic wake.
- `hv_vgicv3_init()` no longer installs the three `hv_map_hook()` calls. The guest sees
  no synthetic GICv3 at all. It sees the *real* AIC MMIO, which was never hooked by
  anything in this tree in the first place (see &sect;3).
- `hv_exc_irq()` is not expected to fire for ordinary AIC IRQs anymore (that's the whole
  point of clearing IMO). It is left intact rather than deleted, with a new fail-closed
  fallback replacing the old "translate into a vGIC injection" tail, in case that
  assumption is ever wrong on real hardware (see &sect;6, OQ-3).
- Before ExitBootServices, Mu timer FIQ is reflected through reserved AIC software
  lines. After the Windows-ready AIC2 handoff, timer FIQ is reflected as a per-CPU
  synthetic AIC EVENT(2/3) through the HCR doorbell, not as an AIC2 SW_SET line and
  not as a GICv3 list-register timer injection.
- The GICv3 virtual-CPU-interface / list-register machinery (`hv_vgicv3_enable_virtual_
  interrupts()`, `hv_vgicv3_init_list_registers()`, `hv_vgic3_inject_irq()` and friends)
  is left running, unchanged, per-core. It is no longer used by the timer. It is kept
  only because the pre-existing, vestigial `ICC_SGI1R_EL1` SGI-emulation trap-and-emulate
  (`src/hv_exc.c`, `case SYSREG_ISS(ICC_SGI1R_EL1)`) still uses it, and a genuinely
  native-AIC Windows guest is not expected to exercise that path at all (see &sect;5).

## 2. HCR_EL2 change

`src/hv.c`, `hv_init()`, primary site (secondary cores inherit this value verbatim via
`hv_secondary_info.hcr` / `hv_init_secondary()`, see the comments at both call sites):

```
HCR_API | HCR_APK | HCR_TEA | HCR_RW | HCR_TSC | HCR_TID3 | HCR_AMO |
/* HCR_IMO intentionally omitted */
HCR_FMO | HCR_VM
```

Bit facts, cited from `src/arm_cpu_regs.h`:
- `HCR_IMO = BIT(4)` (line 177) -- routes physical Group 1 IRQ to EL2 when set.
- `HCR_FMO = BIT(3)` (line 178) -- routes physical FIQ to EL2 when set.
- `HCR_AMO = BIT(5)` (line 176) -- routes physical SError to EL2 (unchanged, stays set).

Clearing IMO is the entire mechanism by which "the guest drives AIC directly" is
achieved: it is a single bit, and nothing else in the patch does the routing work --
everything else (unhooking the vGIC MMIO, the AIC-IRQ fail-closed fallback) exists to
make sure the *rest* of the system is consistent with that one bit being clear.
`HCR_TID3` is untouched (still traps ID-group-3 registers so `ID_AA64PFR0_EL1` can be
faked for GICv3-CPU-interface detection, see the comment at
`src/hv_exc.c`'s `case SYSREG_ISS(ID_AA64PFR0_EL1)`).

## 3. AIC MMIO passthrough: nothing to change

Item 2 of the originating task asked for "make sure the real AIC MMIO is left mapped
straight through to the guest (no hooks)." This is already true, unconditionally, and
required no code change:

- The only place in this tree that ever calls `hv_map_hook()` against AIC's own MMIO
  base is `src/hv_aic.c`'s `hv_trace_irq()` (an opt-in host-debugger IRQ tracer), which
  is only ever invoked from `src/proxy.c:502` in response to an explicit proxy command
  from the *host* debug tool -- never from the boot path, never automatically.
- The vGIC distributor/redistributor/ITS hooks (`src/hv_vgic.c`, now gated out under
  this flag) were installed at entirely separate, synthetic addresses (`DIST_BASE_*`
  etc.) chosen to be "unoccupied MMIO space" -- they never targeted AIC's real MMIO
  region to begin with, in either the old or new design.
- Guest access to physical MMIO that has no explicit `hv_map_hook()`/`hv_map_sw()`
  falls through to whatever bulk stage-2 identity mapping the *host-side* boot tooling
  (the Python proxyclient driving `hv_map`/`hv_map_hw`, `src/proxy.c:479`) established
  before `hv_start()` was called -- that is outside this repository's C source and
  outside this patch's scope.

## 4. Timer-FIQ reflector: design decision and re-arm handshake

### 4.1 Current three-phase timer contract

The timer path has three deliberately different phases:

1. **Mu before ExitBootServices:** m1n1 keeps Apple timer FIQ at EL2. After Mu's
   timer handler is registered, the first asserted CNTP/CNTV source posts a
   reserved `HV_TIMER_P_SWIRQ(cpu)`/`HV_TIMER_V_SWIRQ(cpu)` through `AIC_SW_SET`.
   Mu's AIC handler completes that source through the existing timer SMC/clear
   handshake. These lines exist only for Mu and are cleared at each transition.
2. **Windows startup carrier:** before Windows enables native AIC2 CONFIG,
   m1n1 uses the compatibility GIC list-register carrier for the architected
   timer PPIs 30/27. This is a bounded bootstrap path for the kernel's per-CPU
   setup, not the steady-state Windows timer transport.
3. **Windows-ready native AIC:** FMO remains set so raw Apple timer FIQ never
   reaches EL1. m1n1 masks the physical source, sets per-CPU reflection state,
   and arms HCR.IMO|HCR.VI only while a synthetic wake is pending. The EVENT
   hook returns source value 2 for CNTP or 3 for CNTV; the HAL maps those
   processor-local values to its native timer source. This path does **not** call
   `aic_set_sw()` and does not use AIC2 target nibbles or AIC affinity.

### 4.2 Windows-ready state machine (CNTP and CNTV independently)

Implemented in `hv_update_fiq()` (`src/hv_exc.c`), called from every EL2 exception exit
(`hv_exc_exit()`, itself called from the sync-trap fast and slow paths, `hv_exc_fiq()`,
and `hv_exc_serr()`):

1. **Physical timer expires -> FIQ to EL2.** `HCR_EL2.FMO` stays set so the raw
   Apple timer FIQ cannot reach Windows at EL1.
2. **EL2 masks/suppresses that source.** It clears the corresponding
   `VM_TMR_FIQ_ENA_ENA_{P,V}` bit and, if no reflection is pending, sets the
   source's `*_reflection_pending` and `*_event_unread` state.
3. **EL2 arms a CPU-local synthetic wake.**
   `hv_native_aic_doorbell_sync()` treats `*_event_unread` as pending and arms
   HCR.IMO|HCR.VI. A second observation while pending is coalesced; it cannot
   create another synthetic event or re-enable the source.
4. **The EVENT hook supplies the local timer source.** A real non-reserved EVENT
   is passed through. Otherwise the hook returns EVENT value 3 for CNTV or 2
   for CNTP and clears only `*_event_unread`; `*_reflection_pending` remains set.
5. **The guest timer write completes the generation.** The trapped timer write,
   or the timer-completion SMC fallback, clears pending/unread state, re-enables
   that physical source, and resynchronizes HCR. EVENT consumption alone is never
   a re-arm.

The P and V sources have independent state. The per-source counters
`PERCPU(timer_p_fiq_count)` / `PERCPU(timer_v_fiq_count)` are monotonic
reflection-generation counters: they increment only on the first observation
while a source is not pending, not on every repeated observation of an asserted
condition. They are diagnostic state, not a delivered tick count.

### 4.3 Re-arm boundary and retained fallback

The source-register reflection macro calls `hv_timer_reflect_guest_rearm()` on
trapped timer writes. On hardware where ECV makes those accesses trap, this is
the synchronous write-side completion edge. The SMC magic (`"NTAIC"`, logical
sources 17/18) remains as a compatibility fallback for the HAL path.

The retained pass-through boundary is deliberate. With ECV/trapped timer
accesses, the write reaches EL2 synchronously and clears pending state at the
write itself. If a target instead gives EL1 direct, non-ECV timer access, the
write cannot enter EL2; the next `hv_update_fiq()` observation is the only
available fallback completion point. That timing limitation remains an explicit
hardware-validation item. Do not force a new timer transport or guess AIC2
affinity as part of this bridge audit.

### 4.4 Coalescing semantics

`timer_{p,v}_fiq_count` counts reflected generations, not every physical timer
expiration. It increments on the first observed asserted condition while that
source is not pending. While pending, the physical source stays masked and the
guest sees at most one synthetic EVENT. This is safe only if Windows recomputes
elapsed time from the architectural counter when it programs the next deadline;
the live J414s path still needs a trace to prove that behavior under delayed
service.

### 4.5 Why the GIC-list-register design was dropped

Implementing the original GIC-hybrid plan (inject the timer via `ICH_LR<n>_EL2`, vINTID
17/18, reusing `hv_vgic3_inject_irq()`) surfaced three concrete problems specific to
combining it with `HCR_EL2.IMO = 0`:

1. **The GICv3 virtual-CPU-interface maintenance interrupt's routing relative to
   `HCR_EL2.IMO` on Apple Silicon is unverified** (see OQ-3, &sect;6). On textbook ARM
   systems the maintenance interrupt is architecturally a physical interrupt subject to
   the same `IMO`/`FMO` routing as any other -- clearing IMO could mean it stops
   trapping to EL2 at all, which would silently break `hv_exc_irq()`'s
   list-register-cleanup housekeeping (the `type == 0` branch). This project's own
   existing code has a bare `//maintenance IRQ?` with a question mark at that exact
   check (`src/hv_exc.c`), i.e. even the original author of that logic wasn't certain.
2. **`timer_queue` (the LR-injection overflow path) had exactly one drain point**
   (`hv_exc_irq()`'s maintenance-interrupt branch), which is the same code whose
   liveness OQ-3 puts in question. If physical IRQ (and, possibly, the maintenance
   interrupt with it) genuinely stops trapping to EL2, an overflowed timer virq could be
   stranded forever with no other retry point in the old design.
3. **The vINTID numbers (17, 18) don't match SBSA convention** (PPI 30/27 for
   CNTP/CNTV), which is fine only if a to-be-written GTDT explicitly matches them --
   another moving part to get right and keep in sync, entirely avoided once the
   Windows-ready timer uses the synthetic EVENT(2/3) bridge (with Mu-only
   reserved software lines confined to the pre-ExitBootServices phase).

The Windows-ready synthetic bridge sidesteps all three: it does not touch list
registers and does not depend on the maintenance interrupt. The Mu-only
pre-ExitBootServices software-line path remains separate. The GICv3
virtual-CPU-interface machinery (`hv_vgicv3_enable_virtual_interrupts()`,
`hv_vgic3_inject_irq()`, etc.) is left in the tree because the pre-existing
`ICC_SGI1R_EL1` SGI emulation still references it (see &sect;7).

## 5. ACPI implication (native-AIC handoff)

The startup carrier still uses the architected GTDT timer PPIs 30/27 while Windows
is calibrating its clock. After native AIC2 CONFIG handoff, the Windows HAL's
processor-local timer source is the synthetic EVENT value 2/3 described in
section 4; it is not a reserved AIC2 software-line/target mapping:

- **Peripherals** remain direct native-AIC interrupts. The synthetic timer values
  are processor-local HAL sources and must not be advertised as ordinary
  hardware lines.
- **GTDT** remains a startup-carrier compatibility description. Whether Windows
  still consults any GTDT field after the native AIC handoff is a HAL contract;
  do not change it to encode the unvalidated reserved-line scheme.
- No GIC-describing ACPI structures (minimal MADT GICC/GICD/GICR entries) should be
  needed at all in the fully-AIC-native design, since the guest never touches the GICv3
  CPU interface for anything Windows is expected to use.

## 6. PMU / Fast-IPI FIQ handling (item 4)

Both are FIQ-class physical interrupt sources that keep trapping to EL2 because
`HCR_EL2.FMO` stays set (they are not fixed by clearing `IMO`, unlike ordinary AIC
IRQs). PMU remains fail-closed. Fast IPI now has a hardware-tested native-AIC
transport described below.

**PMU** (`src/hv_exc.c`, `hv_exc_fiq()`, the `SYS_IMP_APL_PMCR0` handling): unchanged.
The physical source is masked (IACT + IMODE cleared) on FIQ, and
`PERCPU(pmc_pending)` is only ever exposed to the guest via the pre-existing trapped
read of `SYS_IMP_APL_PMCR0` -- nothing wakes the guest to look at it, before or after
this patch. Noted as a low-risk future option, not implemented: `cpu_regs.h:461`
defines `PMCR0_IMODE_AIC` (route the PMU interrupt through AIC as an ordinary IRQ)
alongside `PMCR0_IMODE_FIQ`; nothing in this codebase currently requests `IMODE_AIC`.
Switching the guest-facing `SYS_PMCR_EL0` emulation to request it would let PMU
interrupts bypass EL2 the same way ordinary peripherals do -- unvalidated, out of scope
for this pass.

**Fast-IPI** (`SYS_IMP_APL_IPI_RR_LOCAL_EL1`/`IPI_RR_GLOBAL_EL1`/`IPI_SR_EL1`):
CPU-local, bypasses AIC entirely, and is also used by m1n1's own EL2 rendezvous.
On J414s the measured target encoding is Aff0 for `RR_LOCAL`, and Aff0 plus
Aff1 in bits 16..23 for `RR_GLOBAL`; it covers every present CPU in the
`0x0..0x3`, `0x10100..0x10102`, and `0x10200..0x10202` topology. Guest RR
writes are trapped, tagged before the physical send, and relayed as real Fast
IPIs. On the target, EL2 acknowledges the physical edge and turns only a
guest-tagged arrival into a synthetic AIC `EVENT_TYPE_IPI` wake driven by
`HCR.IMO|HCR.VI`.

The synthetic wake is transactional. `PERCPU(ipi_pending)` contains distinct
`DELIVERABLE` and `INFLIGHT` generations. Reading EVENT moves one generation to
`INFLIGHT`; it does not retire it. The Windows AIC HAL keeps the corresponding
IPI class active until its controller EOI and writes trapped `IPI_SR_EL1` there.
That write commits only `INFLIGHT`, preserving a newer `DELIVERABLE` send. Each
new transaction places a seven-bit generation in EVENT bits 30:24. EVENT bit 31
marks real NT IPI work migrated from the GIC startup carrier; the HAL dispatches
that classless origin as class 0, while explicitly retiring an ordinary
classless redundant Fast-IPI edge as spurious. Retries reuse the exact raw
token, so the HAL's comparison rejects an EOI delayed from an older Active
lifetime. If no EOI arrives, the local architectural-counter clock
rate-limits retries to at least 250 ms apart and continues until completion;
the diagnostic retry count saturates instead of abandoning the transaction.
This closes the observed failure in which NT retained its KPCR pending-vector
bit and KPRCB request node after HAL and m1n1 had both destructively cleared
their wake state.

The J414s target-code/SW_SET experiment does **not** establish a private AIC2
doorbell map: target 0 always arrived on CPU0, independent of writer CPU, and
targets 1..15 produced no observed EVENT. Do not replace Fast IPI with those
IRQ_CFG target nibbles. The complete ten-by-ten request matrix and live
per-target counter evidence are recorded in AuroraSilicon's
`drivers/AppleAic/J414S_FAST_IPI_TRANSPORT.md`.

## 7. What was disabled vs. kept

**Disabled** (gated behind `#ifndef ENABLE_NATIVE_AIC_PASSTHROUGH` / `#ifdef`, not
deleted):
- `HCR_EL2.IMO` (`src/hv.c`).
- The three `hv_map_hook()` calls in `hv_vgicv3_init()` for the GICD/GICR/ITS synthetic
  MMIO regions (`src/hv_vgic.c`).
- `hv_exc_irq()`'s translation of a real AIC HW/IPI event into a `hv_vgic3_inject_irq()`
  call (`src/hv_exc.c`) -- replaced with a fail-closed log-and-mask fallback.
- The GICv3 list-register-based timer reflection in the Windows-ready
  `hv_update_fiq()` path -- replaced with the synthetic HCR/AIC EVENT(2/3)
  handshake. The list-register carrier remains only for Windows startup.

**Kept, unchanged:**
- `HCR_EL2.FMO`, `HCR_EL2.TID3`.
- `hv_vgicv3_init()`'s in-memory distributor/redistributor/ITS struct
  allocation+initialization (now unused, kept to minimize diff).
- `hv_vgicv3_enable_virtual_interrupts()` / `hv_vgicv3_init_list_registers()` (per-core
  vCPU-interface enablement) -- still called from `hv_start()`/`hv_init_secondary()`,
  no longer used by the timer, kept for the vestigial `ICC_SGI1R_EL1` path.
- The `ICC_SGI1R_EL1` SGI trap-and-emulate (`src/hv_exc.c`) and its
  `sgi_queue`/list-register drain in `hv_exc_irq()`'s maintenance branch and in
  `hv_exc_fiq()`'s Fast-IPI-arrival handling -- unchanged, now understood to be
  vestigial for a genuinely-native-AIC guest (see &sect;6).
- `ID_AA64PFR0_EL1`'s GICv3-CPU-interface-present bit forcing, and the rest of the
  `HCR_TID3` ID-register pass-through table (`src/hv_exc.c`) -- unchanged; whether
  anything in the native-AIC boot path still needs it is OQ-5.
- All PMU virtualization (`SYS_PMCR_EL0`, `PMCNTEN*`, `PMOVS*`, etc.) -- entirely
  untouched by this patch.

**New:**
- `struct hv_pcpu_data`: `timer_p_fiq_count`, `timer_v_fiq_count`,
  `timer_p_reflection_pending`, `timer_v_reflection_pending` (`src/hv_exc.c`).
- `HV_TIMER_SWIRQ_BASE`/`HV_TIMER_P_SWIRQ()`/`HV_TIMER_V_SWIRQ()` macros
  (`src/hv_exc.c`) for the Mu-only pre-ExitBootServices compatibility path.
- `hv_timer_reflect_init()` (`src/hv_exc.c`, declared in `src/hv.h`), called once from
  `hv_init()` (`src/hv.c`).
- The fail-closed fallback in `hv_exc_irq()` (`src/hv_exc.c`).

## 8. Open questions

- **OQ-1 (Mu-only line range):** `HV_TIMER_SWIRQ_BASE = aic->nr_irq -
  (2 * MAX_CPUS)` reserves implementation-private lines used before
  `ExitBootServices()`. Their placement must remain free of real Mu claims, but
  they are not Windows-ready timer affinity targets.
- **OQ-1b (invalidated AIC2 affinity hypothesis):** `aic_set_affinity()` is a
  no-op for AIC2/AIC3, and the J414s target/SW_SET sweep did not produce a valid
  per-CPU target map. Windows-ready timer ownership is instead CPU-local HCR
  state plus synthetic EVENT(2/3). Do not implement or infer
  `AIC23_IRQ_CFG_TARGET` semantics without new hardware evidence.
- **OQ-2 (transactional Fast IPI, &sect;6):** confirm on hardware that every emitted
  synthetic EVENT is followed by a HAL EOI commit during clean SMP startup and
  that the bounded retry counter remains zero. A nonzero retry is recovery
  evidence and must be correlated with NT's KPCR+0xCC and KPRCB request queue.
- **OQ-3 (maintenance interrupt routing, &sect;4.5 point 1):** does the GICv3
  virtual-CPU-interface maintenance interrupt trap to EL2 independent of
  `HCR_EL2.IMO`, or does it share the same physical-IRQ routing gate as ordinary AIC
  IRQs on Apple Silicon specifically? This determines whether `hv_exc_irq()` (the
  physical-IRQ vector) is ever entered again at all under this patch, and thus whether
  its fail-closed fallback (&sect;7) is dead code or a real safety net.
- **OQ-4 (post-handoff GTDT use, &sect;5):** does Windows consult any GTDT timer
  field after native AIC2 handoff, or only during startup-carrier calibration?
  This is a Windows-HAL-internals question; it does not justify encoding the
  unvalidated reserved-line/AIC2-target scheme in ACPI.
- **OQ-5 (`ID_AA64PFR0_EL1`.GIC relevance):** is forcing the "GICv3 CPU interface
  present" bit still necessary for anything in a fully-AIC-native Windows boot path, now
  that the timer is no longer GIC-PPI-delivered? Left unchanged rather than guessed at
  either way (see `src/hv_exc.c`, `case SYSREG_ISS(ID_AA64PFR0_EL1)`).

## 9. J414S HARDWARE BRING-UP STATUS (2026-07-26)

The path has now run on an M2 Pro Mac14,9 (`apple,j414s`) with the current m1n1
RAM-chainloaded over the older resident proxy.  Observed hardware evidence:

- Mu starts with native-AIC passthrough active and reaches Windows Boot Manager.
- After waking the correct HPM and handing USB1's PHY/controller to the guest in
  host mode, Mu enumerates a Satechi NVMe enclosure as high-speed USB mass storage,
  validates its GPT/FAT ESP, and loads `EFI/BOOT/BOOTAA64.EFI` and `bootmgfw.efi`.
  SuperSpeed operation is not yet proven; the successful enumeration is USB2.
- The right-side XHC2 root hub has also been observed starting, but a later
  USB-C Ethernet test had no connector power.  The root cause is above xHCI:
  the internal USB2 PHY host-role bit does not configure the external
  CD3217/TPS6598x policy controller to source VBUS.  The unified baseline now
  carries a guarded, exact-readback Source/DFP policy handoff for every
  non-proxy HPM.  It is not yet live-validated and does not enable SuperSpeed.
- Windows reaches its kernel transition and executes PMUv3/PSCI and feature-register
  probes.  An EL2 undefined exception caused by operandless `TLBI VMALLE1OS` was
  fixed by explicitly issuing `TLBI VMALLE1IS` with the reserved `XZR` operand.
- The next captured failure was Windows recovery status `0xc000000d`, "Fatal error
  transitioning to the operating system."  Mu simultaneously rejected
  `ExitBootServices()` because runtime descriptors were not 64 KiB aligned.  The raw
  guest load address and T602x runtime code/data bins have been corrected; live
  verification of that correction is pending the next powered target run.

The checklist below therefore distinguishes what the hardware run has already
established from the remaining native-AIC correctness work.  Reaching the kernel is
not evidence that timer reflection, per-CPU affinity, IPIs, or peripheral interrupts
are fully correct.

### 2026-07-27 checkpoint

Later J414s runs advanced substantially beyond the first recovery failure:

- Keeping the x18/KPCR repair active after the AIC2 `CONFIG` handoff eliminated the
  repeatable `IRQL_NOT_LESS_OR_EQUAL (0xA)` at `KfRaiseIrql+4`. A four-E-core control
  configuration then ran for minutes with the Windows logo/spinner and no bugcheck.
  **Correction (2026-08-05):** exact running-PE disassembly later proved that
  every recorded zero was inside Windows' exception-vector hardening stub. The
  stub deliberately saves x18, counts it to zero through `SB`, and reloads it at
  RVAs `0x629c20/0x629c24`; the public-PDB-nearest name is
  `FrontendBhbSbKiUserExceptionHandler`. The apparent improvement was
  correlation, not proof that x18 caused the bugcheck. Commit `b2428a6b`
  removed the broad repair and passed the clean ten-core hardware gate. The
  independent Apple WFI-retention policy remains enforced.
- Windows can populate `TPIDR_EL1` before both of its exception stacks are usable.
  The carrier now requires writable `PanicStackBase` and `InterruptStackBase` ranges
  before delivering an SGI. This allows the first Avalanche core (logical CPU 4) to
  acknowledge and EOI its startup INTID 0; the earlier gate on only the panic stack
  entered `KxSwitchStackAndPlayInterrupt` with an unsafe interrupt stack.
- A software pending/active carrier is required for the short GIC compatibility
  window. The hardware ICH LR path worked on Blizzard but left the same interrupt
  pending indefinitely on Avalanche. HCR.VI now asserts only while a software queue
  has a priority-eligible, non-active Group-1 entry. The first Windows AIC2 `CONFIG`
  enable still removes this carrier permanently; normal operation remains native AIC.
- CPU4 receives exactly one startup SGI from CPU0, returns INTID 0 from IAR, and EOIs
  it. At EOI the SGI queue is empty and the coalesced count is zero. Windows sends no
  second SGI to CPU4, so the remaining stall is not a lost carrier notification.
- Normalizing `VPIDR_EL2` so Avalanche reports the Blizzard MIDR was tested and made
  no behavioral difference. All other trapped architectural feature registers were
  already identical across the two core types; that experiment was removed.
- A 5 kHz EL2 sampler found CPU4 alive after EOI with stable x18/SP, looping at kernel
  offsets `0x4fc4`, `0x13a88`, and `0x13a98` until the BSP times out and tears down the
  temporary KPCR mapping. The next hardware step is to capture/disassemble this loop
  and identify its waited-on value. The sampler itself was diagnostic-only and is not
  retained in the checkpoint commit.

The last session ended with stale USB CDC device nodes: neither proxy endpoint
answered NOPs after a chainload re-enumeration race. A physical cable replug or proxy
restart is required before that next capture; this is an external test-access blocker,
not a new guest failure.

## 10. M1-VALIDATION CHECKLIST

Most of the interrupt-correctness analysis above remains design reasoning from m1n1's
source plus the retained AuroraSilicon hardware evidence. The limited J414s evidence
in section 9 does not satisfy the timer/IPI/peripheral acceptance tests below.

**T0 -- sanity / does it boot at all**
- [ ] Boots a Windows guest (or, as a cheaper first step, m1n1's own test harness /a
      minimal EL1 payload that just spins reading `PMCR_EL0`/`CNTPCT_EL0`) under
      `hv_start()` with `ENABLE_NATIVE_AIC_PASSTHROUGH` defined, without an immediate
      SError/panic from `hv_exc_serr()`/`hv_panic()`.
- [ ] No FIQ-class exception is ever observed at EL1 in the guest (confirms
      `HCR_EL2.FMO` masking plus the synthetic EVENT(2/3) bridge is working, and
      that nothing else is leaking a raw FIQ through) -- this is the literal
      "no FIQ bugcheck" acceptance criterion from the originating task.

**T1 -- timer tick arrives, at all**
- [ ] The guest's clock ISR receives synthetic EVENT(2/3) at least once per
      present CPU after the Windows-ready AIC2 handoff. The Mu-only reserved
      SWIRQ numbers are not the Windows acceptance criterion.
- [ ] `PERCPU(timer_p_fiq_count)`/`PERCPU(timer_v_fiq_count)` (add a debug dump path,
      e.g. via the m1n1 proxy console) increments at the expected physical timer
      frequency.

**T2 -- the FIRST M1 trace, required before trusting PASS-THROUGH re-arm (&sect;4.3)**
- [ ] Record **every** guest system-register write to `CNTP_CTL_EL0`/`CNTP_CVAL_EL0`/
      `CNTP_TVAL_EL0` (and the `CNTV_*` equivalents) around a single clock interrupt,
      from FIQ-entry to the guest's next `WFI`/idle. Determine: (a) does Windows always
      rewrite the control/compare register on every reprogramming path, or are there
      optimized paths that only touch e.g. `TVAL`; (b) is there any path where Windows
      leaves the timer disabled/masked from the guest's perspective without an
      intervening m1n1 EL2 entry to notice via `hv_update_fiq()`'s poll.
- [ ] Cross-check against `hv_has_ecv` (`src/hv.c:127`) on the actual target chip: is
      ECV support detected, and does that match the ECV-vs-non-ECV re-arm-timing
      analysis in &sect;4.3?
- [ ] If T2 shows the non-ECV pass-through re-arm is not prompt enough (guest goes idle
      after reprogramming without another EL2 entry before the new deadline), the
      documented fallback is: force `CNTHCTL_EL1PTEN = 0` unconditionally (make the ECV
      trapped-and-emulated path the only path, regardless of hardware ECV support) --
      i.e. retroactively adopt TRAPPED/COOPERATIVE. Not implemented in this patch.

**T3 -- coalescing correctness (&sect;4.4)**
- [ ] Deliberately induce a second physical expiration while a reflection is still
      outstanding (e.g. by holding the guest off-core briefly via the m1n1 debugger).
      Confirm `timer_{p,v}_fiq_count` advanced by 2 (or more) while the guest still only
      observed one AIC interrupt, AND confirm Windows' timekeeping does not lose the
      elapsed time (i.e. the "recompute from the live counter" assumption in &sect;4.4
      holds). If it does not hold, this design needs a way to communicate the count to
      the guest -- not implemented here.

**T4 -- per-CPU ownership and affinity boundary**
- [x] J414s AIC2 target/SW_SET characterization rejects the proposed private
      per-CPU AIC2 doorbell map. No guessed target encoding is used.
- [ ] Trace that synthetic EVENT(2/3) is consumed on the same CPU whose timer
      FIQ set the pending state, including delayed service and both timer sources.

**T5 -- IPI path**
- [x] Confirm the hardware HAL executes Apple Fast-IPI RR instructions and that
      tagged arrivals become native-AIC synthetic EVENTs on all ten present
      J414s CPUs, including both performance clusters.
- [ ] Validate the transactional EOI build with no debugger mutation. Capture
      per-CPU send/tag/EVENT/commit counters plus `DELIVERABLE`, `INFLIGHT`, and
      retry count. Clean startup requires NT's vector-pending bit and request
      queue to drain without a manual `pcpu[].ipi_pending` write.

**T6 -- maintenance-interrupt routing (OQ-3)**
- [ ] Confirm whether `hv_exc_irq()` (the physical-IRQ vector, `src/hv_exc.c`) is ever
      entered at all once the guest is running with `HCR_EL2.IMO = 0`. If it is, capture
      what triggered it (the new fail-closed fallback logs loudly -- see &sect;7) and
      determine whether it was a genuine AIC HW/IPI event (meaning the IMO=0 assumption
      is violated somewhere) or the GICv3 maintenance interrupt (meaning OQ-3 resolves
      to "still routes independent of IMO," which is only relevant to the now-vestigial
      SGI path, &sect;7, and otherwise harmless).

**T7 -- peripheral IRQ passthrough (the actual point of this whole patch)**
- [ ] Confirm ordinary AIC-routed peripheral IRQs (anything other than the timer/IPI)
      reach the guest directly with no EL2 involvement / no measurable added latency
      versus bare metal, and that Windows' native AIC HAL extension can mask/unmask/
      ack/EOI them via AIC's real MMIO without faulting.
