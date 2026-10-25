/* SPDX-License-Identifier: MIT */
/* Host stand-in for src/exception.h: the guard flags the fake MMIO honours. */
#ifndef EXCEPTION_H
#define EXCEPTION_H
enum exc_guard_t {
    GUARD_OFF = 0,
    GUARD_SKIP,
    GUARD_MARK,
    GUARD_RETURN,
    GUARD_TYPE_MASK = 0xff,
    GUARD_SILENT = 0x100,
};
extern volatile enum exc_guard_t exc_guard;
extern volatile int exc_count;
#endif
