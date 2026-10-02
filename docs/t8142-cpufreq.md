# M5 CPU frequency initialization

T8142 has two cluster controllers at `0x210e00000` and `0x211e00000`.
Their command registers at offset `0x20020` use five-bit P-state IDs. J813
firmware hands over ECPU state 2 (972 MHz) and PCPU state 10 (3516 MHz).
Initialization visits state 1 and returns to these handoff states, using
bounded busy polling and command-state readback.

M5 uses firmware CLPC/PMP management. The older SoCs' voltage, PLL and
throttling feature registers are left untouched. This support initializes
boot states; it does not implement a Linux scaling governor or thermal policy.
Command-state readback is not a measurement of the effective CPU clock.

Build with `make RELEASE=1`, then RAM-chainload `build/m1n1.macho` using a
client matching the resident proxy ABI. With this candidate running:

```sh
python3 proxyclient/tools/validate_t8142_cpufreq.py \
    --device /dev/cu.kis-00100000-ch-0 --output cpufreq-proof.json
```

The device path is host-specific. The tool requires J813/T8142 on KIS and
refuses unexpected initial states. It performs three initialization cycles,
checking return values, state readback, busy completion and preservation of
unrelated command bits. It saves a JSON artifact. All three cycles passed on
J813 with command values `0x400102` and `0x40010a` before and after.

State 1 is a 300 MHz floor on both clusters. The SRAM voltage tables start
at state 2, not state 1. A dependent decrement loop timed against CNTPCT_EL0
on J813 measured approximately 300/972 MHz for E states 1/2 and
300/1308/1608/1932/2220/2508/2820/3072/3300/3516 MHz for P states 1 through
10. These short measurements confirm the state-to-frequency mapping; they
do not establish sustained performance or thermal behavior.

After a timeout, recover through VDM; do not issue further DVFS requests on
that boot. States above the firmware handoff values remain unqualified.
