"""Retirement-only completion tracking from production event/end/shutdown bodies."""
from pathlib import Path
import subprocess
import pytest
from test_usb_shutdown_lifetime import function

@pytest.mark.parametrize('j700',[False,True])
def test_later_terminal_completion_does_not_issue_stale_end_and_never_rearms(tmp_path,j700):
    ep_handler=function('src/usb_dwc3.c','static void usb_dwc3_handle_event_ep(')
    # Keep the exact bounds/IOC dispatch prefix; traffic handlers must stay unused.
    ep_handler=ep_handler[:ep_handler.index('\n#ifdef J700_CDC_PROXY')]+ '\n    traffic_calls++;\n}\n'
    harness=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <stddef.h>
typedef uint8_t u8;typedef uint32_t u32;typedef uint64_t u64;
#define MAX_ENDPOINTS 16
#define CDC_ACM_PIPE_MAX 2
#define DWC3_DEPEVT_XFERCOMPLETE 1
#define DWC3_DEPEVT_XFERINPROGRESS 2
#define DWC3_DEPEVT_XFERNOTREADY 3
#define DWC3_DEPEVT_EPCMDCMPLT 7
#define DEPEVT_STATUS_BUSERR 1
#define DWC3_EVENT_TYPE_DEV 0
#define DWC3_DEPCMD_ENDTRANSFER 8
#define DWC3_DEPCMD_CMDIOC 0x100
#define DWC3_DEPCMD_HIPRI_FORCERM 0x800
#define DWC3_DEPCMD_PARAM(x) ((x)<<16)
#define DWC3_DEVTEN 10
#define DWC3_DALEPENA 20
#define DWC3_DCTL 30
#define DWC3_DSTS 40
#define DWC3_DSTS_DEVCTRLHLT 1
#define DWC3_DCTL_RUN_STOP 2
#define DWC3_DCTL_CSFTRST 4
#define TRB_BUFFER_IOVA 100
#define XFER_BUFFER_IOVA 200
#define SCRATCHPAD_IOVA 300
#define EVENT_BUFFER_IOVA 400
#define TRB_BUFFER_SIZE 16
#define XFER_BUFFER_SIZE 16
#define DWC3_SCRATCHPAD_SIZE 16
#define DWC3_EVENT_BUFFERS_SIZE 16
#define SZ_16K 16384
#define max(a,b) ((a)>(b)?(a):(b))
#define usb_debug_printf(...) ((void)0)
struct dwc3_event_depevt {unsigned one:1;unsigned endpoint_number:5;unsigned endpoint_event:4;unsigned reserved:2;unsigned status:4;unsigned parameters:16;};
union dwc3_event {uint32_t raw;struct{unsigned is_devspec:1;unsigned type:7;unsigned rest:24;}type;struct dwc3_event_depevt depevt;unsigned devt;};
typedef struct {u64 regs;void *dart,*evtbuffer,*scratchpad,*xferbuffer,*trbs;bool failed,dma_unsafe,shutting_down;u32 before;
struct{bool xfer_in_progress,end_cmd_pending;u8 resource_index;}endpoints[MAX_ENDPOINTS];u32 after;
struct{bool ready;void *device2host,*host2device;}pipe[CDC_ACM_PIPE_MAX];}dwc3_dev_t;
static bool usb_shutdown_failed,halted,reset_done;static dwc3_dev_t *usb_retained_dev;
static unsigned traffic_calls,device_calls,commands,pumps,iocs,unmaps,frees,writes,polls,ticks;
static u8 current_ep;static int fault;
static void write32(u64 a,u32 v){writes++;assert(!unmaps&&!frees);}
static void clear32(u64 a,u32 v){writes++;assert(!unmaps&&!frees);}
static void set32(u64 a,u32 v){writes++;assert(halted&&!unmaps&&!frees);}
static int poll32(u64 a,u32 m,u32 v,unsigned t){assert(t==1000);polls++;
if(a==DWC3_DSTS){if(fault==2)return-1;halted=true;}
else if(a==DWC3_DCTL){assert(halted);if(fault==3)return-1;reset_done=true;}
return 0;}
static void dart_unmap(void*d,u64 a,size_t n){assert(halted&&reset_done&&iocs&&commands&&traffic_calls==0&&device_calls==0);unmaps++;}
static bool dart_has_failed(void*d){return false;}
static bool dart_shutdown_checked(void*d){assert(halted&&reset_done&&unmaps==4);return true;}
static void ringbuffer_free(void*p){assert(unmaps==4&&halted&&reset_done);}
static void mock_free(void*p){assert(unmaps==4&&halted&&reset_done);frees++;}
#define free mock_free
static int usb_dwc3_ep_command(dwc3_dev_t*d,u8 ep,u32 c,u32 a,u32 b,u32 v){
assert(d->shutting_down&&d->endpoints[ep].end_cmd_pending&&(c&DWC3_DEPCMD_CMDIOC));assert((c&15)==DWC3_DEPCMD_ENDTRANSFER);
#ifdef J700_CDC_PROXY
assert(!(c&DWC3_DEPCMD_HIPRI_FORCERM));
#else
assert(c&DWC3_DEPCMD_HIPRI_FORCERM);
#endif
assert((c>>16)==d->endpoints[ep].resource_index);if(fault<4)assert(ep==2);else assert(ep==2||ep==4);
commands++;current_ep=ep;pumps=0;return 0;}
static u64 timeout_calculate(u32 t){assert(t==100000);ticks=0;return 4;}
static bool timeout_expired(u64 d){return ++ticks>4;}
static void usb_dwc3_handle_event_dev(dwc3_dev_t*d,unsigned e){device_calls++;}
'''+ep_handler+function('src/usb_dwc3.c','static void usb_dwc3_handle_event(dwc3_dev_t *dev,')+r'''
static void inject(dwc3_dev_t*d,u8 ep,unsigned kind,unsigned status,bool device){union dwc3_event e={0};e.depevt.endpoint_number=ep;e.depevt.endpoint_event=kind;e.depevt.status=status;e.type.is_devspec=device;if(kind==7)e.depevt.parameters=DWC3_DEPCMD_ENDTRANSFER<<8;usb_dwc3_handle_event(d,e);}
static void usb_dwc3_handle_events(dwc3_dev_t*d){pumps++;
if(pumps==1){
if(current_ep==2){
unsigned kind=fault==6?2:fault==7?3:1;inject(d,fault==5?31:4,kind,fault==4?1:0,fault==8);
if(fault<4)assert(!d->endpoints[4].xfer_in_progress&&!d->endpoints[4].resource_index);
else assert(d->endpoints[4].xfer_in_progress&&d->endpoints[4].resource_index==7);
}
/* Terminal completion of the earlier endpoint must not acknowledge END IOC. */
inject(d,current_ep,1,0,false);assert(!d->endpoints[current_ep].xfer_in_progress&&!d->endpoints[current_ep].resource_index&&d->endpoints[current_ep].end_cmd_pending&&!writes&&!unmaps&&!frees);
assert(!traffic_calls&&!device_calls);return;}
if(fault==1)return;
inject(d,current_ep,7,0,false);assert(!d->endpoints[current_ep].end_cmd_pending);iocs++;}
'''+function('src/usb_dwc3.c','static int usb_dwc3_end_transfer(')+function('src/usb_dwc3.c','static void usb_dwc3_mark_unsafe(')+function('src/usb_dwc3.c','bool usb_dwc3_shutdown(')+r'''
int main(int argc,char**argv){assert(argc==2);fault=atoi(argv[1]);dwc3_dev_t d={0};d.dart=&d;d.before=0x12345678;d.after=0x87654321;d.endpoints[2].xfer_in_progress=d.endpoints[4].xfer_in_progress=true;d.endpoints[2].resource_index=3;d.endpoints[4].resource_index=7;d.pipe[0].ready=d.pipe[1].ready=true;
bool result=usb_dwc3_shutdown(&d);assert(!traffic_calls&&!device_calls&&d.before==0x12345678&&d.after==0x87654321);
if(fault>=1&&fault<=3){assert(!result&&usb_shutdown_failed&&d.failed&&d.dma_unsafe&&!unmaps&&!frees);if(fault==1)assert(!iocs&&!writes&&!polls);unsigned previous=commands;assert(!usb_dwc3_shutdown(&d)&&commands==previous);}
else {assert(result&&!usb_shutdown_failed&&halted&&reset_done&&unmaps==4&&frees==5);assert(commands==(fault<4?1U:2U));assert(iocs==commands);}
return 0;}
'''
    binary=tmp_path/'fixture';flags=['-DJ700_CDC_PROXY'] if j700 else []
    subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-fsanitize=address,undefined',*flags,'-x','c','-','-o',str(binary)],input=harness,text=True,check=True)
    for case in range(9):subprocess.run([str(binary),str(case)],check=True)
