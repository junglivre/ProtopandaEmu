/*
 * Minimal HUB75-shaped LCD_CAM/DMA sink for the experimental QEMU fork.
 *
 * The Phase-A guest supplies one linked-list descriptor through the model's
 * research register at offset 0x00, then writes LCD_CAM_LCD_USER at offset
 * 0x14 with LCD_START. The model reads the descriptor and payload through an
 * AddressSpace and records the observed bytes. For the original firmware it
 * accepts the real LCD_CAM register writes and clears LCD_START immediately so
 * the library's completion poll can advance; no visual timing is claimed.
 */
#include "qemu/osdep.h"
#include "qemu/error-report.h"
#include "qapi/error.h"
#include "sysemu/dma.h"
#include "hw/qdev-properties.h"
#include "hw/sysbus.h"
#include "hw/display/esp32s3_toy_hub75.h"

#define TOY_LCD_USER_OFFSET 0x14
#define TOY_LCD_START       (1u << 27)

static void toy_hub75_dma_trace(Esp32s3ToyHub75State *s, uint32_t desc_addr)
{
    uint32_t desc[3] = {0};
    uint8_t sample[16] = {0};
    uint32_t cfg;
    uint32_t length;
    uint32_t buffer_addr;
    MemTxResult res;

    res = dma_memory_read(&s->dma_as, desc_addr, desc, sizeof(desc),
                          MEMTXATTRS_UNSPECIFIED);
    if (res != MEMTX_OK) {
        info_report("[protopandaemu.hub75] LCD_CAM DMA start observed descriptor=0x%08x descriptor-read=failed",
                    desc_addr);
        return;
    }

    cfg = le32_to_cpu(desc[0]);
    length = (cfg >> 12) & 0xfff;
    if (length == 0) {
        length = cfg & 0xfff;
    }
    length = MIN(length, (uint32_t)sizeof(sample));
    buffer_addr = le32_to_cpu(desc[1]);
    res = dma_memory_read(&s->dma_as, buffer_addr, sample, length,
                          MEMTXATTRS_UNSPECIFIED);
    if (res != MEMTX_OK) {
        info_report("[protopandaemu.hub75] LCD_CAM DMA start observed descriptor=0x%08x payload-read=failed",
                    desc_addr);
        return;
    }

    info_report("[protopandaemu.hub75] DMA transfer observed descriptor=0x%08x bytes=%u first=%02x%02x%02x%02x",
                desc_addr, length, sample[0], sample[1], sample[2], sample[3]);
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
        if (s->regs[0] != 0) {
            toy_hub75_dma_trace(s, s->regs[0]);
        }
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

    if (s->soc_mr == NULL) {
        error_setg(errp, "toy HUB75 device requires soc_mr");
        return;
    }
    address_space_init(&s->dma_as, s->soc_mr, "protopandaemu.toy-hub75-dma");
}

static void toy_hub75_unrealize(DeviceState *dev)
{
    Esp32s3ToyHub75State *s = ESP32S3_TOY_HUB75(dev);

    address_space_destroy(&s->dma_as);
}

static Property toy_hub75_properties[] = {
    DEFINE_PROP_LINK("soc_mr", Esp32s3ToyHub75State, soc_mr,
                     TYPE_MEMORY_REGION, MemoryRegion *),
    DEFINE_PROP_END_OF_LIST(),
};

static void toy_hub75_class_init(ObjectClass *klass, void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);

    device_class_set_legacy_reset(dc, toy_hub75_reset);
    dc->realize = toy_hub75_realize;
    dc->unrealize = toy_hub75_unrealize;
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
