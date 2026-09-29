"""Keep HPM selection inside the fixed USB interrupt-state array."""

from pathlib import Path
import subprocess


def test_hpm_restore_index_bounds(tmp_path):
    source = (Path(__file__).resolve().parents[2] / "src/usb.c").read_text()
    idx = source[source.index("static int hpm_idx("):source.index("static bool usb_init_match(")]
    restore = source[source.index("static bool usb_hpm_restore_irqs_match("):
                     source.index("static int usb_hpm_restore_irqs_one(")]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
typedef uint8_t u8;
#define FIRST_USB_IODEV 0
#define USB_IODEV_COUNT 4
#define IODEV_USB0 5
static struct { bool valid; } tps6598x_irq_state[USB_IODEV_COUNT];
static int iodev_get_usage(int dev) { (void)dev; return 0; }
''' + idx + restore + r'''
int main(void)
{
    bool force = true;
    tps6598x_irq_state[3].valid = true;
    assert(usb_hpm_restore_irqs_match("/arm-io/hpm3", &force));
    assert(!usb_hpm_restore_irqs_match("/arm-io/hpm4", &force));
    assert(!usb_hpm_restore_irqs_match("/arm-io/hpm9", &force));
    assert(!usb_hpm_restore_irqs_match("/arm-io/hpmx", &force));
    return 0;
}
'''
    binary = tmp_path / "usb-hpm-bounds"
    subprocess.run(["cc", "-std=c11", "-Wall", "-Wextra", "-Werror", "-x", "c", "-",
                    "-o", str(binary)], input=harness, text=True, check=True)
    subprocess.run([str(binary)], check=True)
