/*
 * SSD1306 I2C model for the experimental ESP32-S3 QEMU fork (Rock 3/M4).
 *
 * Real command/data protocol subset, real 128x64 GDDRAM, real addressing-
 * window wraparound. See the header for the protocol notes.
 */
#include "qemu/osdep.h"
#include "qemu/error-report.h"
#include "hw/i2c/i2c.h"
#include "hw/i2c/esp32s3_toy_ssd1306.h"

enum {
    SSD1306_CMD_MEMORYMODE = 0x20,
    SSD1306_CMD_COLUMNADDR = 0x21,
    SSD1306_CMD_PAGEADDR = 0x22,
    SSD1306_CMD_CONTRAST = 0x81,
    SSD1306_CMD_CHARGEPUMP = 0x8D,
    SSD1306_CMD_MULTIPLEX = 0xA8,
    SSD1306_CMD_DISPLAYOFFSET = 0xD3,
    SSD1306_CMD_CLOCKDIV = 0xD5,
    SSD1306_CMD_PRECHARGE = 0xD9,
    SSD1306_CMD_COMPINS = 0xDA,
    SSD1306_CMD_VCOMDETECT = 0xDB,
    SSD1306_CMD_DISPLAYOFF = 0xAE,
    SSD1306_CMD_DISPLAYON = 0xAF,
    SSD1306_CMD_HSCROLL_SETUP_R = 0x26,
    SSD1306_CMD_HSCROLL_SETUP_L = 0x27,
    SSD1306_CMD_VHSCROLL_SETUP_R = 0x29,
    SSD1306_CMD_VHSCROLL_SETUP_L = 0x2A,
};

/* Number of argument bytes the real SSD1306 expects after this opcode. */
static uint8_t ssd1306_command_arg_count(uint8_t opcode)
{
    switch (opcode) {
    case SSD1306_CMD_MEMORYMODE:
    case SSD1306_CMD_CONTRAST:
    case SSD1306_CMD_CHARGEPUMP:
    case SSD1306_CMD_MULTIPLEX:
    case SSD1306_CMD_DISPLAYOFFSET:
    case SSD1306_CMD_CLOCKDIV:
    case SSD1306_CMD_PRECHARGE:
    case SSD1306_CMD_COMPINS:
    case SSD1306_CMD_VCOMDETECT:
        return 1;
    case SSD1306_CMD_COLUMNADDR:
    case SSD1306_CMD_PAGEADDR:
        return 2;
    case SSD1306_CMD_VHSCROLL_SETUP_R:
    case SSD1306_CMD_VHSCROLL_SETUP_L:
        return 5;
    case SSD1306_CMD_HSCROLL_SETUP_R:
    case SSD1306_CMD_HSCROLL_SETUP_L:
        return 6;
    default:
        /* Covers DISPLAYOFF/ON, DISPLAYALLON*, NORMAL/INVERTDISPLAY,
         * SEGREMAP, COMSCAN*, (DE)ACTIVATE_SCROLL, SET_START_LINE
         * (0x40-0x7F), page-mode PAGE_START (0xB0-0xB7) and column
         * low/high nibble (0x00-0x1F) -- all zero-argument on this part. */
        return 0;
    }
}

static void ssd1306_dump_frame(Esp32s3ToySsd1306State *s)
{
    GString *hex = g_string_sized_new(SSD1306_GDDRAM_SIZE * 2 + 1);
    for (size_t i = 0; i < SSD1306_GDDRAM_SIZE; i++) {
        g_string_append_printf(hex, "%02x", s->gddram[i]);
    }
    info_report("[protopandaemu.ssd1306] GDDRAM_DUMP seq=%u width=%u height=%u "
                "bytes=%u hex=%s",
                s->frame_seq, SSD1306_WIDTH, SSD1306_HEIGHT,
                (unsigned)SSD1306_GDDRAM_SIZE, hex->str);
    g_string_free(hex, TRUE);
    s->frame_seq++;
}

static void ssd1306_apply_command(Esp32s3ToySsd1306State *s, uint8_t opcode,
                                  const uint8_t *args, uint8_t nargs)
{
    switch (opcode) {
    case SSD1306_CMD_COLUMNADDR: {
        uint8_t start = args[0] % SSD1306_WIDTH;
        uint8_t end = args[1] >= SSD1306_WIDTH ? SSD1306_WIDTH - 1 : args[1];
        s->col_start = start;
        s->col_end = end >= start ? end : start;
        s->col_cur = s->col_start;
        info_report("[protopandaemu.ssd1306] COLUMNADDR start=%u end=%u",
                    s->col_start, s->col_end);
        break;
    }
    case SSD1306_CMD_PAGEADDR: {
        uint8_t start = args[0] % SSD1306_PAGES;
        uint8_t end = args[1] >= SSD1306_PAGES ? SSD1306_PAGES - 1 : args[1];
        s->page_start = start;
        s->page_end = end >= start ? end : start;
        s->page_cur = s->page_start;
        info_report("[protopandaemu.ssd1306] PAGEADDR start=%u end=%u",
                    s->page_start, s->page_end);
        break;
    }
    case SSD1306_CMD_DISPLAYOFF:
        s->display_on = false;
        info_report("[protopandaemu.ssd1306] DISPLAY_OFF");
        break;
    case SSD1306_CMD_DISPLAYON:
        s->display_on = true;
        info_report("[protopandaemu.ssd1306] DISPLAY_ON");
        break;
    default:
        if (opcode >= 0xB0 && opcode <= 0xB7) {
            s->page_cur = opcode - 0xB0; /* page-mode PAGE_START, 0 args */
        } else if (opcode <= 0x0F) {
            s->col_cur = (s->col_cur & 0xF0) | opcode; /* column low nibble */
        } else if (opcode >= 0x10 && opcode <= 0x1F) {
            s->col_cur = (s->col_cur & 0x0F) | ((opcode & 0x0F) << 4);
        }
        break;
    }
}

static void ssd1306_feed_command_byte(Esp32s3ToySsd1306State *s, uint8_t byte)
{
    if (s->pending_args_needed > 0) {
        s->pending_args[s->pending_args_got++] = byte;
        if (s->pending_args_got < s->pending_args_needed) {
            return;
        }
        ssd1306_apply_command(s, s->pending_opcode, s->pending_args,
                              s->pending_args_got);
        s->pending_args_needed = 0;
        s->pending_args_got = 0;
        return;
    }
    uint8_t nargs = ssd1306_command_arg_count(byte);
    if (nargs == 0) {
        ssd1306_apply_command(s, byte, NULL, 0);
        return;
    }
    s->pending_opcode = byte;
    s->pending_args_needed = nargs;
    s->pending_args_got = 0;
}

static void ssd1306_feed_data_byte(Esp32s3ToySsd1306State *s, uint8_t byte)
{
    size_t idx = (size_t)s->page_cur * SSD1306_WIDTH + s->col_cur;
    if (idx < SSD1306_GDDRAM_SIZE) {
        s->gddram[idx] = byte;
    }
    if (s->col_cur >= s->col_end) {
        s->col_cur = s->col_start;
        if (s->page_cur >= s->page_end) {
            s->page_cur = s->page_start;
            ssd1306_dump_frame(s);
        } else {
            s->page_cur++;
        }
    } else {
        s->col_cur++;
    }
}

static int toy_ssd1306_event(I2CSlave *i2c, enum i2c_event event)
{
    Esp32s3ToySsd1306State *s = ESP32S3_TOY_SSD1306(i2c);

    if (event == I2C_START_SEND) {
        s->transfer_count++;
        s->byte_count = 0;
        s->control_byte = 0;
        s->first_byte_of_transfer = true;
    } else if (event == I2C_FINISH && s->byte_count != 0) {
        info_report("[protopandaemu.ssd1306] I2C transfer observed address=0x3c bytes=%u control=0x%02x",
                    s->byte_count, s->control_byte);
    }
    return 0;
}

static int toy_ssd1306_send(I2CSlave *i2c, uint8_t data)
{
    Esp32s3ToySsd1306State *s = ESP32S3_TOY_SSD1306(i2c);

    if (s->first_byte_of_transfer) {
        s->control_byte = data;
        s->first_byte_of_transfer = false;
    } else if (s->control_byte == 0x40) {
        ssd1306_feed_data_byte(s, data);
    } else {
        /* Control byte 0x00 (Co=0, D/C=0): a command stream. Any other
         * control value is not used by the pinned driver; treat it as a
         * command stream too rather than silently dropping bytes. */
        ssd1306_feed_command_byte(s, data);
    }
    s->byte_count++;
    return 0; /* ACK */
}

static uint8_t toy_ssd1306_recv(I2CSlave *i2c)
{
    return 0;
}

static void toy_ssd1306_reset(DeviceState *dev)
{
    Esp32s3ToySsd1306State *s = ESP32S3_TOY_SSD1306(dev);

    memset(s->gddram, 0, sizeof(s->gddram));
    s->col_start = 0;
    s->col_end = SSD1306_WIDTH - 1;
    s->col_cur = 0;
    s->page_start = 0;
    s->page_end = SSD1306_PAGES - 1;
    s->page_cur = 0;
    s->display_on = false;
    s->pending_args_needed = 0;
    s->pending_args_got = 0;
    s->transfer_count = 0;
    s->byte_count = 0;
    s->frame_seq = 0;
}

static void toy_ssd1306_class_init(ObjectClass *klass, void *data)
{
    I2CSlaveClass *sc = I2C_SLAVE_CLASS(klass);
    DeviceClass *dc = DEVICE_CLASS(klass);

    sc->event = toy_ssd1306_event;
    sc->send = toy_ssd1306_send;
    sc->recv = toy_ssd1306_recv;
    device_class_set_legacy_reset(dc, toy_ssd1306_reset);
}

static const TypeInfo toy_ssd1306_info = {
    .name = TYPE_ESP32S3_TOY_SSD1306,
    .parent = TYPE_I2C_SLAVE,
    .instance_size = sizeof(Esp32s3ToySsd1306State),
    .class_init = toy_ssd1306_class_init,
};

static void toy_ssd1306_register_types(void)
{
    type_register_static(&toy_ssd1306_info);
}

type_init(toy_ssd1306_register_types)
