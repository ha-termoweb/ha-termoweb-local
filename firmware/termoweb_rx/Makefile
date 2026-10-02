MCU      = atmega328p
F_CPU    = 16000000UL
PORT     = /dev/ttyUSB0
BAUD     = 115200

SYNC1    = 0x2D
SYNC0    = 0xE5
PA       = 0x50
LEN      = 64

CC       = avr-gcc
OBJCOPY  = avr-objcopy
SIZE     = avr-size
AVRDUDE  = avrdude

CFLAGS   = -mmcu=$(MCU) -DF_CPU=$(F_CPU) -DSYNC1_VAL=$(SYNC1) -DSYNC0_VAL=$(SYNC0) -DPKT_LEN=$(LEN) -DPA_VAL=$(PA) \
           -Os -std=gnu99 -Wall -Wextra -ffunction-sections -fdata-sections
LDFLAGS  = -mmcu=$(MCU) -Wl,--gc-sections

SRC      = main.c cc1101.c
OBJ      = $(SRC:.c=.o)
TARGET   = termoweb_rx

.PHONY: build flash signature clean

build: $(TARGET).hex
	$(SIZE) $(TARGET).elf
	@echo "hex size:"; wc -c < $(TARGET).hex

$(TARGET).elf: $(OBJ)
	$(CC) $(LDFLAGS) $(OBJ) -o $@

$(TARGET).hex: $(TARGET).elf
	$(OBJCOPY) -O ihex -R .eeprom $< $@

%.o: %.c cc1101.h
	$(CC) $(CFLAGS) -c $< -o $@

# The nanoCUL's Nano clone ships with either the classic Arduino bootloader
# (57600 baud) or Optiboot (115200 baud); try both, do not guess.
signature:
	@echo "trying 57600 (classic bootloader)..."
	@avrdude -c arduino -p $(MCU) -P $(PORT) -b 57600 2>&1 | tee /tmp/termoweb_rx_sig.log | grep -q "device signature" \
		&& echo "57600 OK" && exit 0; \
	echo "57600 failed, trying 115200 (Optiboot)..."; \
	avrdude -c arduino -p $(MCU) -P $(PORT) -b 115200

flash: build
	$(AVRDUDE) -c arduino -p $(MCU) -P $(PORT) -b $(BAUD) -D -U flash:w:$(TARGET).hex:i

clean:
	rm -f *.o *.elf *.hex
