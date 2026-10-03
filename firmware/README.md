# firmware

`termoweb_rx`: a dedicated [CC1101](https://www.ti.com/product/CC1101) packet receiver and transmitter for the Sun Ray RF heater link at 869.525 MHz, replacing [culfw](https://github.com/heliflieger/a-culfw) on the [nanoCUL868](https://www.ebay.ie/itm/372221622516). Current version 3.5.

To put a-culfw back on the stick, build it from upstream source (see [Restoring culfw](#restoring-culfw)); no a-culfw binary is distributed here.

The sections below are the firmware's development record, version by version; where they mention `tools/nano_rx.py`, `tools/termoweb_frame.py` or `docs/`, those are development tools and notes from the private research tree that are not part of this repository. The same frame codec ships here as [`custom_components/termoweb_local/vendor/termoweb_local/_vendored_frame.py`](../custom_components/termoweb_local/vendor/termoweb_local/_vendored_frame.py). The user-facing build and flash guide is [docs/firmware.md](../docs/firmware.md).

## Wiring

From a-culfw's own [`culfw/Devices/nanoCUL/board.h`](https://github.com/heliflieger/a-culfw/blob/f4305ea7ca9aba2ace6978c9c29e2645072e5c66/culfw/Devices/nanoCUL/board.h) (fetched from [`heliflieger/a-culfw`](https://github.com/heliflieger/a-culfw), master branch, commit [`f4305ea7`](https://github.com/heliflieger/a-culfw/commit/f4305ea7ca9aba2ace6978c9c29e2645072e5c66)), confirmed byte for byte:

| Signal | AVR pin | Arduino Nano pin |
|---|---|---|
| MOSI | PB3 | D11 |
| MISO | PB4 | D12 |
| SCK | PB5 | D13 |
| CS (manual GPIO, not hardware SS) | PB2 | D10 |
| GDO0 (packet interrupt, INT1) | PD3 | D3 |
| GDO2 (wired, unused by this firmware) | PD2 | D2 |

The board also confirms `HAS_16MHZ_CLOCK`, so the Nano clone runs its normal 16 MHz crystal.

## Build

```
cd firmware/termoweb_rx
make build
```

Needs `avr-gcc`, `avr-objcopy`, `avr-size` and [avr-libc](https://github.com/avrdudes/avr-libc) (no Arduino IDE, no arduino-cli). Produces `termoweb_rx.hex`; the sync word and the fixed packet length are build-time overridable:

```
make build SYNC1=0x2D SYNC0=0xE5 LEN=64
```

Measured (3.1): 2824 bytes text + 262 bytes data in the ELF (3086 bytes flash), 8707-byte Intel HEX, both well under the 32 KB flash.

Measured (3.2): 4494 bytes text + 512 bytes data in the ELF (5006 bytes flash), 792 bytes RAM (data + bss) of 2048 available, 14094-byte Intel HEX; both well under the 32 KB flash and 2 KB RAM. The RAM growth over 3.1 is printf format strings (this firmware uses plain `printf`, not `printf_P`/`PSTR`, so every distinct format string is copied into RAM at boot, same as 3.1 already did for its own strings) plus the new dynamic-length receive buffer (`RX_BUF_LEN` = 257 bytes, sized for the 255-byte length ceiling plus 2 status bytes).

## Flash

The Nano clone's bootloader baud is not known in advance; read the signature only (no write) to find it:

```
avrdude -c arduino -p m328p -P /dev/ttyUSB0 -b 57600   # classic bootloader
avrdude -c arduino -p m328p -P /dev/ttyUSB0 -b 115200  # Optiboot
```

On this stick 57600 timed out (`programmer is not responding`) and 115200 worked (`device signature = 0x1e950f (probably m328p)`), so it runs [Optiboot](https://github.com/Optiboot/optiboot). Then:

```
make flash PORT=/dev/ttyUSB0 BAUD=115200
```

`make flash` reuses whichever baud is set in the Makefile's `BAUD` variable (default 115200); override it on the command line if a different stick needs 57600.

## CC1101 register table

All addresses and purposes are in `firmware/termoweb_rx/cc1101.c`; the arithmetic behind the link-specific ones:

| Register | Value | Reasoning |
|---|---|---|
| FREQ2/1/0 | `0x21 0x71 0xA0` | `round(869.54e6 * 2^16 / 26e6) = 0x2171A0` -> 869.540039 MHz actual, on the nanoCUL's 26 MHz crystal |
| MDMCFG4 | `0x68` | upper nibble: CHANBW_E=1, M=2 -> `26e6/(8*6*2) = 270.8` kHz RX filter BW; lower nibble DRATE_E=8 (culfw HomeMatic's DRATE_E, only the BW nibble changed) |
| MDMCFG3 | `0x83` | DRATE_M=131 with DRATE_E=8 -> `(256+131)*2^8*26e6/2^28 = 9596` bps. The link is 9.6 kbps: the Flipper measured 104 us per bit, and the earlier 0x93 (9.99 kbps) setting duplicated one received bit every 24 to 28 bits. |
| MDMCFG2 | `0x02` | 2-FSK (MOD_FORMAT=000), no Manchester, sync mode 16/16 |
| SYNC1/0 | `0x2D 0xE5` | the real frame starts one bit after the 16-bit pattern 0x16F2 , so the on-air bits shifted by one give 0x2DE5 as the byte-aligned sync word; build-time overridable |
| DEVIATN | `0x50` | DEVIATION_E=5, M=0 -> `26e6/2^17*8*2^5 = 50.8` kHz, matches the measured ~50 kHz |
| PKTLEN | `64` (0x40) | fixed length, build-time overridable via `LEN`; deliberately longer than any single real frame (longest observed is 29 bytes) so a fixed-length capture beginning at one frame's sync still reaches into whatever comes next, for `tools/nano_rx.py` to split back out (see "Packet framing" below) |
| PKTCTRL0 | `0x00` | fixed length mode, CRC off (unknown, so left off rather than rejecting real frames), whitening off (unknown) |
| PKTCTRL1 | `0x04` | APPEND_STATUS on: RSSI and LQI/CRC_OK follow every FIFO read |
| FSCTRL1 | `0x06` | standard IF frequency for this class of data rate |
| FIFOTHR | `0x47` | default FIFO thresholds plus ADC_RETENTION, required by the [CC1101 datasheet](https://www.ti.com/lit/ds/symlink/cc1101.pdf) whenever the RX filter BW is <= 325 kHz |
| TEST2/1/0 | `0x81 0x35 0x09` | same BW<=325kHz requirement; TEST0 also sets VCO_SEL_CAL_EN |
| IOCFG0 | `0x06` | GDO0 asserts on sync detect, deasserts once `PKTLEN` bytes are in -> drives the RX-done interrupt |

Registers not tied to a measured parameter (FOCCFG, BSCFG, AGCCTRL2/0, FREND1/0, FSCAL3/2/1/0, MCSM1/0, FSTEST, MDMCFG1/0, CHANNR, ADDR, FSCTRL0, IOCFG2) use the generic 2-FSK baseline from the widely deployed ELECHOUSE/SmartRC CC1101 driver ([LSatan/SmartRC-CC1101-Driver-Lib](https://github.com/LSatan/SmartRC-CC1101-Driver-Lib), `SmartRC_CC1101.cpp`). That baseline was cross-checked, not assumed: its TEST2/TEST1/TEST0, PKTCTRL1 and FSCTRL1 values match the numbers this link already required from the CC1101 datasheet's own BW<=325kHz rule, which is why it was trusted for the registers the datasheet does not pin to a specific value. SmartRF Studio itself needs Windows and was not available on this host, so this is the best available substitute; if reception is marginal, the rest of the AGC (AGCCTRL2/0, FOCCFG, BSCFG) is the first place to revisit.

## Packet framing: fixed length again, split on the host

Fixed length 40 (the original design) meant every sync detection returned 40 FIFO bytes regardless of the real frame length, and stopped the CC1101 listening 33 ms after sync, which cut off the ack that follows every data frame. Ending the frame dynamically on the CC1101 itself was tried three ways and rejected each time, on this bench, with evidence:

1. **Carrier sense** (infinite packet length, `PKTSTATUS` bit 6 polled against `RXBYTES`): `PKTSTATUS.CS` blips low for a single poll during ordinary 2-FSK mark/space tone changes, which cut real frames short at a handful of bytes.
2. **RXBYTES stall** (end when the FIFO byte count stops growing): never fired. Infinite packet length mode keeps the demodulator clocking bits into the FIFO at the bit rate for as long as the chip stays in RX, real signal or not, so `RXBYTES` never actually stops increasing.
3. **RSSI drop** (end when RSSI reads more than 12 dB below its in-frame peak for 2 consecutive bytes): closer, but still truncated some real frames early (one 29-byte heater frame cut at 16 bytes) since the drop can register before the frame's own trailing bytes are all in.

Given (2), a length-based end condition can never be data-driven on this chip in infinite packet length mode: the FIFO always looks "busy". Given (1) and (3), a signal-quality-based end condition is inherently noisy at 9.6 kbps on this hardware. So framing moved entirely off the CC1101: `PKTCTRL0` is back to fixed length, `PKTLEN` is 64 (comfortably longer than the longest real frame, 29 bytes, so a capture starting at one frame's sync reaches into whatever follows it rather than stopping short), and `tools/nano_rx.py` does the real framing on the host:

- each piece is first trimmed to the length its own first byte implies (`termoweb_frame.frame_length`: on-air byte 0 XOR 0xFF, since the link's keystream always starts with 0xFF) and CRC-checked there;
- it then bit-searches past that length for an embedded sync word (`0x2DE5`, or the same on-air pattern read as `0x16F2` one bit early) to find where the next piece begins, recursively, into as many pieces as it finds.

This trades a data-driven cutoff (which never worked reliably) for a deterministic one (which cannot truncate, since it always reads the same 64 bytes) plus host-side reconstruction using the frame structure already known from `docs/60-protocol-findings.md`.

## Serial protocol (115200 8N1)

- Boot and `V` (3.2): `# termoweb_rx 3.2 freq=869.525 rate=9.6k sync=2DE5 mode=dynamic tx=pa<value>`
- `Q` (3.2): `# Q termoweb_rx 3.2 freq=869.525 pa=<hex> sync=2DE5 mode=dynamic autoack=<on|off> id=<hex>`
- Frame: `RX <micros> <rssi_dbm> <lqi> <crc_ok_bit> <hex bytes, no spaces>`
  - as of 3.2 the hex field is exactly that frame's own on-air length, already trimmed to it (no more fixed 64-byte reads for `tools/nano_rx.py` to split; see "Dynamic framing and auto-ack" below)
  - `rssi_dbm` = signed status byte / 2 - 74, one decimal place
  - `lqi` is bits 6:0 of the second status byte (lower is better)
  - `crc_ok_bit` is bit 7 of the same byte; meaningless here since the CC1101's own hardware CRC is off (it only supports init 0xFFFF, not this link's init 0x1D0F), kept for format compatibility. `tools/nano_rx.py` does the real CRC-16/CCITT check in software, via `termoweb_frame`, the same one CRC for every frame
- `ACK <micros> <hex bytes>` (3.2): an ack this firmware sent under auto-ack (`A1`); see "Dynamic framing and auto-ack" below
- `X`: toggle raw mode, which appends the two raw status bytes as hex to every frame line and prints `# raw=on`/`# raw=off` on toggle
- `A0` / `A1` (3.2): auto-ack off (default) / on
- `I<hex>` (3.2): set our station id (default `01`), used by auto-ack
- `D`: dump `# marcstate=<hex> pktstatus=<hex> rxbytes=<hex> syncs=<count> rxreset=<count>` on demand, for diagnosing a receiver that fell out of RX without waiting for a packet; `syncs` counts every GDO0 rising edge including noise, so it also shows whether the interrupt is firing at all; `rxreset` (3.5) counts how many times the liveness guard has force-recovered a stuck receiver, see "Receive deadlock fix" below
- `# rxreset n=<count>` (3.5): printed unprompted whenever the liveness guard fires, same count as `D`'s `rxreset` field

## Restoring culfw

[a-culfw](https://github.com/heliflieger/a-culfw) ([GPL-2.0-or-later](https://github.com/heliflieger/a-culfw/blob/master/LICENSE)) publishes no binary releases. Build it from source and flash it with the same [avrdude](https://github.com/avrdudes/avrdude) invocation as `make flash`:

- Source: [heliflieger/a-culfw](https://github.com/heliflieger/a-culfw), commit [`f4305ea7ca9aba2ace6978c9c29e2645072e5c66`](https://github.com/heliflieger/a-culfw/commit/f4305ea7ca9aba2ace6978c9c29e2645072e5c66)
- In `culfw/Devices/nanoCUL/`: `make TARGET=nanoCUL868 mostly_clean sizebefore build sizeafter`. With [avr-gcc 10 or newer](https://gcc.gnu.org/gcc-10/porting_to.html) add `-fcommon` to the CFLAGS, because the source relies on tentative definitions in headers.
- Flash: `avrdude -c arduino -p m328p -P /dev/ttyUSB0 -b 115200 -D -U flash:w:nanoCUL868.hex:i`

## Chunked transmit past 64 bytes (3.3)

`T<hex>\n` accepted up to 60 bytes (120 hex characters) through 3.2.1, because `cc1101_transmit` wrote the whole payload into the CC1101's 64-byte TX FIFO in one SPI burst before strobing `STX`; the 99-byte program write (PROTOCOL.md 5.7) and the heater's own 28-byte `E6` reply, both real on-air frames, are within `PKT_LEN`'s reach but past the FIFO's physical size, and could not be sent (`T` with 198 hex characters returned `TXERR empty or bad hex`, since 99 exceeded the old 60-byte `TX_MAX_LEN`).

3.3 raises `TX_MAX_LEN` to 255 (PKTLEN is one byte; 255 is the hard ceiling) and changes `cc1101_transmit` for any `len > 64`: PKTLEN is still set to the exact frame length (fixed length mode, as before), but the FIFO is filled in two stages instead of one. First, 63 bytes go in before `STX` (not 64: the CC1101 has a documented erratum where the TXBYTES status register reads back 0, indistinguishable from empty, when the FIFO is read at exactly full, so the first post-`STX` status read is never taken during that ambiguous instant). Once transmitting, the firmware polls `TXBYTES` (status register `0x3A`, low 7 bits; bit 7 is the underflow flag) and, whenever the FIFO's occupancy drops below 32 bytes, writes another `min(remaining, 64 - occupied - 1)` bytes, until the whole frame has been queued; the final wait for `MARCSTATE` idle is unchanged. `len <= 64` keeps the original single-burst path byte for byte, so the 16- and 17-byte command frames (the ones that must be acked inside the observed 16 ms window) see no new polling and no added latency.

An underflow (the FIFO running dry before the chip has clocked out every queued byte, `MARCSTATE` `0x16`, `TXFIFO_UNDERFLOW`) is checked for both during the refill loop's `TXBYTES` reads and during the idle-wait loop; either way, the firmware strobes `SFTX` to flush and calls `cc1101_enter_rx()` to recover, then reports it as `TXERR underflow`, distinct from the existing `TXERR marcstate=<hex>` reported for any other MARCSTATE that never reached idle.

Limits not verified without hardware: whether the 32-byte refill threshold and 100 us `MARCSTATE` poll interval (unchanged from 3.1/3.2) keep the FIFO fed in time for a 99-byte frame on real hardware without underflowing, given that this main loop is also servicing UART and, if auto-ack is on, decoding received frames; and the actual on-air correctness of a 99-byte `T` command's bytes, since a longer transmit has more SPI transactions than the already-proven-on-air 16- and 17-byte commands. `TX_MAX_LEN`'s bump from 60 to 255 did not need read_hex_line's own logic to change (it already decoded hex pairs one at a time up to a caller-supplied `max`, checked data-driven with `avr-size`: see the RAM budget note below), only the buffer size handed to it.

RAM: raising `TX_MAX_LEN` to 255 would have added up to 255 bytes on `main()`'s own stack frame on top of the existing 257-byte RX snapshot buffer it already allocates once a frame is fully received, had the two stayed as separate stack arrays. Instead, 3.3 makes that 257-byte buffer (`scratch` in `main.c`) a single static array reused for both purposes: it holds the `T` command's decoded TX payload while one is being sent, and the RX snapshot once a frame completes. The two never overlap in time (a `T` command finishes printing its result before the main loop's `packet_ready` check below it runs in the same iteration), and reusing it moved the 257 bytes out of the stack entirely rather than doubling stack pressure. Measured with `avr-size` (`make clean build PA=0xC0`): `.data` 528 + `.bss` 537 = 1065 bytes of static RAM, out of the ATmega328P's 2048; `avr-gcc -fstack-usage` shows `main()`'s own frame at 34 bytes (down from needing 257+ once `tx[TX_MAX_LEN]` would otherwise have been a second stack array), with the deepest call chains (`main` into `cc1101_transmit`/`write_burst`, or `main` into `print_hex`/`printf`) each well under 100 bytes; even a generous allowance for the C library's own `printf` stack use plus interrupt nesting leaves several hundred bytes of headroom under the 983 bytes (2048 - 1065) available for the stack.

## Interrupt-driven UART receive (3.4)

Through 3.3, `uart_getc_nonblock` polled `UCSR0A`/`UDR0` directly, and the hardware receive path behind it is only 2 bytes deep (the UDR0 shift-in plus one buffered byte). On the bench, a `T` command line (30 hex characters, a full setpoint frame) sent while the firmware was still printing a 230-character `RX` line (the 99-byte program-write reply, `RX` header plus 99 hex-pair bytes) was lost or mangled, and the stick answered `TXERR empty or bad hex`: `printf` blocks on `UDRE0` for each output byte, and while it does, incoming host bytes arriving faster than the main loop gets back to polling `UDR0` overran that 2-byte buffer and were dropped before `read_hex_line` ever saw them.

3.4 moves UART receive onto an interrupt (`USART_RX_vect`) filling a 128-byte ring buffer (`uart_rx_buf`, `uart_rx_head`/`uart_rx_tail` as `volatile uint8_t`, wrapped with a mask since 128 is a power of two, not a modulo). The ISR copies `UDR0` into the ring and returns; no SPI, matching the same constraint already on `INT0_vect`/`INT1_vect`, and AVR does not nest interrupts unless an ISR calls `sei()` itself, so `USART_RX_vect` cannot preempt one of the CC1101 ISRs mid-transfer any more than they can preempt each other. `uart_getc_nonblock` and `uart_getc_blocking` now read from the ring instead of `UDR0`; `read_hex_line`'s own logic is unchanged, since it only ever calls `uart_getc_nonblock`. The transmit path stays polling, unchanged from 3.1: 115200 8N1's TX side already keeps up (the ISR is what fixes the RX side's loss window), and a second ring would cost RAM without addressing the failure observed.

`UCSR0B`'s `RXCIE0` bit is enabled at the same point `sei()` is (after `cc1101_init()`/`cc1101_enter_rx()`, same as the 3.2.1 fix below for `EIMSK`), for the same reason: nothing needs to poll the UART during `cc1101_init()`'s fixed sequence of blocking SPI writes, so there is no cost to leaving the interrupt masked until then, and it avoids a receive interrupt landing before `stdout`/`uart_out` and the rest of `main()`'s state exist.

A ring-full byte is dropped (not the oldest queued byte) and sets `uart_rx_overrun`; the main loop checks it once per iteration, ahead of the `uart_getc_nonblock` poll, and reports `# uart overrun` once, clearing the flag under `cli()`/`sei()` so a byte that arrives exactly during the clear is not lost from the report.

Measured with `avr-size` (`make clean build PA=0xC0`): `.data` 544 + `.bss` 668 = 1212 bytes of static RAM, up from 3.3's 528 + 537 = 1065 (+147: 131 bytes for the 128-byte ring plus its head/tail/overrun bytes, 16 bytes for the new `# uart overrun` format string), out of the ATmega328P's 2048; zero compiler warnings (`-Wall -Wextra`).

Limits not verified without hardware: whether 128 bytes is enough ring depth for the worst realistic host burst (a `T` line arriving while the firmware is mid-print of the longest `RX` line, both at 115200 baud, is the scenario that failed; the ring is sized well past that single case but sustained back-to-back host traffic during printing was not bench-measured); and whether `uart_rx_overrun` ever actually latches on real hardware now that the loss window is closed, since it could previously only be inferred from the `TXERR empty or bad hex` symptom, never observed directly.

## Receive deadlock fix (3.5)

Through 3.4, the `T` command's interrupt-masked block cleared `packet_ready` but never touched `rx_state`. If a frame finished landing (`ISR(INT1_vect)` setting `rx_state = RX_DONE` and `packet_ready = 1`) in the window between `EIMSK` masking GDO0/GDO2 and that clear running, the clear left `rx_state` stuck at `RX_DONE` with no path back to `RX_IDLE`: `ISR(INT0_vect)` returns immediately once `rx_state == RX_DONE`, `ISR(INT1_vect)` only acts from `RX_FIXED` or `RX_LENPENDING`, and the main loop's `packet_ready` block, the only other place besides `rx_drain_fifo()` (itself unreachable from `RX_DONE`) that reaches `RX_IDLE`, never ran because `packet_ready` read false. The receiver then stayed silent forever while `T` still worked, recoverable only by the DTR-triggered AVR reset on the next serial port open. Observed live 208 times in the Home Assistant log, roughly every 12 minutes.

3.5 re-arms the whole state machine in that block instead of clearing the one flag: `cc1101_enter_rx()` (`SIDLE`, `SFRX`, `PKTCTRL0` back to infinite length, `SRX`) runs first while GDO0/GDO2 are still masked, flushing any FIFO bytes left over from the discarded frame, then `rx_have` and `rx_state` drop to their idle values, then `packet_ready` clears and `EIFR`/`EIMSK` restore as before. Flushing and re-arming before dropping `rx_state` means those stale FIFO bytes can never be attributed to whatever frame starts next. `cc1101_transmit()` already ends with its own `cc1101_enter_rx()` call on the normal (non-discarding) path, so the one added here is redundant there and load-bearing only on the frame-discarded path, where `cc1101_transmit()` returned without reaching its own call. This is not a new loss: a frame arriving mid-transmit was already discarded under every prior version, it just stops being discarded into a dead receiver.

A second, independent guard covers the same symptom regardless of cause: the main loop now checks, every iteration, whether `rx_state == RX_DONE` while `packet_ready == 0` for longer than `RXRESET_TIMEOUT_US` (250 ms), using `rx_done_micros` (already stamped by `ISR(INT1_vect)` the instant it sets `RX_DONE`) as the clock. If so, it calls `cc1101_enter_rx()`, forces `rx_have = 0` and `rx_state = RX_IDLE` under `cli()`/`sei()`, and prints `# rxreset n=<count>` so a recovery shows up in the Home Assistant log as a counter instead of as silence. 250 ms is well past the normal packet_ready handler's own idle-to-idle window (a worst-case 255-byte auto-ack transmit at 9.6 kbps finishes in a few tens of ms), so it will not fire on a receiver that is merely busy.

Measured with `avr-size` (`make clean build PA=0xC0`): `.data` 570 + `.bss` 668 = 1238 bytes of static RAM, up from 3.4's 544 + 668 = 1212 (+26, the new `# rxreset n=%u` format string and the extra `rxreset=%u` field on the existing `D` dump line), out of the ATmega328P's 2048; flash (`text` + `data`) 5604 bytes, up from 3.4's 5418 (+186), out of 32 KB; zero compiler warnings (`-Wall -Wextra`).

Limits not verified without hardware: whether the liveness guard's 250 ms threshold is long enough to never fire during ordinary operation on real hardware (derived from the auto-ack transmit path's own worst case, not bench-measured against real traffic timing) yet short enough that a wedged receiver's silence is not mistaken for a working one for long; and the fixed `T`-handler deadlock itself, since reproducing it needs a frame timed to land inside the few-microsecond `EIMSK` mask window around a live `T` command, which was not attempted on the bench (the fix follows directly from the state machine's documented transitions, not from a reproduction).

## Known limitations

- 3.3's chunked TX FIFO refill (see "Chunked transmit past 64 bytes" above) has not been bench-tested: the 16- and 17-byte command path is unchanged from the already-proven 3.1 `cc1101_transmit`, but a `T` command past 64 bytes has only been checked by static build (`make clean build`) and `verify_ack.py`'s offline CRC/keystream check, neither of which exercises real SPI timing against the chip's actual TX FIFO drain rate.
- 3.2.1 fix: 3.2 was silent on boot (no banner, no reply to any command) because `sei()` ran before `cc1101_init()`, and the new `ISR(INT0_vect)`/`ISR(INT1_vect)` do SPI transactions of their own; GDO2's power-on default (CHIP_RDYn) toggles during the chip reset inside `cc1101_init()`, and with INT0 already unmasked that could fire an ISR mid SPI-transaction on the same bus, hanging forever in `cs_low()`'s MISO wait. `main()` now enables interrupts only after `cc1101_init()`/`cc1101_enter_rx()` return, clearing `EIFR` first to drop anything that latched while masked.
- 3.2's dynamic length read has not been bench-tested (see "Dynamic framing and auto-ack" above for exactly which parts are unverified); 3.1's fixed 64-byte read remains available by reverting `main.c`/`cc1101.c` if the dynamic path proves unreliable on the bench.
- Hardware CRC is off because the CC1101's own CRC-16 only supports init 0xFFFF, not this link's init 0x1D0F; every sync match is reported, real or not. `tools/nano_rx.py --table` and its per-piece `crc=ok`/`crc=bad`/`crc=n/a` output are the real quality gate now.
- 16-bit hardware sync detection with no bit-error tolerance still fires on band noise; `tools/nano_rx.py` drops RX lines below -95 dBm by default (`--all` keeps them) as a cheap first filter, ahead of framing.
- A piece too short to hold the frame length its own first byte implies is passed through untrimmed with `crc=n/a`: `tools/nano_rx.py` (via `termoweb_frame.frame_length`) has no fixed table to miss, but a genuinely incomplete read at the end of a capture still can't be checked.
- Single frequency: 869.54 MHz is fixed at flash time; retuning means rebuilding with different FREQ2/1/0 or adding a runtime command, neither of which this firmware does.

## Transmit (3.1)

`T<hex>\n` on the serial port sends one fixed-length packet: the chip adds 8 preamble bytes (MDMCFG1 0x42) and the sync word 2D E5, then the given bytes, then returns to receive. Reply `TX <micros> <n> <hex>` or `TXERR <marcstate>`. The packet-done interrupt is masked during transmission so the transmitted bytes are not reported as a reception. Transmit power comes from the PATABLE written at init: `make flash PA=0x50` for 0 dBm (default), `PA=0xC0` for +10 dBm (868 MHz values from the CC1101 datasheet); the banner shows `tx=pa<value>`. The carrier is 869.525 MHz (FREQ 0x21717A), the value printed on the gateway label; the banner reports it. First live test 2026-09-06: at +10 dBm with the 8-byte preamble the master bedroom heater acked setpoint commands within 65 ms and reported them to the cloud; one frame at 0 dBm with a 4-byte preamble at 869.540 MHz got no ack, the cause was not isolated. Keep transmissions to single frames on demand; the [869.4 to 869.65 MHz sub-band](../README.md#radio-and-spectrum-notice) allows 500 mW ERP at 10 percent duty, so this is far inside the limit, but never loop.

## Dynamic framing and auto-ack (3.2)

3.2 replaces the fixed 64-byte read with a per-frame length read off the frame's own first byte, so a real frame is never truncated (a 99-byte gateway program write and a 28-byte reply that followed another frame in one read were both cut short under the old fixed-64 scheme) and `tools/nano_rx.py` no longer needs to bit-search and split a read into pieces.

How it works: the CC1101 starts each reception in infinite packet length mode (`PKTCTRL0` `LENGTH_CONFIG=2`), because the true length is not knowable until the frame's own first byte arrives. `FIFOTHR` is set to a 4-byte RX threshold and GDO2 (`IOCFG2`, wired to `INT0`) is configured to interrupt on that threshold, so the firmware gets the first bytes as early as possible. Once byte 0 is in, `total = (byte0 XOR 0xFF) + 3` gives the frame's exact on-air length (the keystream's own first byte is always `0xFF`, so no descrambling is needed for this), and the chip is switched to fixed length mode with `PKTLEN=total` (`cc1101_set_fixed_length` in `cc1101.c`). The chip's own received-byte count keeps counting through that switch, so GDO0 still deasserts exactly at the true end of the frame; this is the standard CC1101 technique for a length the radio cannot see in advance. A total under 8 or over 255 is rejected and the receiver resyncs (`SIDLE`/`SFRX`/re-arm). The same `INT0` threshold interrupt keeps draining the FIFO in small chunks for the rest of the frame, which is what lets a frame past the 64-byte RX FIFO capacity (the 99-byte case) be read without a FIFO overflow; a hardware overflow flag is also checked defensively and forces a resync if it ever fires.

`Q`: prints firmware version, frequency, PA value, sync word, framing mode, auto-ack state and our station id on one line, for `tools/nano_rx.py` to log.

`A0` / `A1`: auto-ack off (default) / on. `I<hex>`: sets our station id (default `01`).

When auto-ack is on, bytes 3 to 5 of a fully received frame (source, destination, flags) are descrambled with the link's fixed keystream (`termoweb_rx` only needs the first 8 keystream bytes, `FF 87 B8 59 B7 A1 CC 24`, for this and for scrambling the ack it sends; PROTOCOL.md section 3). If the destination is our own id and the flags byte is `00` (a data frame, not itself an ack), an 8-byte ack is built (CRC-16/CCITT, init `0x1D0F`, poly `0x1021`, final XOR `0xFFFF`, over the 6 logical bytes, then XORed with the keystream; PROTOCOL.md section 5.5) and sent immediately, ahead of the slower UART print of the `RX` line, so it goes out within a few milliseconds rather than after the host has even seen the frame. A sent ack is reported as `ACK <micros> <hex bytes>`.

Limits not verified without hardware: whether the infinite-to-fixed length switch reliably lands before the chip's internal byte counter reaches the new `PKTLEN` for every real frame length seen so far (spec-derived from the CC1101 datasheet's documented behaviour, not bench-tested, since a logger is currently using the only nanoCUL on hand); whether the 4-byte FIFO threshold and the chunked `INT0` drain keep up with a 99-byte frame on real hardware without an overflow; and the actual ack latency in milliseconds once transmitted for real (the code path reuses the already-proven `cc1101_transmit`, but the "before the UART print" ordering has not been measured on the bench). The `build_ack`/CRC logic itself is verified offline against `termoweb_frame.build_ack` by `firmware/termoweb_rx/verify_ack.py` (a Python port of the AVR C, no `make test` host build target added since the C is not structured as a separable library), which is the part that does not need hardware to check: `python3 firmware/termoweb_rx/verify_ack.py`.
