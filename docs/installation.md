# Installation

Read the [safety notice and the radio notice](../README.md#safety-notice) first. This software switches mains heaters, and the stick it runs on is a transmitter you operate.

## What you need

- **A nanoCUL868**: an [Arduino Nano](https://docs.arduino.cc/hardware/nano/) (ATmega328P, 16 MHz) wired to a [CC1101](https://www.ti.com/product/CC1101) 868 MHz module, sold ready-made under that name ([example listing](https://www.ebay.ie/itm/372221622516)). The wiring the firmware expects is in [firmware/README.md](../firmware/README.md#wiring).
- **An antenna tuned for 868 MHz.** The stock helical antenna works at room-to-room range.
- **Sun Ray RF heaters** ([manual](https://atc.ie/wp-content/uploads/Manual-atc-Sun-Ray-RF_v07.pdf)). Heaters already paired to a Termoweb gateway are found automatically. Unpaired or factory-reset heaters can be paired from the integration.
- **[Home Assistant](https://www.home-assistant.io/installation/) 2026.9.0 or newer.**
- **A machine to build and flash the firmware**, with `avr-gcc`, [`avr-libc`](https://github.com/avrdudes/avr-libc) and [`avrdude`](https://github.com/avrdudes/avrdude). On Debian or Ubuntu: `sudo apt install gcc-avr avr-libc binutils-avr avrdude make`.

## 1. Flash the stick

```
make -C firmware/termoweb_rx clean build PA=0xC0
make -C firmware/termoweb_rx flash PA=0xC0 PORT=/dev/ttyUSB0
```

Each [GitHub release](https://github.com/ha-termoweb/ha-termoweb-local/releases) also attaches a prebuilt `termoweb_rx-<version>-paC0.hex`, so you can skip the build and flash that file directly. [firmware.md](firmware.md) covers the build options, finding the bootloader baud rate, checking the flash worked and going back to culfw.

## 2. Install the integration

1. In [HACS](https://hacs.xyz/), open the menu, choose **Custom repositories** ([how](https://hacs.xyz/docs/faq/custom_repositories/)), add `https://github.com/ha-termoweb/ha-termoweb-local` with category **Integration**.
2. Install **Termoweb Local (unofficial)** and restart Home Assistant.

Without HACS, copy `custom_components/termoweb_local` from the [latest release](https://github.com/ha-termoweb/ha-termoweb-local/releases/latest)'s `termoweb_local.zip` into your Home Assistant `config/custom_components/` directory and restart. The protocol package is bundled inside it, so nothing else needs installing.

## 3. Take over from the gateway

Only one station may answer the heaters. Power the Termoweb gateway off before adding the integration, and leave it off. If both run, both acknowledge the heaters' reports and the heaters see two masters. The cloud app stops working while the gateway is off, and so does the cloud [`termoweb` integration](https://github.com/ha-termoweb/ha-termoweb) if you run it: disable it.

## 4. Add the integration

Settings -> Devices & services -> Add integration -> **Termoweb Local**.

- **Serial URL**: the stick's stable path, `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_<serial>-if00-port0`. List the candidates with `ls -l /dev/serial/by-id/` on the Home Assistant host. Do not use `/dev/ttyUSB0`, which can change number across reboots. If the stick sits on another machine, use `socket://<host>:<port>` instead ([remote-stick.md](remote-stick.md)).
- **Station id**: leave it at `01`. Heaters accept commands only from the id they were bonded to, and every gateway seen so far uses `01`.

The form sends the stick a status query before accepting, so a wrong path or unflashed stick is reported there.

On setup, the integration scans ids 2 to 65 and adds every heater that answers. A heater the first scan misses is added the next time it reports, or press the **Scan for heaters** button. Pairing new heaters, the options and the rest are in [configuration.md](configuration.md).

## Network id

The link's network id (`1B 30`) is fixed in the firmware and the protocol package. If your gateway's network uses a different one, the heaters will ignore the station: nothing is found by the scan and commands are never acknowledged. See [troubleshooting.md](troubleshooting.md#no-heater-is-found).
