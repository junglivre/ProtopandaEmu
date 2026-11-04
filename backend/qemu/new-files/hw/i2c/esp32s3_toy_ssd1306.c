/*
 * Minimal SSD1306-shaped I2C sink for the experimental ESP32-S3 QEMU fork.
 *
 * The model ACKs every byte and reports the transfer boundary and first
 * control byte. That is enough to prove that a guest's Wire/I2C traffic
 * reaches a registered SSD1306-shaped device without pretending to render an
 * OLED framebuffer.
 */
#include "qemu/osdep.h"
#include "qemu/error-report.h"
#include "hw/i2c/i2c.h"
#include "hw/i2c/esp32s3_toy_ssd1306.h"

static int toy_ssd1306_event(I2CSlave *i2c, enum i2c_event event)
{
    Esp32s3ToySsd1306State *s = ESP32S3_TOY_SSD1306(i2c);

    if (event == I2C_START_SEND) {
        s->transfer_count++;
        s->byte_count = 0;
        s->control_byte = 0;
    } else if (event == I2C_FINISH && s->byte_count != 0) {
        info_report("[protopandaemu.ssd1306] I2C transfer observed address=0x3c bytes=%u control=0x%02x",
                    s->byte_count, s->control_byte);
    }
    return 0;
}

static int toy_ssd1306_send(I2CSlave *i2c, uint8_t data)
{
    Esp32s3ToySsd1306State *s = ESP32S3_TOY_SSD1306(i2c);

    if (s->byte_count == 0) {
        s->control_byte = data;
    }
    s->byte_count++;
    return 0; /* ACK */
}

static uint8_t toy_ssd1306_recv(I2CSlave *i2c)
{
    return 0;
}

static void toy_ssd1306_class_init(ObjectClass *klass, void *data)
{
    I2CSlaveClass *sc = I2C_SLAVE_CLASS(klass);

    sc->event = toy_ssd1306_event;
    sc->send = toy_ssd1306_send;
    sc->recv = toy_ssd1306_recv;
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
