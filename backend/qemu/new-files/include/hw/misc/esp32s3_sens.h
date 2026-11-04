/*
 * ESP32-S3 SENS (SAR ADC one-shot) peripheral -- minimal research model.
 *
 * This is NOT a calibrated electrical model. It implements exactly the
 * register state machine the ESP-IDF ADC one-shot driver (adc_oneshot_ll.h /
 * adc_ll.h) polls at boot/calibration time: writing the START bit in either
 * SAR_MEAS{1,2}_CTRL2 immediately (same CPU cycle) completes the conversion,
 * sets the corresponding DONE bit, and exposes a fixed, declared-uncalibrated
 * sample value in the DATA field. No clock division, sampling window, pad
 * routing, attenuation, or power-management behaviour is modelled. The
 * ESP32-S3 TRM register map used is cited in esp32s3_sens.c.
 *
 * Copyright (c) 2026 ProtopandaEmu project. GPLv2-or-later, matching the
 * QEMU tree this file is added to.
 */
#pragma once

#include "hw/hw.h"
#include "hw/sysbus.h"

#define TYPE_ESP32S3_SENS "misc.esp32s3.sens"
#define ESP32S3_SENS(obj) OBJECT_CHECK(Esp32s3SensState, (obj), TYPE_ESP32S3_SENS)

/* soc/esp32s3/include/soc/reg_base.h: DR_REG_SENS_BASE */
#define ESP32S3_SENS_BASE   0x60008800
/* Spacing to the next peripheral (DR_REG_RTC_I2C_BASE) in reg_base.h. */
#define ESP32S3_SENS_SIZE   0x400

typedef struct Esp32s3SensState {
    SysBusDevice parent_obj;
    MemoryRegion iomem;

    /* Raw register storage, byte-addressed, covering the modelled range. */
    uint32_t regs[ESP32S3_SENS_SIZE / sizeof(uint32_t)];

    /* Declared-uncalibrated one-shot sample each unit returns on DONE. */
    uint16_t adc1_sample;
    uint16_t adc2_sample;
} Esp32s3SensState;
