#!/system/bin/sh
# Magisk late-start service. BusyBox owns the lock across the supervisor child.
BASE=/data/adb/wiimote-relay
if [ "$1" != --supervise ]; then
  /data/adb/magisk/busybox flock -n "$BASE/supervisor.lock" \
    /system/bin/sh "$0" --supervise </dev/null >/dev/null 2>&1 &
  exit 0
fi
while [ "$(getprop sys.boot_completed)" != 1 ]; do sleep 5; done
mkdir -p "$BASE/logs"
while true; do
  [ -f "$BASE/disabled" ] && exit 0
  if [ -f "$BASE/logs/relay.log" ] && [ "$(wc -c < "$BASE/logs/relay.log")" -gt 1000000 ]; then
    mv -f "$BASE/logs/relay.log" "$BASE/logs/relay.previous.log"
  fi
  "$BASE/onn-usb-relay" "$BASE" 18765 00:1E:35:DA:71:16 >> "$BASE/logs/relay.log" 2>&1
  sleep 3
done
