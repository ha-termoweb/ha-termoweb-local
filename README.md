# Termoweb Local (unofficial)

Local, cloud-free control of Sun Ray RF electric radiators from Home Assistant. A nanoCUL868 (CC1101) USB stick running the firmware in this repository talks to the heaters directly over their own 869.525 MHz radio link, replacing the Termoweb Smart App Gateway and its cloud.

## Independence and trade marks

This is an independent, unofficial project. It is not affiliated with, endorsed by, or supported by Casple S.A., ATC (ATC Electrical and Mechanical), the Electric Heating Company, Tevolve or Helki. Termoweb, Tevolve, Helki, Casple and Sun Ray are trade marks of their respective owners and are used here only to describe compatibility. No vendor logo or artwork is used anywhere in this repository.

The protocol was worked out from the radio traffic of the author's own devices. This repository contains no vendor firmware, no vendor documents and no decompiled vendor code.

## Safety notice

This software switches mains-powered electric heaters in people's homes. Read this before using it.

- The state a heater reports can differ from what it is physically doing. In one observed case a heater ran its element for about fifty minutes after a Boost started at its own panel, while reporting itself off with zero duty. The integration's `heating_while_off` binary sensor watches the heater's PCB temperature for this. Check the PCB temperature and the energy counter after any panel session before trusting the reported mode.
- Keep every heater's own thermal cut-outs, panel controls and physical isolation in place. This software is not a safety device and must never be relied on to turn a heater off.
- Do not leave heaters unattended on the strength of this software alone, especially while you are first setting it up.
- The software is provided as is, without warranty of any kind (see `LICENSE`).

## Radio and spectrum notice

The firmware transmits. It is written for the European 869.4 to 869.65 MHz short-range-device sub-band only, and must not be operated anywhere that allocation does not exist.

| Parameter | Value |
|---|---|
| Carrier | 869.525 MHz |
| Modulation | 2-FSK, about 50.8 kHz deviation |
| Bit rate | 9.6 kbps |
| Output power | CC1101 PATABLE `0xC0`, about +10 dBm (about 10 mW) at 868 MHz, set at build time with `PA=0xC0` |
| Sub-band limits | 500 mW e.r.p., duty cycle at most 10 percent (or listen-before-talk with adaptive frequency agility) |

In Ireland these conditions are ComReg Document 02/71, Table 1 entry 15i, under the Short Range Devices exemption order (S.I. No. 405 of 2002 as amended by S.I. No. 160 of 2006); across the EU they are band 54 of Commission Decision 2006/771/EC. The same band and limits apply under ERC/REC 70-03 elsewhere in CEPT; check your own national rules.

Duty cycle, estimated rather than enforced: a frame of n bytes takes (8 preamble + 2 sync + n) x 8 / 9600 s on air, so a typical 16 to 20 byte command or confirmation takes 22 to 25 ms and an 8-byte link ack 15 ms. A station running three heaters sends, per hour, about 72 link keepalives (one per heater every 150 s), 36 status requests (one per heater every 300 s by default), a few dozen report confirmations, one link ack per received heater frame and one energy read per heater, so a few hundred frames at most: about 400 x 25 ms = 10 s of transmit time per hour, about 0.3 percent, against the 360 s (10 percent) the sub-band allows. Neither the firmware nor the integration enforces a transmit-time limit today, so a bug or a misuse could exceed it; a duty-cycle guard is planned. Do not script the stick to transmit in a loop.

Whoever flashes and runs the stick is the operator of the transmitter and is responsible for operating it within these limits.

## What is in this repository

```
custom_components/termoweb_local/   Home Assistant custom integration (HACS)
  vendor/termoweb_local/            the radio protocol package the integration uses (frame codec, nanoCUL serial client, heater model, network logic)
  frontend/                         the weekly schedule editor Lovelace card
firmware/termoweb_rx/               nanoCUL868 firmware (ATmega328P + CC1101): receives, transmits and auto-acks the heater link
hacs.json                           HACS metadata
```

## Requirements

- A nanoCUL868 (Arduino Nano clone with a CC1101 868 MHz module), flashed with `firmware/termoweb_rx`. See `firmware/README.md`.
- Sun Ray RF heaters already paired to a Termoweb gateway, or heaters you are prepared to pair to this station from the integration.
- Home Assistant 2026.9.0 or newer.

## Install

1. Build and flash the firmware: `make -C firmware/termoweb_rx clean build PA=0xC0`, then `make -C firmware/termoweb_rx flash PA=0xC0 PORT=/dev/ttyUSB0`. `clean` is needed whenever a build flag changes, and the Makefile's own `PA` default (`0x50`, 0 dBm) is too weak for this link.
2. In HACS, add `https://github.com/ha-termoweb/ha-termoweb-local` as a custom repository (category Integration), install "Termoweb Local (unofficial)" and restart Home Assistant.
3. Settings -> Devices & services -> Add integration -> "Termoweb Local". Give it the stick's `/dev/serial/by-id/...` path, or a `socket://host:port` URL if the stick sits on another machine behind ser2net.
4. Power the Termoweb gateway off. Only one station should answer the heaters, and only one process may hold the stick's serial port.

Heaters already bonded to the gateway are found by the integration's scan; others are added with its Pair heater button. `custom_components/termoweb_local/README.md` describes the entities, services, schedule card and pairing.

## Known limitations

- The network id (`1B 30`) and the station id (`01`) are fixed in the firmware and the protocol package, matching the one installation this was developed against. Whether another gateway's network uses the same id is not known yet; if yours differs, the heaters will ignore the station until the id is made configurable.
- Developed and tested against three heaters (one Sun Ray RF 1800 and two Sun Ray RF 750) on a single installation.
- The CC1101's own LQI status byte reads 0 on every frame with this firmware, so link quality is reported from RSSI only. Frame integrity is checked by the host-side CRC.

## Licence

Apache-2.0, see `LICENSE`.
