/* SPDX-License-Identifier: MIT */

#include <stdarg.h>

#include "adt.h"
#include "iodev.h"
#include "types.h"
#include "utils.h"
#include "vsprintf.h"

#define DATA_TX8      0x4004
#define DATA_TX_FREE  0x4014
#define DATA_RX8      0x401c
#define DATA_RX_COUNT 0x402c

static u64 uart_base = 0;

#ifndef DOCKCHANNEL_UART_CHANNEL
#define DOCKCHANNEL_UART_CHANNEL 1
#endif

int dockchannel_uart_init(void)
{
    int path[8];

    if (adt_path_offset_trace(adt, "/arm-io/dockchannel-uart", path) < 0)
        return -1;

    if (adt_get_reg(adt, path, "reg", 0, &uart_base, NULL)) {
        printf("!!! Failed to get dockchannel UART reg property!\n");
        return -1;
    }

    int channel = DOCKCHANNEL_UART_CHANNEL;
    int chosen = adt_path_offset(adt, "/chosen");
    u32 chip;
    if (chosen >= 0 && ADT_GETPROP(adt, chosen, "chip-id", &chip) >= 0 && chip == T8140)
        channel = 0;
    uart_base += channel * 0x10000;

    printf("Initialized dockchannel UART channel %d at 0x%lx\n", channel, uart_base);

    return 0;
}

void dockchannel_uart_putbyte(u8 c)
{
    if (!uart_base)
        return;

    while (read32(uart_base + DATA_TX_FREE) == 0)
        ;

    write32(uart_base + DATA_TX8, c);
}

u8 dockchannel_uart_getbyte(void)
{
    if (!uart_base)
        return 0;

    while (read32(uart_base + DATA_RX_COUNT) == 0)
        ;

    return read32(uart_base + DATA_RX8) >> 8;
}

void dockchannel_uart_putchar(u8 c)
{
    if (c == '\n')
        dockchannel_uart_putbyte('\r');

    dockchannel_uart_putbyte(c);
}

u8 dockchannel_uart_getchar(void)
{
    return dockchannel_uart_getbyte();
}

void dockchannel_uart_puts(const char *s)
{
    while (*s)
        dockchannel_uart_putchar(*(s++));

    dockchannel_uart_putchar('\n');
}

void dockchannel_uart_write(const void *buf, size_t count)
{
    const u8 *p = buf;

    while (count--)
        dockchannel_uart_putbyte(*p++);
}

size_t dockchannel_uart_read(void *buf, size_t count)
{
    u8 *p = buf;
    size_t recvd = 0;

    while (count--) {
        *p++ = dockchannel_uart_getbyte();
        recvd++;
    }

    return recvd;
}

int dockchannel_uart_printf(const char *fmt, ...)
{
    va_list args;
    char buffer[512];
    int i;

    va_start(args, fmt);
    i = vsnprintf(buffer, sizeof(buffer), fmt, args);
    va_end(args);

    dockchannel_uart_write(buffer, min(i, (int)(sizeof(buffer) - 1)));

    return i;
}

static bool dockchannel_uart_iodev_can_write(void *opaque)
{
    UNUSED(opaque);

    if (!uart_base)
        return false;

    return read32(uart_base + DATA_TX_FREE) > 0;
}

static ssize_t dockchannel_uart_iodev_can_read(void *opaque)
{
    UNUSED(opaque);

    if (!uart_base)
        return 0;

    return read32(uart_base + DATA_RX_COUNT);
}

static ssize_t dockchannel_uart_iodev_read(void *opaque, void *buf, size_t len)
{
    UNUSED(opaque);
    return dockchannel_uart_read(buf, len);
}

static ssize_t dockchannel_uart_iodev_write(void *opaque, const void *buf, size_t len)
{
    UNUSED(opaque);
    dockchannel_uart_write(buf, len);
    return len;
}

static ssize_t dockchannel_uart_iodev_write_nonblocking(void *opaque, const void *buf, size_t len)
{
    UNUSED(opaque);

    if (!uart_base)
        return 0;

    size_t count = min(len, (size_t)read32(uart_base + DATA_TX_FREE));
    const u8 *p = buf;
    for (size_t i = 0; i < count; i++)
        write32(uart_base + DATA_TX8, p[i]);

    return count;
}

static ssize_t dockchannel_uart_iodev_write_atomic(void *opaque, const void *buf, size_t len)
{
    UNUSED(opaque);

    if (!uart_base || read32(uart_base + DATA_TX_FREE) < len)
        return 0;

    const u8 *p = buf;
    for (size_t i = 0; i < len; i++)
        write32(uart_base + DATA_TX8, p[i]);

    return len;
}

static struct iodev_ops iodev_dockchannel_uart_ops = {
    .can_read = dockchannel_uart_iodev_can_read,
    .can_write = dockchannel_uart_iodev_can_write,
    .read = dockchannel_uart_iodev_read,
    .write = dockchannel_uart_iodev_write,
    .write_nonblocking = dockchannel_uart_iodev_write_nonblocking,
    .write_atomic = dockchannel_uart_iodev_write_atomic,
};

struct iodev iodev_dockchannel_uart = {
    .ops = &iodev_dockchannel_uart_ops,
    .usage = USAGE_CONSOLE | USAGE_UARTPROXY,
    .lock = SPINLOCK_INIT,
};
