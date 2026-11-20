#!/bin/sh
set -eu
: "${WEB_TOKEN:?Set WEB_TOKEN in your environment}"
mkdir -p /data/bluetooth /run/dbus /var/lib
chmod 700 /data
rm -rf /var/lib/bluetooth
ln -s /data/bluetooth /var/lib/bluetooth
dbus-uuidgen --ensure
# Private system bus: never mount the host D-Bus socket into this container.
dbus-daemon --system --nofork &
bus=$!
/opt/wiimote/bluez-4.101/dist/sbin/bluetoothd -n &
bluetooth=$!
/opt/venv/bin/python bridge/app.py &
web=$!
cleanup() {
  kill "$web" "$bluetooth" "$bus" 2>/dev/null || true
  wait || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT
while kill -0 "$web" 2>/dev/null && kill -0 "$bluetooth" 2>/dev/null && kill -0 "$bus" 2>/dev/null; do sleep 1; done
exit 1
