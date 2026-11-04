ENTRY(_start)
MEMORY
{
  IRAM (rx) : ORIGIN = 0x40370000, LENGTH = 0x00080000
}
SECTIONS
{
  .text : ALIGN(4)
  {
    *(.literal .literal.*)
    *(.text .text.*)
  } > IRAM
}
