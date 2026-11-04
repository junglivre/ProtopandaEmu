/*
 * Minimal HUB75-shaped LCD_CAM/DMA sink for the experimental QEMU fork.
 *
 * This device consumes a small ESP32-style DMA descriptor for the Phase-A
 * guest and also exposes the LCD_CAM LCD_USER start bit used by the pinned
 * ESP32-HUB75-MatrixPanel-I2S-DMA library.
 */
#pragma once

#include "hw/hw.h"
#include "hw/sysbus.h"

#define TYPE_ESP32S3_TOY_HUB75 "display.esp32s3.toy-hub75"
#define ESP32S3_TOY_HUB75(obj) \
    OBJECT_CHECK(Esp32s3ToyHub75State, (obj), TYPE_ESP32S3_TOY_HUB75)

#define ESP32S3_TOY_HUB75_SIZE 0x1000

typedef struct Esp32s3ToyHub75State {
    SysBusDevice parent_obj;
    MemoryRegion iomem;
    MemoryRegion *soc_mr;
    AddressSpace dma_as;
    uint32_t regs[ESP32S3_TOY_HUB75_SIZE / sizeof(uint32_t)];
    uint32_t transfer_count;
} Esp32s3ToyHub75State;
