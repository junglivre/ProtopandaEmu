/*
 * ESP32-S3 SENS (SAR ADC one-shot) peripheral -- minimal research model.
 *
 * ProtopandaEmu Rock 1/M1 spike: the pinned Espressif QEMU fork
 * (esp-develop-9.2.2-20260417) has no SENS device; the whole SENS MMIO range
 * falls through the SoC's generic always-zero read handler
 * (hw/xtensa/esp32s3.c esp32s3_io_read). ESP-IDF's ADC one-shot driver
 * (adc_oneshot_ll_get_event, hal/esp32s3/include/hal/adc_ll.h) polls the
 * MEASx_DONE_SAR bit after setting MEASx_START_SAR, so it spins forever
 * against a register that always reads 0. That is the first blocker found
 * booting the real, unmodified Proto-panda v3.4.0 firmware past its ADC
 * calibration step (see docs/qemu-adc-spike.md for the exact evidence: QMP
 * CPU-state sample, addr2line symbolization, and the ESP-IDF header offsets
 * this model implements).
 *
 * This is deliberately minimal and explicitly NOT a calibrated electrical
 * model: writing the START bit completes the conversion on the same access
 * (no clock division, no real sampling window, no pad/attenuation routing),
 * and the returned sample is a fixed, declared-uncalibrated constant. Every
 * other register in the mapped range behaves as a plain read/write storage
 * cell so firmware that merely configures other SENS fields does not read
 * back a static zero.
 *
 * Register facts (ESP-IDF esp32s3/include/soc/{reg_base.h,sens_reg.h}):
 *   DR_REG_SENS_BASE            = 0x60008800
 *   SENS_SAR_MEAS1_CTRL2_REG    = base + 0x0C  (bit17 START, bit16 DONE, [15:0] DATA)
 *   SENS_SAR_MEAS2_CTRL2_REG    = base + 0x30  (bit17 START, bit16 DONE, [15:0] DATA)
 *
 * Copyright (c) 2026 ProtopandaEmu project. GPLv2-or-later, matching the
 * QEMU tree this file is added to.
 */

#include "qemu/osdep.h"
#include "qemu/log.h"
#include "hw/hw.h"
#include "hw/sysbus.h"
#include "hw/misc/esp32s3_sens.h"

#define SENS_SAR_MEAS1_CTRL2_OFFSET   0x0C
#define SENS_SAR_MEAS2_CTRL2_OFFSET   0x30

#define SENS_MEASX_START_SAR_BIT   (1u << 17)
#define SENS_MEASX_DONE_SAR_BIT    (1u << 16)
#define SENS_MEASX_DATA_SAR_MASK   0x0000FFFFu

/*
 * Fixed one-shot samples. These are declared-uncalibrated placeholders (not
 * a real analog source); callers that need real stimulus must not treat
 * them as physical measurements. Picked as distinguishable, in-range 12-bit
 * values purely so a trace can tell the two ADC units' results apart.
 */
#define ESP32S3_SENS_ADC1_FIXED_SAMPLE   0x0800
#define ESP32S3_SENS_ADC2_FIXED_SAMPLE   0x0C00

static void esp32s3_sens_complete_ctrl2(Esp32s3SensState *s,
                                         unsigned word_index,
                                         uint16_t sample,
                                         uint32_t written_value)
{
    /* START triggers an immediate (same-access) one-shot completion: set
     * DONE, publish the fixed sample in DATA, and clear START (hardware
     * clears it once the conversion is under way on real silicon; nothing
     * in the ESP-IDF driver polls START itself, only DONE). Writes that do
     * not set START leave DONE/DATA cleared, i.e. "no pending/finished
     * conversion", while preserving whatever other configuration bits the
     * driver wrote into the same register. */
    uint32_t value = written_value & ~(SENS_MEASX_START_SAR_BIT
                                        | SENS_MEASX_DONE_SAR_BIT
                                        | SENS_MEASX_DATA_SAR_MASK);
    if (written_value & SENS_MEASX_START_SAR_BIT) {
        value |= SENS_MEASX_DONE_SAR_BIT;
        value |= ((uint32_t)sample) & SENS_MEASX_DATA_SAR_MASK;
    }
    s->regs[word_index] = value;
}

static uint64_t esp32s3_sens_read(void *opaque, hwaddr addr, unsigned int size)
{
    Esp32s3SensState *s = ESP32S3_SENS(opaque);
    unsigned word_index = (unsigned)(addr / sizeof(uint32_t));

    if (word_index >= ESP32S3_SENS_SIZE / sizeof(uint32_t)) {
        qemu_log_mask(LOG_GUEST_ERROR,
                      "[esp32s3.sens] out-of-range read at offset 0x%" HWADDR_PRIX "\n",
                      addr);
        return 0;
    }
    return s->regs[word_index];
}

static void esp32s3_sens_write(void *opaque, hwaddr addr, uint64_t value,
                                unsigned int size)
{
    Esp32s3SensState *s = ESP32S3_SENS(opaque);
    unsigned word_index = (unsigned)(addr / sizeof(uint32_t));

    if (word_index >= ESP32S3_SENS_SIZE / sizeof(uint32_t)) {
        qemu_log_mask(LOG_GUEST_ERROR,
                      "[esp32s3.sens] out-of-range write at offset 0x%" HWADDR_PRIX "\n",
                      addr);
        return;
    }

    if (addr == SENS_SAR_MEAS1_CTRL2_OFFSET) {
        esp32s3_sens_complete_ctrl2(s, word_index, s->adc1_sample, (uint32_t)value);
        return;
    }
    if (addr == SENS_SAR_MEAS2_CTRL2_OFFSET) {
        esp32s3_sens_complete_ctrl2(s, word_index, s->adc2_sample, (uint32_t)value);
        return;
    }

    /* Every other SENS register: plain read/write storage, no behaviour. */
    s->regs[word_index] = (uint32_t)value;
}

static const MemoryRegionOps esp32s3_sens_ops = {
    .read = esp32s3_sens_read,
    .write = esp32s3_sens_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 4,
        .max_access_size = 4,
    },
};

static void esp32s3_sens_reset(DeviceState *dev)
{
    Esp32s3SensState *s = ESP32S3_SENS(dev);
    memset(s->regs, 0, sizeof(s->regs));
    s->adc1_sample = ESP32S3_SENS_ADC1_FIXED_SAMPLE;
    s->adc2_sample = ESP32S3_SENS_ADC2_FIXED_SAMPLE;
}

static void esp32s3_sens_init(Object *obj)
{
    Esp32s3SensState *s = ESP32S3_SENS(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &esp32s3_sens_ops, s,
                          TYPE_ESP32S3_SENS, ESP32S3_SENS_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
}

static void esp32s3_sens_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    device_class_set_legacy_reset(dc, esp32s3_sens_reset);
}

static const TypeInfo esp32s3_sens_info = {
    .name = TYPE_ESP32S3_SENS,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(Esp32s3SensState),
    .instance_init = esp32s3_sens_init,
    .class_init = esp32s3_sens_class_init,
};

static void esp32s3_sens_register_types(void)
{
    type_register_static(&esp32s3_sens_info);
}

type_init(esp32s3_sens_register_types)
