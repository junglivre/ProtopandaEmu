/*
 * Minimal SSD1306-shaped I2C sink for the experimental ESP32-S3 QEMU fork.
 *
 * It deliberately models only the observable contract needed by Rock 4:
 * address ACKs, byte acceptance, and a trace of controller traffic. It is not
 * a pixel-accurate OLED model.
 */
#pragma once

#include "hw/i2c/i2c.h"

#define TYPE_ESP32S3_TOY_SSD1306 "i2c.esp32s3.toy-ssd1306"
#define ESP32S3_TOY_SSD1306(obj) \
    OBJECT_CHECK(Esp32s3ToySsd1306State, (obj), TYPE_ESP32S3_TOY_SSD1306)

typedef struct Esp32s3ToySsd1306State {
    I2CSlave parent_obj;
    uint32_t transfer_count;
    uint32_t byte_count;
    uint8_t control_byte;
} Esp32s3ToySsd1306State;
