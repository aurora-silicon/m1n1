/* SPDX-License-Identifier: MIT */

#ifndef MIDR_H
#define MIDR_H

/* Part IDs in MIDR_EL1 */
#define MIDR_PART_S5L8960X_CYCLONE       0x1
#define MIDR_PART_T7000_TYPHOON          0x2
#define MIDR_PART_T7001_TYPHOON          0x3
#define MIDR_PART_S8000_TWISTER          0x4
#define MIDR_PART_S8001_3_TWISTER        0x5
#define MIDR_PART_T8010_2_HURRICANE      0x6
#define MIDR_PART_T8011_HURRICANE        0x7
#define MIDR_PART_T8015_MONSOON          0x8
#define MIDR_PART_T8015_MISTRAL          0x9
#define MIDR_PART_T8020_VORTEX           0xb
#define MIDR_PART_T8020_TEMPSET          0xc
#define MIDR_PART_T8006_TEMPSET          0xf
#define MIDR_PART_T8027_VORTEX           0x10
#define MIDR_PART_T8027_TEMPSET          0x11
#define MIDR_PART_T8030_LIGHTNING        0x12
#define MIDR_PART_T8030_THUNDER          0x13
#define MIDR_PART_T8101_ICESTORM         0x20
#define MIDR_PART_T8101_FIRESTORM        0x21
#define MIDR_PART_T8103_ICESTORM         0x22
#define MIDR_PART_T8103_FIRESTORM        0x23
#define MIDR_PART_T6000_ICESTORM         0x24
#define MIDR_PART_T6000_FIRESTORM        0x25
#define MIDR_PART_T8301_THUNDER          0x26
#define MIDR_PART_T6001_ICESTORM         0x28
#define MIDR_PART_T6001_FIRESTORM        0x29
#define MIDR_PART_T8110_BLIZZARD         0x30
#define MIDR_PART_T8110_AVALANCHE        0x31
#define MIDR_PART_T8112_BLIZZARD         0x32
#define MIDR_PART_T8112_AVALANCHE        0x33
#define MIDR_PART_T6020_BLIZZARD         0x34
#define MIDR_PART_T6020_AVALANCHE        0x35
#define MIDR_PART_T6021_BLIZZARD         0x38
#define MIDR_PART_T6021_AVALANCHE        0x39
#define MIDR_PART_T8122_SAWTOOTH         0x42
#define MIDR_PART_T8122_EVEREST          0x43
#define MIDR_PART_T6030_SAWTOOTH         0x44
#define MIDR_PART_T6030_EVEREST          0x45
#define MIDR_PART_T6031_SAWTOOTH         0x48
#define MIDR_PART_T6031_EVEREST          0x49
#define MIDR_PART_T8132_DONAN_ECORE      0x52
#define MIDR_PART_T8132_DONAN_PCORE      0x53
#define MIDR_PART_T6040_BRAVA_CHOP_ECORE 0x54
#define MIDR_PART_T6040_BRAVA_CHOP_PCORE 0x55
#define MIDR_PART_T6041_BRAVA_ECORE      0x58
#define MIDR_PART_T6041_BRAVA_PCORE      0x59
#define MIDR_PART_T8140_TAHITI_ECORE     0x60
#define MIDR_PART_T8140_TAHITI_PCORE     0x61
#define MIDR_PART_T8142_HIDRA_ECORE      0x62
#define MIDR_PART_T8142_HIDRA_PCORE      0x63
#define MIDR_PART_T6050_SOTRA_MCORE      0x64
#define MIDR_PART_T6050_SOTRA_PCORE      0x65
#define MIDR_PART_T6051_SOTRAC_MCORE     0x68
#define MIDR_PART_T6051_SOTRAC_PCORE     0x69

//
// T8142 (Apple M5).
//
// The P-core value is MEASURED on J704 hardware:
//     MIDR_EL1 = 0x612f0630  -> implementer 0x61 (Apple), part 0x063,
//                               variant 0x2, revision 0x0
// read on the boot CPU, whose MPIDR_EL1 was 0x80010100 (Aff2=1, Aff1=1, Aff0=0),
// i.e. cluster 1 core 0 -- a P-core.
//
// The E-core value is INFERRED, not measured. Every generation in the list above
// pairs an even E-core part with the P-core immediately after it (0x22/0x23,
// 0x32/0x33, 0x44/0x45, 0x52/0x53), so 0x62 follows. It has not been read off an
// E-core, because that needs either SMP or a boot CPU in cluster 0. Confirm it
// before relying on it for anything that differs per core type.
//
// Naming: Apple's ADT calls these "everest"/"sawtooth" on both M4 and M5, but
// those are recycled M3 core names and Asahi independently calls the M4 cores
// "donan". Rather than invent a codename for M5, these are just ECORE/PCORE.
//
#define MIDR_PART_T8142_ECORE 0x62 /* inferred from the pattern, unverified */
#define MIDR_PART_T8142_PCORE 0x63 /* measured on J704 */

#define MIDR_REV_LOW  GENMASK(3, 0)
#define MIDR_PART     GENMASK(15, 4)
#define MIDR_CORE_TYPE_P BIT(16)
#define MIDR_CORE_TYPE_M BIT(18)
#define MIDR_REV_HIGH GENMASK(23, 20)

#endif
