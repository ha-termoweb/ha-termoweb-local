# Firmware

`firmware/termoweb_rx` replaces culfw on the nanoCUL868. It receives the heater link, transmits frames the host sends it, and acknowledges frames addressed to the station on its own, fast enough for the heaters' timing. [firmware/README.md](../firmware/README.md) holds the design notes: the CC1101 register table, framing, transmit and the fixes in each version.

## Building

```
make -C firmware/termoweb_rx clean build PA=0xC0
```

| Variable | Default | Notes |
|---|---|---|
| `PA` | `0x50` (about 0 dBm) | Transmit power. Use `0xC0` (about +10 dBm): the default is too weak for this link. Within the sub-band's limits either way, see the [radio notice](../README.md#radio-and-spectrum-notice). |
| `PORT` | `/dev/ttyUSB0` | Serial port for `flash` and `signature`. |
| `BAUD` | `115200` | Bootloader baud rate. |
| `SYNC1`, `SYNC0`, `LEN` | `0x2D`, `0xE5`, `64` | Link constants; do not change them. |

Run `clean` whenever you change a variable: the objects do not rebuild when only a flag changes.

The firmware version is not in the source. The Makefile reads the `firmware=` line of the repository's [`version`](../version) file and compiles it into the banner; building without the Makefile stops with an error. See [releasing.md](releasing.md).

## Flashing

Nano clones ship with one of two bootloaders. Find out which, without writing anything:

```
make -C firmware/termoweb_rx signature PORT=/dev/ttyUSB0
```

Then flash at the baud rate that answered (`115200` for Optiboot, `57600` for the classic bootloader):

```
make -C firmware/termoweb_rx flash PA=0xC0 PORT=/dev/ttyUSB0 BAUD=115200
```

To flash a release's prebuilt hex without building:

```
avrdude -c arduino -p m328p -P /dev/ttyUSB0 -b 115200 -D -U flash:w:termoweb_rx-<version>-paC0.hex:i
```

Nothing else may hold the port while flashing: stop the integration, or ser2net if the stick is remote.

## Checking it

Open the port at 115200 8N1 (`python3 -m serial.tools.miniterm /dev/ttyUSB0 115200`) and send `Q`. The reply names the version, frequency, power and station id:

```
# Q termoweb_rx 3.5 freq=869.525 pa=C0 sync=2DE5 mode=dynamic autoack=off id=01
```

Received frames print as `RX <micros> <rssi> <lqi> <crc_bit> <hex>`. The full command list (`Q`, `V`, `T<hex>`, `A0`/`A1`, `I<hex>`, `X`, `D`) is in [firmware/README.md](../firmware/README.md#serial-protocol-115200-8n1). The integration sets the station id and turns auto-ack on itself after every connect.

## Restoring culfw

No culfw binary is distributed here. Build a-culfw from upstream source as described in [firmware/README.md](../firmware/README.md#restoring-culfw).
