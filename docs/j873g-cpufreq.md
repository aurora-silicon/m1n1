# J873g CPU frequency requests

Failure cases include the wrong board or PMGR2 aperture, duplicate register-map
IDs, malformed or non-monotonic ADT frequency tables, a wrong state-index bias,
a stuck busy bit and an unacknowledged request. Validate the complete layout
before writing. Preserve firmware voltage, PLL and throttling configuration.

The explicit `cpufreq_get_cluster_hz()` and `cpufreq_set_cluster_pstate()` APIs
use two domains: E (0) and shared P/M (1). The getter reports the acknowledged
request, not a measured clock. A failed transition disables further requests
for that domain until reset. Startup does not change either clock.

For E2E validation, record the exact image hash, identity, initial requests and
ADT tables. Time dependent workloads using the architectural counter on an E
CPU and a P CPU before, during and after a request round trip. Check busy clears,
the acknowledged index matches, elapsed-time ratios match the requested clocks,
and the original requests are restored. Retain JSON measurements and raw logs.
This does not qualify higher-state thermal policy or independent M-core clocks.
