"""Actual caller lookup ordering and constructor failed-domain admission."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index("\n}", source.index("{", start)) + 2] + "\n"


def test_lookup_failure_never_publishes_a_domain_and_failed_domain_never_progresses(tmp_path):
    usb = (ROOT / "src/usb.c").read_text()
    dwc = (ROOT / "src/usb_dwc3.c").read_text()
    start = dwc.index("dwc3_dev_t *usb_dwc3_init(")
    prefix = dwc[start:dwc.index("#ifdef J700_CDC_PROXY", start)]
    harness = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
typedef uint32_t u32;
typedef struct { bool failed; } dart_dev_t;
#define CDC_ACM_PIPE_MAX 2
typedef struct { bool failed, dma_unsafe; struct { bool ready; } pipe[2]; } dwc3_dev_t;
static dwc3_dev_t device, *usb_retained_dev;
static dart_dev_t domain;
static bool usb_shutdown_failed;
static int fault, lookups, domains, published, controllers;
static char order[8]; static unsigned ordered;
struct usb_drd_regs { uintptr_t drd_regs; };
static bool usb_dwc3_shutdown_failed(void) { return usb_shutdown_failed; }
static bool dart_has_failed(dart_dev_t *d) { return d->failed; }
static int usb_drd_get_regs(unsigned idx, struct usb_drd_regs *r) {
    lookups++; order[ordered++] = 'M'; r->drd_regs = 0x1000; return fault == 1 ? -1 : 0;
}
static dart_dev_t *usb_dart_init(unsigned idx) {
    domains++; order[ordered++] = 'D';
    if (fault == 2) return NULL;
    published++; domain.failed = fault == 3; return &domain;
}
'''
    harness += function(dwc, "static void usb_dwc3_mark_unsafe(")
    harness += prefix + "    controllers++; order[ordered++] = 'H';\n    return &device;\n}\n"
    harness += function(usb, "dwc3_dev_t *usb_iodev_bringup(")
    harness += r'''
int main(int argc, char **argv) {
    assert(argc == 2); fault = atoi(argv[1]); usb_shutdown_failed = fault == 4;
    dwc3_dev_t *result = usb_iodev_bringup(0);
    if (!fault) assert(result && !strcmp(order, "MDH") && domains == 1 && controllers == 1);
    if (fault == 1) assert(!result && lookups == 1 && !domains && !published && !controllers);
    if (fault == 2) assert(!result && lookups == 1 && domains == 1 && !published && !controllers);
    if (fault == 3) assert(!result && lookups == 1 && published == 1 && !controllers && usb_shutdown_failed);
    if (fault == 4) assert(!result && !lookups && !domains && !published && !controllers);
    return 0;
}
'''
    binary = tmp_path / "bringup-admission"
    subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-parameter", "-x", "c", "-", "-o", str(binary)],
                   input=harness, text=True, check=True)
    for case in range(5):
        subprocess.run([str(binary), str(case)], check=True)
