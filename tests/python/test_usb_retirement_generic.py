"""Actual generic retirement commands, completion matching and status failures."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def function(source, signature):
    start = source.index(signature)
    return source[start:source.index("\n}", source.index("{", start)) + 2] + "\n"


def compile_run(tmp_path, text):
    binary = tmp_path / "fixture"
    subprocess.run(["cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror",
                    "-Wno-unused-function", "-Wno-unused-parameter", "-I", str(ROOT / "src"),
                    "-x", "c", "-", "-o", str(binary)], input=text, text=True, check=True)
    subprocess.run([str(binary)], check=True)


def test_forced_resource_index_end_includes_ep0_and_waits_matching_completion(tmp_path):
    source = (ROOT / "src/usb_dwc3.c").read_text()
    harness = '#include "types.h"\n' + r'''
#include <assert.h>
#include <string.h>
#include "usb_dwc3_regs.h"
#define MAX_ENDPOINTS 16
#define CDC_ACM_PIPE_MAX 2
#define USB_LEP_CTRL_IN 1
#define usb_debug_printf(...) ((void)0)
#define TRB_BUFFER_IOVA 0xf00d0000
#define XFER_BUFFER_IOVA 0xbabe0000
#define SCRATCHPAD_IOVA 0xbeef0000
#define EVENT_BUFFER_IOVA 0xdead0000
#define TRB_BUFFER_SIZE 16384
#define XFER_BUFFER_SIZE 16384
#define DWC3_SCRATCHPAD_SIZE 16384
#define max(a,b) ((a) > (b) ? (a) : (b))
typedef struct {
    uintptr_t regs; bool failed, dma_unsafe, shutting_down;
    void *dart,*evtbuffer,*scratchpad,*xferbuffer,*trbs;
    struct { bool ready; void *device2host,*host2device; } pipe[2];
    struct { bool xfer_in_progress, end_cmd_pending; u8 resource_index; } endpoints[16];
} dwc3_dev_t;
static bool usb_shutdown_failed;
static dwc3_dev_t *usb_retained_dev;
static unsigned unmaps,frees;
static bool dart_has_failed(void *d) { return false; }
static void dart_unmap(void *d,u64 iova,size_t n) { unmaps++; }
static bool dart_shutdown_checked(void *d) { return true; }
static void mock_free(void *p) { frees++; }
static void ringbuffer_free(void *p) { frees++; }
#define free mock_free
static u32 register_value, submitted[16];
static unsigned last_ep, events, ticks, reads, writes, fail_mode;
static u32 read32(u64 addr) { reads++; return register_value; }
static void write32(u64 addr,u32 value) {
    writes++;
    for (unsigned ep=0;ep<16;ep++) if (addr == DWC3_DEPCMD(ep)) {
        submitted[ep] = value; last_ep=ep;
    }
}
static void set32(u64 a,u32 v) { write32(a,v); }
static void clear32(u64 a,u32 v) { write32(a,v); }
static int poll32(u64 a,u32 m,u32 t,unsigned us) { return fail_mode == 2 ? -1 : 0; }
static u64 timeout_calculate(unsigned us) { assert(us==100000); return 3; }
static bool timeout_expired(u64 end) { return ++ticks > end; }
'''
    # Exact production command-completion branch, without unrelated data handlers.
    start = source.index("static void usb_dwc3_handle_event_ep(")
    end = source.index("\n#ifdef J700_CDC_PROXY\n    if (event.endpoint_event", start)
    harness += source[start:end] + "\n}\n"
    harness += r'''
static void usb_dwc3_handle_events(dwc3_dev_t *d) {
    events++;
    struct dwc3_event_depevt event = {.endpoint_number=last_ep,
        .endpoint_event=DWC3_DEPEVT_EPCMDCMPLT,
        .parameters=(fail_mode == 1 ? DWC3_DEPCMD_STARTTRANSFER : DWC3_DEPCMD_ENDTRANSFER) << 8};
    usb_dwc3_handle_event_ep(d,event);
}
'''
    harness += function(source, "static void usb_dwc3_mark_unsafe(")
    harness += function(source, "static int usb_dwc3_ep_command(")
    harness += function(source, "static int usb_dwc3_end_transfer(")
    harness += function(source, "bool usb_dwc3_shutdown(")
    harness += r'''
int main(void) {
    dwc3_dev_t dev = {.shutting_down=true};
    dev.endpoints[0].resource_index=7; dev.endpoints[4].resource_index=19;
    for(unsigned ep=0;ep<=4;ep+=4) {
        ticks=0; assert(usb_dwc3_end_transfer(&dev,ep)==0);
        assert(submitted[ep] & DWC3_DEPCMD_HIPRI_FORCERM);
        assert(submitted[ep] & DWC3_DEPCMD_CMDIOC);
        assert(DWC3_DEPCMD_GET_RSC_IDX(submitted[ep]) == dev.endpoints[ep].resource_index);
        assert(!dev.endpoints[ep].end_cmd_pending);
    }
    fail_mode=1; ticks=0;
    assert(usb_dwc3_end_transfer(&dev,0)==-1);
    assert(events > 0);
    fail_mode=2; ticks=0; unsigned before=reads;
    assert(usb_dwc3_end_transfer(&dev,4)==-1 && reads==before);
    fail_mode=0;
    for(unsigned status=1;status<16;status++) {
        register_value=status<<12; ticks=0; before=events;
        assert(usb_dwc3_end_transfer(&dev,4)==-1 && events==before);
    }
    register_value=0; ticks=0; dev.endpoints[0].xfer_in_progress=true;
    dev.endpoints[4].xfer_in_progress=true;
    submitted[0]=submitted[4]=0;
    assert(usb_dwc3_shutdown(&dev));
    assert(submitted[0] && submitted[4] && unmaps==4 && frees==9);
    dev.failed=dev.dma_unsafe=false; usb_shutdown_failed=false;
    fail_mode=1; ticks=0; unmaps=frees=0;
    assert(!usb_dwc3_shutdown(&dev));
    assert(usb_shutdown_failed && dev.failed && dev.dma_unsafe && !unmaps && !frees);
    return 0;
}
'''
    compile_run(tmp_path, harness)
