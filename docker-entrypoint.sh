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
i=0
while [ ! -S /run/dbus/system_bus_socket ]; do
  i=$((i+1))
  [ "$i" -lt 50 ] || exit 1
  sleep 0.1
done
/opt/wiimote/bluez-4.101/dist/sbin/bluetoothd -n &
bluetooth=$!
/opt/venv/bin/gunicorn --workers 1 --threads 4 --bind 0.0.0.0:8080 --access-logfile - bridge.wsgi:app &
web=$!
cleanup() {
  kill "$web" "$bluetooth" "$bus" 2>/dev/null || true
  wait || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT
while kill -0 "$web" 2>/dev/null && kill -0 "$bluetooth" 2>/dev/null && kill -0 "$bus" 2>/dev/null; do sleep 1; done
exit 1
