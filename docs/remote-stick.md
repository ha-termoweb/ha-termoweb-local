# Running the stick on another machine

Plugging the stick into the Home Assistant host is the simplest setup. If that host is in the wrong place for radio range, or is a virtual machine without USB passthrough, put the stick on any Linux machine nearer the heaters and share its serial port over TCP with ser2net. The integration reaches it with a `socket://` URL; nothing else changes.

## ser2net configuration

Install ser2net 4.x (`sudo apt install ser2net`) and put this in `/etc/ser2net.yaml`, replacing the address and the device path with your own:

```yaml
%YAML 1.1
---
connection: &nanocul868
    accepter: tcp,192.0.2.10,5555
    connector: serialdev,/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_XXXXXXXX-if00-port0,115200n81,local
    options:
        max-connections: 1
        kickolduser: false
```

- `192.0.2.10` is a placeholder for the stick machine's LAN address. Binding to that address rather than `0.0.0.0` keeps the port off other interfaces.
- `tcp` (not `telnet`) gives a raw byte stream with no option negotiation.
- `local` tells ser2net to ignore the modem control lines, so a TCP connection never toggles DTR and never resets the stick. The integration does not need the reset: it configures the stick after every connect.
- `max-connections: 1` enforces the rule that only one client may hold the stick ([troubleshooting.md](troubleshooting.md#garbled-or-missing-frames)).

Then:

```
sudo systemctl enable --now ser2net
ss -tlnp | grep 5555
```

Allow only the Home Assistant host through the firewall, for example `sudo ufw allow from 192.0.2.20 to any port 5555 proto tcp`. The bridge has no authentication: anyone who can reach the port can drive your heaters.

## The integration's Serial URL

```
socket://192.0.2.10:5555
```

## Restarts

When ser2net restarts, the integration's connection drops and it reconnects with a backoff from 5 s up to 60 s, resynchronising every heater when it is back. Automatic upgrades can restart ser2net. On Debian and Ubuntu, `needrestart` restarts services after library upgrades; to stop it restarting ser2net, add `/etc/needrestart/conf.d/90-ser2net.conf` containing:

```
$nrconf{override_rc}{qr(^ser2net)} = 0;
```

## Privacy

The by-id path contains your stick's USB serial number. Replace it with `XXXXXXXX` before pasting your config anywhere public ([privacy.md](privacy.md)).
