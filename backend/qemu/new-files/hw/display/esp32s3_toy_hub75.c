/*
 * HUB75-shaped LCD_CAM/GDMA sink for the experimental QEMU fork (Rock 3/M4).
 * See the header for the continuous-streaming model this device implements.
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

/* Matches BITS_RGB1_OFFSET/BITS_RGB2_OFFSET/BITS_ADDR_OFFSET in the pinned
 * ESP32-HUB75-MatrixPanel-I2S-DMA.h: every pixel word carries these bits
 * regardless of which bit-plane (colour-depth weight) buffer it came from. */
#define HUB75_BIT_R1 (1u << 0)
#define HUB75_BIT_G1 (1u << 1)
#define HUB75_BIT_B1 (1u << 2)
#define HUB75_BIT_R2 (1u << 3)
#define HUB75_BIT_G2 (1u << 4)
#define HUB75_BIT_B2 (1u << 5)
#define HUB75_ADDR_SHIFT 8
#define HUB75_ADDR_MASK 0x1Fu /* A..E, 5 bits -> up to 32 virtual rows */

#define HUB75_REFRESH_INTERVAL_NS (5 * 1000 * 1000) /* 5ms: real refresh rate is not claimed, only steady-state content */
#define HUB75_MAX_DESCRIPTORS_PER_LAP 4096

typedef struct Hub75Plane {
    uint32_t buf_addr;
    uint32_t length;
    uint8_t *bytes;
} Hub75Plane;

typedef struct Hub75RowBucket {
    uint32_t row_addr;
    GPtrArray *planes; /* Hub75Plane*, in real first-visit (LSB-first) order. */
} Hub75RowBucket;

typedef struct Hub75WalkContext {
    GHashTable *rows; /* row_addr -> Hub75RowBucket* */
    uint32_t descriptors_visited;
    bool logged_first_transfer;
} Hub75WalkContext;

static void hub75_plane_free(gpointer data)
{
    Hub75Plane *plane = data;
    g_free(plane->bytes);
    g_free(plane);
}

static void hub75_row_bucket_free(gpointer data)
{
    Hub75RowBucket *bucket = data;
    g_ptr_array_free(bucket->planes, TRUE);
    g_free(bucket);
}

static bool hub75_walk_cb(void *opaque, uint32_t buf_addr, const uint8_t *data,
                          uint32_t length, bool is_eof)
{
    Hub75WalkContext *ctx = opaque;

    ctx->descriptors_visited++;
    if (!ctx->logged_first_transfer) {
        ctx->logged_first_transfer = true;
        info_report("[protopandaemu.hub75] DMA transfer observed channel=%u bytes=%zu first=%02x%02x%02x%02x",
                    0, (size_t)length,
                    length > 0 ? data[0] : 0, length > 1 ? data[1] : 0,
                    length > 2 ? data[2] : 0, length > 3 ? data[3] : 0);
    }
    if (length < 2) {
        return true;
    }

    uint16_t first_word = (uint16_t)data[0] | ((uint16_t)data[1] << 8);
    uint32_t row_addr = (first_word >> HUB75_ADDR_SHIFT) & HUB75_ADDR_MASK;

    Hub75RowBucket *bucket = g_hash_table_lookup(ctx->rows, GUINT_TO_POINTER(row_addr));
    if (!bucket) {
        bucket = g_new0(Hub75RowBucket, 1);
        bucket->row_addr = row_addr;
        bucket->planes = g_ptr_array_new_with_free_func(hub75_plane_free);
        g_hash_table_insert(ctx->rows, GUINT_TO_POINTER(row_addr), bucket);
    }

    /* Dedup real repeated bit-plane-weight visits to the SAME guest buffer;
     * only a genuinely new buffer address is a new bit-plane. */
    for (guint i = 0; i < bucket->planes->len; i++) {
        Hub75Plane *existing = g_ptr_array_index(bucket->planes, i);
        if (existing->buf_addr == buf_addr) {
            return true;
        }
    }

    Hub75Plane *plane = g_new0(Hub75Plane, 1);
    plane->buf_addr = buf_addr;
    plane->length = length;
    plane->bytes = g_memdup2(data, length);
    g_ptr_array_add(bucket->planes, plane);
    return true;
}

static uint8_t hub75_scale_to_8bit(uint32_t value, uint32_t bit_depth)
{
    if (bit_depth == 0) {
        return 0;
    }
    if (bit_depth >= 8) {
        return (uint8_t)(value >> (bit_depth - 8));
    }
    uint32_t max_value = (1u << bit_depth) - 1;
    return (uint8_t)((value * 255u) / max_value);
}

static uint16_t hub75_word_at_ptr(const uint8_t *base, uint32_t col)
{
    uint32_t offset = col * 2;
    return (uint16_t)base[offset] | ((uint16_t)base[offset + 1] << 8);
}


static void hub75_decode_and_emit(Esp32s3ToyHub75State *s, Hub75WalkContext *ctx)
{
    guint num_rows = g_hash_table_size(ctx->rows);
    if (num_rows == 0) {
        return;
    }

    /* The real pinned ESP32-HUB75-MatrixPanel-I2S-DMA ring carries one
     * fixed descriptor every single lap whose embedded row address reads
     * as HUB75_ADDR_MASK, the all-ones 5-bit sentinel value -- confirmed
     * via trace against a real 1/16-scan 32-row panel: EVERY lap, across
     * two independent sessions (28 frames total), shows row_addr 0..15
     * densely populated with real per-row descriptor data, plus exactly
     * one extra bucket at row_addr=31 (=HUB75_ADDR_MASK), same address
     * every time. Its descriptor's own byte volume is not a reliable
     * discriminator (a real sparsely-updated row can carry less payload
     * than this sentinel), so detect it structurally instead: it is the
     * single bucket sitting at the maximum representable address, outside
     * the dense 0..N-1 run every real config fills contiguously from zero.
     * Trusting max(row_addr)+1 for `rows_per_frame` let that one sentinel
     * double the computed scan depth (16 -> 32), doubling decoded height
     * and duplicating/misplacing every real row into a spurious second
     * copy -- the root cause of a quadrant-duplication artifact visible
     * with dense, asymmetric content (a 64x32 static icon); sparse or
     * symmetric content hid it. */
    GHashTableIter iter;
    gpointer key, value;
    uint32_t dense_run = 0; /* largest N such that addresses 0..N-1 are all present */
    while (g_hash_table_contains(ctx->rows, GUINT_TO_POINTER(dense_run))) {
        dense_run++;
    }
    bool has_sentinel = dense_run > 0 && dense_run <= HUB75_ADDR_MASK &&
                        g_hash_table_contains(ctx->rows, GUINT_TO_POINTER(HUB75_ADDR_MASK));

    uint32_t rows_per_frame = 0;
    g_hash_table_iter_init(&iter, ctx->rows);
    while (g_hash_table_iter_next(&iter, &key, &value)) {
        uint32_t row_addr = GPOINTER_TO_UINT(key);
        if (has_sentinel && row_addr == HUB75_ADDR_MASK) {
            continue; /* sentinel/park descriptor, not real scan content */
        }
        if (row_addr + 1 > rows_per_frame) {
            rows_per_frame = row_addr + 1;
        }
    }
    if (rows_per_frame == 0) {
        return;
    }
    /* The FIRST descriptor configureDMA() links for a row is NOT always a
     * single bit-plane: when a row's full `width*depth*sizeof(uint16_t)`
     * fits in one DMA payload, that one descriptor packs ALL `depth` planes
     * back to back (`getDataPtr(0,0)` with length `getColorDepthSize()`,
     * the whole-row default). Only the ADDITIONAL weight-repeat descriptors
     * (for planes above the refresh-rate-driven transition bit) are ever
     * guaranteed single-plane sized. The smallest length observed anywhere
     * in the lap is therefore the real `width*sizeof(uint16_t)` unit; any
     * larger, evenly-divisible descriptor is that many concatenated planes
     * in bit-plane (LSB-first) order, exactly like the un-packed ones. */
    uint32_t unit_width_bytes = UINT32_MAX;
    g_hash_table_iter_init(&iter, ctx->rows);
    while (g_hash_table_iter_next(&iter, &key, &value)) {
        Hub75RowBucket *bucket = value;
        for (guint i = 0; i < bucket->planes->len; i++) {
            Hub75Plane *plane = g_ptr_array_index(bucket->planes, i);
            if (plane->length >= 2 && plane->length < unit_width_bytes) {
                unit_width_bytes = plane->length;
            }
        }
    }
    if (unit_width_bytes == UINT32_MAX || unit_width_bytes % 2 != 0) {
        return;
    }
    uint32_t width = unit_width_bytes / 2;
    uint32_t height = rows_per_frame * 2;
    uint32_t frame_len = width * height * 3;
    uint8_t *frame = g_malloc0(frame_len);

    g_hash_table_iter_init(&iter, ctx->rows);
    while (g_hash_table_iter_next(&iter, &key, &value)) {
        uint32_t row_addr = GPOINTER_TO_UINT(key);
        Hub75RowBucket *bucket = value;
        if (row_addr >= rows_per_frame) {
            continue; /* outlier/sentinel bucket excluded above; never paint it */
        }
        GPtrArray *logical = g_ptr_array_new(); /* const uint8_t*, not owned */
        bool row_ok = true;
        for (guint i = 0; i < bucket->planes->len && row_ok; i++) {
            Hub75Plane *plane = g_ptr_array_index(bucket->planes, i);
            if (plane->length % unit_width_bytes != 0) {
                row_ok = false;
                break;
            }
            uint32_t sub_planes = plane->length / unit_width_bytes;
            for (uint32_t k = 0; k < sub_planes; k++) {
                g_ptr_array_add(logical, plane->bytes + k * unit_width_bytes);
            }
        }
        uint32_t depth = logical->len;
        if (!row_ok || depth == 0) {
            g_ptr_array_free(logical, TRUE);
            continue;
        }
        for (uint32_t col = 0; col < width; col++) {
            uint32_t r1 = 0, g1 = 0, b1 = 0, r2 = 0, g2 = 0, b2 = 0;
            for (uint32_t plane_idx = 0; plane_idx < depth; plane_idx++) {
                const uint8_t *base = g_ptr_array_index(logical, plane_idx);
                uint16_t word = hub75_word_at_ptr(base, col);
                if (word & HUB75_BIT_R1) r1 |= (1u << plane_idx);
                if (word & HUB75_BIT_G1) g1 |= (1u << plane_idx);
                if (word & HUB75_BIT_B1) b1 |= (1u << plane_idx);
                if (word & HUB75_BIT_R2) r2 |= (1u << plane_idx);
                if (word & HUB75_BIT_G2) g2 |= (1u << plane_idx);
                if (word & HUB75_BIT_B2) b2 |= (1u << plane_idx);
            }
            uint32_t top_y = row_addr;
            uint32_t bottom_y = row_addr + rows_per_frame;
            uint8_t *top_px = frame + (top_y * width + col) * 3;
            top_px[0] = hub75_scale_to_8bit(r1, depth);
            top_px[1] = hub75_scale_to_8bit(g1, depth);
            top_px[2] = hub75_scale_to_8bit(b1, depth);
            uint8_t *bottom_px = frame + (bottom_y * width + col) * 3;
            bottom_px[0] = hub75_scale_to_8bit(r2, depth);
            bottom_px[1] = hub75_scale_to_8bit(g2, depth);
            bottom_px[2] = hub75_scale_to_8bit(b2, depth);
        }
        g_ptr_array_free(logical, TRUE);
    }

    bool changed = (s->last_frame_rgb == NULL || s->last_frame_len != frame_len ||
                    memcmp(s->last_frame_rgb, frame, frame_len) != 0);
    if (changed) {
        GString *hex = g_string_sized_new(frame_len * 2 + 1);
        for (uint32_t i = 0; i < frame_len; i++) {
            g_string_append_printf(hex, "%02x", frame[i]);
        }
        info_report("[protopandaemu.hub75] FRAME_DUMP seq=%u width=%u height=%u rows_per_frame=%u "
                    "bytes=%u descriptors=%u hex=%s",
                    s->frame_seq, width, height, rows_per_frame, frame_len,
                    ctx->descriptors_visited, hex->str);
        g_string_free(hex, TRUE);
        s->frame_seq++;
        g_free(s->last_frame_rgb);
        s->last_frame_rgb = frame;
        s->last_frame_len = frame_len;
        s->last_frame_width = width;
        s->last_frame_height = height;
    } else {
        g_free(frame);
    }
}

static void hub75_refresh_tick(void *opaque)
{
    Esp32s3ToyHub75State *s = opaque;
    uint32_t gdma_out_idx;

    if (s->gdma == NULL || !esp_gdma_get_channel_periph(s->gdma, GDMA_LCDCAM, ESP_GDMA_OUT_IDX, &gdma_out_idx)) {
        /* Not configured yet this tick; try again next tick rather than
         * treating a transient pre-setup state as a fatal error. */
        timer_mod(s->refresh_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + HUB75_REFRESH_INTERVAL_NS);
        return;
    }

    Hub75WalkContext ctx = {0};
    ctx.rows = g_hash_table_new_full(g_direct_hash, g_direct_equal, NULL, hub75_row_bucket_free);

    bool ok = esp_gdma_walk_channel_until_eof(s->gdma, gdma_out_idx, HUB75_MAX_DESCRIPTORS_PER_LAP,
                                              hub75_walk_cb, &ctx);
    if (ok && ctx.descriptors_visited > 0) {
        hub75_decode_and_emit(s, &ctx);
    }
    g_hash_table_destroy(ctx.rows);

    if (s->streaming) {
        timer_mod(s->refresh_timer, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL) + HUB75_REFRESH_INTERVAL_NS);
    }
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
        /* Real hardware auto-loops the same continuous DMA stream forever
         * once started; the pinned library never re-triggers LCD_START or
         * polls it again, so this bit is left exactly as the guest wrote it
         * (unlike a one-shot transfer, it is never cleared here). */
        if (!s->streaming) {
            s->streaming = true;
            hub75_refresh_tick(s);
        }
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
    s->streaming = false;
    s->frame_seq = 0;
    if (s->refresh_timer) {
        timer_del(s->refresh_timer);
    }
    g_free(s->last_frame_rgb);
    s->last_frame_rgb = NULL;
    s->last_frame_len = 0;
}

static void toy_hub75_init(Object *obj)
{
    Esp32s3ToyHub75State *s = ESP32S3_TOY_HUB75(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &toy_hub75_ops, s,
                          TYPE_ESP32S3_TOY_HUB75, ESP32S3_TOY_HUB75_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    s->refresh_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, hub75_refresh_tick, s);
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
