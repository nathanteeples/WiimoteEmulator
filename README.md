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
   The upstream code supports only that adapter. Docker Desktop on macOS/Windows
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
the web interface. The adapter must be hci0 and available exclusively to this
container. No host D-Bus mount or privileged mode is required.

The image's default web port remains 8080; set `WEB_PORT` to change it. Update
Unraid's WebUI link alongside the variable when changing the template's port.
