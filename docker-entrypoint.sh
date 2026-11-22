#!/bin/sh
set -eu
: "${WEB_TOKEN:?Set WEB_TOKEN in your environment}"
WEB_PORT=${WEB_PORT:-8080}
case "$WEB_PORT" in
  ''|*[!0-9]*) echo 'WEB_PORT must be an integer from 1 to 65535' >&2; exit 1 ;;
esac
[ "$WEB_PORT" -ge 1 ] && [ "$WEB_PORT" -le 65535 ] || exit 1
mkdir -p /data/bluetooth /run/dbus /var/lib
chmod 700 /data
rm -rf /var/lib/bluetooth
ln -s /data/bluetooth /var/lib/bluetooth
dbus-uuidgen --ensure
# Private system bus: never mount the host D-Bus socket into this container.
# Both paths belong to this private bus and can survive a Docker restart.
rm -f /run/dbus/pid /run/dbus/system_bus_socket
dbus-daemon --system --nofork --nopidfile &
bus=$!
i=0
while [ ! -S /run/dbus/system_bus_socket ]; do
  i=$((i+1))
  [ "$i" -lt 50 ] || exit 1
  sleep 0.1
done
export MANAGE_BLUETOOTH=1
/opt/venv/bin/gunicorn --workers 1 --threads 4 --bind "0.0.0.0:$WEB_PORT" --access-logfile - bridge.wsgi:app &
web=$!
cleanup() {
  kill "$web" "$bus" 2>/dev/null || true
  wait || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT
while kill -0 "$web" 2>/dev/null && kill -0 "$bus" 2>/dev/null; do sleep 1; done
exit 1
