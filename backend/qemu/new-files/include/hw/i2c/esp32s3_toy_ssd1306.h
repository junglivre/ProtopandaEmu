/*
 * SSD1306 I2C model for the experimental ESP32-S3 QEMU fork (Rock 3/M4).
 *
 * Models the subset of the real SSD1306 command/data protocol the pinned
 * Adafruit_SSD1306 library actually drives: horizontal addressing mode via
 * SSD1306_COLUMNADDR/SSD1306_PAGEADDR, and raw GDDRAM byte streaming under
 * control byte 0x40. Multi-byte commands (and even a single logical command
 * list) can be split across several I2C transfers by the real driver's
 * WIRE_MAX chunking, so command-parser state is a persistent device field,
 * not transfer-local. Column/page addressing honors the SSD1306's own
 * sub-window wraparound (not just a flat buffer index) so future callers
 * that set a narrower window behave correctly too.
 */
#pragma once

#include "hw/i2c/i2c.h"

#define TYPE_ESP32S3_TOY_SSD1306 "i2c.esp32s3.toy-ssd1306"
#define ESP32S3_TOY_SSD1306(obj) \
    OBJECT_CHECK(Esp32s3ToySsd1306State, (obj), TYPE_ESP32S3_TOY_SSD1306)

#define SSD1306_WIDTH 128
#define SSD1306_HEIGHT 64
#define SSD1306_PAGES (SSD1306_HEIGHT / 8)
#define SSD1306_GDDRAM_SIZE (SSD1306_WIDTH * SSD1306_PAGES)

typedef struct Esp32s3ToySsd1306State {
    I2CSlave parent_obj;

    uint32_t transfer_count;
    uint32_t byte_count;
    uint8_t control_byte;
    bool first_byte_of_transfer;

    uint8_t gddram[SSD1306_GDDRAM_SIZE];

    /* Horizontal-addressing-mode write window and cursor. */
    uint8_t col_start, col_end, col_cur;
    uint8_t page_start, page_end, page_cur;
    bool display_on;

    /* Command byte-stream parser state; persists across I2C transfers
     * because the real driver can split one logical command list over
     * several WIRE_MAX-bounded transactions. */
    uint8_t pending_opcode;
    uint8_t pending_args_needed;
    uint8_t pending_args_got;
    uint8_t pending_args[6];

    uint32_t frame_seq;
} Esp32s3ToySsd1306State;
