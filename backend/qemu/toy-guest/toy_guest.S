/* Minimal Xtensa guest for Rock 4 Phase A. */

.section .literal, "a"
.align 4
stack_top_literal:
    .word 0x3fcff000

.section .text, "ax"
.align 4
.global _start
.type _start, @function
_start:
    l32r    a1, stack_top_literal
    call0   toy_main
1:
    j       1b
.size _start, .-_start

.type putc, @function
putc:
    movi    a2, 0x60000000
    s8i     a3, a2, 0
    ret
.size putc, .-putc

.type toy_main, @function
toy_main:
    /* UART marker: TOY_GUEST onSetup completed\n */
    movi    a3, 'T'
    call0   putc
    movi    a3, 'O'
    call0   putc
    movi    a3, 'Y'
    call0   putc
    movi    a3, '_'
    call0   putc
    movi    a3, 'G'
    call0   putc
    movi    a3, 'U'
    call0   putc
    movi    a3, 'E'
    call0   putc
    movi    a3, 'S'
    call0   putc
    movi    a3, 'T'
    call0   putc
    movi    a3, ' '
    call0   putc
    movi    a3, 'o'
    call0   putc
    movi    a3, 'n'
    call0   putc
    movi    a3, 'S'
    call0   putc
    movi    a3, 'e'
    call0   putc
    movi    a3, 't'
    call0   putc
    movi    a3, 'u'
    call0   putc
    movi    a3, 'p'
    call0   putc
    movi    a3, ' '
    call0   putc
    movi    a3, 'c'
    call0   putc
    movi    a3, 'o'
    call0   putc
    movi    a3, 'm'
    call0   putc
    movi    a3, 'p'
    call0   putc
    movi    a3, 'l'
    call0   putc
    movi    a3, 'e'
    call0   putc
    movi    a3, 't'
    call0   putc
    movi    a3, 'e'
    call0   putc
    movi    a3, 'd'
    call0   putc
    movi    a3, 10
    call0   putc

    /* SSD1306-shaped I2C transfer: 0x3c write, control byte, command. */
    movi    a2, 0x60013000
    movi    a3, 0x78
    s32i    a3, a2, 0x1c
    movi    a3, 0x00
    s32i    a3, a2, 0x1c
    movi    a3, 0xae
    s32i    a3, a2, 0x1c
    movi    a3, 0x0903       /* cmd0: 3 bytes, ACK-check, WRITE */
    s32i    a3, a2, 0x58
    movi    a3, 0x1000       /* cmd1: STOP (ESP32-S3 opcode 2) */
    s32i    a3, a2, 0x5c
    movi    a3, 0x30         /* master mode + TRANS_START */
    s32i    a3, a2, 0x04

    /* A tiny DMA descriptor and payload in ESP32-S3 DRAM. */
    movi    a2, 0x3fc80000
    movi    a3, 0xc0010010   /* owner+suc_eof, size=length=16 */
    s32i    a3, a2, 0
    movi    a3, 0x3fc80020
    s32i    a3, a2, 4
    movi    a3, 0
    s32i    a3, a2, 8
    movi    a2, 0x3fc80020
    movi    a3, 0x11223344
    s32i    a3, a2, 0
    movi    a3, 0x55667788
    s32i    a3, a2, 4
    movi    a3, 0x99aabbcc
    s32i    a3, a2, 8
    movi    a3, 0xddeeff00
    s32i    a3, a2, 12

    /* Configure the real GDMA channel 0 TX path for LCD_CAM (peripheral 5). */
    movi    a2, 0x6003f000
    movi    a3, 5
    s32i    a3, a2, 0xa8       /* GDMA_OUT_PERI_SEL_CH0 */
    movi    a3, 0x3fc80000
    s32i    a3, a2, 0x80       /* GDMA_OUT_LINK_CH0 */

    movi    a2, 0x60041000
    movi    a3, 0x08000000   /* LCD_CAM_LCD_START */
    s32i    a3, a2, 0x14

    /* UART marker after both toy devices have been exercised. */
    movi    a3, 'T'
    call0   putc
    movi    a3, 'O'
    call0   putc
    movi    a3, 'Y'
    call0   putc
    movi    a3, '_'
    call0   putc
    movi    a3, 'G'
    call0   putc
    movi    a3, 'U'
    call0   putc
    movi    a3, 'E'
    call0   putc
    movi    a3, 'S'
    call0   putc
    movi    a3, 'T'
    call0   putc
    movi    a3, ' '
    call0   putc
    movi    a3, 'o'
    call0   putc
    movi    a3, 'n'
    call0   putc
    movi    a3, 'L'
    call0   putc
    movi    a3, 'o'
    call0   putc
    movi    a3, 'o'
    call0   putc
    movi    a3, 'p'
    call0   putc
    movi    a3, ' '
    call0   putc
    movi    a3, 'c'
    call0   putc
    movi    a3, 'o'
    call0   putc
    movi    a3, 'm'
    call0   putc
    movi    a3, 'p'
    call0   putc
    movi    a3, 'l'
    call0   putc
    movi    a3, 'e'
    call0   putc
    movi    a3, 't'
    call0   putc
    movi    a3, 'e'
    call0   putc
    movi    a3, 'd'
    call0   putc
    movi    a3, 10
    call0   putc
    ret
.size toy_main, .-toy_main
