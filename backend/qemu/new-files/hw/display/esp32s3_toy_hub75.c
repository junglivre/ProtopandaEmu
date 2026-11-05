/*
 * Minimal HUB75-shaped LCD_CAM/DMA sink for the experimental QEMU fork.
 *
 * The guest configures the real ESP32-S3 GDMA LCD_CAM channel and then writes
 * LCD_CAM_LCD_USER at offset 0x14 with LCD_START. The model consumes a sample
 * through the same GDMA descriptor-chain API used by the real peripherals.
 * It clears LCD_START after a successful read so the pinned matrix library's
 * completion poll can advance; no visual timing is claimed.
 */
#include "qemu/osdep.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "hw/dma/esp_gdma.h"
#include "hw/qdev-properties.h"
#include "hw/sysbus.h"
#include "hw/display/esp32s3_toy_hub75.h"

#define TOY_LCD_USER_OFFSET 0x14
#define TOY_LCD_START       (1u << 27)

static bool toy_hub75_dma_trace(Esp32s3ToyHub75State *s)
{
    uint8_t sample[16] = {0};
    uint32_t gdma_out_idx;

    if (s->gdma == NULL) {
        warn_report("[protopandaemu.hub75] LCD_CAM DMA start observed without GDMA link");
        return false;
    }

    if (!esp_gdma_get_channel_periph(s->gdma, GDMA_LCDCAM, ESP_GDMA_OUT_IDX,
                                     &gdma_out_idx)) {
        warn_report("[protopandaemu.hub75] LCD_CAM DMA start observed without configured GDMA LCD_CAM channel");
        return false;
    }

    if (!esp_gdma_read_channel(s->gdma, gdma_out_idx, sample, sizeof(sample))) {
        warn_report("[protopandaemu.hub75] LCD_CAM DMA start observed GDMA descriptor-chain read failed channel=%u",
                    gdma_out_idx);
        return false;
    }

    info_report("[protopandaemu.hub75] DMA transfer observed channel=%u bytes=%zu first=%02x%02x%02x%02x",
                gdma_out_idx, sizeof(sample), sample[0], sample[1], sample[2], sample[3]);
    return true;
}

static uint64_t toy_hub75_read(void *opaque, hwaddr addr, unsigned int size)
{
    Esp32s3ToyHub75State *s = ESP32S3_TOY_HUB75(opaque);
    unsigned index = (unsigned)(addr / sizeof(uint32_t));

    if (index >= ARRAY_SIZE(s->regs)) {
        return 0;
    }
    return s->regs[index];
}

static void toy_hub75_write(void *opaque, hwaddr addr, uint64_t value,
                            unsigned int size)
{
    Esp32s3ToyHub75State *s = ESP32S3_TOY_HUB75(opaque);
    unsigned index = (unsigned)(addr / sizeof(uint32_t));
    uint32_t value32 = (uint32_t)value;

    if (index >= ARRAY_SIZE(s->regs)) {
        return;
    }

    if (addr == TOY_LCD_USER_OFFSET && (value32 & TOY_LCD_START)) {
        s->transfer_count++;
        info_report("[protopandaemu.hub75] LCD_CAM DMA start observed count=%u value=0x%08x",
                    s->transfer_count, value32);
        toy_hub75_dma_trace(s);
        /* LCD_START is the completion condition polled by the matrix library. */
        value32 &= ~TOY_LCD_START;
    }
    s->regs[index] = value32;
}

static const MemoryRegionOps toy_hub75_ops = {
    .read = toy_hub75_read,
    .write = toy_hub75_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 4,
        .max_access_size = 4,
    },
};

static void toy_hub75_reset(DeviceState *dev)
{
    Esp32s3ToyHub75State *s = ESP32S3_TOY_HUB75(dev);

    memset(s->regs, 0, sizeof(s->regs));
    s->transfer_count = 0;
}

static void toy_hub75_init(Object *obj)
{
    Esp32s3ToyHub75State *s = ESP32S3_TOY_HUB75(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &toy_hub75_ops, s,
                          TYPE_ESP32S3_TOY_HUB75, ESP32S3_TOY_HUB75_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
}

static void toy_hub75_realize(DeviceState *dev, Error **errp)
{
    Esp32s3ToyHub75State *s = ESP32S3_TOY_HUB75(dev);

    if (s->gdma == NULL) {
        error_setg(errp, "toy HUB75 device requires gdma");
        return;
    }
}

static Property toy_hub75_properties[] = {
    DEFINE_PROP_END_OF_LIST(),
};

static void toy_hub75_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    device_class_set_legacy_reset(dc, toy_hub75_reset);
    dc->realize = toy_hub75_realize;
    device_class_set_props(dc, toy_hub75_properties);
}

static const TypeInfo toy_hub75_info = {
    .name = TYPE_ESP32S3_TOY_HUB75,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(Esp32s3ToyHub75State),
    .instance_init = toy_hub75_init,
    .class_init = toy_hub75_class_init,
};

static void toy_hub75_register_types(void)
{
    type_register_static(&toy_hub75_info);
}

type_init(toy_hub75_register_types)
