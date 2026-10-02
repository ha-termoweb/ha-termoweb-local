#include <avr/io.h>
#include <util/delay.h>
#include "cc1101.h"

#define CS_DDR  DDRB
#define CS_PORT PORTB
#define CS_PIN  PB2

static void cs_low(void) {
    CS_PORT &= ~(1 << CS_PIN);
    /* CC1101 datasheet 10.1: after CS goes low, wait for MISO low before
     * clocking, so the chip has time to leave SLEEP/XOFF and settle. */
    while (PINB & (1 << PB4)) {
    }
}

static void cs_high(void) {
    CS_PORT |= (1 << CS_PIN);
}

static uint8_t spi_xfer(uint8_t v) {
    SPDR = v;
    while (!(SPSR & (1 << SPIF))) {
    }
    return SPDR;
}

static void spi_init(void) {
    DDRB |= (1 << PB3) | (1 << PB5);   /* MOSI, SCK out */
    DDRB &= ~(1 << PB4);               /* MISO in */
    CS_DDR |= (1 << CS_PIN);
    cs_high();
    /* Mode 0, MSB first, fosc/4 (4 MHz at 16 MHz F_CPU): well under the
     * CC1101's 10 MHz SPI limit with margin for the Nano's wiring. */
    SPCR = (1 << SPE) | (1 << MSTR);
    SPSR = 0;
}

uint8_t cc1101_strobe(uint8_t cmd) {
    cs_low();
    uint8_t status = spi_xfer(cmd);
    cs_high();
    return status;
}

static void write_reg(uint8_t addr, uint8_t val) {
    cs_low();
    spi_xfer(addr);
    spi_xfer(val);
    cs_high();
}

static void write_burst(uint8_t addr, const uint8_t *buf, uint8_t len) {
    cs_low();
    spi_xfer(addr | CC1101_WRITE_BURST);
    for (uint8_t i = 0; i < len; i++) {
        spi_xfer(buf[i]);
    }
    cs_high();
}

uint8_t cc1101_read_status(uint8_t addr) {
    cs_low();
    spi_xfer(addr | CC1101_READ_BURST);
    uint8_t v = spi_xfer(0);
    cs_high();
    return v;
}

void cc1101_read_burst(uint8_t addr, uint8_t *buf, uint8_t len) {
    cs_low();
    spi_xfer(addr | CC1101_READ_BURST);
    for (uint8_t i = 0; i < len; i++) {
        buf[i] = spi_xfer(0);
    }
    cs_high();
}

/* Register table for the Termoweb link: 869.54 MHz, 2-FSK, 9.6 kbps,
 * ~270 kHz RX filter BW, ~50 kHz deviation, 16-bit sync, fixed length,
 * CRC and whitening off (see firmware/README.md for the full derivation).
 *
 * Registers not tied to a measured link parameter (FOCCFG, BSCFG, AGCCTRL*,
 * FREND1, FSCAL*, MCSM0, FSTEST, MDMCFG1/0, CHANNR, ADDR, FSCTRL0, IOCFG2)
 * use the generic 2-FSK baseline from the widely deployed ELECHOUSE/SmartRC
 * CC1101 driver (github.com/LSatan/SmartRC-CC1101-Driver-Lib), cross-checked
 * against the CC1101 datasheet: its TEST2/TEST1/TEST0/PKTCTRL1/FSCTRL1
 * values match the datasheet's own BW<325kHz guidance exactly, which is why
 * that baseline was trusted for the values the datasheet does not pin down.
 * SmartRF Studio itself needs Windows and was not available on this host. */
struct reg_val {
    uint8_t addr;
    uint8_t val;
};

static const struct reg_val CONFIG[] = {
    /* GDO2 now drives the RX-FIFO-threshold interrupt (INT0): the frame's
     * own first byte is unreadable until it lands in the FIFO, so this is
     * what lets the ISR grab it as soon as it arrives, ahead of knowing the
     * frame's total length. GDO2 doubles as the TX FIFO threshold during
     * transmit, unused there since INT0 is masked around cc1101_transmit. */
    {CC1101_IOCFG2,   0x00},
    {CC1101_IOCFG0,   0x06}, /* GDO0 (INT1) asserts on sync detect, deasserts at the true end of packet once PKTLEN is switched in mid-reception */
    {CC1101_FIFOTHR,  0x40}, /* ADC_RETENTION (required for BW<=325kHz) + RX_ATTHRESH=0 -> 4-byte RX threshold, for the fastest possible read of the length byte */
    {CC1101_SYNC1,    SYNC1_VAL},
    {CC1101_SYNC0,    SYNC0_VAL},
    {CC1101_PKTLEN,   PKT_LEN}, /* don't-care while LENGTH_CONFIG=2 below; only meaningful once cc1101_set_fixed_length rewrites it per frame */
    {CC1101_PKTCTRL1, 0x04}, /* APPEND_STATUS on: RSSI and LQI/CRC_OK follow every FIFO read */
    {CC1101_PKTCTRL0, 0x02}, /* infinite packet length mode (LENGTH_CONFIG=2): the true length isn't known until byte 0 arrives, so nothing can be fixed at SRX time. CRC off (unknown, so left off rather than rejecting real frames), whitening off (unknown) */
    {CC1101_ADDR,     0x00}, /* address filtering unused */
    {CC1101_CHANNR,   0x00}, /* carrier set directly via FREQx, no channel hopping */
    {CC1101_FSCTRL1,  0x06}, /* IF frequency, standard for this family of data rates */
    {CC1101_FSCTRL0,  0x00}, /* no frequency offset */
    /* FREQ = round(869.54e6 * 2^16 / 26e6) = 0x2171A0 -> 869.540039 MHz */
    {CC1101_FREQ2,    0x21},
    {CC1101_FREQ1,    0x71},
    {CC1101_FREQ0,    0x7A}, /* 869.525 MHz, the gateway label value; was 0xA0 = 869.540 */
    /* CHANBW_E=1,M=2 -> 26e6/(8*6*2) = 270.8 kHz; DRATE_E=8 nibble kept from culfw HomeMatic */
    {CC1101_MDMCFG4,  0x68},
    /* DRATE_M=147 with E=8 -> (256+147)*2^8*26e6/2^28 = 9992.6 bps, culfw HomeMatic mantissa */
    {CC1101_MDMCFG3,  0x83}, /* 9.6 kbps: Flipper measured 104 us per bit; 0x93 (9.99 kbps) duplicated one bit per 25 */
    {CC1101_MDMCFG2,  0x02}, /* 2-FSK, no Manchester, 16/16 sync word detect */
    {CC1101_MDMCFG1,  0x42}, /* TX preamble 8 bytes (NUM_PREAMBLE=100); chan spacing exponent 2 */
    {CC1101_MDMCFG0,  0xF8}, /* channel spacing mantissa, irrelevant to RX */
    /* DEVIATN_E=5,M=0 -> 26e6/2^17*8*2^5 = 50.8 kHz, matches the measured ~50 kHz */
    {CC1101_DEVIATN,  0x50},
    {CC1101_MCSM1,    0x30}, /* reset default: IDLE after packet; firmware re-arms RX itself */
    {CC1101_MCSM0,    0x18}, /* auto-calibrate on IDLE->RX so every SRX retunes cleanly */
    {CC1101_FOCCFG,   0x16},
    {CC1101_BSCFG,    0x1C},
    {CC1101_AGCCTRL2, 0xC7},
    {CC1101_AGCCTRL1, 0x00},
    {CC1101_AGCCTRL0, 0xB2},
    {CC1101_FREND1,   0x56},
    {CC1101_FREND0,   0x11}, /* PA table index, TX only, unused by this RX-only firmware */
    {CC1101_FSCAL3,   0xE9},
    {CC1101_FSCAL2,   0x2A},
    {CC1101_FSCAL1,   0x00},
    {CC1101_FSCAL0,   0x1F},
    {CC1101_FSTEST,   0x59}, /* TI-documented test register default */
    {CC1101_TEST2,    0x81}, /* required for RX filter BW <= 325 kHz */
    {CC1101_TEST1,    0x35}, /* required for RX filter BW <= 325 kHz */
    {CC1101_TEST0,    0x09}, /* required, enables VCO_SEL_CAL_EN */
};

void cc1101_init(void) {
    spi_init();

    cs_low();
    cs_high();
    _delay_us(40);
    cc1101_strobe(CC1101_SRES);
    /* SRES needs the chip's crystal running again; MISO drops low once it is,
     * which cs_low() on the next transfer already waits for. */
    _delay_ms(2);

    for (uint8_t i = 0; i < sizeof(CONFIG) / sizeof(CONFIG[0]); i++) {
        write_reg(CONFIG[i].addr, CONFIG[i].val);
    }
    cc1101_strobe(CC1101_SCAL);
    _delay_ms(1);

    uint8_t pa[2] = {PA_VAL, PA_VAL};
    write_burst(CC1101_PATABLE, pa, 2);
}

/* TXFIFO_UNDERFLOW per the CC1101 MARCSTATE table (datasheet Table 33). */
#define MARCSTATE_TXFIFO_UNDERFLOW 0x16

/* The 64-byte TX FIFO's own byte-count status register (TXBYTES) is known to
 * read back 0, indistinguishable from empty, when the FIFO is completely
 * full (a documented CC1101 silicon erratum); the initial fill for a
 * len > 64 send is therefore kept to 63 bytes rather than 64, so the first
 * post-STX TXBYTES read is never taken during that ambiguous instant. */
#define CC1101_TX_FIFO_SIZE 64
#define CC1101_TX_INITIAL_FILL 63
#define CC1101_TX_REFILL_THRESHOLD 32

static uint8_t cc1101_tx_recover(void) {
    cc1101_strobe(CC1101_SFTX);
    cc1101_enter_rx();
    return CC1101_TX_UNDERFLOW;
}

uint8_t cc1101_transmit(const uint8_t *buf, uint8_t len) {
    cc1101_strobe(CC1101_SIDLE);
    cc1101_strobe(CC1101_SFTX);
    /* RX may currently be in infinite-length mode (LENGTH_CONFIG=2); TX
     * always needs fixed-length framing, so force it explicitly rather
     * than assume whatever mode RX last left the chip in. */
    write_reg(CC1101_PKTCTRL0, 0x00);
    write_reg(CC1101_PKTLEN, len);

    if (len <= CC1101_TX_FIFO_SIZE) {
        write_burst(CC1101_FIFO_ADDR, buf, len);
        cc1101_strobe(CC1101_STX);
    } else {
        write_burst(CC1101_FIFO_ADDR, buf, CC1101_TX_INITIAL_FILL);
        cc1101_strobe(CC1101_STX);
        uint8_t sent = CC1101_TX_INITIAL_FILL;
        /* Every other wait in this driver is bounded (the idle-wait loop
         * below, uart_getc_blocking's timeout_us): a refill that never makes
         * progress, e.g. STX failing to bring the chip into TX, must not
         * spin forever either. */
        for (uint16_t poll = 0; sent < len; poll++) {
            if (poll >= 2000) {
                uint8_t stuck = cc1101_read_status(CC1101_MARCSTATE) & 0x1F;
                cc1101_strobe(CC1101_SFTX);
                cc1101_enter_rx();
                return stuck;
            }
            uint8_t txbytes = cc1101_read_status(CC1101_TXBYTES);
            if (txbytes & 0x80) {
                return cc1101_tx_recover();
            }
            uint8_t occupied = txbytes & 0x7F;
            if (occupied < CC1101_TX_REFILL_THRESHOLD) {
                /* -1 keeps this refill under the same completely-full
                 * ambiguity as the initial fill above. */
                uint8_t space = CC1101_TX_FIFO_SIZE - occupied - 1;
                uint8_t remaining = len - sent;
                uint8_t chunk = remaining < space ? remaining : space;
                if (chunk > 0) {
                    write_burst(CC1101_FIFO_ADDR, buf + sent, chunk);
                    sent += chunk;
                }
            }
            _delay_us(100);
        }
    }

    uint8_t state = 0;
    for (uint16_t i = 0; i < 2000; i++) {
        _delay_us(100);
        state = cc1101_read_status(CC1101_MARCSTATE) & 0x1F;
        if (state == 0x01) {
            break;
        }
        if (state == MARCSTATE_TXFIFO_UNDERFLOW) {
            return cc1101_tx_recover();
        }
    }
    cc1101_enter_rx();
    return state == 0x01 ? 0 : state;
}

void cc1101_enter_rx(void) {
    cc1101_strobe(CC1101_SIDLE);
    cc1101_strobe(CC1101_SFRX);
    write_reg(CC1101_PKTCTRL0, 0x02); /* back to infinite length mode for the next, as yet unknown, frame length */
    cc1101_strobe(CC1101_SRX);
}

void cc1101_set_fixed_length(uint8_t total) {
    write_reg(CC1101_PKTLEN, total);
    write_reg(CC1101_PKTCTRL0, 0x00); /* fixed length mode; see the header comment on this function for why this is safe mid-reception */
}
