/*
 * HUB75-shaped LCD_CAM/GDMA sink for the experimental QEMU fork (Rock 3/M4).
 *
 * Real HUB75 panel refresh is a CONTINUOUS DMA stream: the pinned
 * ESP32-HUB75-MatrixPanel-I2S-DMA library triggers `gdma_start()` and sets
 * `LCD_CAM.lcd_user.lcd_start` exactly once (never polling or re-setting it
 * afterward), relying on the real hardware auto-looping the same circular
 * descriptor ring forever; a buffer flip only rewrites `next` links in
 * guest RAM and waits for the ring's own ongoing end-of-lap interrupt. This
 * model reproduces that with a repeating QEMU timer that re-walks the ring
 * from its (possibly just-redirected) link head on every tick, decodes the
 * real bit-plane-weighted pixel stream into an RGB framebuffer, and only
 * emits a new frame event when the decoded content actually changes.
 */
#pragma once

#include "hw/hw.h"
#include "hw/sysbus.h"
#include "hw/dma/esp_gdma.h"
#include "qemu/timer.h"

#define TYPE_ESP32S3_TOY_HUB75 "display.esp32s3.toy-hub75"
#define ESP32S3_TOY_HUB75(obj) \
    OBJECT_CHECK(Esp32s3ToyHub75State, (obj), TYPE_ESP32S3_TOY_HUB75)

#define ESP32S3_TOY_HUB75_SIZE 0x1000

typedef struct Esp32s3ToyHub75State {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    uint32_t regs[ESP32S3_TOY_HUB75_SIZE / sizeof(uint32_t)];
    uint32_t transfer_count;
    /* Set by the ESP32-S3 SoC before realization, like the AES/SHA models. */
    ESPGdmaState *gdma;

    QEMUTimer *refresh_timer;
    bool streaming;
    uint32_t frame_seq;
    uint8_t *last_frame_rgb;
    uint32_t last_frame_width;
    uint32_t last_frame_height;
    uint32_t last_frame_len;
} Esp32s3ToyHub75State;
