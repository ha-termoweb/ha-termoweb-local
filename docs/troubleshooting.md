# Troubleshooting

Before opening an issue, turn on debug logging and read the log. Remove your installation's identifiers from anything you paste ([privacy.md](privacy.md)): debug lines contain whole frames, heater identities included.

```yaml
# configuration.yaml
logger:
  default: warning
  logs:
    custom_components.termoweb_local: debug
```

## The setup form rejects the Serial URL

- Check the path exists: `ls -l /dev/serial/by-id/` on the Home Assistant host.
- Check the stick runs termoweb_rx, not culfw: send `Q` and expect a `# Q termoweb_rx ...` line ([firmware.md](firmware.md#checking-it)).
- Check nothing else holds the port: a terminal, another integration, a second config entry.
- For `socket://` URLs, check ser2net is listening and the firewall allows the Home Assistant host ([remote-stick.md](remote-stick.md)).

## No heater is found

- **The gateway is still on.** Power it off ([installation.md](installation.md#3-take-over-from-the-gateway)).
- **The heaters are not bonded to this station.** Heaters that were never paired to a gateway, or were factory reset, are silent until paired ([configuration.md](configuration.md#pairing-a-heater)).
- **Different network id.** In the debug log, received heater frames show `rx #N: class=... src=...`. If frames arrive but none is acknowledged and the scan finds nothing, your network's id may differ from the fixed `1B 30` ([installation.md](installation.md#network-id)). Please open an issue; that case is not supported yet.
- **Range.** Move the stick closer, or onto a machine nearer the heaters ([remote-stick.md](remote-stick.md)). The `rssi=` on each received line is the signal level; below about -90 dBm reception becomes unreliable.

## A heater is missing after a restart

The first scan can miss a heater that is busy reporting. Heaters already known stay configured; a missing new one is added at its next report or by pressing **Scan for heaters**.

## A heater panel flashes LINK

The heater has not heard from the station for about 12 minutes. The integration sends every heater a keepalive every 150 s, so this means the integration is not running, the stick is not connected, or the heater is out of range. Check the gateway-online binary sensor and the log for reconnects.

## Garbled or missing frames

Only one process may hold the stick's serial port. Two readers each see part of the stream and both fail without a clear error. Stop anything else using the port: a terminal, a logging script, a second Home Assistant instance. With ser2net, `max-connections: 1` prevents this ([remote-stick.md](remote-stick.md)).

## Reported state disagrees with the heater

A heater's reported mode and duty can differ from what its element is doing, in particular after a Boost started from the panel. The `heating_while_off` binary sensor turns on when a heater reports off while its PCB temperature keeps rising. Trust the PCB temperature and the energy counter over the reported mode, and see the [safety notice](../README.md#safety-notice).

## A setpoint is not applied

Heaters have their own maximum setpoint, set from the panel or the vendor app, which can be below the 35 C the entity offers. A setpoint above it is acknowledged and then ignored; the entity follows what the heater reports. Runback, when on, also clamps the setpoint.

## Link quality always reads 0

Expected: the CC1101 does not report LQI with this firmware's receive mode. Use `rssi=` instead ([firmware/README.md](../firmware/README.md)).
