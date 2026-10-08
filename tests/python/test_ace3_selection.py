"""Exercise ACE3 register selection before allowing logical-register data reads."""
from pathlib import Path
import subprocess


def test_selector_matches_requested_register(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source = (repo / "src/ace3.c").read_text()
    start = source.index("#define ACE3_SPMI_SELECT")
    source = source[start:]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stddef.h>
typedef uint8_t u8;
typedef uint64_t u64;
typedef struct {int unused;} spmi_dev_t;
#define BIT(n) (1U << (n))
static u8 selectors[4];
static unsigned selector_count, selector_index, payload_reads, selections;
static unsigned ticks;
static int spmi_reg0_write(spmi_dev_t *dev, u8 sid, u8 reg) {
    assert(sid == 12 && reg == 0x1a); selections++; return 0;
}
static int spmi_ext_read(spmi_dev_t *dev, u8 sid, u8 reg, u8 *data, size_t len) {
    assert(sid == 12);
    if (reg == 0) {
        assert(len == 1 && selector_index < selector_count);
        *data = selectors[selector_index++];
    } else if (reg == 0x1f) {
        assert(len == 1); *data = 4;
    } else {
        assert(reg == 0x20 && len == 4);
        payload_reads++;
        data[0] = 0x11; data[1] = data[2] = data[3] = 0;
    }
    return 0;
}
static u64 timeout_calculate(unsigned usec) {assert(usec == 20000); ticks = 0; return 10;}
static bool timeout_expired(u64 deadline) {return ++ticks >= deadline;}
static void udelay(unsigned usec) {assert(usec == 50);}
''' + source + r'''
static void reset(void) {selector_index = payload_reads = selections = 0;}
int main(void) {
    spmi_dev_t dev = {0}; u8 data[4];
    reset(); selectors[0] = 0x1a; selector_count = 1;
    assert(ace3_read(&dev, 12, 0x1a, data, 4) == 4);
    assert(selections == 1 && payload_reads == 1 && data[0] == 0x11);
    reset(); selectors[0] = 0x9a; selectors[1] = 0x1a; selector_count = 2;
    assert(ace3_read(&dev, 12, 0x1a, data, 4) == 4);
    assert(selector_index == 2 && payload_reads == 1);
    reset(); selectors[0] = 0x03; selector_count = 1;
    assert(ace3_read(&dev, 12, 0x1a, data, 4) < 0);
    assert(selector_index == 1 && payload_reads == 0);
    reset(); selectors[0] = 0x83; selector_count = 1;
    assert(ace3_read(&dev, 12, 0x1a, data, 4) < 0);
    assert(selector_index == 1 && payload_reads == 0);
    reset(); selectors[0] = 0x9a; selectors[1] = 0x03; selector_count = 2;
    assert(ace3_read(&dev, 12, 0x1a, data, 4) < 0);
    assert(selector_index == 2 && payload_reads == 0);
    return 0;
}
'''
    cfile = tmp_path / "ace3.c"
    binary = tmp_path / "ace3-test"
    cfile.write_text(harness)
    subprocess.run(["cc", "-std=c11", str(cfile), "-o", str(binary)], check=True)
    subprocess.run([str(binary)], check=True, capture_output=True)
