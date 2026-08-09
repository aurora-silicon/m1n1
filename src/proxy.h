/* SPDX-License-Identifier: MIT */

#ifndef __PROXY_H__
#define __PROXY_H__

#include "types.h"

typedef enum {
    P_NOP = 0x000, // System functions
    P_EXIT,
    P_CALL,
    P_GET_BOOTARGS,
    P_GET_BASE,
    P_SET_BAUD,
    P_UDELAY,
    P_SET_EXC_GUARD,
    P_GET_EXC_COUNT,
    P_EL0_CALL,
    P_EL1_CALL,
    P_VECTOR,
    P_GL1_CALL,
    P_GL2_CALL,
    P_GET_SIMD_STATE,
    P_PUT_SIMD_STATE,
    P_REBOOT,
    P_SLEEP,
    P_EL3_CALL,
    P_GET_CHIPID,
    P_GET_CPU_FEATURES,

    P_WRITE64 = 0x100, // Generic register functions
    P_WRITE32,
    P_WRITE16,
    P_WRITE8,
    P_READ64,
    P_READ32,
    P_READ16,
    P_READ8,
    P_SET64,
    P_SET32,
    P_SET16,
    P_SET8,
    P_CLEAR64,
    P_CLEAR32,
    P_CLEAR16,
    P_CLEAR8,
    P_MASK64,
    P_MASK32,
    P_MASK16,
    P_MASK8,
    P_WRITEREAD64,
    P_WRITEREAD32,
    P_WRITEREAD16,
    P_WRITEREAD8,

    P_MEMCPY64 = 0x200, // Memory block transfer functions
    P_MEMCPY32,
    P_MEMCPY16,
    P_MEMCPY8,
    P_MEMSET64,
    P_MEMSET32,
    P_MEMSET16,
    P_MEMSET8,

    P_IC_IALLUIS = 0x300, // Cache and memory ops
    P_IC_IALLU,
    P_IC_IVAU,
    P_DC_IVAC,
    P_DC_ISW,
    P_DC_CSW,
    P_DC_CISW,
    P_DC_ZVA,
    P_DC_CVAC,
    P_DC_CVAU,
    P_DC_CIVAC,
    P_MMU_SHUTDOWN,
    P_MMU_INIT,
    P_MMU_DISABLE,
    P_MMU_RESTORE,
    P_MMU_INIT_SECONDARY,

    P_XZDEC = 0x400, // Decompression and data processing ops
    P_GZDEC,

    P_SMP_START_SECONDARIES = 0x500, // SMP and system management ops
    P_SMP_CALL,
    P_SMP_CALL_SYNC,
    P_SMP_WAIT,
    P_SMP_SET_WFE_MODE,
    P_SMP_IS_ALIVE,
    P_SMP_STOP_SECONDARIES,
    P_SMP_CALL_EL1,
    P_SMP_CALL_EL1_SYNC,
    P_SMP_CALL_EL0,
    P_SMP_CALL_EL0_SYNC,

    P_HEAPBLOCK_ALLOC = 0x600, // Heap and memory management ops
    P_MALLOC,
    P_MEMALIGN,
    P_FREE,
    P_TOP_OF_MEMORY_ALLOC,
    P_HEAPBLOCK_SET_LIMIT,

    P_KBOOT_BOOT = 0x700, // Kernel boot ops
    P_KBOOT_SET_CHOSEN,
    P_KBOOT_SET_INITRD,
    P_KBOOT_PREPARE_DT,
    P_KBOOT_SET_UBOOT,

    P_PMGR_POWER_ENABLE = 0x800, // power/clock management ops
    P_PMGR_POWER_DISABLE,
    P_PMGR_ADT_POWER_ENABLE,
    P_PMGR_ADT_POWER_DISABLE,
    P_PMGR_RESET,

    P_IODEV_SET_USAGE = 0x900,
    P_IODEV_CAN_READ,
    P_IODEV_CAN_WRITE,
    P_IODEV_READ,
    P_IODEV_WRITE,
    P_IODEV_WHOAMI,
    P_USB_IODEV_VUART_SETUP,

    P_TUNABLES_APPLY_GLOBAL = 0xa00,
    P_TUNABLES_APPLY_LOCAL,
    P_TUNABLES_APPLY_LOCAL_ADDR,

    P_DART_INIT = 0xb00,
    P_DART_SHUTDOWN,
    P_DART_MAP,
    P_DART_UNMAP,

    P_HV_INIT = 0xc00,
    P_HV_MAP,
    P_HV_START,
    P_HV_TRANSLATE,
    P_HV_PT_WALK,
    P_HV_MAP_VUART,
    P_HV_TRACE_IRQ,
    P_HV_WDT_START,
    P_HV_START_SECONDARY,
    P_HV_SWITCH_CPU,
    P_HV_SET_TIME_STEALING,
    P_HV_PIN_CPU,
    P_HV_WRITE_HCR,
    P_HV_MAP_VIRTIO,
    P_VIRTIO_PUT_BUFFER,
    P_HV_EXIT_CPU,
    P_HV_ADD_TIME,
    P_HV_PSCI_SUSPEND_CPU,
    P_HV_PSCI_TURN_OFF_CPU,
    P_HV_PSCI_TURN_ON_CPU,
    P_HV_PSCI_TURN_OFF_SYSTEM,
    P_HV_PSCI_RESET_SYSTEM,
    P_HV_PSCI_FEATURES,
    P_HV_PSCI_MEM_PROTECT,
    P_HV_PSCI_MEM_PROTECT_CHECK_RANGE,

    P_FB_INIT = 0xd00,
    P_FB_SHUTDOWN,
    P_FB_BLIT,
    P_FB_UNBLIT,
    P_FB_FILL,
    P_FB_CLEAR,
    P_FB_DISPLAY_LOGO,
    P_FB_RESTORE_LOGO,
    P_FB_IMPROVE_LOGO,

    P_PCIE_INIT = 0xe00,
    P_PCIE_SHUTDOWN,
    P_WIRELESS_HANDOFF_INIT,
    P_PCIE_WIRELESS_INIT,

    P_NVME_INIT = 0xf00,
    P_NVME_SHUTDOWN,
    P_NVME_READ,
    P_NVME_FLUSH,

    P_MCC_GET_CARVEOUTS = 0x1000,

    P_DISPLAY_INIT = 0x1100,
    P_DISPLAY_CONFIGURE,
    P_DISPLAY_SHUTDOWN,
    P_DISPLAY_START_DCP,
    P_DISPLAY_IS_EXTERNAL,

    P_DAPF_INIT_ALL = 0x1200,
    P_DAPF_INIT,

    P_HV_MAP_TPM = 0x1400,

    P_CPUFREQ_INIT = 0x1300,

    // Apple Type-C PHY (T6020); keep in sync with proxyclient/m1n1/atcphy.py
    P_ATCPHY_APPLY_MODE = 0x1500,
    P_ATCPHY_SET_ORIENTATION,
    P_ATCPHY_POWER_OFF,
    P_ATCPHY_GET_REG_BASE,
    P_ATCPHY_ARM_GUEST_MODE,
    /*
     * P_ATCPHY_READ_ORIENTATION(port) -> packed u64. Read-only: it issues a
     * single SMBus read of the port's CD3217 STATUS register and never
     * writes to the PD controller. ABI (keep in sync with
     * proxyclient/m1n1/atcphy.py and auroradbg's USB handoff client):
     *
     *   0xFFFFFFFFFFFFFFFF  read failed; the caller must NOT infer an
     *                       orientation from this
     *   otherwise           bit 32 set (marks a valid reply)
     *                       bit 33 = plug present
     *                       bit 34 = plug upside down (flipped)
     *                       bits 31:0 = raw STATUS dword, for the log
     *
     * The all-ones failure code is chosen so that it can never collide with
     * a valid reply (bit 32 set implies bits 63:35 clear).
     */
    P_ATCPHY_READ_ORIENTATION,
    /*
     * P_ATCPHY_READ_LINK_STATE(port, selector) -> packed u64. One bounded
     * read-only STATUS + DATA_STATUS sample and, for USB4, USB4_STATUS; no
     * HPM command or write is issued.
     *
     *   0xFFFFFFFFFFFFFFFF  either register read failed
     *   selector 0          bit 40 set (valid)
     *                       bits 39:32 = STATUS byte 0
     *                       bits 31:0 = raw DATA_STATUS dword
     *   selector 1          bit 63 set (valid)
     *                       bits 55:40 = Apple router cable-info word
     *                       bits 39:32 = USB4 mode/status byte
     *                       bits 31:0 = raw USB4 EUDO
     *
     * STATUS byte 0 contains every role/orientation field used by the boot
     * gate. DATA_STATUS distinguishes direct USB3 from USB4/TBT/DP. The
     * selectors deliberately share one operation so clients cannot drift
     * to a second register-number ABI.
     */
    P_ATCPHY_READ_LINK_STATE,
    /* External Type5 firmware bundle owner. START validates/copies the
     * bundle and reaches FW_READY only; it never commits the routed PIPE.
     * args: port, bundle address, bundle size, flipped, thunderbolt-mode,
     * diagnostic stop (0=full, 1=PHY, 2=state5, 3=state7, 4=copy,
     * 5=release). */
    P_ACIO_TYPE5_FW_START,
    P_ACIO_TYPE5_ABORT,
    P_ACIO_TYPE5_PHASE,
    /* Bounded USB4 control-ring access. READ returns bit 32 set plus the
     * complete 32-bit value, or all-ones on failure. WRITE returns 0/-1.
     * args: port, route, adapter, config-space, dword-offset[, value]. */
    P_ACIO_TYPE5_CONFIG_READ32,
    P_ACIO_TYPE5_CONFIG_WRITE32,
    /* Execute the root-router configuration handshake against the live
     * control ring. Requires CONTROL_READY. Creates no tunnel and commits
     * no PIPE mux. args: port, route, all-parents-support-usb.
     * Returns 0 or -1; read P_ACIO_TYPE5_STATUS for the failing phase. */
    P_ACIO_TYPE5_ROUTER_CONFIGURE,
    /* Bounded telemetry snapshot; never touches hardware. args: port,
     * selector.
     *   selector 0  bits 63:32 = phase, bits 31:0 = last error code
     *   selector 1  bits 63:32 = last error detail word,
     *               bits 31:0  = total error count
     *   selector 2  bits 63:32 = config requests, bits 31:0 = failures
     *   selector 3  bits 31:0  = router state machine index
     *   selector 4  bits 31:0  = monotonic run id; latch before a run and
     *                            require a strictly greater value after, so
     *                            a stale result cannot be read as this
     *                            run's
     * Returns all-ones for an invalid port or selector. */
    P_ACIO_TYPE5_STATUS,
    /* Bring the bidirectional USB3 tunnel up or down. Requires ROUTER_READY.
     * Reaches TUNNEL_READY at most -- it does NOT commit the PIPE mux, and
     * there is deliberately no selector that does.
     * args: port, up(1)/down(0), down_route, down_adapter, up_route,
     *       up_adapter. */
    P_ACIO_TYPE5_USB3_TUNNEL,
    /* Scan the device router on a host downstream adapter and locate its
     * USB3 Up adapter. Requires ROUTER_READY and a real link partner.
     * args: port, host down-adapter.
     * reply: all-ones on failure, else bit 63 set | route<<16 |
     *        device-upstream-port<<8 | usb3-up-adapter.
     * The upstream (lane) port is included because a real tunnel needs it: the
     * device-side hops run BETWEEN that adapter and the USB3 Up adapter, so a
     * reply carrying only the Up adapter describes half a path. */
    P_ACIO_TYPE5_SCAN_DEVICE,
    /* Commit the routed USB4 PIPE mux (pipehandler -> 0x11) and transition
     * TUNNEL_READY -> PIPE_COMMITTED. This is the ONLY selector in the whole
     * Type5 surface that moves the mux off DUMMY. It requires TUNNEL_READY and
     * is VERIFIED BY READBACK: it returns 0 only when MUX_CTRL reads back
     * exactly 0x11, else -1 (read P_ACIO_TYPE5_STATUS to tell pipe-commit --
     * the sequence itself failed -- from pipe-readback -- the mux did not
     * settle on 0x11). Committing 0x11 points the DWC3 SuperSpeed PIPE at the
     * ACIO router; it must NOT be issued while a guest xHCI is already bound to
     * this port. args: port. Returns 0 or -1. */
    P_ACIO_TYPE5_PIPE_COMMIT,
    /* Bring up a REAL end-to-end USB3 tunnel: scan through the host lane
     * adapter for the device router, program four hop descriptors (two per
     * router) and enable both protocol adapters at their DISCOVERED
     * capability-0x04 offsets. Requires ROUTER_READY; reaches TUNNEL_READY.
     * Replaces driving P_ACIO_TYPE5_USB3_TUNNEL with both endpoints on
     * route 0, which programmed a host-internal loopback.
     * args: port, host lane adapter, host USB3-Down adapter.
     * Returns 0 or -1; read P_ACIO_TYPE5_STATUS for the failing condition. */
    P_ACIO_TYPE5_USB3_TUNNEL_DEVICE,

    // J414s media profile; keep in sync with proxyclient/m1n1/media_handoff.py
    P_MEDIA_HANDOFF_INIT = 0x1600,

    /*
     * AGX preboot initdata handoff; keep in sync with
     * proxyclient/m1n1/gpu_handoff.py and auroradbg's GPU handoff client.
     *
     * P_GPU_INITDATA_FILL(reservation_base, reservation_size) -> 0 or a
     * negative gpu_handoff_error. It generates HwDataA/HwDataB/Globals into
     * the reservation and stamps each aperture with the identity of the
     * generator arm that produced it; the caller reads the addresses and sizes
     * actually produced out of those stamps (see gpu_handoff_abi.h), which is
     * also exactly what the Windows driver authenticates.
     *
     * Read-only with respect to the GPU: it samples the ADT power tables and
     * two ID registers, writes only DRAM, and never powers the GPU down.
     */
    P_GPU_INITDATA_FILL = 0x1800,

    // Bulk host<->guest channel; keep in sync with proxyclient/m1n1/hv/xfer.py
    P_HV_MAP_XFER = 0x1700,
} ProxyOp;

#define S_OK     0
#define S_BADCMD -1

typedef struct {
    u64 opcode;
    u64 args[6];
} ProxyRequest;

typedef struct {
    u64 opcode;
    s64 status;
    u64 retval;
} ProxyReply;

int proxy_process(ProxyRequest *request, ProxyReply *reply);

#endif
