/* CC1101 SPI driver and register configuration for the Termoweb 869.54 MHz link.
 *
 * Wiring (nanoCUL868, a-culfw board.h: Devices/nanoCUL/board.h):
 *   MOSI  PB3 / D11
 *   MISO  PB4 / D12
 *   SCK   PB5 / D13
 *   CS    PB2 / D10  (manual GPIO, not the AVR hardware SS pin function)
 *   GDO0  PD3 / D3   (INT1, packet-done interrupt)
 *   GDO2  PD2 / D2   (wired, unused by this firmware)
 */
#ifndef CC1101_H
#define CC1101_H

#include <stdint.h>

/* Build-time overridable link parameters. SYNC defaults to 0x2DE5: the real
 * frame starts one bit after the 16-bit pattern 0x16F2 (docs/60), so the
 * on-air bits shifted by one give 0x2DE5 as the byte-aligned sync word.
 * PKT_LEN was the fixed CC1101 packet length in firmware 3.1 (every sync
 * match read exactly this many bytes, since every CC1101-side end-of-packet
 * method tried on this link either never triggered or triggered mid-packet;
 * see firmware/README.md). 3.2 ends each packet dynamically instead (see
 * cc1101_set_fixed_length in cc1101.c and main.c's rx_service), so PKT_LEN
 * is now only the chip's PKTLEN register value between receptions, while
 * the chip is in infinite-length mode and PKTLEN is a don't-care; kept
 * build-time overridable for Makefile/README compatibility. */
#ifndef SYNC1_VAL
#define SYNC1_VAL 0x2D
#endif
#ifndef SYNC0_VAL
#define SYNC0_VAL 0xE5
#endif
#ifndef PKT_LEN
#define PKT_LEN 64
#endif

/* TX power: PATABLE entry for 868 MHz per the CC1101 datasheet, 0x50 = 0 dBm, 0xC0 = +10 dBm. */
#ifndef PA_VAL
#define PA_VAL 0x50
#endif
/* 255 is the hard ceiling (PKTLEN is one byte); the longest real on-air
 * frame seen so far is the 99-byte program write (docs/PROTOCOL.md 5.7).
 * cc1101_transmit chunks the TX FIFO refill for any len > 64 (3.3). */
#define TX_MAX_LEN 255

/* Config register addresses used below (CC1101 datasheet Table 45). */
#define CC1101_IOCFG2   0x00
#define CC1101_IOCFG0   0x02
#define CC1101_FIFOTHR  0x03
#define CC1101_SYNC1    0x04
#define CC1101_SYNC0    0x05
#define CC1101_PKTLEN   0x06
#define CC1101_PKTCTRL1 0x07
#define CC1101_PKTCTRL0 0x08
#define CC1101_ADDR     0x09
#define CC1101_CHANNR   0x0A
#define CC1101_FSCTRL1  0x0B
#define CC1101_FSCTRL0  0x0C
#define CC1101_FREQ2    0x0D
#define CC1101_FREQ1    0x0E
#define CC1101_FREQ0    0x0F
#define CC1101_MDMCFG4  0x10
#define CC1101_MDMCFG3  0x11
#define CC1101_MDMCFG2  0x12
#define CC1101_MDMCFG1  0x13
#define CC1101_MDMCFG0  0x14
#define CC1101_DEVIATN  0x15
#define CC1101_MCSM1    0x17
#define CC1101_MCSM0    0x18
#define CC1101_FOCCFG   0x19
#define CC1101_BSCFG    0x1A
#define CC1101_AGCCTRL2 0x1B
#define CC1101_AGCCTRL1 0x1C
#define CC1101_AGCCTRL0 0x1D
#define CC1101_FREND1   0x21
#define CC1101_FREND0   0x22
#define CC1101_FSCAL3   0x23
#define CC1101_FSCAL2   0x24
#define CC1101_FSCAL1   0x25
#define CC1101_FSCAL0   0x26
#define CC1101_FSTEST   0x29
#define CC1101_TEST2    0x2C
#define CC1101_TEST1    0x2D
#define CC1101_TEST0    0x2E

/* Status registers, each read individually with cc1101_read_status (the
 * burst bit CC1101 status reads require does NOT auto-increment through
 * addresses the way config register bursts do; it just re-reads the same
 * register for every clocked byte, so these cannot be batched into one
 * multi-byte burst transaction). */
#define CC1101_LQI       0x33
#define CC1101_RSSI      0x34
#define CC1101_MARCSTATE 0x35
#define CC1101_PKTSTATUS 0x38
#define CC1101_TXBYTES   0x3A
#define CC1101_RXBYTES   0x3B

/* Command strobes. */
#define CC1101_SRES  0x30
#define CC1101_SCAL  0x33
#define CC1101_SRX   0x34
#define CC1101_SIDLE 0x36
#define CC1101_STX   0x35
#define CC1101_SFRX  0x3A
#define CC1101_SFTX  0x3B
#define CC1101_PATABLE 0x3E

/* SPI header bits. */
#define CC1101_WRITE_BURST 0x40
#define CC1101_READ_SINGLE 0x80
#define CC1101_READ_BURST  0xC0
#define CC1101_FIFO_ADDR   0x3F

void cc1101_init(void);
/* Re-arms the chip for the next reception in infinite packet length mode
 * (PKTCTRL0 LENGTH_CONFIG=2): the true frame length is not known until its
 * first byte lands in the FIFO, so nothing can be assumed at SRX time. */
void cc1101_enter_rx(void);
/* Switches PKTCTRL0 to fixed length mode with PKTLEN=total, mid-reception.
 * The chip's own received-byte count (tracked independently of how many
 * bytes the firmware has since drained out of the FIFO) keeps counting
 * through this switch, so calling it any time before that count reaches
 * total ends the packet at exactly the right byte and asserts GDO0's
 * packet-done edge normally; this is the standard CC1101 technique for a
 * length the radio itself cannot see in advance. */
void cc1101_set_fixed_length(uint8_t total);
uint8_t cc1101_strobe(uint8_t cmd);
uint8_t cc1101_read_status(uint8_t addr);
void cc1101_read_burst(uint8_t addr, uint8_t *buf, uint8_t len);
/* Sentinel return from cc1101_transmit for a detected TX FIFO underflow
 * (MARCSTATE 0x16): kept outside the 5-bit MARCSTATE range (0-0x1F) so the
 * caller can always tell an underflow apart from "timed out sitting in
 * MARCSTATE 0x16", which the raw marcstate value alone cannot. */
#define CC1101_TX_UNDERFLOW 0xFF

/* Sends len bytes as one fixed-length packet (preamble and sync added by the
 * chip), then re-arms RX. len <= 64 goes into the TX FIFO in one SPI burst
 * before STX, as in 3.1/3.2. len > 64 exceeds the 64-byte TX FIFO, so the
 * chip is started on a partial fill and topped up while it transmits (3.3):
 * see the implementation comment in cc1101.c for the refill sequence.
 * Returns 0 on success, CC1101_TX_UNDERFLOW if the FIFO ran dry mid-send, else
 * a MARCSTATE value that never reached idle. */
uint8_t cc1101_transmit(const uint8_t *buf, uint8_t len);

#endif
