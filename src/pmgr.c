/* SPDX-License-Identifier: MIT */

#include "pmgr.h"
#include "adt.h"
#include "string.h"
#include "types.h"
#include "utils.h"

#define PMGR_RESET        BIT(31)
#define PMGR_AUTO_ENABLE  BIT(28)
#define PMGR_PS_AUTO      GENMASK(27, 24)
#define PMGR_PARENT_OFF   BIT(11)
#define PMGR_DEV_DISABLE  BIT(10)
#define PMGR_WAS_CLKGATED BIT(9)
#define PMGR_WAS_PWRGATED BIT(8)
#define PMGR_PS_ACTUAL    GENMASK(7, 4)
#define PMGR_PS_TARGET    GENMASK(3, 0)

/* 192000 us.  NOTE: this was previously justified as "Apple's TARGET/ACTUAL
 * wait = 0x2ee00 us".  That justification is wrong twice over and is
 * corrected here rather than propagated:
 *
 *  - Apple's 0x2ee00 is a raw mach-tick delta, not microseconds.
 *    ApplePMGR::waitReg32 does `deadline = mach_absolute_time() + timeout`
 *    with no unit conversion, so at the 24 MHz timebase 0x2ee00 ticks is
 *    ~8 ms, not 192 ms.
 *  - 0x2ee00 is ApplePMGR's house-standard constant seen in
 *    enableCioReconfig's pre-wait (and configISPRefClock / enableTVM), not
 *    in the PS TARGET/ACTUAL convergence poll, which uses its own
 *    mach_absolute_time loop.
 *
 * The numeric value is deliberately kept: 192 ms is ~24x Apple's bound, so
 * it is strictly more permissive and cannot cause a spurious timeout.  It
 * is a safety margin, not a decoded Apple constant. */
#define PMGR_POLL_TIMEOUT 192000

#define PMGR_FLAG_VIRTUAL 0x10

struct pmgr_device {
    u8 flags;
    u16 unk1;
    u8 id1;
    union {
        struct {
            u8 parent[2];
            u8 unk2[2];
        } u8id;
        struct {
            u16 parent[2];
        } u16id;
    };
    u8 unk3[2];
    u8 addr_offset;
    u8 psreg_idx;
    u8 unk4[4];
    struct {
        u32 offset : 24;
        u32 group : 8;
    } group_and_offset;
    u8 unk5[6];
    u16 id2;
    u8 unk6[4];
    const char name[0x10];
} PACKED;

static int pmgr_initialized = 0;

static int pmgr_path[8];
static int pmgr_offset;
static int pmgr_dies;

static const u32 *pmgr_ps_regs = NULL;
static u32 pmgr_ps_regs_len = 0;

//
// T8142 (Apple M5) replaced `ps-regs` with `ps-groups`.
//
// Up to and including T8132 (M4), a device's power-state register was found with
// a two-level lookup:
//
//     addr = pmgr_reg[ps_regs[psreg_idx].reg_idx]
//          + ps_regs[psreg_idx].offset
//          + (addr_offset << 3)
//
// T8142 flattens that. `ps-regs` is gone; `ps-groups` is a much smaller table
// (3 entries on J704) whose first word is just a reg index, and the device record
// carries a single u32 at offset 0x10 holding both the group and the complete
// byte offset:
//
//     v    = u32 at device record + 0x10
//     addr = pmgr_reg[ps_groups[v >> 24].reg_idx] + (v & 0xffffff)
//
// The old addr_offset/psreg_idx bytes (record +0x0a/+0x0b) are zero on T8142.
//
// Derived by diffing the J704 and J713 ADTs. Corroborated three ways: the top
// byte takes exactly three distinct values across all 386 T8142 devices, matching
// the three ps-groups entries; the resulting offsets stay inside their respective
// pmgr reg windows; and the addresses land next to M4's for the same devices
// (ATC0_USB is reg[1]+0xc0 on M4 and reg[1]+0xd0 on M5).
//
#define PMGR_DEV_PS_GROUP_OFFSET 0x10
#define PMGR_PS_GROUP_INDEX(v)   ((v) >> 24)
#define PMGR_PS_GROUP_OFFSET(v)  ((v) & 0xffffff)

static const u32 *pmgr_ps_groups = NULL;
static u32 pmgr_ps_groups_len = 0;
static bool pmgr_use_ps_groups = false;

static const struct pmgr_device *pmgr_devices = NULL;
static u32 pmgr_devices_len = 0;

static bool pmgr_u8id = false;
static bool pmgr_use_group_and_offset = false;

static uintptr_t pmgr_get_psreg(u8 idx)
{
    if (idx * 12 >= pmgr_ps_regs_len) {
        printf("pmgr: Index %d is out of bounds for ps-regs\n", idx);
        return 0;
    }

    u32 reg_idx = pmgr_ps_regs[3 * idx];
    u32 reg_offset = pmgr_ps_regs[3 * idx + 1];

    u64 pmgr_reg;
    if (adt_get_reg(adt, pmgr_path, "reg", reg_idx, &pmgr_reg, NULL) < 0) {
        printf("pmgr: Error getting /arm-io/pmgr regs\n");
        return 0;
    }

    return pmgr_reg + reg_offset;
}

int pmgr_set_mode(uintptr_t addr, u8 target_mode)
{
    mask32(addr, PMGR_AUTO_ENABLE | PMGR_WAS_CLKGATED | PMGR_WAS_PWRGATED | PMGR_PS_TARGET,
           FIELD_PREP(PMGR_PS_TARGET, target_mode));
    if (poll32(addr, PMGR_PS_ACTUAL, FIELD_PREP(PMGR_PS_ACTUAL, target_mode), PMGR_POLL_TIMEOUT) <
        0) {
        printf("pmgr: timeout while trying to set mode %x for device at 0x%lx: %x\n", target_mode,
               addr, read32(addr));
        return -1;
    }

    return 0;
}

static u16 pmgr_adt_get_id(const struct pmgr_device *device)
{
    if (pmgr_u8id)
        return device->id1;
    else
        return device->id2;
}

static int pmgr_find_device(u16 id, const struct pmgr_device **device)
{
    for (size_t i = 0; i < pmgr_devices_len; ++i) {
        const struct pmgr_device *i_device = &pmgr_devices[i];
        if (pmgr_adt_get_id(i_device) != id)
            continue;

        *device = i_device;
        return 0;
    }

    return -1;
}

//
// T8142 path: resolve a device's power-state register from ps-groups.
// See the commentary next to pmgr_ps_groups above for the format.
//
static uintptr_t pmgr_get_psgroup_addr(const struct pmgr_device *device)
{
    u32 v;
    memcpy(&v, (const u8 *)device + PMGR_DEV_PS_GROUP_OFFSET, sizeof(v));

    u32 group = PMGR_PS_GROUP_INDEX(v);
    u32 offset = PMGR_PS_GROUP_OFFSET(v);

    if (group * 3 >= pmgr_ps_groups_len / sizeof(u32)) {
        printf("pmgr: ps-group %u out of bounds (%u entries)\n", group,
               pmgr_ps_groups_len / (3 * (u32)sizeof(u32)));
        return 0;
    }

    u32 reg_idx = pmgr_ps_groups[3 * group];

    u64 pmgr_reg;
    if (adt_get_reg(adt, pmgr_path, "reg", reg_idx, &pmgr_reg, NULL) < 0) {
        printf("pmgr: Error getting /arm-io/pmgr reg %u for ps-group %u\n", reg_idx, group);
        return 0;
    }

    return pmgr_reg + offset;
}

static uintptr_t pmgr_device_get_addr(u8 die, const struct pmgr_device *device)
{
    uintptr_t addr;

    if (pmgr_use_ps_groups) {
        //
        // The complete byte offset is already folded into the ps-group word, so
        // unlike the ps-regs path there is no addr_offset to add afterwards.
        //
        addr = pmgr_get_psgroup_addr(device);
        if (addr == 0)
            return 0;
        return addr + PMGR_DIE_OFFSET * die;
    }

    addr = pmgr_get_psreg(device->psreg_idx);
    if (addr == 0)
        return 0;

    addr += PMGR_DIE_OFFSET * die;

    if (pmgr_use_group_and_offset)
        addr += device->group_and_offset.offset;
    else
        addr += (device->addr_offset << 3);

    return addr;
}

static void pmgr_adt_get_parents(const struct pmgr_device *device, u16 parent[2])
{
    if (pmgr_u8id) {
        parent[0] = device->u8id.parent[0];
        parent[1] = device->u8id.parent[1];
    } else {
        parent[0] = device->u16id.parent[0];
        parent[1] = device->u16id.parent[1];
    }
}

static int pmgr_set_mode_recursive(u8 die, u16 id, u8 target_mode, bool recurse)
{
    if (!pmgr_initialized) {
        printf("pmgr: pmgr_set_mode_recursive() called before successful pmgr_init()\n");
        return -1;
    }

    if (id == 0)
        return -1;

    const struct pmgr_device *device;

    if (pmgr_find_device(id, &device))
        return -1;

    if (target_mode == 0 && !(device->flags & PMGR_FLAG_VIRTUAL)) {
        uintptr_t addr = pmgr_device_get_addr(die, device);
        if (!addr)
            return -1;
        if (pmgr_set_mode(addr, target_mode))
            return -1;
    }

    if (recurse)
        for (int i = 0; i < 2; i++) {
            u16 parents[2];
            pmgr_adt_get_parents(device, parents);
            if (parents[i]) {
                int ret = pmgr_set_mode_recursive(die, parents[i], target_mode, true);
                if (ret < 0)
                    return ret;
            }
        }

    if (target_mode != 0 && !(device->flags & PMGR_FLAG_VIRTUAL)) {
        uintptr_t addr = pmgr_device_get_addr(die, device);
        if (!addr)
            return -1;
        if (pmgr_set_mode(addr, target_mode))
            return -1;
    }

    return 0;
}

int pmgr_power_enable(u32 id)
{
    u16 device = FIELD_GET(PMGR_DEVICE_ID, id);
    u8 die = FIELD_GET(PMGR_DIE_ID, id);
    return pmgr_set_mode_recursive(die, device, PMGR_PS_ACTIVE, true);
}

int pmgr_power_disable(u32 id)
{
    u16 device = FIELD_GET(PMGR_DEVICE_ID, id);
    u8 die = FIELD_GET(PMGR_DIE_ID, id);
    return pmgr_set_mode_recursive(die, device, PMGR_PS_PWRGATE, false);
}

static int pmgr_adt_find_devices(const char *path, const u32 **devices, u32 *n_devices)
{
    int node_offset = adt_path_offset(adt, path);
    if (node_offset < 0) {
        printf("pmgr: Error getting node %s\n", path);
        return -1;
    }

    *devices = adt_getprop(adt, node_offset, "clock-gates", n_devices);
    if (*devices == NULL || *n_devices == 0) {
        printf("pmgr: Error getting %s clock-gates.\n", path);
        return -1;
    }

    *n_devices /= 4;

    return 0;
}

static int pmgr_adt_devices_set_mode(const char *path, u8 target_mode, int recurse)
{
    const u32 *devices;
    u32 n_devices;
    int ret = 0;

    if (pmgr_adt_find_devices(path, &devices, &n_devices) < 0)
        return -1;

    for (u32 i = 0; i < n_devices; ++i) {
        u16 device = FIELD_GET(PMGR_DEVICE_ID, devices[i]);
        u8 die = FIELD_GET(PMGR_DIE_ID, devices[i]);
        if (pmgr_set_mode_recursive(die, device, target_mode, recurse))
            ret = -1;
    }

    return ret;
}

static int pmgr_adt_device_set_mode(const char *path, u32 index, u8 target_mode, int recurse)
{
    const u32 *devices;
    u32 n_devices;
    int ret = 0;

    if (pmgr_adt_find_devices(path, &devices, &n_devices) < 0)
        return -1;

    if (index >= n_devices)
        return -1;

    u16 device = FIELD_GET(PMGR_DEVICE_ID, devices[index]);
    u8 die = FIELD_GET(PMGR_DIE_ID, devices[index]);
    if (pmgr_set_mode_recursive(die, device, target_mode, recurse))
        ret = -1;

    return ret;
}

int pmgr_adt_power_enable(const char *path)
{
    int ret = pmgr_adt_devices_set_mode(path, PMGR_PS_ACTIVE, true);
    return ret;
}

int pmgr_adt_power_disable(const char *path)
{
    return pmgr_adt_devices_set_mode(path, PMGR_PS_PWRGATE, false);
}

int pmgr_adt_power_enable_index(const char *path, u32 index)
{
    int ret = pmgr_adt_device_set_mode(path, index, PMGR_PS_ACTIVE, true);
    return ret;
}

int pmgr_adt_power_disable_index(const char *path, u32 index)
{
    return pmgr_adt_device_set_mode(path, index, PMGR_PS_PWRGATE, false);
}

static int pmgr_reset_device(int die, const struct pmgr_device *dev)
{
    if (die < 0 || die > 16) {
        printf("pmgr: invalid die id %d for device %s\n", die, dev->name);
        return -1;
    }

    uintptr_t addr = pmgr_device_get_addr(die, dev);

    u32 reg = read32(addr);
    if (FIELD_GET(PMGR_PS_ACTUAL, reg) != PMGR_PS_ACTIVE) {
        printf("pmgr: will not reset disabled device %d.%s\n", die, dev->name);
        return -1;
    }

    printf("pmgr: resetting device %d.%s\n", die, dev->name);

    set32(addr, PMGR_DEV_DISABLE);
    set32(addr, PMGR_RESET);
    udelay(10);
    clear32(addr, PMGR_RESET);
    clear32(addr, PMGR_DEV_DISABLE);

    return 0;
}

int pmgr_adt_reset(const char *path)
{
    const u32 *devices;
    u32 n_devices;
    int ret = 0;

    if (pmgr_adt_find_devices(path, &devices, &n_devices) < 0)
        return -1;

    for (u32 i = 0; i < n_devices; ++i) {
        const struct pmgr_device *device;
        u16 id = FIELD_GET(PMGR_DEVICE_ID, devices[i]);
        u8 die = FIELD_GET(PMGR_DIE_ID, devices[i]);

        if (pmgr_find_device(id, &device)) {
            ret = -1;
            continue;
        }

        if (pmgr_reset_device(die, device))
            ret = -1;
    }

    return ret;
}

int pmgr_device_name_by_id(u16 id, char *out, size_t len)
{
    const struct pmgr_device *device;

    if (!pmgr_initialized || !out || !len || pmgr_find_device(id, &device) < 0)
        return -1;

    size_t i = 0;
    while (i < len - 1 && i < sizeof(device->name) && device->name[i])
        out[i] = device->name[i], i++;
    out[i] = 0;
    return 0;
}

int pmgr_reset(int die, const char *name)
{
    const struct pmgr_device *dev = NULL;

    for (unsigned int i = 0; i < pmgr_devices_len; ++i) {
        if (strncmp(pmgr_devices[i].name, name, 0x10) == 0) {
            dev = &pmgr_devices[i];
            break;
        }
    }

    if (!dev)
        return -1;

    return pmgr_reset_device(die, dev);
}

int pmgr_power_enable_name(int die, const char *name)
{
    const struct pmgr_device *dev = NULL;

    for (unsigned int i = 0; i < pmgr_devices_len; ++i) {
        if (strncmp(pmgr_devices[i].name, name, 0x10) == 0) {
            dev = &pmgr_devices[i];
            break;
        }
    }

    if (!dev)
        return -1;

    return pmgr_set_mode_recursive(die, pmgr_adt_get_id(dev), PMGR_PS_ACTIVE, true);
}

int pmgr_power_on(int die, const char *name)
{
    const struct pmgr_device *dev = NULL;

    for (unsigned int i = 0; i < pmgr_devices_len; ++i) {
        if (strncmp(pmgr_devices[i].name, name, 0x10) == 0) {
            dev = &pmgr_devices[i];
            break;
        }
    }

    if (!dev)
        return -1;

    uintptr_t addr = pmgr_device_get_addr(die, dev);

    if (!addr)
        return -1;

    return pmgr_set_mode(addr, PMGR_PS_ACTIVE);
}

int pmgr_init(void)
{
    int node = adt_path_offset(adt, "/arm-io");
    if (node < 0) {
        printf("pmgr: Error getting /arm-io node\n");
        return -1;
    }
    if (ADT_GETPROP(adt, node, "die-count", &pmgr_dies) < 0)
        pmgr_dies = 1;

    pmgr_offset = adt_path_offset_trace(adt, "/arm-io/pmgr", pmgr_path);
    if (pmgr_offset < 0) {
        printf("pmgr: Error getting /arm-io/pmgr node\n");
        return -1;
    }

    pmgr_ps_regs = adt_getprop(adt, pmgr_offset, "ps-regs", &pmgr_ps_regs_len);
    if (pmgr_ps_regs == NULL || pmgr_ps_regs_len == 0) {
        //
        // T8142 (M5) and later drop ps-regs in favour of ps-groups. Fall back
        // rather than failing: bailing here leaves every power domain unmanaged,
        // which shows up much later as an unpowered device faulting on its first
        // MMIO access (on T8142 that was the USB DART).
        //
        pmgr_ps_groups = adt_getprop(adt, pmgr_offset, "ps-groups", &pmgr_ps_groups_len);
        if (pmgr_ps_groups == NULL || pmgr_ps_groups_len == 0) {
            printf("pmgr: Error getting /arm-io/pmgr ps-regs or ps-groups\n");
            return -1;
        }
        pmgr_use_ps_groups = true;
        printf("pmgr: using ps-groups (%u entries)\n",
               pmgr_ps_groups_len / (3 * (u32)sizeof(u32)));
    }

    pmgr_devices = adt_getprop(adt, pmgr_offset, "devices", &pmgr_devices_len);
    if (pmgr_devices == NULL || pmgr_devices_len == 0) {
        printf("pmgr: Error getting /arm-io/pmgr devices.\n");
        return -1;
    }

    pmgr_devices_len /= sizeof(*pmgr_devices);
    pmgr_initialized = 1;

    printf("pmgr: Cleaning up device states...\n");

    // detect whether u8 or u16 PMGR IDs are used by comparing the IDs of the
    // first 2 devices
    if (pmgr_devices_len >= 2)
        pmgr_u8id = pmgr_devices[0].id1 != pmgr_devices[1].id1;

    for (u8 die = 0; die < pmgr_dies; ++die) {
        for (size_t i = 0; i < pmgr_devices_len; ++i) {
            const struct pmgr_device *device = &pmgr_devices[i];

            if ((device->flags & PMGR_FLAG_VIRTUAL))
                continue;

            uintptr_t addr = pmgr_device_get_addr(die, device);
            if (!addr)
                continue;

            u32 reg = read32(addr);

            if (reg & PMGR_AUTO_ENABLE || FIELD_GET(PMGR_PS_TARGET, reg) == PMGR_PS_ACTIVE) {
                for (int j = 0; j < 2; j++) {
                    u16 parent[2];
                    pmgr_adt_get_parents(device, parent);
                    if (parent[j]) {
                        const struct pmgr_device *pdevice;
                        if (pmgr_find_device(parent[j], &pdevice)) {
                            printf("pmgr: Failed to find parent #%d for %s\n", parent[j],
                                   device->name);
                            continue;
                        }

                        if ((pdevice->flags & PMGR_FLAG_VIRTUAL))
                            continue;

                        addr = pmgr_device_get_addr(die, pdevice);
                        if (!addr)
                            continue;

                        reg = read32(addr);

                        if (!(reg & PMGR_AUTO_ENABLE) &&
                            FIELD_GET(PMGR_PS_TARGET, reg) != PMGR_PS_ACTIVE) {
                            printf("pmgr: Enabling %d.%s, parent of active device %s\n", die,
                                   pdevice->name, device->name);
                            pmgr_set_mode(addr, PMGR_PS_ACTIVE);
                        }
                    }
                }
            }
        }
    }

    printf("pmgr: initialized, %d devices on %u dies found.\n", pmgr_devices_len, pmgr_dies);

    return 0;
}

u32 pmgr_get_feature(const char *name)
{
    u32 val = 0;

    int node = adt_path_offset(adt, "/arm-io/pmgr");
    if (node < 0)
        return 0;

    if (ADT_GETPROP(adt, node, name, &val) < 0)
        return 0;

    return val;
}
