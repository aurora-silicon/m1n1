# M5/M6 integration regression checks

The shared entry path must preserve both M5's quiet WFE dispatch and M6's
private-stack/MMU handshake. Failure modes include applying a new chip's
release bitmap or fast-IPI policy to older CPUs, changing legacy CPU tuning,
altering PMGR requests, or sending legacy ASC traffic through the new mailbox
format. A successful M6 boot alone cannot rule these out.

Build the baseline and candidate with the same compiler and settings, retaining
their source trees and `build/m1n1-raw.elf` files. The integration baseline used
here is `ae94c931bbbaef6ce97e6caf1fcef453edb3e3e6`. With Python dependencies
`unicorn`, `capstone` and `pyelftools`, run:

```sh
python3 proxyclient/tools/validate_legacy_boot.py /path/to/baseline /path/to/candidate --output /path/to/new-receipt
```

The checker relocates and executes the compiled AArch64 code. It compares
primary/secondary reset dispatch at EL1/EL2/EL3, CPU-register tuning, frequency
setup with firmware features enabled/disabled and poll failure, WFE-mode
dispatch, and legacy ASC send/receive. The receipt records both ELF hashes and
per-case trace hashes; a mismatch preserves the differing traces. Output must
be a new directory.

PMGR feature results, poll completion and architectural-register inputs are
modeled. During reset-entry replay, downstream initialization calls are recorded
at their boundaries; CPU tuning and frequency code then execute separately.
This checks observable behavior in the stated model, not hardware timing,
coherency, thermal behavior, or display-firmware compatibility on older devices.
Unsupported baseline paths remaining unsupported are not new support claims.

The integrated build passed 105 cases across 35 A7–M4/A18 Pro CPU parts. Physical
M6 qualification separately passed twelve CPUs, concurrent atomic work, frequency
round trips and USB-C display through U-Boot into Linux. The operator confirmed
the integrated Linux console and test banner on the monitor. Older devices were
not physically rebooted by this qualification.
