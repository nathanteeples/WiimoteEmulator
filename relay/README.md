# Persistent ONN USB Bluetooth relay

The dedicated TP-Link UB400 stays on a rooted ONN 4K Pro near the console. A
small Android executable forwards only that adapter's HCI/ACL traffic over
mutually authenticated TLS to a Linux `/dev/vhci` controller. The existing Docker
container uses that controller through host networking. The ONN's built-in
Bluetooth controller and Android Bluetooth service are not replaced.

## Build and identity

`.github/workflows/onn-relay.yml` builds the Android ARMv7 executable on GitHub
using the Android NDK and checksum-verified Mbed TLS 3.6.8 LTS. The workflow
uploads `onn-usb-relay-armv7`, including its checksum and the Mbed TLS license.
No Android or Docker builds are needed on the deployment machines.

The executable accepts `DATA_DIR PORT CONTROLLER_ADDRESS`. It claims USB
interface 0 only for VID `0a12`, PID `0001`, and reapplies the configured Nintendo
address using CSR PSRAM and warm reset when necessary. That avoids flash writes
and keeps the identity consistent across USB power loss. Keep the same identity
as the console's pairing record and the container's persistent BlueZ keys.

H4 packets use a two-byte big-endian length prefix. An empty frame is a heartbeat.
After TLS authentication and USB setup the server sends `WMR1` followed by its
six-byte Bluetooth address in controller byte order. The Linux client verifies
that identity before creating a virtual HCI device. A disconnected transport
removes that virtual device and reconnects automatically; heartbeats detect
silent network failures. Audio SCO/ISO transport is not supported. The relay
acknowledges VHCI's mandatory synchronous-audio flow-control initialization
command because the UB400 does not implement it; Wii Remote data uses ACL.

## Installed layout

ONN (root only):

- `/data/adb/wiimote-relay/onn-usb-relay`
- `/data/adb/wiimote-relay/tls/{ca.crt,server.crt,server.key}`
- `/data/adb/service.d/wiimote-relay.sh` (from `onn-service.sh`)
- `/data/adb/wiimote-relay/logs/relay.log`

The Magisk service starts after Android boot, holds a supervisor lock, and
restarts the relay after a process failure. The native server retries USB
initialization for each authenticated connection, allowing unplug/replug.

Unraid (root only):

- `/boot/config/plugins/wiimote-relay/{unraid_vhci_client.py,unraid-start.sh,config.json}`
- `/boot/config/plugins/wiimote-relay/tls/{ca.crt,client.crt,client.key}`
- a background startup hook in `/boot/config/go`
- `/var/log/wiimote-relay.log` (size-limited, two backups)
- `/var/run/wiimote-relay/status.json`

Example client config:

```json
{
  "host": "192.168.1.50",
  "port": 18765,
  "adapter": "hci1",
  "controller_address": "00:1E:35:DA:71:16"
}
```

Issue separate server and client certificates from a private relay-only CA.
Require `serverAuth` for the server and `clientAuth` for the client. The server
certificate must include `DNS:wiimote-onn`; the client verifies this name as well
as the CA, regardless of the configured IP. Do not put keys in this repository.
Use mode 0600 for keys and mode 0700 for their directories. Reserve the ONN's LAN
IP in DHCP, or update `config.json` if its address changes. Only the encrypted
certificate-authenticated TCP port is exposed; no raw HCI TCP listener is used.

Start the host client with `nohup sh /boot/config/plugins/wiimote-relay/unraid-start.sh
</dev/null >/var/log/wiimote-relay-start.log 2>&1 &` from the Unraid boot script.
The supervisor waits for Python and `/dev/vhci`, and uses a lock to avoid duplicate
clients. Set the container's `HCI_DEVICE` to the configured virtual controller,
use host networking with NET_ADMIN/NET_RAW, and retain `/data` across updates.
Set its restart policy to `unless-stopped` for Docker startup recovery. Configure
the console address in the web UI; paired keys remain in the container's appdata.

The container publishes Nintendo's name and HID service in both legacy name
requests and extended inquiry response, and supports both limited and general
inquiry codes when the controller permits it. This prevents the host's old name
from being advertised while emulating a controller.

## Operations

Check `status.json`, both logs, and `hci1` after startup. To pause the client,
create `/boot/config/plugins/wiimote-relay/disabled` and stop the Python client.
To pause Android, create `/data/adb/wiimote-relay/disabled` and stop
`onn-usb-relay`. Remove the marker and rerun the corresponding startup script to
resume. Before unplugging a test setup, stop any emulator command cleanly.

For complete removal, stop both services, remove only the marked relay startup
block from `/boot/config/go`, and remove the dedicated Magisk service script.
Keep a secure backup of certificates and BlueZ pairing data if moving the setup.
