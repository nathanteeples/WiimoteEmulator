# Wiimote Emulator

Emulates a Bluetooth Wii controller in software.

![Raspberry Pi 3 running the emulator in Raspbian](rpi_ss.png)

### Features

  - Emulate the Wiimote's many features and extensions
  - Allows use of different input devices (keyboard etc.)

### Build/Install

The following dependencies/packages are required (if not already installed):

  - libdbus-1-dev
  - libglib2.0-dev
  - libsdl1.2-dev

Run the build script (in the project directory):

  > source ./build-custom.sh

For more information on the build script, see [this explainer](https://github.com/rnconrad/WiimoteEmulator/blob/master/CustomBuild.md).

### Using the Emulator

Stop any running Bluetooth service, e.g.:

  > sudo service bluetooth stop

Start the custom Bluetooth stack (e.g. from the project directory):

  > sudo ./bluez-4.101/dist/sbin/bluetoothd

Run the emulator (in the project directory):

  > ./wmemulator

With no arguments, the emulator will listen for incoming connections (similar to
syncing a real Wiimote). Pressing the sync button on a Wii should cause it to
connect.

You can also supply the address of a Wii to directly connect to it as long as
you have connected to it before (or you change your device's address to the
address of a trusted Wiimote).

  > ./wmemulator XX:XX:XX:XX:XX:XX

You will need to run the custom Bluetooth stack (as described above) whenever
using the emulator (it won't persist after e.g. a device restart). Also, the
custom stack generally won't be useful for anything besides Wiimote emulation.

To stop the custom stack and restore the original Bluetooth service, e.g.:

  > sudo killall bluetoothd

  > sudo service bluetooth start
## Docker power bridge

The fork adds a password-protected configuration page on port **8080**, persistent
pairing, MQTT commands/results, and Home Assistant MQTT discovery for separate
**Power on** and **Power off** buttons. No console power telemetry is available:
success means a Bluetooth connection or power-off instruction was sent, not that
the console's physical power state was verified.

### Run the published image (Linux only)

GitHub Actions builds and publishes `ghcr.io/nathanteeples/wiimoteemulator:latest`
for amd64 and arm64. Builds run on GitHub, never on your Docker host. The workflow
also runs Python tests and builds PR images without publishing them. Initial GHCR
packages may be private; set package visibility to public or authenticate Docker
with a GitHub token with `read:packages` permission.

1. Use a Linux host with a dedicated Bluetooth adapter exposed as **hci0**.
   Set HCI_DEVICE to select another adapter (for example hci1 or its MAC address). Docker Desktop on macOS/Windows
   cannot provide the required host Bluetooth sockets.
2. Stop the host Bluetooth service (`sudo systemctl stop bluetooth`) before
   starting the container. The bundled patched BlueZ owns the adapter exclusively.
   Do not mount the host D-Bus socket. The container starts its own D-Bus service.
3. Save `compose.yaml` and create `.env` alongside it with
   `WEB_TOKEN=your-long-random-password`. Run `docker compose pull` then
   `docker compose up -d`. There is intentionally no local build directive.
4. Visit `http://HOST-IP:8080`, with any username and your WEB_TOKEN as password.
   Keep this on a trusted LAN, or put an HTTPS reverse proxy in front of it.
5. Press the Wii's physical red SYNC button and click **Pair console**. Wait up
   to 90 seconds. Pairing saves the console address automatically; reload the
   page to see it. Keep the same adapter and persistent Docker volume.
6. Configure the broker address, port, optional credentials and TLS. With the
   Home Assistant MQTT integration connected to that broker, the two buttons
   appear under **Wii power bridge**.

Power on connects as the paired Wiimote. Power off first connects, then sends the
upstream HCI disconnect with reason `HCI_OE_POWER_OFF`. Neither command controls
mains power. Pairing and wake behavior depend on the adapter's chipset/firmware
and the Wii's standby mode; Bluetooth address changes required by upstream do not
work on every adapter. Check `docker compose logs -f` for connection failures.
Real Wii/adapter validation is required after installation.

The named volume stores configuration (including the broker password, mode 0600)
and BlueZ pairing keys. Back it up securely. To restore normal host Bluetooth,
stop the container, then `sudo systemctl start bluetooth`.

### MQTT API

One bridge per broker (fixed discovery IDs and client ID):

| Topic | Direction | Payload |
| --- | --- | --- |
| `wiimote/bridge/command` | Subscribe | `on` or `off`, non-retained |
| `wiimote/bridge/result` | Publish | JSON command/result; not retained |
| `wiimote/bridge/availability` | Publish | `online` / `offline`, retained with last will |
| `homeassistant/button/wiimote_bridge_{on,off}/config` | Publish | Retained discovery |

Retained commands are ignored so reconnecting cannot replay an old shutdown.
Commands are serialized and additional requests while busy are rejected. Broker
TLS verifies certificates against the container CA store. The UI never returns
the stored MQTT password; a blank password field preserves it. The bridge
republishes discovery after a Home Assistant restart.

### Development and publishing

Pushes to `master`/`main`, version tags, and manual workflow runs build and publish
using the repository's `GITHUB_TOKEN` (`packages: write`). No registry password
secret is needed. See `.github/workflows/container.yml`. Hardware pairing/power
checks cannot be performed on GitHub-hosted runners. Unit tests use mocked
Bluetooth/MQTT boundaries and run with `python -m unittest discover -s tests -v`.

### Unraid

Use `unraid/my-Wii-Power-Bridge.xml` in Unraid's user templates directory:
`/boot/config/plugins/dockerMan/templates-user/`.
Choose **Docker → Add Container → Wii-Power-Bridge**, enter a strong `WEB_TOKEN`,
and apply. The template uses host port **8087** (`WEB_PORT`) to avoid a port 8080
conflict on NTnet, and stores pairing/configuration under
`/mnt/user/appdata/wiimote-power-bridge`. Configure console pairing and MQTT in
the web interface. The adapter selected by HCI_DEVICE must be available exclusively to this
container. No host D-Bus mount or privileged mode is required.

The image's default web port remains 8080; set `WEB_PORT` to change it. Update
Unraid's WebUI link alongside the variable when changing the template's port.

Set `HCI_DEVICE=hci1` to use a second adapter. Pairing listeners, outgoing connections, and power-off HCI commands are bound to that adapter; missing adapters fail instead of falling back to another controller. A Bluetooth MAC address can also select the adapter across changes in HCI numbering.

### Remote USB adapter on rooted ONN

For an adapter located near the console rather than the server, see
[`relay/README.md`](relay/README.md). The rooted Android relay and Unraid client
provide certificate-authenticated transport, stable controller identity, USB
hotplug recovery, heartbeats, and startup supervisors. The Android executable is
built separately in GitHub Actions. Pairing timeout defaults to five minutes and
can be changed with `PAIR_TIMEOUT`.

### Wii U wake behavior

Power on uses the dedicated `wmwake` helper. A standby Wii U accepts an
authenticated Bluetooth ACL connection but does not answer the normal HID channel
request. The helper first checks for an awake HID/L2CAP host; if it answers,
no power-button signal is sent. Otherwise, after authenticating the saved bond,
it sends the Wiimote power-button disconnect reason `0x15` and waits up to 35
seconds for the console's HID channel to become available. A recent unconfirmed
wake is not repeated for 60 seconds, including across a container restart, to
avoid toggling a console that is still booting. Power off retains the existing
connected-controller shutdown path. Power confirmation is based on Bluetooth
service availability rather than direct measurement of the front-panel LED.

### Multiple consoles and Nintendo Switch

Save a separate labeled profile for each Wii, Wii U, or Nintendo Switch in the web
interface, then use **Pair console** on that profile. Pairing learns the address
without replacing another profile. For Wii U use **Wii Remote/Other Controllers →
Pair**; for Switch open **Controllers → Change Grip/Order**. Existing single-console
settings migrate to a Wii U profile, preserving the address, MQTT password, and bonds.
Keep the ONN relay's controller address stable: each console bonds to that identity.

Each profile shows its stable MQTT ID. Publish a non-retained payload to:

```
wiimote/bridge/consoles/<id>/command
```

Wii / Wii U accept `on`, `off`, and `pair`. Switch accepts `home`, `on`, `wake`, and
`pair` (`on` and `wake` send HOME). Subscribe to the corresponding `/result` topic
for the result. Home Assistant discovers separate buttons named for each console;
renaming keeps their IDs stable and deleting a profile removes its discovery entries.
The original `wiimote/bridge/command` and original Home Assistant buttons still target
the selected default console. Commands are serialized and retained commands ignored.

Switch emulation uses [joycontrol](https://github.com/mart1nro/joycontrol), pinned to
`18a09da1a04306534ff9e1df8a1a69c0192a3244`, distributed under GPL-3.0 with its license
in `/usr/share/doc/joycontrol/LICENSE`. It emulates a Pro Controller on the configured
adapter, including during reconnection. Switch power off and Switch 2 compatibility
are not implemented. HOME is asserted during reconnect and sent again after the
controller handshake; a successful result confirms HID delivery, **not physical wake**.
Upstream [wake support remains unresolved](https://github.com/mart1nro/joycontrol/issues/59),
so sleep wake must be verified with the actual Switch and firmware.

For speed, the selected BlueZ daemon stays running between commands. Switch HOME
commands reuse their active HID session, avoiding another connection and handshake.
Changing from Wii to Switch replaces only the container's private Bluetooth daemon;
the persistent ONN USB relay stays connected. BlueZ 4 Wii bonds and BlueZ 5 Switch
bonds are stored separately under `/data/bluetooth` and `/data/bluez5`. The adapter is
shared, so only one command/session can own it at a time. Wii wake intentionally
keeps its five-second authenticated-link check to avoid toggling an awake console off.

Switch compatibility notes: modern Switch firmware checks the controller class and
rejects excessive report rates in pairing/HOME menus. The Switch daemon disables
BlueZ's `hostname` plugin (which otherwise classifies the Unraid server as a desktop),
uses its own configuration, and sets Gamepad through MGMT before HCI. This keeps
BlueZ's cached class and radio advertisement consistent. Startup reports use 1 Hz;
controller reports are limited to 15 Hz. See the upstream
[firmware compatibility discussion](https://github.com/mart1nro/joycontrol/issues/104).
The pairing log shows connection attempts and authentication results using a
read-only HCI event filter that excludes link-key notifications.
