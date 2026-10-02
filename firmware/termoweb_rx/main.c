/* termoweb_rx: dedicated CC1101 packet receiver for the Termoweb 869.54 MHz
 * heater link, running on a nanoCUL868 (Arduino Nano clone, ATmega328P).
 *
 * Serial protocol (115200 8N1):
 *   boot / 'V' -> "# termoweb_rx <version> freq=869.525 rate=9.6k sync=2DE5 mode=dynamic tx=pa<value>"
 *   'Q'        -> "# Q termoweb_rx <version> freq=... pa=... sync=... mode=dynamic autoack=<on/off> id=<hex>"
 *   'X'        -> toggle raw mode (appends the 2 raw status bytes as hex)
 *   'A0'/'A1'  -> auto-ack off/on (default off)
 *   'I<hex>'   -> set our station id (default 01), used by auto-ack
 *   'T<hex>'   -> transmit one frame, up to 255 bytes since 3.3 (chunked TX
 *                 FIFO refill past 64 bytes; TXERR underflow on a FIFO underrun)
 *   received frame -> "RX <micros> <rssi_dbm> <lqi> <crc_ok_bit> <hex bytes>"
 *   sent ack        -> "ACK <micros> <hex bytes>"
 *
 * Framing (3.2): each frame's total on-air length is read off its own first
 * byte the instant that byte lands in the RX FIFO (total = (byte0 ^ 0xFF) +
 * 3, since the link's keystream's own first byte is always 0xFF), then the
 * CC1101 is switched from infinite packet length mode into fixed length
 * mode with that total, mid-reception (cc1101_set_fixed_length). This ends
 * the packet exactly on its real boundary instead of always reading a fixed
 * 64 bytes, so nano_rx.py no longer needs to split multiple frames back out
 * of one read, and a frame past the old 64-byte cap (a 99-byte program
 * write was observed on air) is no longer truncated. See firmware/README.md
 * and cc1101.h's comment on cc1101_set_fixed_length for why switching modes
 * mid-reception does not disturb the byte count the chip is already
 * tracking.
 *
 * Auto-ack (off by default, 'A1' to enable): once a frame's total is known
 * and it is fully received, bytes 3-5 (source, destination, flags) are
 * descrambled with the link's fixed keystream; if the destination is our
 * own station id and the flags byte is 0x00 (data, not itself an ack), an
 * 8-byte ack is built and sent within a few milliseconds, ahead of the host,
 * which only sees the frame after the slower UART print. PROTOCOL.md
 * section 5.5.
 */
#include <avr/interrupt.h>
#include <avr/io.h>
#include <stdio.h>
#include <string.h>
#include <util/delay.h>
#include "cc1101.h"

#define VERSION "3.5"

/* Longest frame observed so far is a 99-byte gateway program write; 255 is
 * the hard ceiling (PKTLEN is one byte), so the buffer is sized for the
 * worst case plus the 2 appended status bytes. */
#define RX_TOTAL_MIN 8
#define RX_TOTAL_MAX 255
#define RX_BUF_LEN (RX_TOTAL_MAX + 2)

/* 3.5: main loop poll interval for the rx_state liveness guard (see its use
 * below). 250 ms is comfortably past the couple of milliseconds the normal
 * packet_ready handler ever spends between clearing packet_ready and
 * returning rx_state to RX_IDLE (an auto-ack transmit is the slowest thing
 * in that window, and even a 255-byte one finishes in well under 250 ms at
 * 9.6 kbps), so it will not fire on a receiver that is merely busy. */
#define RXRESET_TIMEOUT_US 250000UL

enum {
    RX_IDLE = 0,   /* nothing received since the last full frame */
    RX_LENPENDING, /* sync seen, waiting for byte 0 to learn the total */
    RX_FIXED,      /* total known, chip switched to fixed length mode */
    RX_DONE,       /* full frame + status bytes captured; main loop owns rx_buf until it clears this */
};

/* Shared by the 'T' command's TX payload and the packet_ready block's RX
 * snapshot (main.c's own "local" copy of rx_buf): the two never overlap in
 * time within one main-loop iteration (a 'T' finishes printing before the
 * packet_ready check below it runs), so one 257-byte static buffer covers
 * both instead of two, which is what keeps the 3.3 TX_MAX_LEN bump from
 * growing main()'s stack frame (see firmware/README.md's 3.3 RAM budget). */
static uint8_t scratch[RX_BUF_LEN];

static volatile uint8_t rx_buf[RX_BUF_LEN];
static volatile uint16_t rx_have = 0;
static volatile uint16_t rx_total = 0;
static volatile uint8_t rx_state = RX_IDLE;
static volatile uint32_t rx_done_micros = 0;
static volatile uint8_t packet_ready = 0;
static volatile uint32_t timer0_overflow_count = 0;
static volatile uint16_t sync_count = 0; /* diagnostic: INT1 firings, incl. noise */

static volatile uint8_t auto_ack = 0;
static volatile uint8_t our_id = 0x01;

/* PROTOCOL.md section 3: the LFSR keystream, seed 0xFF, is identical for
 * every frame; only the first 8 bytes are ever needed here (bytes 3-5 to
 * read a received frame's addressing, bytes 0-7 to scramble the 8-byte ack
 * this firmware sends), so a full 255-byte table is not worth the flash. */
static const uint8_t KEYSTREAM[8] = {0xFF, 0x87, 0xB8, 0x59, 0xB7, 0xA1, 0xCC, 0x24};
static const uint8_t NETWORK_ID[2] = {0x1B, 0x30};

static uint32_t micros(void);

/* Reads the current RX FIFO occupancy and copies whatever fits into
 * rx_buf, then, once byte 0 is in, computes the frame's total length and
 * switches the chip to fixed length mode. Recovers from a hardware FIFO
 * overflow (chunked draining is meant to prevent this, but the check costs
 * nothing) by flushing and re-arming rather than trusting bytes that may
 * already be corrupt. */
static void rx_drain_fifo(void) {
    uint8_t status = cc1101_read_status(CC1101_RXBYTES);
    if (status & 0x80) {
        cc1101_strobe(CC1101_SIDLE);
        cc1101_strobe(CC1101_SFRX);
        cc1101_enter_rx();
        rx_have = 0;
        rx_state = RX_IDLE;
        return;
    }
    uint8_t n = status & 0x7F;
    if (n == 0) {
        return;
    }
    uint16_t room = RX_BUF_LEN - rx_have;
    uint8_t to_read = (n > room) ? (uint8_t)room : n;
    if (to_read > 0) {
        cc1101_read_burst(CC1101_FIFO_ADDR, (uint8_t *)&rx_buf[rx_have], to_read);
        rx_have += to_read;
    }
    if (rx_state == RX_LENPENDING && rx_have >= 1) {
        uint8_t first = rx_buf[0];
        uint16_t total = (uint16_t)(first ^ 0xFF) + 3;
        if (total < RX_TOTAL_MIN || total > RX_TOTAL_MAX) {
            cc1101_strobe(CC1101_SIDLE);
            cc1101_strobe(CC1101_SFRX);
            cc1101_enter_rx();
            rx_have = 0;
            rx_state = RX_IDLE;
            return;
        }
        rx_total = total;
        cc1101_set_fixed_length((uint8_t)total);
        rx_state = RX_FIXED;
    }
}

ISR(INT0_vect) {
    /* GDO2: RX FIFO at/above the 4-byte FIFOTHR. Fires repeatedly while a
     * frame comes in, draining it in small chunks well ahead of the
     * 64-byte RX FIFO filling up, which is what lets a frame past 64
     * bytes (e.g. the 99-byte program write) be read without overflow.
     * Skipped while RX_DONE: the previous frame is still in rx_buf and
     * main loop hasn't released it yet, so new bytes are deliberately
     * left sitting in the hardware FIFO (safe for tens of ms, well past
     * any UART print) rather than overwriting data still being read out. */
    if (rx_state == RX_DONE) {
        return;
    }
    if (rx_state == RX_IDLE) {
        rx_have = 0;
        rx_state = RX_LENPENDING;
    }
    rx_drain_fifo();
}

ISR(INT1_vect) {
    /* GDO0: asserts on sync, deasserts at the true end of packet once
     * cc1101_set_fixed_length has switched PKTLEN in; only the falling
     * (packet-done) edge is enabled (see gdo_init), as in 3.1. */
    sync_count++;
    if (rx_state == RX_FIXED || rx_state == RX_LENPENDING) {
        rx_drain_fifo();
        rx_done_micros = micros();
        rx_state = RX_DONE;
        packet_ready = 1;
    }
    cc1101_enter_rx();
}

ISR(TIMER0_OVF_vect) {
    timer0_overflow_count++;
}

/* Same construction as the Arduino core's micros(): timer0 free-runs at
 * F_CPU/64, so each tick is 4 us at 16 MHz and the overflow count supplies
 * the missing high bits. */
static uint32_t micros(void) {
    uint8_t sreg = SREG;
    cli();
    uint32_t ovf = timer0_overflow_count;
    uint8_t cnt = TCNT0;
    if ((TIFR0 & (1 << TOV0)) && cnt < 255) {
        ovf++;
    }
    SREG = sreg;
    return (ovf << 8 | cnt) * 4UL;
}

static void timer0_init(void) {
    TCCR0A = 0;
    TCCR0B = (1 << CS01) | (1 << CS00); /* prescaler 64 */
    TIMSK0 = (1 << TOIE0);
}

static void gdo_init(void) {
    DDRD &= ~((1 << PD3) | (1 << PD2));
    /* INT1 (GDO0): falling edge only, the packet-done signal (unchanged from 3.1). */
    EICRA |= (1 << ISC11);
    EICRA &= ~(1 << ISC10);
    /* INT0 (GDO2): rising edge, the RX-FIFO-at-threshold signal. */
    EICRA |= (1 << ISC01) | (1 << ISC00);
    EIMSK |= (1 << INT1) | (1 << INT0);
}

/* 3.4: a 128-byte power-of-two ring so the index wrap is a mask, not a
 * modulo, keeping USART_RX_vect short enough to stay safe next to the
 * CC1101's own INT0/INT1 ISRs (AVR doesn't nest interrupts unless an ISR
 * calls sei() itself, which none of these three do, but a long RX ISR would
 * still delay GDO0/GDO2 servicing). Sized well past the 30-hex-char (15-byte)
 * 'T' command line that was measured lost during a 230-character RX print in
 * 3.3: uart_getc_nonblock polled UDR0 directly, so bytes that arrived while
 * printf was blocked on a full transmit buffer overran the UART's own 2-deep
 * hardware receive buffer and were dropped before the main loop ever polled. */
#define UART_RX_BUF_LEN 128
static volatile uint8_t uart_rx_buf[UART_RX_BUF_LEN];
static volatile uint8_t uart_rx_head = 0; /* next slot USART_RX_vect will write */
static volatile uint8_t uart_rx_tail = 0; /* next slot uart_getc_nonblock will read */
static volatile uint8_t uart_rx_overrun = 0;

ISR(USART_RX_vect) {
    uint8_t c = UDR0;
    uint8_t head = uart_rx_head;
    uint8_t next = (uint8_t)(head + 1) & (UART_RX_BUF_LEN - 1);
    if (next == uart_rx_tail) {
        /* Ring full: drop the new byte rather than the oldest unread one,
         * and flag it once for the main loop to report. */
        uart_rx_overrun = 1;
        return;
    }
    uart_rx_buf[head] = c;
    uart_rx_head = next;
}

static void uart_init(void) {
    /* 115200 at 16 MHz: U2X0 gives UBRR=16 (117647 actual, +2.1%), the
     * closer of the two divisors avr-libc's util/setbaud.h would offer. */
    UBRR0H = 0;
    UBRR0L = 16;
    UCSR0A = (1 << U2X0);
    UCSR0B = (1 << RXEN0) | (1 << TXEN0);
    UCSR0C = (1 << UCSZ01) | (1 << UCSZ00);
    /* RXCIE0 is deliberately left off here: it is set in main() at the same
     * point sei() runs, after cc1101_init(), for the same reason 3.2.1 delays
     * sei() itself (see main()'s comment above cc1101_init()). */
}

static void uart_putc(char c) {
    while (!(UCSR0A & (1 << UDRE0))) {
    }
    UDR0 = c;
}

static int uart_putc_stream(char c, FILE *f) {
    (void)f;
    if (c == '\n') {
        uart_putc('\r');
    }
    uart_putc(c);
    return 0;
}

static FILE uart_out = FDEV_SETUP_STREAM(uart_putc_stream, NULL, _FDEV_SETUP_WRITE);

/* Reads from the ring USART_RX_vect fills, not UDR0 directly (3.4): a single
 * volatile uint8_t compare/read needs no cli()/sei() guard on this single
 * core AVR, since the ISR only ever advances uart_rx_head, never tail. */
static uint8_t uart_getc_nonblock(char *c) {
    uint8_t tail = uart_rx_tail;
    if (tail == uart_rx_head) {
        return 0;
    }
    *c = (char)uart_rx_buf[tail];
    uart_rx_tail = (uint8_t)(tail + 1) & (UART_RX_BUF_LEN - 1);
    return 1;
}

static uint8_t uart_getc_blocking(char *c, uint32_t timeout_us) {
    uint32_t start = micros();
    while (!uart_getc_nonblock(c)) {
        if (micros() - start > timeout_us) {
            return 0;
        }
    }
    return 1;
}

static void print_banner(void) {
    printf("# termoweb_rx %s freq=869.525 rate=9.6k sync=%02X%02X mode=dynamic tx=pa%02X\n",
           VERSION, SYNC1_VAL, SYNC0_VAL, PA_VAL);
}

static void print_query(void) {
    printf("# Q termoweb_rx %s freq=869.525 pa=%02X sync=%02X%02X mode=dynamic autoack=%s id=%02X\n",
           VERSION, PA_VAL, SYNC1_VAL, SYNC0_VAL, auto_ack ? "on" : "off", our_id);
}

static int8_t hex_nibble(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

/* Reads hex pairs after the command letter until newline; blocks for at most ~1 s. */
static uint8_t read_hex_line(uint8_t *out, uint8_t max) {
    uint8_t n = 0;
    int8_t hi = -1;
    uint32_t start = micros();
    for (;;) {
        char c;
        if (!uart_getc_nonblock(&c)) {
            if (micros() - start > 1000000UL) return 0;
            continue;
        }
        if (c == '\n' || c == '\r') {
            return hi < 0 ? n : 0;
        }
        int8_t v = hex_nibble(c);
        if (v < 0) return 0;
        if (hi < 0) {
            hi = v;
        } else {
            if (n >= max) return 0;
            out[n++] = (uint8_t)(hi << 4) | (uint8_t)v;
            hi = -1;
        }
    }
}

static void print_hex(const uint8_t *buf, uint8_t len) {
    for (uint8_t i = 0; i < len; i++) {
        printf("%02X", buf[i]);
    }
}

/* CRC-16/CCITT, poly 0x1021, init 0x1D0F, final XOR 0xFFFF: PROTOCOL.md
 * section 3, identical to termoweb_frame.crc16. */
static uint16_t crc16(const uint8_t *data, uint8_t len) {
    uint16_t crc = 0x1D0F;
    for (uint8_t i = 0; i < len; i++) {
        crc ^= (uint16_t)data[i] << 8;
        for (uint8_t b = 0; b < 8; b++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
        }
    }
    return crc ^ 0xFFFF;
}

/* On-air bytes for the 8-byte ack (PROTOCOL.md 5.5, termoweb_frame.build_ack):
 * logical 05 1B 30 <acker id> <acked frame's sender id> 80, CRC, then XORed
 * with the keystream. */
static void build_ack(uint8_t acker_id, uint8_t src_id, uint8_t out[8]) {
    uint8_t logical[8] = {0x05, NETWORK_ID[0], NETWORK_ID[1], acker_id, src_id, 0x80, 0, 0};
    uint16_t crc = crc16(logical, 6);
    logical[6] = (uint8_t)(crc >> 8);
    logical[7] = (uint8_t)crc;
    for (uint8_t i = 0; i < 8; i++) {
        out[i] = logical[i] ^ KEYSTREAM[i];
    }
}

int main(void) {
    uart_init();
    stdout = &uart_out;
    timer0_init();
    gdo_init();

    /* Both ISRs do SPI transactions (dynamic-length framing), so interrupts
     * stay masked until the CC1101 is configured: GDO2 toggles CHIP_RDYn
     * during SRES and a premature INT0 mid-transfer deadlocked cs_low(). */
    cc1101_init();
    print_banner();
    cc1101_enter_rx();
    EIFR |= (1 << INTF0) | (1 << INTF1);
    UCSR0B |= (1 << RXCIE0);
    sei();

    uint8_t raw_mode = 0;
    uint16_t rxreset_count = 0; /* 3.5: counts the liveness guard below, exposed in 'D' */

    for (;;) {
        if (uart_rx_overrun) {
            cli();
            uart_rx_overrun = 0;
            sei();
            printf("# uart overrun\n");
        }

        char c;
        if (uart_getc_nonblock(&c)) {
            if (c == 'V') {
                print_banner();
            } else if (c == 'Q') {
                print_query();
            } else if (c == 'X') {
                raw_mode = !raw_mode;
                printf("# raw=%s\n", raw_mode ? "on" : "off");
            } else if (c == 'A') {
                char mode;
                if (uart_getc_blocking(&mode, 1000000UL) && (mode == '0' || mode == '1')) {
                    auto_ack = (mode == '1');
                    printf("# autoack=%s\n", auto_ack ? "on" : "off");
                } else {
                    printf("# A? expected 0 or 1\n");
                }
            } else if (c == 'I') {
                uint8_t idbuf[1];
                if (read_hex_line(idbuf, 1) == 1) {
                    our_id = idbuf[0];
                    printf("# id=%02X\n", our_id);
                } else {
                    printf("# I? expected 2 hex digits\n");
                }
            } else if (c == 'T') {
                uint8_t n = read_hex_line(scratch, TX_MAX_LEN);
                if (n == 0) {
                    printf("TXERR empty or bad hex\n");
                } else {
                    EIMSK &= ~((1 << INT0) | (1 << INT1));
                    uint8_t err = cc1101_transmit(scratch, n);
                    /* 3.5: a frame that finished landing in the window
                     * between the EIMSK mask above and here already ran
                     * ISR(INT1_vect) to completion, setting rx_state =
                     * RX_DONE and packet_ready = 1 before this code even
                     * started transmitting. Clearing only packet_ready (as
                     * every version through 3.4 did) left rx_state stuck at
                     * RX_DONE forever: ISR(INT0_vect) returns immediately
                     * once rx_state == RX_DONE, ISR(INT1_vect) only acts from
                     * RX_FIXED or RX_LENPENDING, and the packet_ready block
                     * below (the only other place that reaches RX_IDLE
                     * outside rx_drain_fifo(), itself unreachable from
                     * RX_DONE) never runs because packet_ready reads false.
                     * Confirmed live: 208 silent-receiver events in the HA
                     * log, about every 12 minutes, recovered only by the
                     * DTR-triggered AVR reset on the next port open.
                     * cc1101_transmit() already ends with cc1101_enter_rx()
                     * on its own success path, so the call below is a no-op
                     * there; it is load-bearing only on the discarded-frame
                     * path above, where cc1101_transmit() returned without
                     * ever reaching its own cc1101_enter_rx(). Flushing the
                     * FIFO and re-arming before dropping rx_state, rather
                     * than after, means any bytes still sitting in the FIFO
                     * from that discarded frame are gone before RX_IDLE
                     * makes the state machine willing to start a new one, so
                     * they can never be attributed to the next frame. */
                    cc1101_enter_rx();
                    rx_have = 0;
                    rx_state = RX_IDLE;
                    packet_ready = 0;
                    EIFR |= (1 << INTF0) | (1 << INTF1);
                    EIMSK |= (1 << INT0) | (1 << INT1);
                    if (err == CC1101_TX_UNDERFLOW) {
                        printf("TXERR underflow\n");
                    } else if (err) {
                        printf("TXERR marcstate=%02X\n", err);
                    } else {
                        printf("TX %lu %u ", micros(), n);
                        print_hex(scratch, n);
                        printf("\n");
                    }
                }
            } else if (c == 'D') {
                /* On-demand chip state dump, for diagnosing a receiver
                 * that fell out of RX without needing a packet to trigger
                 * a read. */
                uint8_t marcstate = cc1101_read_status(CC1101_MARCSTATE);
                uint8_t pktstatus = cc1101_read_status(CC1101_PKTSTATUS);
                uint8_t rxbytes = cc1101_read_status(CC1101_RXBYTES);
                printf("# marcstate=%02X pktstatus=%02X rxbytes=%02X syncs=%u rxreset=%u\n",
                       marcstate, pktstatus, rxbytes, sync_count, rxreset_count);
            }
        }

        if (packet_ready) {
            packet_ready = 0;
            uint16_t total = rx_total;
            uint16_t have = rx_have;
            uint32_t t = rx_done_micros;
            uint8_t ack_sent = 0, ack[8];

            /* Ack decision and transmit happen first, straight off rx_buf,
             * ahead of the slower UART print below, so the ack goes out
             * within a few ms of the frame ending rather than after it. */
            if (auto_ack && have >= 6) {
                uint8_t src = rx_buf[3] ^ KEYSTREAM[3];
                uint8_t dst = rx_buf[4] ^ KEYSTREAM[4];
                uint8_t flags = rx_buf[5] ^ KEYSTREAM[5];
                if (dst == our_id && flags == 0x00) {
                    build_ack(our_id, src, ack);
                    EIMSK &= ~((1 << INT0) | (1 << INT1));
                    uint8_t err = cc1101_transmit(ack, 8);
                    EIFR |= (1 << INTF0) | (1 << INTF1);
                    EIMSK |= (1 << INT0) | (1 << INT1);
                    ack_sent = !err;
                }
            }

            /* Snapshot the frame bytes into scratch before releasing
             * rx_state back to RX_IDLE: the moment that happens, a new frame
             * is free to start overwriting rx_buf. Any 'T' command's use of
             * scratch as its TX payload is already finished by this point
             * (see scratch's declaration comment above). */
            uint16_t local_have = have > RX_BUF_LEN ? RX_BUF_LEN : have;
            memcpy(scratch, (const void *)rx_buf, local_have);

            cli();
            rx_have = 0;
            rx_state = RX_IDLE;
            rx_drain_fifo(); /* catch up on anything the RX_DONE gate held back */
            sei();

            if (ack_sent) {
                printf("ACK %lu ", micros());
                print_hex(ack, 8);
                printf("\n");
            }

            uint8_t rssi_raw = 0, lqi_raw = 0;
            if (local_have >= (uint16_t)total + 2) {
                rssi_raw = scratch[total];
                lqi_raw = scratch[total + 1];
            }
            int8_t rssi_signed = (int8_t)rssi_raw;
            int16_t rssi_dbm10 = (int16_t)rssi_signed * 5 - 740; /* dBm x10 */
            int16_t rssi_mag = rssi_dbm10 < 0 ? -rssi_dbm10 : rssi_dbm10;
            uint8_t lqi = lqi_raw & 0x7F;
            /* Meaningless while hardware CRC is off (kept for format
             * compatibility); nano_rx.py does the real CRC check in
             * software with the link's actual, non-default init. */
            uint8_t crc_ok = (lqi_raw >> 7) & 1;
            uint8_t frame_len = (uint8_t)(total > 255 ? 255 : total);
            if (frame_len > local_have) {
                frame_len = (uint8_t)local_have; /* truncated read; report what we actually have */
            }

            printf("RX %lu %s%d.%d %u %u ", t, rssi_dbm10 < 0 ? "-" : "",
                   rssi_mag / 10, rssi_mag % 10, lqi, crc_ok);
            print_hex(scratch, frame_len);
            if (raw_mode) {
                printf(" %02X%02X", rssi_raw, lqi_raw);
            }
            printf("\n");
        }

        /* 3.5: last-resort recovery for rx_state stuck at RX_DONE with
         * packet_ready false, the deadlock the 'T' handler fix above closes
         * for its own cause but which this guard covers regardless of cause
         * (e.g. a future code path with the same clear-the-flag mistake).
         * rx_state and packet_ready are read together under cli()/sei(),
         * same as every other multi-field access to this pair in the file,
         * so a mid-read ISR firing cannot show a combination that never
         * really existed. rx_done_micros is the timestamp ISR(INT1_vect)
         * already stamps at the same instant it sets RX_DONE, so it is
         * exactly the "how long has it been stuck" clock this needs with no
         * extra state. */
        uint8_t stuck;
        uint32_t stuck_since;
        cli();
        stuck = (rx_state == RX_DONE) && !packet_ready;
        stuck_since = rx_done_micros;
        sei();
        if (stuck && (micros() - stuck_since) > RXRESET_TIMEOUT_US) {
            cc1101_enter_rx();
            cli();
            rx_have = 0;
            rx_state = RX_IDLE;
            sei();
            rxreset_count++;
            printf("# rxreset n=%u\n", rxreset_count);
        }
    }
}
